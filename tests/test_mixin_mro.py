"""Both service editions must dispatch version adapters before legacy methods."""
from copy import deepcopy
from pathlib import Path
import sys
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.multiversion import MultiVersionEditorMixin
from gr4_editor.service import Editor, _LegacyEditor


class MultiVersionDispatchTests(unittest.TestCase):
    def test_adapter_methods_are_not_shadowed_by_the_legacy_service(self):
        for name in ('status', 'import_firmware', 'project', 'save', 'build', 'import_project',
                     'add_filter', 'remove_filter', 'add_shutdown_item', 'import_shutdown_image'):
            with self.subTest(method=name):
                self.assertIs(getattr(Editor, name), getattr(MultiVersionEditorMixin, name))
        self.assertLess(Editor.__mro__.index(MultiVersionEditorMixin), Editor.__mro__.index(_LegacyEditor))

    def test_legacy_status_still_runs_under_the_adapter(self):
        editor = Editor.__new__(Editor)
        with patch.object(_LegacyEditor, 'status', return_value={'legacy': True}) as legacy:
            result = editor.status()
        legacy.assert_called_once_with()
        self.assertTrue(result['legacy'])
        self.assertEqual(result['supported_families'], ['1.11', '1.20'])

    def test_disabled_native_effect_is_compatible_with_legacy_positional_save(self):
        editor = Editor.__new__(Editor)
        editor.store = SimpleNamespace(get_project=lambda _: {'input': {'version': '1.11.10.70'}})
        args = ('fixture-project', 'legacy-positional-value')
        with patch.object(_LegacyEditor, 'save', return_value={'legacy': True}) as legacy:
            result = editor.save(*args, native_effects_draft={'schema_version': 1, 'pictorialist': False})
        legacy.assert_called_once_with(*args)
        self.assertTrue(result['legacy'])
        with self.assertRaisesRegex(ValueError, '1.20'):
            editor.save(*args, native_effects_draft={'schema_version': 1, 'pictorialist': True})

    def test_120_native_integer_ids_and_edition_positional_save_preserve_aliases(self):
        editor = Editor.__new__(Editor)
        empty_shutdown = {'schema_version': 2, 'target_id': None, 'selected_asset_id': None,
                          'assets': [], 'items': [], 'selected_item_id': None}
        stored = {'id': 'fixture-project', 'input': {'version': '1.20.10.12'},
                  'firmware': {'editor_supported': True}, 'filters': [{'id': 11, 'kind': 'native'}],
                  'shutdown': deepcopy(empty_shutdown), 'shutdown_inventory': {'resources': []},
                  'native_effects_draft': {'schema_version': 1, 'pictorialist': False},
                  'build_options': {'allow_older': True}, 'title': 'before'}
        editor.store = SimpleNamespace(get_project=lambda _: deepcopy(stored), lock=RLock(),
                                       save_project=lambda row: deepcopy(row))
        editor.calibration = SimpleNamespace(status=lambda: {})
        editor._allocate_version = lambda _: '1.20.10.13'
        editor._caps120 = lambda editable, rows: {'can_build': editable}
        rows = [{'id': 11, 'kind': 'native'}]
        if hasattr(editor, '_filter_rows'):
            result = editor.save(stored['id'], 'after', filters=rows, shutdown=empty_shutdown)
        else:
            result = editor.save(stored['id'], rows, 'after', shutdown_draft=empty_shutdown)
        self.assertEqual(result['title'], 'after')
        self.assertEqual(str(result['filters'][0]['id']), '11')
        self.assertEqual(result['shutdown'], result['shutdown_draft'])
        self.assertFalse(result['native_effects_draft']['pictorialist'])

    def test_new_custom_from_monochrome_template_exposes_custom_scalar_controls(self):
        editor = Editor.__new__(Editor)
        template = {'id': 13, 'kind': 'native', 'identity': {'style_id': 13},
                    'resources': {'banks': [{'hardware_index': n} for n in range(3)]},
                    'parameters': [
                        {'id': 'Saturation', 'native_field_supported': False, 'default': None},
                        {'id': 'Particle', 'native_field_supported': True, 'default': None}]}
        project = {'input': {'version': '1.20.10.11'}, 'filters': [template],
                   'capabilities': {'can_add_filter': True}}
        editor.project = lambda _: deepcopy(project)
        editor.store = SimpleNamespace(new_id=lambda _: 'filter-new', save_project=lambda row: deepcopy(row))
        saved = editor.add_filter('fixture-project', '13', 'New custom')
        parameters = {row['id']: row for row in saved['filters'][-1]['parameters']}
        self.assertTrue(parameters['Saturation']['native_field_supported'])
        self.assertTrue(parameters['Saturation']['parameter_enable_supported'])
        self.assertEqual(parameters['Saturation']['default'], 4)
        self.assertFalse(parameters['Particle']['native_field_supported'])
        self.assertFalse(parameters['Particle']['enabled'])


if __name__ == '__main__':
    unittest.main()
