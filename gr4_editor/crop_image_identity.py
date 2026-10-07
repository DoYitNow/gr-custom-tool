# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Keep archived crop identities through the native photo byte codec/playback."""
import hashlib
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .toolchain import linker_flags

from .demo_build import BASE, _branch, _target, _toolchain


GATES = (
    (0x537F5770, 0x543D3700, 'crop_image_extract_gate'),
    (0x5380D1EC, 0x543D3780, 'crop_image_set_gate'),
    (0x536B6848, 0x543D3800, 'crop_image_playback_gate'),
    (0x536C79E0, 0x543D3940, 'crop_image_aspect_gate'),
)
NATIVE_JOINS = {
    'native_extract_table': 0x537F5774, 'native_extract_unknown': 0x537F5778,
    'native_extract_return': 0x537F5798,
    'native_set_table': 0x5380D1F0, 'native_set_unknown': 0x5380D1F4,
    'native_set_join': 0x5380D20C,
    'native_playback_table': 0x536B684C, 'native_playback_unknown': 0x536B6850,
    'native_playback_join': 0x536B6868,
    'native_aspect_table': 0x536C79E4, 'native_aspect_unknown': 0x536C79E8,
    'native_aspect_return': 0x536C7A00,
    'native_screen_rect_body': 0x536CD890,
    'native_thumbnail_rect_body': 0x536CDA84,
}


def _playback_rectangles(records):
    def signature(row):
        geometry = row.get('geometry', {})
        return tuple(geometry[kind][key] for kind in ('screen', 'thumbnail')
                     for key in ('left', 'top', 'width', 'height'))
    factory = {}
    for row in records:
        if row['identity']['public_id'] >= 4:
            continue
        for size in row.get('geometry', {}).get('photo_sizes', []):
            factory[size['width'], size['height']] = signature(row)
    rectangles = {}
    for row in records:
        public = row['identity']['public_id']
        if public < 4:
            continue
        geometry = row.get('geometry', {})
        if not all(kind in geometry for kind in ('screen', 'thumbnail', 'photo_sizes')):
            continue  # No source-size geometry is inferred for foreign records.
        rect = signature(row)
        for size in geometry['photo_sizes']:
            key = size['width'], size['height']
            if key in factory:
                if rect != factory[key]:
                    raise ValueError('回放相同尺寸对应不同原厂/自定义矩形，无法从尺寸判断: ' + str(key))
                continue  # Factory source dimensions retain the original path.
            prior = rectangles.get(key)
            if prior and tuple(prior['rectangles']) != rect:
                raise ValueError('回放相同尺寸对应不同退休/活动矩形，无法从尺寸判断: ' + str(key))
            if prior:
                prior['archive_public_ids'].append(public)
            else:
                rectangles[key] = {'source_size': list(key), 'rectangles': list(rect),
                                   'archive_public_ids': [public]}
    for row in rectangles.values():
        row['archive_public_ids'] = sorted(set(row['archive_public_ids']))
    return [rectangles[key] for key in sorted(rectangles)]


def install_image_identity(patch, plan):
    """Use all archived identities; never normalize a historical image to active IDs.

    Install after preparing the native base and the geometry/menu modules.
    No native file serializer or MakerNote type/count is replaced.
    """
    for site, prior, symbol in GATES:
        if getattr(patch, 'official_base', False):
            native = {0x537F5770: 0xE3500003, 0x5380D1EC: 0xE3530003,
                      0x536B6848: 0xE35A0003, 0x536C79E0: 0xE3500003}
            valid = patch.word(site) == native[site]
        else:
            valid = _target(site, patch.word(site)) == prior
        if not valid:
            raise ValueError('照片比例身份入口与已识别的原生布局不一致: ' + hex(site))
    if patch.word(0x536CD414) != _branch(0x536CD414, 0x536C79CC, link=True):
        raise ValueError('回放尺寸更新入口与已识别的原生布局不一致')
    if any(patch.word(site) != 0xE1A0C00D for site in (0x536CD88C, 0x536CDA80)):
        raise ValueError('回放尺寸矩形入口与已识别的原生布局不一致')
    known = sorted({row['identity']['public_id'] for row in plan['records']
                    if row['identity']['public_id'] >= 4})
    if any(type(public) is not int or public > 255 for public in known):
        raise ValueError('照片裁切身份超出原生 BYTE1 存储域')
    archive = patch.append(bytes(int(public in known) for public in range(256)), 16)
    rectangles = _playback_rectangles(plan['records'])
    table = patch.append(b''.join(struct.pack('<10I', *row['source_size'], *row['rectangles'])
                                 for row in rectangles), 16)
    source = '#define CROP_REPLAY_RECT_COUNT %d\n' % len(rectangles)
    source += (Path(__file__).parent / 'native/crop_image_identity.c').read_text(encoding='utf-8')
    origin = (BASE + len(patch.image) + 15) & ~15
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-crop-image-') as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = (directory / name for name in
                             ('crop_image.c', 'crop_image.ld', 'crop_image.elf', 'crop_image.arm'))
        c.write_text(source, encoding='utf-8')
        bindings = {'crop_image_archive': archive, 'crop_image_rectangles': table, **NATIVE_JOINS}
        ld.write_text(' '.join(f'{name} = {address:#x};' for name, address in bindings.items()) +
                      f' SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
                      '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        result = subprocess.run([str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9',
                    '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
                    '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
                    '-Wl,-T,' + str(ld), '-Wl,--entry=crop_image_extract_gate', str(c), '-o', str(elf)],
                    capture_output=True, text=True)
        if result.returncode:
            raise ValueError('ARM 照片比例身份模块编译失败: ' + result.stderr[-1500:])
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text',
                        str(elf), str(binary)], check=True, capture_output=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True,
                                 capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('ARM 照片比例身份模块有未解析的符号')
        symbols = {name: int(at, 16) for at, kind, name in
                   re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M)
                   if kind in 'Tt'}
        blob = binary.read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError('照片比例身份模块地址变化')
    change_start = len(patch.changes)
    for site, prior, symbol in GATES:
        patch.set_word(site, _branch(site, symbols[symbol]),
                       'preserve archived photo crop identities and original factory/unknown mapping')
    # The caller has the extracted source image in r7. Do not let its dimension
    # classification overwrite a known custom identity with the nearest factory.
    context = patch.append(lambda at: [0xE1A01007,
                _branch(at + 4, symbols['crop_image_source_aspect'])], 16)
    patch.set_word(0x536CD414, _branch(0x536CD414, context, link=True),
                   'keep known source crop identity during playback dimension update')
    for site, helper in ((0x536CD88C, 'crop_image_screen_rect'),
                         (0x536CDA80, 'crop_image_thumbnail_rect')):
        patch.set_word(site, _branch(site, symbols[helper]),
                       'resolve archived exact source dimensions to their existing playback rectangle')
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('照片比例身份追加区域碰到已知运行数据映射')
    return {'address': hex(origin), 'bytes': len(blob),
            'sha256': hashlib.sha256(blob).hexdigest(),
            'source_c_sha256': hashlib.sha256(source.encode()).hexdigest(),
            'symbols': {name: hex(address) for name, address in symbols.items()},
            'archive_bitmap': hex(archive), 'known_custom_ids': known,
            'playback_rectangle_table': hex(table), 'playback_rectangles': rectangles,
            'dimension_rectangle_policy': 'exact size selects rectangle only; conflicting archive geometry rejected',
            'factory_dimension_path_preserved': True,
            'changes': patch.changes[change_start:],
            'source_image_identity_normalized': False,
            'metadata_storage': {'type': 'BYTE', 'count': 1, 'tag': '0x0080', 'public_bits': 8},
            'factory_public_to_metadata': [1, 0, 2, 3],
            'factory_playdefs_to_public': [0, 2, 1, 3],
            'unknown_native_fallback_preserved': True,
            'limits': ['Native byte setter/getter and playback software paths require offline ARM regression',
                       'Foreign source dimensions retain native factory approximation; no archive identity guessed',
                       'Complete photo serialization, boot, filesystem and camera remain unverified'],
            'hardware': 'not_tested'}
