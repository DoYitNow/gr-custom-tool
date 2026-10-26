"""Proven UserData fields in the supplied GR IV 1.20.10.11 image.

This is a layout description, not an installed firmware patch. Main fields and
mirror fields are deliberately separate: the native snapshot grew from 2,793
to 2,884 bytes, and the mirror moved. Existing photo/movie/crop main fields
below are supported by actual native loads, not by a global address delta.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import struct

BASE = 0x53000000
SOURCE_RTOS_SHA256 = "cf51224e6eb6bed79eeafa2e7923acf5434df39629510e75907c1dfbffab61a9"


@dataclass(frozen=True)
class UserData120:
    snapshot_bytes: int = 0xB44
    payload_offset: int = 4
    mirror_offset: int = 0xB48
    snapshot_count: int = 10
    snapshot_pointer_offset: int = 4
    crop_offset: int = 0x49F
    snapshot_crop_offset: int = 0x49B
    mirror_crop_offset: int = 0xFE3
    photo_selector_offset: int = 0x152
    photo_styles_offset: int = 0x153
    mirror_photo_selector_offset: int = 0xC96
    mirror_photo_styles_offset: int = 0xC97
    movie_selector_offset: int = 0x514
    movie_styles_offset: int = 0x515
    mirror_still_movie_offset: int = 0xB76
    current_getter: int = 0x53240C38
    current_object: int = 0x550832A8
    constructor: int = 0x5329B040
    copy_snapshot: int = 0x5329B218
    apply_snapshot: int = 0x532ADEC0
    current_crop_getter: int = 0x532A03E0
    mirror_crop_getter: int = 0x5329CCC4
    current_photo_style_getter: int = 0x532A1280
    current_movie_style_getter: int = 0x532A14BC
    copy_saved_to_registered: int = 0x533F69F0
    native_memcpy: int = 0x53157380
    native_memset: int = 0x531579A8
    native_crop_menu_count: int = 0x531BB684
    native_crop_settings_count: int = 0x531D0A74
    native_crop_order: int = 0x53DE5890
    native_crop_text: int = 0x5338F198


USERDATA120 = UserData120()

# These words support individual fields and entry roles. They are not a
# certificate that every downstream UI, persistence or hardware ABI is known.
_WORDS = {
    0x5329B050: 0xE3009B44,  # ctor zeroes 0xb44 bytes in each native payload
    0x5329B224: 0xE3002B44,  # UserData -> bare snapshot native copy size
    0x532ADEC4: 0xE3002B44,  # bare snapshot -> UserData native copy size
    0x532A03E0: 0xE5D0049F,  # current/main crop
    0x5329CCC4: 0xE5D00FE3,  # mirrored crop
    0x5329B6C4: 0xE5D00C96,  # mirrored photo group
    0x5329B6CC: 0xE5D00C97,  # first mirrored photo style
    0x5329B310: 0xE5D00B76,  # mirrored StillMovie field
    0x532A1290: 0xE5D03152,  # photo main selector
    0x532A12BC: 0xE5D03153,  # photo main style
    0x532A14CC: 0xE5D03514,  # movie main selector
    0x532A14F8: 0xE5D03515,  # movie main style
    0x533F6A20: 0xE3002B44,  # native registered snapshot copy length
    0x533F6A28: 0xE5931014,  # six saved snapshots begin at mode+0x14
    0x533F6A2C: 0xE5960004,  # four registered snapshots begin at mode+4
    0x533F2E10: 0xE2876004,
    0x533F2E14: 0xE2874014,
    0x533F2E48: 0xE287602C,  # table end: (0x2c - 4)/4 = ten pointers
    0x531BB684: 0xE3A00004,
    0x531BB688: 0xE12FFF1E,
    0x531D0A74: 0xE3A00004,
    0x531D0A78: 0xE12FFF1E,
}


def inspect_userdata120(rtos: bytes, *, require_official=True) -> dict:
    """Verify the supplied layout before a 1.20 compiler consumes its fields.

    ``require_official=False`` allows re-reading an appended candidate whose
    relevant native field instructions remain intact. It is not a general
    acceptance rule for unknown firmware inputs.
    """
    data = bytes(rtos)
    digest = sha256(data).hexdigest()
    if require_official and digest != SOURCE_RTOS_SHA256:
        raise ValueError("UserData 1.20 profile requires the verified official RTOS")
    verified = []
    for address, expected in _WORDS.items():
        offset = address - BASE
        if offset < 0 or offset + 4 > len(data):
            raise ValueError(f"UserData 1.20 instruction is outside RTOS: {address:#x}")
        word, = struct.unpack_from("<I", data, offset)
        if word != expected:
            raise ValueError(f"UserData 1.20 instruction changed at {address:#x}")
        verified.append({"address": hex(address), "word": hex(word)})
    order = list(data[USERDATA120.native_crop_order - BASE:USERDATA120.native_crop_order - BASE + 4])
    if order != [0, 1, 3, 2]:
        raise ValueError("UserData 1.20 native crop order changed")
    return {"layout": asdict(USERDATA120), "source_rtos_sha256": digest,
            "native_crop_order": order, "verified_words": verified,
            "scope": "Native field instructions and snapshot copy/count layout only",
            "unknown": ["old persistent-record schema upgrade", "AF Point storage/apply ordering",
                        "complete save/load hook ABI", "pixel/ISP/physical persistence"],
            "hardware": "not_tested", "firmware_patched": False}


def state_compile_defines120() -> dict[str, int]:
    """Values for newly compiled sidecars; does not rewrite old native code."""
    return {"GR4_USERDATA_CROP_OFFSET": USERDATA120.crop_offset,
            "GR4_SNAPSHOT_CROP_OFFSET": USERDATA120.snapshot_crop_offset,
            "GR4_USERDATA_PHOTO_STYLES_OFFSET": USERDATA120.mirror_photo_styles_offset,
            "GR4_SNAPSHOT_COUNT": USERDATA120.snapshot_count,
            "GR4_USERDATA_CURRENT_GETTER": USERDATA120.current_getter}
