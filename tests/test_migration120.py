"""Recipe migration uses synthetic user data, never bundled firmware."""
from copy import deepcopy
from io import BytesIO
from hashlib import sha256
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.migration120 import migrate_configuration
from gr4_editor.multiversion import MultiVersionEditorMixin
from gr4_editor.shutdown import prepare_asset, validate_draft


def projects():
    target_resource = {'id': 'goodbye', 'path': 'A:\\Resource\\Jpeg\\GoodBye.jpg',
                       'size': 4096, 'width': 72, 'height': 48, 'sha256': '2' * 64,
                       'display_layer_verified': True, 'replacement_supported': True}
    source_resource = {**target_resource, 'size': 3072, 'sha256': '1' * 64,
                       'width': 64, 'height': 48}
    stream = BytesIO()
    Image.new('RGB', (80, 60), (90, 120, 35)).save(stream, 'PNG')
    asset = prepare_asset(stream.getvalue(), 'my-screen.png', source_resource, 'cover')
    source = {'id': 'source111', 'title': '我的配置',
              'input': {'version': '1.11.10.97', 'sha256': 'a' * 64},
              'build_options': {'allow_older': False},
              'filters': [
                  {'id': 'standard111', 'kind': 'factory', 'name': 'Old Standard', 'enabled': False,
                   'identity': {'style_id': 11, 'profile_id': 11, 'text_id': 10},
                   'resources': {'native111': 'must not overwrite120'}},
                  {'id': 'my-filter', 'kind': 'custom', 'name': 'My Color', 'enabled': True,
                   'identity': {'style_id': 34, 'profile_id': 34, 'raw_ordinal': 30,
                                'text_id': 825, 'icon_id': 705, 'apdata_record': 72},
                   'resources': {'banks': [{'matrix': {'hex': '1234'}, 'gamma': {'hex': 'abcd'}}],
                                 'icon': {'hex': 'aabbccdd', 'width': 1, 'height': 1}},
                   'parameters': [{'id': 'Contrast', 'enabled': True, 'default': 6}],
                   'xmp': {'path': 'C:/old-machine/private.xmp'},
                   'calibration': {'id': 'old-job', 'path': 'C:/old-machine/job'},
                   'pending_color_conversion': False}],
              'crops': [
                  {'id': 'native111', 'kind': 'factory', 'identity': {'public_id': 0}, 'native': 'old'},
                  {'id': 'wide111', 'kind': 'custom', 'name': '宽银幕', 'requested_ratio': '2.35:1',
                   'identity': {'public_id': 7, 'internal_id': 6, 'rom': 0x53001111},
                   'geometry': {'screen': {'width': 720, 'height': 308}}}],
              'shutdown': {'schema_version': 2, 'target_id': 'goodbye', 'selected_asset_id': asset['id'],
                  'assets': [asset], 'items': [{'id': 'cover111', 'selection_id': 123,
                  'name': 'My screen', 'asset_id': asset['id'], 'fit': 'cover'}],
                  'selected_item_id': 'cover111'}}
    target = {'id': 'target120', 'input': {'version': '1.20.10.11', 'sha256': 'b' * 64},
              'capabilities': {'max_custom_slots': 16},
              'filters': [
                  {'id': 'standard120', 'kind': 'factory', 'name': 'Standard 120', 'enabled': True,
                   'identity': {'style_id': 11, 'profile_id': 11, 'text_id': 10},
                   'resources': {'native120': 'preserve'}},
                  {'id': 'palette120', 'kind': 'factory', 'name': 'Palette', 'enabled': True,
                   'identity': {'style_id': 33}, 'resources': {'new120': 'preserve'}}],
              'crops': [{'id': 'native120', 'kind': 'factory', 'identity': {'public_id': 0},
                         'native': 'target120 geometry'}],
              'shutdown_inventory': {'resources': [target_resource]}}
    return source, target


class ConfigurationMigrationTests(unittest.TestCase):
    def setUp(self):
        self.source, self.target = projects()
        self.before = deepcopy(self.source)

    def test_preserves_source_and_target_native_payloads_with_user_visibility(self):
        original_target = deepcopy(self.target)
        result = migrate_configuration(self.source, self.target)
        self.assertEqual(self.source, self.before)
        native = result['filters'][0]
        self.assertEqual(native['resources'], original_target['filters'][0]['resources'])
        self.assertEqual(native['name'], 'Standard 120')
        self.assertFalse(native['enabled'])
        palette = next(row for row in result['filters'] if row['id'] == 'palette120')
        self.assertEqual(palette['resources'], original_target['filters'][1]['resources'])
        self.assertTrue(palette['enabled'])
        self.assertEqual([row['menu_order'] for row in result['filters']], [0, 1, 2])
        self.assertTrue(result['migration']['source_project_preserved'])
        self.assertEqual(result['migration']['source']['sha256'], 'a' * 64)
        self.assertEqual(result['migration']['target']['sha256'], 'b' * 64)

    def test_custom_identity_keeps_style_only_and_colors_are_embedded(self):
        result = migrate_configuration(self.source, self.target)
        row = next(row for row in result['filters'] if row['kind'] == 'custom')
        self.assertEqual(row['identity'], {'style_id': 34})
        self.assertEqual(row['source_identity'], self.before['filters'][1]['identity'])
        self.assertEqual(row['resources'], self.before['filters'][1]['resources'])
        self.assertEqual(row['parameters'], self.before['filters'][1]['parameters'])
        self.assertNotIn('xmp', row)
        self.assertNotIn('calibration', row)
        self.assertFalse(row['pending_color_conversion'])
        row['resources']['banks'][0]['matrix']['hex'] = 'ffff'
        self.assertEqual(self.source, self.before)

    def test_factory_geometry_is_target_owned_and_legacy_custom_data_is_retained(self):
        result = migrate_configuration(self.source, self.target)
        native, custom = result['crops']
        self.assertEqual(native['native'], 'target120 geometry')
        self.assertEqual(native['id'], 'native120')
        self.assertEqual(custom['identity'], {})
        self.assertEqual(custom['geometry'], self.source['crops'][1]['geometry'])
        self.assertEqual(custom['source_geometry'], custom['geometry'])
        self.assertEqual(custom['source_requested_ratio'], '2.35:1')
        self.assertEqual(custom['name'], '宽银幕')
        self.assertEqual(custom['requested_ratio'], '2.35:1')

    def test_shutdown_images_are_rebound_and_reencoded_against_target(self):
        result = migrate_configuration(self.source, self.target)
        asset = result['shutdown']['assets'][0]
        old = self.before['shutdown']['assets'][0]
        self.assertNotEqual(asset['id'], old['id'])
        self.assertEqual(asset['target_sha256'], '2' * 64)
        self.assertEqual((asset['width'], asset['height'], asset['target_size']), (72, 48, 4096))
        self.assertEqual(asset['source_payload_base64'], old['source_payload_base64'])
        self.assertEqual(asset['fit'], 'cover')
        item = result['shutdown']['items'][0]
        self.assertEqual(item['asset_id'], asset['id'])
        self.assertEqual(item['selection_id'], 123)
        self.assertEqual(validate_draft(result['shutdown'], result['shutdown_inventory']), result['shutdown'])
        self.assertEqual(result['shutdown_draft'], result['shutdown'])
        self.assertEqual(result['build_options'], {'allow_older': False})
        self.assertEqual(result['native_effects_draft'], {'schema_version': 1, 'pictorialist': False})

    def test_selected_shutdown_item_is_preserved_after_rebinding(self):
        result = migrate_configuration(self.source, self.target)
        self.assertEqual(result['shutdown']['selected_item_id'], 'migrated-cover111')
        self.assertEqual(result['shutdown']['selected_asset_id'], result['shutdown']['assets'][0]['id'])

    def test_unreferenced_background_materials_remain_available(self):
        unused = deepcopy(self.source['shutdown']['assets'][0])
        unused.update(id='unused-material', name='Unused', filename='unused.png')
        self.source['shutdown']['assets'].append(unused)
        result = migrate_configuration(self.source, self.target)
        self.assertEqual(len(result['shutdown']['assets']), 2)
        self.assertEqual(len(result['shutdown']['items']), 1)
        self.assertEqual(result['shutdown']['assets'][1]['filename'], 'unused.png')
        self.assertTrue(all(asset['target_sha256'] == '2' * 64 for asset in result['shutdown']['assets']))

    def test_invalid_family_rejects_without_mutating_source(self):
        for source_version, target_version in (('1.20.10.18', '1.20.10.11'), ('1.11.10.97', '1.11.10.7')):
            with self.subTest(source=source_version, target=target_version):
                source, target = projects()
                source['input']['version'], target['input']['version'] = source_version, target_version
                before = deepcopy(source)
                with self.assertRaises(ValueError):
                    migrate_configuration(source, target)
                self.assertEqual(source, before)

    def test_unfinished_colors_or_capacity_overflow_reject(self):
        self.source['filters'][1]['pending_color_conversion'] = True
        with self.assertRaisesRegex(ValueError, '未完成'):
            migrate_configuration(self.source, self.target)
        source, target = projects()
        target['capabilities']['max_custom_slots'] = 0
        with self.assertRaisesRegex(ValueError, '容量'):
            migrate_configuration(source, target)

    def test_unmatched_native_crop_or_unavailable_shutdown_image_reject(self):
        self.target['crops'] = []
        with self.assertRaisesRegex(ValueError, '裁切身份'):
            migrate_configuration(self.source, self.target)
        source, target = projects()
        source['shutdown']['items'][0]['asset_id'] = 'missing-image'
        with self.assertRaisesRegex(ValueError, '尚未选择图片'):
            migrate_configuration(source, target)
        source, target = projects()
        target['shutdown_inventory']['resources'] = []
        with self.assertRaisesRegex(ValueError, '关机资源'):
            migrate_configuration(source, target)


class MigrationServiceBoundaryTests(unittest.TestCase):
    def test_upgrade_rejects_modified_container_with_only_official_rtos(self):
        import gr4_editor
        from gr4_editor import firmware
        source, target = projects()
        source['crop_inspection'] = {'compiler_supported': True}
        source['title'] = 'source'
        imported = []

        class Editor(MultiVersionEditorMixin):
            def __init__(self):
                self.store = SimpleNamespace(object=lambda data: ('synthetic-object', 'digest'))

            def project(self, project_id):
                return deepcopy(source)

            def import_firmware(self, *args, **kwargs):
                imported.append((args, kwargs))
                return deepcopy(target)

        official_rtos = b'synthetic-official-rtos'
        backend = SimpleNamespace(OFFICIAL_RTOS_SHA256=sha256(official_rtos).hexdigest(), SOURCE_SHA256='c' * 64)
        modified = SimpleNamespace(internal_version='1.20.10.11', rtos=official_rtos,
                                   editable=False, history=None, sha256='d' * 64)
        with patch.object(gr4_editor, 'product120', backend, create=True), patch.dict(
                sys.modules, {'gr4_editor.product120': backend}), patch.object(
                firmware, 'load_image', return_value=modified):
            with self.assertRaisesRegex(ValueError, '官方原版'):
                Editor().upgrade_firmware('source111', b'modified-container')
        self.assertEqual(imported, [])


if __name__ == '__main__':
    unittest.main()
