# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Compile the global image selector while chaining current persistence hooks.

The eight-byte choice uses APData74, separate from native records0..15 and prior
research records16..73. No RAW owner or UserMode snapshot is extended.
"""
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile

from .toolchain import linker_flags

from .demo_build import BASE, _branch, _mov, _target, _toolchain

RECORD_BYTES = 8
STATE_MAGIC = 0x32424753
LEGACY_STATE_MAGIC = 0x31424753
SAVE_ENTRY = 0x533E1C04
LOAD_ENTRY = 0x533E17CC
BACKGROUND_ENTRY = 0x531B9488
UPPER_ENTRY = 0x53A80ADC


def _existing_upper_bound(patch):
    """Read either the factory STR r9 (upper 16) or a registry-bound stub."""
    word = patch.word(UPPER_ENTRY)
    if word == 0xE58D9000:
        return 16
    address = _target(UPPER_ENTRY, word)
    word = patch.word(address)
    # MOVW r3 has its upper immediate nibble in instruction bits 19..16.
    if word & 0xFFF0F000 != 0xE3003000:
        raise ValueError('APData 上界不是已知注册表 stub')
    if (patch.word(address + 4) != 0xE58D3000 or
            _target(address + 8, patch.word(address + 8)) != 0x53A80AE0):
        raise ValueError('APData 上界 stub 返回入口未知')
    return (word & 0xFFF) | ((word >> 4) & 0xF000)


def _choice_table(choices, resource_ids, legacy_choices):
    if choices is None:
        choices = [{'selection_id': i + 1, 'native_id': value}
                   for i, value in enumerate(resource_ids)]
    if not isinstance(choices, (list, tuple)) or len(choices) > 16:
        raise ValueError('关机图案列表最多支持 16 项自定义图案')
    rows, ids, resources = [], set(), set()
    for row in choices:
        if not isinstance(row, dict):
            raise ValueError('关机图案选择表格式错误')
        choice, resource = row.get('selection_id'), row.get('native_id')
        if (type(choice) is not int or not 0 < choice <= 0xFFFFFFFF or choice in ids or
                type(resource) is not int or not 12 <= resource < 256 or resource in resources):
            raise ValueError('关机图案必须有独立稳定 ID 和新增 JPEG 资源 ID')
        rows.append({'selection_id': choice, 'native_id': resource})
        ids.add(choice); resources.add(resource)
    if legacy_choices is not None and not isinstance(legacy_choices, dict):
        raise ValueError('旧关机槽位迁移表格式错误')
    legacy = {}
    for key, value in (legacy_choices or {}).items():
        if str(key) not in ('1', '2') or type(value) is not int or not 0 < value <= 0xFFFFFFFF:
            raise ValueError('旧关机槽位迁移必须指向有效的稳定图案 ID')
        legacy[int(key)] = value
    return rows, legacy


def install_shutdown_state(patch, *, record, choices=None,
                           legacy_choices=None, resource_ids=(12, 13)):
    """Install background and chained global save/load; return ARM symbols.

    The previous save/load targets are captured before these hooks are replaced,
    so native or crop wrappers execute exactly once. The menu confirmation
    writes the independent choice through the existing native Domain API.
    """
    if record != 74:
        raise ValueError('Demo 关机选择必须使用独立 APData74')
    choices, legacy = _choice_table(choices, resource_ids, legacy_choices)
    if patch.word(BACKGROUND_ENTRY) != 0xE1A0C00D:
        raise ValueError('关机背景入口不符合已适配的原生函数布局')
    prior_save = _target(SAVE_ENTRY, patch.word(SAVE_ENTRY))
    prior_load = _target(LOAD_ENTRY, patch.word(LOAD_ENTRY))
    upper_bound = max(_existing_upper_bound(patch), record + 1)
    body = bytes(patch.image[BACKGROUND_ENTRY + 4 - BASE:0x531B9538 - BASE])
    pairs = ['{0u,0u}'] + ['{%du,%du}' % (row['selection_id'], row['native_id'])
                          for row in choices]
    source = ('#define SHUTDOWN_RECORD %du\n'
              '#define SHUTDOWN_CHOICES {%s}\n'
              '#define SHUTDOWN_CHOICE_COUNT %du\n'
              '#define SHUTDOWN_LEGACY_1 %du\n'
              '#define SHUTDOWN_LEGACY_2 %du\n' %
              (record, ','.join(pairs), len(pairs),
               legacy.get(1, 0), legacy.get(2, 0)))
    source += (Path(__file__).parent / 'native/shutdown_state.c').read_text(encoding='utf-8')
    origin = (BASE + len(patch.image) + 15) & ~15
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-shutdown-state-') as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = (directory / name for name in
                             ('shutdown.c', 'shutdown.ld', 'shutdown.elf', 'shutdown.arm'))
        c.write_text(source, encoding='utf-8')
        ld.write_text(
            f'prior_shutdown_save = {prior_save:#x}; prior_shutdown_load = {prior_load:#x}; '
            'original_shutdown_background_body = 0x531B948C; '
            f'SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
            '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }',
            encoding='ascii')
        command = [str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi', '-mcpu=cortex-a9',
                   '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
                   '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib',
                   '-Wl,-T,' + str(ld), '-Wl,--entry=shutdown_get', str(c), '-o', str(elf)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError('ARM 关机选择状态模块编译失败: ' + result.stderr[-1500:])
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text',
                        str(elf), str(binary)], check=True, capture_output=True)
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True,
                                 capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M):
            raise ValueError('ARM 关机选择状态模块有未解析符号')
        symbols = {name: int(at, 16) for at, kind, name in
                   re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M)
                   if kind in 'Tt'}
        blob = binary.read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError('ARM 关机选择状态追加地址变化')
    change_start = len(patch.changes)
    patch.set_word(SAVE_ENTRY, _branch(SAVE_ENTRY, symbols['shutdown_state_save']),
                   'retain native/crop save, then independent shutdown choice')
    patch.set_word(LOAD_ENTRY, _branch(LOAD_ENTRY, symbols['shutdown_state_load']),
                   'retain native/crop load, then independent shutdown choice')
    patch.set_word(BACKGROUND_ENTRY, _branch(BACKGROUND_ENTRY, symbols['shutdown_request_background']),
                   'global static background choice; preserve original variant/edition selector')
    upper = patch.append(lambda at: [_mov(3, upper_bound), 0xE58D3000,
                                    _branch(at + 8, 0x53A80AE0)], 16)
    patch.set_word(UPPER_ENTRY, _branch(UPPER_ENTRY, upper),
                   'APData upper bound includes independent shutdown choice record')
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('关机状态追加区域碰到已知运行数据映射')
    return {
        'address': hex(origin), 'bytes': len(blob),
        'sha256': hashlib.sha256(blob).hexdigest(),
        'source_c_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'symbols': {name: hex(at) for name, at in symbols.items()},
        'record': record, 'record_bytes': RECORD_BYTES,
        'storage': 'independent native APData record; no RAW owner extension',
        'records_without_demo_writes': '0..73', 'native_domain_exclusive_upper_before': 16,
        'prior_save': hex(prior_save), 'prior_load': hex(prior_load),
        'schema_version': 2, 'state_magic': 'SGB2',
        'apdata_upper_bound': upper_bound,
        'resource_ids': [row['native_id'] for row in choices],
        'choices': choices, 'legacy_choices': {str(key): value for key, value in legacy.items()},
        'choice_values': {'0': 'factory variant/edition', **{
            str(row['selection_id']): 'custom JPEG %d' % row['native_id'] for row in choices}},
        'scope': 'global; excluded from UserMode snapshots',
        'invalid_or_missing_choice_fallback': 0,
        'original_background_body_sha256': hashlib.sha256(body).hexdigest(),
        'original_statistics_field_preserved': True,
        'changes': patch.changes[change_start:],
        'hardware': 'not_tested',
        'limits': ['源码接入候选；未运行本 Demo 的 ARM、实机或断电持久化验证'],
    }
