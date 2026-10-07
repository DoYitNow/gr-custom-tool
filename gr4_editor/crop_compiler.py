# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Coordinate the native crop menu, geometry, RAW and persistence consumers."""
from copy import deepcopy
import hashlib
import json

from .crop_registry import historical_records, plan_crops
from .crops import read_crops, recipe, changed, parse_ratio, preview_geometry, needs_geometry_repair

MAX_ACTIVE_CUSTOM_CROPS = 250  # RAW u8 count: four factory targets + current sentinel.


def prepare_crops(source, metadata, desired=None, prior=None, context=None):
    actual, inspection = read_crops(source)
    if inspection.get('native_readback') != 'passed':
        raise ValueError('输入裁切的原生读回失败：' + inspection.get('reason', '未知入口'))
    if prior is not None and (recipe(prior) != recipe(actual) or
            [row.get('geometry') for row in prior] != [row.get('geometry') for row in actual]):
        raise ValueError('输入与已读取的裁切清单/几何不一致')
    records = historical_records() + metadata.get('crop_registry', [])
    planning_rows = actual
    next_public = metadata.get('crop_next_public')
    if context:
        if context.get('source_rtos_sha256') != hashlib.sha256(source).hexdigest():
            raise ValueError('裁切身份历史与本项目输入 RTOS 不一致')
        known = {row['identity']['public_id']: row for row in records + actual}
        for row in context['records']:
            previous = known.get(row['identity']['public_id'])
            if previous and previous.get('geometry') != row.get('geometry'):
                raise ValueError('裁切身份历史不能改写已有照片的几何')
        registry = {row['identity']['public_id']: row for row in context['records']}
        order = context['order']
        if (len(registry) != len(context['records']) or len(set(order)) != len(order) or
                {public for public, row in registry.items() if row.get('active')} != set(order) or
                order[:4] != [0, 1, 3, 2]):
            raise ValueError('裁切身份历史的活动清单与记录不一致')
        records += context['records']
        # Repeated builds keep the same immutable imported parent. Use the
        # last compiled custom identities as the assignment history, while
        # factory protection and input verification still use actual bytes.
        planning_rows = [row for row in actual if row['kind'] == 'factory'] + [
            registry[public] for public in order if public >= 4]
        next_public = max(next_public or 7, context['next_public'])
    prepared = deepcopy(actual if desired is None else desired)
    originals = {row['id']: row for row in planning_rows}
    geometry_sources = actual + records
    replacements = []
    for row in prepared:
        previous = originals.get(row['id'])
        repair = previous is not None and needs_geometry_repair(previous)
        if row['kind'] == 'custom' and (previous is None or
                parse_ratio(row['requested_ratio']) != parse_ratio(previous['requested_ratio']) or repair):
            proposal = preview_geometry(source, row['requested_ratio'], geometry_sources=geometry_sources)
            row.update(geometry=proposal['geometry'], actual_ratio=proposal['actual_ratio'])
            if repair:
                replacements.append(row['id'])
    plan = plan_crops(planning_rows, prepared, records=records, next_public=next_public,
                      replace_geometry_ids=replacements)
    plan['repaired_geometry_ids'] = replacements
    plan['changed_from_input'] = changed(plan['crops'], actual)
    custom_count = sum(row['kind'] == 'custom' for row in plan['crops'])
    if custom_count > MAX_ACTIVE_CUSTOM_CROPS:
        raise ValueError('RAW 原生 byte 数量最多容纳 250 项活动自定义裁切（另有四个原厂项和当前图项）')
    return plan, inspection


def install_crops(patch, plan, icon_bytes):
    from .crop_icons import install_crop_icons
    from .crop_registry import install_menu
    from .crop_geometry import install_geometry
    from .crop_raw import install_raw
    from .crop_state import install_state_metadata
    from .crop_image_identity import install_image_identity
    # Every module uses the shared allocator and official recovery records.
    icons, icon_report = install_crop_icons(patch, icon_bytes, plan)
    menu = install_menu(patch, plan, icon_ids={row['public_id']: row['icon_id']
                                            for row in icon_report['custom']})
    geometry = install_geometry(patch, plan)
    raw = install_raw(patch, plan)
    state = install_state_metadata(patch, plan, menu['active_bitmap'])
    image_identity = install_image_identity(patch, plan)
    return icons, {'menu': menu, 'icons': icon_report, 'geometry': geometry, 'raw': raw,
            'state': state, 'image_identity': image_identity,
            'identity_policy': 'ratio changes retire the prior ID; deleted source geometry stays archived',
            'geometry_repaired_recipe_ids': plan.get('repaired_geometry_ids', []),
            'maximum_active_custom_crops': MAX_ACTIVE_CUSTOM_CROPS,
            'history_geometry_sha256': hashlib.sha256(json.dumps(
                plan['records'], sort_keys=True, separators=(',', ':'),
                ensure_ascii=False).encode('utf-8')).hexdigest(),
            'hardware': 'not_tested'}


def crop_metadata(plan, *, verified=False):
    # Firmware payloads, language resources and drafts are not needed to keep
    # the identity and mathematical source geometry across a later recompile.
    fields = ('id', 'kind', 'identity', 'name', 'requested_ratio', 'actual_ratio',
              'geometry', 'active', 'provenance')
    return {'crop_compiler': 'native-crops-v1', 'crop_next_public': plan['next_public'],
            'crop_order': list(plan['order']), 'crop_verified_geometry': verified,
            'crop_registry': [{key: deepcopy(row[key]) for key in fields if key in row}
                              for row in plan['records']]}


def freeze_readback(plan, actual, inspection):
    if inspection.get('native_readback') != 'passed' or not inspection.get('raw_recut_complete'):
        raise ValueError('生成裁切的原生几何/RAW 读回失败：' + inspection.get('reason', 'RAW 选项诊断'))
    if recipe(actual) != recipe(plan['crops']):
        raise ValueError('编译裁切的名称、身份或次序读回不一致')
    for expected, observed in zip(plan['crops'], actual):
        geometry = expected['geometry']
        for kind in ('screen', 'thumbnail'):
            for key in ('width', 'height', 'left', 'top', 'stride'):
                if geometry[kind][key] != observed['geometry'][kind][key]:
                    raise ValueError('原生裁切小图与编译配方不一致: ' + expected['name'])
        sizes = lambda rows: {(row['model'], row['selector'], row['magnification']):
                              (row['width'], row['height']) for row in rows}
        if sizes(geometry['photo_sizes']) != sizes(observed['geometry']['photo_sizes']):
            raise ValueError('原生裁切主尺寸与编译配方不一致: ' + expected['name'])
    archived = {row['identity']['public_id']: row for row in plan['records']}
    for row in actual:
        archived[row['identity']['public_id']] = {**deepcopy(row), 'active': True}
    plan['records'] = [archived[key] for key in sorted(archived)]
    plan['crops'] = deepcopy(actual)
