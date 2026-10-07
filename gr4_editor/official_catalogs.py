# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Expand the verified official input's resource directories for editor use.

All original pointers come from the user's input RTOS. New slots are empty;
this module provides no firmware resources, custom labels, icons or colors.
Call before appending code that would occupy the fixed catalog region.
"""
import hashlib
import struct

from .firmware import _Memory


ICON_COUNT = 663
ICON_CAPACITY = 1024
TEXT_COUNT = 837
TEXT_CAPACITY = 896
LANGUAGES = 21
ROW_STRIDE = TEXT_CAPACITY * 4


def initialize_catalogs(patch):
    """Copy native catalogs, reserve empty slots, and redirect both lookups."""
    from .demo_build import BASE, ICON_CATALOG, TEXT_CATALOG, _branch, _mov

    memory = _Memory(bytes(patch.image))
    icons = memory.read(0x55013410, ICON_COUNT * 4)
    text_roots = struct.unpack('<21I', memory.read(0x55002118, LANGUAGES * 4))
    text_rows = [memory.read(address, TEXT_COUNT * 4) for address in text_roots]
    rows_start = TEXT_CATALOG + 0x100
    end = rows_start + LANGUAGES * ROW_STRIDE
    if BASE + len(patch.image) > ICON_CATALOG:
        raise ValueError('原厂目录初始化必须先于固定目录区域内的其他追加代码')
    patch.image.extend(bytes(end - BASE - len(patch.image)))
    patch.image[ICON_CATALOG - BASE:ICON_CATALOG - BASE + len(icons)] = icons
    relocated_rows = [rows_start + language * ROW_STRIDE for language in range(LANGUAGES)]
    catalog = struct.pack('<21I', *relocated_rows)
    patch.image[TEXT_CATALOG - BASE:TEXT_CATALOG - BASE + len(catalog)] = catalog
    for address, contents in zip(relocated_rows, text_rows):
        patch.image[address - BASE:address - BASE + len(contents)] = contents

    # Both icon consumers use the same extended directory. Keep the original
    # valid range until the crop or End Screen compiler extends its bound.
    for site, register, high in ((0x533EF604, 0, False), (0x533EF608, 0, True)):
        value = ICON_CATALOG >> 16 if high else ICON_CATALOG & 65535
        patch.set_word(site, _mov(register, value, high=high), 'use input-derived extended icon catalog')
    for site, register in ((0x5323DBB4, 3), (0x5323E40C, 2)):
        patch.set_word(site, _mov(register, ICON_COUNT - 1), 'native icon upper bound')

    # Drawing and measurement must share one relocated language directory.
    for low, high in ((0x53573500, 0x53573504), (0x53572AEC, 0x53572AF4)):
        patch.set_word(low, _mov(3, TEXT_CATALOG & 65535), 'extended language catalog low address')
        patch.set_word(high, _mov(3, TEXT_CATALOG >> 16, high=True), 'extended language catalog high address')

    bound_sites = []
    for site, conditional in ((0x535734F4, False), (0x53572AE0, True)):
        # Preserve R3. The measurement CMP runs under LS, retaining the
        # preceding language check's flags when the language is out of range.
        compare = 0x91570003 if conditional else 0xE1570003
        helper = patch.append(lambda address, site=site, compare=compare: [
            0xE52D3004, _mov(3, TEXT_COUNT - 1), compare, 0xE49D3004,
            _branch(address + 16, site + 4),
        ], 16)
        patch.set_word(site, _branch(site, helper), 'expand native text bound through preserved-register helper')
        bound_sites.append(helper + 4)
    patch.text_bound_sites = tuple(bound_sites)

    return {
        'source': 'uploaded_official_rtos',
        'icon_catalog': hex(ICON_CATALOG), 'icon_capacity': ICON_CAPACITY,
        'native_icon_entries': ICON_COUNT,
        'native_icon_directory_sha256': hashlib.sha256(icons).hexdigest(),
        'text_catalog': hex(TEXT_CATALOG), 'text_capacity': TEXT_CAPACITY,
        'languages': LANGUAGES, 'native_text_entries_per_language': TEXT_COUNT,
        'language_rows': [hex(address) for address in relocated_rows],
        'native_text_directory_sha256': [hashlib.sha256(row).hexdigest() for row in text_rows],
        'text_bound_sites': [hex(address) for address in bound_sites],
        'custom_slots_initially_empty': True,
    }
