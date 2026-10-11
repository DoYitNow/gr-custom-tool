"""Adobe enhanced-Look table wire decoding and optional inventory exports.

Format follows the published DNG SDK. This product includes DNG technology
under license by Adobe Systems Incorporated; see licenses/adobe-dng-sdk.
"""
import csv
import hashlib
import struct
import zlib
from pathlib import Path
ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-:+=^!/*?`'|()[]{}@%$#"
DECODE = {char: index for index, char in enumerate(ALPHABET)}
CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
PRIMARIES = ["sRGB", "Adobe RGB", "ProPhoto RGB", "P3", "Rec.2020"]
GAMMA = ["linear", "sRGB", "1.8", "2.2", "Rec.2020"]
GAMUT = ["clip", "extend"]


def decode_table(text: str, fingerprint: str) -> bytes:
    if len(text) % 5 == 1:
        raise ValueError("This XMP table has an incomplete base85 group")
    binary = bytearray()
    for pos in range(0, len(text), 5):
        group = text[pos : pos + 5]
        value = 0
        for power, char in enumerate(group):
            value += DECODE[char] * 85**power
        # Adobe encodes 1/2/3 remaining bytes as 2/3/4 characters.
        count = 4 if len(group) == 5 else len(group) - 1
        binary.extend(struct.pack("<I", value)[:count])
    expected_size = struct.unpack_from("<I", binary)[0]
    raw = zlib.decompress(binary[4:])
    if len(raw) != expected_size:
        raise ValueError("Decompressed table length differs from its header")
    if hashlib.md5(raw).hexdigest().upper() != fingerprint:
        raise ValueError("Decoded table differs from its XMP fingerprint")
    return raw


def export_look(raw: bytes, output: Path) -> dict:
    kind, version, hue, saturation, value = struct.unpack_from("<5I", raw)
    if (kind, version) != (0, 1):
        raise ValueError(f"Unsupported LookTable kind/version: {kind}/{version}")
    count = hue * saturation * value
    end = 20 + count * 12
    if len(raw) != end + 4:
        raise ValueError("Unexpected LookTable length")
    encoding = struct.unpack_from("<I", raw, end)[0]
    with Path(output).open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["hue_index", "sat_index", "val_index", "hue_shift_degrees", "sat_scale", "val_scale"])
        for h in range(hue):
            for s in range(saturation):
                for v in range(value):
                    # dng_hue_sat_map stores saturation fastest, then hue, then value.
                    index = (v * hue + h) * saturation + s
                    writer.writerow([h, s, v, *struct.unpack_from("<3f", raw, 20 + index * 12)])
    return {
        "type": "HSV LookTable",
        "grid": [hue, saturation, value],
        "encoding": {0: "linear", 1: "sRGB"}.get(encoding, encoding),
        "decoded_bytes": len(raw),
        "csv": output.name,
    }


def export_rgb(raw: bytes, output: Path) -> dict:
    kind, version, dimensions, divisions = struct.unpack_from("<4I", raw)
    if (kind, version, dimensions) != (1, 1, 3):
        raise ValueError(f"Unsupported RGBTable kind/version/dimensions: {kind}/{version}/{dimensions}")
    count = divisions**3
    tail = 16 + count * 6
    if len(raw) not in (tail + 28, tail + 32):
        raise ValueError("Unexpected RGBTable length")
    primaries, gamma, gamut = struct.unpack_from("<3I", raw, tail)
    amount_min, amount_max = struct.unpack_from("<2d", raw, tail + 12)
    if primaries >= len(PRIMARIES) or gamma >= len(GAMMA) or gamut >= len(GAMUT):
        raise ValueError("Unknown RGBTable color encoding")
    overflow_nodes = 0
    with Path(output).open("w", newline="\n", encoding="ascii") as stream:
        stream.write('TITLE "Adobe XMP RGBTable only; excludes LookTable and tone curve"\n')
        stream.write(f"LUT_3D_SIZE {divisions}\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        stream.write(f"# Primaries: {PRIMARIES[primaries]}; transfer: {GAMMA[gamma]}; gamut: {GAMUT[gamut]}\n")
        # CUBE order has red changing fastest. Adobe's table stores blue fastest.
        for b in range(divisions):
            for g in range(divisions):
                for r in range(divisions):
                    index = (r * divisions + g) * divisions + b
                    delta = struct.unpack_from("<3h", raw, 16 + index * 6)
                    identity = [(x * 65535 + divisions // 2) // (divisions - 1) for x in (r, g, b)]
                    values = [identity[i] + delta[i] for i in range(3)]
                    overflow_nodes += any(x < 0 or x > 65535 for x in values)
                    stream.write(" ".join(f"{(x & 65535) / 65535:.9f}" for x in values) + "\n")
    return {
        "type": "3D RGBTable",
        "grid": [divisions] * 3,
        "primaries": PRIMARIES[primaries],
        "transfer": GAMMA[gamma],
        "gamut": GAMUT[gamut],
        "min_amount": amount_min,
        "max_amount": amount_max,
        "overflow_nodes_before_uint16_cast": overflow_nodes,
        "decoded_bytes": len(raw),
        "cube": output.name,
        "scope": "RGBTable only; Adobe LookTable, tone curve, base profile and processing order are excluded",
    }
