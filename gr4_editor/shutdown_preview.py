# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Embed static thumbnails for the existing camera End Screen graphic widget.

The widget uses the existing RGBA8 ICONBIN directory. Its original
520x200 canvas is retained; these thumbnails contain the compiled 720x480 JPEG
on opaque black. This module prepares local bytes and does not access a camera.
"""
from io import BytesIO
from types import SimpleNamespace
import struct

from PIL import Image, ImageOps

from .firmware import _Memory, _icon_resource
from .shutdown import _decode_b64, _jpeg_info, _sha
from .demo_build import ICON_CATALOG, _half, _mov

WIDTH, HEIGHT = 520, 200
FACTORY_NATIVE_IDS = (1, 2, 3, 11)
FACTORY_ICON_FIRST = 768
CUSTOM_ICON_FIRST = 772
MAX_CUSTOM_IMAGES = 16
ICON_LAST = CUSTOM_ICON_FIRST + MAX_CUSTOM_IMAGES - 1
LOOKUP_BOUNDS = ((0x5323DBB4, 3), (0x5323E40C, 2))


def _pixels(row):
    payload = _decode_b64(row.get('payload_base64'), 'shutdown preview JPEG')
    if _sha(payload) != row.get('sha256'):
        raise ValueError('关机预览图片与编译 JPEG SHA256 不一致')
    info = _jpeg_info(payload)
    if not (info['baseline'] and info['mode'] == 'RGB' and
            (info['width'], info['height']) == (720, 480)):
        raise ValueError('关机预览来源必须是 720×480 RGB baseline JPEG')
    with Image.open(BytesIO(payload)) as picture:
        picture.load()
        resized = ImageOps.contain(picture.convert('RGB'), (WIDTH, HEIGHT),
                                   Image.Resampling.LANCZOS)
    canvas = Image.new('RGBA', (WIDTH, HEIGHT), (0, 0, 0, 255))
    canvas.paste(resized, ((WIDTH - resized.width) // 2,
                         (HEIGHT - resized.height) // 2))
    return canvas.tobytes(), _sha(payload)


def install_shutdown_previews(patch, icon_bytes, plan):
    """Append pixels/descriptors in empty crop/End Screen catalog slots.

    Factory previews use 768..771 and custom previews use 772..787.
    """
    factory = plan.get('factory_previews', [])
    custom = plan.get('presets', [])
    if (not isinstance(factory, list) or
            [row.get('native_id') for row in factory] != list(FACTORY_NATIVE_IDS)):
        raise ValueError('关机预览需要四个原厂 JPEG 来源，顺序为 1、2、3、11')
    if not isinstance(custom, list) or len(custom) > MAX_CUSTOM_IMAGES:
        raise ValueError('机内关机预览最多支持 16 个自定义图案')
    selections = [row.get('selection_id') for row in custom]
    if (any(type(choice) is not int or not 0 < choice <= 0xffffffff
            for choice in selections) or len(set(selections)) != len(selections)):
        raise ValueError('关机预览图案必须有唯一稳定 ID')
    if any(patch.word(ICON_CATALOG + identity * 4)
           for identity in range(FACTORY_ICON_FIRST, ICON_LAST + 1)):
        raise ValueError('关机预览目录 768–787 与已有图形资源冲突')

    # Read and retain the existing statistics graphics, rather than assigning
    # new thumbnails over those IDs or changing any existing pixel offsets.
    before_icons = bytes(icon_bytes)
    memory = _Memory(bytes(patch.image))
    originals = []
    for identity in (17, 18):
        resource = _icon_resource(memory, SimpleNamespace(icon_bytes=before_icons), identity)
        originals.append({'icon_id': identity, 'descriptor_address': resource['descriptor_address'],
                          'descriptor_hex': resource['descriptor_hex'],
                          'rgba_sha256': resource['sha256']})
    pictures = [_pixels(row) for row in factory + custom]
    icons = bytearray(before_icons)
    rows, pixel_offsets = [], {}
    for index, (row, (pixels, source_sha)) in enumerate(zip(factory + custom, pictures)):
        identity = FACTORY_ICON_FIRST + index
        pixel_sha = _sha(pixels)
        offset = pixel_offsets.get(pixel_sha)
        if offset is None:
            icons.extend(bytes((-len(icons)) % 4))
            offset = len(icons)
            icons.extend(pixels)
            pixel_offsets[pixel_sha] = offset
        descriptor = patch.append(struct.pack('<HHHHI', 1, WIDTH, HEIGHT, 0, offset // 4), 16)
        patch.set_word(ICON_CATALOG + identity * 4, descriptor,
                       'register shutdown preview icon%d' % identity)
        value = {'native_id': row['native_id'], 'icon_id': identity,
                 'descriptor_address': hex(descriptor), 'offset': offset,
                 'bytes': len(pixels), 'rgba_sha256': pixel_sha,
                 'source_jpeg_sha256': source_sha}
        if index >= len(factory):
            value['selection_id'] = row['selection_id']
        rows.append(value)
    upper = max(row['icon_id'] for row in rows)
    for site, register in LOOKUP_BOUNDS:
        word = patch.word(site)
        if word & 0xfff0f000 != 0xe3000000 | (register << 12):
            raise ValueError('关机预览 ICON 查表上界不是已验证的 MOVW 指令')
        bound = max(_half(word), upper)
        patch.set_word(site, _mov(register, bound), 'shutdown preview icon lookup upper bound')
        upper = max(upper, bound)
    return bytes(icons), {'schema_version': 1, 'width': WIDTH, 'height': HEIGHT,
                         'pixel_format': 'RGBA8', 'fit': 'contain',
                         'picture_rectangle': [110, 0, 300, 200],
                         'factory': rows[:len(factory)], 'custom': rows[len(factory):],
                         'original_statistics': originals, 'icon_upper_bound': upper,
                         'input_icon_bytes': len(before_icons),
                         'input_icon_sha256': _sha(before_icons),
                         'added_icon_bytes': len(icons) - len(before_icons),
                         'unique_preview_pixels': len(pixel_offsets),
                         'hardware': 'not_tested'}


def verify_shutdown_previews(image, report):
    """Read actual catalog descriptors and ICONBIN pixels from a final image."""
    if (report.get('width'), report.get('height'), report.get('pixel_format')) != (WIDTH, HEIGHT, 'RGBA8'):
        raise ValueError('机内关机预览元数据画布无效')
    memory = _Memory(image.rtos)
    if memory.pair(0x533EF604, 0x533EF608) != ICON_CATALOG:
        raise ValueError('机内关机预览 ICON 查表入口不一致')
    original_size = report['input_icon_bytes']
    if _sha(image.icon_bytes[:original_size]) != report['input_icon_sha256']:
        raise ValueError('机内关机预览改变了原有 ICONBIN 像素')
    expected_bounds = []
    for site, register in LOOKUP_BOUNDS:
        word = memory.word(site)
        if word & 0xfff0f000 != 0xe3000000 | (register << 12):
            raise ValueError('机内关机预览 ICON 查表上界指令不一致')
        expected_bounds.append(_half(word))
    rows = report['factory'] + report['custom']
    if any(bound < max(row['icon_id'] for row in rows) for bound in expected_bounds):
        raise ValueError('机内关机预览 ICON 查表上界不足')
    for row in rows:
        resource = _icon_resource(memory, image, row['icon_id'])
        expected = (WIDTH, HEIGHT, 0, row['offset'], row['bytes'], row['rgba_sha256'])
        observed = (resource['width'], resource['height'], resource['flags'],
                    resource['offset'], resource['bytes'], resource['sha256'])
        if observed != expected:
            raise ValueError('机内关机预览图形描述符或像素读回不一致')
    for row in report['original_statistics']:
        resource = _icon_resource(memory, image, row['icon_id'])
        if (resource['descriptor_address'] != row['descriptor_address'] or
                resource['descriptor_hex'] != row['descriptor_hex'] or
                resource['sha256'] != row['rgba_sha256']):
            raise ValueError('机内关机预览改变了原有统计图形')
    return {'status': 'passed', 'factory_count': len(report['factory']),
            'custom_count': len(report['custom']), 'original_statistics_preserved': True,
            'original_icon_pixels_preserved': True, 'hardware': 'not_tested'}
