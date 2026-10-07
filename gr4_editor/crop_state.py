# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Native crop recovery and archived metadata rectangles over the native layout.

Current and snapshot persistence continues through the preserved native save
and load entries, including the independently installed End Screen selection.
"""
from fractions import Fraction
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .crop_native import CropNative
from .toolchain import linker_flags

from .demo_build import BASE, _branch, _target, _toolchain


class _MetadataBase(CropNative):
    def _hook(self, cpu, at, size, user):
        if at == 0x53B3919C:
            self._return(0)  # Table constructor's exit callback registration.
        else:
            super()._hook(cpu, at, size, user)


def _source_rectangles(rtos, records):
    native = _MetadataBase(rtos)
    native.call(0x537FCC00)
    bases = []
    for mag in native.magnifications('Kb636'):
        pointer = native.call(0x537FCA6C, float_value=mag)
        if not pointer or native.diagnostics:
            raise ValueError('原生拍摄信息矩形倍率未解析')
        bases.append((mag, struct.unpack('<4I', native.uc.mem_read(pointer, 16))))
    rectangles = []
    for row in records:
        public = row['identity']['public_id']
        if public < 4:
            continue
        geometry = row.get('geometry', {})
        preview = geometry.get('preview')
        if preview:
            width_factor = Fraction(preview['width'], 45 * 65536)
            height_factor = Fraction(preview['height'], 30 * 65536)
            geometry_source = 'archived native virtual preview rectangle'
        else:
            screen = geometry.get('screen')
            if not screen:
                # Reserved or foreign historical identities have no proved
                # geometry. They remain unknown; no new rectangle is invented.
                continue
            width_factor = Fraction(screen['width'], screen['canvas'][0])
            height_factor = Fraction(screen['height'], screen['canvas'][1])
            geometry_source = 'compiled virtual preview from aligned screen geometry'
        if not 0 < width_factor <= 1 or not 0 < height_factor <= 1:
            raise ValueError('拍摄信息虚拟矩形超出原生 45×30 画布')
        for mag, (base_w, base_h, base_left, base_top) in bases:
            width = round(base_w * width_factor)
            height = round(base_h * height_factor)
            rectangles.append({
                'public_id': public,
                'mag_bits': struct.unpack('<I', struct.pack('<f', mag))[0],
                'magnification': mag,
                'rectangle_q16': [width, height,
                                  base_left + (base_w - width) // 2,
                                  base_top + (base_h - height) // 2],
                'geometry_source': geometry_source,
            })
    return rectangles


def install_state_metadata(patch, plan, active_bitmap):
    """Install current/snapshot normalization and source-only metadata closure.

    ``active_bitmap`` is the 256-byte active table already emitted by the menu
    compiler. Archived source rectangles deliberately include retired records.
    """
    source_before = bytes(patch.image)
    prior_save = _target(0x533E1C04, patch.word(0x533E1C04))
    prior_load = _target(0x533E17CC, patch.word(0x533E17CC))
    if (patch.word(0x532A90CC) != 0xEBFE566C or
            patch.word(0x537F6AAC) != 0xE1A0C00D):
        raise ValueError('裁切状态/拍摄信息原生入口与已识别的布局不一致')
    rectangles = _source_rectangles(source_before, plan['records'])
    table = patch.append(b''.join(struct.pack('<6I', row['public_id'],
                        row['mag_bits'], *row['rectangle_q16'])
                        for row in rectangles), 16)
    source = '#define CROP_RECT_COUNT %d\n' % len(rectangles)
    source += (Path(__file__).parent / 'native/crop_state.c').read_text(encoding='utf-8')
    origin = (BASE + len(patch.image) + 15) & ~15
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-crop-state-') as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = (directory / name for name in
                             ('crop_state.c', 'crop_state.ld', 'crop_state.elf', 'crop_state.arm'))
        c.write_text(source, encoding='utf-8')
        ld.write_text(
            f'crop_active_bitmap = {active_bitmap:#x}; crop_source_rectangles = {table:#x}; '
            f'prior_native_save = {prior_save:#x}; prior_native_load = {prior_load:#x}; '
            'factory_source_rectangle_body = 0x537F6AB0; '
            f'SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
            '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }',
            encoding='ascii')
        command = [str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9',
                   '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
                   '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
                   '-Wl,-T,' + str(ld), '-Wl,--entry=crop_state_load', str(c), '-o', str(elf)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('ARM 裁切状态模块编译失败: ' + result.stderr[-1500:])
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text',
                        str(elf), str(binary)], check=True, capture_output=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True,
                                 capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('ARM 裁切状态模块有未解析的符号')
        symbols = {name: int(at, 16) for at, kind, name in
                   re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M)
                   if kind in 'Tt'}
        blob = binary.read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError('ARM 裁切状态模块地址变化')
    change_start = len(patch.changes)
    patch.set_word(0x533E1C04, _branch(0x533E1C04, symbols['crop_state_save']),
                   'normalize active current and ten saved crop identities; retain native save')
    patch.set_word(0x533E17CC, _branch(0x533E17CC, symbols['crop_state_load']),
                   'normalize loaded bare current and ten crop snapshots; retain native load')
    site = 0x532A90CC
    stub = patch.append(lambda at: [0xE92D500F, 0xE1A00004,
                        _branch(at + 8, symbols['crop_normalize_userdata'], link=True),
                        0xE8BD500F, _branch(at + 16, 0x5323EA84)], 16)
    patch.set_word(site, _branch(site, stub, link=True),
                   'normalize crop after native snapshot copy before unchanged notifications and LiveView application')
    patch.set_word(0x537F6AAC, _branch(0x537F6AAC, symbols['crop_metadata_rectangle']),
                   'write archived custom source rectangle; retain original factory metadata')
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('裁切状态追加区域碰到已知运行数据映射')
    return {
        'address': hex(origin), 'bytes': len(blob),
        'sha256': hashlib.sha256(blob).hexdigest(),
        'source_c_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'symbols': {key: hex(value) for key, value in symbols.items()},
        'prior_native_save': hex(prior_save), 'prior_native_load': hex(prior_load),
        'active_bitmap': hex(active_bitmap), 'source_rectangle_table': hex(table),
        'source_rectangles': rectangles, 'changes': patch.changes[change_start:],
        'current_snapshot_bytes': 2793, 'runtime_crop_offset': '0x49F',
        'bare_snapshot_crop_offset': '0x49B', 'snapshot_count': 10,
        'deleted_setting_fallback_public_id': 0,
        'source_image_identity_normalized': False,
        'factory_metadata_body_preserved': True,
        'metadata_supported_model': 'Kb636 native magnification domain',
        'limits': ['Kb588 non-unit metadata magnifications retain original no-write behavior',
                   'Unknown source identities or magnifications retain original ROI and identity',
                   'Offline ARM validation; filesystem, complete boot and hardware remain unverified'],
        'hardware': 'not_tested',
    }
