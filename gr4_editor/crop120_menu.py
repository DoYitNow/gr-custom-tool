"""1.20 crop menu/order/resources, with all .97 archived IDs retained."""
from __future__ import annotations

from hashlib import sha256
import struct

from .crop_icons import render_ratio
from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, _branch, _mov

ORDER_PAIRS = ((0x531BC4C0, 0x531BC4C4), (0x531BC7A4, 0x531BC7A8),
               (0x531BF854, 0x531BF85C), (0x531D254C, 0x531D2554),
               (0x531D4894, 0x531D4898), (0x531D620C, 0x531D6210),
               (0x5325CA8C, 0x5325CA90))
COUNT_SITES = (0x531BB684, 0x531D0A74)
TEXT_MAPPER, ICON_MAPPER, VALIDATOR = 0x5338F198, 0x53390A88, 0x5376FB10
CYCLE, CYCLE_JOIN = 0x5325CA94, 0x5325CACC
FACTORY_TEXT = {0: 281, 1: 282, 2: 280, 3: 284}
FACTORY_ICON = {0: 255, 1: 370, 2: 368, 3: 254}


def _half(word):
    return (word & 0xFFF) | ((word >> 4) & 0xF000)


def install_crop_menu120(patch, plan, catalogs, icon_bytes, *, baseline_rtos,
                         text_first=890, icon_first=718, source_icon_bytes=None,
                         source_icon_report=None):
    """Use expanded 1.20 catalogs; preserve original factory translations."""
    baseline = bytes(baseline_rtos)
    if sha256(baseline).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('Crop menu requires the exact official 1.20 base')
    order = list(plan['order'])
    if order[:4] != [0, 1, 3, 2] or len(set(order)) != len(order) or not 4 <= len(order) <= 255:
        raise ValueError('Invalid crop public-ID order')
    records = sorted((row for row in plan['records'] if row['identity']['public_id'] >= 4),
                     key=lambda row: row['identity']['public_id'])
    if set(order[4:]) != {row['identity']['public_id'] for row in records if row.get('active', True)}:
        raise ValueError('Active crop menu differs from archived records')
    for low, high in ORDER_PAIRS:
        words = patch.word(low), patch.word(high)
        if ((words[0] & 0xFFF0F000) != 0xE3003000 or
                (words[1] & 0xFFF0F000) != 0xE3403000 or
                (_half(words[0]) | (_half(words[1]) << 16)) != 0x53DE5890):
            raise ValueError(f'1.20 crop order contract differs at {low:#x}')
    for site in (*COUNT_SITES, TEXT_MAPPER, ICON_MAPPER, VALIDATOR, CYCLE):
        if patch.word(site) != struct.unpack_from('<I', baseline, site - BASE)[0]:
            raise ValueError(f'1.20 crop menu hook already changed: {site:#x}')
    by_source = {row['public_id']: row for row in (source_icon_report or {}).get('custom', [])}
    text_ids, icon_ids = dict(FACTORY_TEXT), dict(FACTORY_ICON)
    icons = bytearray(icon_bytes)
    resources = []
    for index, row in enumerate(records):
        public = row['identity']['public_id']
        text_id, icon_id = text_first + index, icon_first + index
        catalogs.add_text(text_id, row['name'])
        if source_icon_bytes is not None:
            source = by_source[public]
            offset = source['offset']
            pixels = source_icon_bytes[offset:offset + 60 * 40 * 4]
            if sha256(pixels).hexdigest() != source['rgba_sha256']:
                raise ValueError('Actual .97 crop icon differs from its recorded pixels')
        else:
            pixels = render_ratio(row['requested_ratio'])[0].tobytes()
        resource = catalogs.add_icon(icon_id, pixels, icons, width=60, height=40)
        text_ids[public], icon_ids[public] = text_id, icon_id
        resources.append({'public_id': public, 'text_id': text_id, 'icon': resource,
                          'active': row.get('active', True)})
    order_at = patch.append(bytes(order), 16)
    for low, high in ORDER_PAIRS:
        patch.set_word(low, _mov(3, order_at & 65535), '1.20 crop order low')
        patch.set_word(high, _mov(3, order_at >> 16, high=True), '1.20 crop order high')
    for site in COUNT_SITES:
        patch.set_word(site, _mov(0, len(order)), '1.20 active crop count')
    bitmap = patch.append(bytes(int(public in order) for public in range(256)), 16)
    text_at = patch.append(struct.pack('<256I', *[text_ids.get(n, 0) for n in range(256)]), 16)
    icon_at = patch.append(struct.pack('<256I', *[icon_ids.get(n, 0) for n in range(256)]), 16)
    def mapper(table, blank=False):
        result = [_mov(3, table & 65535), _mov(3, table >> 16, high=True)]
        if blank:
            result += [0xE3520000, 0x13A00000, 0x112FFF1E]
        result += [0xE35100FF, 0x83A00000, 0x812FFF1E, 0xE7930101, 0xE12FFF1E]
        return result
    text_entry = patch.append(mapper(text_at), 16)
    icon_entry = patch.append(mapper(icon_at, True), 16)
    valid_entry = patch.append([_mov(3, bitmap & 65535), _mov(3, bitmap >> 16, high=True),
                                0xE35100FF, 0x83A00000, 0x812FFF1E, 0xE7D30001, 0xE12FFF1E], 16)
    for site, target in ((TEXT_MAPPER, text_entry), (ICON_MAPPER, icon_entry), (VALIDATOR, valid_entry)):
        patch.set_word(site, _branch(site, target), '1.20 active/archive crop mapping')
    def cycle(at):
        words = [0xE3A02000, 0xE7D31002, 0xE1510000, 0,
                 0xE2822001, _mov(12, len(order)), 0xE152000C, 0,
                 0xE3A02000, 0, 0xE2822001, 0xE152000C,
                 0x03A02000, 0xE7D31002, _branch(at + 56, CYCLE_JOIN)]
        words[3] = _branch(at + 12, at + 40, condition=0)
        words[7] = _branch(at + 28, at + 4, condition=3)
        words[9] = _branch(at + 36, at + 52)
        return words
    cycle_at = patch.append(cycle, 16)
    patch.set_word(CYCLE, _branch(CYCLE, cycle_at), '1.20 crop quick toggle follows active order')
    return bytes(icons), {'active_order': order, 'order_address': hex(order_at),
                           'active_bitmap': hex(bitmap), 'resources': resources,
                           'text_mapper': hex(text_entry), 'icon_mapper': hex(icon_entry),
                           'text_table': hex(text_at), 'icon_table': hex(icon_at),
                           'validator': hex(valid_entry), 'cycle': hex(cycle_at),
                           'native_factory_text_ids': FACTORY_TEXT,
                           'native_factory_icon_ids': FACTORY_ICON,
                           'source_icons_exact': source_icon_bytes is not None,
                           'readback': 'pending', 'hardware': 'not_tested'}
