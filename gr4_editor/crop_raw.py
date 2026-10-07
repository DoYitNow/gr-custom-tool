# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Source-dependent RAW targets and native ImageDevelop parameter views.

RAW internal codes are current-model bytes; public source identities remain
archived image metadata. The native setter/apply and map lifecycle execute.
"""
from fractions import Fraction
from pathlib import Path
import hashlib
import re
import struct
import subprocess
import tempfile
from unicorn import arm_const as reg
from .crop_native import CropNative, BASE
from .toolchain import assemble_arm, linker_flags

from .demo_build import _toolchain

VIEW_BYTES = 400 + 12 * 48
FACTORY_CODES = {0: 2, 1: 3, 2: 1, 3: 4}


def _asm(s, a):
    return assemble_arm(s, a)


def _imm(r, v):
    return f'movw {r},#{v & 65535};movt {r},#{v >> 16};'


class _Program:
    def __init__(self, patch):
        self.patch = patch
        self.hooks = []

    def emit(self, s):
        at = (BASE + len(self.patch.image) + 15) & ~15
        assert self.patch.append(_asm(s, at), 16) == at
        return at

    def hook(self, site, s):
        at = self.emit(s)
        self.patch.set_word(site, struct.unpack('<I', _asm(f'b #{at}', site))[0], 'native crop RAW/map consumer')
        self.hooks.append(site)
        return at


class _MapProducer(CropNative):
    """Original constructor/setup with explicit model/config/heap services."""

    def __init__(self, image):
        self.heap = 0x62050000
        self.allocations = []
        super().__init__(image)

    def _hook(self, cpu, at, size, user):
        if at == 0x538E09B0:
            size = cpu.reg_read(reg.UC_ARM_REG_R0)
            pointer = self.heap
            self.heap += (size + 7) & ~7
            self.allocations.append((pointer, size))
            self.uc.mem_write(pointer, bytes(size))
            self._return(pointer)
        elif at == 0x538E09EC:
            self._return()
        elif at in (0x538D094C, 0x538D0928, 0x5386A538, 0x5386A608, 0x538D0EF4):
            self._return({0x538D094C: 5, 0x538D0928: 636, 0x5386A538: 0x62030000, 0x5386A608: 0, 0x538D0EF4: 1}[at])
        else:
            super()._hook(cpu, at, size, user)

    def word(self, a, v):
        self.uc.mem_write(a, struct.pack('<I', v))

    def produce(self):
        setting = 0x62010000
        translator = 0x62038000
        self.uc.mem_write(setting, bytes(0x4000))
        self.word(translator, 0x53FD89A0)
        self.word(0x5539FC84, 1)
        self.word(0x5539FC78, translator)
        self.call(0x5387F154, setting)
        if self.diagnostics:
            raise ValueError('原生 Kb636 参数构造出现诊断')
        self.call(0x5387E4F8, setting)
        if self.diagnostics:
            raise ValueError('原生 Kb636 Map 参数生产出现诊断')
        return [struct.unpack('<I', self.uc.mem_read(setting + o, 4))[0] for o in (4, 12, 16)]


def _factors(row):
    geometry = row.get('geometry', {})
    preview = geometry.get('preview')
    if preview:
        x = Fraction(preview['width'], 45*65536)
        y = Fraction(preview['height'], 30*65536)
    else:
        screen = geometry.get('screen')
        if not screen:
            raise ValueError(f"裁切 {row['identity']['public_id']} 缺少已执行的几何")
        x = Fraction(screen['width'], screen['canvas'][0])
        y = Fraction(screen['height'], screen['canvas'][1])
    if not 0 < x <= 1 or not 0 < y <= 1:
        raise ValueError('裁切参数超出原生中心画布')
    return x, y


def _map_values(records):
    return [
        (r['identity']['public_id'],
         *_factors(r)[0].as_integer_ratio(),
         *_factors(r)[1].as_integer_ratio())
        for r in records if r['identity']['public_id'] >= 4
    ]


def _validate_map(source, records, values):
    native = _MapProducer(source)
    maps = native.produce()
    rows = []
    sro_equal = True
    by_id = {r['identity']['public_id']: r for r in records}
    for variant, m in enumerate(maps):
        sro = struct.unpack('<I', native.uc.mem_read(m + 0xF0, 4))[0]
        for process in range(4):
            for mag in range(3):
                original = struct.unpack('<I', native.uc.mem_read(m + 4 + (process*12 + mag)*4, 4))[0]
                src = struct.unpack('<23H', native.uc.mem_read(original, 46))
                strips = []
                for aspect in range(4):
                    ptr = struct.unpack('<I', native.uc.mem_read(sro + (process*12 + aspect*3 + mag)*4, 4))[0]
                    strips.append(bytes(native.uc.mem_read(ptr, 44)))
                if len(set(strips)) != 1:
                    sro_equal = False
                for identity, xn, xd, yn, yd in values:
                    h = (src[3]*src[5]*xn // xd) // src[5] & ~1
                    v = (src[4]*src[6]*yn // yd) // src[6] & ~1
                    if not h or not v:
                        raise ValueError(f"裁切 public{identity} {by_id[identity]['requested_ratio']} 在 Kb636 selector{variant}/process{process}/倍率档{mag} 的 WB 块尺寸为零 ({h}×{v})；原生最小非零偶数块为2，不能写入")
                    if h > 65535 or v > 65535 or h*src[5] > src[3]*src[5] or v*src[6] > src[4]*src[6]:
                        raise ValueError(f'裁切 public{identity} 的 WB 参数超界')
                    rows.append({'public_id': identity, 'selector': variant, 'process': process, 'crop_mag_index': mag, 'block_size': [h, v], 'block_count': [src[5], src[6]]})
    if not sro_equal:
        raise ValueError('原生 SRO 四比例记录不同，不能共享原3:2物理映射')
    return {
        'native_constructor': '0x5387F154',
        'native_producer': '0x5387E4F8',
        'native_templates': 144,
        'sro_144_factory_records_equal_across_aspects': True,
        'wb_records': rows,
        'environment': 'synthetic allocator; model636/ModelNumber5; config feature queries0; one development strip; no pixels',
    }


def _compile_map(patch, values):
    table = patch.append(b''.join(struct.pack('<5I', *r) for r in values), 16)
    text = (
        f'#define CUSTOM_COUNT {len(values)}\n#define VIEW_BYTES {VIEW_BYTES}\n'
        + (Path(__file__).parent / 'native/crop_image_map.c').read_text(encoding='utf8')
    )
    origin = (BASE + len(patch.image) + 15) & ~15
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-crop-map-') as d:
        d = Path(d)
        (d / 'map.c').write_text(text, encoding='utf8')
        (d / 'map.ld').write_text(
            f'ratios = {table:#x}; SECTIONS {{ . = {origin:#x}; '
            '.text : { *(.text*) *(.rodata*) } /DISCARD/ : '
            '{ *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }',
            encoding='ascii',
        )
        result = subprocess.run([
            str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9',
            '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
            '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
            '-Wl,-T,' + str(d / 'map.ld'), '-Wl,--entry=crop_make_view',
            str(d / 'map.c'), '-o', str(d / 'map.elf'),
        ], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('ImageDevelop 裁切模块编译失败: ' + result.stderr[-1500:])
        subprocess.run([
            str(objcopy), '-O', 'binary', '--only-section=.text',
            str(d / 'map.elf'), str(d / 'map.arm'),
        ], check=True, capture_output=True)
        listing = subprocess.run(
            [str(nm), '-n', str(d / 'map.elf')],
            check=True, capture_output=True, text=True,
        ).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('ImageDevelop 模块有未解析符号')
        symbols = {
            name: int(at, 16)
            for at, kind, name in re.findall(r'^([a-fA-F0-9]+)\s+(\w)\s+(\w+)$', listing, re.M)
            if kind in 'Tt'
        }
        blob = (d / 'map.arm').read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError('ImageDevelop 追加地址变化')
    return symbols, {'address': hex(origin), 'bytes': len(blob), 'sha256': hashlib.sha256(blob).hexdigest(), 'symbols': {k: hex(v) for k, v in symbols.items()}}

def install_image_map(patch, plan):
    source = bytes(patch.image)
    values = _map_values(plan['records'])
    proof = _validate_map(source, plan['records'], values)
    symbols, compiled = _compile_map(patch, values)
    p = _Program(patch)
    # Native primary record zero has its own allocation and release. Extend
    # that allocation instead of the Map object: one caller embeds the Map
    # inside a larger recipe, so changing the Map's 0x190 size is unsafe.
    first_bytes = 48 + len(values) * VIEW_BYTES
    p.hook(0x53877334, 'sub r0,r4,sl;cmp r0,#4;movne r0,#46;' + _imm('ip', first_bytes) + 'moveq r0,ip;b #0x53877338;')
    factory = p.emit('uxtb r0,r0;b #0x538D0898;')
    p.hook(0x538D0894, 'uxtb r0,r0;push {r0-r3,ip,lr};bl #' + str(symbols['crop_identity']) + ';cmp r0,#0;bne custom;pop {r0-r3,ip,lr};b #' + str(factory) + ';custom:add sp,sp,#4;pop {r1-r3,ip,lr};bx lr;')
    def wrapper(site, map_expr, id_expr, dest, normalize=None, index=None, factor=3, post='', bias=False):
        # Preserve R0..R12, LR and caller NZCVQ. The 16-byte local frame holds
        # saved APSR, original Map and public identity. R0/R1 are C arguments;
        # replace only the displaced instruction's destination/index/enum.
        body = 'push {r0-r12,lr};mrs ip,apsr;sub sp,sp,#16;str ip,[sp];' + map_expr + 'str r0,[sp,#4];' + id_expr + 'str r1,[sp,#8];bl #' + str(symbols['crop_make_view']) + ';ldr ip,[sp,#4];cmp r0,ip;'
        if bias:
            body += 'ldrne ip,[sp,#8];subne r0,r0,ip,lsl #2;subne r0,r0,ip,lsl #3;'
        body += 'str r0,[sp,#' + str(16 + dest*4) + '];'
        if normalize is not None:
            body += 'movne ip,#0;' + ('strne ip,[fp,#-0x48];' if normalize == 'stack' else 'strne ip,[sp,#' + str(16 + normalize*4) + '];')
        if index is not None:
            body += 'ldrne ip,[sp,#8];ldrne r0,[sp,#' + str(16 + index*4) + '];'
            body += ('subne r0,r0,ip;subne r0,r0,ip,lsl #1;' if factor == 3 else 'subne r0,r0,ip,lsl #2;subne r0,r0,ip,lsl #3;')
            body += 'strne r0,[sp,#' + str(16 + index*4) + '];'
        body += 'ldr ip,[sp];msr apsr_nzcvq,ip;add sp,sp,#16;pop {r0-r12,lr};' + post + f'b #{site + 4};'
        p.hook(site, body)
    wrapper(0x536FCC8C, 'ldr r0,[r3,#0xa50];', 'mov r1,r4;', 12, normalize = 4)
    wrapper(0x536FEF60, 'ldr r0,[r4,#0xa50];', 'mov r1,r8;', 12, normalize = 8)
    wrapper(0x536FF504, 'ldr r0,[r3,#0xa50];', 'mov r1,sl;', 3, index = 0)
    # r0 is reused for both planes. Bias only the transient map address instead
    # of repeatedly subtracting the public component from the persistent index.
    wrapper(0x537064A0, 'ldr r0,[r4,#0xa1c];', 'ldr r1,[r2,#0x34];ldrb r1,[r1,#0x7da];', 3, bias = True)
    wrapper(0x5370655C, 'ldr r0,[r4,#0xa1c];', 'mov r1,r7;', 2, normalize = 7)
    # Cached command aspect is normalized after plane one. Read source metadata
    # again on every iteration so plane two retains the same custom view.
    wrapper(0x5370A2E4, 'ldr r0,[r4,#0xa1c];', 'ldr r1,[r4,#0xa10];ldr r1,[r1,#0x34];ldrb r1,[r1,#0x7da];', 5, normalize = 'stack')
    wrapper(0x5370A5DC, 'ldr r0,[sb,#0xa1c];', 'ldr r1,[sb,#0xa10];ldr r1,[r1,#0x34];ldrb r1,[r1,#0x7da];', 12, normalize = 4)
    wrapper(0x5370D62C, 'mov r0,r7;', 'ldr r1,[r5,#0x34];ldrb r1,[r1,#0x7da];', 7, index = 0, post = 'add r7,r7,r0,lsl #2;')
    wrapper(0x5370D768, 'mov r0,r5;', 'ldr r1,[r4,#0x34];ldrb r1,[r1,#0x7da];', 5, index = 0, post = 'add r5,r5,r0,lsl #2;')
    wrapper(0x5372AD4C, 'mov r0,r7;', 'mov r1,r8;', 7, index = 3, post = 'add r3,r7,r3,lsl #2;')
    # Run the original deep clone before copying the appended records. C
    # rebuilds each view header from dst so no pointer can reference src.
    clone = p.emit('mov ip,sp;b #0x53877CA0;')
    p.hook(0x53877C9C, 'push {r0-r3,ip,lr};bl #' + str(clone) + ';pop {r0-r3,ip,lr};push {r0-r3,ip,lr};bl #' + str(symbols['crop_clone_tail']) + ';pop {r0-r3,ip,lr};bx lr;')
    return {
        **compiled, **proof,
        'hooks': list(map(hex, p.hooks)),
        'first_record_allocation_bytes': first_bytes,
        'custom_view_bytes_each': VIEW_BYTES,
        'original_map_object_bytes': 400,
        'original_factory_stride_preserved': True,
        'original_first_record_release_preserved': True,
        'clone_rebinds_all_view_pointers': True,
        'heap_capacity_on_camera': 'not_tested',
        'hardware': 'not_tested',
    }


def install_raw(patch, plan):
    source = bytes(patch.image)
    native = CropNative(source)
    active = plan['order']
    if len(active) > 254:
        raise ValueError('RAW byte 数量最多254目标，另留当前图项')
    # RAW codes are transient bytes. Keep factory 1..4, reserve 0 for the
    # unchanged source, and densely assign active custom 5..254. Archived
    # source public identities never depend on the active menu's dense code.
    forward = [0] * 256
    reverse = [0] * 256
    for public, code in FACTORY_CODES.items():
        forward[public] = code
        reverse[code] = public
    for code, public in enumerate([n for n in active if n >= 4], 5):
        forward[public] = code
        reverse[code] = public
    dimensions = {}
    for row in plan['records']:
        public = row['identity']['public_id']
        value = native.dimensions('Kb636', public, 1.0, 0)
        if value['diagnostics']:
            raise ValueError(f'裁切 public{public} 的原生源图几何诊断')
        dimensions[public] = (value['width'], value['height'])
    # Both rectangles are centered on the same native 3:2 canvas. A target
    # must fit both source axes; matching the current shooting list alone
    # would illegally offer wider/taller targets for already cropped RAWs.
    rows = {src: [forward[t] for t in active if dimensions[t][0] <= w and dimensions[t][1] <= h] for src, (w, h) in dimensions.items()}
    for src, expected in {0: [2, 3, 4, 1], 1: [3, 4], 2: [1], 3: [4]}.items():
        if [n for n in rows[src] if n < 5] != expected:
            raise ValueError('RAW 原厂源图包含关系不一致')
    packets = bytearray()
    offsets = []
    for src in range(256):
        offsets.append(len(packets))
        values = rows.get(src, [])
        packets.extend(bytes([len(values), *values]))
    data = patch.append(bytes(packets), 16)
    table = patch.append(struct.pack('<256I', *[data + o for o in offsets]), 16)
    maps = patch.append(bytes(forward) + bytes(reverse), 16)
    p = _Program(patch)
    p.hook(0x5336B768, f'ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r1,[r0,#5];ldrb r0,[r2];cmp r1,#0;addne r0,r0,#1;uxtb r0,r0;bx lr;')
    p.hook(0x5336B8C4, 'ldrb r3,[r0,#5];cmp r3,#0;beq source;cmp r1,#0;moveq r0,#0;bxeq lr;sub r1,r1,#1;uxtb r1,r1;source:ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r3,[r2];cmp r1,r3;movhs r0,#0;bxhs lr;add r2,r2,#1;ldrb r0,[r2,r1];bx lr;')
    p.hook(0x5336D814, 'ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r3,[r2],#1;loop:cmp r3,#0;moveq r0,#0;bxeq lr;ldrb r0,[r2],#1;cmp r0,r1;moveq r0,#1;bxeq lr;sub r3,r3,#1;b loop;')
    p.hook(0x53374D20, 'uxtb r1,r1;' + _imm('r2', maps) + 'ldrb r0,[r2,r1];bx lr;')
    p.hook(0x53375088, 'cmp r1,#0;ldreq r3,[r0,#0x24];ldrbeq r0,[r3,#0x7da];bxeq lr;uxtb r1,r1;' + _imm('r2', maps + 256) + 'ldrb r0,[r2,r1];bx lr;')
    image_map = install_image_map(patch, plan)
    return {
        'hooks': list(map(hex, p.hooks)),
        'active_ids': active,
        'source_rows': rows,
        'source_dimensions': dimensions,
        'forward_codes': {n: forward[n] for n in active},
        'dense_codes_transient_in_raw_model': True,
        'source_public_ids_preserved': True,
        'original_setter': '0x5337525C',
        'original_apply': '0x53375210',
        'target_validation_installed': True,
        'source_dependent_targets': True,
        'maximum_active_custom_software_byte_limit': 250,
        'capacity_basis': 'u8 native RAW count: 254 targets plus one current-image item; camera heap/UI/ISP capacity untested',
        'image_map': image_map,
        'hardware': 'not_tested',
    }
