"""Rebuild RAW sidecars in the original owner's heap from a known native base.

The original constructor/destructor and NEWOPERATOR remain in control. All
80 known routes load that same owner pointer; no extra lifetime is introduced.
Logical rows and active custom ordinals follow the compiled registry. Native
rows retain their flags; deleted custom rows are disabled. State may occupy the
heap tail and initialize through the compiled registry callback.
"""
import hashlib
import re
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
from .raw_layout import EXPLICIT_ROUTES, REGIONS, READERS

BASE = 0x53000000
OWNER = 0x550885B4
ALLOCATE_SIZE = 0x543EBF70
MEMSET = 0x53156768
RAW_CURSOR_SITE = 0x531A7D6C
LEGACY_RAW_CURSOR_WRAPPER = 0x543D4700
LEGACY_CURRENT_GETTER = 0x5336D5D0
RAW_CURRENT_GETTER = 0x5336CC8C
RAW_SELECTOR_CAPACITY_GETTER = 0x5336B89C


def word(image, address):
    return struct.unpack_from("<I", image, address - BASE)[0]


def mov_half(register, value, high=False):
    return (0xE3400000 if high else 0xE3000000) | ((value & 0xF000) << 4) | (register << 12) | (value & 0xFFF)


def branch(site, target, condition=14, link=False):
    delta = target - site - 8
    if delta % 4 or not -(1 << 25) <= delta < (1 << 25):
        raise ValueError("ARM branch target out of range")
    return (condition << 28) | (0x0B000000 if link else 0x0A000000) | ((delta // 4) & 0xFFFFFF)


def branch_target(site, instruction):
    if instruction & 0x0E000000 != 0x0A000000:
        raise ValueError(f"expected native ARM branch at {site:#x}")
    relative = instruction & 0xFFFFFF
    if relative & 0x800000:
        relative -= 0x1000000
    return site + 8 + relative * 4


def immediate(value):
    for rotation in range(16):
        shift = rotation * 2
        candidate = ((value << shift) | (value >> (32 - shift if shift else 32))) & 0xFFFFFFFF
        if candidate <= 255:
            return (rotation << 8) | candidate
    raise ValueError(f"unencodable ARM immediate {value:#x}")


def add_offset(register, offset):
    if not 0 <= offset <= 0xFFFF:
        raise ValueError("heap offset outside the proven expansion")
    return [0xE2800000 | (register << 16) | (register << 12) | immediate(part)
            for part in (offset & 0xFF00, offset & 0xFF) if part]


def _routes(source):
    cs = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    routes = list(EXPLICIT_ROUTES)
    for field, entry in READERS:
        instructions = list(cs.disasm(source[entry - BASE:entry - BASE + 0x124], entry))
        load = next(i for i in instructions if i.mnemonic == "ldrb" and i.op_str == f"r1, [r3, #0x{0xA1 + field:x}]")
        store = next(i for i in instructions if i.mnemonic == "strb" and i.op_str == f"r1, [ip, #0x{0xA1 + field:x}]")
        routes.extend([(load.address - 4, 3, 3, 0, "record", f"field {field} consume"),
                       (store.address - 4, 12, 2, 0, "record", f"field {field} update")])
    if len(routes) != 79 or len({row[0] for row in routes}) != 79:
        raise ValueError("RAW route inventory does not match the proven image")
    return routes


def install_raw_current_cursor(image, append_callable, *, official_base=False):
    """Move the RAW selector cursor wrapper out of the icon catalog.

    The .24 wrapper occupies catalog entries 784–787, which registry resource
    rebuilding clears. Install fresh executable bytes after that cleanup;
    keep the original cold-model fallback until the RAW service is attached.
    """
    original = word(image, RAW_CURSOR_SITE)
    previous_target = LEGACY_CURRENT_GETTER if official_base else LEGACY_RAW_CURSOR_WRAPPER
    if original != branch(RAW_CURSOR_SITE, previous_target, link=True):
        raise ValueError("RAW cursor call does not match the supported native layout")
    if word(image, RAW_CURSOR_SITE - 4) != branch(RAW_CURSOR_SITE - 4, 0x5323F078, link=True):
        raise ValueError("RAW cursor model getter differs from the reviewed layout")
    address = BASE + ((len(image) + 3) & ~3)
    words = [0xE5903018, 0xE3530000,
             branch(address + 8, LEGACY_CURRENT_GETTER, condition=0),
             branch(address + 12, RAW_CURRENT_GETTER)]
    blob = struct.pack("<4I", *words)
    installed = append_callable(blob, alignment=4)
    if installed != address:
        raise ValueError("RAW cursor appender does not use the requested word alignment")
    replacement = branch(RAW_CURSOR_SITE, address, link=True)
    struct.pack_into("<I", image, RAW_CURSOR_SITE - BASE, replacement)
    return {"site": hex(RAW_CURSOR_SITE), "old_target": hex(previous_target),
            "wrapper": hex(address), "cold_fallback": hex(LEGACY_CURRENT_GETTER),
            "attached_getter": hex(RAW_CURRENT_GETTER), "service_pointer_offset": "0x18",
            "outside_icon_catalog": True,
            "patches": [{"site": hex(RAW_CURSOR_SITE), "before": hex(original),
                         "after": hex(replacement), "reason": "RAW cursor wrapper outside icon catalog"}],
            "code_blocks": [{"name": "raw_current_cursor", "address": hex(address),
                             "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()}],
            "hardware_verified": False}


def install_raw_selector_capacity(image, registered_filter_count):
    """Size the shared ImageControl row pool before its lazy construction.

    Its capacity getter is independent of the dynamically growing model vectors.
    Reserve the full registered list plus Custom 1–3 and the Original option;
    querying the current model here would underallocate during cold startup.
    The constructor stores this count in Screen+0x180 as one byte.
    """
    if type(registered_filter_count) is not int or not 20 <= registered_filter_count <= 251:
        raise ValueError("RAW 列表控件容量超出已识别的字节计数范围")
    capacity = registered_filter_count + 4
    site = RAW_SELECTOR_CAPACITY_GETTER
    original = word(image, site)
    if original != 0xE3A00018 or word(image, site + 4) != 0xE12FFF1E:
        raise ValueError("RAW list capacity getter differs from the reviewed .27 layout")
    replacement = 0xE3A00000 | immediate(capacity)
    struct.pack_into("<I", image, site - BASE, replacement)
    return {"getter": hex(site), "registered_filters": registered_filter_count,
            "row_capacity": capacity, "extra_rows": {"custom_groups": 3, "original": 1},
            "screen_capacity_offset": "0x180", "cold_model_independent": True,
            "patches": [{"site": hex(site), "before": hex(original), "after": hex(replacement),
                         "reason": "ImageControl row pool follows compiled registry plus group/Original rows"}],
            "hardware_verified": False}


def migrate_raw_arrays(image, row_count, append_callable, *, custom_state_bytes=0,
                       active_raw_ordinals=None, init_callback=None, official_base=False):
    if type(row_count) is not int or not 21 <= row_count <= 255:
        raise ValueError("RAW 行数必须在已识别的 21–255 字节编号范围内")
    if type(custom_state_bytes) is not int or custom_state_bytes < 0:
        raise ValueError("参数堆存储大小无效")
    active = set(range(21, row_count)) if active_raw_ordinals is None else set(active_raw_ordinals)
    if any(type(value) is not int or not 21 <= value < row_count for value in active):
        raise ValueError("活动 RAW 编号超出当前注册表")
    source = bytes(image)
    if not official_base and word(source, ALLOCATE_SIZE) != mov_half(0, 0x800):
        raise ValueError("RAW owner allocator is not the original 0x800-byte layout")
    record_offset = 0x800
    record_bytes = row_count * 4 * 18
    half_offset = record_offset + record_bytes
    half_bytes = row_count * 4 * 2
    state_heap_offset = half_offset + half_bytes
    heap_size = state_heap_offset + custom_state_bytes
    if heap_size > 0xFFFFFFFF:
        raise ValueError("参数堆存储超过 32 位范围")
    patches, routes, blocks = [], [], []

    def edit(site, expected, replacement, reason):
        actual = word(image, site)
        if actual != expected:
            raise ValueError(f"RAW layout differs at {site:#x}: {actual:#x} != {expected:#x}")
        struct.pack_into("<I", image, site - BASE, replacement)
        patches.append({"site": hex(site), "before": hex(actual), "after": hex(replacement), "reason": reason})

    def code(name, make):
        predicted = BASE + ((len(image) + 3) & ~3)
        words = make(predicted)
        blob = struct.pack("<" + str(len(words)) + "I", *words)
        address = append_callable(blob, alignment=4)
        if address != predicted:
            raise ValueError("RAW appender does not use the requested word alignment")
        blocks.append({"name": name, "address": hex(address), "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
        return address

    # Official inputs still address their original object. Legacy outputs
    # have the historical static sidecar stubs; rebuild both into the same
    # dynamic owner heap without copying any prebuilt patch payload.
    for site, dest, index, shift, storage, reason in _routes(source):
        old_branch = word(source, site)
        scratch = 12 if dest != 12 else 11
        old_stub = site if official_base else branch_target(site, old_branch)
        expected = [0xE92D0000 | (1 << scratch), 0xE1A00000 | (scratch << 12) | index | (shift << 7),
                    mov_half(dest, OWNER & 0xFFFF), mov_half(dest, OWNER >> 16, True),
                    0xE0800000 | (dest << 16) | (dest << 12) | scratch,
                    0xE8BD0000 | (1 << scratch), branch(old_stub + 24, site + 4)]
        if not official_base and any(word(source, old_stub + i * 4) != value for i, value in enumerate(expected)):
            raise ValueError(f"unrecognised RAW sidecar stub at {site:#x}")
        offset = (record_offset - 0xA1) if storage == "record" else (half_offset - 0x71A)

        def make(address):
            result = expected[:4] + [0xE5900000 | (dest << 16) | (dest << 12)] + add_offset(dest, offset) + expected[4:6]
            return result + [branch(address + len(result) * 4, site + 4)]

        stub = code(f"{storage}_{site:x}", make)
        edit(site, old_branch, branch(site, stub, old_branch >> 28), reason)
        routes.append({"site": hex(site), "stub": hex(stub), "destination_register": dest,
                       "index_register": index, "index_shift": shift, "storage": storage,
                       "heap_bias": offset, "condition": old_branch >> 28, "scratch_register": scratch})

    site = 0x53374ED0
    old_branch = word(source, site)
    old_stub = site if official_base else branch_target(site, old_branch)
    expected = [0xE92D0010, mov_half(4, OWNER & 0xFFFF), mov_half(4, OWNER >> 16, True),
                0xE0274597, 0xE8BD0010, branch(old_stub + 20, site + 4)]
    if not official_base and any(word(source, old_stub + i * 4) != value for i, value in enumerate(expected)):
        raise ValueError("unrecognised current-style RAW MLA stub")

    def mla(address):
        result = expected[:3] + [0xE5944000] + add_offset(4, record_offset - 0xA1) + expected[3:5]
        return result + [branch(address + len(result) * 4, site + 4)]

    stub = code("current_style_mla", mla)
    edit(site, old_branch, branch(site, stub), "current style dynamic record writeback")
    routes.append({"site": hex(site), "stub": hex(stub), "destination_register": 7,
                   "storage": "record", "heap_bias": record_offset - 0xA1, "kind": "mla", "scratch_register": 4, "condition": 14})
    if official_base:
        owner_lifecycle = _install_owner_lifecycle(image, heap_size, code, edit)
    elif heap_size <= 0xFFFF:
        allocator = mov_half(0, heap_size)
    else:
        allocator_stub = code("owner_allocation_size", lambda a: [mov_half(0, heap_size & 0xFFFF),
                              mov_half(0, heap_size >> 16, True), branch(a + 8, ALLOCATE_SIZE + 4)])
        allocator = branch(ALLOCATE_SIZE, allocator_stub)
    if not official_base:
        edit(ALLOCATE_SIZE, mov_half(0, 0x800), allocator, "rebuild original NEWOPERATOR owner allocation")

    for site, old_helper, offset, count, storage in [(0x53367088, 0x543EBFB0, record_offset, record_bytes, "record"),
                                                   (0x533670B0, 0x543EBFD0, half_offset, half_bytes, "half")]:
        def clear(address, storage=storage, count=count, offset=offset):
            result = [0xE92D4008, branch(address + 4, MEMSET, link=True), mov_half(0, OWNER & 0xFFFF),
                      mov_half(0, OWNER >> 16, True), 0xE5900000] + add_offset(0, offset)
            result += [0xE3A01000, mov_half(2, count)]
            result += [branch(address + len(result) * 4, MEMSET, link=True)]
            if storage == "half" and init_callback is not None:
                result += [0xE92D500F, mov_half(0, OWNER & 0xFFFF), mov_half(0, OWNER >> 16, True), 0xE5900000]
                result += [branch(address + len(result) * 4, init_callback, link=True), 0xE8BD500F]
            result += [0xE8BD8008]
            return result
        target = code("clear_" + storage, clear)
        expected_clear = branch(site, MEMSET if official_base else old_helper, link=True)
        edit(site, expected_clear, branch(site, target, link=True), "clear original and appended " + storage)

    # Extend the current enable flags, keeping the established Standard row.
    old_enable = word(source, 0x5336C84C) - 8
    old_rows = 21 if official_base else 23
    flags = source[old_enable - BASE:old_enable - BASE + old_rows * 18]
    if len(flags) != old_rows * 18 or flags[:378] != source[0x53DBB8F0 - BASE:0x53DBB8F0 - BASE + 378]:
        raise ValueError("RAW enable matrix does not preserve the original 21 rows")
    registry_flags = bytearray(max(23, row_count) * 18)
    registry_flags[:378] = flags[:378]
    for ordinal in active:
        registry_flags[ordinal * 18:(ordinal + 1) * 18] = flags[18:36]
    new_enable = append_callable(bytes(registry_flags), alignment=4)
    cs = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    seen, bounds, strides, halves, enables = set(), 0, 0, 0, 0
    for start, size in REGIONS:
        lows = {}
        for ins in cs.disasm(source[start - BASE:start - BASE + size], start):
            at, old = ins.address, word(source, ins.address)
            if ins.mnemonic == "cmp" and ins.op_str.endswith(f", #0x{old_rows:x}"):
                edit(at, old, (old & ~255) | row_count, "RAW logical row bound"); bounds += 1
            elif ins.mnemonic.startswith("movw") and ins.op_str.endswith(f", #0x{old_rows * 18:x}"):
                new = (mov_half((old >> 12) & 15, row_count * 18) & 0x0FFFFFFF) | (old & 0xF0000000)
                edit(at, old, new, "RAW record group stride"); strides += 1
            elif ins.mnemonic.startswith("mov") and ins.op_str.endswith(f", #0x{old_rows:x}"):
                edit(at, old, (old & ~255) | row_count, "RAW half group stride"); halves += 1
            elif ins.mnemonic == "movw" and (old & 0xFFF) | ((old >> 4) & 0xF000) == (old_enable & 0xFFFF):
                register = (old >> 12) & 15
                edit(at, old, mov_half(register, new_enable & 0xFFFF), "RAW enable pointer low"); lows[register] = at; enables += 1
            elif ins.mnemonic == "movt" and (old & 0xFFF) | ((old >> 4) & 0xF000) == (old_enable >> 16):
                register = (old >> 12) & 15
                if register in lows and at - lows[register] <= 32:
                    edit(at, old, mov_half(register, new_enable >> 16, True), "RAW enable pointer high"); enables += 1
            elif ins.mnemonic == "ldr" and "[pc, #" in ins.op_str:
                match = re.search(r"\[pc, #(-?0x[0-9a-f]+)\]", ins.op_str)
                if match:
                    literal = at + 8 + int(match.group(1), 16)
                    value = word(source, literal)
                    if old_enable <= value < old_enable + old_rows * 18 and literal not in seen:
                        edit(literal, value, new_enable + value - old_enable, "RAW enable literal"); seen.add(literal); enables += 1
    if not all((bounds, strides, halves, enables)):
        raise ValueError("RAW count/stride/enable inventory is incomplete")
    cursor = install_raw_current_cursor(image, append_callable, official_base=official_base)
    patches.extend(cursor["patches"])
    blocks.extend(cursor["code_blocks"])
    return {"backend": "original-owner-heap-sidecars", "rows": row_count, "groups": 4,
            "heap_size": heap_size, "record_heap_offset": record_offset, "half_heap_offset": half_offset,
            "state_heap_offset": state_heap_offset, "custom_state_bytes": custom_state_bytes,
            "active_raw_ordinals": sorted(active), "initialize_callback": hex(init_callback) if init_callback is not None else None,
            "record_bytes": record_bytes, "half_bytes": half_bytes, "routes": routes, "count": len(routes),
            "enable_matrix": hex(new_enable), "patches": patches, "code_blocks": blocks,
            "current_cursor": cursor,
            "counts": {"bounds": bounds, "record_strides": strides, "half_strides": halves, "enable_sites": enables},
            "owner_lifecycle": owner_lifecycle if official_base else "preserved",
            "next_static_singleton_untouched": True, "hardware_verified": False}


def _install_owner_lifecycle(image, heap_size, code, edit):
    """Generate the original singleton's heap allocation and exit wrappers."""
    fast = code("initialized_owner", lambda a: [
        mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True),
        0xE5900000, branch(a + 12, 0x5323F0A0)])

    def cold(a):
        size = [mov_half(0, heap_size & 65535)]
        if heap_size > 65535:
            size.append(mov_half(0, heap_size >> 16, True))
        words = size + [branch(a + len(size) * 4, 0x538E09B0, link=True), 0xE3500000, 0,
                        mov_half(12, OWNER & 65535), mov_half(12, OWNER >> 16, True),
                        0xE58C0000]
        words += [branch(a + len(words) * 4, 0x53366F58, link=True),
                  branch(a + (len(words) + 1) * 4, 0x5323F0C0)]
        failure = a + len(words) * 4
        words[len(size) + 2] = branch(a + (len(size) + 2) * 4, failure, condition=0)
        words += [0xE1A00004, branch(failure + 4, 0x53B22DA0, link=True),
                  0xE3A00000, branch(failure + 12, 0x5323F0A0)]
        return words

    allocate = code("allocate_native_owner_and_sidecars", cold)
    destroy = code("destroy_native_owner", lambda a: [
        0xE92D4010, mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True),
        0xE5904000, 0xE3540000, branch(a + 20, a + 48, condition=0),
        0xE1A00004, branch(a + 28, 0x53367530, link=True),
        0xE1A00004, branch(a + 36, 0x538E09EC, link=True),
        mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True),
        0xE3A03000, 0xE5803000, 0xE1A00004, 0xE8BD8010])
    for site, target, reason in ((0x5323F098, fast, "load original owner pointer"),
                                 (0x5323F0B4, allocate, "allocate original owner with sidecars"),
                                 (0x5323E888, destroy, "destroy and free original owner")):
        edit(site, mov_half(0, OWNER & 65535), branch(site, target), reason)
    return {"pointer_slot": hex(OWNER), "heap_size": heap_size,
            "getter": hex(fast), "allocator": hex(allocate), "destructor": hex(destroy),
            "original_constructor": "0x53366f58", "original_destructor": "0x53367530"}
