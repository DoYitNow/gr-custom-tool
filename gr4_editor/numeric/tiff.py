"""Small read-only TIFF/MakerNote value helpers."""
import struct


def unpack(data, offset, fmt, endian):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise ValueError("Truncated TIFF/IFD data")
    return struct.unpack_from(endian + fmt, data, offset)


def ifd(data, offset, endian):
    count = unpack(data, offset, "H", endian)[0]
    if offset + 2 + count * 12 + 4 > len(data):
        raise ValueError("Truncated IFD entries or next-IFD pointer")
    result = {}
    for index in range(count):
        pos = offset + 2 + index * 12
        tag, kind, amount = unpack(data, pos, "HHI", endian)
        if tag in result:
            raise ValueError("Duplicate IFD tag")
        result[tag] = (kind, amount, pos + 8)
    return result


def value(data, entry, endian):
    kind, count, pos = entry
    widths = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}
    if kind not in widths:
        raise ValueError("Unsupported type for requested EXIF field")
    size = count * widths[kind]
    start = pos if size <= 4 else unpack(data, pos, "I", endian)[0]
    if start < 0 or start + size > len(data):
        raise ValueError("Requested EXIF value exceeds data")
    return data[start:start + size]
