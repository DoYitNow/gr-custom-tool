"""Allocate 1.20 catalogs after its complete official body and retain native IDs.

The original initialized language-root array remains the shared runtime entry;
only its copied pointers change. Native rows and descriptors stay intact.
"""
from copy import deepcopy
from hashlib import sha256
import struct

from .firmware import BASE, _Memory
from .slots import _branch, _mov


ICON_COUNT = 709
TEXT_COUNT = 881
LANGUAGES = 21
NATIVE_COLUMNS = 38
TABLE_SITES = (
    ('color_matrix', 0x5394DFB0, 0x5394DFB4, 0x5394DFBC),
    ('multi_axial', 0x5394DFDC, 0x5394DFE0, 0x5394DFE8),
    ('member_16c', 0x5394E008, 0x5394E00C, 0x5394E014),
    ('member_188', 0x5394E034, 0x5394E038, 0x5394E040),
)
OLD_TABLE_SITES = {
    'color_matrix': (0x53925F9C, 0x53925FA4),
    'multi_axial': (0x53925FC8, 0x53925FD0),
    'member_16c': (0x53925FF4, 0x53925FFC),
    'member_188': (0x53926020, 0x53926028),
}


def _set_checked(patch, address, expected, value, reason):
    if patch.word(address) != expected:
        raise ValueError(f'1.20 catalog contract changed: {address:#x}')
    patch.set_word(address, value, reason)


class Catalog120:
    def __init__(self, patch, *, icon_capacity=1024, text_capacity=1408):
        self.patch = patch
        self.memory = _Memory(bytes(patch.image))
        if self.memory.layout['family'] != (1, 20, 10):
            raise ValueError('1.20 catalogs require the 1.20 native layout')
        if not ICON_COUNT <= icon_capacity <= 4096 or not TEXT_COUNT <= text_capacity <= 4096:
            raise ValueError('catalog capacity outside the supported range')
        if BASE + len(patch.image) < 0x5440B600:
            raise ValueError('complete official 1.20 body is required')
        self.icon_capacity, self.text_capacity = icon_capacity, text_capacity
        original_icons = self.memory.read(0x55014280, ICON_COUNT * 4)
        original_roots = struct.unpack('<21I', self.memory.read(0x55002118, 84))
        original_rows = [self.memory.read(root, TEXT_COUNT * 4) for root in original_roots]
        self.icons = patch.append(original_icons + bytes((icon_capacity - ICON_COUNT) * 4), 16)
        self.rows = [patch.append(row + bytes((text_capacity - TEXT_COUNT) * 4), 16) for row in original_rows]
        self.text_root = self.memory.source + 0x2118
        for index, row in enumerate(self.rows):
            patch.set_word(self.text_root + index * 4, row, '1.20 copied language root uses expanded native-preserving row')
        _set_checked(patch, 0x534013F8, 0xE3040280, _mov(0, self.icons & 65535), 'expanded 1.20 icon catalog low')
        _set_checked(patch, 0x534013FC, 0xE3450501, _mov(0, self.icons >> 16, high=True), 'expanded 1.20 icon catalog high')
        self.bound_helpers = []
        for address, register, condition, expected, maximum in (
            (0x5323F7EC, 4, 14, 0xE3540FB1, icon_capacity - 1),
            (0x53240064, 7, 14, 0xE3570FB1, icon_capacity - 1),
            (0x535967C0, 7, 14, 0xE3570E37, text_capacity - 1),
            (0x53595DA8, 7, 9, 0x93570E37, text_capacity - 1),
        ):
            helper = patch.append(lambda at, address=address, register=register, condition=condition, maximum=maximum: [
                0xE52D3004, _mov(3, maximum),
                condition << 28 | 0x01500003 | register << 16,
                0xE49D3004, _branch(at + 16, address + 4),
            ], 16)
            _set_checked(patch, address, expected, _branch(address, helper), 'expanded 1.20 resource bound with preserved scratch/conditional flags')
            self.bound_helpers.append({'site': hex(address), 'helper': hex(helper), 'maximum': maximum})
        self.report = {
            'native_icons': ICON_COUNT, 'native_texts': TEXT_COUNT, 'languages': LANGUAGES,
            'icon_catalog': hex(self.icons), 'icon_capacity': icon_capacity,
            'language_root_rom': hex(self.text_root), 'text_capacity': text_capacity,
            'language_rows': list(map(hex, self.rows)), 'bounds': self.bound_helpers,
            'native_icon_directory_sha256': sha256(original_icons).hexdigest(),
            'native_text_directory_sha256': [sha256(row).hexdigest() for row in original_rows],
            'hardware': 'not_tested',
        }

    def add_text(self, identity, text, *, translations=None):
        if not TEXT_COUNT <= identity < self.text_capacity:
            raise ValueError('custom text must follow all 1.20 native text IDs')
        for language, table in enumerate(self.rows):
            if self.patch.word(table + identity * 4):
                raise ValueError('custom text ID is already occupied')
            value = (translations or {}).get(language, text)
            encoded = (value + '\0').encode('utf-16-le')
            units = len(encoded) // 2
            if not 1 < units <= 255:
                raise ValueError('native text does not fit its byte-sized length')
            payload = self.patch.append(encoded, 4)
            descriptor = self.patch.append(bytes((units, 1, 0, 0)) + struct.pack('<I', payload), 4)
            self.patch.set_word(table + identity * 4, descriptor, 'register 1.20 custom text after native IDs')

    def add_icon(self, identity, pixels, icon_bytes, *, width=40, height=40):
        if not ICON_COUNT <= identity < self.icon_capacity or self.patch.word(self.icons + identity * 4):
            raise ValueError('custom icon ID is invalid or occupied')
        if len(pixels) != width * height * 4:
            raise ValueError('RGBA payload differs from native icon dimensions')
        icon_bytes.extend(bytes((-len(icon_bytes)) % 4))
        offset = len(icon_bytes)
        icon_bytes.extend(pixels)
        descriptor = self.patch.append(struct.pack('<HHHHI', 1, width, height, 0, offset // 4), 16)
        self.patch.set_word(self.icons + identity * 4, descriptor, 'register 1.20 custom RGBA icon')
        return {'id': identity, 'descriptor': hex(descriptor), 'offset': offset,
                'sha256': sha256(pixels).hexdigest()}


def plan_filter_resources120(filters):
    """Keep photo/style identities while moving internal profile/text/icon IDs."""
    custom = sorted((deepcopy(row) for row in filters if row.get('kind') == 'custom' and row.get('enabled', True)),
                    key=lambda row: row['identity']['style_id'])
    for index, row in enumerate(custom):
        row['identity'].update(profile_id=NATIVE_COLUMNS + index,
                               text_id=TEXT_COUNT + index, icon_id=ICON_COUNT + index)
    return custom


def install_filter_resources120(patch, catalogs, icon_bytes, filters, *, reference_rtos):
    """Reuse exact .97 color payloads; retain all 38 native profiles per bank."""
    memory = _Memory(bytes(patch.image))
    if sha256(reference_rtos).hexdigest() != 'a9a9a51530c35819df47dee1cce9f4f560a17eeed1da258950167d282884fce1':
        raise ValueError('descriptor migration requires the exact official 1.11 reference')
    old_memory = _Memory(reference_rtos)
    custom = plan_filter_resources120(filters)
    columns = NATIVE_COLUMNS + len(custom)
    if columns > 255:
        raise ValueError('native profile stride is byte-sized')
    families, descriptor_templates = {}, {}
    for family, stride, low, high in TABLE_SITES:
        if patch.word(stride) != 0xE3A02026:
            raise ValueError('1.20 native color table does not have 38 columns')
        pointer = memory.pair(low, high)
        original = list(struct.unpack('<114I', memory.read(pointer, 456)))
        old_pointer = old_memory.pair(*OLD_TABLE_SITES[family])
        old_original = struct.unpack('<102I', old_memory.read(old_pointer, 408))
        descriptor_templates[family] = [
            (old_memory.read(old_original[bank * 34 + 11], 128),
             memory.read(original[bank * 38 + 11], 128)) for bank in range(3)]
        families[family] = [original[bank * 38:(bank + 1) * 38] + [original[bank * 38 + 11]] * len(custom)
                            for bank in range(3)]
    icons, payloads = bytearray(icon_bytes), []

    def raw(value, length):
        if isinstance(value, dict):
            value = value['hex']
        blob = bytes.fromhex(value)
        if len(blob) != length:
            raise ValueError('custom filter resource payload has an unexpected length')
        return blob

    for row in custom:
        identity = row['identity']
        banks = sorted(row['resources']['banks'], key=lambda bank: bank['hardware_index'])
        if [bank['hardware_index'] for bank in banks] != [0, 1, 2]:
            raise ValueError('three exact hardware banks are required')
        for bank in banks:
            descriptors = {name: bytearray(raw(bank['descriptor_hex'][name], 128)) for name, *_ in TABLE_SITES}
            translations = []
            for family, descriptor in descriptors.items():
                old_template, new_template = descriptor_templates[family][bank['hardware_index']]
                payload_offsets = {'color_matrix': (0, 4, 84), 'multi_axial': (8, 12),
                                   'member_16c': (), 'member_188': (8, 12, 16)}[family]
                for offset in range(0, 128, 4):
                    value = struct.unpack_from('<I', descriptor, offset)[0]
                    old_value = struct.unpack_from('<I', old_template, offset)[0]
                    new_value = struct.unpack_from('<I', new_template, offset)[0]
                    if offset in payload_offsets:
                        continue
                    if 0x53000000 <= value < 0x56000000:
                        if value != old_value or not 0x53000000 <= new_value < 0x56000000:
                            raise ValueError(f'unknown custom descriptor pointer: {family}+{offset:#x}')
                        struct.pack_into('<I', descriptor, offset, new_value)
                        translations.append({'family': family, 'offset': offset,
                                             'old': hex(value), 'new': hex(new_value)})
                    elif value == old_value:
                        # Unmodified factory fields follow the official new template.
                        struct.pack_into('<I', descriptor, offset, new_value)
            addresses = {}
            hashes = {}
            for key, size in (('camera_basis', 18), ('matrix', 18), ('multi_main', 2160), ('multi_aux', 1440)):
                blob = raw(bank[key], size)
                addresses[key] = patch.append(blob, 16)
                hashes[key] = sha256(blob).hexdigest()
            for name, target in (('multi_main_wrapper', 'multi_main'), ('multi_aux_wrapper', 'multi_aux')):
                blob = bytearray(raw(bank[name], 20))
                struct.pack_into('<I', blob, 16, addresses[target])
                addresses[name] = patch.append(blob, 16)
            basis = struct.unpack_from('<I', descriptors['color_matrix'])[0]
            for offset in range(0, 128, 4):
                if struct.unpack_from('<I', descriptors['color_matrix'], offset)[0] == basis:
                    struct.pack_into('<I', descriptors['color_matrix'], offset, addresses['camera_basis'])
            struct.pack_into('<I', descriptors['color_matrix'], 4, addresses['matrix'])
            struct.pack_into('<II', descriptors['multi_axial'], 8, addresses['multi_main_wrapper'], addresses['multi_aux_wrapper'])
            gamma = raw(bank['gamma'], 1536)
            hashes['gamma'] = sha256(gamma).hexdigest()
            for channel in range(3):
                pointer = patch.append(gamma[channel * 512:(channel + 1) * 512], 16)
                struct.pack_into('<I', descriptors['member_188'], 8 + channel * 4, pointer)
            for family, descriptor in descriptors.items():
                pointer = patch.append(descriptor, 16)
                families[family][bank['hardware_index']][identity['profile_id']] = pointer
            payloads.append({'style': identity['style_id'], 'bank': bank['hardware_index'],
                             'sha256': hashes, 'descriptor_pointer_translations': translations})
        catalogs.add_text(identity['text_id'], row['name'])
        pixels = raw(row.get('icon_rgba', row['resources']['icon']), 6400)
        catalogs.add_icon(identity['icon_id'], pixels, icons)
    table_report = {}
    for family, stride, low, high in TABLE_SITES:
        pointers = [pointer for bank in families[family] for pointer in bank]
        pointer = patch.append(struct.pack('<%dI' % len(pointers), *pointers), 16)
        patch.set_word(stride, 0xE3A02000 | columns, '1.20 native plus custom color profile stride')
        patch.set_word(low, _mov(3, pointer & 65535), '1.20 color profile directory low')
        patch.set_word(high, _mov(3, pointer >> 16, high=True), '1.20 color profile directory high')
        table_report[family] = {'address': hex(pointer), 'columns': columns, 'native_profiles_per_bank': 38}
    return bytes(icons), custom, {'catalogs': catalogs.report, 'tables': table_report,
                                  'payloads': payloads, 'hardware': 'not_tested'}
