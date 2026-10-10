"""Regression checks for the reviewed low-version update policy."""

from pathlib import Path
import struct
import unittest

from gr4_editor.codec.frames import section_rows
from gr4_editor.update_policy import (
    BASE,
    COMPARISONS,
    GATE_BYTES,
    GATE_ENTRY,
    apply_update_policy,
    inspect_update_policy,
    remove_update_policy,
)


ROOT = Path(__file__).resolve().parents[2]
DECODED = ROOT / "data" / "firmware" / "unpackedFW"


def _factory_rtos():
    if not DECODED.is_file():
        return None
    decoded = DECODED.read_bytes()
    rows = section_rows(decoded)
    row = next(row for row in rows if row["name"] == "RTOS")
    return decoded[row["start"] + 16:row["end"]]


class UpdatePolicyValidationTests(unittest.TestCase):
    def test_short_or_unrecognized_buffers_are_rejected(self):
        for data in (b"", bytes(128), bytes(GATE_ENTRY - BASE + GATE_BYTES - 1)):
            with self.subTest(length=len(data)):
                self.assertEqual(inspect_update_policy(data)["status"], "unsupported")
                with self.assertRaises(ValueError):
                    apply_update_policy(data)


@unittest.skipUnless(DECODED.is_file(), "requires the local decoded firmware fixture")
class UpdatePolicyByteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.factory = _factory_rtos()

    def test_exact_eight_instruction_patch_and_inverse(self):
        before = inspect_update_policy(self.factory)
        self.assertEqual((before["status"], before["policy"], before["installed"]),
                         ("verified", "factory", False))
        patched, report = apply_update_policy(self.factory)
        self.assertEqual((report["policy"], report["installed"]), ("allow_older", True))
        self.assertEqual(report["changed_instruction_count"], len(COMPARISONS))
        allowed = {address - BASE + offset
                   for address, *_ in COMPARISONS for offset in range(4)}
        self.assertTrue(all(index in allowed
                            for index, (left, right) in enumerate(zip(self.factory, patched))
                            if left != right))
        restored, inverse = remove_update_policy(patched)
        self.assertEqual(restored, self.factory)
        self.assertEqual((inverse["policy"], inverse["installed"]), ("factory", False))

    def test_reapplying_is_byte_exact_and_gate_is_known(self):
        patched, _ = apply_update_policy(self.factory)
        repeated, report = apply_update_policy(patched)
        self.assertEqual(repeated, patched)
        self.assertEqual(report["changed_instruction_count"], 0)
        self.assertEqual(report["normalized_gate_sha256"],
                         "664636e5d27cec4aed6cbe08c7dc038785628aede04032dccacbe924d7596954")
        for address, _factory, relaxed, *_ in COMPARISONS:
            self.assertEqual(struct.unpack_from("<I", patched, address - BASE)[0], relaxed)
