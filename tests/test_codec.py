# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Synthetic frame reuse and container rebuilding; no firmware fixture."""
import struct
import unittest

from gr4_editor.codec.container import inspect_container, repair_last_word, sum32
from gr4_editor.codec.frames import FrameError, decode_payload, framed_container, section_rows
from gr4_editor.firmware import _repack_frames


def section(name, payload, flags=4, count=None):
    return name.encode().ljust(8, b'\0') + struct.pack(
        '<II', flags, len(payload) if count is None else count) + payload


def stream(extra=b'', seed=b'A'):
    return repair_last_word(section('BOOT', b'BOOT') +
        section('RTOS', seed * 36 + b'last' + extra) +
        section('ICONBIN', b'Z' * 24) + section('RES', b'RES!', count=1) + bytes(4))


def literal(data):
    return struct.pack('>H', 0x8000 | len(data)) + data


def match12():
    body = bytes.fromhex('c000070c020000')
    return struct.pack('>H', len(body)) + body


def container(decoded):
    header = bytearray(128)
    struct.pack_into('<I', header, 48, 636)
    struct.pack_into('<I', header, 60, 1)
    struct.pack_into('<II', header, 120, 0x027C027C, 0xA55A5AA5)
    row = next(row for row in section_rows(decoded) if row['name'] == 'RTOS')
    start = row['start'] + 16
    payload = literal(decoded[:start + 12]) + match12() + match12() + literal(decoded[start + 36:]) + b'\0\0'
    result = header + payload
    result.extend(bytes((-len(result)) % 4))
    result.extend(bytes(128))
    result.extend(struct.pack('<6I', 0x027C027C, 0xA55A5AA5, 2, len(payload), len(decoded), 0))
    return repair_last_word(result)


class FrameTests(unittest.TestCase):
    def test_noop_keeps_cross_frame_back_references(self):
        decoded = stream()
        source = container(decoded)
        payload, report = _repack_frames(source, decoded, decoded, False)
        self.assertEqual(decode_payload(payload, len(decoded))[0], decoded)
        self.assertEqual(framed_container(source, payload, decoded), source)
        self.assertTrue(report['fresh_history_roundtrip_exact'])

    def test_changed_seed_and_large_growth_replay_against_new_output(self):
        original = stream()
        source = container(original)
        for target in (stream(seed=b'B'), stream(extra=b'new!' * 15000)):
            payload, report = _repack_frames(source, original, target, len(original) != len(target))
            decoded, frames = decode_payload(payload, len(target))
            self.assertEqual(decoded, target)
            self.assertTrue(all(frame.decoded_end - frame.decoded_start <= 0x6000
                                for frame in frames if frame.literal))
            result = framed_container(source, payload, target)
            self.assertEqual(sum32(result), 0)
            self.assertEqual(inspect_container(result)['decoded_size'], len(target))
            self.assertTrue(report['fresh_history_roundtrip_exact'])

    def test_malformed_streams_reject_boundary_violations(self):
        cases = [('80000000', 0), ('80', 0), ('800241', 2),
                 ('000200000000', 1), ('0004800000010000', 3),
                 ('000580000000410000', 0), ('000000', 0), ('800241420000', 1)]
        for encoded, size in cases:
            with self.subTest(encoded=encoded), self.assertRaises(FrameError):
                decode_payload(bytes.fromhex(encoded), size)

    def test_unrequested_section_edits_reject(self):
        original = stream()
        source = container(original)
        target = bytearray(stream(extra=b'new!'))
        target[16] ^= 1
        with self.assertRaisesRegex(ValueError, 'unexpected section'):
            _repack_frames(source, original, repair_last_word(target), True)


if __name__ == '__main__':
    unittest.main()
