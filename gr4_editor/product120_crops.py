"""Current 1.20 crop inventory and proposals, read from native ARM consumers.

Only known service boundaries are fixtures. No camera, pixels, or DMA run.
"""
from copy import deepcopy
from fractions import Fraction
import struct

from .crop_native import CropNative, OBJECT, PIPE, PARAM, INPUT, OUTPUT, CONFIG, STOP, STACK
from .crops import _aligned_crop, parse_ratio, ratio_text


class CropNative120(CropNative):
    def __init__(self, image):
        from unicorn import Uc, UC_ARCH_ARM, UC_MODE_ARM, UC_HOOK_CODE
        from unicorn import arm_const as reg
        self.image = bytes(image)
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_ARM)
        self.uc.mem_map(0x53000000, (len(image) + 4095) & ~4095)
        self.uc.mem_write(0x53000000, self.image)
        self.uc.mem_map(0x55000000, 0x400000)
        self.uc.mem_map(0x61000000, 0x100000)
        self.uc.mem_map(0x62000000, 0x100000)
        src, dst, size = struct.unpack_from('<3I', image, 0x450)
        if (src, dst, size) != (0x543B3100, 0x55000000, 0x58500):
            raise ValueError('未知 1.20 裁切初始化布局')
        self.uc.mem_write(dst, image[src - 0x53000000:src - 0x53000000 + size])
        self.uc.reg_write(reg.UC_ARM_REG_C1_C0_2, 0xF00000)
        self.uc.reg_write(reg.UC_ARM_REG_FPEXC, 0x40000000)
        self.diagnostics, self.mapping, self.public_id = [], False, 0
        self.uc.hook_add(UC_HOOK_CODE, self._hook)

    def _hook(self, cpu, at, size, data):
        from unicorn import arm_const as reg
        if at in (0x538ADFA4, 0x53907A84):
            self._return(0)
        elif at == 0x539154F8:
            if not cpu.reg_read(reg.UC_ARM_REG_R0):
                self.diagnostics.append({'function': hex(at), 'line': cpu.reg_read(reg.UC_ARM_REG_R3)})
            self._return()

    def preview(self, public, scale=8192):
        state = bytearray(0x2600)
        for offset, value in ((4, 45 << 16), (8, 30 << 16), (20, 45 << 16), (24, 30 << 16), (0x1CD4, 1)):
            struct.pack_into('<I', state, offset, value)
        self.uc.mem_write(OBJECT, bytes(state))
        self.call(0x538AEFC8, OBJECT, 0, public, scale)
        return dict(zip(('width', 'height', 'left', 'top'), struct.unpack('<4I', self.uc.mem_read(OBJECT + 36, 16))))

    def magnifications(self, model):
        from unicorn import arm_const as reg
        at = {'Kb588': 0x538F6D4C, 'Kb636': 0x538F74B8}[model]
        result = []
        for index in range(3):
            self.call(at, OBJECT, index)
            result.append(struct.unpack('<f', struct.pack('<I', self.uc.reg_read(reg.UC_ARM_REG_S0)))[0])
        return result

    def dimensions(self, model, aspect, magnification, selector):
        at = {'Kb588': 0x538F6DB0, 'Kb636': 0x538F7520}[model]
        self.uc.mem_write(OUTPUT, b'\xff' * 4)
        self.call(at, OBJECT, selector, aspect, OUTPUT, float_value=magnification, stack=struct.pack('<I', OUTPUT + 2))
        width, height = struct.unpack('<2H', self.uc.mem_read(OUTPUT, 4))
        return dict(model=model, selector=selector, magnification=magnification,
                    width=width, height=height, diagnostics=list(self.diagnostics))

    def small(self, model, aspect, kind):
        # Original native geometry takes its output canvas from this descriptor.
        self.uc.mem_write(OBJECT + 0x4000, struct.pack('<I', {'Kb588':0x5400F4E0,'Kb636':0x5400F560}[model]))
        self.uc.mem_write(0x553A1984, struct.pack('<I', 1))
        self.uc.mem_write(0x553A1964 + 20, struct.pack('<I', OBJECT + 0x4000))
        self.uc.mem_write(PIPE, bytes(0x9000))
        self.uc.mem_write(PARAM, bytes(0x1000))
        self.uc.mem_write(PIPE + 0x8000 + 0xA48, struct.pack('<I', PARAM))
        self.uc.mem_write(PARAM + 0x7DA, bytes([aspect]))
        w, h, stride = (720, 480, 736) if kind == 'screen' else (160, 120, 160)
        descriptor = bytearray(32)
        struct.pack_into('<III', descriptor, 4, 0x70000000, 0x71000000, 0)
        struct.pack_into('<6H', descriptor, 16, w, h, stride, 0, 0, 0)
        self.uc.mem_write(INPUT, bytes(descriptor))
        self.uc.mem_write(CONFIG, bytes(128))
        if kind == 'screen':
            ptr = self.call(0x5371FB98, PIPE, CONFIG, INPUT)
        else:
            ptr = self.call(0x5371FC78, PIPE, INPUT)
        width, height, pitch, _, left, top = struct.unpack_from('<6H', self.uc.mem_read(ptr, 32), 16)
        if (pitch, width + 2 * left, height + 2 * top) != (stride, w, h):
            raise ValueError('1.20 小图尺寸/画布/stride 不一致')
        return dict(width=width, height=height, stride=pitch, left=left, top=top, canvas=[w, h], diagnostics=list(self.diagnostics))


def _photo_sizes(native, public):
    result = []
    for model in ('Kb588', 'Kb636'):
        for selector in range(4):
            for mag in native.magnifications(model):
                item = native.dimensions(model, public, mag, selector)
                if item['diagnostics'] or not item['width'] or not item['height']:
                    raise ValueError('1.20 原生照片尺寸进入诊断路径')
                result.append(item)
    return result


def factory_crops120(rtos):
    from .firmware import _Memory
    from .product120_resources import text120
    native, memory, rows = CropNative120(rtos), _Memory(rtos), []
    for public, nameid, ratio in ((0, 281, '3:2'), (1, 282, '4:3'), (3, 284, '1:1'), (2, 280, '16:9')):
        geometry = {'screen': native.small('Kb636', public, 'screen'),
                    'thumbnail': native.small('Kb636', public, 'thumbnail'),
                    'photo_sizes': _photo_sizes(native, public)}
        # Factory preview enum and public aspect enum are distinct.
        preview_id = {0:0,1:2,3:3,2:1}[public]
        geometry['preview'] = native.preview(preview_id)
        rows.append({'id': 'crop-'+str(public), 'kind': 'factory', 'name': text120(memory, nameid)[0],
            'identity': {'public_id': public, 'preview_id': preview_id, 'text_id': nameid},
            'requested_ratio': ratio, 'actual_ratio': ratio,
            'geometry': geometry, 'editable': False, 'active': True})
    return rows


def preview_geometry120(rtos, ratio, *, geometry_sources=None):
    requested = parse_ratio(ratio)
    source = next((r for r in (geometry_sources or []) if r.get('geometry') and
        r.get('identity', {}).get('public_id', 256) <= 5 and parse_ratio(r['requested_ratio']) == requested), None)
    if source:
        geometry = deepcopy(source['geometry'])
    else:
        native = CropNative120(rtos)
        screen = {**_aligned_crop(720, 480, requested), 'stride': 736}
        actual = Fraction(screen['width'], screen['height'])
        geometry = {'screen': screen, 'thumbnail': {**_aligned_crop(160, 120, actual), 'stride': 160}, 'photo_sizes': []}
        for item in _photo_sizes(native, 0):
            region = _aligned_crop(item['width'], item['height'], actual)
            geometry['photo_sizes'].append({**item, **region})
        geometry['preview'] = {'width': (45 << 16) * screen['width'] // 720,
            'height': (30 << 16) * screen['height'] // 480,
            'left': (45 << 16) * screen['left'] // 720,
            'top': (30 << 16) * screen['top'] // 480}
    screen = geometry['screen']
    actual = Fraction(screen['width'], screen['height'])
    return {'geometry': geometry, 'requested_ratio': ratio,
            'actual_ratio': ratio_text(actual.numerator, actual.denominator),
            'relative_error_percent': float(actual / requested - 1) * 100,
            'alignment_changed_ratio': actual != requested, 'status': 'geometry_draft',
            'native_write_supported': True, 'hardware': 'not_tested'}


def verify_crops120(rtos, records):
    native = CropNative120(rtos)
    count = 0
    for row in records:
        public = row['identity']['public_id']
        if public >= 4 and native.preview(public) != row['geometry']['preview']:
            raise ValueError('1.20 裁切预览几何与配方不一致: ' + row['name'])
        for wanted in row['geometry']['photo_sizes']:
            actual = native.dimensions(wanted['model'], public, wanted['magnification'], wanted['selector'])
            if (actual['width'], actual['height']) != (wanted['width'], wanted['height']) or actual['diagnostics']:
                raise ValueError('1.20 裁切照片尺寸与配方不一致: ' + row['name'])
            count += 1
    return {'status': 'passed', 'native_dimension_cases': count, 'hardware': 'not_tested'}
