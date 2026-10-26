"""Project recipes retain photo identities across builds and backup-only restore.

Native compilation and container decoding are replaced at their boundaries;
the real service, content store, JSON backup, version allocator and save paths
run normally. Separate pure planning checks exercise the actual ID allocator.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import product120
from gr4_editor.service import Editor
from gr4_editor.store import Store, sha256


def native_filter():
    return {'id': 11, 'kind': 'native', 'name': 'Standard', 'enabled': True,
            'identity': {'style_id': 11}, 'resources': {'banks': [{'fixture': 'native'}]},
            'parameters': []}


def custom_filter(name, style=None):
    identity = {} if style is None else {'style_id': style, 'profile_id': style + 4,
        'text_id': style + 847, 'icon_id': style + 675, 'maker_note_code': 0x8200 + style}
    return {'id': name, 'kind': 'custom', 'name': name, 'enabled': True,
            'editable': True, 'identity': identity, 'template_id': 11,
            'resources': {'banks': [{'fixture': name}]}, 'parameters': []}


def crop(name, public, *, custom=True, active=True):
    return {'id': name, 'kind': 'custom' if custom else 'factory', 'name': name,
            'identity': {'public_id': public, 'internal_id': public},
            'requested_ratio': '2.35:1' if custom else '3:2',
            'actual_ratio': '2.35:1' if custom else '3:2', 'active': active,
            'geometry': {'screen': {'width': 720, 'height': 304 if custom else 480}}}


def make_editor(folder):
    editor = Editor.__new__(Editor)
    editor.store = Store(folder)
    editor._input_version_cache = {}
    editor.calibration = SimpleNamespace(status=lambda: {})
    editor.source_provenance = lambda: {'fixture': 'codec/native boundary replaced'}
    return editor


def initial_project(editor, *, identifier='project-context'):
    path, digest = editor.store.object(b'user-supplied-official120-fixture')
    factory = crop('factory-0', 0, custom=False)
    shutdown = {'schema_version': 2, 'target_id': None, 'assets': [], 'items': [],
                'selected_asset_id': None, 'selected_item_id': None}
    return {'schema_version': 1, 'edition': 'demo-crop-shutdown', 'id': identifier,
            'title': 'Context fixture', 'input_path': str(path),
            'input': {'version': '1.20.10.11', 'sha256': digest, 'filename': 'official.bin'},
            'firmware': {'editor_supported': True}, 'filters': [native_filter()],
            'original_filters': [native_filter()], 'crops': [factory],
            'original_crops': [deepcopy(factory)], 'crop_capabilities': {'can_edit': True},
            'shutdown': shutdown, 'shutdown_inventory': {'resources': []}, 'builds': [],
            'native_effects_draft': {'schema_version': 1, 'pictorialist': False},
            'build_options': {'allow_older': True}}


class ProductProjectContextTests(unittest.TestCase):
    def test_build_then_delete_add_and_backup_only_restore_keep_completed_meanings(self):
        with tempfile.TemporaryDirectory() as original_root, tempfile.TemporaryDirectory() as restored_root:
            editor = make_editor(original_root)
            project = initial_project(editor)
            project['filters'].append(custom_filter('A'))
            draft_crop = crop('current-crop', 7)
            draft_crop['identity'] = {}
            project['crops'].append(draft_crop)
            editor.store.save_project(project)
            official = SimpleNamespace(rtos=b'official', editable=True,
                version=(1, 20, 10, 11), sha256=project['input']['sha256'])
            compiled, snapshots, records, loaded_candidates = {}, {}, {}, {}

            def compile_native(image, **options):
                # The service must always rebuild on the imported original,
                # even after its first completed build.
                self.assertIs(image, official)
                number = len(compiled) + 1
                compiled[number] = deepcopy(options)
                rows = deepcopy(options['filters'])
                for row in rows:
                    if row['kind'] == 'custom':
                        row['identity'] = custom_filter(row['id'], 34 if row['id'] == 'A' else 35)['identity']
                complete = [crop('factory-0', 0, custom=False), crop('retired-crop', 6, active=False),
                            crop('current-crop', 7)]
                rtos = f'compiled-{number}'.encode()
                snapshots[rtos] = {'filters': rows, 'crops': [complete[0], complete[2]],
                                   'crop_inspection': {'native_readback': 'passed'}}
                records[rtos] = {'crop_records': complete}
                return rtos, b'icons', b'resources', {'fixture': 'native compiler'}

            def repack(image, rtos, **options):
                candidate = b'candidate-' + rtos
                loaded_candidates[candidate] = SimpleNamespace(rtos=rtos, editable=True)
                return candidate, {'update_policy': {'status': 'verified', 'installed': True,
                                                     'policy': 'allow_older'}}

            def load(filename):
                data = Path(filename).read_bytes()
                if sha256(data) == official.sha256:
                    return official
                return loaded_candidates[data]

            with ExitStack() as stack:
                stack.enter_context(patch('gr4_editor.firmware.load_image', side_effect=load))
                stack.enter_context(patch('gr4_editor.firmware.repack_image', side_effect=repack))
                stack.enter_context(patch('gr4_editor.firmware.inspect_firmware', return_value={'fixture': True}))
                stack.enter_context(patch('gr4_editor.product120.compile_product120', side_effect=compile_native))
                stack.enter_context(patch('gr4_editor.product120.snapshot120', side_effect=lambda image: deepcopy(snapshots[image.rtos])))
                stack.enter_context(patch('gr4_editor.product120.metadata', side_effect=lambda rtos: deepcopy(records[rtos])))
                stack.enter_context(patch('gr4_editor.product120.canonical_base', return_value=b'official'))
                first = editor.build(project['id'])
                self.assertTrue(Path(first['firmware_path']).is_file())
                saved = editor.store.get_project(project['id'])
                self.assertEqual(saved['input'], project['input'])
                self.assertEqual(saved['filters'][-1]['identity']['style_id'], 34)
                self.assertEqual(saved['compiler_filter_archive'][0]['id'], 'A')
                self.assertEqual([row['identity']['public_id'] for row in saved['migration_crop_records']], [0, 6, 7])
                self.assertEqual(saved['crops'][-1]['identity']['public_id'], 7)
                self.assertEqual(saved['crops'][-1]['source_geometry'], crop('current-crop', 7)['geometry'])

                saved['filters'] = [native_filter(), custom_filter('B')]
                editor.store.save_project(saved)
                editor.build(project['id'])
                forwarded = {row['id']: row for row in compiled[2]['filters']}
                self.assertFalse(forwarded['A']['enabled'])
                self.assertEqual(forwarded['A']['identity']['style_id'], 34)
                self.assertEqual(forwarded['A']['resources'], custom_filter('A')['resources'])
                self.assertEqual(compiled[2]['crop_records'], saved['migration_crop_records'])
                backup = json.loads(json.dumps(editor.store.get_project(project['id'])))
                self.assertEqual(backup['filters'][-1]['identity']['style_id'], 35)
                self.assertEqual({row['id'] for row in backup['compiler_filter_archive']}, {'A', 'B'})

                # A second installation receives only the original input BIN
                # and JSON backup: no previous generated BINs or ledger.
                restored = make_editor(restored_root)
                new_project = initial_project(restored, identifier='project-restored')
                restored.store.save_project(new_project)
                restored.import_firmware = Mock(return_value=deepcopy(new_project))
                restored.crop_preview = Mock(side_effect=AssertionError('Saved migration geometry should be retained'))
                self.assertEqual(restored.store.ledger(), [])
                reopened = restored.import_project(backup)
                restored.import_firmware.assert_called_once_with(
                    b'user-supplied-official120-fixture', 'official.bin', '1.20')
                self.assertEqual(reopened['compiler_filter_archive'], backup['compiler_filter_archive'])
                self.assertEqual(reopened['migration_crop_records'], backup['migration_crop_records'])
                self.assertEqual(reopened['crops'][-1]['geometry'], backup['crops'][-1]['geometry'])
                restored.crop_preview.assert_not_called()
                restored.build(reopened['id'])
                again = {row['id']: row for row in compiled[3]['filters']}
                self.assertFalse(again['A']['enabled'])
                self.assertEqual(again['A']['identity']['style_id'], 34)
                self.assertEqual(again['B']['identity']['style_id'], 35)
                self.assertEqual(compiled[3]['crop_records'], backup['migration_crop_records'])

    def test_filter_planner_reserves_retired_identity_even_when_new_row_comes_first(self):
        native = native_filter()
        retired = custom_filter('A', 34)
        retired['enabled'] = False
        incoming = custom_filter('B')
        image = SimpleNamespace(rtos=b'official')
        for desired in ([incoming, retired], [retired, incoming]):
            with self.subTest(order=[row['id'] for row in desired]), \
                 patch.object(product120, 'read_filters120', return_value=[native]), \
                 patch.object(product120, 'metadata', return_value=None):
                result = product120._filter_plan(image, deepcopy(desired))
            byid = {row['id']: row for row in result}
            self.assertEqual(byid['A']['identity']['style_id'], 34)
            self.assertFalse(byid['A']['enabled'])
            self.assertEqual(byid['B']['identity']['style_id'], 35)

    def test_copying_a_custom_template_does_not_clone_its_source_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            editor = make_editor(folder)
            project = initial_project(editor)
            template = custom_filter('A', 34)
            template.update(source_family='1.11', source_identity={'style_id': 34, 'maker_note_code': 0x8106})
            project['filters'].append(template)
            editor.store.save_project(project)
            saved = editor.add_filter(project['id'], 'A', 'B')
            copied = saved['filters'][-1]
            self.assertEqual(copied['identity'], {})
            self.assertNotIn('source_identity', copied)
            self.assertEqual(copied['source_family'], '1.20')
            self.assertEqual(saved['filters'][-2]['source_identity'], template['source_identity'])


if __name__ == '__main__':
    unittest.main()
