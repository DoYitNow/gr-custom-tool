# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Inspect imported shutdown resources and prepare self-contained image drafts.

No function in this module patches/rebuilds firmware or accesses a camera. The
native addresses are hypotheses until the imported bytes prove the known code
and initialized-data layout. Resource payload lengths come from RES records,
never from searching for the first JPEG EOI (EXIF thumbnails also contain EOI).
"""
from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import struct
import unicodedata
from uuid import uuid4
import zipfile

from PIL import Image, ImageOps, UnidentifiedImageError

from .firmware import BASE, FirmwareError, FirmwareImage, _Memory

SCHEMA_VERSION = 2
# This is the capacity exercised by our compiler/QA, not a measured hardware maximum.
MAX_MENU_ITEMS = 16
MAX_NAME_UNITS = 24
JPEG_TABLE = 0x55013E70
_TARGETS = (
    ('goodbye', 1, 'GoodBye.jpg', '普通关机'),
    ('hdf', 2, 'GB_HDF.jpg', 'HDF 关机'),
    ('mono', 3, 'GB_Mono.jpg', '单色关机'),
    ('20th', 11, 'GB_20th.jpg', '20 周年关机'),
)
# These function bodies were disassembled against the actual .41 import. The
# hashes prove applicability of the earlier research to these exact bodies;
# other layouts remain unknown, rather than inheriting old sample conclusions.
_CODE = (
    ('selection', 0x531B9488, 0x531B9538, '79c8230a65fcd0b259ae58188e400c72f234b23d125e29442f4593d18ef5da3f'),
    ('display_geometry', 0x53262558, 0x53262660, '6c65a2c4cba48fda55edf9ad0b8cf264e812305eb623bfabacc2c8dffeba07c8'),
    ('file_request', 0x53262318, 0x53262558, 'f7dc71d0d6061293c8f1183dbd9a6b4f7777b1bb7492376a3d54b8258dcaa316'),
    ('shutdown_reasons', 0x531B95BC, 0x531B9754, '426e40a6a42fc7e23d7298fe8b5270dd76f0ee7e1a89d64399f15df299901e5d'),
    ('end_wait', 0x531B9754, 0x531B97B0, '35abc2c6ae2e607be3264094898babdf215f6303c5251dd66b1dc9c0fe3c9a5a'),
    ('completion_events', 0x531B9844, 0x531B98B0, '9336581d3f83b873a86876818e38f1b67065aa9445be41709c6c7e261f4e3bd2'),
    ('timer_cleanup', 0x531B8DE4, 0x531B8E98, '516c2cba537a6b06be654e9b46a95de97b937b5e4729b53a86f18457da1ad821'),
    ('completion_producer', 0x5325F9BC, 0x5325FB64, '8520001d4f5cccec0f3c596a705ac1511f0d3293f9371b8658ce681ced839cb6'),
    ('setting_start_display', 0x531B9538, 0x531B95BC, 'bc1aba89c3d2fcd2f6d699bf8885d056fd2e34dbfcdfb282670251a8076a72d0'),
    ('today_overlay', 0x531B915C, 0x531B9230, '830e5c23725a75b7cc81e64e1732274a1375bcf85974720e14ae3aad90279adc'),
    ('today_total_fw_overlay', 0x531B9230, 0x531B9380, '9b8dbd39626c268fc0dd3435fcf2fa07a0eea3d2bccd297148b2bb1dfc2e20a7'),
    ('statistics_mode_getter', 0x5329AF24, 0x5329AF30, '96adf1d204fe7a5caf397743d28094bf7f5fe6caf27310112ee47ac3a490e76c'),
)
_CONDITIONS = {
    'goodbye': '普通关机的默认图案；当前相机选择无法由 BIN 判断。',
    'hdf': 'HDF 特定关机分支使用；当前相机选择无法由 BIN 判断。',
    'mono': '单色特定关机分支使用；当前相机选择无法由 BIN 判断。',
    '20th': '20 周年特定关机分支使用；当前相机选择无法由 BIN 判断。',
}
_QUERY_EVIDENCE = {
    'goodbye': {'variant_query': 0, 'edition_query': 'other than 1 or 2'},
    'hdf': {'variant_query': 0, 'edition_query': 2},
    'mono': {'variant_query': 0, 'edition_query': 1},
    '20th': {'variant_query': 5, 'edition_query': 'any'},
}


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def _decode_b64(value, field: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f'{field} must be base64 text')
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f'{field} is not valid base64') from exc


def _data_url(data: bytes) -> str:
    return 'data:image/jpeg;base64,' + _b64(data)


def _jpeg_info(data: bytes) -> dict:
    """Read complete JPEG records, skipping APP/EXIF sections by their lengths."""
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('payload is not a JPEG')
    cursor, frame, in_scan, comment_bytes = 2, None, False, 0
    while cursor < len(data):
        if in_scan:
            marker_start = data.find(b'\xff', cursor)
            if marker_start < 0:
                raise ValueError('JPEG has no final EOI')
            cursor = marker_start
        if data[cursor] != 0xFF:
            raise ValueError('invalid JPEG marker boundary')
        while cursor < len(data) and data[cursor] == 0xFF:
            cursor += 1
        if cursor >= len(data):
            raise ValueError('truncated JPEG marker')
        marker = data[cursor]
        cursor += 1
        if in_scan and (marker == 0 or 0xD0 <= marker <= 0xD7):
            continue
        if marker == 0xD9:
            if cursor != len(data):
                raise ValueError('JPEG contains bytes after its final EOI')
            if frame is None:
                raise ValueError('JPEG has no frame header')
            break
        in_scan = False
        if marker in (0, 0xD8) or 0xD0 <= marker <= 0xD7:
            raise ValueError('invalid standalone JPEG marker')
        if cursor + 2 > len(data):
            raise ValueError('truncated JPEG segment length')
        size = struct.unpack_from('>H', data, cursor)[0]
        if size < 2 or cursor + size > len(data):
            raise ValueError('JPEG segment is outside payload')
        if marker in (0xC0, 0xC1, 0xC2):
            if size < 8:
                raise ValueError('truncated JPEG frame header')
            precision, height, width = struct.unpack_from('>BHH', data, cursor + 2)
            channels = data[cursor + 7]
            if size != 8 + channels * 3:
                raise ValueError('JPEG frame component count differs from segment length')
            frame = {'sof_marker': marker, 'precision': precision,
                     'width': width, 'height': height, 'channels': channels,
                     'sampling': [{'component': data[cursor + 8 + index * 3],
                                   'horizontal': data[cursor + 9 + index * 3] >> 4,
                                   'vertical': data[cursor + 9 + index * 3] & 15}
                                  for index in range(channels)]}
        if marker == 0xFE:
            comment_bytes += size + 2
        if marker == 0xDA:
            in_scan = True
        cursor += size
    else:
        raise ValueError('JPEG has no final EOI')
    try:
        with Image.open(BytesIO(data)) as picture:
            picture.load()
            if picture.format != 'JPEG' or picture.size != (frame['width'], frame['height']):
                raise ValueError('decoded JPEG dimensions differ from its frame header')
            frame['mode'] = picture.mode
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError(f'JPEG decode failed: {exc}') from exc
    frame['baseline'] = frame['sof_marker'] == 0xC0 and frame['precision'] == 8
    frame['comment_bytes'] = comment_bytes
    return frame


def _res_records(image: FirmwareImage) -> dict[str, dict]:
    sections = [row for row in image.sections if row.get('name') == 'RES']
    if len(sections) != 1:
        raise ValueError('import has no unique RES section')
    section = sections[0]
    start, end = section['start'], section['end']
    if not 0 <= start < start + 16 <= end <= len(image.decoded):
        raise ValueError('RES section is outside imported decoded data')
    if image.decoded[start:start + 4] != b'RES\0':
        raise ValueError('RES header does not match imported section')
    count = struct.unpack_from('<I', image.decoded, start + 12)[0]
    if count != section.get('resource_count'):
        raise ValueError('RES header and section inventory counts disagree')
    records, cursor = {}, start + 16
    for _ in range(count):
        if cursor + 36 > end:
            raise ValueError('truncated RES resource header')
        path_bytes = image.decoded[cursor:cursor + 32]
        if b'\0' not in path_bytes:
            raise ValueError('RES resource path has no bounded NUL terminator')
        try:
            path = path_bytes.split(b'\0', 1)[0].decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError('RES resource path is not ASCII') from exc
        size = struct.unpack_from('<I', image.decoded, cursor + 32)[0]
        offset = cursor + 36
        if not path or path in records or offset + size > end:
            raise ValueError('invalid, duplicate or out-of-bounds RES resource')
        records[path] = {'record_offset': cursor, 'offset': offset, 'size': size,
                         'record_header_hex': image.decoded[cursor:offset].hex()}
        cursor = offset + size
    padding = image.decoded[cursor:end]
    # Retiring embedded menu images can leave a zero-filled tail. Keeping RES
    # nonshrinking preserves container frame coordinates on the next rebuild.
    if any(padding):
        raise ValueError('unrecognised bytes after RES resource records')
    return records


def read_shutdown(image: FirmwareImage) -> dict:
    """Return resources proved by this imported BIN, with static selection evidence."""
    result = {
        'schema_version': SCHEMA_VERSION, 'source_sha256': image.sha256,
        'source_version': image.internal_version, 'resources': [],
        'inspection': {'resource_readback': 'unknown', 'selection_code_verified': False,
                       'notes': [], 'code_proof': [],
                       'proof_method': 'Imported function-byte comparison; not camera execution'},
        'native_selection': {'camera_current_selection': None,
                             'camera_values_available': False, 'verified': False},
        'capabilities': {'can_prepare_assets': False, 'can_patch_firmware': False,
                         'can_change_camera_selection': False, 'hardware_verified': False,
                         'can_build_menu': False, 'menu_build_reason': '尚未匹配可编译的机内关机布局'},
    }
    inspection = result['inspection']
    try:
        from .demo_build import _metadata, canonical_base, supports_image
        try:
            metadata = _metadata(image.rtos) or {}
        except (ValueError, TypeError, struct.error) as exc:
            metadata = {}
            inspection['notes'].append('Editor registry unavailable: ' + str(exc))
        from .shutdown_compiler import read_menu_resources
        installed = read_menu_resources(image, metadata)
        if installed:
            result['installed_menu'] = installed
        records = _res_records(image)
        memory = _Memory(image.rtos)
        proof_memory = _Memory(canonical_base(image.rtos)) if installed else memory
        table = memory.pair(0x5326255C, 0x5326256C)
        request_table = memory.pair(0x53262324, 0x5326232C)
        expected_table = int(installed['jpeg_table']['address'], 16) if installed else JPEG_TABLE
        if table != request_table or table != expected_table:
            raise ValueError('unknown native JPEG table consumers')
        inspection['jpeg_table'] = {'runtime_address': table,
                                   'rom_address': table if table < memory.destination else memory.source + table - memory.destination,
                                   'source': 'native MOVW/MOVT consumers plus startup initialized-data copy'}
        proofs = {}
        for name, start, end, expected in _CODE:
            actual = _sha(proof_memory.read(start, end - start))
            proofs[name] = actual == expected
            inspection['code_proof'].append({'name': name, 'address': start, 'end': end,
                                             'sha256': actual, 'expected_sha256': expected,
                                             'verified': actual == expected})
        if installed:
            inspection['proof_method'] = 'Verified recoverable canonical original control bodies plus fresh installed JPEG descriptors/files; native menu/state execution tested separately.'
        selection_verified = proofs['selection']
        inspection['selection_code_verified'] = selection_verified
        layer_verified = proofs['display_geometry'] and proofs['file_request']
        result['native_selection'].update(
            verified=selection_verified,
            selection_entry_address=0x531B9488,
            conditions=([
                {'variant_query': 0, 'edition_query': 1, 'resource_id': 'mono'},
                {'variant_query': 0, 'edition_query': 2, 'resource_id': 'hdf'},
                {'variant_query': 0, 'edition_query': 'other', 'resource_id': 'goodbye'},
                {'variant_query': 5, 'edition_query': 'any', 'resource_id': '20th'},
            ] if selection_verified else []),
            note='查询字段语义未完整恢复；当前相机设置不在 BIN 中，不能从固件推断已选图案。',
        )
        if installed:
            result['native_selection'].update(
                conditions_scope='factory choice 0 only',
                image_choices=[0] + [row['selection_id'] for row in installed['presets']],
                custom_resource_ids=[row['native_id'] for row in installed['presets']],
                note='已安装全局图案列表：原厂值0走上述条件，自定义稳定ID请求对应图片；相机当前值不在BIN中。')
        overlay_verified = all(proofs[name] for name in ('setting_start_display', 'today_overlay',
                                                        'today_total_fw_overlay', 'statistics_mode_getter'))
        result['native_overlay'] = {
            'verified': overlay_verified, 'camera_current_value': None,
            'camera_values_available': False,
            'summary': ('原关机画面会在背景 JPEG 上叠加拍摄统计；替换图片会保留这些文字。'
                        if overlay_verified else '统计叠加层未识别，不能预测自定义图片的完整机内画面。'),
            'parameter': {'name': 'SetupGoodByeDisplay', 'user_data_offset': 0x1474,
                          'semantics': 'statistics display mode, not background image choice'} if overlay_verified else None,
            'modes': [{'value': 0, 'name': 'TodaysShots', 'summary': '当日拍摄数'},
                      {'value': 1, 'name': 'TodaysShotsAndTotalShotsAndFwVersion',
                       'summary': '当日拍摄数、累计拍摄数和固件版本'}] if overlay_verified else [],
            'hardware_full_composite_verified': False,
        }
        inspection['notes'].append(result['native_overlay']['summary'])
        inspection['shutdown_flow'] = {
            'verified': all(proofs[name] for name in ('shutdown_reasons', 'end_wait',
                             'completion_events', 'timer_cleanup', 'completion_producer')),
            'normal_reason_values': [0, 1, 10] if proofs['shutdown_reasons'] else None,
            'status_reason_values': [2, 3, 4, 7, 11] if proofs['shutdown_reasons'] else None,
            'immediate_handoff_reason_values': [5, 6, 8, 9] if proofs['shutdown_reasons'] else None,
            'completion_event': 286 if proofs['completion_events'] else None,
            'normal_timer_argument': 1000 if proofs['end_wait'] else None,
            'timer_time_unit': 'unknown; argument is not a measured screen duration',
            'hardware_decode_display_poweroff_verified': False,
        }
        for identifier, native_id, filename, name in _TARGETS:
            expected_path = 'A:\\Resource\\Jpeg\\' + filename
            try:
                address = memory.word(table + native_id * 4)
                width, height, path_address = struct.unpack('<HHI', memory.read(address, 8))
                path = bytearray()
                for index in range(64):
                    unit = memory.read(path_address + index, 1)
                    if unit == b'\0':
                        break
                    path.extend(unit)
                else:
                    raise ValueError('native resource path has no bounded NUL terminator')
                if bytes(path).decode('ascii') != expected_path:
                    raise ValueError('native descriptor path differs from the known shutdown resource')
                record = records.get(expected_path)
                if record is None:
                    raise ValueError('native shutdown path is absent from imported RES')
                payload = image.decoded[record['offset']:record['offset'] + record['size']]
                decoded = _jpeg_info(payload)
                if (width, height) != (decoded['width'], decoded['height']):
                    raise ValueError('native descriptor and JPEG dimensions disagree')
                result['resources'].append({
                    'id': identifier, 'native_id': native_id, 'path': expected_path,
                    'name': name, 'width': width, 'height': height,
                    'sha256': _sha(payload), 'payload_base64': _b64(payload),
                    'preview_data_url': _data_url(payload), **record,
                    'descriptor_address': address, 'path_address': path_address,
                    'encoding': 'JPEG baseline' if decoded['baseline'] else 'JPEG non-baseline',
                    'jpeg_info': decoded,
                    'selection_condition': {
                        'summary': _CONDITIONS[identifier] if selection_verified else '未知：导入 BIN 的选择代码未匹配。',
                        'verified': selection_verified,
                        'query_evidence': _QUERY_EVIDENCE[identifier] if selection_verified else None},
                    'selection_condition_verified': selection_verified,
                    'display_layer_verified': layer_verified,
                    'display_origin': [0, 0] if layer_verified and (width, height) == (720, 480) else None,
                    'display_canvas_fixture': [720, 480] if layer_verified else None,
                    'display_geometry_note': 'Centering code is verified; 720×480 canvas is the offline research fixture, not a hardware measurement.',
                    'replacement_supported': (layer_verified and decoded['baseline'] and
                                              decoded['mode'] == 'RGB' and decoded['channels'] == 3 and
                                              _subsampling({'jpeg_info': decoded}) is not None),
                    'source': 'imported BIN: RES record and initialized native JPEG descriptor',
                })
            except (ValueError, FirmwareError, UnicodeError, struct.error) as exc:
                inspection['notes'].append(f'{filename}: {exc}')
        inspection['resource_readback'] = ('passed' if len(result['resources']) == len(_TARGETS)
                                           else 'partial' if result['resources'] else 'unknown')
        result['capabilities']['can_prepare_assets'] = bool(result['resources'])
        result['capabilities'].update(
            can_build_menu=bool(selection_verified and layer_verified and supports_image(image.rtos)),
            menu_build_reason='可构建原厂及自定义图案列表；当前编译与离线验证容量为16项。')
        if not selection_verified:
            inspection['notes'].append('Selection function differs from verified research bytes; selection conditions remain unknown.')
        if not layer_verified:
            inspection['notes'].append('Display/file request functions differ; previews are available but replacement readiness is blocked.')
    except (ValueError, FirmwareError, UnicodeError, KeyError, struct.error) as exc:
        inspection['notes'].append(str(exc))
    result['reason'] = ('已核对关机资源；可通过图案列表构建机内选择菜单。' if result['capabilities']['can_build_menu'] else
                        '已读取关机资源；该输入暂不支持机内菜单构建。' if result['resources'] else
                        '导入 BIN 的关机资源或显示层未识别：' + '; '.join(inspection['notes']))
    return result


def _pad_jpeg(data: bytes, target_size: int) -> bytes | None:
    """Insert legal COM segments after SOI; do not append bytes after EOI."""
    remaining = target_size - len(data)
    if remaining < 0 or 0 < remaining < 4:
        return None
    comments = bytearray()
    while remaining:
        size = min(remaining, 65537)  # marker + 16-bit segment length
        if 0 < remaining - size < 4:
            size -= 4 - (remaining - size)
        comments.extend(b'\xff\xfe' + struct.pack('>H', size - 2) + bytes(size - 4))
        remaining -= size
    return data[:2] + bytes(comments) + data[2:]


def _source_image(data: bytes) -> tuple[Image.Image, dict]:
    try:
        with Image.open(BytesIO(data)) as source:
            source.seek(0)
            source.load()
            metadata = {'source_width': source.width, 'source_height': source.height,
                        'source_mode': source.mode, 'source_format': source.format,
                        'source_frames': getattr(source, 'n_frames', 1),
                        'source_exif_orientation': source.getexif().get(274)}
            oriented = ImageOps.exif_transpose(source)
            # Palette transparency and RGBA both become black-backed RGB.
            rgba = oriented.convert('RGBA')
            background = Image.new('RGBA', rgba.size, (0, 0, 0, 255))
            background.alpha_composite(rgba)
            picture = background.convert('RGB')
            metadata['oriented_width'], metadata['oriented_height'] = picture.size
            return picture, metadata
    except (OSError, UnidentifiedImageError, ValueError) as exc:
        raise ValueError('无法读取图片，请选择有效的 JPG、PNG 或其他支持的图片文件') from exc


def _target_identity(target: dict) -> dict:
    try:
        result = {'target_id': target['id'], 'target_path': target['path'],
                  'target_sha256': target['sha256'], 'target_size': target['size'],
                  'width': target['width'], 'height': target['height']}
    except (KeyError, TypeError) as exc:
        raise ValueError('upload target is not an identified imported resource') from exc
    if not all(isinstance(result[key], int) and result[key] > 0
               for key in ('target_size', 'width', 'height')):
        raise ValueError('target dimensions/length are unknown')
    return result


def _subsampling(target: dict) -> int | None:
    sampling = target.get('jpeg_info', {}).get('sampling')
    if sampling is None:  # small unit fixtures without a source JPEG header
        return 2
    pattern = [(row['horizontal'], row['vertical']) for row in sampling]
    return {((1, 1), (1, 1), (1, 1)): 0,
            ((2, 1), (1, 1), (1, 1)): 1,
            ((2, 2), (1, 1), (1, 1)): 2}.get(tuple(pattern))


def _checks(payload: bytes, target: dict) -> dict:
    info = _jpeg_info(payload)
    result = {
        'dimensions_match': (info['width'], info['height']) == (target['width'], target['height']),
        'baseline_jpeg': info['baseline'], 'exact_length': len(payload) == target['size'],
        'rgb_jpeg': info['mode'] == 'RGB' and info['channels'] == 3,
        'decoded_mode': info['mode'], 'channels': info['channels'], 'sampling': info['sampling'],
        'target_sampling_match': _subsampling({'jpeg_info': info}) == _subsampling(target) is not None,
        'target_encoding_supported': bool(target.get('replacement_supported', target.get('display_layer_verified'))),
        'payload_decoded': True,
        'display_layer_verified': bool(target.get('display_layer_verified')),
        'hardware_decoder_verified': False,
    }
    result['replacement_ready'] = all(result[key] for key in (
        'dimensions_match', 'baseline_jpeg', 'rgb_jpeg', 'target_sampling_match',
        'target_encoding_supported', 'exact_length', 'payload_decoded', 'display_layer_verified'))
    return result


def prepare_asset(data: bytes, filename: str, target: dict, fit: str = 'contain') -> dict:
    """Convert a static frame and try baseline JPEG with exact RES payload size."""
    identity = _target_identity(target)
    if fit not in ('contain', 'cover'):
        raise ValueError('fit must be contain or cover')
    picture, metadata = _source_image(data)
    dimensions = (target['width'], target['height'])
    if fit == 'cover':
        canvas = ImageOps.fit(picture, dimensions, method=Image.Resampling.LANCZOS)
    else:
        resized = ImageOps.contain(picture, dimensions, method=Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', dimensions, 'black')
        canvas.paste(resized, ((dimensions[0] - resized.width) // 2,
                              (dimensions[1] - resized.height) // 2))
    payload, padding, quality, encoded_size = b'', 0, 95, 0
    subsampling = _subsampling(target)
    for quality in range(95, 4, -1):
        output = BytesIO()
        canvas.save(output, format='JPEG', quality=quality, optimize=True,
                    progressive=False, subsampling=subsampling if subsampling is not None else 2)
        encoded = output.getvalue()
        payload, encoded_size = encoded, len(encoded)
        candidate = _pad_jpeg(encoded, target['size'])
        if candidate is not None:
            # The padded file, rather than only its unpadded source, is decoded.
            _jpeg_info(candidate)
            payload, padding = candidate, len(candidate) - len(encoded)
            break
    checks = _checks(payload, target)
    if checks['replacement_ready']:
        reason = '已按原生尺寸生成 RGB baseline JPEG，并用合法 COM 段补齐原资源长度；仅通过离线解码，相机 JPEG 解析器尚未实测。'
    elif not checks['exact_length']:
        reason = f'最低尝试质量仍无法适配 {target["size"]} 字节原资源；保留预览，可换图或换目标后重试。'
    else:
        reason = '尺寸与长度已适配，但原 JPEG 编码条件或显示层尚未匹配，不能导出替换 JPEG。'
    filename = Path(str(filename).replace('\\', '/')).name or 'shutdown-image'
    notes = ['透明区域合成到黑底；EXIF 方向已应用；输出不携带上传文件的 EXIF。']
    if metadata['source_frames'] > 1:
        notes.append('上传文件含多帧，本阶段只使用第一帧作为静态关机图案。')
    from .shutdown_compiler import encode_menu_image
    menu_payload = encode_menu_image(data, fit)
    return {
        'id': 'shutdown-' + uuid4().hex, 'name': Path(filename).stem,
        'filename': filename, **identity, **metadata, 'fit': fit,
        'source_sha256': _sha(data), 'source_bytes': len(data),
        'source_payload_base64': _b64(data),
        'encoding': 'JPEG baseline', 'quality': quality, 'bytes': len(payload),
        'sha256': _sha(payload), 'payload_base64': _b64(payload),
        'preview_data_url': _data_url(payload), 'checks': checks,
        'jpeg_info': _jpeg_info(payload),
        'adaptation': {'status': 'ready' if checks['replacement_ready'] else 'blocked',
                       'reason': reason, 'padding_bytes': padding,
                       'encoded_bytes': encoded_size, 'padding': 'JPEG COM segments after SOI'},
        'conversion_notes': notes,
        'menu_ready': True, 'menu_bytes': len(menu_payload),
        'menu_preview_data_url': _data_url(menu_payload),
    }


def normalize_item_name(value) -> str:
    """Accept short BMP text that the native UTF-16 label path can represent."""
    if not isinstance(value, str):
        raise ValueError('关机图案名称必须是文字')
    name = value.strip()
    if not name or len(name.encode('utf-16-le', errors='surrogatepass')) // 2 > MAX_NAME_UNITS:
        raise ValueError(f'关机图案名称需为1至{MAX_NAME_UNITS}个UTF-16字符')
    if any(ord(character) > 0xffff or unicodedata.category(character)[0] not in 'LNPMZ'
           or (unicodedata.category(character)[0] == 'Z' and character != ' ')
           for character in name):
        raise ValueError('关机图案名称不支持表情、控制字符或特殊符号')
    return name


def _legacy_item_name(value, fallback: str) -> str:
    # Old source filenames were free text. Migration keeps representable text
    # instead of making an otherwise valid old project impossible to open.
    text = ''.join(character for character in str(value or '').strip()
                   if ord(character) <= 0xffff and unicodedata.category(character)[0] in 'LNPMZ'
                   and (unicodedata.category(character)[0] != 'Z' or character == ' '))[:MAX_NAME_UNITS]
    return normalize_item_name(text or fallback)


def item_menu_payload(asset: dict, fit: str) -> bytes:
    """Preserve imported JPEG bytes until the user changes its display fit."""
    source = _decode_b64(asset['source_payload_base64'], 'source image')
    if asset.get('source_kind') == 'installed_menu' and fit == asset.get('fit', 'contain'):
        return _decode_b64(asset['menu_payload_base64'], 'installed menu picture')
    from .shutdown_compiler import encode_menu_image
    return encode_menu_image(source, fit)


def validate_draft(value, inventory: dict) -> dict:
    """Validate JSON backup against imported resource identity; rebuild previews."""
    targets = {row['id']: row for row in inventory.get('resources', [])}
    if value is None:
        result = {'schema_version': SCHEMA_VERSION, 'target_id': next(iter(targets), None),
                  'selected_asset_id': None, 'assets': [], 'items': [],
                  'selected_item_id': None}
        installed = inventory.get('installed_menu')
        if installed and result['target_id']:
            target = targets[result['target_id']]
            for row in installed['presets']:
                payload = _decode_b64(row['payload_base64'], 'installed menu image')
                selection_id = row.get('selection_id', row['slot'])
                name = _legacy_item_name(row.get('name'), f'自定义 {row["slot"]}')
                fit = row.get('fit', 'contain')
                asset = prepare_asset(payload, name + '.jpg', target, fit)
                asset['id'] = row.get('asset_id') or 'installed-shutdown-' + format(selection_id, '08x')
                if any(previous['id'] == asset['id'] for previous in result['assets']):
                    asset['id'] = 'installed-shutdown-' + format(selection_id, '08x')
                asset.update(source_kind='installed_menu', menu_payload_base64=_b64(payload), menu_ready=True)
                result['assets'].append(asset)
                item = {'id': row.get('item_id') or 'shutdown-item-' + format(selection_id, '08x'),
                        'selection_id': selection_id, 'name': name, 'asset_id': asset['id'], 'fit': fit}
                if row.get('legacy_slot') in (1, 2):
                    item['legacy_slot'] = row['legacy_slot']
                elif installed.get('schema_version', 1) == 1:
                    item['legacy_slot'] = row['slot']
                result['items'].append(item)
            return validate_draft(result, inventory)
        return result
    if not isinstance(value, dict) or value.get('schema_version', 1) not in (1, SCHEMA_VERSION):
        raise ValueError('unsupported shutdown draft schema')
    target_id = value.get('target_id')
    if target_id not in targets and not (target_id is None and not targets):
        raise ValueError('shutdown draft target is absent from imported BIN')
    assets = value.get('assets', [])
    if not isinstance(assets, list):
        raise ValueError('shutdown draft assets must be a list')
    normalized, identifiers = [], set()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get('id'), str) or not asset['id']:
            raise ValueError('shutdown asset has no stable ID')
        if asset['id'] in identifiers:
            raise ValueError('duplicate shutdown asset ID')
        identifiers.add(asset['id'])
        target = targets.get(asset.get('target_id'))
        if target is None:
            raise ValueError('shutdown asset target is absent from imported BIN')
        identity = _target_identity(target)
        if any(asset.get(key) != expected for key, expected in identity.items()):
            raise ValueError('shutdown asset target identity differs from imported resource')
        payload = _decode_b64(asset.get('payload_base64'), 'asset payload')
        if asset.get('sha256') != _sha(payload) or asset.get('bytes') != len(payload):
            raise ValueError('shutdown asset payload hash/length differs')
        source = _decode_b64(asset.get('source_payload_base64'), 'source image')
        if asset.get('source_sha256') != _sha(source) or asset.get('source_bytes') != len(source):
            raise ValueError('shutdown source image hash/length differs')
        _, metadata = _source_image(source)
        if any(asset.get(key) != expected for key, expected in metadata.items()):
            raise ValueError('shutdown source image metadata differs')
        if asset.get('fit') not in ('contain', 'cover'):
            raise ValueError('fit must be contain or cover')
        checks = _checks(payload, target)
        if not checks['dimensions_match'] or not checks['baseline_jpeg'] or not checks['rgb_jpeg']:
            raise ValueError('shutdown asset JPEG encoding/dimensions do not match target')
        if not isinstance(asset.get('quality'), int) or not 5 <= asset['quality'] <= 95:
            raise ValueError('shutdown asset JPEG quality is outside conversion range')
        row = dict(asset)
        row.update(identity)
        row.update(metadata)
        row['payload_base64'], row['source_payload_base64'] = _b64(payload), _b64(source)
        row['preview_data_url'], row['checks'] = _data_url(payload), checks
        row['jpeg_info'] = _jpeg_info(payload)
        row['encoding'] = 'JPEG baseline'
        row['name'] = str(asset.get('name') or Path(asset.get('filename', 'shutdown-image')).stem).strip()[:120]
        row['filename'] = Path(str(asset.get('filename', 'shutdown-image')).replace('\\', '/')).name
        adaptation = asset.get('adaptation') if isinstance(asset.get('adaptation'), dict) else {}
        padding = row['jpeg_info']['comment_bytes']
        if adaptation.get('padding_bytes', padding) != padding:
            raise ValueError('invalid JPEG padding size')
        row['adaptation'] = {
            'status': 'ready' if checks['replacement_ready'] else 'blocked',
            'reason': ('仅离线资源适配通过；COM 填充的相机 JPEG 解析器、屏幕显示和关闭时序尚未实测。' if checks['replacement_ready']
                       else f'资源适配未通过：目标 {target["size"]} 字节，素材 {len(payload)} 字节；显示层也必须通过识别。'),
            'padding_bytes': padding, 'encoded_bytes': len(payload) - padding,
            'padding': 'JPEG COM segments after SOI',
        }
        row['conversion_notes'] = ['透明区域合成到黑底；EXIF 方向已应用；仅静态第一帧。']
        row['menu_ready'] = True
        if row.get('source_kind') == 'installed_menu':
            menu_payload = _decode_b64(row.get('menu_payload_base64'), 'installed menu payload')
            if menu_payload != source:
                raise ValueError('installed menu payload differs from its original source')
            info = _jpeg_info(menu_payload)
            if not (info['baseline'] and info['mode'] == 'RGB' and (info['width'], info['height']) == (720, 480)):
                raise ValueError('installed menu image encoding differs')
        else:
            from .shutdown_compiler import encode_menu_image
            menu_payload = encode_menu_image(source, row['fit'])
        row.update(menu_preview_data_url=_data_url(menu_payload), menu_bytes=len(menu_payload))
        normalized.append(row)
    selected = value.get('selected_asset_id')
    if selected is not None:
        match = next((asset for asset in normalized if asset['id'] == selected), None)
        if match is None or match['target_id'] != target_id:
            raise ValueError('selected shutdown asset is absent or belongs to another target')
    if value.get('schema_version', 1) == 1:
        enabled = value.get('menu_enabled', False)
        if type(enabled) is not bool:
            raise ValueError('menu_enabled must be a boolean')
        preset_ids = value.get('preset_asset_ids', [None, None])
        if not isinstance(preset_ids, list) or len(preset_ids) != 2 or any(
                identifier is not None and (not isinstance(identifier, str) or identifier not in identifiers)
                for identifier in preset_ids):
            raise ValueError('旧关机菜单槽位素材无效')
        by_id = {asset['id']: asset for asset in normalized}
        items = []
        for slot, asset_id in enumerate(preset_ids, 1):
            if not enabled or asset_id is None:
                continue
            asset = by_id[asset_id]
            items.append({'id': 'shutdown-item-legacy-' + str(slot), 'selection_id': slot,
                          'legacy_slot': slot,
                          'name': _legacy_item_name(asset['name'], f'自定义 {slot}'),
                          'asset_id': asset_id, 'fit': asset['fit']})
        selected_item = next((item['id'] for item in items if item['asset_id'] == selected), None)
    else:
        items, selected_item = value.get('items', []), value.get('selected_item_id')
    if not isinstance(items, list) or len(items) > MAX_MENU_ITEMS:
        raise ValueError(f'关机图案列表需为数组，当前最多支持{MAX_MENU_ITEMS}项')
    by_id = {asset['id']: asset for asset in normalized}
    entries, item_ids, selection_ids, legacy_slots = [], set(), set(), set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id']:
            raise ValueError('关机图案缺少稳定ID')
        selection_id = item.get('selection_id')
        if type(selection_id) is not int or not 1 <= selection_id <= 0xffffffff:
            raise ValueError('关机图案selection_id必须是正uint32')
        if item['id'] in item_ids or selection_id in selection_ids:
            raise ValueError('关机图案ID或selection_id重复')
        item_ids.add(item['id'])
        selection_ids.add(selection_id)
        asset_id = item.get('asset_id')
        if asset_id is not None and (not isinstance(asset_id, str) or asset_id not in by_id):
            raise ValueError('关机图案引用的图片素材不存在')
        fit = item.get('fit', 'contain')
        if fit not in ('contain', 'cover'):
            raise ValueError('fit must be contain or cover')
        row = {'id': item['id'], 'selection_id': selection_id,
               'name': normalize_item_name(item.get('name')), 'asset_id': asset_id, 'fit': fit}
        if 'legacy_slot' in item:
            legacy_slot = item['legacy_slot']
            if type(legacy_slot) is not int or legacy_slot not in (1, 2) or legacy_slot in legacy_slots:
                raise ValueError('旧关机图案槽位标识无效或重复')
            legacy_slots.add(legacy_slot)
            row['legacy_slot'] = legacy_slot
        if asset_id is not None:
            payload = item_menu_payload(by_id[asset_id], fit)
            row.update(preview_data_url=_data_url(payload), menu_preview_data_url=_data_url(payload),
                       menu_bytes=len(payload), menu_sha256=_sha(payload), menu_ready=True)
        else:
            row.update(preview_data_url=None, menu_preview_data_url=None,
                       menu_bytes=0, menu_sha256=None, menu_ready=False)
        entries.append(row)
    if selected_item is not None and (not isinstance(selected_item, str) or selected_item not in item_ids):
        raise ValueError('当前关机图案不存在')
    return {'schema_version': SCHEMA_VERSION, 'target_id': target_id,
            'selected_asset_id': selected, 'assets': normalized,
            'items': entries, 'selected_item_id': selected_item}


def replacement_bundle(draft: dict, inventory: dict, input_info: dict) -> bytes:
    """Export a future replacement plan/report, optionally an adapted JPEG."""
    value = validate_draft(draft, inventory)
    asset = next((row for row in value['assets'] if row['id'] == value['selected_asset_id']), None)
    if asset is None:
        raise ValueError('select a custom shutdown asset before exporting its plan')
    target = next(row for row in inventory['resources'] if row['id'] == value['target_id'])
    ready = asset['checks']['replacement_ready']
    source_hash = inventory.get('source_sha256')
    if input_info.get('sha256', source_hash) != source_hash:
        raise ValueError('replacement plan input differs from imported BIN')
    filename = f'replacement-{target["id"]}.jpg' if ready else None
    boundary = {
        'firmware_modified': False, 'firmware_build_created': False,
        'new_firmware_version_assigned': False, 'camera_selection_changed': False,
        'sd_card_written': False, 'camera_flashed': False,
        'hardware_decode_display_poweroff_verified': False,
    }
    plan = {
        'schema_version': SCHEMA_VERSION, 'kind': 'shutdown-static-resource-draft',
        'status': 'prepared' if ready else 'blocked', 'blocked': not ready,
        'applied': False, 'source_sha256': source_hash,
        'source_version': inventory.get('source_version', input_info.get('internal_version')),
        'actual_build_parent_sha256': source_hash,
        'compiler_baseline': 'separate concern; this package is not a firmware build',
        'target': {key: target[key] for key in ('id', 'native_id', 'path', 'size', 'width',
                                               'height', 'sha256', 'offset', 'record_offset')},
        'selection_condition': target['selection_condition'],
        'camera_current_selection': None,
        'replacement_file': filename,
        'replacement_sha256': asset['sha256'] if ready else None,
        'future_operation': 'Replace only this exact RES payload, preserving record length and all other resources; then rebuild and re-import the complete container with independent checks.',
        'reason': asset['adaptation']['reason'], 'boundary': boundary,
    }
    report = {
        'schema_version': SCHEMA_VERSION, 'blocked': not ready,
        'verification_kind': 'offline image decode and resource adaptation only',
        'checks': asset['checks'], 'adaptation': asset['adaptation'],
        'source_image': {key: asset[key] for key in ('filename', 'source_sha256', 'source_bytes',
                                                     'source_width', 'source_height', 'fit')},
        'prepared_image': {key: asset[key] for key in ('sha256', 'bytes', 'width', 'height', 'quality', 'encoding')},
        'import_inspection': inventory['inspection'], 'boundary': boundary,
        'next_stage_gaps': [
            'Implement an explicit RES-only replacement build path while preserving this imported BIN as the actual parent.',
            'Verify complete container rebuilding and re-imported resource identities; this ZIP is not an update BIN.',
            'Verify hardware JPEG acceptance, screen output, cancellation/completion and shutdown timing on a camera.',
            'A new camera menu/choice needs a separately traced parameter persistence and UserMode/default recovery path.',
        ],
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        if ready:
            archive.writestr(filename, _decode_b64(asset['payload_base64'], 'asset payload'))
        archive.writestr('plan.json', json.dumps(plan, ensure_ascii=False, indent=2) + '\n')
        archive.writestr('report.json', json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return buffer.getvalue()
