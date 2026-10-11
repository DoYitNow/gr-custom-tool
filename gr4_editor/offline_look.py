"""Restricted, approximate enhanced-Look rendering without Adobe applications.

Input is LibRaw camera-WB/BaselineExposure corrected *linear sRGB*, not the
Adobe Standard camera rendering. Output is display sRGB. This implements the
supplied global color tables, but does not reproduce Lightroom/Camera Raw.

Format/order references: Adobe Enhanced Profiles SDK (April 2018), and Adobe
DNG SDK dng_big_table, dng_reference and dng_render. XMP RGBTable primaries
use the SDK enum (ProPhoto=2), NOT the DNG RGBTables tag enum (ProPhoto=4).
https://www.adobe.com/support/downloads/dng/dng_sdk.html
This product includes DNG technology under license by Adobe.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
from pathlib import Path
import struct
from xml.etree import ElementTree as ET

import numpy as np

from .calibration_assets import load_sdk_tone
from .offline_raw import RAW_PARAMETERS, TARGET_RENDER_SETTINGS

from .numeric.xmp import CRS, RDF, decode_table


RECIPE_VERSION = 3
CURVES = ('ToneCurvePV2012', 'ToneCurvePV2012Red', 'ToneCurvePV2012Green', 'ToneCurvePV2012Blue')
PRIMARIES = ('sRGB', 'Adobe RGB', 'ProPhoto RGB', 'Display P3', 'Rec.2020')
TRANSFERS = ('linear', 'sRGB', '1.8', '2.2', 'Rec.2020')
_METADATA = {
    'PresetType', 'Cluster', 'UUID', 'Copyright', 'ContactInfo', 'CameraModelRestriction',
    'Version', 'CompatibleVersion', 'ProcessVersion', 'HasSettings', 'RequiresRGBTables',
}
_TEXT_NODES = {'Name', 'ShortName', 'SortName', 'Group', 'Description'}
_LIMITATIONS = [
    '近似离线颜色转换；未重建 Adobe Standard 相机配置、Lightroom Develop 引擎或其默认色调。',
    '输入为 LibRaw 线性 sRGB 代理；不等同相机 ISP 输入，机内效果仍须实拍验证。',
    'XMP 点曲线在 sRGB 编码的 ProPhoto 中作分段线性插值；不是 Adobe 曲线引擎。',
]


def _xyz(xy):
    x, y = xy
    return np.array([x / y, 1., (1. - x - y) / y])


def _matrix(primaries, white):
    columns = np.column_stack([_xyz(point) for point in primaries])
    return columns * np.linalg.solve(columns, _xyz(white))


_D65, _D50 = (.3127, .3290), (.3457, .3585)
_BRADFORD = np.array([[.8951, .2664, -.1614], [-.7502, 1.7135, .0367], [.0389, -.0685, 1.0296]])
_ADAPT = np.linalg.inv(_BRADFORD) @ np.diag((_BRADFORD @ _xyz(_D50)) / (_BRADFORD @ _xyz(_D65))) @ _BRADFORD
_CHROMATICITIES = (
    ((.64, .33), (.30, .60), (.15, .06)),
    ((.64, .33), (.21, .71), (.15, .06)),
    ((.7347, .2653), (.1596, .8404), (.0366, .0001)),
    ((.68, .32), (.265, .69), (.15, .06)),
    ((.708, .292), (.170, .797), (.131, .046)),
)
_TO_PCS = tuple(_matrix(p, _D50) if i == 2 else _ADAPT @ _matrix(p, _D65)
                for i, p in enumerate(_CHROMATICITIES))


def _convert(rgb, source, destination):
    if source == destination:
        return rgb
    transform = np.linalg.solve(_TO_PCS[destination], _TO_PCS[source])
    return rgb @ transform.T


def _encode(value, kind=1):
    value = np.maximum(np.asarray(value), 0.)
    if kind == 0:
        return value
    if kind == 1:
        return np.where(value <= .0031308, 12.92 * value, 1.055 * value ** (1. / 2.4) - .055)
    if kind in (2, 3):
        power = 1. / (1.8 if kind == 2 else 2.2)
        join = 2. * 32. ** (1. / (power - 1.))
        t = np.minimum(value / join, 1.)
        end, slope = join ** power, power * join ** (power - 1.)
        # Hermite toe joins a finite slope of 32 to the power-law segment.
        toe = (t ** 3 - 2. * t ** 2 + t) * join * 32.
        toe += (-2. * t ** 3 + 3. * t ** 2) * end + (t ** 3 - t ** 2) * join * slope
        return np.where(value <= join, toe, value ** power)
    # Rec.2020 12-bit transfer constants; also used by the DNG SDK gamma enum.
    alpha, beta = 1.09929682680944, .018053968510807
    return np.where(value < beta, 4.5 * value, alpha * value ** .45 - (alpha - 1.))


def _decode(value, kind=1):
    value = np.maximum(np.asarray(value), 0.)
    if kind == 0:
        return value
    if kind == 1:
        return np.where(value <= .0031308 * 12.92, value / 12.92, ((value + .055) / 1.055) ** 2.4)
    if kind in (2, 3):
        gamma = 1.8 if kind == 2 else 2.2
        join = 2. * 32. ** (1. / (1. / gamma - 1.))
        linear = value ** gamma
        selected = value < join ** (1. / gamma)
        low, high = np.zeros_like(value[selected]), np.full_like(value[selected], join)
        for _ in range(32):
            middle = (low + high) / 2.
            above = _encode(middle, kind) >= value[selected]
            high, low = np.where(above, middle, high), np.where(above, low, middle)
        linear[selected] = (low + high) / 2.
        return linear
    alpha, beta = 1.09929682680944, .018053968510807
    return np.where(value < 4.5 * beta, value / 4.5, ((value + alpha - 1.) / alpha) ** (1. / .45))


def _rgb_to_hsv(rgb):
    maximum, minimum = rgb.max(axis=-1), rgb.min(axis=-1)
    delta = maximum - minimum
    divisor = np.where(delta > 0, delta, 1.)
    r, g, b = np.moveaxis(rgb, -1, 0)
    hue = np.where(maximum == r, (g - b) / divisor,
                   np.where(maximum == g, (b - r) / divisor + 2., (r - g) / divisor + 4.))
    hue = np.where(delta > 0, np.mod(hue / 6., 1.), 0.)
    saturation = np.divide(delta, maximum, out=np.zeros_like(delta), where=maximum > 0)
    return np.stack([hue, saturation, maximum], axis=-1)


def _hsv_to_rgb(hsv):
    hue, saturation, value = np.moveaxis(hsv, -1, 0)
    k = np.mod(np.mod(hue, 1.)[..., None] * 6. + np.array([5., 3., 1.]), 6.)
    return value[..., None] * (1. - saturation[..., None] * np.maximum(0., np.minimum(np.minimum(k, 4. - k), 1.)))


def _trilinear(table, coordinates, periodic_first=False):
    """HSV table interpolation; table axes are hue, saturation, value."""
    lengths = np.array(table.shape[:3])
    scale = lengths - 1
    if periodic_first:
        scale[0] = lengths[0]
    positions = np.clip(coordinates, 0., 1.) * scale
    lower = np.floor(positions).astype(np.intp)
    fraction = positions - lower
    upper = np.minimum(lower + 1, lengths - 1)
    if periodic_first:
        lower[..., 0] %= lengths[0]
        upper[..., 0] = (lower[..., 0] + 1) % lengths[0]
    lower = np.minimum(lower, lengths - 1)
    output = np.zeros(coordinates.shape, dtype=np.float64)
    for a in (0, 1):
        for b in (0, 1):
            for c in (0, 1):
                picks = (a, b, c)
                index = [upper[..., i] if p else lower[..., i] for i, p in enumerate(picks)]
                weight = np.prod([fraction[..., i] if p else 1. - fraction[..., i] for i, p in enumerate(picks)], axis=0)
                output += table[tuple(index)] * weight[..., None]
    return output


def _tetrahedral(table, coordinates):
    """Four-vertex RGB interpolation, matching the published DNG SDK method."""
    position = np.clip(coordinates, 0., 1.) * (len(table) - 1)
    lower = np.minimum(np.floor(position).astype(np.intp), len(table) - 2)
    fraction = position - lower
    order = np.argsort(-fraction, axis=-1)
    fractions = np.take_along_axis(fraction, order, axis=-1)
    first = lower + np.eye(3, dtype=np.intp)[order[..., 0]]
    second = first + np.eye(3, dtype=np.intp)[order[..., 1]]
    points = (lower, first, second, lower + 1)
    weights = (1. - fractions[..., 0], fractions[..., 0] - fractions[..., 1],
               fractions[..., 1] - fractions[..., 2], fractions[..., 2])
    return sum(table[tuple(np.moveaxis(point, -1, 0))] * weight[..., None]
               for point, weight in zip(points, weights))


def _read_rgb(raw):
    if len(raw) < 16:
        raise ValueError('RGBTable 头不完整')
    kind, version, dimensions, divisions = struct.unpack_from('<4I', raw)
    if (kind, version, dimensions) != (1, 1, 3) or not 2 <= divisions <= 32:
        raise ValueError('尚不支持此 RGBTable 格式/维度')
    end = 16 + divisions ** 3 * 6
    if len(raw) not in (end + 28, end + 32):
        raise ValueError('RGBTable 长度不匹配')
    primaries, transfer, gamut, minimum, maximum = struct.unpack_from('<3I2d', raw, end)
    if primaries >= len(PRIMARIES) or transfer >= len(TRANSFERS) or gamut not in (0, 1):
        raise ValueError('RGBTable 色彩编码不受支持')
    if not np.isfinite([minimum, maximum]).all() or not 0 <= minimum <= 1 <= maximum:
        raise ValueError('RGBTable Amount 范围无效')
    if len(raw) == end + 32 and struct.unpack_from('<I', raw, end + 28)[0]:
        raise ValueError('尚不支持 RGBTable 扩展 flags')
    delta = np.frombuffer(raw, dtype='<u2', count=divisions ** 3 * 3, offset=16).astype(np.uint32)
    axis = (np.arange(divisions, dtype=np.uint32) * 65535 + divisions // 2) // (divisions - 1)
    identity = np.stack(np.meshgrid(axis, axis, axis, indexing='ij'), axis=-1)
    samples = ((delta.reshape(divisions, divisions, divisions, 3) + identity) & 65535) / 65535.
    return {'samples': samples, 'primaries': primaries, 'transfer': transfer, 'gamut': gamut,
            'grid': [divisions] * 3, 'amount_range': [minimum, maximum]}


def _read_hsv(raw):
    if len(raw) < 24:
        raise ValueError('LookTable 头不完整')
    kind, version, hue, saturation, value = struct.unpack_from('<5I', raw)
    if (kind, version) != (0, 1) or hue < 1 or saturation < 2 or value < 1:
        raise ValueError('尚不支持此 LookTable 格式/维度')
    count = hue * saturation * value * 3
    if len(raw) != 24 + count * 4:
        raise ValueError('LookTable 长度不匹配')
    encoding = struct.unpack_from('<I', raw, len(raw) - 4)[0]
    if encoding not in (0, 1):
        raise ValueError('尚不支持 LookTable 编码')
    samples = np.frombuffer(raw, dtype='<f4', count=count, offset=20).reshape(value, hue, saturation, 3).transpose(1, 2, 0, 3)
    if not np.isfinite(samples).all() or np.any(samples[..., 1:] < 0):
        raise ValueError('LookTable 含无效 HSV 调整')
    # The SDK guarantees no brightness change for zero-saturation entries.
    if not np.allclose(samples[:, 0, :, 2], 1., atol=1e-6, rtol=0):
        raise ValueError('LookTable 零饱和度亮度系数必须为 1')
    return {'samples': samples.astype(np.float64), 'encoding': encoding, 'grid': [hue, saturation, value]}


@lru_cache(maxsize=8)
def _recipe(data):
    tree = ET.fromstring(data)
    descriptions = [node for node in tree.iter(RDF + 'Description') if node.get(CRS + 'PresetType')]
    if len(descriptions) != 1 or descriptions[0].get(CRS + 'PresetType') != 'Look':
        raise ValueError('离线模式仅支持包含完整颜色数据的 Look/Profile XMP')
    node = descriptions[0]
    attributes = {key[len(CRS):]: value for key, value in node.attrib.items() if key.startswith(CRS)}
    uuid = attributes.get('UUID')
    if not uuid:
        raise ValueError('Look/Profile 缺少 UUID')
    if any(n.get(CRS + 'Stubbed', '').lower() == 'true' for n in tree.iter()):
        raise ValueError('XMP 仅引用外部 Look，缺少完整颜色配置')
    name = next((n.text for n in node.findall('.//' + CRS + 'Name//' + RDF + 'li') if n.text), uuid)
    allowed = _METADATA | {'CameraProfile', 'ConvertToGrayscale', 'RGBTable', 'LookTable', 'Amount', 'CurveRefineSaturation'}
    unknown = [key for key in attributes if key not in allowed and not key.startswith(('Supports', 'Table_'))]
    if unknown:
        raise ValueError('离线模式尚不支持这些参数：' + ', '.join(sorted(unknown)))
    for child in tree.iter():
        if child.tag.startswith(CRS) and child.tag[len(CRS):] not in _TEXT_NODES | set(CURVES):
            raise ValueError('离线模式尚不支持这些调整：' + child.tag[len(CRS):])
        if child is not node:
            other = [key[len(CRS):] for key in child.attrib
                     if key.startswith(CRS) and not key.startswith(CRS + 'Table_')]
            if other:
                raise ValueError('离线模式尚不支持嵌套或外部调整：' + ', '.join(sorted(other)))
    if attributes.get('ConvertToGrayscale', 'False').lower() != 'false':
        raise ValueError('离线模式尚不支持黑白转换')
    if attributes.get('CameraProfile', '') not in ('', 'Adobe Standard'):
        raise ValueError('缺少离线可用的基础相机配置：' + attributes['CameraProfile'])
    if float(attributes.get('Amount', 1)) != 1:
        raise ValueError('当前离线 Look 固定 Amount=1')
    refine = float(attributes.get('CurveRefineSaturation', 100))
    if not np.isfinite(refine) or not 0 <= refine <= 100:
        raise ValueError('CurveRefineSaturation 超出 0–100')
    tables, fingerprints = {}, {}
    for key, reader in (('LookTable', _read_hsv), ('RGBTable', _read_rgb)):
        fingerprint = attributes.get(key)
        if not fingerprint:
            continue
        payloads = {n.get(CRS + 'Table_' + fingerprint) for n in tree.iter() if n.get(CRS + 'Table_' + fingerprint)}
        if len(payloads) != 1:
            raise ValueError(key + ' 的颜色数据缺失或引用不明确')
        tables[key] = reader(decode_table(payloads.pop(), fingerprint))
        fingerprints[key] = fingerprint
    if not tables:
        raise ValueError('Look/Profile 未包含支持的颜色表')
    if attributes.get('RequiresRGBTables', '').lower() == 'true' and 'RGBTable' not in tables:
        raise ValueError('此 Look 要求 RGBTable，但未提供')
    curves = {}
    for key in CURVES:
        child = node.find(CRS + key)
        if child is None:
            continue
        points = np.array([[float(x.strip()) for x in item.text.split(',')]
                           for item in child.findall('.//' + RDF + 'li')], dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
            raise ValueError(key + ' 曲线节点无效')
        if (np.any(points < 0) or np.any(points > 255) or np.any(np.diff(points[:, 0]) <= 0)
                or points[0, 0] != 0 or points[-1, 0] != 255):
            raise ValueError(key + ' 曲线必须有序、在 0–255 内且覆盖完整输入范围')
        curves[key] = points / 255.
    return {'name': name, 'uuid': uuid, 'tables': tables, 'fingerprints': fingerprints,
            'curves': curves, 'refine': refine, 'refine_present': 'CurveRefineSaturation' in attributes}


def inspect_support(data, *, base_profile=None):
    """Inspect support without invoking Lightroom or modifying any files."""
    result = {'supported': False, 'reason': '', 'name': None, 'uuid': None,
              'tables': {}, 'recipe_version': RECIPE_VERSION, 'approximate': True,
              'target_raw_parameters': dict(RAW_PARAMETERS),
              'target_render_settings': dict(TARGET_RENDER_SETTINGS),
              'limitations': list(_LIMITATIONS)}
    try:
        recipe = _recipe(bytes(data))
        result.update(supported=True, name=recipe['name'], uuid=recipe['uuid'])
        for key, table in recipe['tables'].items():
            entry = {'fingerprint': recipe['fingerprints'][key], 'grid': table['grid']}
            if key == 'RGBTable':
                entry.update(primaries=PRIMARIES[table['primaries']], transfer=TRANSFERS[table['transfer']],
                             gamut=('clip', 'extend')[table['gamut']], interpolation='tetrahedral')
            else:
                entry.update(encoding=('linear', 'sRGB')[table['encoding']], interpolation='trilinear')
            result['tables'][key] = entry
        result['tone_curves'] = {key: curve.tolist() for key, curve in recipe['curves'].items()}
        if recipe['refine_present']:
            result['curve_refine_saturation'] = recipe['refine']
            result['limitations'].append('CurveRefineSaturation 使用 HSV 饱和度混合近似，未复现 Adobe 算法。')
        if base_profile is not None:
            result['base_profile'] = base_profile['summary']
            result['limitations'].extend(base_profile['summary']['limitations'])
    except (ValueError, TypeError, KeyError, struct.error, ET.ParseError) as exc:
        result['reason'] = str(exc)
    except Exception as exc:
        # A malformed encoded table (Base85/zlib) is unsupported material.
        result['reason'] = '颜色表解码失败：' + str(exc)
    return result


def _apply_hsv(rgb, table):
    hsv = _rgb_to_hsv(np.clip(rgb, 0., 1.))
    encoded_value = _encode(hsv[..., 2], table['encoding'])
    coordinate = hsv.copy()
    coordinate[..., 2] = encoded_value
    delta = _trilinear(table['samples'], coordinate, periodic_first=True)
    hsv[..., 0] = np.mod(hsv[..., 0] + delta[..., 0] / 360., 1.)
    hsv[..., 1] = np.clip(hsv[..., 1] * delta[..., 1], 0., 1.)
    hsv[..., 2] = _decode(np.clip(encoded_value * delta[..., 2], 0., 1.), table['encoding'])
    return _hsv_to_rgb(hsv)


def _curve(value, points):
    return np.interp(value, points[:, 0], points[:, 1])


def _apply_tone(rgb, points):
    """Hue-preserving max/min curve, with middle channels interpolated."""
    maximum, minimum = rgb.max(axis=-1), rgb.min(axis=-1)
    mapped_max, mapped_min = _curve(maximum, points), _curve(minimum, points)
    fraction = np.divide(rgb - minimum[..., None], (maximum - minimum)[..., None],
                         out=np.zeros_like(rgb), where=(maximum > minimum)[..., None])
    return mapped_min[..., None] + fraction * (mapped_max - mapped_min)[..., None]


def _apply_curves(rgb, recipe):
    if not recipe['curves']:
        return rgb
    # XMP PV2012 control points use display-domain 0..255 coordinates. This
    # local recipe explicitly assumes sRGB encoding of the ProPhoto primaries.
    encoded = _encode(np.clip(rgb, 0., 1.))
    master = recipe['curves'].get(CURVES[0])
    if master is not None:
        original = _rgb_to_hsv(encoded)
        encoded = _apply_tone(encoded, master)
        if recipe['refine'] != 100:
            changed = _rgb_to_hsv(encoded)
            weight = recipe['refine'] / 100.
            changed[..., 1] = original[..., 1] * (1. - weight) + changed[..., 1] * weight
            encoded = _hsv_to_rgb(changed)
    for channel, key in enumerate(CURVES[1:]):
        if key in recipe['curves']:
            encoded[..., channel] = _curve(encoded[..., channel], recipe['curves'][key])
    return _decode(np.clip(encoded, 0., 1.))


def _apply_rgb(rgb, table):
    local = _convert(rgb, 2, table['primaries'])
    clipped = np.clip(local, 0., 1.)
    encoded = _encode(clipped, table['transfer'])
    transformed = _decode(_tetrahedral(table['samples'], encoded), table['transfer'])
    if table['gamut'] == 1:
        transformed += local - clipped
    return np.clip(_convert(transformed, table['primaries'], 2), 0., 1.)


def _dcp_values(data, wanted):
    """Read the small TIFF-style first IFD used by DCPs, without changing files."""
    if data[:2] not in (b'II', b'MM') or len(data) < 8:
        raise ValueError('基础 DCP 文件头无效')
    endian = '<' if data[:2] == b'II' else '>'
    magic, offset = struct.unpack_from(endian + 'HI', data, 2)
    if magic not in (42, 0x4352):
        raise ValueError('基础文件不是 TIFF/DCP')
    count = struct.unpack_from(endian + 'H', data, offset)[0]
    types = {1: ('B', 1), 3: ('H', 2), 4: ('I', 4), 5: ('II', 8),
             9: ('i', 4), 10: ('ii', 8), 11: ('f', 4), 12: ('d', 8)}
    result = {}
    for index in range(count):
        start = offset + 2 + index * 12
        tag, kind, length = struct.unpack_from(endian + 'HHI', data, start)
        if tag not in wanted:
            continue
        if kind not in types:
            raise ValueError('基础 DCP 中的数值类型不受支持：' + str(tag))
        fmt, size = types[kind]
        source = start + 8 if size * length <= 4 else struct.unpack_from(endian + 'I', data, start + 8)[0]
        values = struct.unpack_from(endian + fmt * length, data, source)
        if kind in (5, 10):
            if any(v == 0 for v in values[1::2]):
                raise ValueError('基础 DCP 含无效分母')
            values = tuple(a / b for a, b in zip(values[::2], values[1::2]))
        result[tag] = np.array(values)
    return result


def load_base_profile(profile_path=None, *, tone_path=None):
    """Load bundled/local SDK tone and optional installed DCP Look; no LR process.

    DCP Look and enhanced HSV Look are separate consecutive stages. Their
    cumulative order is supported by development references, rather than a
    complete specification of Adobe Develop. DCP matrices, white-dependent
    HueSat maps and black rendering remain outside this partial base.
    Callers must include ``summary`` in job identity and report this boundary.
    Bundled SDK tone retains its separate license; user DCP files are never bundled.
    """
    record, tone_dependency = load_sdk_tone(tone_path)
    samples = np.array(record.get('samples', []), dtype=np.float64)
    summary = {'kind': 'partial_dng_sdk_camera_base', 'approximate': True,
               'default_tone': {'path': tone_dependency['path'], 'sha256': tone_dependency['sha256'],
                                'source_url': record.get('source_url'), 'source_sha256': record.get('source_sha256'),
                                'sample_count': 1025},
               'tone_kind': 'dng-sdk-acr3-default', 'dcp': None, 'look_table': None,
               'order': 'DCP Look -> enhanced HSV Look when present -> linear base tone -> XMP curves -> RGBTable',
               'limitations': ['SDK 默认色调来自公开参考实现，不能代表 Lightroom 当前 Develop 默认引擎。',
                               'DCP Look 与增强 HSV Look 累加顺序经开发样本验证；未完整复现 Adobe Develop。',
                               '基础 DCP 相机矩阵、双光源 HueSat、黑色渲染和 RAW 白平衡映射尚未重建。']}
    base = {'tone_curve': np.column_stack([np.linspace(0., 1., len(samples)), samples]),
            'look_table': None, 'summary': summary}
    if profile_path is None:
        summary['limitations'].append('未提供相机 DCP，仅使用公开 SDK 默认色调。')
        return base
    path = Path(profile_path)
    if not path.is_file():
        raise ValueError('指定的基础 DCP 文件不存在：' + str(path))
    data = path.read_bytes()
    tags = _dcp_values(data, {50940, 50981, 50982, 51108})
    summary['dcp'] = {'path': str(path.resolve()), 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    if 50940 in tags:
        curve = tags[50940].reshape(-1, 2)
        if (len(curve) < 2 or not np.isfinite(curve).all() or np.any(curve < 0) or np.any(curve > 1)
                or curve[0, 0] != 0 or curve[-1, 0] != 1 or np.any(np.diff(curve[:, 0]) <= 0)):
            raise ValueError('基础 DCP 色调曲线无效')
        base['tone_curve'] = curve
        summary['tone_kind'] = 'dcp-profile-tone-piecewise-linear'
        summary['limitations'].append('基础 DCP 显式色调曲线使用分段线性插值，未复现 SDK spline。')
    if 50981 in tags or 50982 in tags:
        if 50981 not in tags or 50982 not in tags:
            raise ValueError('基础 DCP LookTable 数据不完整')
        dims = tags[50981]
        if dims.shape != (3,) or np.any(dims < [1, 2, 1]):
            raise ValueError('基础 DCP LookTable 维度无效')
        hue, saturation, value = [int(v) for v in dims]
        samples = tags[50982]
        encoding_values = tags.get(51108, np.array([0]))
        if encoding_values.shape != (1,):
            raise ValueError('基础 DCP LookTable 编码无效')
        raw = struct.pack('<5I', 0, 1, hue, saturation, value) + samples.astype('<f4').tobytes()
        raw += struct.pack('<I', int(encoding_values[0]))
        base['look_table'] = _read_hsv(raw)
        summary['look_table'] = {'grid': base['look_table']['grid'], 'encoding': int(encoding_values[0])}
    return base


def render_pixels(data, linear_srgb, *, base_profile=None):
    """Apply all supported Look components; return approximate display sRGB."""
    recipe = _recipe(bytes(data))
    pixels = np.asarray(linear_srgb, dtype=np.float64)
    if pixels.ndim < 1 or pixels.shape[-1] != 3 or not np.isfinite(pixels).all():
        raise ValueError('输入必须是有限的 RGB 数组')
    if np.any(pixels < -1e-7) or np.any(pixels > 1. + 1e-7):
        raise ValueError('当前离线模式仅支持 0–1 的线性 sRGB，未支持 HDR/overrange')
    shape = pixels.shape
    flat = pixels.reshape(-1, 3)
    output = np.empty_like(flat, dtype=np.float32)
    # Bound temporary arrays for full-size photos without changing the recipe.
    for start in range(0, len(flat), 65536):
        rgb = _convert(np.clip(flat[start:start + 65536], 0., 1.), 0, 2)
        if base_profile is not None and base_profile['look_table'] is not None:
            rgb = _apply_hsv(rgb, base_profile['look_table'])
        if 'LookTable' in recipe['tables']:
            rgb = _apply_hsv(rgb, recipe['tables']['LookTable'])
        if base_profile is not None:
            rgb = _apply_tone(np.clip(rgb, 0., 1.), base_profile['tone_curve'])
        rgb = _apply_curves(rgb, recipe)
        if 'RGBTable' in recipe['tables']:
            rgb = _apply_rgb(rgb, recipe['tables']['RGBTable'])
        output[start:start + len(rgb)] = _encode(np.clip(_convert(rgb, 2, 0), 0., 1.))
    return output.reshape(shape)
