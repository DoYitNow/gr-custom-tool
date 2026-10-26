"""HTTP forwards family selection, upgrade payloads and explicit feature flags."""
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.multiversion import MultiVersionEditorMixin, native_effects
from gr4_editor.server import make_handler
from gr4_editor.service import Editor


class MultiVersionHttpTests(unittest.TestCase):
    def setUp(self):
        self.editor = Mock()
        self.editor.import_firmware.return_value = {'id': 'imported'}
        self.editor.upgrade_firmware.return_value = {'id': 'target120', 'migration': {'source_project_id': 'source111'}}
        self.editor.save.return_value = {'id': 'project120', 'saved': True}
        self.editor.build.return_value = {'id': 'build120'}
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.editor))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, body=b'synthetic-bin', headers=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            connection.request('POST', path, body, headers or {'Content-Type': 'application/octet-stream'})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_firmware_import_forwards_explicit_family_and_raw_bytes(self):
        status, result = self.request('/api/import?name=official120.bin&family=1.20', b'official120-bytes')
        self.assertEqual(status, 200)
        self.assertEqual(result['id'], 'imported')
        self.editor.import_firmware.assert_called_once_with(b'official120-bytes', 'official120.bin', expected_family='1.20')

    def test_old_import_clients_default_to_automatic_family(self):
        status, _ = self.request('/api/import', b'legacy111-bytes')
        self.assertEqual(status, 200)
        self.editor.import_firmware.assert_called_once_with(b'legacy111-bytes', 'fwdc248b.bin', expected_family='auto')

    def test_import_family_mismatch_is_reported_without_success(self):
        self.editor.import_firmware.side_effect = ValueError('选择的是 1.11，文件实际为 1.20')
        status, result = self.request('/api/import?family=1.11', b'official120-bytes')
        self.assertEqual(status, 400)
        self.assertIn('实际为 1.20', result['error'])

    def test_upgrade_transmits_source_project_and_user_supplied_target_bin(self):
        status, result = self.request('/api/upgrade?project_id=source111&name=target120.bin', b'target120-bytes')
        self.assertEqual(status, 200)
        self.assertEqual(result['migration']['source_project_id'], 'source111')
        self.editor.upgrade_firmware.assert_called_once_with('source111', b'target120-bytes', 'target120.bin')
        self.editor.import_firmware.assert_not_called()

    def test_upgrade_failure_does_not_return_a_candidate(self):
        self.editor.upgrade_firmware.side_effect = ValueError('请选择官方原版 1.20')
        status, result = self.request('/api/upgrade?project_id=source111', b'custom-target')
        self.assertEqual(status, 400)
        self.assertIn('官方原版', result['error'])
        self.assertNotIn('download_url', result)
        self.editor.build.assert_not_called()

    def test_save_passes_both_states_of_pictorialist_switch(self):
        for enabled in (False, True):
            with self.subTest(pictorialist=enabled):
                draft = {'schema_version': 1, 'pictorialist': enabled}
                payload = {'project_id': 'project120', 'title': 'Saved', 'filters': [],
                           'native_effects_draft': draft, 'build_options': {'allow_older': False}}
                status, result = self.request('/api/project', json.dumps(payload).encode())
                self.assertEqual(status, 200)
                self.assertTrue(result['saved'])
                self.assertEqual(self.editor.save.call_args.kwargs['native_effects_draft'], draft)
                self.assertEqual(self.editor.save.call_args.kwargs['build_options'], {'allow_older': False})

    def test_old_saved_projects_without_feature_flag_forward_none(self):
        status, _ = self.request('/api/project', json.dumps({'project_id': 'project111', 'filters': []}).encode())
        self.assertEqual(status, 200)
        self.assertIsNone(self.editor.save.call_args.kwargs['native_effects_draft'])

    def test_build_false_policy_and_requested_family_version_are_not_lost(self):
        status, _ = self.request('/api/build', json.dumps({'project_id': 'project120',
            'version': '1.20.10.19', 'allow_older': False}).encode())
        self.assertEqual(status, 200)
        self.editor.build.assert_called_once_with('project120', '1.20.10.19', False)

    def test_upgrade_missing_source_or_empty_file_is_a_client_error(self):
        for path, body in (('/api/upgrade', b'bin'), ('/api/upgrade?project_id=source111', b'')):
            with self.subTest(path=path, body=body):
                status, result = self.request(path, body)
                self.assertEqual(status, 400)
                self.assertTrue(result['error'])
        self.editor.upgrade_firmware.assert_not_called()

    def test_upgrade_obeys_existing_origin_policy(self):
        status, _ = self.request('/api/upgrade?project_id=source111', b'bin',
                                 {'Origin': 'https://foreign.example'})
        self.assertEqual(status, 403)
        self.editor.upgrade_firmware.assert_not_called()

    def test_both_editor_classes_route_through_shared_mixin(self):
        self.assertIn(MultiVersionEditorMixin, Editor.__mro__)

    def test_feature_schema_rejects_coerced_or_unknown_flags(self):
        for value in ({'pictorialist': 'false'}, {'pictorialist': 1},
                      {'pictorialist': True, 'af_phase': True}, {'schema_version': 2, 'pictorialist': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native_effects(value)
        self.assertEqual(native_effects(), {'schema_version': 1, 'pictorialist': False})


if __name__ == '__main__':
    unittest.main()
