# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Service builds follow the selected backend through packaged readback."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gr4_editor.crops import recipe
from gr4_editor.service import EDITION, Editor
from gr4_editor.shutdown import validate_draft
from gr4_editor.store import sha256
from gr4_editor.versions import version_word


class BuildRoutingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.editor = Editor(temporary.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def mock(name, **kwargs):
            return self.stack.enter_context(patch(name, **kwargs))

        source_path, source_hash = self.editor.store.object(b'synthetic official .7 container')
        self.source = SimpleNamespace(
            rtos=b'synthetic official .7 RTOS', icon_bytes=b'original icons',
            sha256=source_hash, editable=True, version=version_word('1.11.10.7'),
            internal_version='1.11.10.7')
        self.candidate = b'synthetic packaged candidate'
        self.packaged = SimpleNamespace(
            rtos=b'compiled filter RTOS', icon_bytes=b'compiled filter icons',
            sha256=sha256(self.candidate), editable=True)
        crops = [{'id': 'crop-0', 'kind': 'factory', 'identity': {'public_id': 0},
                  'name': '3:2', 'requested_ratio': '3:2',
                  'geometry': {'screen': {'width': 720, 'height': 480}}}]
        self.readback_crops = deepcopy(crops)
        inventory = {'resources': []}
        self.inventory = inventory
        factory = {'id': 'standard', 'kind': 'factory', 'name': 'Standard',
                   'identity': {'style_id': 11}, 'enabled': True}
        custom = {'id': 'xmp-look', 'kind': 'custom', 'name': 'XMP look',
                  'identity': {}, 'enabled': True, 'xmp': {'render_engine': 'offline'},
                  'calibration': {'status': 'fitted_offline'}}
        self.resolved_filters = [deepcopy(factory), deepcopy(custom)]
        self.resolved_filters[1]['identity'] = {'style_id': 34}
        self.project = {
            'schema_version': 1, 'edition': EDITION, 'id': 'project-routing',
            'input_path': str(source_path),
            'input': {'sha256': source_hash, 'version': '1.11.10.7'},
            'firmware': {'editor_supported': True,
                         'layout_proof': {'backend_source_verified': True}},
            'crops': crops, 'original_crops': deepcopy(crops),
            'shutdown': validate_draft(None, inventory),
            'filters': [factory, custom], 'original_filters': [deepcopy(factory)],
            'builds': [], 'build_options': {'allow_older': True},
        }

        def load_image(path):
            if Path(path) == source_path:
                return self.source
            # Exercise the actual build output path and independent reopen.
            self.assertEqual(Path(path).read_bytes(), self.candidate)
            return self.packaged

        mock('gr4_editor.firmware.load_image', side_effect=load_image)
        self.repack = mock('gr4_editor.firmware.repack_image', return_value=(
            self.candidate, {'update_policy': {'status': 'verified', 'policy': 'allow_older'}}))
        mock('gr4_editor.firmware.inspect_firmware', return_value={'fixture': 'packaged inspection'})
        mock('gr4_editor.service.inspect_update_policy', return_value={'status': 'verified'})
        # The official source is accepted by both compilers. The edited
        # recipe, rather than mere support, must determine the selected one.
        self.demo_supports = mock('gr4_editor.demo_build.supports_image', return_value=True)
        self.filter_supports = mock('gr4_editor.slots.supports_image', return_value=True)
        mock('gr4_editor.demo_build.OFFICIAL7_SHA', new=sha256(self.source.rtos))
        self.demo_canonical = mock('gr4_editor.demo_build.canonical_base', return_value=self.source.rtos)
        self.filter_canonical = mock('gr4_editor.slots.canonical_base', return_value=self.source.rtos)
        self.metadata = mock('gr4_editor.demo_build._metadata', return_value={})
        self.filter_compile = mock('gr4_editor.slots.apply_filter_registry', return_value=(
            self.packaged.rtos, self.packaged.icon_bytes,
            {'recipe_filters': deepcopy(self.resolved_filters)}))
        self.filter_read = mock('gr4_editor.filter_reader.read_filters',
                                side_effect=lambda image: deepcopy(self.resolved_filters))
        self.demo_compile = mock('gr4_editor.demo_build.build_features', return_value=(
            b'compiled demo RTOS', b'compiled demo icons', {'backend': 'demo-crop-shutdown-v1'}))
        self.prepare_crops = mock('gr4_editor.crop_compiler.prepare_crops',
                                  return_value=({'crops': crops}, {'native_readback': 'passed',
                                                                  'raw_recut_complete': True}))
        mock('gr4_editor.shutdown.read_shutdown', return_value=inventory)
        self.shutdown_prepare = mock('gr4_editor.shutdown_compiler.prepare_menu_plan',
                                     return_value=(None, None))
        self.shutdown_read = mock('gr4_editor.shutdown_compiler.read_menu_resources', return_value=None)
        self.res_preservation = mock('gr4_editor.shutdown_compiler.verify_original_res',
                                     return_value={'preserved': True})
        self.crop_icons = mock('gr4_editor.crop_icons.verify_crop_icons',
                               return_value={'status': 'passed'})
        self.crop_read = mock('gr4_editor.crops.read_crops',
                              side_effect=lambda rtos: (deepcopy(self.readback_crops),
                                                        {'native_readback': 'passed',
                                                         'raw_recut_complete': True}))
        self.crop_recipe = mock('gr4_editor.crops.recipe', wraps=recipe)
        self.stack.enter_context(patch.object(self.editor.calibration, 'status', return_value={}))
        self.stack.enter_context(patch.object(self.editor, 'source_provenance', return_value={'fixture': True}))

    def build_manifest(self):
        self.editor.store.save_project(self.project)
        result = self.editor.build(self.project['id'], version='1.11.10.12')
        manifest = json.loads(Path(result['manifest_path']).read_text(encoding='utf-8'))
        self.assertEqual(Path(result['firmware_path']).read_bytes(), self.candidate)
        self.assertEqual(manifest['sha256'], sha256(self.candidate))
        self.assertEqual(manifest['validation']['offline'], 'passed')
        self.assertEqual(manifest['validation']['hardware'], 'not_tested')
        self.assertEqual(self.editor.store.ledger()[0]['id'], result['id'])
        saved = self.editor.store.get_project(self.project['id'])
        self.assertEqual(saved['builds'][0]['id'], result['id'])
        return result, manifest

    def test_official_filter_edit_completes_filter_readback_and_manifest(self):
        result, manifest = self.build_manifest()

        self.filter_compile.assert_called_once()
        self.demo_compile.assert_not_called()
        self.prepare_crops.assert_not_called()
        self.assertEqual(self.crop_read.call_count, 2)
        self.assertEqual(self.crop_recipe.call_count, 2)
        self.demo_canonical.assert_not_called()
        self.shutdown_prepare.assert_called_once_with(self.source, self.inventory, self.project['shutdown'])
        self.shutdown_read.assert_called_once_with(self.packaged, {})
        self.assertEqual(self.filter_read.call_count, 2)
        initial_read = self.filter_read.call_args_list[0].args[0]
        self.assertEqual((initial_read.rtos, initial_read.icon_bytes),
                         (self.packaged.rtos, self.packaged.icon_bytes))
        self.assertIs(self.filter_read.call_args_list[1].args[0], self.packaged)
        self.assertEqual(manifest['filters'], self.resolved_filters)
        self.assertEqual(manifest['recipe_filters'], self.resolved_filters)
        self.assertEqual(manifest['compiler_base']['source'], 'verified native filter registry base')
        self.assertEqual(manifest['compiler_base']['rtos_sha256'], sha256(self.source.rtos))
        self.assertIn('native filter', manifest['validation']['scope'])
        self.assertEqual(result['report']['patch']['backend'], 'canonical-filter-registry')
        self.assertEqual(result['report']['patch']['filter_readback']['status'], 'passed')

    def combined_draft(self):
        custom_crop = {'id': 'wide-crop', 'kind': 'custom', 'identity': {},
                       'name': 'Wide', 'requested_ratio': '21:9'}
        self.project['crops'].append(custom_crop)
        self.project['crop_identity_context'] = {
            'source_rtos_sha256': sha256(self.source.rtos),
            'records': [{'identity': {'public_id': 5}, 'active': False}],
            'next_public': 7, 'order': [0],
        }
        self.readback_crops = deepcopy(self.project['crops'])
        self.readback_crops[-1].update(
            identity={'public_id': 7}, geometry={'screen': {'width': 720, 'height': 308}},
            geometry_status='native')
        self.project['shutdown']['items'] = [
            {'id': 'shutdown-night', 'selection_id': 1, 'name': 'Night',
             'asset_id': 'night-picture', 'fit': 'contain'}]
        self.shutdown_plan = {'schema_version': 2, 'presets': [
            {'selection_id': 1, 'native_id': 12, 'name': 'Night'}]}
        self.shutdown_res = b'synthetic compiled RES section'
        self.shutdown_prepare.return_value = (self.shutdown_plan, self.shutdown_res)
        self.installed_shutdown = {'schema_version': 2, 'presets': [
            {'selection_id': 1, 'native_id': 12, 'name': 'Night', 'readback': 'passed'}]}
        self.shutdown_read.return_value = self.installed_shutdown
        self.metadata.return_value = {'crop_icons': {'fixture': 'native crop icons'}}

    def assert_combined_filter_contract(self, manifest):
        self.filter_compile.assert_called_once()
        args, options = self.filter_compile.call_args
        self.assertEqual(args, (self.source.rtos, self.source.icon_bytes,
                                self.project['filters'], self.project['original_filters']))
        self.assertEqual(options['crops'], self.project['crops'])
        self.assertEqual(options['prior_crops'], self.project['original_crops'])
        self.assertEqual(options['crop_context'], self.project['crop_identity_context'])
        self.assertEqual(options['shutdown_plan'], self.shutdown_plan)
        self.assertEqual(options['soft_glow_plan'], {'enabled': False})
        self.assertEqual(manifest['crops'], self.readback_crops)
        self.assertEqual(manifest['recipe_crops'], self.project['crops'])
        self.assertEqual(manifest['crop_inspection']['native_readback'], 'passed')
        self.assertEqual(manifest['shutdown_menu'], self.installed_shutdown)
        self.assertEqual(manifest['recipe_shutdown'], self.project['shutdown'])
        self.assertEqual(self.repack.call_args.kwargs['new_res_section'], self.shutdown_res)
        self.res_preservation.assert_called_once_with(self.source, self.packaged)
        self.crop_icons.assert_called_once_with(self.packaged, self.metadata.return_value['crop_icons'])
        self.assertEqual(manifest['patch_report']['shutdown_resource_preservation'], {'preserved': True})
        self.assertEqual(manifest['patch_report']['crop_icon_container_readback'], {'status': 'passed'})
        self.demo_compile.assert_not_called()
        self.demo_canonical.assert_not_called()

    def test_filter_edit_can_compile_new_crop_and_shutdown_drafts_together(self):
        self.combined_draft()
        _, manifest = self.build_manifest()

        self.assert_combined_filter_contract(manifest)
        self.assertEqual(self.filter_read.call_count, 2)
        self.assertEqual(self.crop_read.call_count, 2)
        self.assertNotEqual(manifest['crops'][-1]['identity'], self.project['crops'][-1]['identity'])

    def test_registry_rebuild_keeps_existing_crop_and_shutdown_plans(self):
        self.combined_draft()
        self.source.rtos = b'prior compiled registry RTOS with crop and shutdown'
        self.source.version = version_word('1.11.10.11')
        self.source.internal_version = '1.11.10.11'
        self.project['input']['version'] = '1.11.10.11'
        self.project['filters'] = deepcopy(self.resolved_filters)
        self.project['original_filters'] = deepcopy(self.resolved_filters)
        self.project['crops'] = deepcopy(self.readback_crops)
        self.project['original_crops'] = deepcopy(self.readback_crops)
        self.project['crop_identity_context'].update(
            source_rtos_sha256=sha256(self.source.rtos),
            records=[deepcopy(self.readback_crops[-1])], next_public=8, order=[0, 7])
        self.demo_supports.return_value = False
        _, manifest = self.build_manifest()

        self.assert_combined_filter_contract(manifest)
        self.shutdown_prepare.assert_called_once_with(self.source, self.inventory, self.project['shutdown'])
        self.assertEqual(manifest['filters'], self.resolved_filters)

    def test_registry_rebuild_recovers_imported_menu_from_old_empty_inventory(self):
        import base64
        from io import BytesIO
        from PIL import Image

        picture = BytesIO()
        Image.new('RGB', (720, 480), (70, 100, 130)).save(picture, format='JPEG')
        self.inventory.update(resources=[{
            'id': 'goodbye', 'path': 'A:\\Resource\\Jpeg\\GoodBye.jpg',
            'native_id': 1, 'size': 7264, 'width': 720, 'height': 480,
            'sha256': '0' * 64, 'display_layer_verified': True,
        }], installed_menu={'schema_version': 2, 'presets': [{
            'slot': 1, 'selection_id': 41, 'native_id': 12, 'name': 'Imported night',
            'asset_id': 'imported-night', 'fit': 'contain',
            'payload_base64': base64.b64encode(picture.getvalue()).decode('ascii'),
        }]})
        self.project['shutdown_inventory'] = {'resources': []}
        restored = validate_draft(None, self.inventory)
        self.assertEqual(self.project['shutdown']['items'], [])
        self.assertEqual(restored['items'][0]['selection_id'], 41)
        self.project['filters'] = deepcopy(self.resolved_filters)
        self.project['original_filters'] = deepcopy(self.resolved_filters)
        self.demo_supports.return_value = False
        plan = {'schema_version': 2, 'presets': [
            {'selection_id': 41, 'native_id': 12, 'name': 'Imported night'}]}
        self.shutdown_prepare.return_value = (plan, b'preserved imported RES section')
        self.shutdown_read.return_value = deepcopy(self.inventory['installed_menu'])

        _, manifest = self.build_manifest()

        self.shutdown_prepare.assert_called_once_with(self.source, self.inventory, restored)
        self.assertEqual(self.filter_compile.call_args.kwargs['shutdown_plan'], plan)
        self.assertEqual(manifest['recipe_shutdown'], restored)
        self.assertEqual(manifest['shutdown_menu'], self.inventory['installed_menu'])
        saved = self.editor.store.get_project(self.project['id'])
        self.assertEqual(saved['shutdown'], restored)
        self.assertEqual(saved['shutdown_inventory'], self.inventory)

    def test_filter_packaging_rejects_changed_crop_geometry(self):
        frozen = deepcopy(self.readback_crops)
        changed = deepcopy(frozen)
        changed[0]['geometry']['screen']['height'] = 400
        self.crop_read.side_effect = [
            (frozen, {'native_readback': 'passed', 'raw_recut_complete': True}),
            (changed, {'native_readback': 'passed', 'raw_recut_complete': True}),
        ]
        self.editor.store.save_project(self.project)
        with self.assertRaisesRegex(ValueError, '裁切读回不一致'):
            self.editor.build(self.project['id'])
        self.assertEqual(self.editor.store.ledger(), [])

    def test_filter_packaging_rejects_incomplete_crop_readback_even_with_matching_geometry(self):
        self.editor.store.save_project(self.project)
        for inspection in ({'native_readback': 'unknown', 'raw_recut_complete': True},
                           {'native_readback': 'passed', 'raw_recut_complete': False}):
            with self.subTest(inspection=inspection):
                self.crop_read.side_effect = [
                    (deepcopy(self.readback_crops),
                     {'native_readback': 'passed', 'raw_recut_complete': True}),
                    (deepcopy(self.readback_crops), inspection),
                ]
                with self.assertRaisesRegex(ValueError, '裁切/RAW 读回失败'):
                    self.editor.build(self.project['id'])
                self.assertEqual(self.editor.store.ledger(), [])
                self.assertEqual(self.editor.store.get_project(self.project['id'])['builds'], [])
                self.assertEqual(list(self.editor.store.root.glob('builds/*/manifest.json')), [])

    def test_unregistered_input_rejects_crop_and_shutdown_edits(self):
        self.combined_draft()
        self.project['filters'] = deepcopy(self.project['original_filters'])
        self.demo_supports.return_value = False
        self.filter_supports.return_value = False
        self.editor.store.save_project(self.project)
        with self.assertRaisesRegex(ValueError, '只支持保留资源'):
            self.editor.build(self.project['id'])
        self.filter_compile.assert_not_called()
        self.demo_compile.assert_not_called()
        self.repack.assert_not_called()
        self.assertEqual(self.editor.store.ledger(), [])

    def test_unregistered_input_without_edits_preserves_imported_resources(self):
        self.project['filters'] = deepcopy(self.project['original_filters'])
        self.demo_supports.return_value = False
        self.filter_supports.return_value = False
        self.packaged.rtos = self.source.rtos
        self.packaged.icon_bytes = self.source.icon_bytes
        result, manifest = self.build_manifest()

        args, options = self.repack.call_args
        self.assertIs(args[0], self.source)
        self.assertEqual(args[1], self.source.rtos)
        self.assertEqual(options['new_icon_bytes'], self.source.icon_bytes)
        self.assertIsNone(options['new_res_section'])
        self.filter_compile.assert_not_called()
        self.demo_compile.assert_not_called()
        self.shutdown_prepare.assert_not_called()
        self.shutdown_read.assert_not_called()
        self.crop_recipe.assert_not_called()
        self.assertEqual(result['report']['patch']['backend'], 'generic-preserve')
        self.assertEqual(manifest['compiler_base']['rtos_sha256'], sha256(self.source.rtos))

    def test_unchanged_official_filters_keep_demo_crop_and_canonical_validation(self):
        self.project['filters'] = deepcopy(self.project['original_filters'])
        self.packaged.rtos = b'compiled demo RTOS'
        self.packaged.icon_bytes = b'compiled demo icons'
        result, manifest = self.build_manifest()

        self.demo_compile.assert_called_once()
        self.prepare_crops.assert_called_once()
        self.filter_compile.assert_not_called()
        self.filter_canonical.assert_not_called()
        self.filter_read.assert_not_called()
        self.assertEqual(self.crop_read.call_count, 2)
        self.assertEqual(self.crop_recipe.call_count, 2)
        self.demo_canonical.assert_called_once_with(self.packaged.rtos)
        self.shutdown_read.assert_called_once_with(self.packaged, {})
        self.assertEqual(manifest['compiler_base']['source'], 'restored user-supplied official input')
        self.assertIn('native crop readback', manifest['validation']['scope'])
        self.assertEqual(result['report']['patch']['backend'], 'demo-crop-shutdown-v1')

    def test_filter_candidate_without_recoverable_base_is_not_registered(self):
        from gr4_editor import slots

        self.editor.store.save_project(self.project)
        with patch('gr4_editor.service._filter_backend',
                   side_effect=lambda image: None if image is self.packaged else slots):
            with self.assertRaisesRegex(ValueError, '滤镜编译基线无法恢复'):
                self.editor.build(self.project['id'])
        self.assertEqual(self.editor.store.ledger(), [])
        self.assertEqual(self.editor.store.get_project(self.project['id'])['builds'], [])


if __name__ == '__main__':
    unittest.main()
