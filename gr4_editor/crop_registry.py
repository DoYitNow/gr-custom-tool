# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Crop identity planning and menu compilation on the shared RTOS allocator.

This module alone does not make a crop writable. Geometry, RAW, state and
playback consumers must be installed by the containing compiler as well.
"""
from copy import deepcopy
import struct

from .crops import ORDER_SITES, COUNT_SITES, parse_ratio

FACTORY_ORDER = (0, 1, 3, 2)
TEXT_FIRST = 900  # Keep crop names below the End Screen menu namespace.
# Keep the native extension identities reserved; imported registry records
# and a user's project context contain any additional retired identities.
RESERVED_HISTORICAL_IDS = (6,)


def historical_records():
    """No author research archive is bundled with the standalone editor."""
    return []


def text_id(public):
    return {0: 267, 1: 268, 2: 266, 3: 269, 4: 838, 5: 839}.get(public, TEXT_FIRST + public - 6)


def plan_crops(originals, desired, *, records=(), next_public=None, replace_geometry_ids=()):
    """Keep names/order stable; retire geometry identities instead of reusing.

    The public field is a byte. This proves an identity storage boundary, not
    that every possible row count or ratio is accepted by camera hardware.
    """
    original_by_id = {row['id']: row for row in originals}
    archive = {row['identity']['public_id']: deepcopy(row) for row in records}
    for row in originals:
        archive[row['identity']['public_id']] = deepcopy(row)
    high = max([3, *RESERVED_HISTORICAL_IDS, *archive]) + 1
    if next_public is not None:
        if type(next_public) is not int or not high <= next_public <= 256:
            raise ValueError('裁切身份高水位与已有记录不一致')
        high = next_public
    seen = set()
    active = []
    for value in desired:
        row = deepcopy(value)
        key = row['id']
        if key in seen:
            raise ValueError('裁切配方标识重复')
        seen.add(key)
        previous = original_by_id.get(key)
        if row['kind'] == 'factory':
            if not previous or row != previous:
                raise ValueError('原厂裁切不可改写')
            active.append(row)
            continue
        if row['kind'] != 'custom':
            raise ValueError('裁切类型未知')
        requested = parse_ratio(row['requested_ratio'])
        same_geometry = (previous and key not in replace_geometry_ids and
                         requested == parse_ratio(previous['requested_ratio']))
        if same_geometry:
            public = previous['identity']['public_id']
            # Equivalent decimal/fraction spellings must not reinterpret an
            # existing photo identity using newly rounded draft geometry.
            for field in ('geometry', 'actual_ratio'):
                if field in previous:
                    row[field] = deepcopy(previous[field])
            row.pop('geometry_status', None)
        else:
            if high >= 256:
                raise ValueError('裁切公共身份的 byte 空间已耗尽；不能重用旧照片中的身份')
            public, high = high, high + 1
        row['identity'] = {'public_id': public, 'preview_id': public, 'text_id': text_id(public)}
        active.append(row)
    factory = [row['identity']['public_id'] for row in active if row['kind'] == 'factory']
    if factory != list(FACTORY_ORDER) or any(row['kind'] != 'factory' for row in active[:4]):
        raise ValueError('原厂四项顺序和 1:1 必须保留在自定义项之前')
    active_ids = {row['identity']['public_id'] for row in active}
    if len(active_ids) != len(active):
        raise ValueError('裁切公共身份重复')
    for public, row in archive.items():
        row['active'] = public in active_ids
    for row in active:
        archive[row['identity']['public_id']] = {**deepcopy(row), 'active': True}
    return {'crops': active, 'records': [archive[key] for key in sorted(archive)],
            'next_public': high, 'order': [row['identity']['public_id'] for row in active],
            'identity_storage_bits': 8,
            'geometry_change_retires_previous_identity': True,
            'hardware': 'not_tested'}


def install_menu(patch, plan, *, icon_ids=None):
    """Compile all menu selectors, names, outline mapping and validator.

    Reuses the original setter/notifications. Active settings validation is
    intentionally distinct from the archive used to interpret older images.
    """
    from .demo_build import BASE, TEXT_CATALOG, _branch, _mov, _read, _half
    active = plan['crops']
    records = plan['records']
    count = len(active)
    if not 4 <= count <= 256:
        raise ValueError('裁切数量超出公共 byte 身份空间')
    order = patch.append(bytes(plan['order']), 16)
    for site in ORDER_SITES:
        high = site + (8 if site in (0x531BE31C, 0x531D0CE8) else 4)
        patch.set_word(site, _mov(3, order & 65535), 'crop menu order low')
        patch.set_word(high, _mov(3, order >> 16, high=True), 'crop menu order high')
    for site in COUNT_SITES:
        patch.set_word(site, _mov(0, count), 'crop menu count')
    # CROP_COUNT can be 256; all scan indices are words, never wrapping bytes.
    # r0 is current public, r3 points at the compiled order. r4 remains the
    # original selector object. Join its existing setter at 0x5325AE6C.
    def cycle(at):
        words = [0xE3A02000, 0xE7D31002, 0xE1510000, 0x0A000000,
                 0xE2822001, _mov(12, count), 0xE152000C, 0x3A000000,
                 0xE3A02000, 0xEA000000, 0xE2822001, 0xE152000C,
                 0x03A02000, 0xE7D31002, _branch(at + 56, 0x5325AE6C)]
        words[3] = _branch(at + 12, at + 40, condition=0)
        words[7] = _branch(at + 28, at + 4, condition=3)
        words[9] = _branch(at + 36, at + 52)
        return words
    cycle_at = patch.append(cycle, 16)
    patch.set_word(0x5325AE34, _branch(0x5325AE34, cycle_at), 'dynamic crop toggle with missing-id fallback')

    # Move complete language rows, retaining every original translation and
    # any End Screen entries already installed by this build.
    bound_sites = getattr(patch, 'text_bound_sites', (0x543D2BA4, 0x543D2C04))
    prior_columns = max(896, *[_half(patch.word(site)) + 1
                              for site in bound_sites])
    upper = max([839, *[text_id(row['identity']['public_id']) for row in records]])
    columns = max(prior_columns, upper + 1)
    catalogs = []
    for language in range(21):
        old = patch.word(TEXT_CATALOG + language * 4)
        row = bytearray(_read(patch.image, old, prior_columns * 4))
        row.extend(bytes((columns - prior_columns) * 4))
        catalogs.append(row)
    for row in records:
        if row['kind'] != 'custom':
            continue
        encoded = (row['name'] + '\0').encode('utf-16-le')
        if not row['name'].strip() or not 1 < len(encoded) // 2 <= 81:
            raise ValueError('裁切名称必须为 1–80 个 UTF-16 字符')
        address = patch.append(encoded, 4)
        record = patch.append(bytes((len(encoded) // 2, 1, 0, 0)) + struct.pack('<I', address), 4)
        for catalog in catalogs:
            struct.pack_into('<I', catalog, text_id(row['identity']['public_id']) * 4, record)
    for language, row in enumerate(catalogs):
        patch.set_word(TEXT_CATALOG + language * 4, patch.append(row, 16), 'crop-aware complete language directory')
    # Preserve the upper bound of any earlier End Screen catalog expansion.
    for site in bound_sites:
        patch.set_word(site, _mov(3, max(upper, _half(patch.word(site)))), 'shared crop/End Screen text upper bound')

    mapping = {row['identity']['public_id']: text_id(row['identity']['public_id']) for row in records}
    text_table = patch.append(struct.pack('<256I', *[mapping.get(n, 0) for n in range(256)]), 16)
    def text(at):
        return [_mov(3, text_table & 65535), _mov(3, text_table >> 16, high=True),
                0xE35100FF, 0x83A00000, 0x812FFF1E, 0xE7930101, 0xE12FFF1E]
    text_at = patch.append(text, 16)
    patch.set_word(0x5337FBAC, _branch(0x5337FBAC, text_at), 'public crop to original or archived text')
    if icon_ids is None:
        # Standalone menu probes have no ICONBIN assets. Keep their original
        # nearest-outline aliases; the containing compiler supplies real IDs.
        icons = [255] * 256
        for row in records:
            public = row['identity']['public_id']
            if public < 4:
                icons[public] = public
            else:
                ratio = parse_ratio(row['requested_ratio'])
                choices = {0: parse_ratio('3:2'), 1: parse_ratio('4:3'),
                           2: parse_ratio('16:9'), 3: parse_ratio('1:1')}
                icons[public] = min(choices, key=lambda key: abs(choices[key] - ratio))
        icon_table = patch.append(bytes(icons), 16)
        def icon(at):
            return [_mov(3, icon_table & 65535), _mov(3, icon_table >> 16, high=True),
                    0xE35100FF, 0x83A00000, 0x812FFF1E, 0xE7D31001, 0xE3510000,
                    _branch(at + 28, 0x53381440)]
    else:
        custom_public = {row['identity']['public_id'] for row in records if row['kind'] == 'custom'}
        if (set(icon_ids) != custom_public or
                any(type(value) is not int or not 0 < value <= 65535 for value in icon_ids.values())):
            raise ValueError('专用裁切图标清单与已有裁切身份不一致')
        icon_table = patch.append(struct.pack('<256I', *[icon_ids.get(n, 0) for n in range(256)]), 16)
        def icon(at):
            # Factory IDs join the untouched original body with its first CMP
            # restored. Dynamic IDs keep native blank variants and bounds.
            return [0xE3510004, _branch(at + 4, at + 48, condition=3),
                    0xE35100FF, 0x83A00000, 0x812FFF1E,
                    0xE3520000, 0x13A00000, 0x112FFF1E,
                    _mov(3, icon_table & 65535), _mov(3, icon_table >> 16, high=True),
                    0xE7930101, 0xE12FFF1E, 0xE3510000,
                    _branch(at + 52, 0x53381440)]
    icon_at = patch.append(icon, 16)
    patch.set_word(0x5338143C, _branch(0x5338143C, icon_at),
                   'dynamic crop outline aliases' if icon_ids is None else 'dedicated dynamic crop outline resources')
    bitmap = patch.append(bytes(int(n in plan['order']) for n in range(256)), 16)
    def valid(at):
        return [_mov(3, bitmap & 65535), _mov(3, bitmap >> 16, high=True),
                0xE35100FF, 0x83A00000, 0x812FFF1E, 0xE7D30001, 0xE12FFF1E]
    valid_at = patch.append(valid, 16)
    patch.set_word(0x5374ACFC, _branch(0x5374ACFC, valid_at), 'validate only active crop settings identities')
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('裁切追加区域碰到已知运行数据映射，不能编译')
    return {'order_address': order, 'order': plan['order'], 'count': count,
            'text_table': text_table, 'text_columns': columns,
            'icon_alias_table': icon_table if icon_ids is None else None,
            'icon_id_table': icon_table if icon_ids is not None else None,
            'active_bitmap': bitmap, 'validator': valid_at, 'cycle': cycle_at,
            'text_mapper': text_at, 'icon_mapper': icon_at,
            'all_21_language_catalogs_preserved': True,
            'original_selector_setter_and_notifications_preserved': True,
            'geometry_consumers_installed': False, 'hardware': 'not_tested'}
