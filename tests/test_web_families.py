"""Version selection, migration failures and native-effect saves in real JS handlers."""
from pathlib import Path
import shutil
import subprocess
import unittest


class WebFirmwareFamilyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for JavaScript UI verification")
    def test_version_selection_upgrade_and_effect_workflows(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), str(root / "tests/fixtures/web_families_probe.cjs"), str(root / "web")],
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"status":"passed"', result.stdout)


if __name__ == "__main__":
    unittest.main()
