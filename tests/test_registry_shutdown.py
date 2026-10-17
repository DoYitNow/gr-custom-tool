# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Registry shutdown builds use the public independent APData contract."""
from contextlib import ExitStack
from copy import deepcopy
import struct
import unittest
from unittest.mock import patch

from gr4_editor import shutdown_compiler, slots


class _InstalledBeforeRaw(Exception):
    pass


class RegistryShutdownTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def mock(name, **kwargs):
            return self.stack.enter_context(patch(name, **kwargs))

        base = bytearray(0xA81000)
        for site in slots.DEFAULT_ENTRIES:
            struct.pack_into('<I', base, site - slots.BASE,
                             slots._branch(site, site + 8))
        self.base = bytes(base)
        self.metadata = {'compiler_icon_size': 4, 'source_note': 'retained'}
        self.plan = {'schema_version': 2, 'presets': [{
            'selection_id': 0x12345678, 'native_id': 12, 'name': 'Custom',
        }], 'legacy_choices': {'1': 0x12345678}}
        origin = slots.BASE + len(base) + 0x100
        self.symbols = {name: origin + index * 16 for index, name in enumerate((
            'registry_slot_save', 'registry_slot_restore', 'registry_slot_reset',
            'registry_slot_copy', 'registry_save', 'registry_load',
            'registry_initialize_owner', 'registry_main'))}
        mock('gr4_editor.slots.KNOWN27_SHA', new=slots.sha(self.base))
        mock('gr4_editor.slots._metadata', side_effect=lambda source: self.metadata)
        mock('gr4_editor.filter_reader.read_filters', return_value=[])
        mock('gr4_editor.slots.plan_registry', return_value={'filters': [], 'next_style': 34})
        mock('gr4_editor.slots._restore_scalar_fallbacks', return_value=[])
        mock('gr4_editor.registry_resources.rebuild_resources',
             return_value=(b'icon', {}))
        mock('gr4_editor.slots._compile_state', return_value=(self.symbols, {
            'symbols': {name: hex(address) for name, address in self.symbols.items()}}))
        self.state = None

        def install_shutdown_state(patch, *, record, choices=None,
                                   legacy_choices=None, resource_ids=(12, 13)):
            # _install_state remains real: capture the actual registry hooks
            # that the public state installer chains, before later crops.
            self.state = {
                'record': record, 'choices': choices, 'legacy_choices': legacy_choices,
                'prior_save': slots._target(0x533E1C04, patch.word(0x533E1C04)),
                'prior_load': slots._target(0x533E17CC, patch.word(0x533E17CC)),
                'symbols': {'shutdown_get': hex(origin + 0x200),
                            'shutdown_set': hex(origin + 0x210),
                            'shutdown_save_global': hex(origin + 0x220)},
            }
            return self.state

        self.install = mock('gr4_editor.shutdown_state.install_shutdown_state',
                            autospec=True, side_effect=install_shutdown_state)
        self.previews = {'custom': [{'selection_id': 0x12345678, 'icon_id': 772}],
                         'factory': [{'native_id': 1, 'icon_id': 768}]}
        mock('gr4_editor.shutdown_preview.install_shutdown_previews',
             return_value=(b'icons with shutdown previews', self.previews))
        mock('gr4_editor.shutdown_compiler.install_jpeg_table', return_value={})
        self.menu = mock('gr4_editor.shutdown_menu.install_shutdown_menu', return_value={})
        self.allocator = mock('gr4_editor.shutdown_compiler.next_record',
                             wraps=shutdown_compiler.next_record)
        from gr4_editor.shutdown_compiler import menu_metadata

        self.report = None

        def capture_report(*args):
            self.report = menu_metadata(*args)
            return self.report

        mock('gr4_editor.shutdown_compiler.menu_metadata', side_effect=capture_report)
        mock('gr4_editor.slots._dynamic_menu_routes', return_value={})
        mock('gr4_editor.slots._identity_registry', return_value={})
        self.raw = mock('gr4_editor.native_raw.migrate_raw_arrays',
                        side_effect=_InstalledBeforeRaw)

    def build_until_raw(self):
        with self.assertRaises(_InstalledBeforeRaw):
            slots.apply_filter_registry(self.base, b'icon', [], [],
                                        shutdown_plan=deepcopy(self.plan),
                                        soft_glow_plan={'enabled': False})

    def test_public_shutdown_chains_registry_without_extending_raw_owner(self):
        self.build_until_raw()
        self.install.assert_called_once()
        self.assertEqual(self.state['record'], 74)
        self.assertEqual(self.state['prior_save'], self.symbols['registry_save'])
        self.assertEqual(self.state['prior_load'], self.symbols['registry_load'])
        self.assertEqual(self.state['choices'], [{'selection_id': 0x12345678, 'native_id': 12}])
        self.assertEqual(self.state['legacy_choices'], {'1': 0x12345678})
        self.assertEqual(self.raw.call_args.kwargs['custom_state_bytes'], 0)
        self.assertEqual(self.raw.call_args.kwargs['init_callback'],
                         self.symbols['registry_initialize_owner'])
        self.assertNotIn('record_migration', self.report)

    def test_known_research_record73_keeps_menu_ids_and_starts_public_record74(self):
        self.metadata['shutdown_menu'] = {'record': 73, 'source_state': 'old owner'}
        original = deepcopy(self.metadata)
        self.build_until_raw()
        normalized = self.allocator.call_args.args[0]
        self.assertEqual(normalized['shutdown_menu'], {'record': 74, 'source_state': 'old owner'})
        self.assertEqual(normalized['source_note'], 'retained')
        self.assertEqual(self.metadata, original)
        self.assertEqual(self.state['record'], 74)
        self.assertEqual(self.report['presets'], self.plan['presets'])
        self.assertEqual(self.report['record_migration'], {
            'source_record': 73, 'record': 74,
            'camera_selection_migrated': False, 'missing_record_default': 0})

    def test_unknown_record_is_rejected_before_state_installation(self):
        self.metadata['shutdown_menu'] = {'record': 75}
        with self.assertRaisesRegex(ValueError, 'APData74'):
            slots.apply_filter_registry(self.base, b'icon', [], [],
                                        shutdown_plan=deepcopy(self.plan),
                                        soft_glow_plan={'enabled': False})
        self.install.assert_not_called()
        self.raw.assert_not_called()


if __name__ == '__main__':
    unittest.main()
