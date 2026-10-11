"""Calibration jobs keep offline and Lightroom render histories separate."""

from contextlib import ExitStack
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor import calibration
from gr4_editor.store import sha256, write_json


class CalibrationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.editor = calibration.Calibration(self.root)
        self.xmp = self.root / 'source.xmp'
        self.xmp.write_bytes(b'test-look')
        self.dataset = {'schema_version': 1, 'heldout': 'C', 'registered_at': 'fixed', 'samples': []}
        for stem in ('A', 'B', 'C'):
            path = self.root / f'{stem}.DNG'
            payload = stem.encode()
            path.write_bytes(payload)
            self.dataset['samples'].append({'name': path.name, 'path': str(path), 'sha256': sha256(payload),
                                            'role': 'holdout' if stem == 'C' else 'development'})

    def _mocks(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(self.editor, 'dataset', return_value=self.dataset))
        stack.enter_context(patch.object(calibration, 'inspect_xmp', return_value={
            'sha256': sha256(self.xmp.read_bytes()), 'renderable': True, 'kind': 'profile',
            'unsupported_fields': []}))
        stack.enter_context(patch('gr4_editor.offline_look.load_base_profile', return_value=object()))
        stack.enter_context(patch('gr4_editor.offline_look.inspect_support', return_value={
            'supported': True, 'recipe_version': 7}))
        stack.enter_context(patch('gr4_editor.lightroom_bridge.profile_settings', return_value={'uuid': 'test'}))
        stack.enter_context(patch('gr4_editor.calibration_assets.load_gamma_base', return_value=([], {})))

    def test_engine_specific_cache_and_recipe(self):
        self._mocks()
        offline = self.editor.create_job('project', 'filter', self.xmp, 'offline')
        lightroom = self.editor.create_job('project', 'filter', self.xmp, 'lightroom')
        self.assertNotEqual(offline['id'], lightroom['id'])
        self.assertEqual(offline['status'], 'awaiting_offline')
        self.assertEqual(offline['render_recipe_version'], 7)
        self.assertEqual(lightroom['status'], 'awaiting_lightroom')
        self.assertEqual(lightroom['render_recipe_version'], 3)
        self.assertEqual(self.editor.create_job('project', 'filter', self.xmp, 'offline'), offline)
        self.assertEqual(self.editor.create_job('project', 'filter', self.xmp, 'lightroom'), lightroom)

    def test_lightroom_request_and_poll_only_advance_on_valid_receipt(self):
        self._mocks()
        job = self.editor.create_job('project', 'filter', self.xmp, 'lightroom')
        nonce = 'a' * 32
        with patch('gr4_editor.lightroom_bridge.install_plugin'), patch(
                'gr4_editor.lightroom_bridge.prepare_request', return_value={'nonce': nonce}), patch(
                'gr4_editor.lightroom_bridge.complete_request', return_value=None), patch(
                'gr4_editor.lightroom_bridge.status', return_value={'lightroom_bridge': 'not_connected'}):
            queued = self.editor.start_render(job['id'])
            self.assertEqual(queued['status'], 'awaiting_lightroom')
            self.assertEqual(queued['render_nonce'], nonce)
            waiting = self.editor.poll_render(job['id'])
            self.assertEqual(waiting['status'], 'awaiting_lightroom')
        receipt = {'status': 'rendered', 'render_engine': 'lightroom', 'render_recipe_version': 3,
                   'nonce': nonce, 'boundary': 'Lightroom exported target'}
        with patch('gr4_editor.lightroom_bridge.complete_request', return_value=receipt):
            rendered = self.editor.poll_render(job['id'])
        self.assertEqual(rendered['status'], 'rendered')
        self.assertFalse(rendered['target_approximate'])
        self.assertTrue(rendered['approximate'])  # The camera fit is still an estimate.

    def test_fit_requires_engine_specific_target_and_nonce(self):
        self._mocks()
        job = self.editor.create_job('project', 'filter', self.xmp, 'lightroom')
        folder = self.root / 'calibration' / 'jobs' / job['id']
        job['render_nonce'] = 'b' * 32
        write_json(folder / 'job.json', job)
        receipt = {'status': 'rendered', 'job_id': job['id'], 'xmp_sha256': job['xmp_sha256'],
                   'render_engine': 'lightroom', 'render_recipe_version': 3,
                   'nonce': 'c' * 32, 'application': {'name': 'Classic'}}
        write_json(folder / 'render-receipt.json', receipt)
        for stem in ('A', 'B', 'C'):
            (Path(job['photos']) / f'{stem}.jpg').write_bytes(b'jpeg fixture')
        with self.assertRaisesRegex(ValueError, '本次请求'):
            self.editor.fit_job(job['id'])
        receipt['nonce'] = job['render_nonce']
        write_json(folder / 'render-receipt.json', receipt)
        with patch('gr4_editor.fitting.run_job', return_value={'resources': {}, 'fit_report': {}}):
            fitted = self.editor.fit_job(job['id'])
        self.assertEqual(fitted['status'], 'fitted_offline')
        self.assertFalse(fitted['target_approximate'])
        self.assertTrue(fitted['approximate'])
        validation = json.loads((self.root / 'calibration' / 'bridge-validation.json').read_text(encoding='utf-8'))
        self.assertEqual(validation['render_engine'], 'lightroom')
        self.assertEqual(validation['render_recipe_version'], 3)


if __name__ == '__main__':
    unittest.main()
