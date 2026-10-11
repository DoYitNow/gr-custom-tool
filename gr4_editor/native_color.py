"""Read-only native color resource adapter for the public firmware parser.

The public firmware module owns container import/repack policy.  This small
adapter keeps the research fitter's native MultiAxial dependency separate: it
only reads the verified static table pointers and never writes firmware.
"""
from __future__ import annotations

from hashlib import sha256
import struct

from .firmware import FirmwareError, _Memory

# Addresses are the GR4 static profile table probes already used by the
# research reader. They are read-only; an unknown instruction is rejected.
TABLES = (
    ('color_matrix', 0x53925F98, 0x53925F9C, 0x53925FA4),
    ('multi_axial', 0x53925FC4, 0x53925FC8, 0x53925FD0),
    ('member_16c', 0x53925FF0, 0x53925FF4, 0x53925FFC),
    ('member_188', 0x5392601C, 0x53926020, 0x53926028),
)


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _blob(memory: _Memory, address: int, size: int) -> dict:
    data = memory.read(address, size)
    return {'address': address, 'bytes': size, 'hex': data.hex(), 'sha256': _sha(data)}


def read_color_resources(memory: _Memory, profile: int) -> dict:
    """Read the three hardware banks for one native profile index.

    This mirrors only the descriptor/resource extraction needed by the local
    fitter. It does not claim that the addresses or RGB semantics are valid
    for an unregistered firmware layout.
    """
    tables = []
    for name, stride, low, high in TABLES:
        instruction = memory.word(stride)
        if instruction & 0xFFFFFF00 != 0xE3A02000:
            raise FirmwareError('unknown native profile table stride')
        columns = instruction & 255
        if not 1 <= columns <= 255 or not 0 <= profile < columns:
            raise FirmwareError('profile index is outside the native table')
        tables.append((name, columns, memory.pair(low, high)))
    banks = []
    for hardware in range(3):
        descriptors, pointers = {}, {}
        for name, columns, table in tables:
            ptr = memory.word(table + (hardware * columns + profile) * 4)
            pointers[name] = ptr
            descriptors[name] = memory.read(ptr, 128).hex()
        multi = bytes.fromhex(descriptors['multi_axial'])
        gamma = bytes.fromhex(descriptors['member_188'])
        main_wrapper, aux_wrapper = struct.unpack_from('<2I', multi, 8)
        channels = [_blob(memory, struct.unpack_from('<I', gamma, 8 + i * 4)[0], 512)
                    for i in range(3)]
        gamma_bytes = b''.join(bytes.fromhex(row['hex']) for row in channels)
        matrix = bytes.fromhex(descriptors['color_matrix'])
        banks.append({
            'hardware_index': hardware,
            'descriptor_hex': descriptors,
            'descriptor_addresses': pointers,
            'camera_basis': _blob(memory, struct.unpack_from('<I', matrix)[0], 18),
            'matrix': _blob(memory, struct.unpack_from('<I', matrix, 4)[0], 18),
            'multi_main_wrapper': _blob(memory, main_wrapper, 20),
            'multi_aux_wrapper': _blob(memory, aux_wrapper, 20),
            'multi_main': _blob(memory, memory.word(main_wrapper + 16), 2160),
            'multi_aux': _blob(memory, memory.word(aux_wrapper + 16), 1440),
            'gamma': {'channels': channels, 'bytes': len(gamma_bytes),
                      'hex': gamma_bytes.hex(), 'sha256': _sha(gamma_bytes)},
        })
    return {'banks': banks, 'native_table_columns': [row[1] for row in tables],
            'member_16c_semantics': 'unknown; exact descriptor bytes retained'}


# Alias retained for the fitter's historical call site while keeping a public
# name that makes the read-only boundary explicit.
_color_resources = read_color_resources
