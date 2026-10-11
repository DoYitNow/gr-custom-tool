"""Current product bridge receipts and actual JPEG metadata; no Adobe process runs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import lightroom_bridge as bridge
from gr4_editor import firmware
from gr4_editor.fitting import _native_context, generic_audit, run_job
from gr4_editor import numeric
from gr4_editor.numeric.helpers import target_metadata, read_target
from gr4_editor.store import sha256, write_json

PROFILE = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:PresetType="Look" crs:UUID="TEST-LOOK" crs:ProcessVersion="11.0"><crs:Name><rdf:Alt><rdf:li>Test fixture</rdf:li></rdf:Alt></crs:Name></rdf:Description></rdf:RDF></x:xmpmeta>'''
TARGET_XMP = b'''<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" crs:RawFileName="sample.DNG" crs:WhiteBalance="As Shot"><crs:Look><rdf:Description crs:UUID="TEST-LOOK" crs:RGBTable="TABLE-ID"><crs:ToneCurvePV2012><rdf:Seq><rdf:li>0, 0</rdf:li><rdf:li>255, 250</rdf:li></rdf:Seq></crs:ToneCurvePV2012></rdf:Description></crs:Look></rdf:Description></rdf:RDF></x:xmpmeta>'''


class BridgeReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / 'calibration/jobs' / ('a' * 24)
        photos = self.folder / 'photos'
        photos.mkdir(parents=True)
        source = self.folder / 'source.xmp'
        source.write_bytes(PROFILE)
        sample = photos / 'sample.DNG'
        sample.write_bytes(b'DNG fixture bytes; not a renderable camera file')
        self.job = {'id': 'a' * 24, 'xmp_path': str(source), 'xmp_sha256': sha256(PROFILE),
                    'photos': str(photos), 'dataset': {'samples': [
                        {'name': sample.name, 'sha256': sha256(sample.read_bytes()), 'role': 'holdout'}]}}
        appdata = patch.dict(os.environ, {'APPDATA': str(self.root / 'appdata')})
        appdata.start()
        self.addCleanup(appdata.stop)
        refresh = patch.object(bridge, 'import_native_presets')
        refresh.start()
        self.addCleanup(refresh.stop)
        self.request = bridge.prepare_request(self.root, self.job)
        self.jpeg = Path(self.request['output_directory']) / 'sample.jpg'
        Image.new('RGB', (12, 8), 'red').save(self.jpeg)
        preset = Path(self.request['profile_path']).with_name(self.request['native_preset_filename'])
        self.result = {key: self.request[key] for key in ('job_id', 'nonce', 'xmp_sha256', 'profile_uuid')}
        self.result.update(status='rendered', render_engine='lightroom',
                           render_recipe_version=bridge.NATIVE_RENDER_RECIPE_VERSION,
                           profile_tables=self.request['profile_tables'], native_preset_path=str(preset),
                           application={'name': 'test double', 'plugin_version': bridge.PLUGIN_VERSION},
                           outputs=[{'name': 'sample', 'path': str(self.jpeg), 'look_uuid': 'TEST-LOOK'}])

    def publish(self):
        write_json(self.folder / 'render-result.json', self.result)
        return bridge.complete_request(self.root, self.job)

    def test_new_exports_are_published_with_explicit_engine_recipe_and_hashes(self):
        receipt = self.publish()
        self.assertEqual(receipt['render_engine'], 'lightroom')
        self.assertEqual(receipt['render_recipe_version'], bridge.NATIVE_RENDER_RECIPE_VERSION)
        self.assertFalse(receipt['approximate'])
        self.assertEqual(receipt['outputs'][0]['jpeg_sha256'], sha256(self.jpeg.read_bytes()))
        self.assertEqual(Path(receipt['outputs'][0]['target']).read_bytes(), self.jpeg.read_bytes())

    def test_stale_nonce_and_old_plugin_cannot_publish_existing_jpegs(self):
        for key, value in (('nonce', 'stale'), ('render_recipe_version', 2)):
            original = deepcopy(self.result)
            self.result[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.publish()
            self.assertFalse((self.folder / 'render-receipt.json').exists())
            self.result = original

    def test_export_outside_request_directory_is_rejected(self):
        old = Path(self.job['photos']) / 'sample.jpg'
        Image.new('RGB', (12, 8), 'blue').save(old)
        self.result['outputs'][0]['path'] = str(old)
        with self.assertRaisesRegex(ValueError, '本次新建导出目录'):
            self.publish()

    def test_missing_or_partial_result_stays_pending(self):
        self.assertIsNone(bridge.complete_request(self.root, self.job))
        (self.folder / 'render-result.json').write_text('{"status":', encoding='utf-8')
        self.assertIsNone(bridge.complete_request(self.root, self.job))

    def test_fitter_rejects_receipt_from_another_request_before_decoding_dng(self):
        receipt = self.publish()
        job = {**self.job, 'render_engine': 'lightroom', 'render_nonce': 'another-request'}
        with self.assertRaisesRegex(ValueError, '当前渲染请求'):
            run_job(job, receipt)


class LightroomMetadataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'sample.jpg'
        exif = Image.Exif()
        exif[305] = 'JPEG metadata fixture'
        exif[34665] = {36867: '2026:10:11 12:34:56'}
        Image.new('RGB', (80, 64), (60, 100, 130)).save(self.path, exif=exif, xmp=TARGET_XMP)
        self.manifest = {'attributes_without_table_payloads': {'UUID': 'TEST-LOOK'},
                         'tables': {'RGBTable': {'fingerprint': 'TABLE-ID'}},
                         'tone_curves': {'ToneCurvePV2012': [[0, 0], [255, 250]]}}

    def test_real_jpeg_metadata_matches_uuid_tables_curves_and_capture(self):
        target = target_metadata(self.path, self.manifest)
        self.assertTrue(target['look_uuid_tables_curves_match'])
        self.assertEqual(target['capture_datetime'], '2026:10:11 12:34:56')
        self.assertEqual(target['raw_file_name'], 'sample.DNG')
        self.assertEqual(read_target(self.path, 64).shape, (51, 64, 3))
        wrong = deepcopy(self.manifest)
        wrong['tables']['RGBTable']['fingerprint'] = 'wrong table'
        self.assertFalse(target_metadata(self.path, wrong)['look_uuid_tables_curves_match'])

    def test_pair_audit_rejects_wrong_capture_or_changed_look(self):
        target = target_metadata(self.path, self.manifest)
        rows = {name: {'dng': {'sha256': name, 'capture_datetime': target['capture_datetime']},
                       'target': deepcopy(target), 'same_raw_name_and_capture_time': True}
                for name in ('one', 'two', 'holdout')}
        audit = generic_audit(rows, 'holdout', self.manifest)
        self.assertTrue(audit['holdout_excluded_from_selection_and_fit'])
        rows['two']['target']['capture_datetime'] = 'another capture'
        with self.assertRaisesRegex(ValueError, '拍摄时间'):
            generic_audit(rows, 'holdout', self.manifest)
        rows['two']['target'] = deepcopy(target)
        rows['holdout']['target']['look_attributes']['UUID'] = 'another look'
        with self.assertRaisesRegex(ValueError, 'UUID'):
            generic_audit(rows, 'holdout', self.manifest)


class NativeReaderRouteTests(unittest.TestCase):
    def test_shared_fitter_reads_controls_from_either_host_reader_contract(self):
        memory = SimpleNamespace(read=lambda address, size: bytes(size))
        image = SimpleNamespace(rtos=b'fixture', sha256='input-hash', layout_proof={'test_double': True})
        for bundled_reader in (True, False):
            reader = Mock(return_value={'banks': [{'descriptor_hex': {'multi_axial': '00' * 24}}]})
            split_reader = SimpleNamespace(_color_resources=reader)
            with self.subTest(bundled_reader=bundled_reader), \
                    patch.object(firmware, '_color_resources', reader if bundled_reader else None, create=True), \
                    patch.object(firmware, 'load_image', return_value=image), \
                    patch.object(firmware, '_Memory', return_value=memory), \
                    patch.dict(sys.modules, {'gr4_editor.filter_reader': split_reader}), \
                    patch('gr4_editor.fitting.load_gamma_base', return_value=(list(range(256)), {'sha256': 'base'})):
                controls, gamma, dependencies = _native_context(numeric, {'firmware_path': 'input.bin'})
                reader.assert_called_once_with(memory, 11)
                self.assertEqual({key: len(values) for key, values in controls.items()},
                                 {'0x0': 16, '0x4': 20, '0x10': 21, '0x14': 9})
                self.assertEqual(gamma.shape, (256,))
                self.assertEqual(dependencies[0]['sha256'], 'input-hash')


if __name__ == '__main__':
    unittest.main()
