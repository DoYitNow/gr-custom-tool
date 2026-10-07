# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Offline update-container fields and additive checksums."""
from array import array
import hashlib
import struct
import sys


def sum32(data):
    if len(data) % 4:
        raise ValueError('32-bit additive checksum requires a word-aligned length')
    words = array('I')
    words.frombytes(data)
    if sys.byteorder != 'little':
        words.byteswap()
    return sum(words) & 0xFFFFFFFF

def repair_last_word(data):
    result = bytearray(data)
    if len(result) < 4 or len(result) % 4:
        raise ValueError('invalid checksum-bearing buffer length')
    struct.pack_into('<I', result, len(result)-4, (-sum32(result[:-4])) & 0xFFFFFFFF)
    return bytes(result)

def inspect_container(data):
    if len(data) < 152 or len(data) % 4:
        raise ValueError('container is too short or not word aligned')
    footer = struct.unpack_from('<6I', data, len(data)-24)
    project = struct.unpack_from('<I', data, 48)[0]
    repeated, marker = struct.unpack_from('<II', data, 120)
    encoded_size, decoded_size = footer[3:5]
    payload_end = 128 + encoded_size
    if payload_end > len(data)-24:
        raise ValueError('declared encoded payload exceeds container')
    metadata_start = (payload_end+3) & ~3
    metadata = data[metadata_start:-24]
    count = struct.unpack_from('<I', data, 60)[0]
    records = []
    if len(metadata) == count*128:
        for i in range(count):
            record = metadata[i*128:(i+1)*128]
            records.append(dict(project=struct.unpack_from('<I', record)[0],
                                version_pair=list(struct.unpack_from('>HH', record, 4))))
    return dict(length=len(data), project=project, header_project_copy=repeated,
        header_marker=marker, footer_project_copy=footer[0], footer_marker=footer[1],
        firmware_type=footer[2], encoded_size=encoded_size, decoded_size=decoded_size,
        footer_checksum=footer[5], additive_sum32=sum32(data),
        payload_end=payload_end, alignment_length=metadata_start-payload_end,
        metadata_start=metadata_start, metadata_length=len(metadata),
        header_record_count=count, metadata_records=records,
        sha256=hashlib.sha256(data).hexdigest())
