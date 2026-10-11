"""Rebuild custom resources over preserved native directories."""
import hashlib
import struct


def rebuild_resources(patch, icon_bytes, rows):
    from .slots import BASE, TABLES, TEXT_CATALOG, ICON_CATALOG, _half, _read, _hex, _mov
    custom = sorted((row for row in rows if row.get("kind") == "custom" and row.get("enabled", True)), key=lambda row: row["identity"]["style_id"])
    columns = max([34] + [row["identity"]["profile_id"] + 1 for row in custom])
    if columns > 255:
        raise ValueError("颜色 profile 数量超过原生字节范围")
    tables = {}
    for family, stride, low, high in TABLES:
        if patch.word(stride) != 0xE3A02025:
            raise ValueError("资源重建需要规范 .27 的 37 列颜色表")
        address = _half(patch.word(low)) | _half(patch.word(high)) << 16
        pointers = list(struct.unpack("<111I", _read(patch.image, address, 444)))
        tables[family] = []
        for hardware in range(3):
            native = pointers[hardware * 37:hardware * 37 + 34]
            tables[family].extend(native + [native[11]] * (columns - 34))

    # Text 837 is the first custom name; 838/839 are the existing 5:4 and
    # 2.35:1 aspect labels. Later custom names start at 840. Preserve both
    # aspect labels while retiring the exact custom directory entries.
    for language in range(21):
        catalog = patch.word(TEXT_CATALOG + language * 4)
        for text_id in (837, *range(840, 896)):
            if patch.word(catalog + text_id * 4):
                patch.set_word(catalog + text_id * 4, 0, "clear previous custom text registration")
    for icon_id in range(663, 1024):
        if patch.word(ICON_CATALOG + icon_id * 4):
            patch.set_word(ICON_CATALOG + icon_id * 4, 0, "clear previous custom icon registration")
    icons = bytearray(icon_bytes)
    profiles = []
    for row in custom:
        identity = row["identity"]
        profile = identity["profile_id"]
        text_id, icon_id = identity["text_id"], identity["icon_id"]
        if not 34 <= profile < columns or not (text_id == 837 or 840 <= text_id < 896) or not 663 <= icon_id < 1024:
            raise ValueError("自定义资源编号超出实际目录容量")
        banks = row.get("resources", {}).get("banks", [])
        if len(banks) != 3 or {bank.get("hardware_index") for bank in banks} != {0, 1, 2}:
            raise ValueError("动态滤镜需要三个完整硬件颜色 bank")
        bank_reports = []
        for bank in sorted(banks, key=lambda value: value["hardware_index"]):
            hardware = bank["hardware_index"]
            descriptors = {family: bytearray(_hex(bank["descriptor_hex"][family], 128)) for family, *_ in TABLES}
            addresses = {}
            for key, size in (("camera_basis", 18), ("matrix", 18), ("multi_main", 2160), ("multi_aux", 1440)):
                addresses[key] = patch.append(_hex(bank[key], size), 16)
            for key, target in (("multi_main_wrapper", "multi_main"), ("multi_aux_wrapper", "multi_aux")):
                wrapper = bytearray(_hex(bank[key], 20))
                struct.pack_into("<I", wrapper, 16, addresses[target])
                addresses[key] = patch.append(wrapper, 16)
            matrix = descriptors["color_matrix"]
            old_basis = struct.unpack_from("<I", matrix, 0)[0]
            # Basis pointers are repeated within this descriptor. Keep all such
            # aliases, while the editable style matrix has its own field +4.
            for offset in range(0, 128, 4):
                if struct.unpack_from("<I", matrix, offset)[0] == old_basis:
                    struct.pack_into("<I", matrix, offset, addresses["camera_basis"])
            struct.pack_into("<I", matrix, 0, addresses["camera_basis"])
            struct.pack_into("<I", matrix, 4, addresses["matrix"])
            struct.pack_into("<2I", descriptors["multi_axial"], 8, addresses["multi_main_wrapper"], addresses["multi_aux_wrapper"])
            gamma = _hex(bank["gamma"], 1536)
            for channel in range(3):
                at = patch.append(gamma[channel * 512:(channel + 1) * 512], 16)
                struct.pack_into("<I", descriptors["member_188"], 8 + channel * 4, at)
            descriptor_addresses = {}
            for family, descriptor in descriptors.items():
                at = patch.append(descriptor, 16)
                tables[family][hardware * columns + profile] = at
                descriptor_addresses[family] = at
            bank_reports.append({"hardware_index": hardware, "descriptors": descriptor_addresses,
                                 "sha256": {key: hashlib.sha256(_hex(bank[key], size)).hexdigest()
                                            for key, size in (("camera_basis", 18), ("matrix", 18), ("multi_main", 2160), ("multi_aux", 1440), ("gamma", 1536))}})

        name = row["name"]
        encoded = (name + "\0").encode("utf-16-le")
        if not name.strip() or len(encoded) // 2 > 81:
            raise ValueError("滤镜名称必须为 1–80 个 UTF-16 字符")
        text = patch.append(encoded, 4)
        record = patch.append(bytes((len(encoded) // 2, 1, 0, 0)) + struct.pack("<I", text), 4)
        for language in range(21):
            catalog = patch.word(TEXT_CATALOG + language * 4)
            patch.set_word(catalog + text_id * 4, record, "register active custom text in all languages")
        pixels = _hex(row.get("icon_rgba", row["resources"]["icon"]), 6400)
        icons.extend(bytes((-len(icons)) % 4))
        pixel_offset = len(icons)
        icons.extend(pixels)
        descriptor = patch.append(struct.pack("<HHHHI", 1, 40, 40, 0, pixel_offset // 4), 16)
        patch.set_word(ICON_CATALOG + icon_id * 4, descriptor, "register active custom icon")
        profiles.append({"id": row["id"], "identity": identity, "banks": bank_reports,
                         "text_record": record, "icon_descriptor": descriptor,
                         "icon_sha256": hashlib.sha256(pixels).hexdigest()})

    table_report = {}
    for family, stride, low, high in TABLES:
        address = patch.append(struct.pack("<%dI" % len(tables[family]), *tables[family]), 16)
        patch.set_word(stride, 0xE3A02000 | columns, "dynamic color table profile count")
        patch.set_word(low, _mov(3, address & 65535), "dynamic color table low")
        patch.set_word(high, _mov(3, address >> 16, high=True), "dynamic color table high")
        table_report[family] = {"address": address, "columns": columns, "native_profiles_preserved": 34}
    upper_text = max([839] + [row["identity"]["text_id"] for row in custom])
    upper_icon = max([662] + [row["identity"]["icon_id"] for row in custom])
    for site in getattr(patch, "text_bound_sites", (0x543D2BA4, 0x543D2C04)):
        patch.set_word(site, _mov(3, upper_text), "active text directory upper bound")
    for site, register in ((0x5323DBB4, 3), (0x5323E40C, 2)):
        patch.set_word(site, _mov(register, upper_icon), "active icon directory upper bound")
    return bytes(icons), {"tables": table_report, "profiles": profiles,
                          "text_capacity": 896, "icon_capacity": 1024,
                          "removed_custom_entries_zeroed": True,
                          "native_profiles_and_catalog_entries_preserved": True}
