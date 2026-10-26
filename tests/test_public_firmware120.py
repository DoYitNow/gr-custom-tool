"""Portable dual-family container checks using synthetic, non-firmware data."""
from contextlib import ExitStack
from hashlib import sha256
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gr4_editor
from gr4_editor import firmware, update_policy
from gr4_editor.codec.container import repair_last_word
from gr4_editor.codec.frames import _literal_chunks, section_rows
from gr4_editor.versions import allocate_version


FAMILY = (1, 20, 10)
COPY = (0x543B3100, 0x55000000, 0x58500)
LAYOUT = {'family': FAMILY, 'runtime_version': firmware.BASE + 0x24,
          'runtime_records': tuple(firmware.BASE + n for n in (0x20, 0x24, 0x28)),
          'fallback_movw': firmware.BASE + 0x30, 'fallback_mov': firmware.BASE + 0x54}


def fixture(version=0x01140A0B, *, runtime_record=None, rtos_suffix=b''):
    """Complete valid container, with miniature synthetic version/gate sites."""
    rtos = bytearray(0x800)
    struct.pack_into('<3I', rtos, 0x450, *COPY)
    for address in LAYOUT['runtime_records']:
        struct.pack_into('<I', rtos, address - firmware.BASE, version)
    if runtime_record is not None:
        struct.pack_into('<I', rtos, 0x20, runtime_record)
    struct.pack_into('<II', rtos, 0x30, 0xE3000A00 | (version & 255), 0xE3400114)
    struct.pack_into('<I', rtos, 0x54, 0xE3A00000 | (version & 255))
    gate = bytearray(update_policy.GATE_BYTES)
    for address, original, *_ in update_policy.COMPARISONS120:
        struct.pack_into('<I', gate, address - update_policy.GATE120_ENTRY, original)
    rtos[0x480:0x480 + len(gate)] = gate
    rtos.extend(rtos_suffix)
    dsp = bytearray(0xA0)
    struct.pack_into('>I', dsp, 0x90, version)
    struct.pack_into('>I', dsp, 0x7C, (-sum(x[0] for x in struct.iter_unpack('>I', dsp))) & 0xFFFFFFFF)
    sric = bytearray(0x83DC)
    struct.pack_into('<I', sric, 0x83D4, version)
    sric = repair_last_word(sric)

    def section(name, payload):
        return name.encode().ljust(8, b'\0') + struct.pack('<II', 4, len(payload)) + payload

    names = ('BOOTPARA', 'DRAMPARA', 'PARTPARA', 'M0', 'BOOT', 'RTOS', 'LINUX',
             'INITFS', 'SRIC', 'CPU', 'SHELLCMD', 'ICONBIN', 'RES')
    payloads = {'RTOS': bytes(rtos), 'SRIC': sric, 'CPU': bytes(dsp),
                'ICONBIN': b'ICON' * 4, 'RES': b'RES!'}
    decoded = repair_last_word(b''.join(section(name, payloads.get(name, name[:4].encode().ljust(4, b'!')))
                                        for name in names) + bytes(4))
    payload, _ = _literal_chunks(decoded)
    payload += bytes(2)
    header = bytearray(128)
    struct.pack_into('<I', header, 0x30, 636)
    struct.pack_into('>I', header, 0x38, version)
    struct.pack_into('<I', header, 0x3C, 3)
    struct.pack_into('<I', header, 0x4C, version)
    struct.pack_into('>I', header, 0x50, version)
    struct.pack_into('<II', header, 0x78, 0x027C027C, 0xA55A5AA5)
    output = header + payload
    output.extend(bytes(-len(output) % 4))
    for project in (636, 650, 636):
        record = bytearray(128)
        struct.pack_into('<I', record, 0, project)
        struct.pack_into('>I', record, 4, version)
        output.extend(record)
    output.extend(struct.pack('<6I', 0x027C027C, 0xA55A5AA5, 2, len(payload), len(decoded), 0))
    return repair_last_word(output), bytes(rtos), sha256(gate).hexdigest()


class Public120ContainerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data, self.rtos, gate_sha = fixture()
        self.stack.enter_context(patch.dict(firmware.STARTUP_LAYOUTS, {COPY: LAYOUT}))
        comparisons = tuple((firmware.BASE + 0x480 + address - update_policy.GATE120_ENTRY, *rest)
                            for address, *rest in update_policy.COMPARISONS120)
        self.stack.enter_context(patch.object(update_policy, 'GATE120_ENTRY', firmware.BASE + 0x480))
        self.stack.enter_context(patch.object(update_policy, 'COMPARISONS120', comparisons))
        self.stack.enter_context(patch.object(update_policy, 'FACTORY_GATE120_SHA256', gate_sha))
        self.official = patch.object(firmware, '_history_identity', return_value={'kind': 'official', 'version': '1.20.10.11'})
        self.stack.enter_context(self.official)
        self.backend = SimpleNamespace(
            supports_image=lambda rtos: rtos == self.rtos,
            metadata=lambda rtos: None,
            read_filters120=lambda image: [{'id': 'native120', 'version': list(image.version)}],
            prepare_update_policy=lambda rtos, allow_older=True:
                (update_policy.apply_update_policy(rtos) if allow_older else update_policy.remove_update_policy(rtos)))
        self.stack.enter_context(patch.object(gr4_editor, 'product120', self.backend, create=True))
        self.stack.enter_context(patch.dict(sys.modules, {'gr4_editor.product120': self.backend}))

    def test_official120_import_reads_matching_versions_and_family(self):
        image = firmware._load_bytes(self.data)
        self.assertTrue(image.editable)
        self.assertEqual(image.version, (1, 20, 10, 11))
        self.assertEqual(image.layout_proof['layout_family'], [1, 20, 10])
        self.assertTrue(image.layout_proof['backend_source_verified'])
        self.assertEqual(firmware.inspect_firmware(image)['public_version'], '1.20')
        self.assertEqual(firmware.read_filters(image)[0]['id'], 'native120')

    def test_all_three_cpu_runtime_records_must_match(self):
        data, _, _ = fixture(runtime_record=0x01140A0C)
        with self.assertRaisesRegex(firmware.FirmwareError, 'inconsistent component version'):
            firmware._load_bytes(data)

    def test_unregistered120_and_research_footer_stay_readonly(self):
        self.backend.supports_image = lambda rtos: False
        for suffix in (b'', b'GR4R1201' + bytes(8)):
            with self.subTest(suffix=suffix):
                image = firmware._load_bytes(fixture(rtos_suffix=suffix)[0])
                self.assertFalse(image.editable)
                self.assertFalse(image.layout_proof.get('generic_passthrough', False))
                with self.assertRaises(firmware.FirmwareError):
                    firmware.repack_image(image, image.rtos + suffix)

    def test_verified_product_metadata_is_editable_without_official_package_identity(self):
        self.backend.metadata = lambda rtos: {'schema_version': 1, 'compiler_base_sha256': 'synthetic'}
        with patch.object(firmware, '_history_identity', return_value=None):
            image = firmware._load_bytes(self.data)
        self.assertTrue(image.editable)
        self.assertTrue(image.layout_proof['editor_registry_verified'])
        self.assertEqual(image.layout_proof['compiler_base_rtos_sha256'], 'synthetic')
        _, report = firmware.repack_image(image, image.rtos, allow_older=False)
        self.assertFalse(report['requires_local_state_manifest_for_future_editing'])

    def test_native_rtos_in_an_unknown_full_container_is_not_official(self):
        with patch.object(firmware, '_history_identity', return_value=None):
            image = firmware._load_bytes(self.data)
        self.assertFalse(image.editable)
        self.assertFalse(image.layout_proof['backend_source_verified'])

    def test_factory_noop_is_byte_exact(self):
        image = firmware._load_bytes(self.data)
        output, report = firmware.repack_image(image, image.rtos, allow_older=False)
        self.assertEqual(output, self.data)
        self.assertEqual(report['update_policy']['policy'], 'factory')

    def test_version_and_optional_policy_roundtrip_preserve_other_sections(self):
        image = firmware._load_bytes(self.data)
        for enabled in (False, True):
            with self.subTest(allow_older=enabled):
                output, report = firmware.repack_image(image, image.rtos,
                    new_version='1.20.10.12', allow_older=enabled)
                result = firmware._load_bytes(output)
                self.assertEqual(result.version, (1, 20, 10, 12))
                self.assertEqual(result.layout_proof['update_policy']['installed'], enabled)
                self.assertFalse(result.editable)  # Version-only output has no product recipe.
                self.assertEqual(result.icon_bytes, image.icon_bytes)
                for name in ('M0', 'BOOT', 'LINUX', 'INITFS', 'SHELLCMD', 'RES'):
                    self.assertIn(name, report['preserved_sections'])
                original = {row['name']: image.decoded[row['start']:row['end']] for row in image.sections}
                for row in result.sections:
                    if row['name'] not in ('RTOS', 'SRIC', 'CPU'):
                        self.assertEqual(result.decoded[row['start']:row['end']], original[row['name']])

    def test_version_fields_cannot_cross_families_or_change_unknown_words(self):
        image = firmware._load_bytes(self.data)
        with self.assertRaisesRegex(firmware.FirmwareError, 'layout family'):
            firmware.repack_image(image, image.rtos, new_version='1.11.10.99')
        corrupted = bytearray(image.rtos)
        struct.pack_into('<I', corrupted, 0x28, 0xDEADBEEF)
        with self.assertRaisesRegex(firmware.FirmwareError, 'version instruction'):
            firmware._patch_rtos_version(corrupted, 0x01140A0B, 0x01140A0C)

    def test_numbering_is_separate_for_both_families(self):
        ledger = [{'version': '1.11.10.250'}, {'version': '1.20.10.18'}]
        self.assertEqual(allocate_version('1.20.10.11', ledger), '1.20.10.19')
        self.assertEqual(allocate_version('1.11.10.7', ledger), '1.11.10.251')

    def test_version_patch_reseals_a_product_footer_checksum(self):
        rtos = repair_last_word(self.rtos + b'GR4P1201' + bytes(8))
        updated = firmware._patch_rtos_version(rtos, 0x01140A0B, 0x01140A0C)
        self.assertEqual(sum(word[0] for word in struct.iter_unpack('<I', updated)) & 0xFFFFFFFF, 0)
        self.assertEqual(updated[-16:-8], b'GR4P1201')


if __name__ == '__main__':
    unittest.main()
