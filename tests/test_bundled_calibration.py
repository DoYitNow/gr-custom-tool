"""First-run bundled DNGs initialize calibration without replacing user data."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import calibration
from gr4_editor.store import sha256


class BundledCalibrationTests(unittest.TestCase):
    def test_fresh_data_root_uses_three_verified_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            target = calibration.Calibration(folder)
            with ThreadPoolExecutor(max_workers=4) as workers:
                statuses = list(workers.map(lambda _: target.status(), range(4)))
            status = statuses[0]
            self.assertTrue(all(row['dataset_ready'] for row in statuses))
            self.assertTrue(status['dataset_ready'])
            self.assertTrue(status['ready'])
            self.assertEqual(status['dataset_source'], 'bundled')
            self.assertEqual(status['samples'], 3)
            self.assertEqual(status['heldout'], 'R0000398')
            dataset = target.dataset()
            self.assertEqual({row['name'] for row in dataset['samples']},
                             {'R0000289.DNG', 'R0000351.DNG', 'R0000398.DNG'})
            self.assertEqual({row['name'] for row in dataset['samples'] if row['role'] == 'holdout'},
                             {'R0000398.DNG'})
            for row in dataset['samples']:
                path = Path(row['path'])
                self.assertTrue(path.is_relative_to(Path(folder)))
                self.assertEqual(sha256(path.read_bytes()), row['sha256'])
            self.assertEqual(target.dataset()['registered_at'], dataset['registered_at'])

    def test_existing_dataset_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'calibration' / 'dataset.json'
            config.parent.mkdir(parents=True)
            original = '{"sentinel":"keep user choice"}\n'
            config.write_text(original, encoding='utf-8')
            target = calibration.Calibration(folder)
            self.assertIsNone(target.dataset())  # Invalid user data is reported, not overwritten.
            self.assertEqual(config.read_text(encoding='utf-8'), original)
            self.assertFalse((config.parent / 'samples').exists())

    def test_damaged_or_unreadable_bundle_reports_error(self):
        with tempfile.TemporaryDirectory() as folder:
            asset_root = Path(folder) / 'assets'
            sample_root = asset_root / 'samples'
            sample_root.mkdir(parents=True)
            manifest = json.loads(calibration.BUNDLED_CALIBRATION_MANIFEST.read_text(encoding='utf-8'))
            (asset_root / 'samples.json').write_text(json.dumps(manifest), encoding='utf-8')
            (sample_root / 'R0000289.DNG').write_bytes(b'corrupt')
            root = Path(folder) / 'user'
            with patch.object(calibration, 'BUNDLED_CALIBRATION_ROOT', asset_root), patch.object(
                    calibration, 'BUNDLED_CALIBRATION_MANIFEST', asset_root / 'samples.json'):
                target = calibration.Calibration(root)
                status = target.status()
                self.assertFalse(status['dataset_ready'])
                self.assertIn('内置校色样片未就绪', status['message'])
                self.assertIn('SHA256', status['message'])
                self.assertFalse(target.config_path.exists())
                with patch.object(Path, 'read_text', side_effect=PermissionError('permission denied')):
                    status = target.status()
                self.assertFalse(status['dataset_ready'])
                self.assertIn('permission denied', status['message'])


if __name__ == '__main__':
    unittest.main()
