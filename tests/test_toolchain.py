# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Tool selection and ARM artifact checks; real probes require user LLVM."""
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import toolchain as llvm


def elf_header(machine=40, relocation=False):
    raw = bytearray(92 if relocation else 52)
    raw[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HH', raw, 16, 2, machine)
    if relocation:
        struct.pack_into('<I', raw, 32, 52)
        struct.pack_into('<HH', raw, 46, 40, 1)
        struct.pack_into('<I', raw, 56, 9)
        struct.pack_into('<I', raw, 72, 4)
    return bytes(raw)


class ToolchainSelectionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.suffix = '.exe' if os.name == 'nt' else ''
        self.bin = self.root / 'llvm'
        self.bin.mkdir()
        self.tools = tuple(self.touch(self.bin / (name + self.suffix)) for name in ('clang', 'llvm-objcopy', 'llvm-nm', 'ld.lld'))
        environment = patch.dict(os.environ, {'GR4_CLANG': '', 'GR4_LLD': '', 'GR4_EDITOR_DATA_DIR': str(self.root)}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        llvm._LINKERS.clear()

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test executable')
        return path.resolve()

    def test_user_bin_path_and_separate_linker_are_saved_after_preflight(self):
        independent = self.touch(self.root / 'lld' / ('ld.lld' + self.suffix))
        with patch.object(llvm, '_preflight', return_value={'probe_bytes': 12}) as probe:
            result = llvm.save_config(self.root, self.bin, independent)
        self.assertTrue(result['ready'])
        self.assertEqual(probe.call_args.args[0], (*self.tools[:3], independent))
        config = json.loads((self.root / 'toolchain.json').read_text(encoding='utf-8'))
        self.assertEqual(config, {'clang': str(self.tools[0]), 'lld': str(independent)})
        self.assertEqual(llvm.linker_flags(self.tools[0]), ['--ld-path=' + str(independent)])

    def test_environment_overrides_saved_compiler(self):
        (self.root / 'toolchain.json').write_text(json.dumps({'clang': str(self.root / 'missing')}), encoding='utf-8')
        with patch.dict(os.environ, {'GR4_CLANG': str(self.tools[0])}):
            self.assertEqual(llvm._find(self.root), self.tools)

    def test_homebrew_can_supply_lld_from_its_independent_formula(self):
        self.tools[3].unlink()
        homebrew = self.root / 'brew' / 'opt' / 'lld' / 'bin'
        linker = self.touch(homebrew / ('ld.lld' + self.suffix))
        with patch.object(llvm, '_homebrew_bins', return_value=[homebrew]), patch.object(llvm.shutil, 'which', return_value=None):
            self.assertEqual(llvm._find(self.root, clang=self.tools[0])[3], linker)

    def test_missing_optional_toolchain_is_reported_without_data_writes(self):
        with patch.object(llvm, '_candidates', return_value=iter(())):
            result = llvm.status(self.root)
        self.assertFalse(result['ready'])
        self.assertIn('生成固件', result['message'])
        self.assertFalse((self.root / 'toolchain.json').exists())

    def test_failed_real_preflight_does_not_save_configuration(self):
        with patch.object(llvm, '_preflight', side_effect=ValueError('wrong ARM product')):
            with self.assertRaisesRegex(ValueError, 'wrong ARM'):
                llvm.save_config(self.root, self.tools[0])
        self.assertFalse((self.root / 'toolchain.json').exists())

    def test_elf_machine_and_unresolved_relocations_are_verified(self):
        llvm._verify_elf(elf_header())
        with self.assertRaisesRegex(ValueError, 'ARM ELF'):
            llvm._verify_elf(elf_header(machine=62))
        with self.assertRaisesRegex(ValueError, '重定位'):
            llvm._verify_elf(elf_header(relocation=True))


@unittest.skipUnless(os.environ.get('GR4_CLANG'), 'real ARM tests require a user-selected GR4_CLANG')
class ActualArmToolchainTests(unittest.TestCase):
    def test_compile_link_extract_and_symbol_preflight(self):
        result = llvm.status()
        self.assertTrue(result['ready'], result['message'])
        self.assertIn('elf_arm32', result['checks'])
        self.assertIn('no_undefined_symbols', result['checks'])
        self.assertGreater(result['probe_bytes'], 0)

    def test_absolute_branch_condition_call_and_local_label_bytes(self):
        source = 'bne #0x10020;bl #0x10040;b local;mov r0,#0;local:bx lr;'
        expected = struct.pack('<5I', 0x1A000006, 0xEB00000D, 0xEA000000, 0xE3A00000, 0xE12FFF1E)
        self.assertEqual(llvm.assemble_arm(source, 0x10000), expected)
        self.assertEqual(llvm.assemble_arm('old:b #0x10020;', 0x10000), struct.pack('<I', 0xEA000006))

    def test_vfp_conditional_instruction_keeps_original_arm_encoding(self):
        # Instruction word independently checked against the former assembler.
        self.assertEqual(llvm.assemble_arm('vldrne s15,[r0];', 0x10000), struct.pack('<I', 0x1DD07A00))


if __name__ == '__main__':
    unittest.main()
