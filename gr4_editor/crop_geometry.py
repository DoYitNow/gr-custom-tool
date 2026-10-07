# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Dynamic crop output, preview, AF and 3A consumers on the shared append area.

Existing public0..5 dimensions, preview and AF paths remain reachable.
ImageDevelop's incomplete custom4/5 consumers join the dynamic route.
Active and retired custom identities share immutable geometry, so a menu edit cannot
reinterpret older photographs. Verification executes native software; pixel
DMA, ISP, AF/AE hardware and encoder output still require camera validation.
"""
import struct
import math
from fractions import Fraction
from .toolchain import assemble_arm
from .crop_native import CropNative, BASE
from .crops import _aligned_crop as _region, parse_ratio

def asm(s, a):
    return assemble_arm(s, a)

def addr(r, value):
    return f'movw {r},#{value & 65535};movt {r},#{value >> 16};'

class Program:

    def __init__(self, patch):
        self.patch = patch
        self.source = bytes(patch.image)
        self.cursor = BASE + len(patch.image) + 3 & ~3
        self.blobs = []
        self.patches = []
        self.rows = {}

    def blob(self, value):
        at = self.patch.append(value, 4)
        self.blobs.append((at, bytes(value)))
        self.cursor = at + len(value) + 3 & ~3
        return at

    def emit(self, code):
        return self.blob(asm(code, self.cursor))

    def hook(self, entry, code):
        at = self.emit(code)
        after = asm(f'b #{at}', entry)
        self.patches.append((entry, after))
        self.patch.set_word(entry, struct.unpack('<I', after)[0], 'dynamic crop geometry consumer')
        return at

    def target(self, at):
        w = struct.unpack_from('<I', self.source, at - BASE)[0]
        if getattr(self.patch, 'official_base', False):
            from .demo_build import _branch
            # Legacy geometry hooks already branch to a replay of the native
            # first instruction. Fresh official entries need that replay now.
            # VFP PC-relative loads carry their input-supplied literal beside
            # the trampoline because the original literal is far away.
            if w & 0x0F3F0F00 in (0x0D1F0A00, 0x0D1F0B00):
                literal = at + 8 + (1 if w & 0x00800000 else -1) * (w & 255) * 4
                size = 8 if w & 0x100 else 4
                data = self.source[literal - BASE:literal - BASE + size]
                replay = (w & ~0x008000FF) | 0x00800000
                origin = (BASE + len(self.patch.image) + 3) & ~3
                return self.blob(struct.pack('<2I', replay, _branch(origin + 4, at + 4)) + data)
            origin = (BASE + len(self.patch.image) + 3) & ~3
            if w & 0x0E000000 == 0x0A000000:
                d = w & 0xFFFFFF
                if d & 0x800000:
                    d -= 1 << 24
                w = _branch(origin, at + 8 + d * 4, condition=w >> 28, link=bool(w & 0x01000000))
            return self.blob(struct.pack('<2I', w, _branch(origin + 4, at + 4)))
        if w & 0xf000000 == 0xa000000:
            d = w & 0xffffff
            if d & 0x800000:
                d -= 1 << 24
            return at + 8 + d * 4
        return at + 4

# Row layout: fx/fy fractions, small-image origins, AF rectangle/sizes,
# tracking axes and native hardware alias; main dimension rows follow.
def record(identity, ratio, retired=False):
    n, d = ratio
    r = Fraction(n, d)
    screen = _region(720, 480, r)
    r = Fraction(screen['width'], screen['height'])
    thumb = _region(160, 120, r)
    fx = min(Fraction(1), r / Fraction(3, 2))
    fy = min(Fraction(1), Fraction(3, 2) / r)
    rect = [max(80, screen['left']), max(60, screen['top']), min(640, screen['left'] + screen['width']), min(420, screen['top'] + screen['height'])]
    w, h = (rect[2] - rect[0], rect[3] - rect[1])
    # Scale native wide/square tracking windows into the requested aperture.
    # These values are software geometry; camera calibration remains pending.
    track = [778, max(1, round(634 * Fraction(180, 77) / r))] if r > Fraction(3, 2) else [max(1, round(667 * min(r, Fraction(1)))), 750]
    dims = []
    for bw, bh in ((6000, 4000), (4800, 3200), (3360, 2240), (1920, 1280), (6192, 4128), (4944, 3296), (3504, 2336)):
        region = _region(bw, bh, r)
        dims.append([bw, bh, region['width'], region['height']])
    return dict(id=identity, requested_ratio=list(ratio), actual_screen_ratio=[r.numerator, r.denominator], retired=retired, screen=screen, thumb=thumb, fx=[fx.numerator, fx.denominator], fy=[fy.numerator, fy.denominator], af=rect, af_size=[w, h], af_small=[w * 3 // 5, h * 3 // 5], tracking=track, mode_alias=5 if r > Fraction(3, 2) else 3, dims=dims)

# Geometry comes from the imported/native plan. Archived identities retain
# their original pixel dimensions and virtual plane.
def planned_record(recipe, source):
    identity = recipe['identity']['public_id']
    ratio = parse_ratio(recipe['requested_ratio'])
    row = record(identity, (ratio.numerator, ratio.denominator), not recipe.get('active', True))
    geometry = recipe.get('geometry', {})
    if geometry.get('photo_sizes'):
        native = CropNative(source)
        mapped = {}
        for size in geometry['photo_sizes']:
            baseline = native.dimensions(size['model'], 0, size['magnification'], size['selector'])
            if baseline['diagnostics']:
                raise ValueError('base dimensions diagnosis')
            key = (baseline['width'], baseline['height'])
            value = (size['width'], size['height'])
            if key in mapped and mapped[key] != value:
                raise ValueError('main dimension table not a consistent native-width map')
            mapped[key] = value
        row['dims'] = [[*key, *value] for key, value in mapped.items()]
        row['screen'] = geometry['screen']
        row['thumb'] = geometry.get('thumbnail', geometry.get('thumb'))
        screen = row['screen']
        aperture = [max(80, screen['left']), max(60, screen['top']),
                    min(640, screen['left'] + screen['width']),
                    min(420, screen['top'] + screen['height'])]
        width, height = aperture[2] - aperture[0], aperture[3] - aperture[1]
        row.update(af=aperture, af_size=[width, height],
                   af_small=[width * 3 // 5, height * 3 // 5])
    if geometry.get('preview'):
        # The virtual45x30 plane is independent of aligned JPEG/display
        # canvases (factory16:9 and historical6 demonstrate the difference).
        preview = geometry['preview']
        fx = Fraction(preview['width'], 45 << 16)
        fy = Fraction(preview['height'], 30 << 16)
        row['fx'] = [fx.numerator, fx.denominator]
        row['fy'] = [fy.numerator, fy.denominator]
    if identity == 6 and recipe.get('requested_ratio') == '65:24' and geometry.get('photo_sizes'):
        row.update(af=[80, 140, 640, 340], af_size=[560, 200], af_small=[336, 120], tracking=[778, 550], mode_alias=5)
        row['fx'] = [1, 1]
        row['fy'] = [Fraction(36, 65).numerator, Fraction(36, 65).denominator]
    return row

# Each wrapper preserves the native entry ABI and falls back to the
# imported0..5 target. Custom records are addressable after retirement.
def _build(records, patch):
    p = Program(patch)
    values = (planned_record(row, p.source) for row in records if row['identity']['public_id'] >= 6)
    for row in values:
        identity = row['id']
        values = [*row['fx'], *row['fy'], row['screen']['left'], row['screen']['top'], row['thumb']['left'], row['thumb']['top'], *row['af'], *row['af_size'], *row['af_small'], *row['tracking'], row['mode_alias']]
        row['address'] = p.blob(struct.pack('<' + str(len(values)) + 'I', *values))
        row['dim_address'] = p.blob(b''.join((struct.pack('<4H', *r) for r in row['dims'])))
        p.rows[identity] = row
    lookup = ''
    for identity, row in p.rows.items():
        lookup += f'cmp r0,#{identity};' + addr('ip', row['address']) + 'moveq r0,ip;bxeq lr;'
    lookup += 'mov r0,#0;bx lr;'
    get = p.emit(lookup)
    scale = p.emit('mov ip,r1;mov r3,#0;umull r0,r1,r0,ip;b #0x53153EEC;')
    preview_scale = p.emit('mov ip,r1;mov r3,#0;umull r0,r1,r0,ip;mov ip,r2,lsr#1;adds r0,r0,ip;adc r1,r1,#0;b #0x53153EEC;')
    out = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r4,r0;bne #0x5388C7F4;b #{p.target(0x5388c7dc)};'
    p.hook(0x5388c7dc, out)
    out = f'push {{r0-r3,ip,lr}};mov r0,r3;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r0,r3;bne #0x5388C8BC;b #{p.target(0x5388c8a4)};'
    p.hook(0x5388c8a4, out)
    out = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};beq old;ldrb r1,[r4,#0x8D];cmp r0,r1;moveq r0,#0;movne r0,#1;b #0x5363FAC8;old:b #{p.target(0x5363faa8)};'
    p.hook(0x5363faa8, out)
    # Both callers select the cropped window only for their original 2..5
    # enum range. A computed custom window is invisible if selector0 copies
    # the untouched full canvas at object+4 instead of object+36.
    out = f'push {{r0-r3,ip,lr}};mov r0,r4;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r0,#1;bne #0x5362E440;sub r4,r4,#2;b #0x5362E434;'
    p.hook(0x5362e430, out)
    out = f'push {{r0-r3,ip,lr}};mov r0,r1;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};beq old;orr r1,r3,#1;ldr r0,[r0,#0x3A0];b #0x53887D48;old:sub r1,r1,#2;b #0x536342F4;'
    p.hook(0x536342f0, out)
    # Main converters call the native base validator before replacing sizes.
    for entry in (0x538cf488, 0x538cfbf8):
        old = p.target(entry)
        out = 'cmp r2,#5;bls old;push {r4-r10,lr};mov r4,r2;mov r5,r3;ldr r6,[sp,#32];'
        for identity, row in p.rows.items():
            out += f'cmp r4,#{identity};' + addr('r7', row['dim_address']) + f'beq foundrow{identity};'
        out += 'pop {r4-r10,lr};b old;'
        for identity, row in p.rows.items():
            out += f"foundrow{identity}: mov r9,#{len(row['dims'])};b calculate;"
        out += f'calculate: mov r2,#0;sub sp,sp,#8;str r6,[sp];bl #{old};add sp,sp,#8;mov r8,r0;ldrh r0,[r5];ldrh r1,[r6];'
        out += 'lookupdim: ldrh r2,[r7];cmp r0,r2;ldrh r2,[r7,#2];cmpeq r1,r2;beq dimensions;add r7,r7,#8;subs r9,r9,#1;bne lookupdim;b done;'
        out += 'dimensions: ldrh r2,[r7,#4];strh r2,[r5];ldrh r2,[r7,#6];strh r2,[r6];done:mov r0,r8;pop {r4-r10,pc};'
        out += f'old:b #{old};'
        p.hook(entry, out)
    for entry, kind in ((0x538cf944, 'screen'), (0x538d00b4, 'screen'), (0x538cf9fc, 'thumb'), (0x538d016c, 'thumb')):
        out = ''
        for identity, row in p.rows.items():
            region = row[kind]
            out += f'cmp r1,#{identity};beq ratio{identity};'
        out += f'b #{p.target(entry)};'
        for identity, row in p.rows.items():
            region = row[kind]
            out += f"ratio{identity}:movw r0,#{region['left']};movw r1,#{region['top']};strh r0,[r2];strh r1,[r3];bx lr;"
        p.hook(entry, out)
    out = 'cmp r2,#5;bls old;cmp r1,#0;bne old;push {r4-r10,lr};mov r5,r0;mov r0,r2;'
    out += f'bl #{get};cmp r0,#0;beq missing;mov r4,r0;mov r0,r5;mov r2,#0;mov ip,sp;bl #0x538876BC;mov r10,r0;'
    for rect in (36, 52):
        for dimension, origin, fn, fd in ((rect, rect + 8, 0, 4), (rect + 4, rect + 12, 8, 12)):
            out += f'ldr r6,[r5,#{dimension}];mov r0,r6;ldr r1,[r4,#{fn}];ldr r2,[r4,#{fd}];bl #{preview_scale};str r0,[r5,#{dimension}];sub r6,r6,r0;add r6,r6,#1;ldr r7,[r5,#{origin}];add r7,r7,r6,lsr#1;str r7,[r5,#{origin}];'
    out += 'mov r0,r10;pop {r4-r10,pc};missing:mov r0,r5;pop {r4-r10,lr};old:mov ip,sp;b #0x538876BC;'
    p.hook(0x538876b8, out)
    for entry, kind in ((0x5339e758, 'rectangle'), (0x5339e990, 'size'), (0x5339e9b8, 'small')):
        out = ''
        for identity, row in p.rows.items():
            out += f'cmp r0,#{identity};beq ratio{identity};'
        out += f'b #{p.target(entry)};'
        for identity, row in p.rows.items():
            if kind == 'rectangle':
                l, t, r, b = row['af']
                out += f'ratio{identity}:movw r0,#{b};movw r1,#{l};movw r2,#{t};movw r3,#{r};b #0x5339E73C;'
            else:
                w, h = row['af_size' if kind == 'size' else 'af_small']
                out += f'ratio{identity}:movw r1,#{w};movw r2,#{h};mov r0,r5;bl #0x538EA168;b #0x5339E924;'
        p.hook(entry, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r2,#{identity};beq ratio{identity};'
    out += 'cmp r2,#5;beq #0x5364DF8C;b #0x5364DEE8;'
    for identity, row in p.rows.items():
        out += f"ratio{identity}:movw r4,#{row['tracking'][0]};movw r7,#{row['tracking'][1]};b #0x5364DF94;"
    p.hook(0x5364dee4, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r2,#{identity};beq ratio{identity};'
    out += 'cmp r2,#5;beq #0x5364E4D4;b #0x5364E488;'
    for identity, row in p.rows.items():
        tx, ty = row['tracking']
        lockx, locky = [778, 1000] if row['mode_alias'] == 5 else [1000, 750]
        out += f'ratio{identity}:cmp r7,#0;movw r5,#{lockx};movw r4,#{locky};bne #0x5364E440;movw r5,#{tx};movw r4,#{ty};b #0x5364E3E0;'
    p.hook(0x5364e484, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r1,#{identity};beq ratio{identity};'
    out += 'b #0x5364E7D4;'
    for identity, row in p.rows.items():
        out += f"ratio{identity}:cmp r2,#0;moveq r0,#0;movne r0,#{row['mode_alias']};b #0x5364E7CC;"
    p.hook(0x5364e750, out)
    for entry, resume in ((0x5364dcb0, 0x5364dcb4), (0x5364dcd8, 0x5364dcdc)):
        out = ''
        for identity, row in p.rows.items():
            out += f'cmp ip,#{identity};beq custom;'
        out += f'bic ip,ip,#4;b #{resume};custom:'
        if entry == 0x5364dcb0:
            out += 'add ip,r0,#0x340;'
        else:
            out += 'ldr lr,[sp,#0xC];cmp lr,#0x2000;addeq ip,r0,#0x320;addne ip,r0,#0x340;'
        out += 'ldr lr,[ip];str lr,[r1];ldr lr,[ip,#4];str lr,[r1,#4];b #0x5364DC68;'
        p.hook(entry, out)
    # The aspect is the SECOND stack argument in both low-level dimension
    # calculators. The first stack argument is another native control; the
    # old height fixture swapped these and missed the aspect5 height path.
    # Probe the original square/panoramic cases to retain each resolution's
    # mode overrides, then replace only its actual crop-dependent dimension.
    for entry, displaced, resume, axis, comparison in (
            (0x5388a954, 'mov ip,sp', 0x5388a958, 0, 3),
            (0x5388af1c, 'ldrb r0,[r0,#9]', 0x5388af20, 8, 5)):
        if asm(displaced, entry) != p.source[entry - BASE:entry - BASE + 4]:
            raise ValueError('LiveView dimension entry ABI has changed')
        original = p.emit(f'{displaced};b #{resume};')
        out = 'push {r4-r10,lr};mov r4,r0;mov r5,r1;mov r6,r2;mov r7,r3;ldr r0,[sp,#36];'
        out += f'bl #{get};cmp r0,#0;beq old;mov r8,r0;sub sp,sp,#16;ldr r0,[sp,#48];str r0,[sp];ldr r0,[sp,#56];str r0,[sp,#8];mov r0,#0;str r0,[sp,#4];'
        call_original = f'mov r0,r4;mov r1,r5;mov r2,r6;mov r3,r7;bl #{original};'
        out += call_original + f'mov r9,r0;mov r0,#{comparison};str r0,[sp,#4];' + call_original
        out += 'cmp r0,r9;beq base;'
        if axis == 0:
            # Panoramic families can have a wider physical output than 3:2;
            # use that native family only for a vertically cropped aperture.
            out += 'ldr r1,[r8];ldr r2,[r8,#4];cmp r1,r2;blo scale;ldr r1,[r8,#8];ldr r2,[r8,#12];cmp r1,r2;bhs base;mov r0,#5;str r0,[sp,#4];'
            out += call_original + 'b done;'
        else:
            out += 'ldr r1,[r8,#8];ldr r2,[r8,#12];'
        out += f'scale:mov r0,r9;bl #{scale};add r0,r0,#1;bic r0,r0,#1;b done;base:mov r0,r9;done:add sp,sp,#16;pop {{r4-r10,pc}};'
        out += f'old:mov r0,r4;mov r1,r5;mov r2,r6;mov r3,r7;pop {{r4-r10,lr}};b #{original};'
        p.hook(entry, out)
    # The vertical scale helper otherwise returns the incoming full-height
    # scale for every new ID. Only a vertically cropped aperture takes its
    # panoramic route; portrait ratios retain the normal route.
    out = f'push {{r0-r3,ip,lr}};mov r0,r7;bl #{get};cmp r0,#0;beq old;ldr r1,[r0,#8];ldr r2,[r0,#12];cmp r1,r2;pop {{r0-r3,ip,lr}};blo #0x5388BE14;b #0x5388BD68;old:pop {{r0-r3,ip,lr}};cmp r7,#5;beq #0x5388BE14;b #0x5388BD68;'
    p.hook(0x5388be10, out)
    out = 'push {r0-r5,r8-r10,lr};ldrb r0,[r4,#0x8D];'
    out += f'bl #{get};cmp r0,#0;beq old;mov r8,r0;'
    for value, origin, fn, fd in (('r6', 300, 0, 4), ('r7', 308, 8, 12)):
        out += f'mov r9,{value};mov r0,{value};ldr r1,[r8,#{fn}];ldr r2,[r8,#{fd}];bl #{scale};add r0,r0,#1;bic r0,r0,#1;mov {value},r0;sub r9,r9,r0;ldr r10,[fp,#-{origin}];add r10,r10,r9,lsr#1;str r10,[fp,#-{origin}];'
    out += 'old:pop {r0-r5,r8-r10,lr};ldr r3,[fp,#-0x12C];b #0x53634EC0;'
    p.hook(0x53634ebc, out)
    for entry, displaced in ((0x53646310, 'cmp r2,#0x100'),):
        native = p.source[entry - BASE:entry - BASE + 4]
        if displaced is None:
            continue
        if asm(displaced, entry) != native:
            raise ValueError('LiveView ROI common ABI has changed')
        out = 'push {r0-r4,r7-r10,lr};mov r0,sl;'
        out += f'bl #{get};cmp r0,#0;beq old;mov r8,r0;mov r0,r6;ldr r1,[r8];ldr r2,[r8,#4];bl #{scale};add r0,r0,#1;bic r6,r0,#1;mov r0,r5;ldr r1,[r8,#8];ldr r2,[r8,#12];bl #{scale};add r0,r0,#1;bic r5,r0,#1;'
        out += f'old:pop {{r0-r4,r7-r10,lr}};{displaced};b #{entry + 4};'
        p.hook(entry, out)
    # Clipped ROI bypasses the common route. Preserve CMP mode flags for
    # the original conditional stores at53646C1C/20.
    out = 'push {r0-r4,r7-r10,lr};mov r0,sl;mrs r10,cpsr;'
    out += f'bl #{get};cmp r0,#0;beq old;mov r8,r0;mov r0,r6;ldr r1,[r8];ldr r2,[r8,#4];bl #{scale};add r0,r0,#1;bic r6,r0,#1;mov r0,r5;ldr r1,[r8,#8];ldr r2,[r8,#12];bl #{scale};add r0,r0,#1;bic r5,r0,#1;'
    out += 'old:msr cpsr_f,r10;pop {r0-r4,r7-r10,lr};strls r5,[fp,#-0x68];b #0x53646C20;'
    p.hook(0x53646c1c, out)
    # This LiveView route has already applied fx/fy to its source ROI above.
    # Its preliminary scales and both converter passes must use the original
    # physical canvas, or the low-level aspect dimensions crop the same axis
    # again. Keep the global calculators and every other caller unchanged.
    # The two preliminary helpers pass aspect in r3; converters use r2.
    p.live_physical_calls = []
    for callsite, target, aspect_register in (
            (0x536462E8, 0x5388C4B8, 'r3'),
            (0x53646300, 0x5388BCB4, 'r3'),
            (0x5364635C, 0x5388B970, 'r2'),
            (0x53646384, 0x5388BAC8, 'r2'),
            (0x53646C48, 0x5388B970, 'r2'),
            (0x53646C6C, 0x5388BAC8, 'r2')):
        if asm(f'bl #{target}', callsite) != p.source[callsite - BASE:callsite - BASE + 4]:
            raise ValueError('LiveView physical converter call ABI has changed')
        out = f'push {{r0-r3,ip,lr}};mov r0,{aspect_register};bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne {aspect_register},#0;'
        # An interior BL replacement must return to the original callsite,
        # rather than the wrapper or an earlier callee's LR value.
        out += addr('lr', callsite + 4) + f'b #{target};'
        p.hook(callsite, out)
        p.live_physical_calls.append(hex(callsite))
    p.get = get
    p.scale = scale
    return p

def _install_develop(program, records):
    """ImageDevelop's coefficient and final descriptor ROI share source IDs."""
    native = CropNative(program.patch.image)
    entries = {}
    for recipe in records:
        public = recipe['identity']['public_id']
        if public < 4:
            continue
        row = planned_record(recipe, program.source)
        preview = native.preview(public)
        factor = math.hypot(preview['width'], preview['height']) / math.hypot(45 << 16, 30 << 16)
        dimensions = program.blob(b''.join(struct.pack('<4H', *values) for values in row['dims']))
        words = [struct.unpack('<I', struct.pack('<f', factor))[0],
                 row['screen']['width'], row['screen']['height'], dimensions, len(row['dims']),
                 row['thumb']['width'], row['thumb']['height']]
        entries[public] = {'address': program.blob(struct.pack('<7I', *words)),
                           'factor': factor, 'preview': preview}
    lookup = ''
    for public, entry in entries.items():
        lookup += f'cmp r0,#{public};' + addr('ip', entry['address']) + 'moveq r0,ip;bxeq lr;'
    get = program.emit(lookup + 'mov r0,#0;bx lr;')
    # Existing 53701D3C divides s15 by source+7DC magnification; the other
    # branch writes it directly. Preserve both original contracts.
    code = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;vldrne s15,[r0];pop {{r0-r3,ip,lr}};beq old;cmp r8,#0;bne #0x53701D48;b #0x53701D3C;old:cmp r0,#1;b #0x53701D90;'
    program.hook(0x53701D8C, code)
    # r8/sb are source dimensions. Try exact native base/target dimensions
    # first, then fit the requested ratio for other runtime descriptors.
    code = f'push {{r0,r4-r7,r10,ip,lr}};mov r0,r3;bl #{get};cmp r0,#0;beq old;mov r4,r0;mov r5,r8;mov r6,sb;'
    code += 'cmp r8,#720;cmpeq sb,#480;ldreq r5,[r4,#4];ldreq r6,[r4,#8];beq crop;cmp r8,#160;cmpeq sb,#120;ldreq r5,[r4,#20];ldreq r6,[r4,#24];beq crop;'
    code += 'ldr r0,[r4,#4];ldr r1,[r4,#8];cmp r8,r0;cmpeq sb,r1;beq crop;ldr r0,[r4,#20];ldr r1,[r4,#24];cmp r8,r0;cmpeq sb,r1;beq crop;'
    code += 'ldr r7,[r4,#12];ldr r10,[r4,#16];dimension:ldrh r0,[r7];ldrh r1,[r7,#2];cmp r8,r0;cmpeq sb,r1;ldrheq r5,[r7,#4];ldrheq r6,[r7,#6];beq crop;ldrh r0,[r7,#4];ldrh r1,[r7,#6];cmp r8,r0;cmpeq sb,r1;beq crop;add r7,r7,#8;subs r10,r10,#1;bne dimension;'
    code += 'ldr r1,[r4,#4];ldr r2,[r4,#8];mul r0,r8,r2;mul r3,sb,r1;cmp r0,r3;blo width;mov r0,r8;mov r7,r1;mov r1,r2;mov r2,r7;'
    code += f'bl #{program.scale};add r0,r0,#2;bic r6,r0,#3;b crop;width:mov r0,sb;bl #{program.scale};add r0,r0,#2;bic r5,r0,#3;'
    code += 'crop:sub r1,r8,r5;sub r2,sb,r6;mov r1,r1,lsr#1;mov r2,r2,lsr#1;mov r3,r5;mov lr,r6;pop {r0,r4-r7,r10,ip};add sp,sp,#4;b #0x53702464;old:pop {r0,r4-r7,r10,ip,lr};cmp r3,#2;b #0x53702564;'
    # LR is the live height argument at this interior ABI. Restore the
    # other saved registers, discard incoming LR, and pass the new height.
    program.hook(0x53702560, code)
    return entries


def install_geometry(patch, plan):
    """Install all geometry consumers and freeze the executed Q16 previews.

    Input must be the server-regenerated plan, including retired records.
    Root's subsequent readback additionally records descriptors and DNG.
    """
    start = len(patch.image)
    program = _build(plan['records'], patch)
    develop = _install_develop(program, plan['records'])
    native = CropNative(patch.image)
    for recipe in plan['records']:
        public = recipe['identity']['public_id']
        if public in program.rows:
            preview = native.preview(public)
            if native.diagnostics:
                raise ValueError('动态裁切原生预览存在诊断')
            recipe.setdefault('geometry', {})['preview'] = preview
    for recipe in plan['crops']:
        archived = next((row for row in plan['records'] if row['identity']['public_id'] == recipe['identity']['public_id']))
        recipe.setdefault('geometry', {}).update(archived.get('geometry', {}))
    return {'hooks': [hex(at) for at, data in program.patches], 'rows': program.rows,
            'develop': develop, 'lookup': program.get, 'scale': program.scale,
            'append_bytes': len(patch.image) - start,
            'original0to5_dimensions_preview_af_preserved': True,
            'original0to3_develop_preserved': True,
            'custom4to5_develop_repaired': True,
            'native_q16_preview_readback': 'passed',
            'software_coordinate_routes': ['53634D6C', '536460B8 normal',
                                           '536460B8 clipped', 'mode>0x100'],
            'live_roi_physical_canvas_calls': program.live_physical_calls,
            'geometry_consumers_installed': True, 'hardware': 'not_tested'}
