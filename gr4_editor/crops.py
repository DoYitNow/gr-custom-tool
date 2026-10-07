# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Actual-BIN crop inventory and geometry proposals for the native compiler."""
from copy import deepcopy
from fractions import Fraction
import hashlib
import re

from .firmware import _Memory, _name_resource, _immediate, _half
from .crop_native import CropNative

ORDER_SITES = (0x531BAF88, 0x531BB26C, 0x531BE31C, 0x531D0CE8,
               0x531D2F88, 0x531D48A0, 0x5325AE2C)
COUNT_SITES = (0x531BA14C, 0x531CF298)
WRITE_REASON = '该输入未匹配原生裁切编译布局；可以保存几何草稿。'


def ratio_text(width, height):
    ratio = Fraction(width, height)
    return f'{ratio.numerator}:{ratio.denominator}'


def parse_ratio(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('请输入宽:高或正数比例')
    fields = re.split(r'[:：/]', value.strip())
    if len(fields) not in (1, 2) or any(not re.fullmatch(r'\d+(?:\.\d+)?', field.strip()) for field in fields):
        raise ValueError('比例格式应为 5:4、2.35:1 或 2.35')
    try:
        result = Fraction(fields[0].strip()) / (Fraction(fields[1].strip()) if len(fields) == 2 else 1)
    except ZeroDivisionError as exc:
        raise ValueError('比例的宽高必须大于零') from exc
    if result <= 0:
        raise ValueError('比例的宽高必须大于零')
    return result


def capabilities(inspection):
    known = inspection.get('native_readback') == 'passed'
    writable = known and bool(inspection.get('compiler_supported'))
    return {'can_edit': known, 'can_add': known, 'can_write': writable,
            'draft_only': not writable,
            'reason': ('可生成自定义裁切的离线候选；比例按各出口整数尺寸对齐，实机待验证。' if writable else
                       WRITE_REASON if known else inspection.get('reason', '未知裁切布局，只能检查')),
            'native_identity_storage_bits': 8 if known else None,
            'writable_custom_capacity': 250 if writable else None,
            'next_public': inspection.get('next_public'),
            'capacity_basis': 'RAW byte count: four factory targets plus 250 custom targets plus current=255; hardware heap not proved',
            'hardware': 'not_tested'}


def menu_layout(memory):
    counts = []
    for site in COUNT_SITES:
        word = memory.word(site)
        if memory.word(site + 4) != 0xE12FFF1E:
            raise ValueError(f'裁切数量回调的返回入口未知 {site:#x}')
        if word & 0xFFFFF000 == 0xE3A00000:
            counts.append(_immediate(word))
        elif word & 0xFFF0F000 == 0xE3000000:
            counts.append(_half(word))
        else:
            raise ValueError(f'裁切数量入口未知 {site:#x}')
    if counts[0] != counts[1] or not 1 <= counts[0] <= 256:
        raise ValueError('拍摄菜单与设置页的裁切数量不一致或超出字节身份空间')
    addresses = [memory.pair(site, site + (8 if site in (0x531BE31C, 0x531D0CE8) else 4))
                 for site in ORDER_SITES]
    if len(set(addresses)) != 1:
        raise ValueError('裁切菜单/快捷切换没有引用一致的次序表')
    order = list(memory.read(addresses[0], counts[0]))
    if len(set(order)) != len(order):
        raise ValueError('原生裁切菜单包含重复身份')
    return {'menu_count': counts[0], 'order': order, 'order_address': hex(addresses[0]),
            'count_sites': list(map(hex, COUNT_SITES)), 'order_sites': list(map(hex, ORDER_SITES))}


def read_crops(rtos):
    """Enumerate the imported menu, then execute every observed identity.

    Metadata, history and the local ledger never supply the menu or geometry.
    Partial observations stay visible if a consumer fails.
    """
    rows = []
    evidence = {'rtos_sha256': hashlib.sha256(rtos).hexdigest(), 'native_readback': 'unknown',
                'hardware': 'not_tested', 'camera_or_card_written': False,
                'scope': 'synthetic objects; native ARM geometry, two model branches, size selectors, descriptors and RAW option count/index'}
    try:
        memory = _Memory(rtos)
        layout = menu_layout(memory)
        order = layout['order']
        evidence.update(layout)
        from .demo_build import _metadata, supports_image
        metadata = _metadata(rtos) or {}
        crop_metadata = metadata.get('crop_compiler') == 'native-crops-v1'
        archived = {row['identity']['public_id']: row for row in metadata.get('crop_registry', [])}
        if crop_metadata and (metadata.get('crop_order') != order or
                {public for public, row in archived.items() if row.get('active')} != set(order)):
            raise ValueError('内嵌裁切配方与实际菜单次序/身份不一致')
        native = CropNative(rtos)
        mags = {model: native.magnifications(model) for model in ('Kb588', 'Kb636')}
        for public in order:
            row = {'id': f'crop-{public}', 'identity': {'public_id': public},
                   'kind': 'factory' if public <= 3 else 'custom', 'editable': False,
                   'name': f'比例 {public}', 'inspection_notes': []}
            rows.append(row)
            text_id = native.call(0x5337FBAC, 0, public)
            name, resource = _name_resource(memory, text_id)
            if not name or not text_id:
                raise ValueError(f'裁切身份 {public} 未解析到原生名称')
            row.update(name=name, resources={'name': resource})
            row['identity']['text_id'] = text_id
            internal = native.preview_id(public)
            row['identity']['preview_id'] = internal
            preview = native.preview(internal)
            if native.diagnostics or not preview['height'] or not preview['width']:
                raise ValueError(f'裁切身份 {public} 的预览几何未读回')
            screens = {model: native.small(model, public, 'screen') for model in mags}
            thumbs = {model: native.small(model, public, 'thumb') for model in mags}
            for small in (*screens.values(), *thumbs.values()):
                small['actual_ratio'] = ratio_text(small['width'], small['height'])
            sizes = [native.dimensions(model, public, mag, selector) for model in mags
                     for selector in range(4) for mag in mags[model]]
            for size in sizes:
                descriptor = native.main_descriptor(size['model'], public, size['magnification'], size['selector'])
                if descriptor['diagnostics'] or (descriptor['width'], descriptor['height']) != (size['width'], size['height']):
                    raise ValueError('照片尺寸与原生主图描述符不一致')
                size['main_descriptor'] = descriptor
                size['actual_ratio'] = ratio_text(size['width'], size['height'])
            dng = [native.dng_window(model, public, mag) for model in mags for mag in mags[model]]
            if any(item['diagnostics'] for item in dng):
                raise ValueError('原生 DNG 窗口走了诊断/回退路径')
            for item in dng:
                full = native.dimensions(item['model'], 0, 1.0, 0)
                if (2 * (item['left'] - item['sensor_origin'][0]) + item['width'],
                    2 * (item['top'] - item['sensor_origin'][1]) + item['height']) != (full['width'], full['height']):
                    raise ValueError('原生 DNG 窗口没有保持型号对应的中心公式')
            if any(item['diagnostics'] or not item['width'] or not item['height'] for item in [*screens.values(), *thumbs.values(), *sizes]):
                raise ValueError(f'裁切身份 {public} 的尺寸函数走了诊断/回退路径')
            actual = ratio_text(preview['width'], preview['height'])
            row['requested_ratio'] = name if re.fullmatch(r'\d+(?:\.\d+)?:\d+(?:\.\d+)?', name) else actual
            row['actual_ratio'] = actual
            row['geometry'] = {'preview': preview, 'screen': screens['Kb636'], 'thumbnail': thumbs['Kb636'],
                               'models': {model: {'screen': screens[model], 'thumbnail': thumbs[model],
                                                  'magnifications': mags[model]} for model in mags},
                               'photo_sizes': sizes, 'dng_windows': dng}
            if crop_metadata:
                declared = archived[public]
                if declared.get('name') != name or declared['identity'] != row['identity']:
                    raise ValueError('内嵌裁切配方与实际名称/身份不一致')
                if metadata.get('crop_verified_geometry') and declared.get('geometry') != row['geometry']:
                    raise ValueError('内嵌裁切几何与实际 ARM 消费者不一致')
                # Logical draft IDs and requested ratio spellings are recipe
                # fields; menu identities and every geometry still come from
                # the imported instructions, never from this footer.
                row['id'] = declared['id']
                row['requested_ratio'] = declared['requested_ratio']
            row['raw_development'] = [native.raw_options(public, current) for current in (False, True)]
            if any(item['diagnostics'] for item in row['raw_development']):
                row['inspection_notes'].append('实际 RAW 重裁切选项函数对该源图比例进入原生诊断路径；当前图像比例桥不等于可重裁切。')
            row['editable'] = public > 3
        evidence.update(native_readback='passed', model_branches=list(mags),
                        detected_custom_count=sum(row['kind'] == 'custom' for row in rows),
                        raw_recut_complete=not any(item['diagnostics'] for row in rows for item in row['raw_development']),
                        compiler_supported=supports_image(rtos), crop_compiler=metadata.get('crop_compiler'),
                        next_public=metadata.get('crop_next_public', max([6, *order]) + 1))
    except Exception as exc:
        evidence['reason'] = '裁切布局只能检查：' + str(exc)
        for row in rows:
            row['editable'] = False
    return rows, evidence


def recipe(rows):
    return [{key: deepcopy(row.get(key)) for key in ('id', 'kind', 'identity', 'name', 'requested_ratio')} for row in rows]


def changed(rows, originals):
    return recipe(rows) != recipe(originals)


def _aligned_crop(width, height, ratio):
    # Symmetric even origins require dimensions in multiples of four.
    def nearest4(value):
        return 4 * ((value + 2) // 4)
    if ratio >= Fraction(width, height):
        w, h = width, int(nearest4(Fraction(width, 1) / ratio))
    else:
        w, h = int(nearest4(Fraction(height, 1) * ratio)), height
    if not 4 <= w <= width or not 4 <= h <= height:
        raise ValueError('请求比例在现有画布中无法保留至少 4×4 的对称对齐区域')
    return {'width': w, 'height': h, 'left': (width - w) // 2, 'top': (height - h) // 2,
            'canvas': [width, height], 'actual_ratio': ratio_text(w, h)}


def needs_geometry_repair(row):
    """The first editor copied retired .12 public6 into new 65:24 IDs.

    Keep that old geometry for existing photographs; a newly active recipe
    must get an aligned, single-axis screen nail under a fresh identity.
    """
    public = (row.get('identity') or {}).get('public_id', 0)
    screen = row.get('geometry', {}).get('screen', {})
    return (public >= 7 and parse_ratio(row['requested_ratio']) == Fraction(65, 24)
            and tuple(screen.get(key) for key in ('width', 'height', 'left', 'top'))
            == (704, 260, 8, 110))


def preview_geometry(rtos, ratio, *, geometry_sources=None):
    requested = parse_ratio(ratio)
    if geometry_sources is None:
        from .crop_registry import historical_records
        from .demo_build import _metadata
        observed, inspection = read_crops(rtos)
        if inspection.get('native_readback') != 'passed':
            raise ValueError(inspection.get('reason', '原生基础裁切未解析'))
        geometry_sources = observed + historical_records() + (_metadata(rtos) or {}).get('crop_registry', [])
    # Only the original four and the two stable native extensions have
    # exceptional, intentionally retained geometry. Retired trials are photo
    # archives, not templates for newly added proportions (notably public6).
    equivalent = next((row for row in geometry_sources if row.get('geometry') and
                       (row.get('identity') or {}).get('public_id', 256) <= 5 and
                       parse_ratio(row['requested_ratio']) == requested), None)
    if equivalent:
        geometry = deepcopy(equivalent['geometry'])
        actual = parse_ratio(equivalent['actual_ratio'])
    else:
        screen = {**_aligned_crop(720, 480, requested), 'stride': 736}
        actual = Fraction(screen['width'], screen['height'])
        geometry = {'screen': screen, 'thumbnail': {**_aligned_crop(160, 120, actual), 'stride': 160},
                    'photo_sizes': []}
    native = CropNative(rtos)
    if not equivalent:
        for model in ('Kb588', 'Kb636'):
            for selector in range(4):
                for mag in native.magnifications(model):
                    base = native.dimensions(model, 0, mag, selector)
                    if base['diagnostics']:
                        raise ValueError('原生基础尺寸进入诊断路径，不能规划裁切')
                    region = _aligned_crop(base['width'], base['height'], actual)
                    geometry['photo_sizes'].append({**region, 'model': model, 'selector': selector, 'magnification': mag})
    screen = geometry['screen']
    for size in geometry['photo_sizes']:
        roi = native.fit_roi((size['width'], size['height']), (screen['width'], screen['height']))
        size.update(quickview_roi=roi, quickview_full_target=roi == [0, 0, screen['width'], screen['height']])
        if not equivalent and not size['quickview_full_target']:
            raise ValueError('这个比例在照片尺寸对齐后无法铺满回看区域，请调整比例')
    return {'requested_ratio': ratio, 'actual_ratio': ratio_text(actual.numerator, actual.denominator),
            'alignment_changed_ratio': requested != actual,
            'relative_error_percent': float(actual / requested - 1) * 100,
            'geometry': geometry,
            'equivalent_native_geometry_reused': equivalent is not None,
            'native_write_supported': False, 'status': 'geometry_draft',
            'reason': '构建前几何预案；生成时还会检查原生开发分块、回放、状态与实际读回。',
            'constraints': ['symmetric even origins; dimensions divisible by four', 'original canvas and stride retained',
                            'base photo dimensions and magnifications read from imported ARM',
                            'per-size QuickView aspect-fit executed; full-target mismatch retained'],
            'hardware': 'not_tested'}


def validate_draft(rows, project):
    if not isinstance(rows, list):
        raise ValueError('裁切草稿必须是列表')
    if not project['crop_capabilities']['can_edit']:
        if changed(rows, project['original_crops']):
            raise ValueError(project['crop_capabilities']['reason'])
        return deepcopy(project['original_crops'])
    originals = {row['id']: row for row in project['original_crops']}
    current = {row['id']: row for row in project['crops']}
    identities = set()
    ids = set()
    result = []
    for row in rows:
        key = row.get('id')
        if not isinstance(key, str) or not key or key in ids:
            raise ValueError('裁切草稿标识必须唯一')
        ids.add(key)
        original = originals.get(key)
        known = original or current.get(key)
        if original and original['kind'] == 'factory':
            if recipe([row]) != recipe([original]):
                raise ValueError('原厂裁切公共编号及 1:1 必须保留')
            result.append(deepcopy(original))
            continue
        if row.get('kind') != 'custom' or row.get('identity') != (known or {}).get('identity'):
            raise ValueError('自定义裁切身份由原生编译器分配，不能手工修改')
        if row.get('identity'):
            number = row['identity']['public_id']
            if number in identities:
                raise ValueError('裁切身份重复')
            identities.add(number)
        name = row.get('name')
        if not isinstance(name, str) or not name.strip() or len(name.encode('utf-16-le')) // 2 > 80:
            raise ValueError('裁切名称必须为 1–80 个 UTF-16 字符')
        parse_ratio(row.get('requested_ratio'))
        clean = deepcopy(known or {})
        clean.update({key: deepcopy(row.get(key)) for key in ('id', 'name', 'kind', 'identity', 'requested_ratio')})
        clean['editable'] = True
        if original and row['requested_ratio'] == original['requested_ratio']:
            clean['geometry'] = deepcopy(original['geometry'])
            clean['actual_ratio'] = original['actual_ratio']
            clean.pop('geometry_status', None)
        else:
            # Geometry is regenerated by the service from the actual RTOS;
            # never trust client-supplied dimensions or support claims.
            clean.pop('geometry', None)
            clean.pop('actual_ratio', None)
        result.append(clean)
    if any(row['kind'] == 'factory' and row['id'] not in ids for row in originals.values()):
        raise ValueError('原厂裁切公共编号及 1:1 必须保留')
    return result
