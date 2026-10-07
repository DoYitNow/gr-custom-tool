# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Find and verify a user-owned ARM LLVM toolchain, only when needed."""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile

from .bootstrap import DATA_ROOT


_FLAGS = ['-target', 'armv7-none-eabi', '-mcpu=cortex-a9', '-marm',
          '-mfloat-abi=soft', '-ffreestanding', '-fno-builtin', '-nostdlib',
          '-fno-unwind-tables', '-fno-asynchronous-unwind-tables']
_LINKERS = {}


def _data_root(root=None):
    return Path(root or os.environ.get('GR4_EDITOR_DATA_DIR') or DATA_ROOT).expanduser().resolve()


def _config(root=None):
    path = _data_root(root) / 'toolchain.json'
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('工具链配置必须为 JSON 对象')
    return value


def _executable(value, name):
    path = Path(value).expanduser()
    if path.is_dir():
        path = path / (name + ('.exe' if os.name == 'nt' else ''))
    if not path.is_file():
        raise ValueError('找不到工具链程序：' + str(path))
    # LLD selects its driver from the executable name; retain ld.lld symlinks.
    return path.parent.resolve() / path.name


def _homebrew_bins():
    if sys.platform != 'darwin':
        return []
    return [Path(prefix) / 'opt' / package / 'bin'
            for prefix in ('/opt/homebrew', '/usr/local') for package in ('llvm', 'lld')]


def _candidates():
    found = shutil.which('clang')
    if found:
        yield Path(found)
    suffix = '.exe' if os.name == 'nt' else ''
    for directory in _homebrew_bins():
        yield directory / ('clang' + suffix)
    host = 'windows-x86_64' if os.name == 'nt' else 'darwin-x86_64' if sys.platform == 'darwin' else 'linux-x86_64'
    roots = []
    for name in ('ANDROID_NDK_HOME', 'ANDROID_NDK_ROOT', 'NDK_HOME'):
        if os.environ.get(name):
            roots.append(Path(os.environ[name]).expanduser())
    for name in ('ANDROID_HOME', 'ANDROID_SDK_ROOT'):
        if os.environ.get(name):
            sdk = Path(os.environ[name]).expanduser()
            roots.extend(sorted((sdk / 'ndk').glob('*'), reverse=True))
            roots.append(sdk / 'ndk-bundle')
    for ndk in roots:
        yield ndk / 'toolchains' / 'llvm' / 'prebuilt' / host / 'bin' / ('clang' + suffix)


def _companion(clang, name):
    suffix = '.exe' if os.name == 'nt' else ''
    choices = [clang.parent / (name + suffix)]
    found = shutil.which(name)
    if found:
        choices.append(Path(found))
    choices.extend(directory / (name + suffix) for directory in _homebrew_bins())
    for path in choices:
        if path.is_file():
            return path.parent.resolve() / path.name
    raise ValueError('工具链缺少 ' + name + '；请安装完整 LLVM/LLD 或 Android NDK')


def _find(root=None, *, clang=None, lld=None):
    config = _config(root)
    explicit = clang or os.environ.get('GR4_CLANG') or config.get('clang')
    linker = lld or os.environ.get('GR4_LLD') or config.get('lld')
    choices = [_executable(explicit, 'clang')] if explicit else _candidates()
    errors = []
    for candidate in choices:
        if not candidate.is_file():
            continue
        try:
            candidate = candidate.resolve()
            tools = (candidate, _companion(candidate, 'llvm-objcopy'), _companion(candidate, 'llvm-nm'))
            linker_path = _executable(linker, 'ld.lld') if linker else _companion(candidate, 'ld.lld')
            return (*tools, linker_path)
        except ValueError as exc:
            if explicit:
                raise
            errors.append(str(exc))
    raise ValueError(errors[-1] if errors else '生成固件需要 ARM LLVM 工具链；请在首次设置选择 clang，可另选 ld.lld，或设置 GR4_CLANG/GR4_LLD')


def _run(command):
    try:
        result = subprocess.run([str(item) for item in command], capture_output=True,
                                text=True, errors='replace', timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('无法运行工具链程序：' + str(exc)) from exc
    if result.returncode:
        raise ValueError('ARM 工具链检查失败：' + (result.stderr or result.stdout)[-1500:])
    return result.stdout


def _verify_elf(raw, *, executable=True):
    if (len(raw) < 52 or raw[:7] != b'\x7fELF\x01\x01\x01'
            or struct.unpack_from('<H', raw, 18)[0] != 40
            or struct.unpack_from('<H', raw, 16)[0] != (2 if executable else 1)):
        raise ValueError('工具链产物不是预期的 32 位小端 ARM ELF')
    if executable:
        offset = struct.unpack_from('<I', raw, 32)[0]
        size, count = struct.unpack_from('<HH', raw, 46)
        for index in range(count):
            row = offset + index * size
            if row + 40 > len(raw):
                raise ValueError('ARM ELF 段表不完整')
            kind, section_size = struct.unpack_from('<I', raw, row + 4)[0], struct.unpack_from('<I', raw, row + 20)[0]
            if kind in (4, 9) and section_size:
                raise ValueError('ARM ELF 仍含重定位记录')


@lru_cache(maxsize=8)
def _preflight_cached(fingerprints):
    clang, objcopy, nm, lld = [Path(row[0]) for row in fingerprints]
    with tempfile.TemporaryDirectory(prefix='firmware-editor-toolchain-') as folder:
        directory = Path(folder)
        source, script, elf, binary = [directory / name for name in ('probe.c', 'probe.ld', 'probe.elf', 'probe.arm')]
        source.write_text('unsigned native_probe(volatile unsigned *value) { return *value + 7u; }\n', encoding='ascii')
        script.write_text('SECTIONS { . = 0x10000; .text : { *(.text*) *(.rodata*) } /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        _run([clang, *_FLAGS, '--ld-path=' + str(lld), '-Os', '-Wl,-T,' + str(script),
              '-Wl,--entry=native_probe', source, '-o', elf])
        _verify_elf(elf.read_bytes())
        if _run([nm, '-u', elf]).strip():
            raise ValueError('ARM 工具链预检含未解析符号')
        symbols = _run([nm, '-n', elf])
        if not re.search(r'^0*10000\s+[Tt]\s+native_probe$', symbols, re.M):
            raise ValueError('ARM 工具链预检的入口符号或链接地址不匹配')
        _run([objcopy, '-O', 'binary', '--only-section=.text', elf, binary])
        blob = binary.read_bytes()
        if not blob or len(blob) % 4:
            raise ValueError('ARM 工具链预检未产生对齐的指令数据')
    version = _run([clang, '--version']).splitlines()[0]
    return {'compiler_version': version, 'target': 'armv7-none-eabi/cortex-a9/ARM/soft-float',
            'probe_bytes': len(blob), 'checks': ['compile', 'link_with_lld', 'elf_arm32', 'no_relocations',
                                             'no_undefined_symbols', 'entry_address', 'objcopy_text']}


def _preflight(paths):
    fingerprints = tuple((str(path), path.stat().st_size, path.stat().st_mtime_ns) for path in paths)
    return _preflight_cached(fingerprints)


def resolve_toolchain(root=None):
    """Return the existing three-tool interface after a real ARM preflight."""
    paths = _find(root)
    _preflight(paths)
    _LINKERS[str(paths[0])] = paths[3]
    return paths[:3]


toolchain = resolve_toolchain


def linker_flags(clang):
    """Use an explicit GNU-flavour LLD path, including a separate Homebrew LLD."""
    path = Path(clang).resolve()
    lld = _LINKERS.get(str(path))
    if lld is None:
        lld = _find(clang=path)[3]
    return ['--ld-path=' + str(lld)]


def assemble_arm(source, address):
    """Assemble ARM code at its actual address, preserving absolute branches.

    Numeric branch destinations are linker symbols, so PC-relative fixups use
    the requested address rather than the assembler's zero-based section.
    """
    clang, objcopy, nm = resolve_toolchain()
    symbols = []
    conditions = '(?:eq|ne|cs|hs|cc|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le|al)?'
    branch = re.compile(r'(^|[;\n])([ \t]*(?:[a-z_.$][\w.$]*:[ \t]*)?)(b(?:l|lx)?' + conditions + r')\s+#?(0x[0-9a-f]+|[0-9]+)(?=\s*(?:[;\n]|$))', re.I)

    def replace(match):
        symbol = 'firmware_target_' + str(len(symbols))
        symbols.append((symbol, int(match.group(4), 0)))
        return match.group(1) + match.group(2) + match.group(3) + ' ' + symbol

    body = branch.sub(replace, source)
    with tempfile.TemporaryDirectory(prefix='firmware-editor-asm-') as folder:
        directory = Path(folder)
        assembly, script, elf, binary = [directory / name for name in ('code.s', 'code.ld', 'code.elf', 'code.arm')]
        assembly.write_text('.syntax unified\n.arm\n.fpu neon\n.text\n.global firmware_asm_entry\nfirmware_asm_entry:\n' + body + '\n', encoding='ascii')
        script.write_text('SECTIONS { . = ' + hex(address) + '; .text : { *(.text*) *(.rodata*) } /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        definitions = ['-Wl,--defsym=' + name + '=' + hex(value) for name, value in symbols]
        _run([clang, *_FLAGS, '-mfpu=neon', *linker_flags(clang), '-Wl,-T,' + str(script),
              '-Wl,--entry=firmware_asm_entry', *definitions, assembly, '-o', elf])
        _verify_elf(elf.read_bytes())
        if _run([nm, '-u', elf]).strip():
            raise ValueError('ARM 汇编仍含未解析符号')
        listing = _run([nm, '-n', elf])
        if 'Thunk_' in listing:
            raise ValueError('ARM 分支目标超出原指令范围')
        _run([objcopy, '-O', 'binary', '--only-section=.text', elf, binary])
        result = binary.read_bytes()
        if len(result) % 4:
            raise ValueError('ARM 汇编产物长度未对齐')
        return result


def status(root=None):
    """Report actual compile/link/extraction readiness without building firmware."""
    try:
        paths = _find(root)
        proof = _preflight(paths)
        _LINKERS[str(paths[0])] = paths[3]
        return {'ready': True, 'message': 'ARM 工具链预检通过', 'clang': str(paths[0]),
                'objcopy': str(paths[1]), 'nm': str(paths[2]), 'lld': str(paths[3]), **proof}
    except (ValueError, OSError) as exc:
        return {'ready': False, 'message': str(exc)}


inspect_toolchain = status


def save_config(root, clang, lld=None):
    """Verify selected executables, then save only this user's tool paths."""
    paths = _find(root, clang=clang, lld=lld)
    proof = _preflight(paths)
    directory = _data_root(root)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'toolchain.json').write_text(json.dumps({'clang': str(paths[0]), 'lld': str(paths[3])}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    _LINKERS[str(paths[0])] = paths[3]
    return {'ready': True, 'message': 'ARM 工具链已配置并通过预检', 'clang': str(paths[0]),
            'objcopy': str(paths[1]), 'nm': str(paths[2]), 'lld': str(paths[3]), **proof}
