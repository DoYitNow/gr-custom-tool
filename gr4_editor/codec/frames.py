# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Strict literal/back-reference decoding and frame reuse."""
from dataclasses import dataclass
import struct

from .container import inspect_container, repair_last_word, sum32


class FrameError(ValueError):
    pass

@dataclass(frozen=True)
class Frame:
    encoded_start: int
    encoded_end: int
    decoded_start: int
    decoded_end: int
    literal: bool

def _decode_body(body, literal, output, output_limit):
    """Append one frame to an existing history, checking every input boundary."""
    if literal:
        if len(output) + len(body) > output_limit:
            raise FrameError('literal frame exceeds declared decoded length')
        output.extend(body)
        return
    position = 0
    size = len(body)

    def take(count):
        nonlocal position
        if position + count > size:
            raise FrameError('compressed token exceeds frame boundary')
        value = body[position:position + count]
        position += count
        return value

    while position < size:
        flags = int.from_bytes(take(2), 'big')
        for bit in range(15, -1, -1):
            if not (flags & (1 << bit)):
                if len(output) >= output_limit:
                    raise FrameError('literal token exceeds declared decoded length')
                output.extend(take(1))
                continue
            first, second = take(2)
            offset = ((first & 0xF8) << 5) | second
            length = first & 7
            if length == 7:
                length += take(1)[0]
            if offset == 0:
                if position != size:
                    raise FrameError('frame terminator is followed by encoded bytes')
                return
            if length == 262:
                extension = 255
                while extension == 255:
                    extension = take(1)[0]
                    length += extension
            length += 3
            if offset > len(output):
                raise FrameError('back-reference precedes decoded history')
            if len(output) + length > output_limit:
                raise FrameError('back-reference exceeds declared decoded length')
            # Repetition implements overlapping copies without a byte-at-a-time
            # loop and uses the existing history even across frame boundaries.
            pattern = bytes(output[len(output) - offset:len(output)])
            repeats, tail = divmod(length, offset)
            output.extend(pattern * repeats)
            output.extend(pattern[:tail])
    raise FrameError('compressed frame has no end marker')

def decode_payload(payload, decoded_size):
    output = bytearray()
    frames = []
    position = 0
    while True:
        if position + 2 > len(payload):
            raise FrameError('missing stream terminator')
        start = position
        prefix = int.from_bytes(payload[position:position + 2], 'big')
        position += 2
        if prefix == 0:
            if position != len(payload):
                raise FrameError('bytes after stream terminator')
            if len(output) != decoded_size:
                raise FrameError('decoded length differs from footer')
            return bytes(output), frames
        size = prefix & 0x7FFF
        if size == 0:
            raise FrameError('zero-length nonterminal frame')
        end = position + size
        if end > len(payload):
            raise FrameError('frame exceeds encoded payload')
        output_start = len(output)
        literal = bool(prefix & 0x8000)
        _decode_body(payload[position:end], literal, output, decoded_size)
        frames.append(Frame(start, end, output_start, len(output), literal))
        position = end

def section_rows(decoded):
    rows = []
    position = 0
    while position + 16 <= len(decoded) - 4:
        name = decoded[position:position + 8].split(b'\0', 1)[0].decode('ascii')
        flags, length = struct.unpack_from('<II', decoded, position + 8)
        if name == 'RES':
            rows.append(dict(name=name, start=position, end=len(decoded) - 4,
                             flags=flags, resource_count=length))
            if len(decoded) % 4 or sum32(decoded) != 0:
                raise ValueError('decoded stream checksum is invalid')
            return rows
        end = position + 16 + length
        if end > len(decoded) - 4 or length % 4:
            raise ValueError('invalid section length')
        rows.append(dict(name=name, start=position, end=end, flags=flags,
                         payload_bytes=length))
        position = end
    raise ValueError('missing RES section')

def _aligned_target(original, target):
    """Remove appended RTOS/ICONBIN bytes to retain stock frame coordinates."""
    old_rows = section_rows(original)
    new_rows = section_rows(target)
    if [r['name'] for r in old_rows] != [r['name'] for r in new_rows]:
        raise ValueError('section names or order changed')
    aligned = bytearray()
    insertions = {}
    changes = []
    for old, new in zip(old_rows, new_rows):
        if old['flags'] != new['flags']:
            raise ValueError('section flags changed')
        if old['name'] == 'RES':
            if old['resource_count'] != new['resource_count']:
                raise ValueError('resource count changed')
            if original[old['start']:old['end']] != target[new['start']:new['end']]:
                raise ValueError('RES section changed')
            aligned.extend(target[new['start']:new['end']])
            continue
        old_length = old['end'] - old['start']
        new_length = new['end'] - new['start']
        if new_length < old_length:
            raise ValueError('section shrink is unsupported')
        if old['name'] not in ('RTOS', 'ICONBIN'):
            if original[old['start']:old['end']] != target[new['start']:new['end']]:
                raise ValueError('unexpected section modification: ' + old['name'])
        aligned.extend(target[new['start']:new['start'] + old_length])
        if new_length > old_length:
            inserted = target[new['start'] + old_length:new['end']]
            insertions[old['end']] = inserted
            changes.append(dict(section=old['name'], original_boundary=old['end'],
                                inserted_bytes=len(inserted)))
    aligned.extend(target[-4:])
    if len(aligned) != len(original):
        raise ValueError('section coordinate mapping differs from original length')
    return bytes(aligned), insertions, changes

def _literal_chunks(data):
    encoded = bytearray()
    count = 0
    # The stock file's literal frames all contain 0x6000 decoded bytes. Keep
    # replacement literals at or below that observed size, rather than using
    # the format's larger 0x7fff length field as a hardware compatibility claim.
    for position in range(0, len(data), 0x6000):
        chunk = data[position:position + 0x6000]
        encoded.extend(struct.pack('>H', 0x8000 | len(chunk)))
        encoded.extend(chunk)
        count += 1
    return bytes(encoded), count

def framed_container(template, payload, decoded):
    info = inspect_container(template)
    if sum32(decoded) != 0:
        raise ValueError('decoded checksum is invalid')
    result = bytearray(template[:128])
    result.extend(payload)
    result.extend(bytes((-len(result)) % 4))
    result.extend(template[info['metadata_start']:-24])
    result.extend(struct.pack('<6I', info['footer_project_copy'], info['footer_marker'],
                              info['firmware_type'], len(payload), len(decoded), 0))
    return repair_last_word(result)
