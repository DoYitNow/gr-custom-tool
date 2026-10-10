# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Build only crop and End Screen candidates from the user's official input."""
import hashlib
import json
import struct
from types import SimpleNamespace

BASE = 0x53000000
OFFICIAL7_SHA = "a9a9a51530c35819df47dee1cce9f4f560a17eeed1da258950167d282884fce1"
OFFICIAL7_ICON_SIZE = 14480480
ICON_CATALOG = 0x543D3AC0
TEXT_CATALOG = 0x543D4AC0
FOOTER_MAGIC = b"FWDEMO01"
LEGACY_FOOTER_MAGIC = b"GR4REG01"
COMPILER = "demo-crop-shutdown-v1"
VERSION_SITES = (0x53FE50F8, 0x53A78498, 0x53A784BC)


def sha(data):
    return hashlib.sha256(data).hexdigest()


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


def _half(word):
    return ((word >> 4) & 0xF000) | (word & 0xFFF)


def _read(data, address, size):
    if not BASE <= address <= address + size <= BASE + len(data):
        raise ValueError("资源范围超出 RTOS")
    return bytes(data[address - BASE:address - BASE + size])


def _word(data, address):
    return struct.unpack("<I", _read(data, address, 4))[0]


def _toolchain():
    from .toolchain import toolchain
    return toolchain()


class _Patch:
    def __init__(self, data):
        self.image = bytearray(data)
        self.changes = []
        self.official_base = True

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
        self.changes.append({"address": hex(address), "before": hex(before),
                             "after": hex(value), "reason": reason})

    def prefix(self, site, make, reason):
        previous = _target(site, self.word(site))
        address = self.append(lambda at: make(at) + [_branch(at + len(make(at)) * 4, previous)], 16)
        self.set_word(site, _branch(site, address), reason)
        return address


def _body_sha(data):
    value = bytearray(data)
    for address in VERSION_SITES:
        if address + 4 <= BASE + len(value):
            value[address - BASE:address - BASE + 4] = bytes(4)
    return sha(value)


def read_registry(data):
    """Read demo recovery records; former editor records remain read-only."""
    if len(data) < 16 or data[-16:-8] not in (FOOTER_MAGIC, LEGACY_FOOTER_MAGIC):
        return None
    magic = data[-16:-8]
    length = struct.unpack_from("<I", data, len(data) - 8)[0]
    start = len(data) - 16 - length
    if start < 0:
        raise ValueError("编辑器恢复记录尾部无效")
    try:
        metadata = json.loads(data[start:start + length])
    except (ValueError, UnicodeError) as exc:
        raise ValueError("编辑器恢复记录尾部无效") from exc
    if not isinstance(metadata, dict) or metadata.get("body_sha256") != _body_sha(data[:start]):
        raise ValueError("编辑器 RTOS 内容校验失败")
    if magic == LEGACY_FOOTER_MAGIC:
        return {**metadata, "demo_read_only": True}
    if (metadata.get("compiler") != COMPILER or metadata.get("schema_version") != 3 or
            metadata.get("base_kind") != "official-1.11.10.7" or
            metadata.get("compiler_base_sha256") != OFFICIAL7_SHA or
            metadata.get("base_rtos_sha256") != OFFICIAL7_SHA):
        raise ValueError("Demo 官方编译基线声明无效")
    return metadata


_metadata = read_registry


def canonical_base(source):
    """Recover exact official .7 from its bytes or this demo's own records."""
    source = bytes(source)
    if sha(source) == OFFICIAL7_SHA:
        return source
    metadata = read_registry(source)
    if not metadata or metadata.get("compiler") != COMPILER or metadata.get("demo_read_only"):
        raise ValueError("Demo 仅支持已适配的官方 1.11.10.7 或本 Demo 生成的候选")
    size = metadata.get("compiler_base_size")
    if type(size) is not int or size <= 0 or size > len(source) or size % 4:
        raise ValueError("Demo 官方基线长度无效")
    restored = bytearray(source[:size])
    for row in metadata.get("compiler_base_restore", []):
        offset, raw = row.get("offset"), bytes.fromhex(row.get("hex", ""))
        if type(offset) is not int or offset < 0 or len(raw) != 4 or offset + 4 > size:
            raise ValueError("Demo 官方基线恢复记录无效")
        restored[offset:offset + 4] = raw
    if sha(restored) != OFFICIAL7_SHA:
        raise ValueError("Demo 官方基线 SHA256 无法还原")
    return bytes(restored)


_canonical_source = canonical_base


def prepare_update_policy(rtos, allow_older=True):
    """Apply or remove the reviewed RTOS version gate before packaging.

    Official input has no editor footer, so the transform operates directly
    on its RTOS bytes.  Demo candidates carry a recovery footer; in that case
    transform only the body and refresh the footer proof so the canonical
    official input remains recoverable.
    """
    if type(allow_older) is not bool:
        raise ValueError("低版本固件支持开关必须是布尔值")
    from .update_policy import apply_update_policy, inspect_update_policy, remove_update_policy

    source = bytes(rtos)
    try:
        metadata = read_registry(source)
    except ValueError:
        # An otherwise valid firmware may carry a registry footer from the
        # research editor or another producer.  It remains a generic input;
        # the policy transform itself does not depend on that footer schema.
        metadata = None
    transform = apply_update_policy if allow_older else remove_update_policy
    if metadata is None:
        return transform(source)

    if metadata.get("demo_read_only"):
        try:
            footer_size = 16 + struct.unpack_from("<I", source, len(source) - 8)[0]
            if footer_size > len(source):
                raise ValueError("编辑器恢复记录尾部无效")
            body, proof = transform(source[:-footer_size])
        except struct.error as exc:
            raise ValueError("编辑器恢复记录尾部无效") from exc
        policy = inspect_update_policy(body)
        if body == source[:-footer_size] and metadata.get("update_policy") == policy:
            return source, proof
        foreign = {key: value for key, value in metadata.items() if key != "demo_read_only"}
        foreign["update_policy"] = policy
        output = _finish_registry(bytearray(body), foreign, source[-16:-8], minimum_size=len(source))
        return output, proof

    base = canonical_base(source)
    try:
        footer_size = 16 + struct.unpack_from("<I", source, len(source) - 8)[0]
    except struct.error as exc:
        raise ValueError("编辑器恢复记录尾部无效") from exc
    if footer_size > len(source):
        raise ValueError("编辑器恢复记录尾部无效")
    body, proof = transform(source[:-footer_size])
    policy = inspect_update_policy(body)
    if body == source[:-footer_size] and metadata.get("update_policy") == policy:
        return source, proof
    metadata = {**metadata, "update_policy": policy,
                "compiler_base_restore": _canonical_restore(base, body)}
    output = _finish(bytearray(body), metadata, minimum_size=len(source))
    if canonical_base(output) != base:
        raise ValueError("版本策略修改后无法恢复编译基线")
    return output, proof


def supports_image(rtos):
    try:
        canonical_base(rtos)
        return True
    except (ValueError, TypeError, KeyError, struct.error):
        return False


def _canonical_restore(base, body):
    if len(body) < len(base):
        raise ValueError("Demo 编译器损坏了官方基线前缀")
    before = memoryview(base).cast("I")
    after = memoryview(body)[:len(base)].cast("I")
    versions = {address - BASE for address in VERSION_SITES}
    return [{"offset": index * 4, "hex": struct.pack("<I", old).hex()}
            for index, (old, new) in enumerate(zip(before, after))
            if old != new or index * 4 in versions]


def _finish(image, metadata, minimum_size=0):
    image.extend(bytes((-len(image)) % 4))
    preview = {**metadata, "base_rtos_sha256": OFFICIAL7_SHA, "body_sha256": "0" * 64}
    estimated = json.dumps(preview, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    footer_size = ((len(estimated) + 3) & ~3) + 16
    required = max(len(image), minimum_size - footer_size)
    image.extend(bytes(max(0, ((required + 3) & ~3) - len(image))))
    metadata = {**metadata, "base_rtos_sha256": OFFICIAL7_SHA, "body_sha256": _body_sha(image)}
    encoded = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    encoded += b" " * ((-len(encoded)) % 4)
    image.extend(encoded + FOOTER_MAGIC + struct.pack("<I", len(encoded)) + bytes(4))
    total = sum(struct.unpack("<%dI" % (len(image) // 4), image))
    struct.pack_into("<I", image, len(image) - 4, (-total) & 0xFFFFFFFF)
    return bytes(image)


def _finish_registry(image, metadata, magic, minimum_size=0):
    """Refresh a foreign recovery footer without changing its producer tag."""
    image = bytearray(image)
    image.extend(bytes((-len(image)) % 4))
    preview = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    footer_size = ((len(preview) + 3) & ~3) + 16
    required = max(len(image), minimum_size - footer_size)
    image.extend(bytes(max(0, ((required + 3) & ~3) - len(image))))
    metadata = {**metadata, "body_sha256": _body_sha(image)}
    encoded = json.dumps(metadata, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    encoded += b" " * ((-len(encoded)) % 4)
    image.extend(encoded + bytes(magic) + struct.pack("<I", len(encoded)) + bytes(4))
    total = sum(struct.unpack("<%dI" % (len(image) // 4), image))
    struct.pack_into("<I", image, len(image) - 4, (-total) & 0xFFFFFFFF)
    return bytes(image)


def wrap_official_input(rtos, icon_bytes, *, minimum_size=0):
    """Append recovery notes to exact official .7 without installing hooks.

    Call before changing container versions. The entire official RTOS prefix
    and ICONBIN remain unchanged; future demo builds can recover their input.
    """
    base = bytes(rtos)
    if sha(base) != OFFICIAL7_SHA or len(icon_bytes) != OFFICIAL7_ICON_SIZE:
        raise ValueError("回退尾注仅适用于已适配的官方 1.11.10.7 原始输入")
    metadata = {
        "schema_version": 3, "compiler": COMPILER, "base_kind": "official-1.11.10.7",
        "compiler_base_sha256": OFFICIAL7_SHA, "compiler_base_size": len(base),
        "compiler_base_restore": _canonical_restore(base, base),
        "compiler_icon_size": len(icon_bytes), "compiler_icon_sha256": sha(icon_bytes),
        "features_installed": False,
    }
    return _finish(bytearray(base), metadata, minimum_size=minimum_size)


def build_features(rtos, icon_bytes, *, crop_plan=None, source_crop_inspection=None,
                   shutdown_plan=None):
    """Return RTOS, ICONBIN and a crop/End Screen build report."""
    from .official_scaffold import initialize_scaffold
    source = bytes(rtos)
    metadata = read_registry(source) or {}
    base = canonical_base(source)
    icon_size = metadata.get("compiler_icon_size", OFFICIAL7_ICON_SIZE)
    if icon_size != OFFICIAL7_ICON_SIZE or icon_size > len(icon_bytes):
        raise ValueError("Demo ICON 官方基线长度无效")
    compiler_icons = bytes(icon_bytes[:icon_size])
    if metadata and sha(compiler_icons) != metadata.get("compiler_icon_sha256"):
        raise ValueError("Demo ICON 官方基线前缀 SHA256 不一致")
    patch = _Patch(base)
    scaffold = initialize_scaffold(patch)
    icons = compiler_icons
    shutdown_report = None
    if shutdown_plan is not None:
        from .shutdown_compiler import install_jpeg_table, next_record, menu_metadata
        from .shutdown_state import install_shutdown_state
        from .shutdown_menu import install_shutdown_menu
        from .shutdown_preview import install_shutdown_previews
        icons, previews = install_shutdown_previews(patch, icons, shutdown_plan)
        custom_icons = {row["selection_id"]: row["icon_id"] for row in previews["custom"]}
        factory_icons = {row["native_id"]: row["icon_id"] for row in previews["factory"]}
        state = install_shutdown_state(patch, record=next_record(metadata),
                choices=[{"selection_id": row["selection_id"], "native_id": row["native_id"]}
                         for row in shutdown_plan["presets"]],
                legacy_choices=shutdown_plan.get("legacy_choices", {}))
        symbols = {key: int(value, 16) for key, value in state["symbols"].items()}
        jpeg_table = install_jpeg_table(patch, shutdown_plan)
        menu = install_shutdown_menu(patch, symbols["shutdown_get"], symbols["shutdown_set"],
                symbols["shutdown_save_global"],
                entries=[{"selection_id": row["selection_id"], "name": row["name"],
                          "preview_icon_id": custom_icons[row["selection_id"]]}
                         for row in shutdown_plan["presets"]], factory_preview_icons=factory_icons)
        shutdown_report = menu_metadata(shutdown_plan, jpeg_table, state, menu, previews)
    crop_report = None
    if crop_plan is not None:
        from .crop_compiler import install_crops, crop_metadata, freeze_readback
        from .crops import read_crops
        icons, crop_report = install_crops(patch, crop_plan, icons)
    if len(icons) < len(icon_bytes):
        icons += bytes(len(icon_bytes) - len(icons))
    for address in VERSION_SITES:
        patch.set_word(address, _word(source, address), "retain input version for container repacker")
    output_metadata = {
        "schema_version": 3, "compiler": COMPILER, "base_kind": "official-1.11.10.7",
        "compiler_base_sha256": OFFICIAL7_SHA, "compiler_base_size": len(base),
        "compiler_base_restore": _canonical_restore(base, patch.image),
        "compiler_icon_size": icon_size, "compiler_icon_sha256": sha(compiler_icons),
    }
    if shutdown_report is not None:
        output_metadata["shutdown_menu"] = shutdown_report
    if crop_plan is not None:
        output_metadata.update(crop_metadata(crop_plan))
        output_metadata["crop_icons"] = crop_report["icons"]
        provisional = _finish(bytearray(patch.image), output_metadata, minimum_size=len(source))
        readback, inspection = read_crops(provisional)
        freeze_readback(crop_plan, readback, inspection)
        output_metadata.update(crop_metadata(crop_plan, verified=True))
        crop_report.update(native_readback=inspection, source_readback=source_crop_inspection,
                crops=crop_plan["crops"], records=crop_plan["records"], next_public=crop_plan["next_public"],
                crop_edits_compiled=crop_plan["changed_from_input"], crop_consumers_installed=True)
    result = _finish(patch.image, output_metadata, minimum_size=len(source))
    if BASE + len(result) >= 0x55000000:
        raise ValueError("Demo 追加区域碰到已知运行数据映射")
    if crop_report is not None:
        from .crop_icons import verify_crop_icons
        crop_report["icon_readback"] = verify_crop_icons(SimpleNamespace(rtos=result, icon_bytes=icons), crop_report["icons"])
    report = {
        "backend": COMPILER, "source_rtos_sha256": sha(source), "rtos_sha256": sha(result),
        "icon_sha256": sha(icons), "base_kind": output_metadata["base_kind"],
        "canonical_base": {"rtos_sha256": OFFICIAL7_SHA, "version": "1.11.10.7",
                           "source": "user official input or demo recovery records"},
        "scaffold": scaffold, "changes": patch.changes, "hardware_validation": "not_tested",
        "limitations": ["源码接入候选；实机启动、显示、ISP、断电保存尚未验证"],
        "shutdown_menu": ({**shutdown_report, "applied": True} if shutdown_report is not None else {"applied": False}),
    }
    if crop_report is not None:
        report["crops"] = crop_report
    return result, icons, report
