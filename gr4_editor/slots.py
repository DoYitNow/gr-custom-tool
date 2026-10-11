"""Compile a custom registry from a known official input or a recoverable output.

The current backend supports zero to sixteen active custom filters. Emitted images
are offline candidates; arbitrary layouts and video scalar editing are unproven.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import tempfile


from .toolchain import linker_flags

BASE = 0x53000000
# Supported registry size, not the native vectors' initial reserve or hardware limit.
MAX_ACTIVE_CUSTOM_SLOTS = 16
# These six factory Mono records are already present in the native style table
# and have profile/text/icon/parameter routes.  Their color-mode admission is
# the only missing path in the reviewed firmware layout.
NATIVE_MONO_STYLES = (25, 26, 27, 28, 29, 30)
NATIVE_FACTORY_STYLE_IDS = tuple(range(11, 31))
NATIVE_MONO_ADMISSION_SITE = 0x533CC6E0
KNOWN27_SHA = "5565f01e03d4f8cda29dc7f3e24750fc8ed4882ccd9e7cbe6331c64b98cca109"
OFFICIAL7_SHA = "a9a9a51530c35819df47dee1cce9f4f560a17eeed1da258950167d282884fce1"
OFFICIAL7_ICON_SIZE = 14480480
TEXT_CATALOG = 0x543D4AC0
ICON_CATALOG = 0x543D3AC0
TABLES = (
    ("color_matrix", 0x53925F98, 0x53925F9C, 0x53925FA4),
    ("multi_axial", 0x53925FC4, 0x53925FC8, 0x53925FD0),
    ("member_16c", 0x53925FF0, 0x53925FF4, 0x53925FFC),
    ("member_188", 0x5392601C, 0x53926020, 0x53926028),
)
FOOTER_MAGIC = b"GR4REG01"
VERSION_SITES = (0x53FE50F8, 0x53A78498, 0x53A784BC)
PARAMETER_IDS = ("Saturation", "ColorHue", "ImageKey", "Contrast", "ContrastHighLight",
                 "ContrastShadow", "Sharpness", "Shading", "ClarityControl")
DEFAULT_ENTRIES = (0x533BF710, 0x533C01BC, 0x533C0C68, 0x533C1C14, 0x533C2BC0,
                   0x533C3B6C, 0x533C4B18, 0x533C93F0, 0x533CA39C)
GETTERS = (0x533B7130, 0x533B7818, 0x533B7F00, 0x533B8928, 0x533B9350,
           0x533B9D78, 0x533BA7A0, 0x533BCDB8, 0x533BD7E0)
SETTERS = (0x533AD4E0, 0x533ADCE0, 0x533AE4E0, 0x533AF100, 0x533AFD20,
           0x533B0940, 0x533B1560, 0x533B3FBC, 0x533B4BDC)
SCALAR_TEMPLATE = 0x543F6F00
SCALAR_FIELD_BYTES = 0x8E0
SNAPSHOT_HOOKS = (
    (0x533E5010, "save", (5, 7), 0xE1A01004),
    (0x533E50C4, "save", (5, 7), 0xE1A01004),
    (0x533E4CBC, "save", (4, 6), 0xEAFFFFEB),
    (0x533E4CF8, "save", (4, 6), 0xEAFFFFDC),
    (0x533E4C10, "restore", (6, 7), 0xE1A01007),
    (0x533E4C2C, "restore", (6, 7), 0xE1A01007),
    (0x533E4D48, "reset", (5, 8), 0xE5D53000),
    (0x533E15D0, "reset", (5, 4), 0xE24BD018),
)


def _branch(at, target, condition=14, link=False):
    delta = target - at - 8
    if delta % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise ValueError("ARM 分支目标超出范围")
    return condition << 28 | (0x0B000000 if link else 0x0A000000) | ((delta // 4) & 0xFFFFFF)


def _target(at, word):
    if word & 0x0E000000 != 0x0A000000:
        raise ValueError("共享入口不是已知 ARM 分支")
    displacement = word & 0xFFFFFF
    if displacement & 0x800000:
        displacement -= 0x1000000
    return at + 8 + displacement * 4


def _mov(reg, value, high=False, condition=14):
    return condition << 28 | (0x03400000 if high else 0x03000000) | ((value & 0xF000) << 4) | reg << 12 | (value & 0xFFF)


class _Patch:
    def __init__(self, data):
        self.image = bytearray(data)
        self.changes = []

    def append(self, blob, alignment=4):
        self.image.extend(bytes((-len(self.image)) % alignment))
        address = BASE + len(self.image)
        if callable(blob):
            blob = blob(address)
        if isinstance(blob, (list, tuple)):
            blob = struct.pack("<%dI" % len(blob), *blob)
        self.image.extend(blob)
        return address

    def word(self, address):
        return _word(self.image, address)

    def set_word(self, address, value, reason):
        before = self.word(address)
        struct.pack_into("<I", self.image, address - BASE, value)
        self.changes.append({"address": hex(address), "before": hex(before), "after": hex(value), "reason": reason})

    def prefix(self, site, make, reason):
        previous = _target(site, self.word(site))
        address = self.append(lambda at: make(at) + [_branch(at + len(make(at)) * 4, previous)], 16)
        self.set_word(site, _branch(site, address), reason)
        return address


def _defaults(row):
    parameters = {item["id"]: item for item in row.get("parameters", [])}
    values = []
    for name in PARAMETER_IDS:
        item = parameters.get(name, {})
        # A disabled verified scalar is compiled as the neutral UI value.  The
        # field stays in the recipe metadata, so the editor can restore it
        # without confusing a disabled control with a zero adjustment.
        value = 4 if item.get("enabled", True) is False else item.get("value", item.get("default"))
        if value is None:
            value = 4
        if type(value) is not int or not 0 <= value <= 8:
            raise ValueError("九项默认参数必须是 0–8 的整数")
        values.append(value)
    return values


def _parameter_enabled(row):
    """Persist the per-field activation bits that the custom writer knows."""
    return {
        item["id"]: bool(item.get("enabled", True))
        for item in row.get("parameters", [])
        if item.get("id") in PARAMETER_IDS
    }


def _toolchain():
    from .toolchain import toolchain
    return toolchain()


def _compile_state(patch, rows, layouts):
    clang, objcopy, nm = _toolchain()
    slot_rows = []
    for row in rows:
        style = row["identity"]["style_id"]
        layout = layouts[style]
        encoded = [value ^ 4 for value in _defaults(row) for group in range(4)]
        slot_rows.append("{%su,%su,%su,%du,%du,{%s}}" %
                         (hex(layout["main_offset"]), hex(layout["mirror_offset"]), hex(layout["shadow_offset"]), layout["record"], style, ",".join(map(str, encoded))))
    source = "#define SLOT_COUNT %d\n#define SLOT_ROWS %s\n" % (len(rows), ",".join(slot_rows))
    source += (Path(__file__).parent / "native/registry_state.c").read_text(encoding="utf-8")
    origin = (BASE + len(patch.image) + 15) & ~15
    with tempfile.TemporaryDirectory(prefix="gr4-registry-") as temporary:
        directory = Path(temporary)
        c, ld, elf, binary = (directory / name for name in ("state.c", "state.ld", "state.elf", "state.arm"))
        c.write_text(source, encoding="utf-8")
        ld.write_text("original_save_body = 0x533E1C08; original_load_body = 0x533E17D0; SECTIONS { . = %s; .text : { *(.text*) *(.rodata*) } /DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }" % hex(origin), encoding="ascii")
        command = [str(clang), *linker_flags(clang), "-target", "armv7-none-eabi", "-mcpu=cortex-a9", "-marm", "-mfloat-abi=soft", "-Os", "-ffreestanding", "-fno-builtin", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-nostdlib", "-Wl,-T," + str(ld), "-Wl,--entry=registry_load", str(c), "-o", str(elf)]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise ValueError("ARM 状态模块编译失败: " + result.stderr[-1500:])
        subprocess.run([str(objcopy), "-O", "binary", "--only-section=.text", str(elf), str(binary)], check=True, capture_output=True)
        listing = subprocess.run([str(nm), "-n", str(elf)], check=True, capture_output=True, text=True).stdout
        if re.search(r"^\s+U\s", listing, re.M):
            raise ValueError("ARM 状态模块有未解析的符号")
        symbols = {name: int(at, 16) for at, kind, name in re.findall(r"^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$", listing, re.M) if kind in "Tt"}
        blob = binary.read_bytes()
    if patch.append(blob, 16) != origin:
        raise ValueError("ARM 状态地址变化")
    return symbols, {"address": hex(origin), "bytes": len(blob), "sha256": sha(blob), "source_sha256": sha(source.encode()), "symbols": {key: hex(value) for key, value in symbols.items()}}


def _install_state(patch, rows, layouts):
    symbols, report = _compile_state(patch, rows, layouts)
    for site, kind, arguments, displaced in SNAPSHOT_HOOKS:
        helper = symbols["registry_slot_" + kind]
        def make(address, helper=helper, arguments=arguments, displaced=displaced, site=site):
            words = [0xE92D500F, 0xE1A00000 | arguments[0], 0xE1A01000 | arguments[1],
                     _branch(address + 12, helper, link=True), 0xE8BD500F]
            if displaced & 0xFF000000 == 0xEA000000:
                words.append(_branch(address + 20, _target(site, displaced)))
            else:
                words += [displaced, _branch(address + 24, site + 4)]
            return words
        stub = patch.append(make, 16)
        patch.set_word(site, _branch(site, stub), "unified ten-snapshot " + kind)
    copy = patch.append(lambda a: [0xE1A03005, _branch(a + 4, symbols["registry_slot_copy"])], 16)
    patch.set_word(0x533E4DE0, _branch(0x533E4DE0, copy, link=True), "one original snapshot copy then all custom sidecars")
    patch.set_word(0x533E1C04, _branch(0x533E1C04, symbols["registry_save"]), "unified current APData save")
    patch.set_word(0x533E17CC, _branch(0x533E17CC, symbols["registry_load"]), "unified current APData load with defaults")
    bound = max((layout["record"] + 1 for layout in layouts.values()), default=16)
    upper = patch.append(lambda a: [_mov(3, bound), 0xE58D3000, _branch(a + 8, 0x53A80AE0)], 16)
    patch.set_word(0x53A80ADC, _branch(0x53A80ADC, upper), "APData custom record upper bound")
    for field, site in enumerate(DEFAULT_ENTRIES):
        previous = _target(site, patch.word(site))
        def make(a, previous=previous, field=field):
            words = []
            for row in rows:
                style, value = row["identity"]["style_id"], _defaults(row)[field]
                here = a + len(words) * 4
                words += [0xE3510000 | style, _branch(here + 4, here + 28, condition=1),
                          0xE5D0C01F, 0xE35C0000, _branch(here + 16, previous, condition=1),
                          0xE3A00000 | value, 0xE12FFF1E]
            return words + [_branch(a + len(words) * 4, previous)]
        stub = patch.append(make, 16)
        patch.set_word(site, _branch(site, stub), "custom default marker " + PARAMETER_IDS[field])
    report["slots"] = [{**layouts[row["identity"]["style_id"]], "defaults": _defaults(row)} for row in rows]
    report["encoding"] = "value XOR4; per-preset defaults initialize missing/invalid APData and reset snapshots"
    return report


def _clone_scalar(patch, style, main_helper):
    """Generate or relocate native scalar closures using the supplied base.

    Each field occupies 0x8e0 bytes. The only embedded data are the four-entry
    setter tables at +0x840; their pointers are relocated as data, never code.
    Native callbacks and external property setters keep their destinations.
    """
    origin = (BASE + len(patch.image) + 15) & ~15
    size = SCALAR_FIELD_BYTES * 9
    if getattr(patch, "official_base", False):
        from .official_scalar import generate_scalar_template
        start = origin
        source = generate_scalar_template(patch, start=start)
    else:
        start = SCALAR_TEMPLATE
        source = _read(patch.image, start, size)
    delta = origin - start
    main = 0x55088604  # Template constants are replaced by heap lookup thunks.
    blob = bytearray(source)

    def relocated(value):
        if start <= value < start + size:
            return value + delta
        return value

    for field in range(9):
        field_start = field * SCALAR_FIELD_BYTES
        pairs = {}
        for offset in range(field_start, field_start + SCALAR_FIELD_BYTES, 4):
            word = struct.unpack_from("<I", source, offset)[0]
            old_address, address = start + offset, origin + offset
            if field_start + 0x840 <= offset < field_start + 0x850:
                struct.pack_into("<I", blob, offset, relocated(word))
                continue
            if word & 0x0E000000 == 0x0A000000:
                target = relocated(_target(old_address, word))
                word = _branch(address, target, condition=word >> 28, link=bool(word & 0x01000000))
            elif word == 0xE3530023:
                word = 0xE3530000 | style
            kind, reg = word & 0x0FF00000, (word >> 12) & 15
            if kind == 0x03000000:
                pairs[reg] = (offset, _half(word), word)
            elif kind == 0x03400000 and reg in pairs:
                low_offset, low_value, low_word = pairs[reg]
                if offset - low_offset <= 32:
                    before = low_value | _half(word) << 16
                    after = relocated(before)
                    if after != before:
                        struct.pack_into("<I", blob, low_offset, _mov(reg, after & 65535, condition=low_word >> 28))
                        word = _mov(reg, after >> 16, high=True, condition=word >> 28)
            struct.pack_into("<I", blob, offset, word)
        # New dispatch falls through to the full previous registry, rather
        # than skipping its style35 node and reaching the first style34 node.
        for kind, entry, dispatch_offset, fallback_offset in (
            ("setter", SETTERS[field], 0x850, 64), ("getter", GETTERS[field], 0x8A0, 44)):
            at = origin + field_start + dispatch_offset + fallback_offset
            previous = _target(entry, patch.word(entry))
            struct.pack_into("<I", blob, field_start + dispatch_offset + fallback_offset, _branch(at, previous))
            patch.set_word(entry, _branch(entry, origin + field_start + dispatch_offset), "style%d %s %s" % (style, PARAMETER_IDS[field], kind))
    if patch.append(blob, 16) != origin:
        raise ValueError("标量闭包布局变化")
    # Replace every static main address entry. Native mutation/callback bodies
    # still use r0 as the owning main block and mirror at +36, unchanged.
    for field in range(9):
        base = origin + field * SCALAR_FIELD_BYTES
        for group in range(4):
            mutator_body = base + group * 0x210 + 0x60
            entry = base + group * 0x210 + 0x140
            stub = patch.append(lambda a, body=mutator_body: [
                0xE92D400E, _mov(0, style), _branch(a + 8, main_helper, link=True),
                0xE8BD400E, 0xE2211004, _branch(a + 20, body)], 16)
            patch.set_word(entry, _branch(entry, stub), "heap main for style%d field%d group%d" % (style, field, group))
        getter = base + 0x8A0
        previous = _target(getter + 44, patch.word(getter + 44))
        def dynamic_getter(a, field=field, previous=previous):
            return [0xE3530000 | style, _branch(a + 4, previous, condition=1),
                    0xE3510000, _branch(a + 12, previous, condition=1),
                    0xE3520003, _branch(a + 20, previous, condition=8),
                    0xE92D400E, _mov(0, style), _branch(a + 32, main_helper, link=True),
                    0xE8BD400E, 0xE280C000 | (36 + field * 4),
                    0xE7DC0002, 0xE2200004, 0xE12FFF1E]
        stub = patch.append(dynamic_getter, 16)
        patch.set_word(GETTERS[field], _branch(GETTERS[field], stub), "heap getter style%d field%d" % (style, field))
    return {"style_id": style, "address": hex(origin), "bytes": size,
            "sha256": sha(blob), "storage": "dynamic RAW owner heap",
            "native_closure_mutator_and_notification_preserved": True,
            "previous_style_dispatch_preserved": True}


def _identity_registry(patch, rows):
    """Generate every proven custom identity exit from one sorted registry."""
    origin = (BASE + len(patch.image) + 15) & ~15
    code, entries = [], {}

    def emit(*words):
        code.extend(words)

    def jump(target, condition=14, link=False):
        emit(_branch(origin + len(code) * 4, target, condition, link))

    identities = [row["identity"] for row in rows]
    entries["writer"] = origin + len(code) * 4
    emit(0xE5D2379C)
    for row in identities:
        emit(0xE3530000 | row["style_id"])
        jump(origin + len(code) * 4 + 20, condition=1)
        emit(0xE1A00001, _mov(2, row["maker_note_code"]), 0xE3A01000)
        jump(0x537E7148)
    emit(0xE30F3198)
    jump(0x537DE478)
    for name, reg, field, success, fallback, resume in (
        ("read_style", 3, "style_id", 0x537D57AC, 0xE300310A, 0x537D5760),
        ("read_property", 2, "playback_property", 0x537D8A00, 0xE3003109, 0x537D89B4)):
        entries[name] = origin + len(code) * 4
        for row in identities:
            emit(_mov(3, row["maker_note_code"]), 0xE1500003, 0x03A00000 | reg << 12 | row[field])
            jump(success, condition=0)
        emit(fallback)
        jump(resume)
    entries["property_to_style"] = origin + len(code) * 4
    for row in identities:
        emit(0xE3500000 | row["playback_property"])
        jump(origin + len(code) * 4 + 16, condition=1)
        emit(0xE3A03000 | row["style_id"], 0xE3540000)
        jump(0x533A383C)
    emit(0xE2400001)
    jump(0x533A3824)
    entries["playback_icon"] = origin + len(code) * 4
    for row in identities:
        emit(0xE3530000 | row["style_id"], _mov(0, row["icon_id"], condition=0))
        jump(0x533A6898, condition=0)
    emit(0xE2433008)
    jump(0x533A687C)
    entries["playback_detail"] = origin + len(code) * 4
    for row in identities:
        emit(0xE3530000 | row["style_id"], 0x03A0300B)
    emit(0xE2433008)
    jump(0x533A7628)
    entries["settings_icon"] = origin + len(code) * 4
    emit(0xE5D02152, 0xE3520000)
    fallback = origin + len(code) * 4 + 8 + len(rows) * 16
    jump(fallback, condition=1)
    emit(0xE5D02153)
    for row in identities:
        emit(0xE3520000 | row["style_id"])
        jump(origin + len(code) * 4 + 12, condition=1)
        emit(_mov(0, row["icon_id"]))
        jump(0x531B0EA4)
    if origin + len(code) * 4 != fallback:
        raise ValueError("拍摄摘要分支布局不一致")
    jump(0x5329CF30, link=True)
    jump(0x531B0E98)
    for name, field, resume in (("menu_icon", "icon_id", 0x53380FB0), ("menu_text", "text_id", 0x5337F828)):
        entries[name] = origin + len(code) * 4
        for row in identities:
            emit(0xE3510000 | row["style_id"], _mov(0, row[field], condition=0), 0x012FFF1E)
        emit(0xE351000A)
        jump(resume)
    # The .25 video summary has its own call site; keep its proven r1 check
    # and UserData fallback, now covering the entire custom registry.
    entries["video_settings_icon"] = origin + len(code) * 4
    for row in identities:
        emit(0xE3510000 | row["style_id"], _mov(0, row["icon_id"], condition=0))
        jump(0x531B15D4, condition=0)
    jump(0x5323F018, link=True)
    emit(0xE5D02153)
    for row in identities:
        emit(0xE3520000 | row["style_id"], _mov(0, row["icon_id"], condition=0))
        jump(0x531B15D4, condition=0)
    emit(0xE1A00005)
    jump(0x533971CC, link=True)
    jump(0x531B15D4)
    if patch.append(code, 16) != origin:
        raise ValueError("注册表地址变化")
    sites = {"writer": 0x537DE474, "read_style": 0x537D575C, "read_property": 0x537D89B0,
             "property_to_style": 0x533A3820, "playback_icon": 0x533A6878,
             "playback_detail": 0x533A7624, "settings_icon": 0x531B0E94,
             "menu_icon": 0x53380FAC, "menu_text": 0x5337F824, "video_settings_icon": 0x531B15D0}
    for name, site in sites.items():
        patch.set_word(site, _branch(site, entries[name], link=name == "video_settings_icon"), "unified custom identity " + name)
    return {"entries": {key: hex(value) for key, value in entries.items()}, "bytes": len(code) * 4,
            "identities": identities, "original_native_fallbacks_retained": True,
            "raw_current24_cursor_fix_preserved": True, "video_summary25_fix_extended": True}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def capabilities():
    return {"newslots_supported": True, "parameter_defaults_known": True,
            # Exposed for project/UI schema compatibility; no verified ARM
            # lifecycle backend exists for Mono grain yet.
            "particle_defaults_supported": False,
            "particle_fields": [
                {"id": "Particle", "tag": 21, "min": 0, "max": 1},
                {"id": "ParticleSize", "tag": 22, "min": 0, "max": 2},
                {"id": "ParticleStrength", "tag": 23, "min": 0, "max": 2},
            ],
            "native_mono_unlock_supported": True,
            "native_mono_styles": list(NATIVE_MONO_STYLES),
            "native_visibility_supported": True,
            "resource_replacement": True, "name_replacement": True,
            "icon_replacement": True, "replacement_supported": True,
            "icon_edit_supported": True, "supported_custom_styles": [34, 35],
            "official_input_initialization": True,
            "official_input_version": "1.11.10.7",
            "maximum_custom_slots": MAX_ACTIVE_CUSTOM_SLOTS, "menu_reorder_supported": True,
            "visibility_supported": True, "removal_supported": True,
            "next_style": 36, "identities_remaining": 55, "maximum_custom_style": 90,
            "capacity_basis": {"native_vector_builder": "0x533755bc", "vector_initial_capacity": 24,
                               "vector_growth": "native-doubling",
                               "ordinal_vector_fields": ["RawDevelopmentModel+0x4c", "+0x50", "+0x54"],
                               "group_vector_fields": ["RawDevelopmentModel+0x58", "+0x5c", "+0x60"],
                               "native_colour_entries": 14, "user_group_entries": 3,
                               "text_catalog_entries": 896},
            "video_scalar_editing_supported": False,
            "limitations": [f"当前支持{MAX_ACTIVE_CUSTOM_SLOTS}项活动自定义滤镜；文字编号高水位最大style90", "实机启动、显示、ISP与断电保存仍待验证", "视频九参数保留原mode限制"]}


def _word(data, address):
    if address < BASE or address + 4 > BASE + len(data):
        raise ValueError("资源地址超出 RTOS")
    return struct.unpack_from("<I", data, address - BASE)[0]


def _half(word):
    return ((word >> 4) & 0xF000) | (word & 0xFFF)


def _read(data, address, size):
    if not BASE <= address <= address + size <= BASE + len(data):
        raise ValueError("资源范围超出 RTOS")
    return bytes(data[address - BASE:address - BASE + size])


def _hex(item, size):
    try:
        value = bytes.fromhex(item["hex"] if isinstance(item, dict) else item)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("颜色资源必须提供有效的十六进制原字节") from exc
    if len(value) != size:
        raise ValueError(f"颜色资源长度必须为 {size} 字节")
    return value


def _body_sha(data):
    value = bytearray(data)
    for address in VERSION_SITES:
        value[address - BASE:address - BASE + 4] = bytes(4)
    return sha(value)


def _metadata(data):
    if len(data) < 16 or data[-16:-8] != FOOTER_MAGIC:
        return None
    length = struct.unpack_from("<I", data, len(data) - 8)[0]
    start = len(data) - 16 - length
    if start < 0:
        raise ValueError("编辑器注册表尾部无效")
    try:
        metadata = json.loads(data[start:start + length])
    except (ValueError, UnicodeError) as exc:
        raise ValueError("编辑器注册表尾部无效") from exc
    base_sha = metadata.get("base_rtos_sha256")
    if base_sha not in (KNOWN27_SHA, OFFICIAL7_SHA) or metadata.get("body_sha256") != _body_sha(data[:start]):
        raise ValueError("编辑器生成 RTOS 校验失败")
    if base_sha == OFFICIAL7_SHA and (metadata.get("schema_version", 0) < 3 or
            metadata.get("base_kind") != "official-1.11.10.7" or
            metadata.get("compiler_base_sha256") != OFFICIAL7_SHA):
        raise ValueError("官方编译基线声明无效")
    return metadata


def _finish(image, metadata, minimum_size=0):
    image.extend(bytes((-len(image)) % 4))
    base_sha = metadata.get("compiler_base_sha256", KNOWN27_SHA)
    preview = {**metadata, "base_rtos_sha256": base_sha, "body_sha256": "0" * 64}
    estimated = json.dumps(preview, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    footer_size = ((len(estimated) + 3) & ~3) + 16
    required_body = max(len(image), minimum_size - footer_size)
    image.extend(bytes(max(0, ((required_body + 3) & ~3) - len(image))))
    metadata = {**metadata, "base_rtos_sha256": base_sha, "body_sha256": _body_sha(image)}
    encoded = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    encoded += b" " * ((-len(encoded)) % 4)
    image.extend(encoded + FOOTER_MAGIC + struct.pack("<I", len(encoded)) + bytes(4))
    total = sum(struct.unpack("<%dI" % (len(image) // 4), image))
    struct.pack_into("<I", image, len(image) - 4, (-total) & 0xFFFFFFFF)
    return bytes(image)


def _canonical_source(source, metadata=None):
    """Recover exact original bytes before rebuilding the runtime scaffold."""
    source = bytes(source)
    if sha(source) in (KNOWN27_SHA, OFFICIAL7_SHA):
        return source
    metadata = _metadata(source) if metadata is None else metadata
    if metadata and metadata.get("schema_version", 0) >= 2:
        size = metadata.get("compiler_base_size")
        if type(size) is not int or size <= 0 or size > len(source) or size % 4:
            raise ValueError("编辑器 compiler base 长度无效")
        restored = bytearray(source[:size])
        for row in metadata.get("compiler_base_restore", []):
            offset, raw = row.get("offset"), bytes.fromhex(row.get("hex", ""))
            if type(offset) is not int or offset < 0 or offset + len(raw) > size:
                raise ValueError("编辑器 compiler base 恢复记录无效")
            restored[offset:offset + len(raw)] = raw
        # Version words are changed later by the container repacker. Older
        # outputs compiled directly from .27 omitted these unchanged words.
        base_sha = metadata.get("compiler_base_sha256", metadata.get("base_rtos_sha256"))
        words = ((0x010B0A07, 0xE3000A07, 0xE3A00007) if base_sha == OFFICIAL7_SHA else
                 (0x010B0A1B, 0xE3000A1B, 0xE3A0001B))
        for address, word in zip(VERSION_SITES, words):
            struct.pack_into("<I", restored, address - BASE, word)
        if base_sha not in (KNOWN27_SHA, OFFICIAL7_SHA) or sha(restored) != base_sha:
            raise ValueError("编辑器 compiler base SHA256 无法还原")
        return bytes(restored)
    raise ValueError("输入没有可恢复的编译基线；请使用含完整恢复记录的编辑器产物")


def canonical_base(rtos):
    """Return the original official or legacy compiler input from its registry."""
    return _canonical_source(rtos)


def _canonical_restore(base, body):
    if len(body) < len(base):
        raise ValueError("编译器损坏了基线前缀")
    before = memoryview(base).cast("I")
    after = memoryview(body)[:len(base)].cast("I")
    version_offsets = {address - BASE for address in VERSION_SITES}
    return [{"offset": index * 4, "hex": struct.pack("<I", old).hex()}
            for index, (old, new) in enumerate(zip(before, after))
            if old != new or index * 4 in version_offsets]


def prepare_update_policy(rtos, allow_older=True):
    """Apply or remove the version policy while preserving registry recovery."""
    if type(allow_older) is not bool:
        raise ValueError("低版本固件支持开关必须是布尔值")
    from .update_policy import apply_update_policy, inspect_update_policy, remove_update_policy
    rtos = bytes(rtos)
    metadata = _metadata(rtos)
    transform = apply_update_policy if allow_older else remove_update_policy
    if metadata is None:
        return transform(rtos)
    base = _canonical_source(rtos, metadata)
    footer_size = 16 + struct.unpack_from("<I", rtos, len(rtos) - 8)[0]
    body, proof = transform(rtos[:-footer_size])
    policy = inspect_update_policy(body)
    if body == rtos[:-footer_size] and metadata.get("update_policy") == policy:
        return rtos, proof
    metadata = {**metadata, "update_policy": policy,
                "compiler_base_restore": _canonical_restore(base, body)}
    output = _finish(bytearray(body), metadata, minimum_size=len(rtos))
    if _canonical_source(output) != base:
        raise ValueError("版本策略修改后无法恢复编译基线")
    return output, proof


def _restore_scalar_fallbacks(patch):
    """Retire .27 static custom dispatch while keeping its native prologue."""
    result = []
    for site in GETTERS + SETTERS + DEFAULT_ENTRIES:
        target = _target(site, patch.word(site))
        for _ in range(16):
            word = patch.word(target)
            if word & 0xFF000000 == 0xEA000000:
                target = _target(target, word)
                continue
            if word & 0xFFFFFF00 in (0xE3530000, 0xE3510000) and word & 255 >= 34:
                conditional = patch.word(target + 4)
                if conditional & 0xFF000000 != 0x1A000000:
                    raise ValueError("旧自定义标量回退入口无法识别")
                target = _target(target + 4, conditional)
                continue
            break
        else:
            raise ValueError("旧自定义标量回退入口循环")
        patch.set_word(site, _branch(site, target), "factory fallback without retired static custom state")
        result.append({"site": hex(site), "native_fallback": hex(target)})
    return result


def _alias_retired(patch, site, register, active, minimum=34, standard=11):
    previous = _target(site, patch.word(site))
    def code(at):
        words = []
        for value in active:
            here = at + len(words) * 4
            words += [0xE3500000 | register << 16 | value,
                      _branch(here + 4, previous, condition=0)]
        words += [0xE3500000 | register << 16 | minimum,
                  0x23A00000 | register << 12 | standard,
                  _branch(at + (len(words) + 2) * 4, previous)]
        return words
    target = patch.append(code, 16)
    patch.set_word(site, _branch(site, target), "retired custom identity falls back to Standard")
    return hex(target)


def _native_mono_option(value, metadata=None):
    """Normalize the explicit factory-Mono admission experiment switch.

    The switch only changes the color-mode admission predicate for the six
    existing factory records.  It never makes those records custom or
    writable.  A missing option inherits a previously installed experiment so
    that a later canonical rebuild does not silently disable it.
    """
    if value is None:
        record = (metadata or {}).get("native_mono", {})
        value = record.get("unlock_native_mono", False) if isinstance(record, dict) else False
    elif isinstance(value, dict):
        keys = set(value)
        if keys in ({"enabled"}, {"schema_version", "enabled"}):
            if "schema_version" in value and type(value["schema_version"]) is not int:
                raise ValueError("原厂 Mono 解锁计划的 schema_version 必须为 1")
            if "schema_version" in value and value["schema_version"] != 1:
                raise ValueError("原厂 Mono 解锁计划的 schema_version 必须为 1")
            value = value["enabled"]
        elif keys in ({"unlock_native_mono"}, {"schema_version", "unlock_native_mono"}):
            if "schema_version" in value and type(value["schema_version"]) is not int:
                raise ValueError("原厂 Mono 解锁计划的 schema_version 必须为 1")
            if "schema_version" in value and value["schema_version"] != 1:
                raise ValueError("原厂 Mono 解锁计划的 schema_version 必须为 1")
            value = value["unlock_native_mono"]
        else:
            raise ValueError("原厂 Mono 解锁计划必须是布尔值或包含 enabled 的对象")
    if type(value) is not bool:
        raise ValueError("原厂 Mono 解锁选项必须是布尔值")
    return value


def _install_native_visibility(patch, visible_styles):
    """Filter the factory menu admission for rows hidden in the editor.

    The native vector keeps all twenty factory records so the reviewed RAW
    control capacity remains valid.  The admission route is what determines
    whether a factory row is offered in the camera menu.
    """
    visible = {int(style) for style in (visible_styles or ())
               if int(style) in NATIVE_FACTORY_STYLE_IDS}
    hidden = [style for style in NATIVE_FACTORY_STYLE_IDS if style not in visible]
    state = {"visible_styles": sorted(visible), "hidden_styles": hidden,
             "site": hex(NATIVE_MONO_ADMISSION_SITE), "hardware": "not_tested"}
    if not hidden:
        state.update({"installed": False, "status": "disabled", "wrapper": None,
                      "bytes": 0, "sha256": None})
        return state
    site = NATIVE_MONO_ADMISSION_SITE
    previous = _target(site, patch.word(site))

    def code(at):
        words = []
        for style in hidden:
            here = at + len(words) * 4
            next_style = here + 24
            words.extend((0xE3510000 | style,
                          _branch(here + 4, next_style, condition=1),
                          0xE3520000,
                          _branch(here + 12, next_style, condition=1),
                          _mov(0, 0),
                          0xE12FFF1E))
        words.append(_branch(at + len(words) * 4, previous))
        return words

    target = patch.append(code, 16)
    size = len(patch.image) - (target - BASE)
    blob = _read(patch.image, target, size)
    patch.set_word(site, _branch(site, target), "filter hidden native factory rows")
    state.update({"installed": True, "status": "installed", "wrapper": hex(target),
                  "bytes": size, "sha256": sha(blob)})
    return state


def _install_native_mono_admission(patch, enabled, styles=None):
    """Add the color-mode admission path for native style IDs 25–30.

    The generated wrapper is installed after the custom/retired identity
    wrapper at ``0x533CC6E0``.  It returns true only for an existing Mono
    style in mode zero and falls through to the prior admission route for all
    other modes/styles.  No profile, menu, text, icon, or parameter table is
    rewritten here.
    """
    site = NATIVE_MONO_ADMISSION_SITE
    previous = _target(site, patch.word(site))
    enabled_styles = tuple(style for style in (styles if styles is not None else NATIVE_MONO_STYLES)
                           if style in NATIVE_MONO_STYLES)
    state = {"unlock_native_mono": bool(enabled), "site": hex(site),
             "previous": hex(previous), "styles": list(enabled_styles),
             "hardware": "not_tested"}
    if not enabled or not enabled_styles:
        state.update({"installed": False, "status": "disabled",
                      "wrapper": None, "bytes": 0, "sha256": None})
        return state

    def code(at):
        words = []
        for style in enabled_styles:
            here = at + len(words) * 4
            next_style = here + 24
            words.extend((0xE3510000 | style,
                          _branch(here + 4, next_style, condition=1),
                          0xE3520000,
                          _branch(here + 12, next_style, condition=1),
                          _mov(0, 1),
                          0xE12FFF1E))
        words.append(_branch(at + len(words) * 4, previous))
        return words

    target = patch.append(code, 16)
    size = len(patch.image) - (target - BASE)
    blob = _read(patch.image, target, size)
    patch.set_word(site, _branch(site, target), "enable native Mono style admission")
    state.update({"installed": True, "status": "installed", "wrapper": hex(target),
                  "bytes": size, "sha256": sha(blob),
                  "route": "style 25..30 and mode r2==0 -> true; otherwise prior admission"})
    return state


def inspect_native_mono_admission(rtos):
    """Read the footer proof and branch bytes for the Mono admission switch."""
    metadata = _metadata(bytes(rtos)) or {}
    record = metadata.get("native_mono", {})
    if not isinstance(record, dict):
        record = {}
    enabled = bool(record.get("unlock_native_mono", False))
    recorded_styles = record.get("styles")
    styles = list(recorded_styles) if isinstance(recorded_styles, list) else list(NATIVE_MONO_STYLES)
    result = {"unlock_native_mono": enabled, "styles": styles,
              "site": hex(NATIVE_MONO_ADMISSION_SITE), "hardware": "not_tested"}
    try:
        instruction = _word(rtos, NATIVE_MONO_ADMISSION_SITE)
        target = _target(NATIVE_MONO_ADMISSION_SITE, instruction)
    except (ValueError, struct.error):
        result.update(status="unreadable", installed=False,
                      reason="admission entry is not a supported ARM branch")
        return result
    result["entry_target"] = hex(target)
    if not enabled:
        result.update(status="disabled", installed=False)
        return result
    expected = record.get("wrapper")
    if isinstance(expected, str):
        try:
            expected_target = int(expected, 16)
        except ValueError:
            expected_target = None
    else:
        expected_target = None
    size = record.get("bytes")
    digest = record.get("sha256")
    if target != expected_target or type(size) is not int or size <= 0:
        result.update(status="mismatch", installed=False,
                      reason="footer wrapper identity differs from admission branch")
        return result
    try:
        blob = _read(rtos, target, size)
    except ValueError:
        result.update(status="mismatch", installed=False,
                      reason="footer wrapper lies outside RTOS")
        return result
    result.update(installed=bool(digest and sha(blob) == digest),
                  status="passed" if digest and sha(blob) == digest else "mismatch",
                  wrapper=hex(target), bytes=size, sha256=sha(blob))
    return result


def _dynamic_menu_routes(patch, rows, custom, main_helper, unlock_native_mono=False, native_order=None):
    from .native_raw import install_raw_selector_capacity
    active_order = [row["identity"]["style_id"] for row in rows]
    native_order = [style for style in (native_order or NATIVE_FACTORY_STYLE_IDS)
                    if style in NATIVE_FACTORY_STYLE_IDS]
    native_order = list(dict.fromkeys(native_order + list(NATIVE_FACTORY_STYLE_IDS)))
    active_native = set(style for style in active_order if style in NATIVE_FACTORY_STYLE_IDS)
    # Keep the complete factory vector for the native control's known minimum
    # capacity.  The admission wrappers below hide rows that are disabled in
    # the editor, so this padding does not make them visible in the menu.
    order = [style for style in native_order if style in active_native or style in NATIVE_FACTORY_STYLE_IDS]
    order.extend(style for style in active_order if style not in NATIVE_FACTORY_STYLE_IDS)
    address = patch.append(bytes(order), 16)
    for low, high in ((0x533CC78C, 0x533CC794), (0x5337561C, 0x53375620)):
        patch.set_word(low, _mov(4, address & 65535), "dynamic menu order low")
        patch.set_word(high, _mov(4, address >> 16, high=True), "dynamic menu order high")
    for site, opcode in ((0x533CC7A4, 0xE2847000), (0x533CC8C8, 0xE3A00000),
                         (0x533BEBF0, 0xE3A00000), (0x53375628, 0xE2847000)):
        patch.set_word(site, opcode | len(order), "dynamic menu count")
    selector_capacity = install_raw_selector_capacity(patch.image, len(order))
    patch.changes.extend({"address": row["site"], "before": row["before"],
                          "after": row["after"], "reason": row["reason"]}
                         for row in selector_capacity["patches"])

    def mapping(site, register, field, destination=0, resume=None, mode_check=False):
        previous = _target(site, patch.word(site))
        def code(at):
            words = []
            for row in custom:
                here = at + len(words) * 4
                words += [0xE3500000 | register << 16 | row["identity"][field[0]]]
                if resume is not None:
                    words += [0x03A00000 | destination << 12 | row["identity"][field[1]],
                              _branch(here + 8, resume, condition=0)]
                elif mode_check:
                    words += [0x03520000, 0x03A00001, 0x012FFF1E]
                else:
                    words += [0x03A00000 | destination << 12 | row["identity"][field[1]], 0x012FFF1E]
            return words + [_branch(at + len(words) * 4, previous)]
        target = patch.append(code, 16)
        patch.set_word(site, _branch(site, target), "dynamic identity lookup")
        return hex(target)

    entries = {}
    entries["profile"] = mapping(0x53912CF4, 2, ("style_id", "profile_id"), 3, 0x53912D08)
    entries["admission"] = mapping(0x533CC6E0, 1, ("style_id", "style_id"), mode_check=True)
    entries["style_to_raw"] = mapping(0x53373BC0, 1, ("style_id", "raw_ordinal"))
    for site in (0x53373F84, 0x53374118):
        entries[hex(site)] = mapping(site, 1, ("raw_ordinal", "style_id"))
    for site, register, resume in ((0x539113FC, 2, 0x53911614), (0x5375F0B0, 1, 0x5375F1B0)):
        previous = _target(site, patch.word(site))
        def code(at, register=register, resume=resume, previous=previous):
            words = []
            for row in custom:
                here = at + len(words) * 4
                words += [0xE3500000 | register << 16 | row["identity"]["style_id"],
                          _branch(here + 4, resume, condition=0)]
            return words + [_branch(at + len(words) * 4, previous)]
        target = patch.append(code, 16)
        patch.set_word(site, _branch(site, target), "dynamic custom extraction/admission")

    previous = _target(0x53370738, patch.word(0x53370738))
    def initializer(at):
        words = []
        for row in custom:
            here = at + len(words) * 4
            body = [0xE92D4070, 0xE1A04000, 0xE1A05002, 0xE3A03001, 0xE1A0C00D,
                    _branch(here + 28, 0x5337073C, link=True), _mov(0, row["identity"]["style_id"]),
                    _branch(here + 36, main_helper, link=True), 0xE280C024, 0xE08C5005]
            for field, offset in enumerate((0, 1, 2, 3, 4, 5, 6, 14, 15)):
                body += [0xE5D51000 | field * 4, 0xE2211004, 0xE5C41000 | offset]
            body += [0xE8BD8070]
            words += [0xE3530000 | row["identity"]["raw_ordinal"],
                      _branch(here + 4, here + (len(body) + 2) * 4, condition=1)] + body
        return words + [0xE3530015, 0x23A03001, _branch(at + (len(words) + 2) * 4, previous)]
    target = patch.append(initializer, 16)
    patch.set_word(0x53370738, _branch(0x53370738, target), "dynamic RAW initializer from heap mirror")
    entries["raw_initializer"] = hex(target)

    # Skip both retired .27 bulk overlays. Their proven factory continuation
    # executes the displaced `mov r1,r6` then resumes the native function.
    def bulk(at):
        words = [0xE92D503F, 0xE5D43B1E, 0xE3530000, 0,
                 0xE5D42C3E, 0xE3520003, 0, 0xE084C002, 0xE5DC3C3F]
        exits = [3, 6]
        for row in custom:
            here = at + len(words) * 4
            body = [0xE1A05002, _mov(0, row["identity"]["style_id"]),
                    _branch(here + 16, main_helper, link=True), 0xE280C024, 0xE08CC005]
            for field, offset in enumerate((0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xC0, 0xCF, 0xD0)):
                body += [0xE5DC3000 | field * 4, 0xE2233004, 0xE5C63000 | offset]
            words += [0xE3530000 | row["identity"]["style_id"],
                      _branch(here + 4, here + (len(body) + 3) * 4, condition=1)] + body
            exits.append(len(words)); words.append(0)
        end = at + len(words) * 4
        words[3] = _branch(at + 12, end, condition=1)
        words[6] = _branch(at + 24, end, condition=8)
        for index in exits[2:]:
            words[index] = _branch(at + index * 4, end)
        return words + [0xE8BD503F, 0xE1A01006, _branch(end + 8, 0x5329F660)]
    target = patch.append(bulk, 16)
    patch.set_word(0x5329F65C, _branch(0x5329F65C, target), "dynamic heap bulk without legacy static overlays")
    entries["bulk"] = hex(target)
    # Every active custom style uses the existing Standard detail widget
    # layout, while its getters/setters keep the independently owned values.
    # Rebuild this route for both official and legacy bases; old .27 prefixes
    # only admitted the first two historical styles.
    def detail_layout(at):
        words = []
        for row in custom:
            here = at + len(words) * 4
            words += [0xE3540000 | row["identity"]["style_id"],
                      0x03500001, 0x03A04000,
                      _branch(here + 12, 0x531C6B58, condition=0)]
        return words + [0xE244400B, _branch(at + (len(words) + 1) * 4, 0x531C6B58)]
    detail = patch.append(detail_layout, 16)
    patch.set_word(0x531C6B54, _branch(0x531C6B54, detail), "dynamic custom scalar detail layout")
    entries["detail_layout"] = hex(detail)
    active_styles = [row["identity"]["style_id"] for row in custom]
    active_raw = [row["identity"]["raw_ordinal"] for row in custom]
    for site, register in ((0x53912CF4, 2), (0x539113FC, 2), (0x5375F0B0, 1),
                           (0x533CC6E0, 1), (0x53373BC0, 1)):
        _alias_retired(patch, site, register, active_styles)
    for site in (0x53373F84, 0x53374118):
        _alias_retired(patch, site, 1, active_raw, minimum=21, standard=1)
    for site in GETTERS + SETTERS:
        _alias_retired(patch, site, 3, active_styles)
    for site in DEFAULT_ENTRIES:
        _alias_retired(patch, site, 1, active_styles)
    visible_factory = [row["identity"]["style_id"] for row in rows
                       if row["identity"]["style_id"] in NATIVE_FACTORY_STYLE_IDS]
    visibility = _install_native_visibility(patch, visible_factory)
    # Install this after the retired-custom wrapper so the native Mono route
    # survives both active custom identities and the Standard fallback.  The
    # Mono list is per-row, derived from the editor visibility switches.
    visible_mono = [style for style in NATIVE_MONO_STYLES if style in visible_factory]
    native_mono = _install_native_mono_admission(patch, unlock_native_mono, visible_mono)
    entries["admission"] = native_mono.get("wrapper") or hex(_target(0x533CC6E0, patch.word(0x533CC6E0)))
    return {"order": order, "entries": entries, "raw_selector_capacity": selector_capacity,
            "retired_identity_fallback": "Standard style11/RAW1",
            "native_visibility": visibility,
            "native_mono": native_mono,
            "native_mono_rows_in_order": visible_mono}


def resolve_template_id(row, originals, drafts):
    """Resolve a copied draft's template chain to an imported slot."""
    seen = {str(row["id"])}
    key = str(row.get("template_id"))
    while key not in originals:
        if key in seen:
            raise ValueError("新增槽位的模板链循环")
        seen.add(key)
        parent = drafts.get(key)
        if parent is None:
            raise ValueError("新增槽位必须引用已有模板")
        key = str(parent.get("template_id"))
    return key


def plan_registry(prior, new, next_style=None):
    old = {str(row["id"]): row for row in prior}
    drafts = {str(row["id"]): row for row in new}
    new_ids = [str(row["id"]) for row in new]
    if len(old) != len(prior) or len(new_ids) != len(set(new_ids)):
        raise ValueError("滤镜标识必须唯一")
    active_ids = {str(row["id"]) for row in new if row.get("enabled", True)}
    if not active_ids:
        raise ValueError("至少保留一项活动滤镜")
    result, added = deepcopy(new), []
    next_style = max(next_style or 36, max((row["identity"]["style_id"] + 1 for row in prior if row.get("kind") == "custom"), default=36))
    menu_position = 0
    for row in result:
        original = old.get(str(row["id"]))
        if original:
            if any(row.get("identity", {}).get(key) != original.get("identity", {}).get(key) for key in ("style_id", "profile_id", "text_id", "icon_id", "maker_note_code")):
                raise ValueError("已存在槽位的内部编号保持稳定")
            if row.get("kind") != original.get("kind"):
                raise ValueError("已存在滤镜种类不能改写")
        else:
            if row.get("identity") or row.get("kind") != "custom":
                raise ValueError("新增槽位必须让构建器统一分配空白身份")
            resolve_template_id(row, old, drafts)
            if next_style > 90:
                raise ValueError("文字目录896项的合法编号已用尽")
            style = next_style
            next_style += 1
            row["identity"] = {"style_id": style, "profile_id": style + 1,
                               "raw_ordinal": style - 13, "text_id": style + 805,
                               "icon_id": style + 629, "maker_note_code": 0x8106 + style - 34,
                               "playback_property": style - 13}
            added.append(str(row["id"]))
        if row.get("kind") == "custom":
            identity = row["identity"]
            identity.setdefault("playback_property", 21 + identity["style_id"] - 34)
            identity.setdefault("raw_ordinal", 21 + identity["style_id"] - 34)
        if row.get("enabled", True):
            menu_position += 1
            row["identity"]["menu_ordinal"] = menu_position
        else:
            row["identity"]["menu_ordinal"] = None
    active = [row for row in result if row.get("kind") == "custom" and row.get("enabled", True)]
    if len(active) > MAX_ACTIVE_CUSTOM_SLOTS:
        raise ValueError(f"当前后端最多支持{MAX_ACTIVE_CUSTOM_SLOTS}项活动自定义滤镜")
    return {"filters": result, "newslots_supported": bool(added), "added_slots": added,
            "removed_slots": [key for key in old if key not in new_ids], "next_style": next_style,
            "custom_capacity": MAX_ACTIVE_CUSTOM_SLOTS,
            "capacity_basis": "Supported registry size; native RAW and menu vectors grow beyond their initial reserve"}


def supports_image(rtos):
    try:
        if sha(rtos) in (KNOWN27_SHA, OFFICIAL7_SHA):
            return True
        metadata = _metadata(rtos)
        if metadata is None:
            return False
        if metadata.get("schema_version", 0) < 2:
            return False
        _canonical_source(rtos, metadata)
        return True
    except (ValueError, TypeError, struct.error):
        return False


def apply_filter_registry(rtos, icon_bytes, filters, prior_filters, *, baseline_rtos=None, baseline_icons=None,
                          crops=None, prior_crops=None, crop_context=None, shutdown_plan=None, soft_glow_plan=None,
                          af_phase_plan=None, af_reacquire_plan=None, unlock_native_mono=None):
    """Recompile active recipes from their exact, locally supplied native base."""
    from types import SimpleNamespace
    from .filter_reader import read_filters
    from .native_raw import migrate_raw_arrays
    from .registry_resources import rebuild_resources
    source = bytes(rtos)
    # Keep the pre-row API's bool form compatible for callers that still use
    # it directly.  The editor now passes a schema object derived from each
    # row's visibility switch, so only that new form can intentionally hide a
    # subset of factory Mono rows.
    legacy_mono_switch = type(unlock_native_mono) is bool
    if not supports_image(source):
        raise ValueError("当前写入后端仅接受已识别的官方输入或可恢复的编辑器产物")
    metadata = _metadata(source) or {}
    unlock_native_mono = _native_mono_option(unlock_native_mono, metadata)
    # An imported experiment must survive canonical recompilation. Fresh
    # sources stay off; explicit off removes its hooks through the same base.
    if af_phase_plan is None:
        af_phase_mode = metadata.get('af_phase', {}).get('mode', 'off')
    elif (not isinstance(af_phase_plan, dict) or set(af_phase_plan) != {'mode'}):
        raise ValueError('AF 实验编译计划必须只包含 mode')
    else:
        af_phase_mode = af_phase_plan['mode']
    if af_phase_mode not in ('off', 'shadow', 'conflict_guard'):
        raise ValueError('AF 实验模式必须是 off、shadow 或 conflict_guard')
    if metadata.get('af_phase'):
        from .autofocus_phase import inspect_af_phase
        prior_af_phase = inspect_af_phase(source, metadata)
        if (prior_af_phase.get('status') != 'verified'
                or prior_af_phase.get('owner_installation') != 'verified'):
            raise ValueError('输入 AF 实验专项读回失败：' + prior_af_phase.get('reason', '入口或动态存储安装未验证'))
    if af_reacquire_plan is None:
        af_reacquire_mode = metadata.get('af_reacquire', {}).get('mode', 'off')
    elif not isinstance(af_reacquire_plan, dict) or set(af_reacquire_plan) != {'mode'}:
        raise ValueError('自动区域 AF 实验计划必须只包含 mode')
    else:
        af_reacquire_mode = af_reacquire_plan['mode']
    if af_reacquire_mode not in ('off', 'phase_repeat'):
        raise ValueError('自动区域 AF 实验模式必须是 off 或 phase_repeat')
    if metadata.get('af_reacquire'):
        from .autofocus_reacquire import inspect_af_reacquire
        prior_reacquire = inspect_af_reacquire(source, metadata)
        if prior_reacquire.get('status') != 'verified':
            raise ValueError('输入自动区域 AF 实验读回失败：' + prior_reacquire.get('reason', '入口不一致'))
    if af_reacquire_mode != 'off' and af_phase_mode != 'off':
        raise ValueError('两种 AF 实验尚未验证组合，请一次只启用一种')
    if soft_glow_plan is None and metadata.get('soft_glow'):
        soft_glow_plan = {'enabled': True}
    if (soft_glow_plan is not None and
            (not isinstance(soft_glow_plan, dict) or set(soft_glow_plan) != {'enabled'} or
             type(soft_glow_plan['enabled']) is not bool)):
        raise ValueError('柔光编译计划必须明确是否启用独立机内菜单')
    crop_plan = None
    if crops is not None or crop_context or metadata.get('crop_compiler'):
        from .crop_compiler import prepare_crops
        crop_plan, source_crop_inspection = prepare_crops(source, metadata, crops, prior_crops, crop_context)
    actual = read_filters(SimpleNamespace(rtos=source, icon_bytes=bytes(icon_bytes), editable=True))
    actual_by_style = {row["identity"]["style_id"]: row for row in actual}
    if {row["identity"]["style_id"] for row in prior_filters} != set(actual_by_style):
        raise ValueError("输入与已读取的原生滤镜列表不一致")
    for row in prior_filters:
        observed = actual_by_style[row["identity"]["style_id"]]
        # A hidden recipe can be retained in the footer as identity-only
        # metadata.  It has no active resources to validate until it is
        # explicitly rebuilt from a complete custom row.
        if (row.get("kind") == "custom" and not row.get("enabled", True)
                and "hex" not in row.get("resources", {}).get("icon", {})):
            continue
        if row["name"] != observed["name"] or len(row["resources"].get("banks", [])) != 3:
            raise ValueError("输入与已读取名称/资源不一致")
        for bank, real in zip(row["resources"]["banks"], observed["resources"]["banks"]):
            if any(bank[key]["hex"] != real[key]["hex"] for key in ("camera_basis", "matrix", "multi_main", "multi_aux", "gamma")):
                raise ValueError("输入与已读取颜色资源不一致")
        if row["resources"]["icon"]["hex"] != observed["resources"]["icon"]["hex"]:
            raise ValueError("输入与已读取图标不一致")
    known = {str(row["id"]): deepcopy(row) for row in prior_filters}
    for record in metadata.get("recipe_registry", metadata.get("registry", [])):
        key = str(record["id"])
        if key not in known:
            known[key] = {"id": record["id"], "kind": "custom", "identity": deepcopy(record["identity"]), "enabled": False}
    desired = deepcopy(filters)
    if legacy_mono_switch:
        for row in desired:
            if row.get("identity", {}).get("style_id") in NATIVE_MONO_STYLES:
                row["enabled"] = bool(unlock_native_mono)
    for row in desired:
        old = known.get(str(row["id"]))
        if old and not row.get("identity"):
            row["identity"] = deepcopy(old["identity"])
    planned = plan_registry(list(known.values()), desired, metadata.get("next_style"))
    recipes = planned["filters"]
    rows = [row for row in recipes if row.get("enabled", True)]
    custom = sorted((row for row in rows if row.get("kind") == "custom"), key=lambda row: row["identity"]["style_id"])
    base = bytes(baseline_rtos) if baseline_rtos is not None else _canonical_source(source, metadata)
    base_sha = sha(base)
    if base_sha not in (KNOWN27_SHA, OFFICIAL7_SHA):
        raise ValueError("compiler base 必须是已识别的原始 RTOS SHA256")
    patch = _Patch(base)
    patch.official_base = base_sha == OFFICIAL7_SHA
    if patch.official_base:
        from .official_scaffold import initialize_scaffold
        scaffold_report = initialize_scaffold(patch)
    else:
        scaffold_report = None
    fallback_report = _restore_scalar_fallbacks(patch)
    base_icon_size = metadata.get("compiler_icon_size", OFFICIAL7_ICON_SIZE if patch.official_base else 14493280)
    if type(base_icon_size) is not int or base_icon_size <= 0 or base_icon_size > len(icon_bytes):
        raise ValueError("编译器图标基线长度无效")
    compiler_icons = bytes(baseline_icons) if baseline_icons is not None else bytes(icon_bytes[:base_icon_size])
    if metadata.get("schema_version", 0) >= 2:
        prefix = bytes(icon_bytes[:base_icon_size])
        if sha(prefix) != metadata.get("compiler_icon_sha256"):
            raise ValueError("编译器 ICON 基线前缀 SHA256 不一致")
        compiler_icons = prefix
    icons, resource_report = rebuild_resources(patch, compiler_icons, rows)
    row_count = max([21] + [row["identity"]["raw_ordinal"] + 1 for row in custom])
    state_start = 0x800 + row_count * 80
    layouts = {}
    for index, row in enumerate(custom):
        style, start = row["identity"]["style_id"], state_start + index * 468
        layouts[style] = {"style_id": style, "main_offset": start, "mirror_offset": start + 36,
                          "shadow_offset": start + 72, "record": 16 + style - 34,
                          "bytes": 396, "snapshot_count": 10}
        row["identity"]["apdata_record"] = 16 + style - 34
    state_report = _install_state(patch, custom, layouts)
    main_helper = int(state_report["symbols"]["registry_main"], 16)
    initialize = int(state_report["symbols"]["registry_initialize_owner"], 16)
    shutdown_report = None
    shutdown_extra = 0
    shutdown_record = None
    if shutdown_plan is not None:
        from .shutdown_compiler import install_jpeg_table, next_record, menu_metadata
        from .shutdown_state import install_shutdown_state, OWNER_BYTES
        from .shutdown_menu import install_shutdown_menu
        from .shutdown_preview import install_shutdown_previews
        icons, shutdown_previews = install_shutdown_previews(patch, icons, shutdown_plan)
        preview_by_choice = {row['selection_id']: row['icon_id'] for row in shutdown_previews['custom']}
        factory_preview_icons = {row['native_id']: row['icon_id'] for row in shutdown_previews['factory']}
        shutdown_record = next_record(metadata, recipes)
        shutdown_state = install_shutdown_state(patch, record=shutdown_record,
                                               owner_offset=state_start + len(custom) * 468,
                                               choices=[{'selection_id': row['selection_id'], 'native_id': row['native_id']}
                                                        for row in shutdown_plan['presets']],
                                               legacy_choices=shutdown_plan.get('legacy_choices', {}))
        shutdown_symbols = {key: int(value, 16) for key, value in shutdown_state['symbols'].items()}
        prior_initialize = initialize
        initialize = patch.append(lambda at: [0xE92D4010, 0xE1A04000,
                                    _branch(at + 8, prior_initialize, link=True),
                                    0xE1A00004, _branch(at + 16, shutdown_symbols['shutdown_initialize_owner'], link=True),
                                    0xE8BD8010], 16)
        jpeg_table = install_jpeg_table(patch, shutdown_plan)
        shutdown_menu = install_shutdown_menu(patch, shutdown_symbols['shutdown_get'], shutdown_symbols['shutdown_set'],
                                             shutdown_symbols['shutdown_save_global'],
                                             entries=[{'selection_id': row['selection_id'], 'name': row['name'],
                                                       'preview_icon_id': preview_by_choice[row['selection_id']]}
                                                      for row in shutdown_plan['presets']],
                                             factory_preview_icons=factory_preview_icons)
        shutdown_report = menu_metadata(shutdown_plan, jpeg_table, shutdown_state, shutdown_menu, shutdown_previews)
        shutdown_extra = OWNER_BYTES
    soft_glow_report = None
    soft_glow_extra = 0
    if soft_glow_plan and soft_glow_plan['enabled']:
        from .soft_glow_state import install_soft_glow_state, next_record, OWNER_BYTES
        soft_glow_state = install_soft_glow_state(
            patch, record=next_record(metadata, recipes, shutdown_record=shutdown_record),
            owner_offset=state_start + len(custom) * 468 + shutdown_extra)
        soft_glow_symbols = {key: int(value, 16) for key, value in soft_glow_state['symbols'].items()}
        prior_initialize = initialize
        initialize = patch.append(lambda at: [0xE92D4010, 0xE1A04000,
                                    _branch(at + 8, prior_initialize, link=True),
                                    0xE1A00004, _branch(at + 16, soft_glow_symbols['soft_glow_initialize_owner'], link=True),
                                    0xE8BD8010], 16)
        soft_glow_report = {'schema_version': 1, 'enabled': True,
                            'record': soft_glow_state['record'], 'state': soft_glow_state,
                            'hardware': 'not_tested'}
        soft_glow_extra = OWNER_BYTES
    scalar = [_clone_scalar(patch, row["identity"]["style_id"], main_helper) for row in custom]
    menu = _dynamic_menu_routes(
        patch, rows, custom, main_helper, unlock_native_mono,
        native_order=[row["identity"]["style_id"] for row in prior_filters
                      if row.get("identity", {}).get("style_id") in NATIVE_FACTORY_STYLE_IDS])
    identity = _identity_registry(patch, custom)
    af_phase_report = None
    af_phase_extra = 0
    if af_phase_mode != 'off':
        from .autofocus_phase import install_af_phase
        af_phase_report = install_af_phase(
            patch, mode=af_phase_mode,
            owner_offset=state_start + len(custom) * 468 + shutdown_extra + soft_glow_extra)
        af_initialize = int(af_phase_report['symbols']['af_phase_initialize_owner'], 16)
        prior_initialize = initialize
        initialize = patch.append(lambda at: [0xE92D4010, 0xE1A04000,
                                    _branch(at + 8, prior_initialize, link=True),
                                    0xE1A00004, _branch(at + 16, af_initialize, link=True),
                                    0xE8BD8010], 16)
        af_phase_report['owner_initializer'] = {'wrapper': hex(initialize),
                                              'prior': hex(prior_initialize)}
        af_phase_extra = 48
    af_reacquire_report = None
    if af_reacquire_mode != 'off':
        from .autofocus_reacquire import install_af_reacquire
        af_reacquire_report = install_af_reacquire(patch, mode=af_reacquire_mode)
    raw = migrate_raw_arrays(patch.image, row_count, patch.append, custom_state_bytes=len(custom) * 468 + shutdown_extra + soft_glow_extra + af_phase_extra,
                             active_raw_ordinals=[row["identity"]["raw_ordinal"] for row in custom],
                             init_callback=initialize, official_base=patch.official_base)
    if af_phase_report is not None:
        allocation_site = (int(raw['owner_lifecycle']['allocator'], 16)
                           if patch.official_base else 0x543EBF70)
        clear_half = next(block for block in raw['code_blocks'] if block['name'] == 'clear_half')
        wrapper_at = int(af_phase_report['owner_initializer']['wrapper'], 16)
        wrapper_blob = bytes(patch.image[wrapper_at-BASE:wrapper_at-BASE+24])
        af_phase_report['owner_initializer'].update(
            hook='0x533670b0', hook_word=hex(patch.word(0x533670B0)),
            bytes=len(wrapper_blob), sha256=sha(wrapper_blob), clear_block=clear_half)
        af_phase_report['owner_storage'] = {
            'allocation_site': hex(allocation_site), 'allocation_word': hex(patch.word(allocation_site)),
            'allocation_before': raw['heap_size']-48, 'allocation_after': raw['heap_size'],
            'owner_cell': '0x550885B4', 'offset': af_phase_report['storage']['owner_offset'],
            'bytes': 48, 'no_apdata_record': True}
    crop_report = None
    if crop_plan is not None:
        from .crop_compiler import install_crops
        icons, crop_report = install_crops(patch, crop_plan, icons)
    if soft_glow_report is not None:
        from .soft_glow_menu import install_soft_glow_menu
        from .soft_glow_routes import install_soft_glow_routes
        soft_glow_report['menu'] = install_soft_glow_menu(
            patch, soft_glow_symbols['soft_glow_get'], soft_glow_symbols['soft_glow_set'],
            soft_glow_symbols['soft_glow_save_global'])
        soft_glow_report['routes'] = install_soft_glow_routes(patch, soft_glow_symbols['soft_glow_get'])
    if len(icons) < len(icon_bytes):
        icons += bytes(len(icon_bytes) - len(icons))
    # Preserve the imported numbering until repack_image assigns the next
    # container-wide version. Version words are restored in compiler metadata.
    for address in VERSION_SITES:
        patch.set_word(address, _word(source, address), "retain input version for container repacker")
    records = {key: {"id": value["id"], "identity": value["identity"], "enabled": False}
               for key, value in known.items() if value.get("kind") == "custom"}
    for row in recipes:
        if row.get("kind") == "custom":
            records[str(row["id"])] = {"id": row["id"], "identity": row["identity"],
                                       "enabled": row.get("enabled", True), "defaults": _defaults(row),
                                       "parameter_enabled": _parameter_enabled(row)}
    output_metadata = {
        "schema_version": 3 if patch.official_base else 2, "compiler": "canonical-registry-v3" if patch.official_base else "canonical-registry-v2", "next_style": planned["next_style"],
        "base_kind": "official-1.11.10.7" if patch.official_base else "legacy-1.11.10.27",
        "compiler_base_sha256": base_sha,
        "compiler_base_size": len(base), "compiler_base_restore": _canonical_restore(base, patch.image),
        "compiler_icon_size": base_icon_size, "compiler_icon_sha256": sha(compiler_icons),
        "custom_capacity": len(custom), "state": state_report,
        "native_mono": {**menu.get("native_mono", {}),
                        "unlock_native_mono": unlock_native_mono,
                        "menu_order": list(menu.get("order", [])),
                        "native_rows_present": list(menu.get("native_mono_rows_in_order", [])),
                        "scope": "color-mode admission plus factory menu visibility; factory identity/resources remain read-only"},
        "native_visibility": {str(style): style in {
            row["identity"]["style_id"] for row in rows
            if row["identity"]["style_id"] in NATIVE_FACTORY_STYLE_IDS
        } for style in NATIVE_FACTORY_STYLE_IDS},
        "registry": [{"id": row["id"], "identity": row["identity"], "defaults": _defaults(row),
                      "parameter_enabled": _parameter_enabled(row)} for row in custom],
        "recipe_registry": list(records.values()),
    }
    if shutdown_report is not None:
        output_metadata['shutdown_menu'] = shutdown_report
    if soft_glow_report is not None:
        output_metadata['soft_glow'] = soft_glow_report
    if af_phase_report is not None:
        output_metadata['af_phase'] = af_phase_report
    if af_reacquire_report is not None:
        output_metadata['af_reacquire'] = af_reacquire_report
    if crop_plan is not None:
        from .crop_compiler import crop_metadata, freeze_readback
        from .crops import read_crops
        output_metadata.update(crop_metadata(crop_plan))
        output_metadata['crop_icons'] = crop_report['icons']
        # Prove the instructions first, then persist their complete actual
        # geometry. The provisional footer is never saved or exposed.
        provisional = _finish(bytearray(patch.image), output_metadata, minimum_size=len(source))
        crop_readback, crop_inspection = read_crops(provisional)
        freeze_readback(crop_plan, crop_readback, crop_inspection)
        output_metadata.update(crop_metadata(crop_plan, verified=True))
        crop_report.update(native_readback=crop_inspection, source_readback=source_crop_inspection,
                           crops=crop_plan['crops'], records=crop_plan['records'],
                           next_public=crop_plan['next_public'],
                           crop_edits_compiled=crop_plan['changed_from_input'],
                           crop_consumers_installed=True)
    result = _finish(patch.image, output_metadata, minimum_size=len(source))
    if BASE + len(result) >= 0x55000000:
        raise ValueError('完整注册表追加区碰到已知运行数据映射')
    native_mono_readback = inspect_native_mono_admission(result)
    expected_native_mono_status = "passed" if unlock_native_mono else "disabled"
    if native_mono_readback.get("status") != expected_native_mono_status:
        raise ValueError("生成结果的原厂 Mono admission 读回失败：" + str(native_mono_readback))
    af_phase_readback = {'installed': False, 'mode': 'off', 'status': 'not_installed', 'hardware': 'not_tested'}
    if af_phase_report is not None:
        from .autofocus_phase import inspect_af_phase
        af_phase_readback = inspect_af_phase(result, _metadata(result))
        if (af_phase_readback.get('status') != 'verified'
                or af_phase_readback.get('mode') != af_phase_mode
                or af_phase_readback.get('owner_installation') != 'verified'):
            raise ValueError('生成 AF 实验专项读回失败：' + af_phase_readback.get('reason', '模式、入口或动态存储安装不一致'))
    af_reacquire_readback = {'installed': False, 'mode': 'off', 'status': 'not_installed', 'hardware': 'not_tested'}
    if af_reacquire_report is not None:
        from .autofocus_reacquire import inspect_af_reacquire
        af_reacquire_readback = inspect_af_reacquire(result, _metadata(result))
        if af_reacquire_readback.get('status') != 'verified':
            raise ValueError('生成自动区域 AF 实验读回失败：' + af_reacquire_readback.get('reason', '入口不一致'))
    if crop_report is not None:
        from .crop_icons import verify_crop_icons
        crop_report['icon_readback'] = verify_crop_icons(
            SimpleNamespace(rtos=result, icon_bytes=icons), crop_report['icons'])
    readback = read_filters(SimpleNamespace(rtos=result, icon_bytes=icons, editable=True))
    active_readback = [row for row in readback if row.get("enabled", True)]
    if [row["identity"]["style_id"] for row in active_readback] != [row["identity"]["style_id"] for row in rows]:
        raise ValueError("动态注册表实际菜单读回不一致")
    unknown_entries = [row["inspection_notes"] for row in readback
                       if row["inspection_notes"] and not (
                           row.get("kind") == "custom" and not row.get("enabled", True)
                           and "hex" not in row.get("resources", {}).get("icon", {}))]
    if unknown_entries:
        raise ValueError("动态注册表原生读回出现未知入口: " + str(unknown_entries))
    report = {"backend": "canonical-dynamic-registry-v3" if patch.official_base else "canonical-dynamic-registry-v2", "filters": readback, "recipe_filters": recipes,
              "source_rtos_sha256": sha(source), "rtos_sha256": sha(result), "icon_sha256": sha(icons),
              "registry_plan": {key: value for key, value in planned.items() if key != "filters"},
              "registry": {"resources": resource_report, "scalar": scalar, "menu": menu, "raw": raw,
                           "identity": identity, "factory_fallbacks": fallback_report},
              "state": state_report, "changes": patch.changes, "newslots_supported": True,
              "base_kind": output_metadata["base_kind"], "scaffold": scaffold_report,
              "defaults_initialized": True, "identity_and_state_routes_preserved": True,
              "hardware_validation": "not_tested",
              "limitations": ["离线候选；实机启动/显示/ISP/断电保存尚未验证", "视频九参数遵循原mode限制"]}
    report["native_mono"] = {**native_mono_readback,
                              "menu_order": list(menu.get("order", [])),
                              "native_rows_present": list(menu.get("native_mono_rows_in_order", [])),
                              "scope": "color-mode admission plus factory menu visibility; factory identity/resources remain read-only"}
    report["native_visibility"] = output_metadata["native_visibility"]
    if crop_report is not None:
        report['crops'] = crop_report
    report['shutdown_menu'] = ({**shutdown_report, 'applied': True} if shutdown_report is not None else {'applied': False})
    report['soft_glow'] = ({**soft_glow_report, 'applied': True} if soft_glow_report is not None else {'applied': False})
    report['af_phase'] = {**(af_phase_report or {}), **af_phase_readback,
                          'applied': af_phase_report is not None}
    report['af_reacquire'] = {**(af_reacquire_report or {}), **af_reacquire_readback,
                              'applied': af_reacquire_report is not None}
    return result, icons, report
