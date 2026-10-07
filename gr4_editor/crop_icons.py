# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Native 60x40 ratio graphics, including their frame and numeric label."""
from decimal import Decimal
import hashlib
import re
import struct
from types import SimpleNamespace

from PIL import Image, ImageDraw, ImageFont

from .crops import parse_ratio
from .firmware import _Memory, _icon_resource
from .demo_build import ICON_CATALOG, _half, _mov

WIDTH, HEIGHT = 60, 40
# Shutdown thumbnails own768..787. Public4..255 need252 identities.
ICON_IDS = (*range(720, 768), *range(788, 992))
FACTORY_ICONS = (241, 349, 347, 240)
LOOKUP_BOUNDS = ((0x5323DBB4, 3), (0x5323E40C, 2))


def ratio_label(value):
    ratio = parse_ratio(value)
    fields = re.split(r'[:：/]', value.strip())
    fields += ['1'] if len(fields) == 1 else []
    label = ':'.join(format(Decimal(field.strip()), 'f').rstrip('0').rstrip('.')
                     if '.' in field else str(int(field.strip())) for field in fields)
    if len(label) <= 9:
        return label
    reduced = f'{ratio.numerator}:{ratio.denominator}'
    return reduced if len(reduced) <= 9 else f'~{float(ratio):.3g}:1'


def render_ratio(value):
    """Follow the factory canvas and colors; keep extreme labels readable."""
    ratio, label = float(parse_ratio(value)), ratio_label(value)
    picture = Image.new('RGBA', (WIDTH, HEIGHT), (255, 255, 255, 0))
    draw = ImageDraw.Draw(picture)
    draw.rectangle((1, 1, 58, 38), fill=(53, 53, 54, 255),
                   outline=(128, 128, 128, 255))
    frame_width, frame_height = (min(52, round(30 * ratio)), 30)
    if frame_width == 52:
        frame_height = max(3, round(52 / ratio))
    frame_width = max(3, frame_width)
    font = None
    for size in range(24, 9, -1):
        candidate = ImageFont.load_default(size=size)
        box = draw.textbbox((0, 0), label, font=candidate)
        if box[2] - box[0] <= frame_width - 6 and box[3] - box[1] <= frame_height - 6:
            font = candidate
            break
    if font is None:
        # Very narrow/tall ratios use a frame above the label, without tiny text.
        frame_width, frame_height = min(52, max(3, round(19 * ratio))), 19
        if frame_width == 52:
            frame_height = max(3, round(52 / ratio))
        center_y = 14
        for size in range(14, 5, -1):
            font = ImageFont.load_default(size=size)
            box = draw.textbbox((0, 0), label, font=font)
            if box[2] - box[0] <= 52:
                break
        label_y = 31
    else:
        center_y = label_y = 20
    left, top = (WIDTH - frame_width) // 2, center_y - frame_height // 2
    draw.rectangle((left, top, left + frame_width - 1, top + frame_height - 1),
                   outline=(255, 255, 255, 255), width=2)
    box = draw.textbbox((0, 0), label, font=font)
    x = (WIDTH - (box[2] - box[0])) // 2 - box[0]
    y = label_y - (box[3] - box[1]) // 2 - box[1]
    draw.text((x, y), label, font=font, fill=(255, 255, 255, 255))
    return picture, label


def install_crop_icons(patch, icon_bytes, plan):
    before = bytes(icon_bytes)
    memory = _Memory(bytes(patch.image))
    originals = [_icon_resource(memory, SimpleNamespace(icon_bytes=before), identity)
                 for identity in FACTORY_ICONS]
    icons, rows = bytearray(before), []
    for row in plan['records']:
        public = row['identity']['public_id']
        if public < 4:
            continue
        identity = ICON_IDS[public - 4]
        if patch.word(ICON_CATALOG + identity * 4):
            raise ValueError('裁切图标目录与已有图形资源冲突')
        picture, label = render_ratio(row['requested_ratio'])
        pixels = picture.tobytes()
        icons.extend(bytes((-len(icons)) % 4))
        offset = len(icons)
        icons.extend(pixels)
        descriptor = patch.append(struct.pack('<HHHHI', 1, WIDTH, HEIGHT, 0, offset // 4), 16)
        patch.set_word(ICON_CATALOG + identity * 4, descriptor, 'dedicated crop ratio icon')
        rows.append({'public_id': public, 'icon_id': identity, 'active': row.get('active', False),
                     'requested_ratio': row['requested_ratio'], 'label': label,
                     'descriptor_address': hex(descriptor), 'offset': offset,
                     'rgba_sha256': hashlib.sha256(pixels).hexdigest()})
    upper = max([0, *[row['icon_id'] for row in rows],
                 *[_half(patch.word(site)) for site, _ in LOOKUP_BOUNDS]])
    for site, register in LOOKUP_BOUNDS:
        word = patch.word(site)
        if word & 0xfff0f000 != 0xe3000000 | (register << 12):
            raise ValueError('裁切图标查表上界不是已验证的 MOVW 指令')
        patch.set_word(site, _mov(register, upper), 'crop icon lookup upper bound')
    return bytes(icons), {'schema_version': 1, 'width': WIDTH, 'height': HEIGHT,
                         'pixel_format': 'RGBA8', 'custom': rows,
                         'input_icon_bytes': len(before),
                         'input_icon_sha256': hashlib.sha256(before).hexdigest(),
                         'added_icon_bytes': len(icons) - len(before),
                         'factory_rgba_sha256': [row['sha256'] for row in originals],
                         'icon_upper_bound': upper, 'hardware': 'not_tested'}


def verify_crop_icons(image, report):
    """Read emitted ARM mapping, catalog descriptors and actual ICONBIN pixels."""
    from .crop_native import CropNative
    memory, native = _Memory(image.rtos), CropNative(image.rtos)
    if hashlib.sha256(image.icon_bytes[:report['input_icon_bytes']]).hexdigest() != report['input_icon_sha256']:
        raise ValueError('裁切图标改变了原有 ICONBIN 像素')
    for public, identity in enumerate(FACTORY_ICONS):
        if native.call(0x5338143C, 0, public, 0) != identity:
            raise ValueError('原厂裁切图标映射被改写')
        resource = _icon_resource(memory, image, identity)
        if resource['sha256'] != report['factory_rgba_sha256'][public]:
            raise ValueError('原厂裁切图标像素被改写')
    for row in report['custom']:
        public, identity = row['public_id'], row['icon_id']
        if native.call(0x5338143C, 0, public, 0) != identity or native.call(0x5338143C, 0, public, 1):
            raise ValueError('裁切专用图标原生映射不一致')
        resource = _icon_resource(memory, image, identity)
        if (resource['width'], resource['height'], resource['flags']) != (WIDTH, HEIGHT, 0) or resource['sha256'] != row['rgba_sha256']:
            raise ValueError('裁切专用图标描述符或像素不一致')
    if any(_half(memory.word(site)) < report['icon_upper_bound'] for site, _ in LOOKUP_BOUNDS):
        raise ValueError('裁切专用图标查表上界不足')
    return {'status': 'native_mapping_and_pixels_passed', 'custom_count': len(report['custom']),
            'factory_preserved': True, 'input_icon_prefix_preserved': True, 'hardware': 'not_tested'}
