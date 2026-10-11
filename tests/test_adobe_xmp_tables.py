"""Adobe little-endian Base85 table tails retain complete compressed bytes."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.numeric import xmp as decoder


class AdobeTableTailTests(unittest.TestCase):
    # Fixed Adobe Base85 fixtures: a little-endian size header followed by
    # zlib-compressed bytes(range(size)), with no padding after the final byte.
    FIXTURES = (
        (1, '10000zKRa01690010'),
        (2, '20000KZk$u7r@@0260'),
        (3, '30000KZk$uh7000boA0'),
        (4, '40000KZk$uy[V00lkkl2'),
    )

    def test_partial_groups_preserve_one_two_three_bytes(self):
        decompress = zlib.decompress
        for size, encoded in self.FIXTURES:
            with self.subTest(tail_bytes=size % 4):
                raw = bytes(range(size))
                fingerprint = hashlib.md5(raw).hexdigest().upper()
                self.assertEqual(len(encoded) % 5, size + 1 if size < 4 else 0)
                with patch.object(decoder.zlib, 'decompress', wraps=decompress) as called:
                    self.assertEqual(decoder.decode_table(encoded, fingerprint), raw)
                self.assertEqual(called.call_args.args[0], zlib.compress(raw))

    def test_one_character_tail_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'incomplete base85 group'):
            decoder.decode_table(self.FIXTURES[0][1][:-1], 'unused')

    def test_truncated_tail_does_not_bypass_zlib_validation(self):
        # A 3-character tail truncated to 2 still has a legal group length,
        # but the compressed stream is missing its final checksum byte.
        raw = bytes(range(2))
        fingerprint = hashlib.md5(raw).hexdigest().upper()
        with self.assertRaises(zlib.error):
            decoder.decode_table(self.FIXTURES[1][1][:-1], fingerprint)

    def test_tail_still_checks_decompressed_fingerprint(self):
        with self.assertRaisesRegex(ValueError, 'XMP fingerprint'):
            decoder.decode_table(self.FIXTURES[0][1], '0' * 32)


if __name__ == '__main__':
    unittest.main()
