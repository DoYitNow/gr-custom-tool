"""1.20 RAW recut targets and native AE/WB ImageDevelop views.

Archived source public identities stay independent of transient RAW menu codes.
All native addresses are 1.20; portable ratio/C compilation code is shared.
"""
from __future__ import annotations
from pathlib import Path
from hashlib import sha256
import hashlib
import struct
import re
import subprocess
import tempfile

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm_const as reg

from .crop_raw import _Program as _ArmProgram, _imm, _map_values, _compile_map
from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, _toolchain
from .toolchain import linker_flags

VIEW_BYTES = 400 + 12 * 48
FACTORY_CODES = {0: 2, 1: 3, 2: 1, 3: 4}


class _Program(_ArmProgram):
    def hook(self, site, code):
        baseline = self.patch._crop120_rawbaseline
        if bytes(self.patch.image[site - BASE:site - BASE + 4]) != baseline[site - BASE:site - BASE + 4]:
            raise ValueError(f'1.20 RAW hook was already changed: {site:#x}')
        return super().hook(site, code)


class _MapProducer:
    """Execute original 1.20 ctor/map producer with declared heap/config RAM."""
    def __init__(self, image):
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(BASE, (len(image) + 4095) & ~4095)
        self.uc.mem_write(BASE, bytes(image))
        self.uc.mem_map(0x55000000, 0x400000)
        src, dst, size = struct.unpack_from('<3I', image, 0x450)
        self.uc.mem_write(dst, bytes(image[src - BASE:src - BASE + size]))
        self.uc.mem_map(0x60000000, 0x1000)
        self.uc.mem_map(0x61000000, 0x100000)
        self.uc.mem_map(0x62000000, 0x400000)
        self.uc.reg_write(reg.UC_ARM_REG_FPEXC, 0x40000000)
        self.uc.hook_add(UC_HOOK_CODE, self._hook)
        self.heap, self.allocations, self.diagnostics = 0x62050000, [], []

    def _return(self, value=0):
        self.uc.reg_write(reg.UC_ARM_REG_R0, value)
        self.uc.reg_write(reg.UC_ARM_REG_PC, self.uc.reg_read(reg.UC_ARM_REG_LR))

    def _hook(self, cpu, at, size, unused):
        if at == 0x539084BC:
            count = cpu.reg_read(reg.UC_ARM_REG_R0)
            pointer = self.heap
            self.heap += (count + 7) & ~7
            if self.heap > 0x62400000:
                raise ValueError('Synthetic map allocator exhausted')
            self.uc.mem_write(pointer, bytes(count))
            self.allocations.append((pointer, count))
            self._return(pointer)
        elif at == 0x539084F8:
            self._return()
        elif at == 0x539154F8:
            if not cpu.reg_read(reg.UC_ARM_REG_R0):
                self.diagnostics.append(hex(cpu.reg_read(reg.UC_ARM_REG_LR)))
                raise ValueError('Native map producer assertion failed: ' + self.diagnostics[-1])
            self._return()
        elif at in (0x538F8274, 0x538F8250, 0x53891E48, 0x53891F18,
                     0x538F881C, 0x538ADFA4, 0x53907A84):
            self._return({0x538F8274: 5, 0x538F8250: 636,
                          0x53891E48: 0x62030000, 0x53891F18: 0,
                          0x538F881C: 1, 0x538ADFA4: 0, 0x53907A84: 0}[at])

    def call(self, address, *arguments):
        for register, value in zip((reg.UC_ARM_REG_R0, reg.UC_ARM_REG_R1,
                                    reg.UC_ARM_REG_R2, reg.UC_ARM_REG_R3), arguments):
            self.uc.reg_write(register, value)
        self.uc.reg_write(reg.UC_ARM_REG_SP, 0x610FF000)
        self.uc.reg_write(reg.UC_ARM_REG_LR, 0x60000000)
        self.uc.emu_start(address, 0x60000000, count=5000000)
        if self.uc.reg_read(reg.UC_ARM_REG_PC) != 0x60000000:
            raise ValueError(f'Native map function did not return: {address:#x}')
        return self.uc.reg_read(reg.UC_ARM_REG_R0)

    def word(self, address, value):
        self.uc.mem_write(address, struct.pack('<I', value))

    def produce(self):
        setting, translator = 0x62010000, 0x62038000
        self.uc.mem_write(setting, bytes(0x4000))
        # Original singleton code at 538f7f18 loads guard553a1984 and
        # root553a1964; its model pointer is root+0x14, not a ROM delta.
        self.word(translator, 0x5400F560)
        self.word(0x553A1984, 1)
        self.word(0x553A1978, translator)
        self.call(0x538A6A64, setting)
        self.call(0x538A5E08, setting)
        return [struct.unpack('<I', self.uc.mem_read(setting + offset, 4))[0] for offset in (4, 12, 16)]


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
        'native_constructor': '0x538a6a64',
        'native_producer': '0x538a5e08',
        'native_templates': 144,
        'sro_144_factory_records_equal_across_aspects': True,
        'wb_records': rows,
        'environment': 'synthetic allocator; model636/ModelNumber5; config feature queries0; one development strip; no pixels',
    }



def install_image_map120(patch, plan):
    source = bytes(patch.image)
    values = _map_values(plan['records'])
    proof = _validate_map(source, plan['records'], values)
    symbols, compiled = _compile_map(patch, values)
    p = _Program(patch)
    # Native primary record zero has its own allocation and release. Extend
    # that allocation instead of the Map object: one caller embeds the Map
    # inside a larger recipe, so changing the Map's 0x190 size is unsafe.
    first_bytes = 48 + len(values) * VIEW_BYTES
    p.hook(0x5389ec44, 'sub r0,r4,sl;cmp r0,#4;movne r0,#46;' + _imm('ip', first_bytes) + 'moveq r0,ip;b #0x5389ec48;')
    factory = p.emit('uxtb r0,r0;b #0x538f81c0;')
    p.hook(0x538f81bc, 'uxtb r0,r0;push {r0-r3,ip,lr};bl #' + str(symbols['crop_identity']) + ';cmp r0,#0;bne custom;pop {r0-r3,ip,lr};b #' + str(factory) + ';custom:add sp,sp,#4;pop {r1-r3,ip,lr};bx lr;')
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
    wrapper(0x537215e0, 'ldr r0,[r3,#0xa50];', 'mov r1,r4;', 12, normalize = 4)
    wrapper(0x537238b4, 'ldr r0,[r4,#0xa50];', 'mov r1,r8;', 12, normalize = 8)
    wrapper(0x53723e58, 'ldr r0,[r3,#0xa50];', 'mov r1,sl;', 3, index = 0)
    # r0 is reused for both planes. Bias only the transient map address instead
    # of repeatedly subtracting the public component from the persistent index.
    wrapper(0x5372adfc, 'ldr r0,[r4,#0xa1c];', 'ldr r1,[r2,#0x34];ldrb r1,[r1,#0x7da];', 3, bias = True)
    wrapper(0x5372aeb8, 'ldr r0,[r4,#0xa1c];', 'mov r1,r7;', 2, normalize = 7)
    # Cached command aspect is normalized after plane one. Read source metadata
    # again on every iteration so plane two retains the same custom view.
    wrapper(0x5372ec44, 'ldr r0,[r4,#0xa1c];', 'ldr r1,[r4,#0xa10];ldr r1,[r1,#0x34];ldrb r1,[r1,#0x7da];', 5, normalize = 'stack')
    wrapper(0x5372ef3c, 'ldr r0,[sb,#0xa1c];', 'ldr r1,[sb,#0xa10];ldr r1,[r1,#0x34];ldrb r1,[r1,#0x7da];', 12, normalize = 4)
    wrapper(0x53731f8c, 'mov r0,r7;', 'ldr r1,[r5,#0x34];ldrb r1,[r1,#0x7da];', 7, index = 0, post = 'add r7,r7,r0,lsl #2;')
    wrapper(0x537320c8, 'mov r0,r5;', 'ldr r1,[r4,#0x34];ldrb r1,[r1,#0x7da];', 5, index = 0, post = 'add r5,r5,r0,lsl #2;')
    wrapper(0x5374f734, 'mov r0,r7;', 'mov r1,r8;', 7, index = 3, post = 'add r3,r7,r3,lsl #2;')
    # Run the original deep clone before copying the appended records. C
    # rebuilds each view header from dst so no pointer can reference src.
    clone = p.emit('mov ip,sp;b #0x5389f5b0;')
    p.hook(0x5389f5ac, 'push {r0-r3,ip,lr};bl #' + str(clone) + ';pop {r0-r3,ip,lr};push {r0-r3,ip,lr};bl #' + str(symbols['crop_clone_tail']) + ';pop {r0-r3,ip,lr};bx lr;')
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


def install_raw120(patch, plan, *, baseline_rtos):
    source = bytes(patch.image)
    if sha256(bytes(baseline_rtos)).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('RAW crop requires the exact official1.20 RTOS')
    patch._crop120_rawbaseline = bytes(baseline_rtos)
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
        value = next(value for value in row['geometry']['photo_sizes']
                     if value['model'] == 'Kb636' and value['selector'] == 0 and value['magnification'] == 1.0)
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
    p.hook(0x53379b38, f'ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r1,[r0,#5];ldrb r0,[r2];cmp r1,#0;addne r0,r0,#1;uxtb r0,r0;bx lr;')
    p.hook(0x53379c94, 'ldrb r3,[r0,#5];cmp r3,#0;beq source;cmp r1,#0;moveq r0,#0;bxeq lr;sub r1,r1,#1;uxtb r1,r1;source:ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r3,[r2];cmp r1,r3;movhs r0,#0;bxhs lr;add r2,r2,#1;ldrb r0,[r2,r1];bx lr;')
    p.hook(0x5337bc70, 'ldr r3,[r0,#0x24];ldrb r3,[r3,#0x7da];' + _imm('r2', table) + 'ldr r2,[r2,r3,lsl #2];ldrb r3,[r2],#1;loop:cmp r3,#0;moveq r0,#0;bxeq lr;ldrb r0,[r2],#1;cmp r0,r1;moveq r0,#1;bxeq lr;sub r3,r3,#1;b loop;')
    p.hook(0x533837f4, 'uxtb r1,r1;' + _imm('r2', maps) + 'ldrb r0,[r2,r1];bx lr;')
    p.hook(0x53383b68, 'cmp r1,#0;ldreq r3,[r0,#0x24];ldrbeq r0,[r3,#0x7da];bxeq lr;uxtb r1,r1;' + _imm('r2', maps + 256) + 'ldrb r0,[r2,r1];bx lr;')
    image_map = install_image_map120(patch, plan)
    return {
        'hooks': list(map(hex, p.hooks)),
        'active_ids': active,
        'source_rows': rows,
        'source_dimensions': dimensions,
        'forward_codes': {n: forward[n] for n in active},
        'dense_codes_transient_in_raw_model': True,
        'source_public_ids_preserved': True,
        'original_setter': '0x53383d3c',
        'original_apply': '0x53383cf0',
        'target_validation_installed': True,
        'source_dependent_targets': True,
        'maximum_active_custom_software_byte_limit': 250,
        'capacity_basis': 'u8 native RAW count: 254 targets plus one current-image item; camera heap/UI/ISP capacity untested',
        'image_map': image_map,
        'hardware': 'not_tested',
    }
