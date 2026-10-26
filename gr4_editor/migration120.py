# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only
"""Transfer user recipes between firmware families, never native code/pointers."""
from copy import deepcopy

from .shutdown import validate_draft, prepare_asset, _decode_b64


def migrate_configuration(source, target):
    if not str(source['input']['version']).startswith('1.11.'):
        raise ValueError('迁移来源必须为 1.11 固件')
    if not str(target['input']['version']).startswith('1.20.'):
        raise ValueError('迁移目标必须为 1.20 固件')
    rows, source_native = [], {}
    for row in source['filters']:
        if row.get('kind') != 'custom':
            source_native[row.get('identity', {}).get('style_id')] = row
    target_native = {row.get('identity', {}).get('style_id'): row
                     for row in target['filters'] if row.get('kind') != 'custom'}
    # Keep the user's common native-menu order/visibility, while retaining new
    # official 1.20 entries and all of their target-family resource descriptors.
    used = set()
    for row in source['filters']:
        style = row.get('identity', {}).get('style_id')
        if row.get('kind') == 'custom':
            item = deepcopy(row)
            item['id'] = 'migrated-' + str(row['id'])
            item['source_family'] = '1.11'
            item['source_identity'] = deepcopy(row.get('identity', {}))
            item['identity'] = {'style_id': style} if style is not None else {}
            # Completed colors are embedded. Machine-specific calibration jobs
            # and XMP file paths do not belong to the migrated BIN recipe.
            item.pop('xmp', None)
            item.pop('calibration', None)
            if row.get('pending_color_conversion'):
                raise ValueError('来源还有未完成的滤镜转换，请完成后再迁移')
            item['pending_color_conversion'] = False
            rows.append(item)
        elif style in target_native:
            item = deepcopy(target_native[style])
            if item.get('visibility_editable', True):
                item['enabled'] = bool(row.get('enabled', True))
            rows.append(item)
            used.add(style)
    rows.extend(deepcopy(row) for style, row in target_native.items() if style not in used)
    if sum(row.get('kind') == 'custom' and row.get('enabled', True) for row in rows) > target['capabilities']['max_custom_slots']:
        raise ValueError('来源活动滤镜数量超过 1.20 当前已验证容量，请先精简后迁移')
    for index, row in enumerate(rows):
        row['menu_order'] = index
    target['filters'] = rows

    factory = {row.get('identity', {}).get('public_id'): row
               for row in target['crops'] if row.get('kind') == 'factory'}
    crops = []
    for row in source['crops']:
        if row.get('kind') == 'factory':
            public = row.get('identity', {}).get('public_id')
            if public not in factory:
                raise ValueError('两版原厂裁切身份不匹配，不能迁移')
            crops.append(deepcopy(factory[public]))
        else:
            crops.append({'id': 'migrated-' + str(row['id']), 'kind': 'custom',
                          'editable': True, 'identity': {}, 'name': row['name'],
                          'requested_ratio': row['requested_ratio'],
                          'actual_ratio': row.get('actual_ratio', row['requested_ratio']),
                          'geometry': deepcopy(row.get('geometry', {})),
                          'source_geometry': deepcopy(row.get('geometry', {})),
                          'source_requested_ratio': row['requested_ratio'],
                          'source_identity': deepcopy(row.get('identity', {}))})
    target['crops'] = crops
    target['migration_crop_records'] = deepcopy(source.get('migration_crop_records') or
        source.get('crop_identity_context', {}).get('records', []))

    source_draft = source.get('shutdown', source.get('shutdown_draft'))
    inventory = target['shutdown_inventory']
    draft = validate_draft(None, inventory)
    by_id = {row['id']: row for row in (source_draft or {}).get('assets', [])}
    destinations = inventory.get('resources', [])
    destination = next((row for row in destinations if row['id'] == 'goodbye'),
                       destinations[0] if destinations else None)
    asset_mapping = {}
    for asset in by_id.values():
        if destination is None:
            raise ValueError('1.20 目标未读到可用关机资源')
        payload = _decode_b64(asset['source_payload_base64'], 'source shutdown picture')
        migrated = prepare_asset(payload, asset.get('filename', 'shutdown.jpg'),
                                 destination, asset.get('fit', 'contain'))
        draft['assets'].append(migrated)
        asset_mapping[asset['id']] = migrated['id']
    for item in (source_draft or {}).get('items', []):
        if item.get('asset_id') not in asset_mapping:
            raise ValueError('来源关机图案尚未选择图片，不能迁移')
        draft['items'].append({'id': 'migrated-' + item['id'], 'selection_id': item['selection_id'],
                               'name': item['name'], 'asset_id': asset_mapping[item['asset_id']],
                               'fit': item.get('fit', 'contain')})
    if source_draft:
        draft['selected_asset_id'] = asset_mapping.get(source_draft.get('selected_asset_id'))
        selected = source_draft.get('selected_item_id')
        draft['selected_item_id'] = 'migrated-' + selected if selected else None
    target['shutdown'] = validate_draft(draft, inventory)
    target['shutdown_draft'] = deepcopy(target['shutdown'])
    target['build_options'] = deepcopy(source.get('build_options', {'allow_older': True}))
    target['native_effects_draft'] = {'schema_version': 1, 'pictorialist': False}
    target['title'] = source['title'][:110] + ' · 1.20'
    target['migration'] = {'schema_version': 1, 'source_project_id': source['id'],
        'source': deepcopy(source['input']), 'target': deepcopy(target['input']),
        'source_version': source['input']['version'], 'target_version': target['input']['version'],
        'features': ['shutdown', 'crops', 'filters'],
        'custom_filters': sum(row.get('kind') == 'custom' for row in rows),
        'custom_crops': sum(row.get('kind') == 'custom' for row in crops),
        'shutdown_images': len(draft['items']), 'source_project_preserved': True,
        'method': 'decoded editable recipes and embedded image/color payloads; no 1.11 ARM code'}
    return target
