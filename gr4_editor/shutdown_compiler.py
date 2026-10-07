# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Compile an embedded shutdown picture list; keep original resources intact.

This module prepares local candidate bytes. It never accesses a camera or card.
Native menu and state installers share the crop allocator and preserved native
APData save/load lifecycle.
"""
from copy import deepcopy
from io import BytesIO
import struct
import re

from PIL import Image, ImageOps

from .firmware import BASE, _Memory
from .shutdown import (_b64, _decode_b64, _jpeg_info, _res_records,
                       _sha, _source_image, validate_draft, item_menu_payload,
                       MAX_MENU_ITEMS, normalize_item_name)

PATHS = ('A:\\Resource\\Jpeg\\GB_C1.jpg', 'A:\\Resource\\Jpeg\\GB_C2.jpg')
RESOURCE_IDS = tuple(range(12, 12 + MAX_MENU_ITEMS))
TABLE_SITES = ((0x5326255C, 0x5326256C), (0x53262324, 0x5326232C))


def encode_menu_image(source, fit):
    """Use the uploaded source, independently of the old replacement budget."""
    picture, _ = _source_image(source)
    if fit == 'contain':
        resized = ImageOps.contain(picture, (720, 480), Image.Resampling.LANCZOS)
        canvas = Image.new('RGB', (720, 480), 'black')
        canvas.paste(resized, ((720 - resized.width) // 2, (480 - resized.height) // 2))
    elif fit == 'cover':
        canvas = ImageOps.fit(picture, (720, 480), Image.Resampling.LANCZOS)
    else:
        raise ValueError('关机图片适配方式必须为 contain 或 cover')
    output = BytesIO()
    canvas.save(output, 'JPEG', quality=95, optimize=True, progressive=False, subsampling=2)
    payload = output.getvalue()
    info = _jpeg_info(payload)
    if not (info['baseline'] and info['mode'] == 'RGB' and
            (info['width'], info['height']) == (720, 480)):
        raise ValueError('机内关机图片 JPEG 转换失败')
    return payload


def prepare_menu_plan(image, inventory, draft):
    value = validate_draft(draft, inventory)
    previous = _owned_menu(image)
    owned_paths = {row['path'] for row in previous['presets']} if previous else set()
    if not value['items']:
        return None, rebuild_res(image, [], owned_paths) if owned_paths else None
    if not inventory.get('capabilities', {}).get('can_build_menu'):
        raise ValueError(inventory.get('capabilities', {}).get('menu_build_reason', '该输入尚未支持机内关机菜单'))
    assets = {row['id']: row for row in value['assets']}
    presets = []
    for index, item in enumerate(value['items']):
        asset_id = item['asset_id']
        asset = assets.get(asset_id)
        if asset is None:
            raise ValueError(f'请为关机图案「{item["name"]}」选择图片')
        payload = item_menu_payload(asset, item['fit'])
        presets.append({'slot': index + 1, 'index': index, 'native_id': RESOURCE_IDS[index],
                        'selection_id': item['selection_id'], 'item_id': item['id'],
                        'path': menu_path(item['selection_id']), 'asset_id': asset_id, 'name': item['name'],
                        'source_sha256': asset['source_sha256'], 'fit': item['fit'],
                        'width': 720, 'height': 480, 'bytes': len(payload),
                        'sha256': _sha(payload), 'payload_base64': _b64(payload)})
        if 'legacy_slot' in item:
            presets[-1]['legacy_slot'] = item['legacy_slot']
    plan = {'schema_version': 2, 'enabled': True, 'presets': presets,
            'legacy_choices': {str(item['legacy_slot']): item['selection_id']
                               for item in value['items'] if 'legacy_slot' in item},
            'default_selection': 0, 'selection_scope': 'global',
            'hardware': 'not_tested'}
    plan['factory_previews'] = [{key: row[key] for key in ('native_id', 'sha256', 'payload_base64')}
                                for row in inventory['resources'] if row['native_id'] in (1, 2, 3, 11)]
    return plan, rebuild_res(image, presets, owned_paths)


def menu_path(selection_id):
    path = 'A:\\Resource\\Jpeg\\G' + format(selection_id, '08x') + '.jpg'
    if len(path.encode('ascii')) >= 32:
        raise ValueError('机内关机图片路径超过RES固定字段')
    return path


def _owned_menu(image, metadata=None):
    """Only a fresh descriptor/file readback establishes owned records."""
    if metadata is None:
        from .demo_build import _metadata
        metadata = _metadata(image.rtos) or {}
    return read_menu_resources(image, metadata)


def rebuild_res(image, presets, owned_paths=None):
    """Retire verified owned records; preserve every unrelated header/payload."""
    records = _res_records(image)
    if owned_paths is None:
        installed = _owned_menu(image)
        owned_paths = {row['path'] for row in installed['presets']} if installed else set()
    owned_paths = set(owned_paths)
    if not owned_paths.issubset(records):
        raise ValueError('已验证关机资源在RES中不存在')
    section = next(row for row in image.sections if row['name'] == 'RES')
    header = bytearray(image.decoded[section['start']:section['start'] + 16])
    desired = {row['path']: _decode_b64(row['payload_base64'], 'menu picture') for row in presets}
    if len(desired) != len(presets):
        raise ValueError('关机图片路径重复')
    if any(path in records and path not in owned_paths for path in desired):
        raise ValueError('关机图片路径与未归属本菜单的RES资源冲突')
    result = bytearray(header)
    count = 0
    for path, record in records.items():
        if path not in owned_paths:
            result.extend(image.decoded[record['record_offset']:record['offset'] + record['size']])
            count += 1
    for path, payload in desired.items():
        encoded_path = path.encode('ascii')
        if len(encoded_path) >= 32:
            raise ValueError('关机图片路径超过RES固定字段')
        result.extend(encoded_path.ljust(32, b'\0') + struct.pack('<I', len(payload)) + payload)
        count += 1
    struct.pack_into('<I', result, 12, count)
    size = max((len(result) + 3) & ~3, section['end'] - section['start'])
    result.extend(bytes(size - len(result)))
    return bytes(result)


def install_jpeg_table(patch, plan):
    """Extend the original twelve descriptors in the user-visible list order."""
    from .demo_build import _mov
    memory = _Memory(bytes(patch.image))
    original = memory.read(0x55013E70, 12 * 4)
    pointers = []
    for preset in plan['presets']:
        path = patch.append(preset['path'].encode('ascii') + b'\0')
        pointers.append(patch.append(struct.pack('<HHI', 720, 480, path)))
    table_bytes = original + struct.pack('<' + str(len(pointers)) + 'I', *pointers)
    table = patch.append(table_bytes, 16)
    for low, high in TABLE_SITES:
        register = (patch.word(low) >> 12) & 15
        patch.set_word(low, _mov(register, table & 0xffff), 'expanded shutdown JPEG table low')
        patch.set_word(high, _mov(register, table >> 16, high=True), 'expanded shutdown JPEG table high')
    return {'address': hex(table), 'count': 12 + len(pointers), 'preserved_original_count': 12,
            'descriptors': [hex(pointer) for pointer in pointers],
            'sha256': _sha(table_bytes)}


def next_record(source_metadata):
    """Use the demo's own record; leave native and prior research records alone."""
    previous = source_metadata.get('shutdown_menu', {}).get('record', 74)
    if previous != 74:
        raise ValueError('Demo 关机状态必须使用独立 APData74')
    return 74


def menu_metadata(plan, table, state, menu, previews=None):
    result = deepcopy(plan)
    result.pop('factory_previews', None)
    for row in result['presets']:
        row.pop('payload_base64', None)
    result.update(jpeg_table=table, state=state, menu=menu, record=state['record'])
    if previews is not None:
        result['previews'] = previews
    return result


def _verify_menu_names(memory, compiled, presets):
    """Check the UTF-16 descriptors actually selected by every language table."""
    if compiled.get('schema_version', 1) == 1:
        return {'status': 'legacy_fixed_labels', 'custom_names_verified': False}
    from .demo_build import TEXT_CATALOG
    labels = compiled.get('menu', {}).get('labels', {})
    text_ids = labels.get('image_text_ids')
    count = labels.get('catalog_entries')
    if (not isinstance(text_ids, list) or len(text_ids) != len(presets) + 1
            or type(count) is not int or any(type(value) is not int or not 0 <= value < count
                                          for value in text_ids)
            or len(set(text_ids)) != len(text_ids)):
        raise ValueError('机内关机图案名称目录或文字ID无效')
    for language in range(21):
        table = memory.word(TEXT_CATALOG + language * 4)
        for text_id, row in zip(text_ids[1:], presets):
            descriptor = memory.word(table + text_id * 4)
            header = memory.read(descriptor, 8)
            expected = (row['name'] + '\0').encode('utf-16-le')
            units = len(expected) // 2
            if header[:4] != bytes((units, 1, 0, 0)):
                raise ValueError(f'机内关机图案名称描述符读回不一致：语言{language}，{row["name"]}')
            pointer = struct.unpack_from('<I', header, 4)[0]
            if memory.read(pointer, units * 2) != expected:
                raise ValueError(f'机内关机图案实际名称读回不一致：语言{language}，{row["name"]}')
    return {'status': 'passed', 'custom_names_verified': True, 'language_count': 21,
            'custom_count': len(presets), 'text_ids': text_ids[1:]}


def read_menu_resources(image, metadata):
    """Read each descriptor/file afresh; compare actual bytes to compiler claims."""
    compiled = metadata.get('shutdown_menu')
    if not compiled:
        return None
    schema = compiled.get('schema_version', 1)
    if schema not in (1, 2) or not isinstance(compiled.get('presets'), list):
        raise ValueError('机内关机菜单元数据版本或列表无效')
    expected_rows = compiled['presets']
    if not 1 <= len(expected_rows) <= MAX_MENU_ITEMS or (schema == 1 and len(expected_rows) != 2):
        raise ValueError('机内关机菜单项数量无效')
    if compiled['jpeg_table'].get('count') != 12 + len(expected_rows):
        raise ValueError('机内关机JPEG表数量不一致')
    memory = _Memory(image.rtos)
    table = int(compiled['jpeg_table']['address'], 16)
    if any(memory.pair(low, high) != table for low, high in TABLE_SITES):
        raise ValueError('机内关机 JPEG 表入口不一致')
    records = _res_records(image)
    presets = []
    selections, item_ids = set(), set()
    for index, source_row in enumerate(expected_rows):
        expected = dict(source_row)
        if expected.get('native_id') != 12 + index or expected.get('slot') != index + 1:
            raise ValueError('机内关机菜单资源顺序不一致')
        if schema == 1:
            expected.update(selection_id=expected['slot'], legacy_slot=expected['slot'],
                            item_id='shutdown-item-legacy-' + str(expected['slot']))
            if expected.get('path') != PATHS[index]:
                raise ValueError('旧机内关机菜单资源路径无效')
        else:
            selection_id = expected.get('selection_id')
            item_id = expected.get('item_id')
            if type(selection_id) is not int or not 1 <= selection_id <= 0xffffffff:
                raise ValueError('机内关机图案selection_id无效')
            if not isinstance(item_id, str) or not item_id or expected.get('path') != menu_path(selection_id):
                raise ValueError('机内关机图案ID或资源路径无效')
            normalize_item_name(expected.get('name'))
        if expected['selection_id'] in selections or expected['item_id'] in item_ids:
            raise ValueError('机内关机图案ID重复')
        selections.add(expected['selection_id'])
        item_ids.add(expected['item_id'])
        descriptor = memory.word(table + expected['native_id'] * 4)
        width, height, path_at = struct.unpack('<HHI', memory.read(descriptor, 8))
        path = memory.read(path_at, len(expected['path']) + 1)
        if path != expected['path'].encode() + b'\0' or (width, height) != (720, 480):
            raise ValueError('机内关机图片描述符读回不一致')
        record = records.get(expected['path'])
        if record is None:
            raise ValueError('机内关机图片不在实际 RES 中')
        payload = image.decoded[record['offset']:record['offset'] + record['size']]
        info = _jpeg_info(payload)
        if _sha(payload) != expected['sha256'] or len(payload) != expected['bytes']:
            raise ValueError('机内关机图片 SHA/长度读回不一致')
        if not (info['baseline'] and info['mode'] == 'RGB' and
                (info['width'], info['height']) == (width, height)):
            raise ValueError('机内关机图片编码读回不一致')
        presets.append({**expected, 'payload_base64': _b64(payload),
                        'preview_data_url': 'data:image/jpeg;base64,' + _b64(payload),
                        'jpeg_info': info, 'descriptor_address': descriptor, **record})
    name_readback = _verify_menu_names(memory, compiled, presets)
    preview_readback = None
    if compiled.get('previews'):
        from .shutdown_preview import verify_shutdown_previews
        previews = compiled['previews']
        custom = previews['custom']
        if ([row['selection_id'] for row in custom] != [row['selection_id'] for row in presets] or
                [row['source_jpeg_sha256'] for row in custom] != [row['sha256'] for row in presets] or
                [row['native_id'] for row in custom] != [row['native_id'] for row in presets]):
            raise ValueError('机内关机预览与图案列表不一致')
        menu_preview = compiled['menu'].get('preview', {})
        factory_icons = {str(row['native_id']): row['icon_id'] for row in previews['factory']}
        if (not menu_preview.get('installed') or
                {str(key): value for key, value in menu_preview['factory_icon_ids'].items()} != factory_icons or
                menu_preview['custom_icon_ids'] != [row['icon_id'] for row in custom]):
            raise ValueError('机内关机菜单与预览资源映射不一致')
        preview_readback = verify_shutdown_previews(image, previews)
    return {**compiled, 'presets': presets, 'resource_readback': 'passed',
            'name_readback': name_readback,
            'preview_readback': preview_readback,
            'camera_current_selection': None, 'hardware': 'not_tested'}


def verify_original_res(before, after, before_metadata=None, after_metadata=None):
    original, actual = _res_records(before), _res_records(after)
    previous = _owned_menu(before, before_metadata)
    installed = _owned_menu(after, after_metadata)
    before_owned = {row['path'] for row in previous['presets']} if previous else set()
    after_owned = {row['path'] for row in installed['presets']} if installed else set()
    preserved = []
    for path, row in original.items():
        if path in before_owned:
            continue
        observed = actual.get(path)
        if observed is None or before.decoded[row['record_offset']:row['offset'] + row['size']] != after.decoded[observed['record_offset']:observed['offset'] + observed['size']]:
            raise ValueError('关机菜单构建改变了其它原始 RES 资源：' + path)
        preserved.append(path)
    if set(actual) - set(original) - after_owned:
        raise ValueError('关机菜单构建增加了未计划的 RES 资源')
    return {'status': 'byte_exact', 'count': len(preserved), 'paths': preserved}
