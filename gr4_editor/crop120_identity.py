"""Preserve actual .97 archived photo crop identities on the 1.20 native base.

Source ImageProperty fields remain +7DA/+7DC/+6C0 in the checked 1.20
instructions. They are distinct from ImageDevelop's changed runtime layout.
The maintained portable identity/metadata C sources are rendered at build time.
"""
from __future__ import annotations

from fractions import Fraction
from hashlib import sha256
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .crop_image_identity import _playback_rectangles
from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, _branch, _toolchain
from .toolchain import linker_flags, _verify_elf


GATES = {
    0x5381C380: ('crop_image_extract_gate', 0xE3500003),
    0x53833DFC: ('crop_image_set_gate', 0xE3530003),
    0x536DB01C: ('crop_image_playback_gate', 0xE35A0003),
    0x536EC1B0: ('crop_image_aspect_gate', 0xE3500003),
}
JOINS = {
    'native_extract_table': 0x5381C384, 'native_extract_unknown': 0x5381C388,
    'native_extract_return': 0x5381C3A8,
    'native_set_table': 0x53833E00, 'native_set_unknown': 0x53833E04,
    'native_set_join': 0x53833E1C,
    'native_playback_table': 0x536DB020, 'native_playback_unknown': 0x536DB024,
    'native_playback_join': 0x536DB03C,
    'native_aspect_table': 0x536EC1B4, 'native_aspect_unknown': 0x536EC1B8,
    'native_aspect_return': 0x536EC1D0,
    'native_screen_rect_body': 0x536F2090,
    'native_thumbnail_rect_body': 0x536F2284,
    'factory_source_rectangle_body': 0x5381D6C0,
}
FIELD_WORDS = {
    0x5381C3A4: 0xE5C237DA,  # original extractor writes source public identity
    0x5381D6D0: 0xE5D327DA,  # source metadata current public identity
    0x5381D6D4: 0xE2833E7D,  # source+7D0 then native VFP load+0xC
    0x5381D70C: 0xE28CCD1B,  # source metadata rectangle destination+6C0
    0x536F1C18: 0xE5C707DA,  # playback caller writes returned source identity
}


def _source_bases120(baseline):
    """Execute native constant-table construction/getter; only atexit is stubbed."""
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
    from unicorn import arm_const as arm
    cpu = Uc(UC_ARCH_ARM, UC_MODE_ARM)
    cpu.mem_map(BASE, (len(baseline) + 4095) & ~4095)
    cpu.mem_write(BASE, bytes(baseline))
    cpu.mem_map(0x55000000, 0x400000)
    source, destination, length = struct.unpack_from('<3I', baseline, 0x450)
    cpu.mem_write(destination, baseline[source - BASE:source - BASE + length])
    cpu.mem_map(0x61000000, 0x100000)
    cpu.mem_map(0x60000000, 0x1000)
    cpu.reg_write(arm.UC_ARM_REG_FPEXC, 0x40000000)
    def boundary(cpu, address, size, unused):
        if address == 0x53B7003C:
            cpu.reg_write(arm.UC_ARM_REG_R0, 0)
            cpu.reg_write(arm.UC_ARM_REG_PC, cpu.reg_read(arm.UC_ARM_REG_LR))
    cpu.hook_add(UC_HOOK_CODE, boundary)
    def call(address):
        cpu.reg_write(arm.UC_ARM_REG_SP, 0x610FF000)
        cpu.reg_write(arm.UC_ARM_REG_LR, 0x60000000)
        cpu.emu_start(address, 0x60000000, count=100000)
        if cpu.reg_read(arm.UC_ARM_REG_PC) != 0x60000000:
            raise ValueError('Native 1.20 metadata table function did not return')
        return cpu.reg_read(arm.UC_ARM_REG_R0)
    call(0x53823810)
    bases = []
    for value in (1.0, *struct.unpack_from('<2f', baseline, 0x538236D0 - BASE)):
        bits, = struct.unpack('<I', struct.pack('<f', value))
        cpu.reg_write(arm.UC_ARM_REG_S0, bits)
        pointer = call(0x5382367C)
        if not 0x55000000 <= pointer < 0x55400000:
            raise ValueError('Unknown native source magnification table')
        bases.append((value, struct.unpack('<4I', cpu.mem_read(pointer, 16))))
    return bases


def _metadata_rectangles120(records, baseline):
    bases = _source_bases120(baseline)
    output = []
    for row in records:
        public = row['identity']['public_id']
        if public < 4:
            continue
        preview = row.get('geometry', {}).get('preview')
        if not preview:
            continue  # Unknown/foreign archived geometry is never invented.
        fx, fy = Fraction(preview['width'], 45 << 16), Fraction(preview['height'], 30 << 16)
        if not 0 < fx <= 1 or not 0 < fy <= 1:
            raise ValueError('Archived source rectangle exceeds native canvas')
        for magnification, (width, height, left, top) in bases:
            cropped_w, cropped_h = round(width * fx), round(height * fy)
            output.append(dict(public_id=public,
                mag_bits=struct.unpack('<I', struct.pack('<f', magnification))[0],
                magnification=magnification,
                rectangle_q16=[cropped_w, cropped_h, left + (width - cropped_w) // 2,
                               top + (height - cropped_h) // 2]))
    return output, bases


def _compile120(origin, identity_source, metadata_source, bindings):
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-crop120-identity-') as temporary:
        directory = Path(temporary)
        c, metadata, ld, elf, binary = (directory / name for name in
            ('identity.c', 'metadata.c', 'identity.ld', 'identity.elf', 'identity.arm'))
        c.write_text(identity_source, encoding='utf-8')
        metadata.write_text(metadata_source, encoding='utf-8')
        ld.write_text(' '.join(f'{name} = {address:#x};' for name, address in bindings.items()) +
            f' SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
            '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        command = [str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9',
            '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
            '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
            '-Wl,-T,' + str(ld), '-Wl,--entry=crop_image_extract_gate', str(c), str(metadata), '-o', str(elf)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('1.20 crop identity link failed: ' + result.stderr[-1800:])
        _verify_elf(elf.read_bytes())
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text', str(elf), str(binary)],
                       check=True, capture_output=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True, capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('Unresolved 1.20 crop identity symbols')
        symbols = {name: int(at, 16) for at, kind, name in
            re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M) if kind in 'Tt'}
        return binary.read_bytes(), symbols


def install_image_identity120(patch, plan, *, baseline_rtos):
    baseline = bytes(baseline_rtos)
    if sha256(baseline).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('1.20 photo identity requires the verified official base')
    contracts = {**FIELD_WORDS, **{site: expected for site, (_, expected) in GATES.items()},
                 0x536F208C: 0xE1A0C00D, 0x536F2280: 0xE1A0C00D,
                 0x5381D6BC: 0xE1A0C00D,
                 0x536F1C14: _branch(0x536F1C14, 0x536EC19C, link=True)}
    for address, expected in contracts.items():
        word, = struct.unpack_from('<I', baseline, address - BASE)
        if word != expected or patch.word(address) != expected:
            raise ValueError(f'1.20 photo identity/source-field contract changed: {address:#x}')
    records = plan['records']
    known = sorted({row['identity']['public_id'] for row in records if row['identity']['public_id'] >= 4})
    if any(type(public) is not int or public > 255 for public in known):
        raise ValueError('Archived crop identity does not fit native BYTE storage')
    playback = _playback_rectangles(records)
    rectangles, bases = _metadata_rectangles120(records, baseline)
    archive = patch.append(bytes(int(public in known) for public in range(256)), 16)
    replay_table = patch.append(b''.join(struct.pack('<10I', *row['source_size'], *row['rectangles']) for row in playback), 16)
    metadata_table = patch.append(b''.join(struct.pack('<6I', row['public_id'], row['mag_bits'], *row['rectangle_q16']) for row in rectangles), 16)
    directory = Path(__file__).parent / 'native'
    original_identity = (directory / 'crop_image_identity.c').read_text(encoding='utf-8')
    identity = '#define CROP_REPLAY_RECT_COUNT %d\n' % len(playback)
    identity += original_identity.replace('0x536c79ccu', '0x536ec19cu')
    original_metadata = (directory / 'crop_state.c').read_text(encoding='utf-8')
    marker = '/* The displaced native entry'
    if marker not in original_metadata:
        raise ValueError('Maintained metadata source fragment changed')
    metadata = ('#include <stdint.h>\n#define CROP_RECT_COUNT %d\n' % len(rectangles) +
        'typedef struct { uint32_t public_id, mag_bits, rectangle[4]; } CropRect;\n'
        'extern const CropRect crop_source_rectangles[];\n' + marker + original_metadata.split(marker, 1)[1])
    origin = (BASE + len(patch.image) + 15) & ~15
    blob, symbols = _compile120(origin, identity, metadata,
        {'crop_image_archive': archive, 'crop_image_rectangles': replay_table,
         'crop_source_rectangles': metadata_table, **JOINS})
    if patch.append(blob, 16) != origin:
        raise ValueError('1.20 crop identity append origin changed')
    first_change = len(patch.changes)
    for address, (symbol, _) in GATES.items():
        patch.set_word(address, _branch(address, symbols[symbol]), '1.20 preserve complete archived photo crop BYTE identities')
    context = patch.append(lambda at: [0xE1A01007, _branch(at + 4, symbols['crop_image_source_aspect'])], 16)
    patch.set_word(0x536F1C14, _branch(0x536F1C14, context, link=True), '1.20 playback dimension update retains actual source crop identity')
    for site, symbol in ((0x536F208C, 'crop_image_screen_rect'), (0x536F2280, 'crop_image_thumbnail_rect'),
                         (0x5381D6BC, 'crop_metadata_rectangle')):
        patch.set_word(site, _branch(site, symbols[symbol]), '1.20 archived crop source/playback rectangle with native factory fallback')
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('1.20 crop identity append reaches runtime memory')
    return dict(backend='official-1.20-archived-photo-crop-identity', origin=hex(origin), bytes=len(blob),
        code_sha256=sha256(blob).hexdigest(), symbols={key: hex(value) for key, value in symbols.items()},
        archive_bitmap=hex(archive), archived_custom_public_ids=known,
        playback_rectangles=playback, source_metadata_rectangles=rectangles,
        native_metadata_bases=[{'magnification': value, 'rectangle_q16': list(rect)} for value, rect in bases],
        source_fields={'public_id': '0x7da', 'magnification': '0x7dc', 'rectangle': '0x6c0'},
        source_identity_normalized=False, factory_unknown_paths_preserved=True,
        source_c_sha256={'identity': sha256(original_identity.encode()).hexdigest(), 'metadata': sha256(original_metadata.encode()).hexdigest()},
        rendered_c_sha256={'identity': sha256(identity.encode()).hexdigest(), 'metadata': sha256(metadata.encode()).hexdigest()},
        changes=patch.changes[first_change:], hardware='not_tested',
        native_base_boundary='Native constant metadata constructor and3 getters execute; only atexit registration is replaced with return0')
