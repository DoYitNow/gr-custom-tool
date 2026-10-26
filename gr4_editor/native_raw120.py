"""1.20 RAW parameter sidecars in the native singleton's original lifetime.

The native schema has 23 rows, 19 byte fields per row and two half bytes.
Its separate six-byte FilterEffect array stays in the original object. All
indexed record/half consumers move together; native constructor/destructor,
guard and exit registration remain responsible for lifetime.
"""
from hashlib import sha256
import re
import struct

from capstone import Cs, CS_ARCH_ARM, CS_MODE_ARM
from capstone.arm import ARM_OP_REG, ARM_OP_MEM
from .layout120_userdata import SOURCE_RTOS_SHA256, USERDATA120
from .native_raw import mov_half, branch, immediate, add_offset

BASE = 0x53000000
OWNER, GUARD = 0x55089688, 0x55089270
GETTER, CONSTRUCTOR, DESTRUCTOR = 0x53240C98, 0x53375304, 0x533758F0
ALLOCATE, FREE, GUARD_ABORT = 0x539084BC, 0x539084F8, 0x53B4BCF0
NATIVE_ROWS, FIELDS = 23, 19
ENABLE = 0x53DE61C0
REGIONS = ((0x5337A6F4, 0x5337B558), (0x5338022C, 0x53381E60),
           (0x533828D4, 0x53382A14), (0x53383878, 0x53383B68))


def raw_layout120(row_count, custom_state_bytes=0):
    if type(row_count) is not int or not NATIVE_ROWS <= row_count <= 251:
        raise ValueError('1.20 RAW row count must include all native ordinals 0..22')
    if type(custom_state_bytes) is not int or custom_state_bytes < 0:
        raise ValueError('Invalid 1.20 shared owner state tail')
    record = 0x900
    record_bytes = row_count * 4 * FIELDS
    half = record + record_bytes
    half_bytes = row_count * 4 * 2
    state = (half + half_bytes + 3) & ~3
    return {'rows': row_count, 'groups': 4, 'fields_per_record': FIELDS,
            'record_heap_offset': record, 'record_bytes': record_bytes,
            'half_heap_offset': half, 'half_bytes': half_bytes,
            'state_heap_offset': state, 'custom_state_bytes': custom_state_bytes,
            'heap_size': state + custom_state_bytes}


def _routes(source):
    """Inventory actual indexed ADDs by their downstream native member access.

    Exact source identity and the 82-route inventory are fixed contracts, not
    heuristic admission of another version. The final all-row half writer
    uses +0x400 in its index, so its member access is +6 instead of +0x806.
    """
    cs = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    cs.detail = True
    cs.skipdata = True
    result = []
    for start, end in REGIONS:
        instructions = list(cs.disasm(source[start - BASE:end - BASE], start))
        for index, ins in enumerate(instructions):
            args = ins.operands
            if not (ins.mnemonic.startswith('add') and len(args) == 3
                    and all(arg.type == ARM_OP_REG for arg in args)):
                continue
            destination = args[0].reg
            for consumer in instructions[index + 1:index + 14]:
                members = [arg.mem for arg in consumer.operands
                           if arg.type == ARM_OP_MEM and arg.mem.base == destination]
                record = any(0xA1 <= member.disp <= 0xB3 for member in members)
                half = any(0x806 <= member.disp <= 0x807 for member in members)
                if record or half:
                    registers = [int(cs.reg_name(arg.reg).replace('sb', 'r9').replace('sl', 'r10')
                                     .replace('fp', 'r11').replace('ip', 'r12').replace('lr', 'r14')[1:])
                                 for arg in (args[0], args[2])]
                    result.append((ins.address, *registers, args[2].shift.value,
                                   'record' if record else 'half'))
                    break
                _, writes = consumer.regs_access()
                if destination in writes:
                    break
    result.append((0x533829D8, 4, 4, 1, 'half'))
    if len(result) != 82 or len({row[0] for row in result}) != 82:
        raise ValueError('1.20 RAW indexed route inventory differs from its 82 ADD contract')
    return result


def migrate_raw_arrays120(patch, row_count, *, baseline_rtos, custom_state_bytes=0,
                          active_raw_ordinals=None, init_callback=None,
                          custom_rows=None, main_helper=None):
    if sha256(baseline_rtos).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('1.20 RAW migration requires the exact official RTOS')
    layout = raw_layout120(row_count, custom_state_bytes)
    active = set(range(23, row_count)) if active_raw_ordinals is None else set(active_raw_ordinals)
    if any(type(value) is not int or not 23 <= value < row_count for value in active):
        raise ValueError('Custom RAW ordinals overlap native 0..22 or exceed the owner')
    source = bytes(baseline_rtos)
    changes, blocks, routes = [], [], []

    def edit(site, expected, replacement, reason):
        if patch.word(site) != expected:
            raise ValueError(f'1.20 RAW native site already differs: {site:#x}')
        patch.set_word(site, replacement, reason)
        changes.append({'site': hex(site), 'before': hex(expected), 'after': hex(replacement), 'reason': reason})

    def code(name, make):
        at = patch.append(make, 4)
        # Code length is read from the appender's before/after addresses.
        blocks.append({'name': name, 'address': hex(at)})
        return at

    def word(at):
        return struct.unpack_from('<I', source, at - BASE)[0]

    for site, dest, index, shift, storage in _routes(source):
        scratch = 12 if dest != 12 and index != 12 else 11
        bias = layout[storage + '_heap_offset'] - (0xA1 if storage == 'record' else 0x806)
        def make(at, dest=dest, index=index, shift=shift, scratch=scratch, bias=bias, site=site):
            words = [0xE92D0000 | 1 << scratch,
                     0xE1A00000 | scratch << 12 | index | shift << 7,
                     mov_half(dest, OWNER & 65535), mov_half(dest, OWNER >> 16, True),
                     0xE5900000 | dest << 16 | dest << 12]
            words += add_offset(dest, bias)
            words += [0xE0800000 | dest << 16 | dest << 12 | scratch,
                      0xE8BD0000 | 1 << scratch]
            return words + [branch(at + len(words) * 4, site + 4)]
        stub = code(storage + '_' + hex(site), make)
        edit(site, word(site), branch(site, stub, word(site) >> 28), '1.20 RAW indexed ' + storage + ' sidecar')
        routes.append({'site': hex(site), 'stub': hex(stub), 'destination_register': dest,
                       'index_register': index, 'index_shift': shift, 'storage': storage, 'heap_bias': bias})
    # Current-style MLA is an independent native record writer.
    site = 0x533839A8
    if word(site) != 0xE0264596:
        raise ValueError('1.20 current RAW MLA ABI differs')
    def mla(at):
        result = [0xE92D0080, mov_half(7, OWNER & 65535), mov_half(7, OWNER >> 16, True), 0xE5977000]
        result += add_offset(7, layout['record_heap_offset'] - 0xA1)
        result += [0xE0267596, 0xE8BD0080]
        return result + [branch(at + len(result) * 4, site + 4)]
    stub = code('current_raw_mla', mla)
    edit(site, word(site), branch(site, stub), '1.20 current RAW record writeback')
    routes.append({'site': hex(site), 'stub': hex(stub), 'storage': 'record', 'kind': 'mla'})

    # Native flags include all 23*19 bytes, including Palette and Mono rows.
    native_flags = source[ENABLE - BASE:ENABLE - BASE + NATIVE_ROWS * FIELDS]
    flags = bytearray(row_count * FIELDS)
    flags[:len(native_flags)] = native_flags
    for ordinal in active:
        flags[ordinal * FIELDS:(ordinal + 1) * FIELDS] = native_flags[FIELDS:2 * FIELDS]
    enable = patch.append(bytes(flags), 4)
    cs = Cs(CS_ARCH_ARM, CS_MODE_ARM)
    cs.skipdata = True
    counts = {'bounds': 0, 'record_strides': 0, 'half_strides': 0, 'enable_sites': 0}
    seen = set()
    for start, end in REGIONS:
        lows = {}
        for ins in cs.disasm(source[start - BASE:end - BASE], start):
            at, old = ins.address, word(ins.address)
            if ins.mnemonic == 'cmp' and ins.op_str.endswith(', #0x17'):
                edit(at, old, old & ~255 | row_count, '1.20 RAW logical row bound'); counts['bounds'] += 1
            elif ins.mnemonic.startswith('movw') and ins.op_str.endswith(', #0x1b5'):
                replacement = mov_half(old >> 12 & 15, row_count * FIELDS) & 0x0FFFFFFF | old & 0xF0000000
                edit(at, old, replacement, '1.20 RAW nineteen-field group stride'); counts['record_strides'] += 1
            elif ins.mnemonic.startswith('mov') and ins.op_str.endswith(', #0x17'):
                edit(at, old, old & ~255 | row_count, '1.20 RAW half group stride'); counts['half_strides'] += 1
            elif ins.mnemonic == 'movw' and ((old & 0xFFF) | (old >> 4 & 0xF000)) == ENABLE & 65535:
                reg = old >> 12 & 15
                edit(at, old, mov_half(reg, enable & 65535), '1.20 RAW flags low'); lows[reg] = at; counts['enable_sites'] += 1
            elif ins.mnemonic == 'movt' and ((old & 0xFFF) | (old >> 4 & 0xF000)) == ENABLE >> 16:
                reg = old >> 12 & 15
                if reg in lows and at - lows[reg] <= 32:
                    edit(at, old, mov_half(reg, enable >> 16, True), '1.20 RAW flags high'); counts['enable_sites'] += 1
            elif ins.mnemonic == 'ldr' and '[pc, #' in ins.op_str:
                match = re.search(r'\[pc, #(-?0x[0-9a-f]+)\]', ins.op_str)
                if match:
                    literal = at + 8 + int(match.group(1), 16)
                    value = word(literal)
                    if ENABLE <= value < ENABLE + len(native_flags) and literal not in seen:
                        edit(literal, value, enable + value - ENABLE, '1.20 RAW flags literal'); seen.add(literal); counts['enable_sites'] += 1
    if not all(counts.values()):
        raise ValueError('1.20 RAW bounds/strides/flags inventory is incomplete')

    # Clear native arrays first; clear the corresponding appended array too.
    for site, storage in ((0x53375438, 'record'), (0x53375460, 'half')):
        def clear(at, storage=storage):
            result = [0xE92D4008, branch(at + 4, USERDATA120.native_memset, link=True),
                      mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True), 0xE5900000]
            result += add_offset(0, layout[storage + '_heap_offset'])
            result += [0xE3A01000, mov_half(2, layout[storage + '_bytes'])]
            result += [branch(at + len(result) * 4, USERDATA120.native_memset, link=True)]
            if storage == 'half' and init_callback is not None:
                result += [mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True), 0xE5900000]
                result += [branch(at + len(result) * 4, init_callback, link=True)]
            return result + [0xE8BD8008]
        at = code('clear_' + storage, clear)
        edit(site, branch(site, USERDATA120.native_memset, link=True), branch(site, at, link=True), '1.20 clear native and appended ' + storage)

    fast = code('owner_fast', lambda at: [mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True),
                                       0xE5900000, branch(at + 12, 0x53240CC0)])
    def allocate(at):
        words = [mov_half(0, layout['heap_size'] & 65535)]
        if layout['heap_size'] > 65535:
            words.append(mov_half(0, layout['heap_size'] >> 16, True))
        words += [branch(at + len(words) * 4, ALLOCATE, link=True), 0xE3500000, 0,
                  mov_half(12, OWNER & 65535), mov_half(12, OWNER >> 16, True), 0xE58C0000]
        failure_index = len(words) - 4
        words += [branch(at + len(words) * 4, CONSTRUCTOR, link=True), branch(at + (len(words) + 1) * 4, 0x53240CE0)]
        failure = at + len(words) * 4
        words[failure_index] = branch(at + failure_index * 4, failure, condition=0)
        words += [0xE1A00004, branch(failure + 4, GUARD_ABORT, link=True), 0xE3A00000, branch(failure + 12, 0x53240CC0)]
        return words
    cold = code('owner_allocate', allocate)
    def destroy(at):
        words = [0xE92D4010, mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True),
                 0xE5904000, 0xE3540000, 0, 0xE1A00004,
                 branch(at + 28, DESTRUCTOR, link=True), 0xE1A00004, branch(at + 36, FREE, link=True),
                 mov_half(0, OWNER & 65535), mov_half(0, OWNER >> 16, True), 0xE3A03000, 0xE5803000,
                 0xE1A00004, 0xE8BD8010]
        words[5] = branch(at + 20, at + 48, condition=0)
        return words
    destructor = code('owner_destroy', destroy)
    for site, target in ((0x53240CB8, fast), (0x53240CD4, cold), (0x532404B4, destructor)):
        edit(site, mov_half(0, OWNER & 65535), branch(site, target), '1.20 RAW native owner heap lifecycle')

    # Keep cold RAW cursor fallback until the native source service is attached.
    cursor_site = 0x531A91D8
    cursor = code('raw_cursor', lambda at: [0xE5903018, 0xE3530000,
                                          branch(at + 8, 0x5337BA14, condition=0), branch(at + 12, 0x5337B0AC)])
    edit(cursor_site, branch(cursor_site, 0x5337BA14, link=True), branch(cursor_site, cursor, link=True), '1.20 RAW current cursor')
    capacity_word = 0xE3A00000 | immediate(row_count + 3)
    if patch.word(0x53379C6C) != capacity_word:
        edit(0x53379C6C, 0xE3A0001A, capacity_word, '1.20 RAW selector row capacity')
    parameters = _install_parameter_routes(patch, custom_rows, main_helper) if custom_rows else None
    return {**layout, 'backend': 'native120-owner-heap-sidecars', 'owner_pointer_slot': hex(OWNER),
            'native_owner_preserved_bytes': 0x900, 'active_raw_ordinals': sorted(active),
            'routes': routes, 'count': len(routes), 'enable_matrix': hex(enable), 'counts': counts,
            'initialize_callback': hex(init_callback) if init_callback is not None else None,
            'patches': changes, 'code_blocks': blocks, 'parameter_routes': parameters,
            'owner_lifecycle': {'getter': hex(GETTER), 'constructor': hex(CONSTRUCTOR), 'destructor': hex(DESTRUCTOR),
                                'fast': hex(fast), 'allocator': hex(cold), 'exit_wrapper': hex(destructor),
                                'guard': hex(GUARD), 'guard_abort': hex(GUARD_ABORT)},
            'next_static_singleton_untouched': True, 'hardware_verified': False}


def _install_parameter_routes(patch, rows, main_helper):
    if main_helper is None:
        raise ValueError('1.20 RAW custom parameter routes need the registry main helper')
    from .crop_geometry import asm
    if patch.word(0x5337EF68) != 0xE1A0C00D:
        raise ValueError('1.20 RAW initializer entry already changed')
    original = patch.append(lambda at: [0xE1A0C00D, branch(at + 4, 0x5337EF6C)], 4)
    code = 'cmp r3,#22;bls old;push {r4-r8,lr};mov r4,r0;mov r5,r2;mov r6,r3;mov r7,r1;mov r3,#1;'
    code += f'bl #{original};'
    for row in rows:
        identity = row['identity']
        code += f"cmp r6,#{identity['raw_ordinal']};beq style{identity['style_id']};"
    code += 'mov r0,r4;pop {r4-r8,pc};'
    for row in rows:
        identity = row['identity']
        code += f"style{identity['style_id']}:mov r0,#{identity['style_id']};bl #{main_helper};add ip,r0,#36;add ip,ip,r5;"
        for field, offset in enumerate((0, 1, 2, 3, 4, 5, 6, 14, 15)):
            code += f'ldrb r1,[ip,#{field * 4}];eor r1,r1,#4;strb r1,[r4,#{offset}];'
        code += 'mov r0,r4;pop {r4-r8,pc};'
    code += f'old:b #{original};'
    at = patch.append(lambda start: asm(code, start), 4)
    patch.set_word(0x5337EF68, branch(0x5337EF68, at), '1.20 RAW nineteen-field native defaults plus custom nine scalars')
    # The native bulk conversion retains all new descriptor fields, then the
    # sidecar replaces only the same nine fields proven in the older product.
    bulk = 'push {r0-r5,r8-r10,lr};ldrb r3,[r4,#0xB76];cmp r3,#0;bne done;ldrb r2,[r4,#0xC96];cmp r2,#3;bhi done;add ip,r4,r2;ldrb r3,[ip,#0xC97];'
    for row in rows:
        identity = row['identity']
        bulk += f"cmp r3,#{identity['style_id']};beq style{identity['style_id']};"
    bulk += 'b done;'
    for row in rows:
        identity = row['identity']
        bulk += f"style{identity['style_id']}:mov r5,r2;mov r0,#{identity['style_id']};bl #{main_helper};add ip,r0,#36;add ip,ip,r5;"
        for field, offset in enumerate((0xB9, 0xBA, 0xBB, 0xBC, 0xBD, 0xBE, 0xC0, 0xCF, 0xD0)):
            bulk += f'ldrb r3,[ip,#{field * 4}];eor r3,r3,#4;strb r3,[r6,#{offset}];'
        bulk += 'b done;'
    bulk += 'done:pop {r0-r5,r8-r10,lr};mov r1,r6;b #0x532A3AF4;'
    target = patch.append(lambda start: asm(bulk, start), 4)
    if patch.word(0x532A3AF0) != 0xE1A01006:
        raise ValueError('1.20 UserData bulk conversion ABI differs')
    patch.set_word(0x532A3AF0, branch(0x532A3AF0, target), '1.20 custom scalar bulk conversion')
    return {'raw_initializer': hex(at), 'raw_original_body': hex(original), 'bulk': hex(target),
            'record_fields': 19, 'overridden_fields': [0, 1, 2, 3, 4, 5, 6, 14, 15],
            'native_standard_defaults_for_other_fields': True}
