"""1.20 geometry consumers for the actual .97 active/retired crop recipes.

Main firmware bytes come from the caller's official 1.20 patch. Geometry is
derived from the supplied .97 recipe, including its exact aligned dimensions.
The portable ARM emitter and ratio arithmetic are shared with the current
core; all native addresses below belong to the 1.20 profile.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
from hashlib import sha256
import math
import struct

from .crop_geometry import Program as _ArmProgram, addr, record, asm
from .crops import parse_ratio
from .layout120_userdata import SOURCE_RTOS_SHA256

BASE = 0x53000000

# Public5 already has native panoramic ROI/coordinate branches. Its source
# .97 uses 180:77 in these branches; official 1.20 still contains 47:20.
# They are independent of the generic public>=6 crop hooks and AF rectangle.
LIVE_NATIVE5_COEFFICIENTS = (
    (0x53659B54, 'lr', 20, 77), (0x53659B5C, 'sb', 47, 180),
    (0x5366B3E0, 'ip', 47, 180), (0x5366B3F8, 'r0', 20, 77),
    (0x5366B868, 'r0', 20, 77), (0x5366B878, 'r1', 47, 180),
    (0x5366B978, 'ip', 20, 77), (0x5366B980, 'r1', 47, 180),
)


class Program(_ArmProgram):
    def hook(self, entry, code):
        baseline = self.patch._crop120_baseline
        before = baseline[entry - BASE:entry - BASE + 4]
        if self.source[entry - BASE:entry - BASE + 4] != before:
            raise ValueError(f'1.20 geometry native hook was already changed: {entry:#x}')
        return super().hook(entry, code)


def prepare_geometry120(records):
    """Preserve the actual source's integer sizes and archived public IDs."""
    base = next(row for row in records if row['identity']['public_id'] == 0)
    def key(item):
        return (item['model'], item['selector'], struct.pack('<f', item['magnification']))
    base_dimensions = {key(item): (item['width'], item['height'])
                       for item in base['geometry']['photo_sizes']}
    result = []
    for recipe in records:
        public = recipe['identity']['public_id']
        if public < 4:
            continue
        geometry = recipe['geometry']
        ratio = parse_ratio(recipe['requested_ratio'])
        row = record(public, (ratio.numerator, ratio.denominator), not recipe.get('active', True))
        row['screen'] = deepcopy(geometry['screen'])
        row['thumb'] = deepcopy(geometry['thumbnail'])
        row['preview'] = deepcopy(geometry['preview'])
        row['fx'] = list(Fraction(row['preview']['width'], 45 << 16).as_integer_ratio())
        row['fy'] = list(Fraction(row['preview']['height'], 30 << 16).as_integer_ratio())
        mapped = {}
        for item in geometry['photo_sizes']:
            original = base_dimensions[key(item)]
            cropped = (item['width'], item['height'])
            if original in mapped and mapped[original] != cropped:
                raise ValueError('Source crop has inconsistent native-size mapping')
            mapped[original] = cropped
        row['dims'] = [[*original, *cropped] for original, cropped in mapped.items()]
        screen = row['screen']
        aperture = [max(80, screen['left']), max(60, screen['top']),
                    min(640, screen['left'] + screen['width']),
                    min(420, screen['top'] + screen['height'])]
        if public == 4:
            aperture = [120, 60, 600, 420]
        elif public == 5:
            aperture = [80, 120, 640, 360]
            row['tracking'] = [778, 634]
        elif public == 6 and recipe['requested_ratio'] == '65:24':
            aperture = [80, 140, 640, 340]
            row['tracking'] = [778, 550]
            row['mode_alias'] = 5
        width, height = aperture[2] - aperture[0], aperture[3] - aperture[1]
        row.update(af=aperture, af_size=[width, height], af_small=[width * 3 // 5, height * 3 // 5])
        result.append(row)
    if len({row['id'] for row in result}) != len(result):
        raise ValueError('Duplicate archived crop public ID')
    return result


def _build_geometry120(rows, patch):
    p = Program(patch)
    values = (deepcopy(row) for row in rows)
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
    # The original aspect5 route has already cropped its ROI. Reapplying
    # fy here turned a native2634-pixel ROI into1690 in 1.20.10.12. Keep its
    # native resolution/binning/zoom choices and restore .97 coefficients.
    live_get = p.emit(f'cmp r0,#5;moveq r0,#0;bxeq lr;b #{get};')
    p.live_native5_coefficients = []
    # Public5 belongs to the legacy panoramic recipe. A fresh 1.20 editor
    # project starts new identities at7, so its unrelated native hidden5
    # coefficients must remain byte-identical unless that legacy ID exists.
    coefficients = LIVE_NATIVE5_COEFFICIENTS if 5 in p.rows else ()
    for at, register, original, replacement in coefficients:
        before = asm(f'mov {register},#{original}', at)
        after = asm(f'mov {register},#{replacement}', at)
        if p.source[at - BASE:at - BASE + 4] != before:
            raise ValueError('Native panoramic LiveView coefficient changed')
        patch.set_word(at, struct.unpack('<I', after)[0], 'preserve .97 native panoramic LiveView ratio')
        p.live_native5_coefficients.append({'address': hex(at), 'before': before.hex(), 'after': after.hex()})
    scale = p.emit('mov ip,r1;mov r3,#0;umull r0,r1,r0,ip;b #0x5315512c;')
    preview_scale = p.emit('mov ip,r1;mov r3,#0;umull r0,r1,r0,ip;mov ip,r2,lsr#1;adds r0,r0,ip;adc r1,r1,#0;b #0x5315512c;')
    out = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r4,r0;bne #0x538b4104;b #{p.target(0x538b40ec)};'
    p.hook(0x538b40ec, out)
    out = f'push {{r0-r3,ip,lr}};mov r0,r3;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r0,r3;bne #0x538b41cc;b #{p.target(0x538b41b4)};'
    p.hook(0x538b41b4, out)
    out = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};beq old;ldrb r1,[r4,#0x8D];cmp r0,r1;moveq r0,#0;movne r0,#1;b #0x53664450;old:b #{p.target(0x53664430)};'
    p.hook(0x53664430, out)
    # Both callers select the cropped window only for their original 2..5
    # enum range. A computed custom window is invisible if selector0 copies
    # the untouched full canvas at object+4 instead of object+36.
    out = f'push {{r0-r3,ip,lr}};mov r0,r4;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne r0,#1;bne #0x53652da8;sub r4,r4,#2;b #0x53652d9c;'
    p.hook(0x53652d98, out)
    out = f'push {{r0-r3,ip,lr}};mov r0,r1;bl #{get};cmp r0,#0;pop {{r0-r3,ip,lr}};beq old;orr r1,r3,#1;ldr r0,[r0,#0x3A0];b #0x538af658;old:sub r1,r1,#2;b #0x53658c5c;'
    p.hook(0x53658c58, out)
    # Main converters call the native base validator before replacing sizes.
    for entry in (0x538f6db0, 0x538f7520):
        old = p.target(entry)
        out = 'cmp r2,#3;bls old;push {r4-r10,lr};mov r4,r2;mov r5,r3;ldr r6,[sp,#32];'
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
    for entry, kind in ((0x538f726c, 'screen'), (0x538f79dc, 'screen'), (0x538f7324, 'thumb'), (0x538f7a94, 'thumb')):
        out = ''
        for identity, row in p.rows.items():
            region = row[kind]
            out += f'cmp r1,#{identity};beq ratio{identity};'
        out += f'b #{p.target(entry)};'
        for identity, row in p.rows.items():
            region = row[kind]
            out += f"ratio{identity}:movw r0,#{region['left']};movw r1,#{region['top']};strh r0,[r2];strh r1,[r3];bx lr;"
        p.hook(entry, out)
    out = 'cmp r2,#3;bls old;cmp r1,#0;bne old;push {r4-r10,lr};mov r5,r0;mov r0,r2;'
    out += f'bl #{get};cmp r0,#0;beq missing;mov r4,r0;mov r0,r5;mov r2,#0;mov ip,sp;bl #0x538aefcc;mov r10,r0;'
    for rect in (36, 52):
        for dimension, origin, fn, fd in ((rect, rect + 8, 0, 4), (rect + 4, rect + 12, 8, 12)):
            out += f'ldr r6,[r5,#{dimension}];mov r0,r6;ldr r1,[r4,#{fn}];ldr r2,[r4,#{fd}];bl #{preview_scale};str r0,[r5,#{dimension}];sub r6,r6,r0;add r6,r6,#1;ldr r7,[r5,#{origin}];add r7,r7,r6,lsr#1;str r7,[r5,#{origin}];'
    out += 'mov r0,r10;pop {r4-r10,pc};missing:mov r0,r5;pop {r4-r10,lr};old:mov ip,sp;b #0x538aefcc;'
    p.hook(0x538aefc8, out)
    for entry, kind in ((0x533adf10, 'rectangle'), (0x533ae148, 'size'), (0x533ae170, 'small')):
        out = ''
        for identity, row in p.rows.items():
            out += f'cmp r0,#{identity};beq ratio{identity};'
        out += f'b #{p.target(entry)};'
        for identity, row in p.rows.items():
            if kind == 'rectangle':
                l, t, r, b = row['af']
                out += f'ratio{identity}:movw r0,#{b};movw r1,#{l};movw r2,#{t};movw r3,#{r};b #0x533adef4;'
            else:
                w, h = row['af_size' if kind == 'size' else 'af_small']
                out += f'ratio{identity}:movw r1,#{w};movw r2,#{h};mov r0,r5;bl #0x53911d30;b #0x533ae0dc;'
        p.hook(entry, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r2,#{identity};beq ratio{identity};'
    out += 'cmp r2,#5;beq #0x5367298c;b #0x536728e8;'
    for identity, row in p.rows.items():
        out += f"ratio{identity}:movw r4,#{row['tracking'][0]};movw r7,#{row['tracking'][1]};b #0x53672994;"
    p.hook(0x536728e4, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r2,#{identity};beq ratio{identity};'
    out += 'cmp r2,#5;beq #0x53672ed0;b #0x53672e84;'
    for identity, row in p.rows.items():
        tx, ty = row['tracking']
        lockx, locky = [778, 1000] if row['mode_alias'] == 5 else [1000, 750]
        out += f'ratio{identity}:cmp r7,#0;movw r5,#{lockx};movw r4,#{locky};bne #0x53672e40;movw r5,#{tx};movw r4,#{ty};b #0x53672de0;'
    p.hook(0x53672e80, out)
    out = ''
    for identity, row in p.rows.items():
        out += f'cmp r1,#{identity};beq ratio{identity};'
    out += 'b #0x536731c8;'
    for identity, row in p.rows.items():
        out += f"ratio{identity}:cmp r2,#0;moveq r0,#0;movne r0,#{row['mode_alias']};b #0x536731c0;"
    p.hook(0x53673144, out)
    for entry, resume in ((0x536726b0, 0x536726b4), (0x536726d8, 0x536726dc)):
        out = ''
        for identity, row in p.rows.items():
            out += f'cmp ip,#{identity};beq custom;'
        out += f'bic ip,ip,#4;b #{resume};custom:'
        if entry == 0x536726b0:
            out += 'add ip,r0,#0x354;'
        else:
            out += 'ldr lr,[sp,#0xC];cmp lr,#0x2000;addeq ip,r0,#0x334;addne ip,r0,#0x354;'
        out += 'ldr lr,[ip];str lr,[r1];ldr lr,[ip,#4];str lr,[r1,#4];b #0x53672668;'
        p.hook(entry, out)
    # The aspect is the SECOND stack argument in both low-level dimension
    # calculators. The first stack argument is another native control; the
    # old height fixture swapped these and missed the aspect5 height path.
    # Probe the original square/panoramic cases to retain each resolution's
    # mode overrides, then replace only its actual crop-dependent dimension.
    for entry, displaced, resume, axis, comparison in (
            (0x538b2264, 'mov ip,sp', 0x538b2268, 0, 3),
            (0x538b282c, 'ldrb r0,[r0,#9]', 0x538b2830, 8, 5)):
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
    out = f'push {{r0-r3,ip,lr}};mov r0,r7;bl #{get};cmp r0,#0;beq old;ldr r1,[r0,#8];ldr r2,[r0,#12];cmp r1,r2;pop {{r0-r3,ip,lr}};blo #0x538b3724;b #0x538b3678;old:pop {{r0-r3,ip,lr}};cmp r7,#5;beq #0x538b3724;b #0x538b3678;'
    p.hook(0x538b3720, out)
    out = 'push {r0-r5,r8-r10,lr};ldrb r0,[r4,#0x8D];'
    out += f'bl #{live_get};cmp r0,#0;beq old;mov r8,r0;'
    for value, origin, fn, fd in (('r6', 300, 0, 4), ('r7', 308, 8, 12)):
        out += f'mov r9,{value};mov r0,{value};ldr r1,[r8,#{fn}];ldr r2,[r8,#{fd}];bl #{scale};add r0,r0,#1;bic r0,r0,#1;mov {value},r0;sub r9,r9,r0;ldr r10,[fp,#-{origin}];add r10,r10,r9,lsr#1;str r10,[fp,#-{origin}];'
    out += 'old:pop {r0-r5,r8-r10,lr};ldr r3,[fp,#-0x12C];b #0x53659828;'
    p.hook(0x53659824, out)
    for entry, displaced in ((0x5366ac98, 'cmp r2,#0x100'),):
        native = p.source[entry - BASE:entry - BASE + 4]
        if displaced is None:
            continue
        if asm(displaced, entry) != native:
            raise ValueError('LiveView ROI common ABI has changed')
        out = 'push {r0-r4,r7-r10,lr};mov r0,sl;'
        out += f'bl #{live_get};cmp r0,#0;beq old;mov r8,r0;mov r0,r6;ldr r1,[r8];ldr r2,[r8,#4];bl #{scale};add r0,r0,#1;bic r6,r0,#1;mov r0,r5;ldr r1,[r8,#8];ldr r2,[r8,#12];bl #{scale};add r0,r0,#1;bic r5,r0,#1;'
        out += f'old:pop {{r0-r4,r7-r10,lr}};{displaced};b #{entry + 4};'
        p.hook(entry, out)
    # Clipped ROI bypasses the common route. Preserve CMP mode flags for
    # the original conditional stores at53646C1C/20.
    out = 'push {r0-r4,r7-r10,lr};mov r0,sl;mrs r10,cpsr;'
    out += f'bl #{live_get};cmp r0,#0;beq old;mov r8,r0;mov r0,r6;ldr r1,[r8];ldr r2,[r8,#4];bl #{scale};add r0,r0,#1;bic r6,r0,#1;mov r0,r5;ldr r1,[r8,#8];ldr r2,[r8,#12];bl #{scale};add r0,r0,#1;bic r5,r0,#1;'
    out += 'old:msr cpsr_f,r10;pop {r0-r4,r7-r10,lr};strls r5,[fp,#-0x68];b #0x5366b5a8;'
    p.hook(0x5366b5a4, out)
    # This LiveView route has already applied fx/fy to its source ROI above.
    # Its preliminary scales and both converter passes must use the original
    # physical canvas, or the low-level aspect dimensions crop the same axis
    # again. Keep the global calculators and every other caller unchanged.
    # The two preliminary helpers pass aspect in r3; converters use r2.
    p.live_physical_calls = []
    for callsite, target, aspect_register in (
            (0x5366ac70, 0x538b3dc8, 'r3'),
            (0x5366ac88, 0x538b35c4, 'r3'),
            (0x5366ace4, 0x538b3280, 'r2'),
            (0x5366ad0c, 0x538b33d8, 'r2'),
            (0x5366b5d0, 0x538b3280, 'r2'),
            (0x5366b5f4, 0x538b33d8, 'r2')):
        if asm(f'bl #{target}', callsite) != p.source[callsite - BASE:callsite - BASE + 4]:
            raise ValueError('LiveView physical converter call ABI has changed')
        out = f'push {{r0-r3,ip,lr}};mov r0,{aspect_register};bl #{live_get};cmp r0,#0;pop {{r0-r3,ip,lr}};movne {aspect_register},#0;'
        # An interior BL replacement must return to the original callsite,
        # rather than the wrapper or an earlier callee's LR value.
        out += addr('lr', callsite + 4) + f'b #{target};'
        p.hook(callsite, out)
        p.live_physical_calls.append(hex(callsite))
    p.get = get
    p.scale = scale
    return p

def _install_develop120(program, rows):
    """ImageDevelop's coefficient and final descriptor ROI share source IDs."""
    entries = {}
    for row in rows:
        public = row['id']
        preview = row['preview']
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
    code = f'push {{r0-r3,ip,lr}};bl #{get};cmp r0,#0;vldrne s15,[r0];pop {{r0-r3,ip,lr}};beq old;cmp r8,#0;bne #0x537266a4;b #0x53726698;old:cmp r0,#1;b #0x537266ec;'
    program.hook(0x537266e8, code)
    # r8/sb are source dimensions. Try exact native base/target dimensions
    # first, then fit the requested ratio for other runtime descriptors.
    code = f'push {{r0,r4-r7,r10,ip,lr}};mov r0,r3;bl #{get};cmp r0,#0;beq old;mov r4,r0;mov r5,r8;mov r6,sb;'
    code += 'cmp r8,#720;cmpeq sb,#480;ldreq r5,[r4,#4];ldreq r6,[r4,#8];beq crop;cmp r8,#160;cmpeq sb,#120;ldreq r5,[r4,#20];ldreq r6,[r4,#24];beq crop;'
    code += 'ldr r0,[r4,#4];ldr r1,[r4,#8];cmp r8,r0;cmpeq sb,r1;beq crop;ldr r0,[r4,#20];ldr r1,[r4,#24];cmp r8,r0;cmpeq sb,r1;beq crop;'
    code += 'ldr r7,[r4,#12];ldr r10,[r4,#16];dimension:ldrh r0,[r7];ldrh r1,[r7,#2];cmp r8,r0;cmpeq sb,r1;ldrheq r5,[r7,#4];ldrheq r6,[r7,#6];beq crop;ldrh r0,[r7,#4];ldrh r1,[r7,#6];cmp r8,r0;cmpeq sb,r1;beq crop;add r7,r7,#8;subs r10,r10,#1;bne dimension;'
    code += 'ldr r1,[r4,#4];ldr r2,[r4,#8];mul r0,r8,r2;mul r3,sb,r1;cmp r0,r3;blo width;mov r0,r8;mov r7,r1;mov r1,r2;mov r2,r7;'
    code += f'bl #{program.scale};add r0,r0,#2;bic r6,r0,#3;b crop;width:mov r0,sb;bl #{program.scale};add r0,r0,#2;bic r5,r0,#3;'
    code += 'crop:sub r1,r8,r5;sub r2,sb,r6;mov r1,r1,lsr#1;mov r2,r2,lsr#1;mov r3,r5;mov lr,r6;pop {r0,r4-r7,r10,ip};add sp,sp,#4;b #0x53726dc0;old:pop {r0,r4-r7,r10,ip,lr};cmp r3,#2;b #0x53726ec0;'
    # LR is the live height argument at this interior ABI. Restore the
    # other saved registers, discard incoming LR, and pass the new height.
    program.hook(0x53726ebc, code)
    return entries


def install_geometry120(patch, records, *, baseline_rtos):
    """Install crop geometry after proving the exact 1.20 native base.

    The returned receipt describes the installed bytes. A caller must still
    run native readback/RAW/menu/state tests before packaging a full candidate.
    """
    if sha256(bytes(baseline_rtos)).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('Crop 1.20 requires the verified original 1.20 RTOS')
    patch._crop120_baseline = bytes(baseline_rtos)
    patch.official_base = True
    rows = prepare_geometry120(records)
    start = len(patch.image)
    program = _build_geometry120(rows, patch)
    develop = _install_develop120(program, rows)
    if BASE + len(patch.image) >= 0x55000000:
        raise ValueError('1.20 crop geometry reaches known runtime-data memory')
    return {'backend': 'official-1.20-crop-geometry',
            'source_rtos_sha256': SOURCE_RTOS_SHA256,
            'hooks': [hex(at) for at, data in program.patches],
            'rows': program.rows, 'develop': develop,
            'lookup': program.get, 'scale': program.scale,
            'append_bytes': len(patch.image) - start,
            'native_factory_public_ids': [0, 1, 2, 3],
            'archived_custom_public_ids': sorted(program.rows),
            'live_roi_physical_canvas_calls': program.live_physical_calls,
            'native5_live_coefficients': program.live_native5_coefficients,
            'native5_live_roi': ('native180:77; excluded from repeated dynamic ROI and physical-canvas overrides'
                if 5 in program.rows else 'factory47:20 coefficient bytes retained; legacy public5 absent'),
            'tracking_recipe_fields': {'normal_pair': '0x334', 'square_pair': '0x354'},
            'native_readback': 'pending', 'hardware': 'not_tested'}

