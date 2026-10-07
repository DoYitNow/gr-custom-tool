# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Read selected crop consumers from the imported RTOS, using synthetic objects.

Only mutex and diagnostic services are stubbed. This does not execute pixels,
DMA, LCD, ISP, allocation, a JPEG encoder, or camera model detection.
"""
import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
from unicorn import arm_const as reg

BASE = 0x53000000
OBJECT, PIPE, PARAM = 0x62010000, 0x62020000, 0x6202C000
OUTPUT, INPUT, CONFIG = 0x6202D000, 0x6202E000, 0x6202F000
STOP, STACK = 0x620FF000, 0x610FF000


class CropNative:
    def __init__(self, image):
        image = bytes(image)
        self.image = image
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(BASE, (len(image) + 4095) & ~4095)
        self.uc.mem_write(BASE, self.image)
        self.uc.mem_map(0x55000000, 0x400000)
        self.uc.mem_map(0x61000000, 0x100000)
        self.uc.mem_map(0x62000000, 0x100000)
        source, destination, size = struct.unpack_from('<3I', image, 0x450)
        if (source, destination, size) != (0x5437B640, 0x55000000, 0x57480):
            raise ValueError('裁切启动数据布局未知')
        self.uc.mem_write(destination, image[source - BASE:source - BASE + size])
        self.uc.reg_write(reg.UC_ARM_REG_C1_C0_2, 0xF00000)
        self.uc.reg_write(reg.UC_ARM_REG_FPEXC, 0x40000000)
        self.diagnostics = []
        self.mapping = False
        self.public_id = 0
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    def _return(self, value=None):
        if value is not None:
            self.uc.reg_write(reg.UC_ARM_REG_R0, value)
        self.uc.reg_write(reg.UC_ARM_REG_PC, self.uc.reg_read(reg.UC_ARM_REG_LR))

    def _hook(self, cpu, at, size, data):
        if at in (0x53886694, 0x538DFF78):
            self._return(0)
        elif at == 0x538ED930:
            # A failed original assertion invalidates that observed output.
            if not cpu.reg_read(reg.UC_ARM_REG_R0):
                self.diagnostics.append({'function': hex(at), 'line': cpu.reg_read(reg.UC_ARM_REG_R3)})
            self._return()
        elif self.mapping:
            values = {0x5372B620: OBJECT + 0x6000, 0x5375840C: self.public_id,
                      0x5386A538: OBJECT + 0x6100, 0x5386A608: 0}
            if at in values:
                self._return(values[at])

    def call(self, at, *arguments, float_value=None, stack=b''):
        self.diagnostics = []
        for number in range(13):
            self.uc.reg_write(getattr(reg, 'UC_ARM_REG_R' + str(number)), 0)
        self.uc.reg_write(reg.UC_ARM_REG_CPSR, 0x13)
        self.uc.reg_write(reg.UC_ARM_REG_SP, STACK)
        self.uc.reg_write(reg.UC_ARM_REG_LR, STOP)
        self.uc.mem_write(STACK, bytes(64))
        if stack:
            self.uc.mem_write(STACK, stack)
        for number, value in enumerate(arguments):
            self.uc.reg_write(getattr(reg, 'UC_ARM_REG_R' + str(number)), value)
        if float_value is not None:
            self.uc.reg_write(reg.UC_ARM_REG_S0, struct.unpack('<I', struct.pack('<f', float_value))[0])
        self.uc.emu_start(at, STOP, count=100000)
        if self.uc.reg_read(reg.UC_ARM_REG_PC) != STOP:
            raise ValueError('原生裁切函数没有在有限步数内返回')
        return self.uc.reg_read(reg.UC_ARM_REG_R0)

    def preview_id(self, public):
        self.public_id = public
        self.uc.mem_write(OBJECT + 0x6004, struct.pack('<I', OBJECT + 0x6200))
        self.mapping = True
        try:
            return self.call(0x5388C7B8, OBJECT)
        finally:
            self.mapping = False

    def preview(self, internal, scale=8192):
        state = bytearray(0x2600)
        for offset, value in ((4, 2949120), (8, 1966080), (20, 2949120), (24, 1966080), (7380, 1)):
            struct.pack_into('<I', state, offset, value)
        self.uc.mem_write(OBJECT, bytes(state))
        self.call(0x538876B8, OBJECT, 0, internal, scale)
        return dict(zip(('width', 'height', 'left', 'top'), struct.unpack('<4I', self.uc.mem_read(OBJECT + 36, 16))))

    def magnifications(self, model):
        at = {'Kb588': 0x538CF424, 'Kb636': 0x538CFB90}[model]
        result = []
        for index in range(3):
            self.call(at, OBJECT, index)
            result.append(struct.unpack('<f', struct.pack('<I', self.uc.reg_read(reg.UC_ARM_REG_S0)))[0])
        return result

    def dimensions(self, model, aspect, magnification, selector):
        at = {'Kb588': 0x538CF488, 'Kb636': 0x538CFBF8}[model]
        self.uc.mem_write(OUTPUT, b'\xff' * 4)
        self.call(at, OBJECT, selector, aspect, OUTPUT, float_value=magnification, stack=struct.pack('<I', OUTPUT + 2))
        width, height = struct.unpack('<2H', self.uc.mem_read(OUTPUT, 4))
        return {'model': model, 'selector': selector, 'magnification': magnification,
                'width': width, 'height': height, 'diagnostics': list(self.diagnostics)}

    def _factory(self, model):
        vtable = {'Kb588': 0x53FD8920, 'Kb636': 0x53FD89A0}[model]
        self.uc.mem_write(OBJECT + 0x4000, struct.pack('<I', vtable))
        self.uc.mem_write(0x5539FC84, struct.pack('<I', 1))
        self.uc.mem_write(0x5539FC64 + 20, struct.pack('<I', OBJECT + 0x4000))

    def small(self, model, aspect, kind):
        self._factory(model)
        self.uc.mem_write(PIPE, bytes(0x9000))
        self.uc.mem_write(PARAM, bytes(0x1000))
        self.uc.mem_write(PIPE + 32768 + 2632, struct.pack('<I', PARAM))
        self.uc.mem_write(PARAM + 2010, bytes([aspect]))
        w, h, stride = (720, 480, 736) if kind == 'screen' else (160, 120, 160)
        descriptor = bytearray(32)
        struct.pack_into('<III', descriptor, 4, 0x70000000, 0x71000000, 0)
        struct.pack_into('<6H', descriptor, 16, w, h, stride, 0, 0, 0)
        self.uc.mem_write(INPUT, bytes(descriptor))
        self.uc.mem_write(CONFIG, bytes(128))
        if kind == 'screen':
            pointer = self.call(0x536FB244, PIPE, CONFIG, INPUT)
        else:
            pointer = self.call(0x536FB324, PIPE, INPUT)
        width, height, pitch, unused, left, top = struct.unpack_from('<6H', self.uc.mem_read(pointer, 32), 16)
        if (pitch, width + 2 * left, height + 2 * top) != (stride, w, h):
            raise ValueError('原生小图描述符不满足输入画布/stride约束')
        return {'width': width, 'height': height, 'stride': pitch, 'left': left, 'top': top,
                'canvas': [w, h], 'diagnostics': list(self.diagnostics),
                'canvas_source': 'known synthetic descriptor supplied to the imported native consumer',
                'stride_source': 'supplied source stride preserved by native descriptor code; producer not executed'}

    def main_descriptor(self, model, aspect, magnification, selector):
        self._factory(model)
        self.uc.mem_write(PIPE, bytes(0x9000))
        self.uc.mem_write(PARAM, bytes(0x1000))
        self.uc.mem_write(PIPE + 32768 + 2632, struct.pack('<I', PARAM))
        self.uc.mem_write(PARAM + 2010, bytes([aspect]))
        self.uc.mem_write(PARAM + 2012, struct.pack('<f', magnification))
        self.uc.mem_write(PARAM + 2016, bytes([selector]))
        planes = (0x70000000, 0x71000000)
        self.uc.mem_write(PIPE + 32768 + 2640, struct.pack('<2I', *planes))
        self.uc.mem_write(OUTPUT, bytes(32))
        self.call(0x536FB0DC, PIPE, OUTPUT, 0, 1, stack=bytes(8))
        descriptor = self.uc.mem_read(OUTPUT, 32)
        if struct.unpack_from('<2I', descriptor, 4) != planes:
            raise ValueError('主图描述符平面地址没有沿原路径保留')
        width, height = struct.unpack_from('<2H', descriptor, 16)
        return {'width': width, 'height': height, 'diagnostics': list(self.diagnostics),
                'scope': 'existing-plane descriptor branch; no allocation or pixel DMA'}

    def dng_window(self, model, aspect, magnification):
        self._factory(model)
        at, origin = {'Kb588': (0x5387B13C, [68, 40]), 'Kb636': (0x5387E220, [54, 58])}[model]
        self.uc.mem_write(OUTPUT, b'\xff' * 8)
        self.call(at, OBJECT, OUTPUT, OUTPUT + 2, OUTPUT + 4, float_value=magnification,
                  stack=struct.pack('<3I', OUTPUT + 6, aspect, 0))
        window = dict(zip(('width', 'height', 'left', 'top'), struct.unpack('<4H', self.uc.mem_read(OUTPUT, 8))))
        return {**window, 'model': model, 'magnification': magnification, 'sensor_origin': origin,
                'diagnostics': list(self.diagnostics), 'scope': 'native crop window; DNG serialization not executed'}

    def raw_options(self, aspect, current):
        self.uc.mem_write(OBJECT, bytes(0x100))
        self.uc.mem_write(OBJECT + 0x24, struct.pack('<I', OBJECT + 0x7000))
        self.uc.mem_write(OBJECT + 5, bytes([int(current)]))
        self.uc.mem_write(OBJECT + 0x7000 + 0x7DA, bytes([aspect]))
        count = self.call(0x5336B768, OBJECT)
        diagnostic = list(self.diagnostics)
        options = []
        if not diagnostic:
            for index in range(count):
                options.append(self.call(0x5336B8C4, OBJECT, index))
                diagnostic.extend(self.diagnostics)
        return {'current_image_option': current, 'count': count, 'internal_ids': options, 'diagnostics': diagnostic}

    def fit_roi(self, source, target):
        self.uc.mem_write(OUTPUT, bytes(16))
        self.uc.mem_write(INPUT, struct.pack('<2I', *source))
        self.uc.mem_write(CONFIG, struct.pack('<2I', *target))
        self.call(0x5369642C, OUTPUT, INPUT, CONFIG)
        return list(struct.unpack('<4I', self.uc.mem_read(OUTPUT, 16)))
