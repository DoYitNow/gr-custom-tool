# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""First-run startup controls, without installing during unit tests."""
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launcher


class LauncherTests(unittest.TestCase):
    def test_check_sets_data_directory_without_starting_server_or_toolchain(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher.sys, 'prefix', str(launcher.VENV)), patch.object(launcher, 'verify_runtime', return_value={}), patch.object(launcher, 'serve') as serve, patch.dict('os.environ', {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.main(['--check', '--data-dir', folder]), 0)
            self.assertEqual(launcher.os.environ['GR4_EDITOR_DATA_DIR'], str(Path(folder).resolve()))
            serve.assert_not_called()

    def test_busy_default_port_uses_actual_free_address(self):
        occupied = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)
        self.addCleanup(occupied.server_close)
        with patch.dict(sys.modules, {'gr4_editor.server': SimpleNamespace(make_handler=lambda editor: BaseHTTPRequestHandler)}):
            server = launcher.local_server(object(), occupied.server_port)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], '127.0.0.1')
        self.assertNotEqual(server.server_port, occupied.server_port)


if __name__ == '__main__':
    unittest.main()
