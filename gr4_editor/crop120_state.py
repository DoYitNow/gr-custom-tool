"""1.20 settings normalization around the composed USER/APData save/load chain.

Only active current/snapshot settings are normalized. Historical image crop
identities and source rectangles belong to the independent identity adapter.
This module owns no additional persistent record or runtime state.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .layout120_userdata import USERDATA120
from .slots import BASE, _branch, _target
from .toolchain import resolve_toolchain, linker_flags

SAVE_ENTRY, LOAD_ENTRY = 0x533F3674, 0x533F3254


def _prior(patch, entry):
    first = patch.word(entry)
    if first & 0x0E000000 == 0x0A000000:
        return _target(entry, first)
    if first != 0xE1A0C00D:
        raise ValueError(f'Unknown 1.20 save/load native entry: {entry:#x}')
    return patch.append(lambda at: [first, _branch(at + 4, entry + 4)], 16)


def install_crop_state120(patch, active_bitmap):
    prior_save, prior_load = _prior(patch, SAVE_ENTRY), _prior(patch, LOAD_ENTRY)
    source = f'''
#include <stdint.h>
extern const uint8_t crop_active_bitmap[];
extern uint32_t prior_crop_save(void *);
extern uint32_t prior_crop_load(void *,void *,void *,void *);
static uint8_t *current(void) {{ return ((uint8_t *(*)(void)){USERDATA120.current_getter:#x}u)(); }}
static void normalize(uint8_t *p,uint32_t offset) {{
    if(p && !crop_active_bitmap[p[offset]]) p[offset]=0;
}}
static void mode(uint8_t *p) {{
    if(!p) return;
    uint8_t **slots=(uint8_t **)(p+{USERDATA120.snapshot_pointer_offset});
    for(uint32_t n=0;n<{USERDATA120.snapshot_count};++n)
        normalize(slots[n],{USERDATA120.snapshot_crop_offset});
}}
uint32_t crop_state120_save(uint8_t *p) {{
    normalize(current(),{USERDATA120.crop_offset}); mode(p);
    return prior_crop_save(p);
}}
uint32_t crop_state120_load(uint8_t *p,uint8_t *snapshot,void *a2,void *a3) {{
    uint32_t result=prior_crop_load(p,snapshot,a2,a3);
    normalize(snapshot,{USERDATA120.snapshot_crop_offset}); mode(p);
    normalize(current(),{USERDATA120.crop_offset});
    return result;
}}
'''
    origin = (BASE + len(patch.image) + 15) & ~15
    clang, objcopy, nm = resolve_toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-crop120-state-') as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = [directory / name for name in ('state.c', 'state.ld', 'state.elf', 'state.arm')]
        c.write_text(source, encoding='ascii')
        ld.write_text(f'crop_active_bitmap = {active_bitmap:#x}; prior_crop_save = {prior_save:#x}; '
                      f'prior_crop_load = {prior_load:#x}; SECTIONS {{ . = {origin:#x}; '
                      '.text : { *(.text*) *(.rodata*) } /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        run = subprocess.run([str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi',
                              '-mcpu=cortex-a9', '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding',
                              '-fno-builtin', '-fno-unwind-tables', '-fno-asynchronous-unwind-tables',
                              '-nostdlib', '-Wl,-T,' + str(ld), '-Wl,--entry=crop_state120_load',
                              str(c), '-o', str(elf)], capture_output=True, text=True)
        if run.returncode:
            raise ValueError('Crop 1.20 state compilation failed: ' + run.stderr[-1500:])
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text', str(elf), str(binary)],
                       capture_output=True, check=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], capture_output=True, text=True, check=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('Crop 1.20 state has unresolved symbols')
        symbols = {name: int(address, 16) for address, kind, name in
                   re.findall(r'^([a-fA-F0-9]+)\s+(\w)\s+(\w+)$', listing, re.M) if kind in 'Tt'}
        blob = binary.read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError('Crop 1.20 state origin changed')
    patch.set_word(SAVE_ENTRY, _branch(SAVE_ENTRY, symbols['crop_state120_save']), 'normalize active crop settings before 1.20 save chain')
    patch.set_word(LOAD_ENTRY, _branch(LOAD_ENTRY, symbols['crop_state120_load']), 'normalize active crop settings after 1.20 load chain')
    return {'address': hex(origin), 'bytes': len(blob), 'sha256': sha256(blob).hexdigest(),
            'source_sha256': sha256(source.encode()).hexdigest(), 'active_bitmap': hex(active_bitmap),
            'symbols': {name: hex(address) for name, address in symbols.items()},
            'prior_save': hex(prior_save), 'prior_load': hex(prior_load),
            'current_crop_offset': hex(USERDATA120.crop_offset),
            'bare_snapshot_crop_offset': hex(USERDATA120.snapshot_crop_offset),
            'snapshot_bytes': USERDATA120.snapshot_bytes, 'snapshot_count': USERDATA120.snapshot_count,
            'source_image_identity_normalized': False, 'owned_state_bytes': 0,
            'native_chain_readback': 'pending', 'hardware': 'not_tested'}
