"""Shared ARM generation accepts explicit 1.20 bindings without private ROM."""
from pathlib import Path
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import slots


class Bindings:
    def resolve(self, address):
        return address + 0x20000


class VersionedStateCompilerTests(unittest.TestCase):
    def test_profile_renders_native_calls_and_links_explicit_original_bodies(self):
        image = slots._Patch(bytes(256))
        rows = [{'identity': {'style_id': 34}}]
        layouts = {34: {'main_offset': 0, 'mirror_offset': 36, 'shadow_offset': 72, 'record': 74}}
        captured = {}

        def command(args, **kwargs):
            if args[0] == 'clang':
                source = next(Path(value) for value in args if str(value).endswith('state.c'))
                captured['source'] = source.read_text(encoding='utf-8')
                captured['linker'] = source.with_suffix('.ld').read_text(encoding='ascii')
            elif args[0] == 'objcopy':
                Path(args[-1]).write_bytes(struct.pack('<I', 0xE12FFF1E))
            return subprocess.CompletedProcess(args, 0,
                stdout='53000100 T registry_load\n53000100 T registry_main\n' if args[0] == 'nm' else '', stderr='')

        with patch.object(slots, '_toolchain', return_value=('clang', 'objcopy', 'nm')), patch.object(
                slots, 'linker_flags', return_value=[]), patch.object(slots.subprocess, 'run', side_effect=command):
            symbols, report = slots._compile_state(image, rows, layouts,
                address_profile=Bindings(), native_overrides={0xC3F: 0xD00},
                original_save_body=0x533F3678, original_load_body=0x533F3258)
        self.assertIn('0x53176140u', captured['source'])
        self.assertIn('0xd00', captured['source'])
        self.assertIn('original_save_body = 0x533f3678', captured['linker'])
        self.assertIn('original_load_body = 0x533f3258', captured['linker'])
        self.assertEqual(symbols['registry_main'], 0x53000100)
        self.assertTrue(report['native_bindings'])

    def test_missing_original_bodies_reject_before_compilation(self):
        image = slots._Patch(bytes(256))
        with patch.object(slots, '_toolchain', return_value=('clang', 'objcopy', 'nm')), patch.object(
                slots.subprocess, 'run') as command:
            with self.assertRaisesRegex(ValueError, 'explicit verified native save/load'):
                slots._compile_state(image, [], {}, address_profile=Bindings())
        command.assert_not_called()

    def test_structural_overrides_require_the_versioned_profile(self):
        with patch.object(slots, '_toolchain', return_value=('clang', 'objcopy', 'nm')):
            with self.assertRaisesRegex(ValueError, 'explicit native address profile'):
                slots._compile_state(slots._Patch(bytes(256)), [], {}, native_overrides={0xC3F: 0xD00})


class VersionedScalarCompilerTests(unittest.TestCase):
    def test_custom_layout_routes_nine_getters_and_setters_to_the_new_closures(self):
        base = slots.BASE
        image = slots._Patch(struct.pack('<I', 0xE1A00000) * (0x1000 // 4))
        getters = tuple(base + 0x100 + index * 4 for index in range(9))
        setters = tuple(base + 0x200 + index * 4 for index in range(9))
        for index, entry in enumerate(getters + setters):
            struct.pack_into('<I', image.image, entry - base, slots._branch(entry, base + 0x500 + index * 4))
        before = bytes(image.image)
        layout = {'getters': getters, 'setters': setters,
                  'property_setters': tuple(base + 0x300 + index * 4 for index in range(9)),
                  'app_get': base + 0x400, 'userdata_get': base + 0x404, 'node_get': base + 0x408,
                  'still_movie': 0x100, 'photo_selector': 0x120, 'photo_styles': 0x124,
                  'mutator_start': base + 0x800, 'wrapper_start': base + 0x8D4,
                  'wrapper_end': base + 0x98C}
        report = slots._clone_scalar(image, 34, base + 0xA00, native_layout=layout)
        self.assertEqual(report['style_id'], 34)
        self.assertTrue(report['previous_style_dispatch_preserved'])
        for entry in getters + setters:
            self.assertGreaterEqual(slots._target(entry, image.word(entry)), base + len(before))
        allowed = {entry - base + offset for entry in getters + setters for offset in range(4)}
        self.assertTrue(all(index in allowed for index, (old, new) in enumerate(zip(before, image.image)) if old != new))


if __name__ == '__main__':
    unittest.main()
