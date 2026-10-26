"""GR IV 1.20 shutdown pictures, with the official downloaded-image path intact.

The caller supplies the actual shared owner and persistence chain. This module
does not choose an old owner tail or change UserData/UserMode snapshot layouts.
All generated outputs are offline candidates; no camera access occurs here.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from io import BytesIO
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from PIL import Image, ImageOps

from .slots import BASE, _branch, _mov, _toolchain
from .toolchain import linker_flags, _verify_elf


OWNER_BYTES = 8
STATE_MAGIC = 0x32424753
BACKGROUND = 0x531BAA50
OFFICIAL_SET = 0x5378F820
APDATA_UPPER = 0x53AA8BE8
JPEG_TABLE = 0x55014D98
OFFICIAL_JPEG_COUNT = 13
OFFICIAL_MENU_LABEL = 'GR standard'
THIRTIETH_SELECTION = 0x47523330
THIRTIETH_PATH = 'A:\\Resource\\Jpeg\\GB_30th.jpg'
ANNIVERSARY_ASSET_PATH = 'A:\\Resource\\Jpeg\\G47523330.jpg'
MENU_HOOKS = {
    'shutdown120_menu_refresh': 0x53225B94,
    'shutdown120_menu_confirm': 0x53225A54,
    'shutdown120_menu_left': 0x532259C4,
    'shutdown120_menu_right': 0x53225A08,
    'shutdown120_menu_footer': 0x53225C4C,
}
NATIVE_APIS = {
    'APP_GET': 0x532406A4, 'DISPLAY_GET': 0x53240998,
    'USERDATA_GET': 0x53240C38, 'JPEG_REQUEST': 0x53264274,
    'APDATA_FACTORY': 0x53AA958C, 'APDATA_WRITE': 0x53AA6764,
    'APDATA_READ': 0x53AA6680, 'APDATA_EXISTS': 0x53AA65D4,
    'APDATA_LENGTH': 0x53AA6524, 'STATISTICS_GET': 0x5329EE24,
    'STATISTICS_SET': 0x53342338, 'STATISTICS_LABEL': 0x5338FE60,
    'LIST_SELECTED': 0x53593E34, 'LIST_ITEM': 0x53593D64,
    'ITEM_ACTIVE': 0x5359E00C, 'ITEM_LABEL': 0x5359E0AC,
    'LIST_FOCUS': 0x53594A50, 'LIST_MOVE': 0x53594880,
    'GRAPHIC_ICON': 0x5359A104, 'GRAPHIC_VISIBLE': 0x535ACD84,
}
# Exact 1.20.10.11 functions/contracts; no fuzzy candidate is installed.
CONTRACTS = (
    (BACKGROUND, 0x20, '026d501b4aec75e72f72f1d572a90cecdc4f0edc8233461397cc52520dfd7058'),
    (OFFICIAL_SET, 0x154, '486aca9681101bdf0ea4b01bf4982ddb3ae8c478d9b76d40ee30c49aac27e09b'),
    (0x53225650, 0xC8, 'd14b439d50e093a290eef3fcdb29974c3494e24fc82de06ef7a3e73c4f29e6fb'),
    (0x532259C4, 0x90, 'fbae4ecb7fe1cb19b76a76b73fb2f36b029dc9cb4be3eb4c2e67fa6eca0ea4b9'),
    (0x53225A54, 0x38, 'f5ab51e6243e5113e3ff1928eb6d918b36cd2641d6cc80405be8d3a48a31a2fb'),
    (0x53225B94, 0xB8, '86b3049c7122f96a960e89a49abc699fdd8b641a2973e8187241df52ae8efddc'),
    (0x53225C4C, 0x7C, '215598eb1bf8dccc2b9f4b5df7e4758cc3beb18e5c5f1bf9e66bc78da353f600'),
    (0x53264274, 0x11C, '213ee259a83c4d969f26e00436525dc78d971b6ca4b514f330f170c5e8f0511c'),
    (0x53AA6524, 0x40, '93e1d7ec044411a5722041668d7dcf9877a9d8a86eddabbd93c7dd5fedfa64ee'),
    (0x53AA65D4, 0x40, '1cc1dd0ca25b9fe060209c774740b175d4b2c1226cef33b53acdb1d63c375179'),
    (0x53AA6680, 0x80, 'b28c42b32c7fd65f7bb499b034cc2afbb7183c174b6921e79821c76fddb1052c'),
    (0x53AA6764, 0x60, '54614bd8f0aa2b91c330c84534b619c282be38b04a2db9aa3ebf4c5b5825107c'),
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_static(rtos, address, length):
    source, destination, count = struct.unpack_from('<3I', rtos, 0x450)
    if (source, destination, count) != (0x543B3100, 0x55000000, 0x58500):
        raise ValueError('1.20 initialized data layout does not match')
    if destination <= address < destination + count:
        if address + length > destination + count:
            raise ValueError('Static read crosses initialized data')
        address = source + address - destination
    offset = address - BASE
    if not 0 <= offset <= offset + length <= len(rtos):
        raise ValueError('Static read is outside 1.20 RTOS')
    return bytes(rtos[offset:offset + length])


def _storage_address(rtos, address):
    source, destination, count = struct.unpack_from('<3I', rtos, 0x450)
    return source + address - destination if destination <= address < destination + count else address


def _set_static_word(patch, address, value, reason):
    patch.set_word(_storage_address(patch.image, address), value, reason)


def _install_upper_bound(patch, site, maximum):
    """Retain the new native CMP condition and live registers when it grows."""
    word = patch.word(site)
    if word & 0x0FF00000 == 0x03000000:  # MOVW bound in an existing generated stub.
        old = (word & 4095) | ((word >> 4) & 0xF000)
        patch.set_word(site, _mov((word >> 12) & 15, max(old, maximum)), '1.20 resource maximum')
        return
    if word & 0x0FF0F000 != 0x03500000:  # CMP Rn,#immediate, including CMPLS.
        raise ValueError('1.20 resource bound is not an identified CMP/MOVW instruction')
    rotation = ((word >> 8) & 15) * 2
    imm = word & 255
    old = ((imm >> rotation) | (imm << (32-rotation))) & 0xFFFFFFFF if rotation else imm
    maximum = max(old, maximum)
    register = (word >> 16) & 15
    compare = (word & 0xF0000000) | 0x01500000 | register << 16 | 12
    stub = patch.append(lambda at: [0xE92D1000, _mov(12, maximum), compare,
                                   0xE8BD1000, _branch(at+16, site+4)], 16)
    patch.set_word(site, _branch(site, stub), '1.20 extended resource CMP preserves condition and ip')


def verify_shutdown120_contract(rtos):
    for address, length, digest in CONTRACTS:
        if _sha(rtos[address-BASE:address-BASE+length]) != digest:
            raise ValueError(f'1.20 shutdown native contract mismatch at {address:#x}')
    table = _read_static(rtos, JPEG_TABLE, OFFICIAL_JPEG_COUNT * 4)
    last, = struct.unpack_from('<I', table, 12 * 4)
    width, height, path = struct.unpack('<HHI', _read_static(rtos, last, 8))
    text = _read_static(rtos, path, 32).split(b'\0', 1)[0]
    if (width, height, text) != (720, 480, b'A:\\Resource\\Jpeg\\GB_30th.jpg'):
        raise ValueError('Official JPEG ID12/30th is not intact')
    return {'status': 'verified_static', 'original_jpeg_count': 13,
            'official_downloaded_image_path': 'B:\\GB_DL.jpg', 'hardware': 'not_tested'}


def _menu_presets(plan):
    """Menu order is independent of native resource IDs and the RES append order."""
    rows = plan['presets']
    if 'menu_order' not in plan:
        return rows
    by_choice = {row['selection_id']: row for row in rows}
    return [by_choice[choice] for choice in plan['menu_order']]


def extract_shutdown120_plan(image, metadata, *, official=None, anniversary_asset=None):
    """Read actual .97 resources, preserving stable choice IDs and exact JPEGs."""
    from .shutdown import _res_records, _jpeg_info
    compiled = metadata.get('shutdown_menu', {})
    records = _res_records(image)
    presets = []
    for index, item in enumerate(compiled.get('presets', [])):
        row = deepcopy(item)
        record = records[row['path']]
        payload = image.decoded[record['offset']:record['offset'] + record['size']]
        info = _jpeg_info(payload)
        if (_sha(payload) != row['sha256'] or
                (info['width'], info['height'], info['baseline']) != (720, 480, True)):
            raise ValueError('Custom shutdown JPEG readback mismatch')
        row.update(native_id=OFFICIAL_JPEG_COUNT + index, index=index, slot=index+1,
                   payload_base64=base64.b64encode(payload).decode('ascii'))
        presets.append(row)
    if not 0 < len(presets) <= 16:
        raise ValueError('Migration needs one to sixteen verified custom shutdown images')
    thirtieth_status = None
    if official is not None:
        verify_shutdown120_contract(official.rtos)
        descriptor, = struct.unpack('<I', _read_static(official.rtos, JPEG_TABLE + 12*4, 4))
        width, height, path = struct.unpack('<HHI', _read_static(official.rtos, descriptor, 8))
        if (width, height, _read_static(official.rtos, path, 32).split(b'\0', 1)[0]) != (
                720, 480, THIRTIETH_PATH.encode('ascii')):
            raise ValueError('Official 30th shutdown picture is not native JPEG ID12')
        thirtieth_status = {'path': THIRTIETH_PATH, 'native_id': 12,
            'included_in_official_res': THIRTIETH_PATH in _res_records(official),
            'factory_descriptor_retained': True}
    menu_order = [row['selection_id'] for row in presets]
    if anniversary_asset is not None:
        payload = anniversary_asset['payload']
        info = _jpeg_info(payload)
        if (_sha(payload) != anniversary_asset['sha256'] or
                (info['width'], info['height'], info['baseline']) != (720, 480, True)):
            raise ValueError('Official public 30th shutdown JPEG digest/dimensions/encoding differ')
        index = len(presets)
        presets.append({'selection_id': THIRTIETH_SELECTION, 'native_id': OFFICIAL_JPEG_COUNT + index,
            'index': index, 'slot': index+1,
            'name': 'GR 30th anniversary', 'path': ANNIVERSARY_ASSET_PATH,
            'sha256': _sha(payload), 'payload_base64': base64.b64encode(payload).decode('ascii'),
            'source': deepcopy(anniversary_asset['source'])})
        menu_order.insert(0, THIRTIETH_SELECTION)
    return {'schema_version': 120, 'presets': presets, 'menu_order': menu_order,
            'official_thirtieth_descriptor': thirtieth_status,
            'legacy_choices': deepcopy(compiled.get('legacy_choices', {})),
            'default_selection': 0, 'selection_scope': 'global',
            'official_path_retained': 'EndingScreenInfo, including GR WORLD download',
            'hardware': 'not_tested'}


def install_shutdown120_jpeg_table(patch, plan):
    """Append custom IDs after all thirteen original descriptors."""
    original = _read_static(patch.image, JPEG_TABLE, 13 * 4)
    pointers = []
    for index, row in enumerate(plan['presets']):
        if row['native_id'] != 13 + index:
            raise ValueError('1.20 custom JPEG IDs must follow all thirteen originals')
        path = patch.append(row['path'].encode('ascii') + b'\0', 4)
        pointers.append(patch.append(struct.pack('<HHI', 720, 480, path), 4))
    table_bytes = original + struct.pack('<%dI' % len(pointers), *pointers)
    address = patch.append(table_bytes, 16)
    for site, expected, high in ((0x53264278, _mov(3, 0x4D98), False),
                                 (0x53264288, _mov(3, 0x5501, high=True), True)):
        if patch.word(site) != expected:
            raise ValueError('1.20 JPEG table consumer is already modified')
        value = address >> 16 if high else address & 65535
        patch.set_word(site, _mov(3, value, high=high), '1.20 retain thirteen official JPEGs and append custom pictures')
    return {'address': hex(address), 'count': 13 + len(pointers),
            'preserved_original_count': 13, 'preserved_original_sha256': _sha(original),
            'sha256': _sha(table_bytes), 'descriptors': [hex(x) for x in pointers]}


def install_shutdown120_labels(patch, plan, *, text_catalog, text_count,
                               first_text_id, bound_sites=()):
    """Extend caller-owned localized catalogs without assuming the old count."""
    labels = [OFFICIAL_MENU_LABEL] + [row['name'] for row in _menu_presets(plan)]
    if first_text_id < text_count:
        raise ValueError('Shutdown labels overlap existing text IDs')
    result = []
    for language in range(21):
        old, = struct.unpack('<I', _read_static(patch.image, text_catalog + language*4, 4))
        prefix = _read_static(patch.image, old, text_count * 4)
        descriptors = []
        for label in labels:
            raw = (label + '\0').encode('utf-16-le')
            if len(raw) // 2 > 255:
                raise ValueError('Shutdown label exceeds native UTF-16 descriptor length')
            pointer = patch.append(raw, 4)
            descriptors.append(patch.append(bytes((len(raw)//2, 1, 0, 0)) + struct.pack('<I', pointer), 4))
        table = patch.append(prefix + bytes((first_text_id-text_count)*4) +
                             struct.pack('<%dI' % len(descriptors), *descriptors), 16)
        _set_static_word(patch, text_catalog + language*4, table, '1.20 shutdown localized names preserve complete prior catalog')
        result.append({'language_index': language, 'table': hex(table),
                       'preserved_entries': text_count, 'preserved_sha256': _sha(prefix)})
    last = first_text_id + len(labels) - 1
    for site in bound_sites:
        _install_upper_bound(patch, site, last)
    return {'text_ids': list(range(first_text_id, last+1)), 'catalog_count': last+1,
            'languages': result, 'labels': labels}


def install_shutdown120_previews(patch, icon_bytes, plan, *, icon_catalog,
                                 first_icon_id, bound_sites=()):
    """Append only custom thumbnails; the official downloaded image stays dynamic."""
    if (icon_catalog in (0x55014280, 0x543C7380) and
            first_icon_id + len(plan['presets']) > 709):
        raise ValueError('Caller must expand/relocate the shared 709-entry 1.20 icon directory first')
    icons = bytearray(icon_bytes)
    rows = []
    for index, row in enumerate(plan['presets']):
        identity = first_icon_id + index
        if struct.unpack('<I', _read_static(patch.image, icon_catalog + identity*4, 4))[0]:
            raise ValueError('1.20 custom shutdown preview ID is occupied')
        picture = Image.open(BytesIO(base64.b64decode(row['payload_base64']))).convert('RGB')
        resized = ImageOps.contain(picture, (520, 200), Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (520, 200), (0, 0, 0, 255))
        canvas.paste(resized, ((520-resized.width)//2, (200-resized.height)//2))
        pixels = canvas.tobytes()
        icons.extend(bytes((-len(icons)) % 4))
        offset = len(icons)
        icons.extend(pixels)
        descriptor = patch.append(struct.pack('<HHHHI', 1, 520, 200, 0, offset//4), 16)
        _set_static_word(patch, icon_catalog + identity*4, descriptor, '1.20 custom shutdown thumbnail')
        rows.append({'selection_id': row['selection_id'], 'icon_id': identity,
                     'descriptor': hex(descriptor), 'pixel_offset': offset,
                     'pixel_sha256': _sha(pixels), 'source_jpeg_sha256': row['sha256']})
    last = first_icon_id + len(rows) - 1
    for site in bound_sites:
        _install_upper_bound(patch, site, last)
    indexed = {row['selection_id']: row for row in rows}
    ordered = [indexed[row['selection_id']] for row in _menu_presets(plan)]
    return bytes(icons), {'icon_ids': [row['icon_id'] for row in ordered], 'rows': ordered,
                         'official_row_static_preview': False,
                         'input_icon_sha256': _sha(icon_bytes), 'hardware': 'not_tested'}


def install_shutdown120_assets(catalogs, icon_bytes, plan, *, first_text_id=900,
                               first_icon_id=768):
    """Use the caller's shared Catalog120 allocation and already-installed bounds."""
    names = [OFFICIAL_MENU_LABEL] + [row['name'] for row in _menu_presets(plan)]
    for index, name in enumerate(names):
        catalogs.add_text(first_text_id+index, name)
    icons, rows = bytearray(icon_bytes), []
    # Retain the two migrated preview IDs; append built-in previews after them.
    for index, row in enumerate(plan['presets']):
        with Image.open(BytesIO(base64.b64decode(row['payload_base64']))) as source:
            picture = ImageOps.contain(source.convert('RGB'), (520, 200), Image.Resampling.LANCZOS)
        canvas = Image.new('RGBA', (520, 200), (0, 0, 0, 255))
        canvas.paste(picture, ((520-picture.width)//2, (200-picture.height)//2))
        resource = catalogs.add_icon(first_icon_id+index, canvas.tobytes(), icons, width=520, height=200)
        rows.append(dict(resource, selection_id=row['selection_id'],
                         source_jpeg_sha256=row['sha256']))
    previews = {row['selection_id']: row for row in rows}
    ordered = [previews[row['selection_id']] for row in _menu_presets(plan)]
    return bytes(icons), {'text_ids': list(range(first_text_id, first_text_id+len(names))),
                         'preview_icon_ids': [row['id'] for row in ordered], 'previews': ordered,
                         'labels': names, 'official_row_static_preview': False,
                         'hardware': 'not_tested'}


def compile_shutdown120_code(origin, plan, context):
    """Compile against explicit callable prior hooks and the actual owner layout."""
    rows = _menu_presets(plan)
    choices = [(0, 0)] + [(row['selection_id'], row['native_id']) for row in rows]
    if len({choice for choice, _ in choices}) != len(choices):
        raise ValueError('Shutdown stable choice IDs are duplicated or zero')
    if len(context['text_ids']) != len(choices) or len(context['preview_icon_ids']) != len(rows):
        raise ValueError('Shutdown text and preview IDs do not match the image plan')
    definitions = dict(NATIVE_APIS)
    definitions.update(OWNER_CELL=context['owner_cell'], OWNER_GETTER=context['owner_getter'],
                       OWNER_OFFSET=context['owner_offset'], SHUTDOWN_RECORD=context.get('record', 73),
                       CHOICE_COUNT=len(choices), MENU_ROWS=2+len(choices),
                       STANDARD_PREVIEW_ICON=context.get('standard_preview_icon', 0),
                       LEGACY_CHOICE_1=plan.get('legacy_choices', {}).get('1', 0),
                       LEGACY_CHOICE_2=plan.get('legacy_choices', {}).get('2', 0),
                       HAS_PRIOR_INITIALIZER=int(bool(context.get('prior_initializer'))))
    source = ''.join(f'#define {name} {value}u\n' for name, value in definitions.items())
    source += '#define SHUTDOWN120_CHOICES {' + ','.join('{%du,%du}' % item for item in choices) + '}\n'
    source += '#define SHUTDOWN120_TEXT_IDS {' + ','.join(str(x)+'u' for x in context['text_ids']) + '}\n'
    source += '#define SHUTDOWN120_PREVIEW_ICONS {' + ','.join(str(x)+'u' for x in context['preview_icon_ids']) + '}\n'
    source += (Path(__file__).parent / 'native/shutdown120.c').read_text(encoding='utf-8')
    targets = {'prior_shutdown120_save': context['prior_save'], 'prior_shutdown120_load': context['prior_load'],
               'official_shutdown120_background_body': BACKGROUND+4,
               'official_shutdown120_set_body': OFFICIAL_SET+4}
    if context.get('prior_initializer'):
        targets['prior_shutdown120_initializer'] = context['prior_initializer']
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-shutdown120-') as folder:
        directory = Path(folder)
        c, linker, elf, binary = [directory/name for name in ('shutdown.c', 'shutdown.ld', 'shutdown.elf', 'shutdown.arm')]
        c.write_text(source, encoding='utf-8')
        linker.write_text(''.join(f'{name} = {value:#x};\n' for name, value in targets.items()) +
                          f'SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
                          '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        result = subprocess.run([str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi',
            '-mcpu=cortex-a9', '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
            '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib', '-Wl,-T,'+str(linker),
            '-Wl,--entry=shutdown120_get', str(c), '-o', str(elf)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('1.20 shutdown ARM compilation failed: '+result.stderr[-2000:])
        _verify_elf(elf.read_bytes())
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True, capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M) or 'Thunk_' in listing:
            raise ValueError('1.20 shutdown module has unresolved or out-of-range native calls')
        symbols = {name: int(at, 16) for at, kind, name in
                   re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M) if kind in 'Tt'}
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text', str(elf), str(binary)], check=True, capture_output=True)
        blob = binary.read_bytes()
    return blob, symbols, {'address': hex(origin), 'bytes': len(blob), 'sha256': _sha(blob),
                           'source_sha256': _sha(source.encode()),
                           'symbols': {name: hex(at) for name, at in symbols.items()}}


def install_shutdown120(patch, plan, context):
    """Install after filter/crop state; prior_save/load must be callable wrappers.

    Required context: owner_cell/getter/offset, prior_save/load, optional
    prior_initializer, save_entry/load_entry, text_ids and preview_icon_ids.
    The returned initializer must replace/chain the caller's owner initializer.
    """
    verify_shutdown120_contract(bytes(patch.image))
    if context.get('record', 73) != 73:
        raise ValueError('The .97 SGB2 record must retain its verified APData73 identity')
    table = install_shutdown120_jpeg_table(patch, plan)
    origin = (BASE + len(patch.image) + 15) & ~15
    blob, symbols, code = compile_shutdown120_code(origin, plan, context)
    if origin + len(blob) >= 0x55000000 or patch.append(blob, 16) != origin:
        raise ValueError('1.20 shutdown module exceeds its executable mapping')
    for site, name in ((BACKGROUND, 'shutdown120_request_background'),
                       (OFFICIAL_SET, 'shutdown120_official_set'), *[(site, name) for name, site in MENU_HOOKS.items()],
                       (context['save_entry'], 'shutdown120_state_save'),
                       (context['load_entry'], 'shutdown120_state_load')):
        patch.set_word(site, _branch(site, symbols[name]), '1.20 shutdown '+name)
    rows = 3 + len(_menu_presets(plan))
    types = patch.append(bytes([21]) * rows, 4)
    init = patch.append(lambda at: [_mov(1, types & 65535), _mov(1, types >> 16, high=True),
                                   _branch(at+8, 0x53225688)], 16)
    patch.set_word(0x53225654, _mov(2, rows), '1.20 shutdown named image rows')
    patch.set_word(0x53225684, _branch(0x53225684, init), '1.20 dynamic type21 row vector')
    upper = max(context.get('apdata_upper_bound', 16), 74)
    upper_stub = patch.append(lambda at: [_mov(3, upper), 0xE58D3000, _branch(at+8, APDATA_UPPER+4)], 16)
    patch.set_word(APDATA_UPPER, _branch(APDATA_UPPER, upper_stub), '1.20 preserve APData domain and custom73 record')
    return {'schema_version': 120, 'installed': True, 'code': code, 'symbols': symbols,
            'jpeg_table': table, 'owner_offset': context['owner_offset'], 'owner_bytes': OWNER_BYTES,
            'initializer': symbols['shutdown120_initialize_owner'], 'record': 73,
            'record_bytes': 8, 'state_magic': 'SGB2', 'apdata_upper_bound': upper,
            'row_count': rows, 'choices': [0]+[row['selection_id'] for row in _menu_presets(plan)],
            'menu_labels': [OFFICIAL_MENU_LABEL]+[row['name'] for row in _menu_presets(plan)],
            'text_ids': list(context['text_ids']), 'preview_icon_ids': list(context['preview_icon_ids']),
            'choice_resources': [{'selection_id': row['selection_id'], 'native_id': row['native_id']}
                                 for row in _menu_presets(plan)],
            'official_choice': 0, 'official_success_clears_override': True,
            'official_downloaded_image_path_preserved': 'B:\\GB_DL.jpg',
            'save_entry': context['save_entry'], 'load_entry': context['load_entry'],
            'original_background_body_sha256': _sha(patch.image[BACKGROUND+4-BASE:BACKGROUND+32-BASE]),
            'original_setter_body_sha256': _sha(patch.image[OFFICIAL_SET+4-BASE:OFFICIAL_SET+0x154-BASE]),
            'statistics_parameters_preserved': True, 'scope': 'global; excluded from UserMode snapshots',
            'hardware': 'not_tested'}


def inspect_shutdown120(rtos, report):
    """Read actual installed instructions/directories from a redecoded candidate."""
    try:
        code = report['code']
        address, size = int(code['address'], 16), code['bytes']
        if _sha(rtos[address-BASE:address-BASE+size]) != code['sha256']:
            raise ValueError('1.20 shutdown appended code differs')
        symbols = {name: int(value, 16) for name, value in code['symbols'].items()}
        hooks = [(BACKGROUND, 'shutdown120_request_background'),
                 (OFFICIAL_SET, 'shutdown120_official_set'),
                 *[(site, name) for name, site in MENU_HOOKS.items()],
                 (report['save_entry'], 'shutdown120_state_save'),
                 (report['load_entry'], 'shutdown120_state_load')]
        for site, name in hooks:
            word, = struct.unpack_from('<I', rtos, site-BASE)
            if word != _branch(site, symbols[name]):
                raise ValueError(f'1.20 shutdown hook differs at {site:#x}')
        for start, end, key in ((BACKGROUND+4, BACKGROUND+32, 'original_background_body_sha256'),
                                (OFFICIAL_SET+4, OFFICIAL_SET+0x154, 'original_setter_body_sha256')):
            if _sha(rtos[start-BASE:end-BASE]) != report[key]:
                raise ValueError('An official shutdown body changed behind its entry wrapper')
        table = report['jpeg_table']
        address, count = int(table['address'], 16), table['count']
        raw = rtos[address-BASE:address-BASE+count*4]
        if _sha(raw) != table['sha256'] or _sha(raw[:13*4]) != table['preserved_original_sha256']:
            raise ValueError('1.20 shutdown JPEG directory readback differs')
        for site, word in ((0x53264278, _mov(3, address & 65535)),
                           (0x53264288, _mov(3, address >> 16, high=True))):
            if struct.unpack_from('<I', rtos, site-BASE)[0] != word:
                raise ValueError('1.20 shutdown JPEG directory is not selected by the native API')
        return {'status': 'verified_static', 'hooks_verified': len(hooks),
                'official_jpeg_entries_preserved': 13, 'custom_jpeg_entries': count-13,
                'official_background_and_setter_bodies': 'byte_identical', 'hardware': 'not_tested'}
    except (KeyError, TypeError, ValueError, struct.error) as error:
        return {'status': 'invalid', 'reason': str(error), 'hardware': 'not_tested'}
