# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.versions import allocate_version, parse_version, version_word


class VersionTests(unittest.TestCase):
    def test_same_number_branches_do_not_reuse_existing_number(self):
        self.assertEqual(allocate_version("1.11.10.25"), "1.11.10.26")
        self.assertEqual(allocate_version("1.11.10.25", [{"version": "1.11.10.27"}]), "1.11.10.28")
        self.assertEqual(allocate_version("1.11.10.27", [{"version": "1.11.10.31"}]), "1.11.10.32")

    def test_official_and_reserved_low_inputs_start_at_ten(self):
        for suffix in (7, 8, 9):
            with self.subTest(suffix=suffix):
                self.assertEqual(allocate_version(f"1.11.10.{suffix}"), "1.11.10.10")
        self.assertEqual(allocate_version("1.11.10.7", requested="1.11.10.10"), "1.11.10.10")
        for suffix in (8, 9):
            with self.assertRaisesRegex(ValueError, "1.11.10.10"):
                allocate_version("1.11.10.7", requested=f"1.11.10.{suffix}")

    def test_repeated_builds_of_the_same_baseline_increment_local_record(self):
        ledger = []
        for expected in (10, 11, 12):
            version = allocate_version("1.11.10.7", ledger)
            self.assertEqual(version, f"1.11.10.{expected}")
            ledger.append({"version": version})
        self.assertEqual(allocate_version("1.11.10.12", ledger), "1.11.10.13")

    def test_internal_history_is_not_read_or_used_in_fresh_allocation(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read research history")):
            self.assertEqual(allocate_version("1.11.10.7"), "1.11.10.10")
            self.assertEqual(allocate_version("1.11.10.40"), "1.11.10.41")

    def test_local_higher_number_wins_and_other_family_is_ignored(self):
        self.assertEqual(allocate_version("1.11.10.7", [{"version": "1.11.10.54"}]), "1.11.10.55")
        self.assertEqual(allocate_version("1.11.10.7", [{"version": "1.12.10.200"}]), "1.11.10.10")
        with self.assertRaisesRegex(ValueError, "1.11.10.55"):
            allocate_version("1.11.10.7", [{"version": "1.11.10.54"}], "1.11.10.10")

    def test_invalid_and_occupied_numbers_rejected(self):
        for value in ("1.11.10.25", "1.11.10.27", "1.11.11.0"):
            with self.assertRaises(ValueError):
                allocate_version("1.11.10.27", requested=value)
        with self.assertRaises(ValueError):
            parse_version("1.11.10.256")

    def test_no_silent_build_byte_wrap(self):
        with self.assertRaises(ValueError):
            allocate_version("1.11.10.255")
        self.assertEqual(version_word("1.11.10.28"), 0x010B0A1C)


if __name__ == "__main__":
    unittest.main()
