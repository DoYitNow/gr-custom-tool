# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Extend the existing camera End Screen controller with a named image list.

The native two statistics choices remain separate from the global image state.
The original controller, routing, lifecycle and menu callbacks are reused. Only
the list count, selection helpers and a cloned localized text catalog change.
Calling the compiled routines in Unicorn verifies routing, not an LCD or camera.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .toolchain import linker_flags

from .demo_build import BASE, TEXT_CATALOG, _branch, _mov, _toolchain

# Compatibility defaults for callers creating the former two-slot fixture.
MENU_ROWS = 5
# Crop public IDs 6..255 use text900..1149. Keep this menu beyond that
# complete namespace, including retired crop identities preserved in images.
IMAGE_TEXT_FIRST = 1152
IMAGE_TEXT_IDS = (1152, 1153, 1154)
MAX_CUSTOM_IMAGES = 16
ORIGINAL_TEXT_COUNT = 896
LANGUAGE_NAMES = (
    'Japanese', 'English', 'French', 'German', 'Spanish', 'Portuguese', 'Italian',
    'Dutch', 'Danish', 'Swedish', 'Finnish', 'Polish', 'Czech', 'Hungarian',
    'Turkish', 'Greek', 'Russian', 'Korean', 'Traditional Chinese',
    'Simplified Chinese', 'Thai',
)
# The language indices are read from the supported native catalog. The original
# title and two statistics labels remain their existing localized resources.
LABELS = {
    0: ('標準', 'カスタム1', 'カスタム2'),
    1: ('Original', 'Custom 1', 'Custom 2'),
    18: ('原廠', '自訂1', '自訂2'),
    19: ('原厂', '自定义1', '自定义2'),
}

_SOURCE = r'''
#include <stdint.h>
#define FN(at,type) ((type)(at))
static void *list(void *controller) { return *(void **)((uint8_t *)controller + 0x168); }
static void *userdata(void) {
    return FN(0x5323f018u,void *(*)(void *))(FN(0x5323ea84u,void *(*)(void))());
}
static uint32_t selected(void *controller) {
    return FN(0x53570b6cu,uint32_t (*)(void *))(list(controller));
}
static uint32_t image(void) { return FN(IMAGE_GET,uint32_t (*)(void))(); }
static const uint32_t image_choices[] = IMAGE_CHOICES;
static const uint16_t image_previews[] = IMAGE_PREVIEWS;
#if PREVIEW_ENABLED
static const uint16_t factory_previews[] = FACTORY_PREVIEWS;
static uint32_t factory_preview(void) {
    void *manager = FN(0x5323ea84u,void *(*)(void))();
    uint8_t *node = FN(0x5372b620u,void *(*)(void))();
    node = *(uint8_t **)(node + 8);
    node = *(uint8_t **)(node + 0xc);
    node = *(uint8_t **)(node + 0x20);
    uint32_t variant = *(uint32_t *)(node + 0x48);
    if (variant == 5) return factory_previews[3];
    if (variant != 0) return factory_previews[0];
    void *model = FN(0x5323ff28u,void *(*)(void *))(manager);
    uint32_t edition = FN(0x533e0324u,uint32_t (*)(void *))(model);
    return factory_previews[edition == 1 ? 2 : edition == 2 ? 1 : 0];
}
#endif
static uint32_t label(uint32_t row) {
    if (row < 2) return 6 + row;
    return IMAGE_TEXT_FIRST + row - 2;
}
static void update(void *controller, uint32_t focus) {
    void *widget = list(controller);
    uint32_t statistics = FN(0x5329af24u,uint32_t (*)(void *))(userdata());
    uint32_t choice = image();
    uint32_t image_row = 2;
    for (uint32_t i = 0; i < IMAGE_COUNT; ++i)
        if (image_choices[i] == choice) image_row = 2 + i;
    for (uint32_t row = 0; row < MENU_ROW_COUNT; ++row) {
        void *item = FN(0x53570a9cu,void *(*)(void *,uint32_t))(widget,row);
        uint32_t active = row < 2 ? row == statistics : row == image_row;
        FN(0x5357ad40u,void (*)(void *,uint32_t))(item,active);
        FN(0x5357ade0u,void (*)(void *,uint32_t))(item,label(row));
    }
    if (focus) FN(0x53571788u,void (*)(void *,uint32_t))(widget,image_row);
}
void shutdown_menu_refresh(void *controller) { update(controller,1); }
uint32_t shutdown_menu_confirm(void *controller) {
    uint32_t row = selected(controller);
    if (row >= MENU_ROW_COUNT) return 0;
    if (row < 2) FN(0x5333d364u,uint32_t (*)(void *,uint32_t))(userdata(),row);
    else {
        FN(IMAGE_SET,uint32_t (*)(uint32_t))(image_choices[row - 2]);
        /* Confirmation explicitly updates the independent APData record. The
         * native controller-close event does not itself prove a save trigger. */
        FN(IMAGE_SAVE,uint32_t (*)(void))();
    }
    update(controller,0);
    return 1;
}
void shutdown_menu_left(void *controller, uint32_t wrap) {
    void *widget = list(controller);
    if (!selected(controller) && !wrap) return;
    FN(0x535715b8u,void (*)(void *,int32_t,uint32_t))(widget,-1,1);
}
void shutdown_menu_right(void *controller, uint32_t wrap) {
    void *widget = list(controller);
    if (selected(controller) >= MENU_ROW_COUNT - 1 && !wrap) return;
    FN(0x535715b8u,void (*)(void *,int32_t,uint32_t))(widget,1,1);
}
void shutdown_menu_footer(void *controller) {
    void *graphic = *(void **)((uint8_t *)controller + 0x164);
    uint32_t row = selected(controller);
    uint32_t preview = row < 2 ? 17 + row : 0;
#if PREVIEW_ENABLED
    if (row == 2) preview = factory_preview();
    else if (row >= 3 && row < MENU_ROW_COUNT) preview = image_previews[row - 3];
#endif
    if (preview) FN(0x53576e38u,void (*)(void *,uint32_t))(graphic,preview);
    FN(0x53589ab4u,void (*)(void *,uint32_t))(graphic,preview != 0);
}
'''

HOOKS = {
    'shutdown_menu_refresh': 0x532240E4,
    'shutdown_menu_confirm': 0x53223FA4,
    'shutdown_menu_left': 0x53223F14,
    'shutdown_menu_right': 0x53223F58,
    'shutdown_menu_footer': 0x5322419C,
}


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read(image, address, size):
    offset = address - BASE
    if offset < 0 or offset + size > len(image):
        raise ValueError('关机菜单资源地址不在输入 RTOS 中')
    return bytes(image[offset:offset + size])


def _word(image, address):
    return struct.unpack('<I', _read(image, address, 4))[0]


def _compile(patch, image_get, image_set, image_save, entries, factory_preview_icons):
    clang, objcopy, nm = _toolchain()
    origin = (BASE + len(patch.image) + 15) & ~15
    source = ('#define IMAGE_GET %su\n#define IMAGE_SET %su\n#define IMAGE_SAVE %su\n'
              '#define IMAGE_TEXT_FIRST %du\n'
              '#define MENU_ROW_COUNT %du\n#define IMAGE_COUNT %du\n'
              '#define PREVIEW_ENABLED %du\n#define FACTORY_PREVIEWS {%s}\n'
              '#define IMAGE_PREVIEWS {%s}\n'
              '#define IMAGE_CHOICES {%s}\n'
              % (hex(image_get), hex(image_set), hex(image_save), IMAGE_TEXT_FIRST,
                 3 + len(entries), 1 + len(entries),
                 int(factory_preview_icons is not None),
                 ','.join(str(factory_preview_icons.get(i, 0)) for i in (1,2,3,11))
                 if factory_preview_icons is not None else '0',
                 ','.join(str(row.get('preview_icon_id', 0)) for row in entries) or '0',
                 ','.join(str(value) + 'u' for value in [0] +
                          [row['selection_id'] for row in entries]))) + _SOURCE
    with tempfile.TemporaryDirectory(prefix='gr4-shutdown-menu-') as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = (directory / name for name in ('menu.c','menu.ld','menu.elf','menu.arm'))
        c.write_text(source, encoding='utf-8')
        ld.write_text('SECTIONS { . = %s; .text : { *(.text*) *(.rodata*) } '
                      '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }' % hex(origin), encoding='ascii')
        command = [str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9', '-marm',
                   '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
                   '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
                   '-Wl,-T,' + str(ld), '-Wl,--entry=shutdown_menu_refresh', str(c), '-o', str(elf)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('ARM 关机菜单编译失败: ' + result.stderr[-1500:])
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text', str(elf), str(binary)],
                       check=True, capture_output=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True,
                                 capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('ARM 关机菜单有未解析符号')
        symbols = {name: int(at,16) for at,kind,name in re.findall(
            r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$',listing,re.M) if kind in 'Tt'}
        blob = binary.read_bytes()
    if patch.append(blob,16) != origin:
        raise ValueError('ARM 关机菜单地址变化')
    return symbols, {'address': hex(origin), 'bytes': len(blob), 'sha256': _sha(blob),
                     'source_sha256': _sha(source.encode()),
                     'symbols': {name: hex(at) for name,at in symbols.items()}}


def _install_labels(patch, entries):
    previous = bytes(patch.image)
    rows = []
    for language in range(21):
        old_table = _word(previous,TEXT_CATALOG + language*4)
        old_entries = _read(previous,old_table,ORIGINAL_TEXT_COUNT*4)
        labels = (LABELS.get(language,LABELS[1])[0],) + tuple(row['name'] for row in entries)
        records = []
        for label in labels:
            encoded = (label + '\0').encode('utf-16-le')
            text = patch.append(encoded,4)
            descriptor = patch.append(bytes((len(encoded)//2,1,0,0)) + struct.pack('<I',text),4)
            records.append(descriptor)
        padding = bytes((IMAGE_TEXT_FIRST-ORIGINAL_TEXT_COUNT)*4)
        table = patch.append(old_entries + padding + struct.pack('<%dI' % len(records),*records),16)
        patch.set_word(TEXT_CATALOG + language*4,table,
                       'shutdown language%d cloned catalog' % language)
        rows.append({'language_index': language, 'language': LANGUAGE_NAMES[language],
                     'old_table': hex(old_table), 'table': hex(table), 'labels': list(labels),
                     'fallback_language': None if language in LABELS else 'English',
                     'preserved_entries_sha256': _sha(old_entries)})
    for site in getattr(patch, 'text_bound_sites', (0x543D2BA4,0x543D2C04)):
        patch.set_word(site,_mov(3,IMAGE_TEXT_FIRST+len(entries)), 'shutdown localized text upper bound')
    return {'catalog_entries': IMAGE_TEXT_FIRST+1+len(entries),
            'image_text_ids': list(range(IMAGE_TEXT_FIRST, IMAGE_TEXT_FIRST+1+len(entries))),
            'existing_text_entries_preserved': ORIGINAL_TEXT_COUNT,
            'languages': rows}


def _menu_entries(entries):
    if entries is None:
        entries = [{'selection_id': 1, 'name': 'Custom 1'},
                   {'selection_id': 2, 'name': 'Custom 2'}]
    if not isinstance(entries, (list, tuple)) or len(entries) > MAX_CUSTOM_IMAGES:
        raise ValueError('机内关机列表最多支持 16 项自定义图案')
    result, seen = [], set()
    for row in entries:
        if not isinstance(row, dict):
            raise ValueError('机内关机列表格式错误')
        choice, name = row.get('selection_id'), row.get('name')
        if type(choice) is not int or not 0 < choice <= 0xFFFFFFFF or choice in seen:
            raise ValueError('机内关机图案必须有唯一稳定 ID')
        if not isinstance(name, str) or not name.strip() or any(ord(c) < 32 for c in name):
            raise ValueError('机内关机图案名称不能为空或含控制字符')
        try:
            length = len(name.encode('utf-16-le')) // 2
        except UnicodeEncodeError as error:
            raise ValueError('机内关机图案名称包含无效 Unicode 字符') from error
        if length > 24:
            raise ValueError('机内关机图案名称最多 24 个 UTF-16 单位')
        item = {'selection_id': choice, 'name': name}
        if 'preview_icon_id' in row:
            icon = row['preview_icon_id']
            if type(icon) is not int or not 1 <= icon <= 0xffff:
                raise ValueError('关机缩略图需要有效的原生图形资源编号')
            item['preview_icon_id'] = icon
        result.append(item); seen.add(choice)
    return result


def install_shutdown_menu(patch, image_get: int, image_set: int, image_save: int,
                          entries=None, factory_preview_icons=None) -> dict:
    """Append menu helpers to a demo_build._Patch after the independent state module.

    image_get() returns a normalized stable ID; image_set(choice) updates global state;
    image_save() synchronously updates only the independent APData record.
    The original two statistics options are rows 0/1; factory and custom images follow.
    The installer preserves all 896 existing catalog entries in every language.
    """
    entries = _menu_entries(entries)
    if factory_preview_icons is not None:
        if (not isinstance(factory_preview_icons, dict) or set(factory_preview_icons) != {1,2,3,11}
                or any(type(icon) is not int or not 1 <= icon <= 0xffff
                       for icon in factory_preview_icons.values())
                or any('preview_icon_id' not in row for row in entries)):
            raise ValueError('关机预览需要四张原厂缩略图及每个列表项的图形编号')
    row_count = 3 + len(entries)
    before = bytes(patch.image)
    expected = {0x53223BA4: 0xE3A02002, 0x53223BD4: 0xE54B3034,
                0x532240E4: 0xE1A0C00D, 0x53223FA4: 0xE5900168,
                0x53223F14: 0xE1A0C00D, 0x53223F58: 0xE1A0C00D,
                0x5322419C: 0xE1A0C00D}
    if any(_word(before,site) != word for site,word in expected.items()):
        raise ValueError('输入关机菜单不是已验证的原始五处入口/两项列表')
    for pointer in (image_get,image_set,image_save):
        if type(pointer) is not int or pointer % 4 or not BASE <= pointer < BASE+len(before):
            raise ValueError('关机图案状态入口必须是已追加 RTOS 内的 ARM 函数')
    labels = _install_labels(patch, entries)
    symbols, code = _compile(patch,image_get,image_set,image_save, entries, factory_preview_icons)
    # The native constructor allocates one vector item per supplied type byte.
    # Give it an appended immutable array, rather than overflowing the original
    # two-byte stack buffer when the list grows. Its callback stack is untouched.
    types = patch.append(bytes([21]) * row_count, 4)
    def initializer(at):
        return [_mov(1, types & 65535), _mov(1, types >> 16, high=True),
                _branch(at+8,0x53223BD8)]
    init = patch.append(initializer,16)
    patch.set_word(0x53223BA4,_mov(2, row_count),'shutdown list has statistics, factory and named image rows')
    patch.set_word(0x53223BD4,_branch(0x53223BD4,init),'shutdown initialize dynamic native type21 rows')
    for name,site in HOOKS.items():
        patch.set_word(site,_branch(site,symbols[name]),'shutdown menu ' + name)
    return {'schema_version':2, 'installed':True, 'menu_entry':'existing End Screen / 结束画面',
            'row_count':row_count, 'entries':entries,
            'row_roles':['statistics:0','statistics:1','image:0'] +
                        ['image:%d' % row['selection_id'] for row in entries],
            'image_get':hex(image_get), 'image_set':hex(image_set), 'image_save':hex(image_save),
            'image_confirm_persistence':'explicit independent APData record write; no assumed controller-close commit',
            'statistics_get':hex(0x5329AF24), 'statistics_set':hex(0x5333D364),
            'statistics_parameters_preserved':True, 'image_scope':'global independent state',
            'image_row_footer':('focus shows static ICONBIN thumbnail; OK alone saves image choice'
                                if factory_preview_icons is not None else
                                'existing statistics thumbnail is hidden; no on-camera image preview added'),
            'preview':{'installed':factory_preview_icons is not None,
                       'factory_icon_ids':factory_preview_icons or {},
                       'custom_icon_ids':[row.get('preview_icon_id') for row in entries],
                       'native_widget':hex(0x53576e38),'native_visibility':hex(0x53589ab4),
                       'layout':{'x':100,'y':80,'width':520,'height':200},
                       'statistics_icon_ids':[17,18],
                       'selection_changed_on_focus':False,
                       'verification_scope':'native graphic routing; physical LCD unverified'},
            'labels':labels,'code':code,'initializer':hex(init), 'row_types':hex(types),
            'name_encoding':'UTF-16LE; original camera fonts reused, glyph coverage/display width unverified',
            'hooks':{name:hex(site) for name,site in HOOKS.items()},
            'verification_scope':'compiled ARM menu routines with external widget and manager services replaced in Unicorn; camera display unverified'}
