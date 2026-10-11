"""Bundled scalar resources retain their established numeric fingerprints."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import calibration_assets as assets


class CalibrationAssetTests(unittest.TestCase):
    def test_shipped_curves_keep_accepted_fingerprints(self):
        curve, dependency = assets.load_gamma_base()
        self.assertEqual(dependency['base_curve_sha256'], 'bcde9efcbdf6c1fb748ac057a7c9f8a91ea1a9bd1c8c3d03e94c40afa93307e4')
        self.assertEqual(len(curve), 256)
        tone, provenance = assets.load_sdk_tone()
        self.assertEqual(tone['samples_sha256'], '1ae4726011b5bf18a806c1c029a68fe3c56c30b0c9f289d95f23073a86c6fa27')
        self.assertEqual(provenance['sha256'], 'f3f5855b77aea1e7d31b19d89cb0c9bce477bac0d53de307bdb31c1c3c1f37f4')
        samples = json.loads((assets.ASSET_ROOT / 'samples.json').read_text(encoding='utf-8'))
        self.assertEqual({row['name'] for row in samples['samples']},
                         {'R0000289.DNG', 'R0000351.DNG', 'R0000398.DNG'})
        self.assertEqual({path.name for path in assets.ASSET_ROOT.rglob('*') if path.suffix.lower() == '.dng'},
                         {row['name'] for row in samples['samples']})
        self.assertNotIn('source_report', dependency)
        self.assertFalse(dependency['requires_local_file'])
        self.assertTrue(assets.status()['ready'])

    def test_missing_bundle_has_no_research_fallback(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(assets, 'ASSET_ROOT', Path(folder)):
            with self.assertRaisesRegex(ValueError, '缺少程序校色资源'):
                assets.load_gamma_base()
            self.assertFalse(assets.status()['ready'])

    def test_altered_nodes_and_condition_are_rejected(self):
        original = json.loads((assets.ASSET_ROOT / 'constructor-gamma.json').read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory() as folder, patch.object(assets, 'ASSET_ROOT', Path(folder)):
            for field in ('nodes', 'condition'):
                record = deepcopy(original)
                if field == 'nodes':
                    record['base_curve'][100] += 1
                else:
                    record['selected_fixture']['style'] = 11
                (Path(folder) / 'constructor-gamma.json').write_text(json.dumps(record), encoding='utf-8')
                with self.subTest(field=field), self.assertRaises(ValueError):
                    assets.load_gamma_base()

    def test_explicit_tone_override_and_hash_failure(self):
        samples = [(index / 1024.) ** 2 for index in range(1025)]
        record = {'kind': 'dng-sdk-acr3-default', 'samples': samples,
                  'samples_sha256': hashlib.sha256(struct.pack('<1025f', *samples)).hexdigest()}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tone.json'
            path.write_text(json.dumps(record), encoding='utf-8')
            tone, provenance = assets.load_sdk_tone(path)
            self.assertEqual(tone['samples'][512], .25)
            self.assertTrue(provenance['requires_local_file'])
            record['samples'][512] += .0001
            path.write_text(json.dumps(record), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                assets.load_sdk_tone(path)


if __name__ == '__main__':
    unittest.main()
