# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Read native filter resources without replacing the public firmware adapter.

This compatibility reader is intentionally separate from ``firmware.py``.  It
uses the public container decoder and memory model, while the native filter
schema and bounded ARM resource reader come from the research implementation.
It never makes an unknown input writable; the caller must gate compilation via
``slots.supports_image``.
"""
from __future__ import annotations

import struct
from typing import Any

from .firmware import FirmwareError, _Memory, _sha, _half
TABLES = (
    ('color_matrix', 0x53925F98, 0x53925F9C, 0x53925FA4),
    ('multi_axial', 0x53925FC4, 0x53925FC8, 0x53925FD0),
    ('member_16c', 0x53925FF0, 0x53925FF4, 0x53925FFC),
    ('member_188', 0x5392601C, 0x53926020, 0x53926028),
)
PARAMETERS = (
    ('Saturation', '饱和度'), ('ColorHue', '色相'), ('ImageKey', '高低调'),
    ('Contrast', '对比度'), ('ContrastHighLight', '高光对比度'),
    ('ContrastShadow', '阴影对比度'), ('Sharpness', '锐度'),
    ('Shading', '暗角'), ('ClarityControl', '清晰度'),
    # Mono grain schema fields; native getter/setter proof is unavailable.
    ('Particle', '颗粒'), ('ParticleSize', '颗粒大小'), ('ParticleStrength', '颗粒强度'),
)
# The decompressed Image Control schema contains more fields than the nine
# scalar defaults used by the custom-slot compiler.  Keep those fields in the
# read model even when the current backend cannot write them.  This gives the
# editor an honest per-field capability view instead of silently dropping a
# native field (for example a Mono tone or filter-effect selector).
EXTENDED_PARAMETERS = (
    ('ColorToneMonotone', '黑白色调'),
    ('ColorToneCinema', 'Cinema 色调'),
    ('ColorToneBleechBypass', 'Bleach Bypass 色调'),
    ('ColorToneRetro', 'Retro 色调'),
    ('ColorToneHdrIntense', 'HDR 色调'),
    ('FilterEffectCustom', '滤镜效果'),
    ('FilterEffectR', '滤镜效果 R'),
    ('FilterEffectG', '滤镜效果 G'),
    ('FilterEffectB', '滤镜效果 B'),
    ('HdrIntense', 'HDR 强度'),
    ('ColorToneCrossProcess', 'Cross Process 色调'),
)
PARTICLE_PARAMETER_IDS = frozenset(('Particle', 'ParticleSize', 'ParticleStrength'))
PARTICLE_PARAMETER_TAGS = {'Particle': 21, 'ParticleSize': 22, 'ParticleStrength': 23}
PARTICLE_PARAMETER_RANGES = {'Particle': (0, 1), 'ParticleSize': (0, 2), 'ParticleStrength': (0, 2)}
PARTICLE_NATIVE_STYLES = frozenset((13, 14, 15, 16, 17, 18, 19, 24, 25, 26, 27, 28, 29))
# The firmware has a static factory catalogue for styles 11..30.  The menu
# vector can still omit a factory row (and the native color admission gate can
# reject the Mono rows), so the editor keeps these rows in its inventory and
# reports their effective enabled state separately from their static identity.
NATIVE_FACTORY_STYLE_IDS = tuple(range(11, 31))
# Tag numbers are the stable positions in the 56-byte Image Control recipe.
# The four RGB channels have high/low halves in the original record.  The
# editor exposes the family as metadata only; no value writer is implied.
PARAMETER_SCHEMA_TAGS = {
    'Saturation': 0, 'ColorHue': 1, 'ImageKey': 2, 'Contrast': 3,
    'ContrastHighLight': 4, 'ContrastShadow': 5, 'Sharpness': 6,
    'ColorToneMonotone': 7, 'ColorToneCinema': 8,
    'ColorToneBleechBypass': 9, 'ColorToneRetro': 10,
    'ColorToneHdrIntense': 11, 'FilterEffectCustom': 12,
    'FilterEffectR': 13, 'FilterEffectG': 15, 'FilterEffectB': 17,
    'Shading': 19, 'ClarityControl': 20, 'Particle': 21,
    'ParticleSize': 22, 'ParticleStrength': 23, 'HdrIntense': 24,
    'ColorToneCrossProcess': 25,
}
# Exact field presence for the official 11..24 style family.  This is derived
# from data/firmware/image_control_schema.json, not from a guessed default.
# Styles 25..30 are a separate Mono family; only their particle coverage is
# currently independently established (PARTICLE_NATIVE_STYLES above).
NATIVE_PARAMETER_FIELDS = {
    11: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl')),
    12: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl')),
    13: frozenset(('ImageKey', 'Contrast', 'ContrastHighLight', 'ContrastShadow',
                   'Sharpness', 'Shading', 'ClarityControl', 'ColorToneMonotone',
                   'FilterEffectCustom')),
    14: frozenset(('ImageKey', 'Contrast', 'ContrastHighLight', 'ContrastShadow',
                   'Sharpness', 'Shading', 'ColorToneMonotone', 'FilterEffectCustom')),
    15: frozenset(('ImageKey', 'Contrast', 'ContrastHighLight', 'ContrastShadow',
                   'Sharpness', 'Shading', 'ClarityControl', 'ColorToneMonotone')),
    16: frozenset(('ImageKey', 'Contrast', 'ContrastHighLight', 'ContrastShadow',
                   'Sharpness', 'Shading', 'ClarityControl', 'ColorToneMonotone',
                   'FilterEffectCustom')),
    17: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl')),
    18: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl',
                   'ColorToneCinema')),
    19: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl',
                   'ColorToneCinema')),
    20: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl',
                   'ColorToneBleechBypass')),
    21: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl',
                   'ColorToneRetro')),
    22: frozenset(('Saturation', 'ColorHue', 'ColorToneHdrIntense', 'HdrIntense')),
    23: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl',
                   'ColorToneCrossProcess')),
    24: frozenset(('Saturation', 'ColorHue', 'ImageKey', 'Contrast', 'ContrastHighLight',
                   'ContrastShadow', 'Sharpness', 'Shading', 'ClarityControl')),
}
DEFAULT_ENTRIES = (
    0x533BF710, 0x533C01BC, 0x533C0C68, 0x533C1C14, 0x533C2BC0,
    0x533C3B6C, 0x533C4B18, 0x533C93F0, 0x533CA39C,
)

def _immediate(word: int) -> int:
    value, rotate = word & 255, ((word >> 8) & 15) * 2
    return ((value >> rotate) | (value << (32 - rotate))) & 0xFFFFFFFF if rotate else value


def _native_lookup(memory: _Memory, entry: int, style: int, stop: int | None = None,
                   default_object: bool = False, default_mode: int = 0) -> int:
    """Interpret only the bounded native ID lookup's arithmetic/branch subset.

    Used for actual text/icon and profile mappings. Unsupported instructions
    raise instead of inventing a resource ID from a hard-coded style name.
    """
    regs = [0] * 16
    regs[1], regs[2] = style, style if stop else 0
    if default_object:
        regs[0] = 0x62010000  # isolated zero-valued mode object, no camera RAM
    pc, flags = entry, (False, False, False, False)
    # Dynamic registries may contain up to a byte-wide set of style IDs; one
    # late row can require far more comparisons than the original two slots.
    for _ in range(4096):
        if pc == stop:
            return regs[3]
        w = memory.word(pc)
        n, z, c, v = flags
        cond = w >> 28
        accepted = (z, not z, c, not c, n, not n, v, not v,
                    c and not z, not c or z, n == v, n != v,
                    not z and n == v, z or n != v, True, False)[cond]
        at, pc = pc, pc + 4
        if not accepted:
            continue
        if w & 0x0FFFFFFF == 0x012FFF1E:
            return regs[0]
        if w & 0x0E000000 == 0x0A000000:
            if w & 0x01000000:
                raise FirmwareError('native ID mapping unexpectedly calls another function')
            displacement = w & 0xFFFFFF
            if displacement & 0x800000:
                displacement -= 0x1000000
            pc = at + 8 + displacement * 4
            continue
        kind = w & 0x0FF00000
        rd, rn = (w >> 12) & 15, (w >> 16) & 15
        if kind == 0x03000000:
            regs[rd] = _half(w)
        elif kind == 0x03400000:
            regs[rd] = (regs[rd] & 65535) | _half(w) << 16
        elif w & 0x0FF00000 == 0x03500000:  # CMP immediate
            a, b = regs[rn], _immediate(w)
            result = (a - b) & 0xFFFFFFFF
            flags = (bool(result >> 31), result == 0, a >= b, bool(((a ^ b) & (a ^ result)) >> 31))
        elif w & 0x0FE00000 == 0x03A00000:
            regs[rd] = _immediate(w)
        elif w & 0x0FE00000 == 0x03800000:
            regs[rd] = regs[rn] | _immediate(w)
        elif w & 0x0FF00FF0 == 0x07D00000:  # LDRB [rn,rm], unshifted
            regs[rd] = memory.read(regs[rn] + regs[w & 15], 1)[0]
        elif w & 0x0FF00000 == 0x05D00000:  # LDRB [rn,#immediate]
            address = regs[rn] + (w & 0xFFF)
            if default_object and 0x62010000 <= address < 0x62010100:
                regs[rd] = default_mode if address == 0x6201001F else 0
            else:
                regs[rd] = memory.read(address, 1)[0]
        else:
            raise FirmwareError(f'unknown native ID mapping instruction {at:#x}: {w:#x}')
    raise FirmwareError('native ID mapping exceeded its instruction bound')


_BASE_PARAMETER_IDS = frozenset(key for key, _ in PARAMETERS[:9])
# The Mono family has the same nine scalar controls in the reviewed style
# table.  Its extra grain coverage remains the separately documented
# PARTICLE_NATIVE_STYLES matrix below.
for _mono_style in (25, 26, 27, 28, 29, 30):
    NATIVE_PARAMETER_FIELDS.setdefault(_mono_style, _BASE_PARAMETER_IDS)


def _parameter_field_info(style: int, key: str) -> dict:
    """Return read-model capability metadata for one parameter field.

    ``native_field_supported`` describes presence in the static Image Control
    schema.  It does not claim that a live camera value can be read from the
    firmware container.  ``parameter_enable_supported`` is deliberately
    narrower: only the nine scalar fields have a verified custom-slot writer.
    This separation lets the UI show every known native switch without making
    an unverified Mono grain setter look writable.
    """
    if style >= 34:
        supported = key in _BASE_PARAMETER_IDS
        source = 'custom-slot nine-parameter backend' if supported else 'not in custom-slot backend'
        enable_supported = supported
    else:
        supported = key in NATIVE_PARAMETER_FIELDS.get(style, ())
        if key in PARTICLE_PARAMETER_IDS:
            supported = style in PARTICLE_NATIVE_STYLES
        source = 'native Image Control schema' if supported else 'native style field matrix'
        enable_supported = False
    info = {
        'native_field_supported': bool(supported),
        # This is the actual schema presence/selection state, not a writable
        # value.  Native rows are read-only; custom rows use the nine verified
        # fields as active by construction.
        'enabled': bool(supported),
        'parameter_enable_supported': bool(enable_supported),
        'enable_source': source,
    }
    tag = PARAMETER_SCHEMA_TAGS.get(key)
    if tag is not None:
        info['native_schema_tag'] = tag
    return info


def _read_parameters(image: FirmwareImage, memory: _Memory, style: int) -> list[dict]:
    lifecycle_editable = False
    if image.editable and style >= 34:
        # This gate only concerns whether the backend can preserve/reset/save
        # edited defaults. Values below always come from actual instructions.
        try:
            from . import slots
            lifecycle_editable = bool(slots.capabilities().get('parameter_defaults_known'))
            lifecycle_editable = lifecycle_editable and (
                _sha(image.rtos) == slots.KNOWN27_SHA or slots._metadata(image.rtos) is not None)
        except (ImportError, AttributeError, ValueError):
            lifecycle_editable = False
    result = []
    all_parameters = PARAMETERS + EXTENDED_PARAMETERS
    for index, (key, name) in enumerate(all_parameters):
        entry = DEFAULT_ENTRIES[index] if index < len(DEFAULT_ENTRIES) else None
        minimum, maximum = PARTICLE_PARAMETER_RANGES.get(key, (0, 8))
        row = dict(id=key, name=name, min=minimum, max=maximum, default=None,
                   current_value=None, editable=False, default_source='unknown',
                   value_source='no camera UserData in firmware container')
        row.update(_parameter_field_info(style, key))
        if key in PARTICLE_PARAMETER_IDS:
            if style in PARTICLE_NATIVE_STYLES:
                row.update(default_source='native UserData schema; getter not verified',
                           value_source='no camera UserData in firmware container',
                           native_schema_tag=PARTICLE_PARAMETER_TAGS[key])
            else:
                row.update(default_source='not present in this native style',
                           value_source='native style field matrix')
        elif key in dict(EXTENDED_PARAMETERS):
            if row['native_field_supported']:
                row.update(default_source='native Image Control schema; getter not verified',
                           value_source='no camera UserData in firmware container')
            else:
                row.update(default_source='not present in this native style',
                           value_source='native style field matrix')
        if style >= 34 and entry is not None:
            try:
                value = _native_lookup(memory, entry, style, default_object=True)
                if not 0 <= value <= 8:
                    raise FirmwareError('default getter returned an unknown UI value')
                row.update(default=value, default_source='native verified getter',
                           editable=lifecycle_editable,
                           default_verification={'entry_address': entry,
                                                 'method': 'bounded ARM interpreter',
                                                 'style_id': style, 'mode_fixture': 0,
                                                 'context': 'isolated zero mode object; not live camera values'})
            except FirmwareError as exc:
                row['default_inspection_note'] = str(exc)
        result.append(row)
    return result


def _blob(memory: _Memory, address: int, size: int) -> dict:
    data = memory.read(address, size)
    return {'address': address, 'bytes': size, 'hex': data.hex(), 'sha256': _sha(data)}


def _color_resources(memory: _Memory, profile: int) -> dict:
    tables = []
    for name, stride, low, high in TABLES:
        instruction = memory.word(stride)
        if instruction & 0xFFFFFF00 != 0xE3A02000:
            raise FirmwareError('unknown native profile table stride')
        columns = instruction & 255
        if not 1 <= columns <= 255 or not 0 <= profile < columns:
            raise FirmwareError('profile index is outside the native table')
        tables.append((name, columns, memory.pair(low, high)))
    banks = []
    for hardware in range(3):
        descriptors, pointers = {}, {}
        for name, columns, table in tables:
            ptr = memory.word(table + (hardware * columns + profile) * 4)
            pointers[name] = ptr
            descriptors[name] = memory.read(ptr, 128).hex()
        matrix = bytes.fromhex(descriptors['color_matrix'])
        multi = bytes.fromhex(descriptors['multi_axial'])
        gamma = bytes.fromhex(descriptors['member_188'])
        main_wrapper, aux_wrapper = struct.unpack_from('<2I', multi, 8)
        channels = [_blob(memory, struct.unpack_from('<I', gamma, 8 + i * 4)[0], 512) for i in range(3)]
        gamma_bytes = b''.join(bytes.fromhex(row['hex']) for row in channels)
        banks.append({
            'hardware_index': hardware, 'descriptor_hex': descriptors,
            'descriptor_addresses': pointers,
            'camera_basis': _blob(memory, struct.unpack_from('<I', matrix)[0], 18),
            'matrix': _blob(memory, struct.unpack_from('<I', matrix, 4)[0], 18),
            'multi_main_wrapper': _blob(memory, main_wrapper, 20),
            'multi_aux_wrapper': _blob(memory, aux_wrapper, 20),
            'multi_main': _blob(memory, memory.word(main_wrapper + 16), 2160),
            'multi_aux': _blob(memory, memory.word(aux_wrapper + 16), 1440),
            'gamma': {'channels': channels, 'bytes': len(gamma_bytes),
                      'hex': gamma_bytes.hex(), 'sha256': _sha(gamma_bytes)},
        })
    return {'banks': banks, 'native_table_columns': [row[1] for row in tables],
            'member_16c_semantics': 'unknown; exact descriptor bytes retained'}


def _name_resource(memory: _Memory, text_id: int) -> tuple[str, dict]:
    catalog = memory.pair(0x53573500, 0x53573504)
    row = memory.word(catalog + 4)  # English language row, not live UI language
    record = memory.word(row + text_id * 4)
    metadata = memory.read(record, 8)
    if metadata[1] != 1:
        raise FirmwareError('text record is not the known Latin UTF-16 format')
    address = struct.unpack_from('<I', metadata, 4)[0]
    raw = bytearray()
    for i in range(128):
        unit = memory.read(address + i * 2, 2)
        raw.extend(unit)
        if unit == b'\0\0':
            return bytes(raw[:-2]).decode('utf-16-le'), {
                'language': 'English', 'record_address': record, 'text_address': address,
                'record_hex': metadata.hex(), 'hex': bytes(raw).hex(),
                'sha256': _sha(bytes(raw)),
            }
    raise FirmwareError('native text has no bounded terminator')


def _icon_resource(memory: _Memory, image: FirmwareImage, icon_id: int) -> dict:
    catalog = memory.pair(0x533EF604, 0x533EF608)
    descriptor = memory.word(catalog + icon_id * 4)
    raw = memory.read(descriptor, 12)
    kind, width, height, flags, words = struct.unpack('<HHHHI', raw)
    if kind != 1 or not width or not height:
        raise FirmwareError('unsupported icon encoding')
    offset, size = words * 4, width * height * 4
    if not 0 <= offset < offset + size <= len(image.icon_bytes):
        raise FirmwareError('native icon is outside ICONBIN')
    pixels = image.icon_bytes[offset:offset + size]
    return {'descriptor_address': descriptor, 'descriptor_hex': raw.hex(),
            'offset': offset, 'bytes': size, 'width': width, 'height': height,
            'flags': flags, 'pixel_format': 'RGBA8', 'hex': pixels.hex(), 'sha256': _sha(pixels)}


def _custom_maker_codes(memory: _Memory) -> dict[int, int]:
    """Read every actual row of the native custom MakerNote writer.

    Rows end at the original writer fallback, rather than a fixed byte window.
    This preserves sparse style IDs when a middle registration is removed.
    """
    def branch(address, condition):
        word = memory.word(address)
        if word & 0xFF000000 != condition << 28 | 0x0A000000:
            return None
        displacement = word & 0xFFFFFF
        if displacement & 0x800000:
            displacement -= 0x1000000
        return address + 8 + displacement * 4

    entry = branch(0x537DE474, 14)
    if entry is None or memory.word(entry) != 0xE5D2379C:
        return {}
    entry += 4
    result = {}
    # Style IDs occupy one byte. Unknown instructions remain unknown, even if
    # a neighbouring function happens to contain a matching CMP/MOV pair.
    for _ in range(256):
        word = memory.word(entry)
        if word == 0xE30F3198 and branch(entry + 4, 14) == 0x537DE478:
            return result
        if word & 0xFFFFFF00 != 0xE3530000:
            return {}
        style = word & 255
        code_word = memory.word(entry + 12)
        if (style < 34 or style in result or branch(entry + 4, 1) != entry + 24 or
                memory.word(entry + 8) != 0xE1A00001 or
                code_word & 0xFFF0F000 != 0xE3002000 or
                memory.word(entry + 16) != 0xE3A01000 or
                branch(entry + 20, 14) != 0x537E7148):
            return {}
        code = _half(code_word)
        if code < 0x8000:
            return {}
        result[style] = code
        entry += 24
    return {}


def read_filters(image: FirmwareImage) -> list[dict]:
    """Read native list, static resources and verified custom initial defaults.

    The first nine definitions describe the established native 0..8 encoded UI
    scale.  Particle fields are carried as schema metadata only until their
    native getter and custom lifecycle routes are independently verified.
    Custom initial/reset defaults come from the native getter with an isolated
    mode-zero object. ``None`` means an unknown getter; current personal values
    always remain unknown. Editing additionally requires the lifecycle backend.
    """
    memory = _Memory(image.rtos)
    registry_metadata = None
    try:
        from . import slots
        registry_metadata = slots._metadata(image.rtos)
    except (ImportError, AttributeError, ValueError, TypeError, struct.error):
        pass
    registered_rows = {}
    # Version-one outputs retain their existing integer reader IDs. Dynamic
    # recipes carry stable IDs independently of sparse native style numbers.
    if (registry_metadata and isinstance(registry_metadata.get('schema_version'), int) and
            registry_metadata['schema_version'] >= 2):
        # ``registry`` historically contained only active custom rows while
        # ``recipe_registry`` also retains rows that were intentionally hidden.
        # Read both so a generated image can round-trip the visible state of
        # every recognized slot.
        for source in ('registry', 'recipe_registry'):
            for row in registry_metadata.get(source, []):
                if isinstance(row, dict) and isinstance(row.get('identity'), dict):
                    registered_rows[row['identity'].get('style_id')] = row
    table = memory.pair(0x533CC78C, 0x533CC794)
    count_word = memory.word(0x533CC7A4)
    if count_word & 0xFFFFFF00 != 0xE2847000:
        raise FirmwareError('unknown native menu count instruction')
    count = count_word & 255
    order = list(memory.read(table, count))
    if not order or len(order) != len(set(order)):
        raise FirmwareError('empty/duplicate native menu order')
    for address, prefix in ((0x533CC8C8, 0xE3A00000), (0x533BEBF0, 0xE3A00000),
                            (0x53375628, 0xE2847000)):
        if memory.word(address) != prefix | count:
            raise FirmwareError('native menu counts disagree')
    filters = []
    registered_codes = _custom_maker_codes(memory)
    # The menu vector is the first source of truth for active rows.  Factory
    # resources remain addressable even when their row is omitted, so append
    # those static identities and registered hidden custom identities below.
    styles = list(order)
    styles.extend(style for style in NATIVE_FACTORY_STYLE_IDS if style not in styles)
    styles.extend(style for style in registered_rows
                  if isinstance(style, int) and style >= 34 and style not in styles)
    mono_styles = frozenset((25, 26, 27, 28, 29, 30))
    try:
        from .slots import NATIVE_MONO_STYLES, inspect_native_mono_admission
        mono_styles = frozenset(NATIVE_MONO_STYLES)
        mono_admission = inspect_native_mono_admission(image.rtos).get('status') == 'passed'
    except (ImportError, AttributeError, TypeError, ValueError, struct.error):
        mono_admission = bool((registry_metadata or {}).get('native_mono', {}).get('unlock_native_mono'))
    visible_ordinal = 0
    for ordinal, style in enumerate(styles, 1):
        in_menu = style in order
        # On the unpatched .70 base the six Mono resources are present in the
        # menu vector, but the color-mode admission routine still rejects
        # them.  Reflect that effective state instead of reporting the raw
        # vector membership as enabled.
        effective_enabled = in_menu and not (style in mono_styles and not mono_admission)
        if style not in NATIVE_FACTORY_STYLE_IDS and style not in order:
            effective_enabled = bool(registered_rows.get(style, {}).get('enabled', False))
        visibility = (registry_metadata or {}).get('native_visibility', {})
        if style in NATIVE_FACTORY_STYLE_IDS and isinstance(visibility, dict):
            recorded_visibility = visibility.get(style, visibility.get(str(style)))
            if isinstance(recorded_visibility, bool):
                effective_enabled = recorded_visibility
        if effective_enabled:
            visible_ordinal += 1
        item: dict[str, Any] = {
            'id': style, 'kind': 'custom' if style >= 34 else 'native',
            'name': f'Style {style}', 'enabled': effective_enabled, 'editable': False,
            'visibility_editable': style in NATIVE_FACTORY_STYLE_IDS,
            'identity': {'style_id': style, 'profile_id': None,
                         'menu_ordinal': visible_ordinal if effective_enabled else None,
                         'text_id': None, 'icon_id': None, 'maker_note_code': None},
            'parameters': _read_parameters(image, memory, style),
            'resources': {}, 'inspection_notes': [],
        }
        try:
            profile = _native_lookup(memory, 0x53912CF4, style, stop=0x53912D08)
            item['identity']['profile_id'] = profile
            item['resources'].update(_color_resources(memory, profile))
        except FirmwareError as exc:
            item['resources']['banks'] = []
            item['inspection_notes'].append(str(exc))
        for kind, entry in (('text_id', 0x5337F824), ('icon_id', 0x53380FAC)):
            try:
                item['identity'][kind] = _native_lookup(memory, entry, style)
            except FirmwareError as exc:
                item['inspection_notes'].append(str(exc))
        try:
            item['name'], item['resources']['name'] = _name_resource(memory, item['identity']['text_id'])
        except (FirmwareError, TypeError) as exc:
            item['resources']['name'] = {'unknown': str(exc)}
        try:
            item['resources']['icon'] = _icon_resource(memory, image, item['identity']['icon_id'])
        except (FirmwareError, TypeError) as exc:
            item['resources']['icon'] = {'unknown': str(exc)}
        # These are actual original rows or bounded writer registrations.
        for original_style, _, code in struct.iter_unpack('<BBH', memory.read(0x53FA1758, 80)):
            if original_style == style:
                item['identity']['maker_note_code'] = code
        if style in registered_codes:
            item['identity']['maker_note_code'] = registered_codes[style]
        registration = registered_rows.get(style)
        registry_consistent = True
        if registration is not None:
            # Footer metadata may supply stable recipe/state namespaces. It
            # cannot replace native menu/profile/text/icon/MakerNote evidence.
            recorded_identity = registration['identity']
            mismatch = [key for key, value in item['identity'].items()
                        if key != 'menu_ordinal' and key in recorded_identity and value is not None
                        and recorded_identity[key] is not None and recorded_identity[key] != value]
            if mismatch or not isinstance(registration.get('id'), (str, int)):
                registry_consistent = False
                item['inspection_notes'].append('editor registry differs from native identity: ' +
                                                ', '.join(mismatch or ['recipe id']))
            else:
                item['id'] = registration['id']
                fields = []
                for key, value in recorded_identity.items():
                    if key not in item['identity'] or item['identity'][key] is None:
                        item['identity'][key] = value
                        fields.append(key)
                item['registry_metadata'] = {
                    'source': 'verified editor registry footer',
                    'schema_version': registry_metadata['schema_version'],
                    'recipe_id': registration['id'], 'identity_fields': fields,
                    'native_identity_compared': True,
                }
            enabled_fields = registration.get('parameter_enabled')
            if isinstance(enabled_fields, dict):
                for parameter in item['parameters']:
                    key = parameter.get('id')
                    if key in enabled_fields and parameter.get('parameter_enable_supported'):
                        parameter['enabled'] = bool(enabled_fields[key])
                        parameter['enable_source'] = 'editor recipe registry'
        item['editable'] = bool(image.editable and style >= 34 and
                                registry_consistent and
                                len(item['resources'].get('banks', [])) == 3 and
                                'hex' in item['resources']['icon'] and
                                'hex' in item['resources']['name'])
        if not registry_consistent:
            for parameter in item['parameters']:
                parameter['editable'] = False
        filters.append(item)
    return filters
