"""Read fitting controls from the input firmware's own family-specific tables."""
from contextlib import ExitStack
from hashlib import sha256
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.fitting import _native_context


class FittingFamilyContextTests(unittest.TestCase):
    def fixture(self, family):
        image = SimpleNamespace(rtos=b'fixture RTOS', sha256='firmware-sha',
                                internal_version=family + '.10.11', layout_proof={'fixture': True})
        descriptor = bytearray(128)
        controls = {}
        expected = {}
        for index, (offset, size, fmt) in enumerate(((0, 32, '<16H'), (4, 20, '<20B'),
                                                   (16, 42, '<21H'), (20, 18, '<9H'))):
            address = 0x56000000 + index * 0x100
            struct.pack_into('<I', descriptor, offset, address)
            values = list(range(index, index + struct.calcsize(fmt) // (1 if fmt.endswith('B') else 2)))
            controls[address] = struct.pack(fmt, *values)
            expected[hex(offset)] = values
        memory = Mock()
        memory.read.side_effect = lambda address, size: controls[address][:size]
        bank = {'banks': [{'descriptor_hex': {'multi_axial': descriptor.hex()}}]}
        return image, memory, bank, expected

    def check_family(self, family):
        image, memory, bank, expected = self.fixture(family)
        backend = SimpleNamespace(np=SimpleNamespace(array=lambda value, dtype: tuple(value)))
        with ExitStack() as stack:
            stack.enter_context(patch('gr4_editor.fitting.load_gamma_base', return_value=([1, 2], {'gamma': True})))
            stack.enter_context(patch('gr4_editor.firmware.load_image', return_value=image))
            stack.enter_context(patch('gr4_editor.firmware._Memory', return_value=memory))
            legacy = stack.enter_context(patch('gr4_editor.firmware._color_resources', create=True, return_value=bank))
            current = stack.enter_context(patch('gr4_editor.product120_resources.color120', return_value=bank))
            native, gamma, dependencies = _native_context(backend, {'firmware_path': 'fixture.bin',
                                                                   'firmware_sha256': image.sha256})
        chosen, other = (current, legacy) if family == '1.20' else (legacy, current)
        chosen.assert_called_once_with(memory, 11)
        other.assert_not_called()
        self.assertEqual(native, expected)
        self.assertEqual(gamma, (1, 2))
        self.assertEqual(memory.read.call_count, 4)
        self.assertEqual(dependencies[0]['rtos_sha256'], sha256(image.rtos).hexdigest())
        self.assertEqual(len(dependencies[0]['control_sources']), 4)

    def test_120_uses_120_standard_bank_and_reads_all_four_control_arrays(self):
        self.check_family('1.20')

    def test_111_keeps_the_existing_reader_and_control_contract(self):
        self.check_family('1.11')

    def test_input_hash_mismatch_stops_before_either_reader(self):
        image, memory, bank, _ = self.fixture('1.20')
        with patch('gr4_editor.fitting.load_gamma_base', return_value=([1, 2], {})), \
             patch('gr4_editor.firmware.load_image', return_value=image), \
             patch('gr4_editor.product120_resources.color120') as reader:
            with self.assertRaisesRegex(ValueError, '哈希'):
                _native_context(SimpleNamespace(), {'firmware_path': 'fixture.bin', 'firmware_sha256': 'changed'})
        reader.assert_not_called()

    @unittest.skipUnless(os.environ.get('GR4_TEST_FIRMWARE120'), 'Set GR4_TEST_FIRMWARE120 for actual 1.20 ROM readback')
    def test_actual_120_rom_control_arrays(self):
        from gr4_editor.firmware import load_image, _Memory
        from gr4_editor.product120_resources import color120
        filename = os.environ['GR4_TEST_FIRMWARE120']
        image = load_image(filename)
        self.assertTrue(image.internal_version.startswith('1.20.'))
        memory = _Memory(image.rtos)
        descriptor = bytes.fromhex(color120(memory, 11)['banks'][0]['descriptor_hex']['multi_axial'])
        backend = SimpleNamespace(np=SimpleNamespace(array=lambda value, dtype: tuple(value)))
        with patch('gr4_editor.fitting.load_gamma_base', return_value=([1, 2], {})):
            native, _, dependencies = _native_context(backend, {'firmware_path': filename,
                                                               'firmware_sha256': image.sha256})
        for offset, size, fmt in ((0, 32, '<16H'), (4, 20, '<20B'), (16, 42, '<21H'), (20, 18, '<9H')):
            address = struct.unpack_from('<I', descriptor, offset)[0]
            self.assertEqual(native[hex(offset)], list(struct.unpack(fmt, memory.read(address, size))))
        self.assertEqual(dependencies[0]['sha256'], image.sha256)


if __name__ == '__main__':
    unittest.main()
