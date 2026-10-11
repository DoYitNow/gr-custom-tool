"""Generate the nine-field scalar closure template from a user's native image.

The authored ARM dispatch code is assembled as words. The native mutator and
wrapper bodies are read from the imported image and relocated during generation;
no original firmware body, prebuilt candidate or preset data is distributed here.
"""
from __future__ import annotations

import struct

BASE = 0x53000000
FIELD_BYTES = 0x8E0
GROUP_BYTES = 0x210
GETTERS = (0x533B7130, 0x533B7818, 0x533B7F00, 0x533B8928, 0x533B9350,
           0x533B9D78, 0x533BA7A0, 0x533BCDB8, 0x533BD7E0)
SETTERS = (0x533AD4E0, 0x533ADCE0, 0x533AE4E0, 0x533AF100, 0x533AFD20,
           0x533B0940, 0x533B1560, 0x533B3FBC, 0x533B4BDC)
PROPERTY_SETTERS = (0x5375976C, 0x537597A0, 0x537597D4, 0x53759808,
                    0x5375983C, 0x53759870, 0x537598A4, 0x53759A78, 0x53759AAC)


def _branch(at, target, *, condition=14, link=False):
    delta = target - at - 8
    if delta % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise ValueError('ARM 分支目标超出范围')
    return condition << 28 | (0x0B000000 if link else 0x0A000000) | ((delta // 4) & 0xFFFFFF)


def _mov(reg, value, *, high=False):
    return 0xE0000000 | (0x03400000 if high else 0x03000000) | ((value & 0xF000) << 4) | reg << 12 | (value & 0xFFF)


def _native_clone(image, old_start, old_end, new_start, overrides):
    words = []
    for at in range(old_start, old_end, 4):
        word = overrides.get(at)
        if word is None:
            word = struct.unpack_from('<I', image, at - BASE)[0]
            if word & 0x0E000000 == 0x0A000000:
                displacement = word & 0xFFFFFF
                if displacement & 0x800000:
                    displacement -= 0x1000000
                target = at + 8 + displacement * 4
                if old_start <= target < old_end:
                    target = new_start + target - old_start
                word = _branch(new_start + at - old_start, target,
                               condition=word >> 28, link=bool(word & 0x01000000))
        words.append(word)
    return words


def generate_scalar_template(patch, *, start, style=35, main=0x55088604) -> bytes:
    """Return 9 × 0x8e0 bytes with the native scalar group/dispatch layout.

    Per field: four 0x210 groups, setter table at +0x840, setter dispatch at
    +0x850 and getter dispatch at +0x8a0. Entry fallbacks replay the native
    MOV ip,sp prologue and continue at entry+4. The caller installs dispatches
    and replaces static main/mirror lookup sites with its owned state helpers.
    """
    if not 0 <= style <= 255 or start % 4:
        raise ValueError('标量模板样式或起始地址无效')
    blob = bytearray(FIELD_BYTES * 9)
    mirror = main + 36

    def write(at, words):
        data = struct.pack('<%dI' % len(words), *words)
        offset = at - start
        blob[offset:offset + len(data)] = data

    for field in range(9):
        field_start = start + field * FIELD_BYTES
        wrappers = []
        for group in range(4):
            index = field * 4 + group
            callback = field_start + group * GROUP_BYTES
            mutator_body = callback + 0x60
            mutator = callback + 0x140
            wrapper = callback + 0x150
            words = [
                0xE92D4070, 0xE1A04000,
                0xE5D43000 | index, 0xE5D42000 | (36 + index), 0xE1520003,
                0x15C43000 | (36 + index),
                _branch(callback + 24, 0x5323EA84, link=True),
                _branch(callback + 28, 0x5323F018, link=True),
                0xE5D03B1E, 0xE3530000, 0,
                0xE5D03C3E, 0xE3530000 | group, 0,
                0xE5D03000 | (0xC3F + group), 0xE3530000 | style, 0,
                _branch(callback + 68, 0x5372B620, link=True), 0xE5900004,
                0xE5D41000 | (36 + index), 0xE2211004, 0xE8BD4070,
                _branch(callback + 88, PROPERTY_SETTERS[field]), 0xE8BD8070,
            ]
            for pos in (10, 13, 16):
                words[pos] = _branch(callback + pos * 4, callback + 92, condition=1)
            write(callback, words)
            write(mutator_body, _native_clone(patch.image, 0x532B74E8, 0x532B75BC,
                  mutator_body, {0x532B7500: 0xE5D00000 | index,
                                 0x532B751C: 0xE5C51000 | index,
                                 0x532B7534: _mov(2, callback & 65535),
                                 0x532B7538: _mov(2, callback >> 16, high=True),
                                 0x532B75A8: 0xE5D53000 | (36 + index)}))
            write(mutator, [0xE2211004, _mov(0, main & 65535),
                            _mov(0, main >> 16, high=True),
                            _branch(mutator + 12, mutator_body)])
            write(wrapper, _native_clone(patch.image, 0x532B75BC, 0x532B7674,
                  wrapper, {0x532B75F4: _branch(wrapper + 0x38, mutator, link=True)}))
            wrappers.append(wrapper)

        table, setter_at, getter_at = field_start + 0x840, field_start + 0x850, field_start + 0x8A0
        write(table, wrappers)
        setter = [
            0xE3530000 | style, 0, 0xE3510000, 0, 0xE3520003, 0,
            0xE92D4070, 0xE1A05002,
            _branch(setter_at + 32, 0x5323EA84, link=True),
            _branch(setter_at + 36, 0x5323F018, link=True),
            0xE59D1010, _mov(12, table & 65535), _mov(12, table >> 16, high=True),
            0xE79CC105, 0xE12FFF3C, 0xE8BD8070,
            0xE1A0C00D, _branch(setter_at + 68, SETTERS[field] + 4),
        ]
        getter = [
            0xE3530000 | style, 0, 0xE3510000, 0, 0xE3520003, 0,
            _mov(12, (mirror + field * 4) & 65535),
            _mov(12, (mirror + field * 4) >> 16, high=True),
            0xE7DC0002, 0xE2200004, 0xE12FFF1E,
            0xE1A0C00D, _branch(getter_at + 48, GETTERS[field] + 4),
        ]
        for pos, condition in ((1, 1), (3, 1), (5, 8)):
            setter[pos] = _branch(setter_at + pos * 4, setter_at + 64, condition=condition)
            getter[pos] = _branch(getter_at + pos * 4, getter_at + 44, condition=condition)
        write(setter_at, setter)
        write(getter_at, getter)
    return bytes(blob)
