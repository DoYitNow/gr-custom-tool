"""1.20 custom identities and capture-version-aware legacy MakerNote codes.

0x8106/7 were used by .97 and are now official. New captures use 0x810f/10.
A bounded RAM cache belongs to the supplied RAW owner; it never edits a photo.
Its entries are keyed by parsed object/context and cleared on native lifecycle
calls. Missing provenance is represented by the native unknown/default value.
"""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import re
import struct
import subprocess
import tempfile

from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, _branch, _mov, _toolchain
from .toolchain import linker_flags, _verify_elf

OWNER_BYTES = 0x188
NATIVE_MAKER_TABLE = 0x53FD7FC8
FUNCTIONS = {
    'raw': (0x5338262C, 0x160), 'inverse': (0x53382A14, 0x178),
    'inverse2': (0x53382BC0, 0x178), 'writer': (0x53804F34, 0x160),
    'style': (0x537FBAC8, 0x1AC), 'property': (0x537FEED0, 0x1C4),
    'version': (0x537FA76C, 0x68), 'software': (0x53816558, 0x74),
    'property_ctor': (0x537F9E8C, 0x310),
    'dispatch': (0x538211A8, 0x6B0), 'dispatch_raw': (0x538473A4, 0x4F0),
    'dispatch_raw2': (0x5384DF30, 0x4C0),
}


def plan_namespace120(rows, *, product=False):
    result = deepcopy(rows)
    custom = sorted(result, key=lambda r: r['identity']['style_id'])
    for index, row in enumerate(custom):
        item = row['identity']
        style = item['style_id']
        if not 34 <= style <= (90 if product else 42):
            raise ValueError('1.20 namespace requires the retained .97 styles 34..42')
        item['legacy_maker_note_code'] = item.get('legacy_maker_note_code', item['maker_note_code'])
        item['maker_note_code'] = (0x8200 + style if product else
                                  {34: 0x810F, 35: 0x8110}.get(style, 0x8106 + style - 34))
        item['raw_ordinal'] = 23 + index
        item['playback_property'] = 23 + index
    return result


def _compile(origin, source, targets):
    clang, objcopy, nm = _toolchain()
    with tempfile.TemporaryDirectory(prefix='gr4-namespace120-') as folder:
        folder = Path(folder)
        c, ld, elf, binary = [folder/name for name in ('namespace.c', 'namespace.ld', 'namespace.elf', 'namespace.arm')]
        c.write_text(source, encoding='utf-8')
        ld.write_text(''.join(f'{k} = {v:#x};\n' for k, v in targets.items()) +
            f'SECTIONS {{ . = {origin:#x}; .text : {{ *(.text*) *(.rodata*) }} '
            '/DISCARD/ : { *(.ARM.exidx*) *(.ARM.extab*) *(.comment*) } }', encoding='ascii')
        result = subprocess.run([str(clang), *linker_flags(clang), '-target', 'armv7-none-eabi',
            '-mcpu=cortex-a9', '-marm', '-mfloat-abi=soft', '-Os', '-ffreestanding', '-fno-builtin',
            '-fno-unwind-tables', '-fno-asynchronous-unwind-tables', '-nostdlib', '-Wl,-T,'+str(ld),
            '-Wl,--entry=namespace120_initialize', str(c), '-o', str(elf)], capture_output=True, text=True)
        if result.returncode:
            raise ValueError('1.20 namespace ARM compilation failed: '+result.stderr[-2000:])
        _verify_elf(elf.read_bytes())
        listing = subprocess.run([str(nm), '-n', str(elf)], check=True, capture_output=True, text=True).stdout
        if re.search(r'^\s+U\s', listing, re.M) or 'Thunk_' in listing:
            raise ValueError('namespace native call is unresolved or outside ARM branch range')
        symbols = {name: int(at, 16) for at, kind, name in
            re.findall(r'^([0-9a-fA-F]+)\s+(\w)\s+(\w+)$', listing, re.M) if kind in 'Tt'}
        subprocess.run([str(objcopy), '-O', 'binary', '--only-section=.text', str(elf), str(binary)], check=True, capture_output=True)
        return binary.read_bytes(), symbols


def install_namespace120(patch, rows, *, owner_offset, baseline_rtos, owner_cell=0x55089688,
                         owner_getter=0x53240C98, prior_initializer=None, product=False):
    if sha256(baseline_rtos).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('namespace migration requires the verified official 1.20 image')
    if owner_offset < 0x1000 or owner_offset % 4:
        raise ValueError('namespace cache needs an aligned actual RAW-owner tail')
    rows = plan_namespace120(rows, product=product)
    if not rows:
        raise ValueError('namespace migration requires at least one custom identity')
    native_pairs = struct.unpack_from('<44H', baseline_rtos, NATIVE_MAKER_TABLE-BASE)
    native_codes = set(native_pairs[1::2])
    if any(row['identity']['maker_note_code'] in native_codes for row in rows):
        raise ValueError('new custom MakerNote code overlaps the complete native table')
    bridges = {}
    contracts = []
    for name, (site, length) in FUNCTIONS.items():
        before = baseline_rtos[site-BASE:site-BASE+length]
        if bytes(patch.image[site-BASE:site-BASE+length]) != before:
            raise ValueError(f'1.20 namespace native {name} body was already changed')
        displaced = patch.word(site)
        # First words are either data-processing or CMP; no PC-relative operand.
        if displaced & 0x0E000000 not in (0x02000000, 0x00000000):
            raise ValueError(f'namespace entry cannot be bridged: {site:#x}')
        bridges['native_'+name] = patch.append(lambda at, word=displaced, site=site:
            [word, _branch(at+4, site+4)], 4)
        contracts.append({'name': name, 'site': hex(site), 'bytes': length,
                          'entry_word': displaced, 'sha256': sha256(before).hexdigest()})
    definitions = {'OWNER_OFFSET': owner_offset, 'OWNER_CELL': owner_cell, 'OWNER_GETTER': owner_getter,
                   'HAS_PRIOR_INITIALIZER': int(bool(prior_initializer))}
    source = ''.join(f'#define {key} {value}u\n' for key, value in definitions.items())
    source += '#define IDENTITIES {' + ','.join('{%du,%du,%du,%du,%du}' % (
        r['identity']['style_id'], r['identity']['raw_ordinal'], r['identity']['playback_property'],
        r['identity']['maker_note_code'], r['identity']['legacy_maker_note_code']) for r in rows) + '}\n'
    source += '#define ICON_IDS {' + ','.join(str(r['identity']['icon_id'])+'u' for r in rows) + '}\n'
    native_source = (Path(__file__).parent/'native/namespace120.c').read_text(encoding='utf-8')
    if product:
        # Same proven capture-cache corrections as .15, at source level for
        # newly linked products rather than a .14-only binary revision.
        native_source = native_source.replace('State *s=state();Entry *e;',
            'if(!*(void *volatile *)OWNER_CELL)((void *(*)(void))OWNER_GETTER)();State *s=state();Entry *e;')
        native_source = native_source.replace("||p[i+4]!=0", "")
        native_source = native_source.replace(
            'return ((u32)(p[i]-\'0\')<<24)|((u32)((p[i+2]-\'0\')*10+p[i+3]-\'0\')<<16);',
            'u32 v=((u32)(p[i]-\'0\')<<24)|((u32)((p[i+2]-\'0\')*10+p[i+3]-\'0\')<<16);'
            'for(i+=4;i<*(u32 *)(e+8);i++)if(p[i]&&p[i]!=32)return 0;return v;')
    source += native_source
    if prior_initializer:
        bridges['prior_namespace120_initializer'] = prior_initializer
    origin = (BASE+len(patch.image)+15)&~15
    blob, symbols = _compile(origin, source, bridges)
    if patch.append(blob, 16) != origin:
        raise ValueError('namespace appended ARM address changed')
    hooks = []
    for name, (site, _) in FUNCTIONS.items():
        patch.set_word(site, _branch(site, symbols['namespace120_'+name]), '1.20 capture-aware custom identity '+name)
        hooks.append({'site': hex(site), 'target': hex(symbols['namespace120_'+name]), 'role': name})
    # Tiny native lifetime methods are exact bodies, not generic relocated LDRs.
    for site, expected, name in (
        (0x537FA3C4, [0xE59F200C, 0xE5802000, 0xE3E02000, 0xE5C02004, 0xE12FFF1E], 'common_context_ctor'),
        (0x537FA3A4, [0xE12FFF1E], 'common_context_dtor'),
        (0x537F9E88, [0xE12FFF1E], 'property_dtor'),
    ):
        if [patch.word(site+4*i) for i in range(len(expected))] != expected:
            raise ValueError(f'namespace metadata lifecycle differs at {site:#x}')
        patch.set_word(site, _branch(site, symbols['namespace120_'+name]), '1.20 metadata provenance lifetime '+name)
        hooks.append({'site': hex(site), 'target': hex(symbols['namespace120_'+name]), 'role': name})
    def emit_dispatch(site, register, field, fallback, success=None):
        words = []
        target = (BASE+len(patch.image)+15)&~15
        for row in rows:
            item = row['identity']
            words.extend((0xE3500000 | register << 16 | item['style_id'], _mov(0, item[field], condition=0)))
            words.append(_branch(target+len(words)*4, success, condition=0) if success else 0x012FFF1E)
        words.extend((fallback, _branch(target+(len(words)+1)*4, site+4)))
        stub = patch.append(words, 16)
        patch.set_word(site, _branch(site, stub), '1.20 custom identity '+field)
        hooks.append({'site': hex(site), 'target': hex(stub), 'role': field})
    for site, field in ((0x5338EDF8, 'text_id'), (0x533905D8, 'icon_id')):
        if patch.word(site) != 0xE351000A:
            raise ValueError('native 1.20 menu identity fallback changed')
        emit_dispatch(site, 1, field, 0xE351000A)
    if patch.word(0x533B6134) != 0xE2433008:
        raise ValueError('native 1.20 playback icon switch changed')
    emit_dispatch(0x533B6134, 3, 'icon_id', 0xE2433008, 0x533B61C8)
    # Native property 0..22 stays intact; custom properties are 23..31.
    site = 0x533B305C
    words = []
    at = (BASE+len(patch.image)+15)&~15
    for row in rows:
        item = row['identity']
        words.extend((0xE3500000|item['playback_property'],
                      _branch(at+(len(words)+1)*4, at+(len(words)+5)*4, condition=1),
                      _mov(3,item['style_id']), 0xE3540000,
                      _branch(at+(len(words)+4)*4, 0x533B3078)))
    words.extend((0xE2400001,_branch(at+(len(words)+1)*4,site+4)))
    if patch.word(site) != 0xE2400001:
        raise ValueError('native 1.20 property-to-style switch changed')
    stub = patch.append(words,16);patch.set_word(site,_branch(site,stub),'1.20 custom playback property-to-style')
    hooks.append({'site':hex(site),'target':hex(stub),'role':'property_to_style'})
    # Reuse the complete native Standard detail widget for the custom recipe.
    site = 0x533B6FEC
    if patch.word(site) != 0xE2433008:
        raise ValueError('native 1.20 playback detail switch changed')
    words=[];at=(BASE+len(patch.image)+15)&~15
    for row in rows:
        words.extend((0xE3530000|row['identity']['style_id'],0x03A0300B))
    words.extend((0xE2433008,_branch(at+(len(words)+1)*4,site+4)))
    stub=patch.append(words,16);patch.set_word(site,_branch(site,stub),'1.20 custom playback detail uses native Standard fields')
    hooks.append({'site':hex(site),'target':hex(stub),'role':'playback_detail'})
    # The custom marker lives only across these two MOVs between the getter and
    # icon call; in particular no byte store or truncation is in this interval.
    summary_chains=[]
    for getter in (0x531B2404,0x531B2B34):
        if [patch.word(getter+4),patch.word(getter+8)] != [0xE1A01000,0xE1A00005]:
            raise ValueError('native 1.20 settings summary no longer directly passes the getter word')
        summary_chains.append({'getter':hex(getter),'icon':hex(getter+12),
            'intermediate_words':[0xE1A01000,0xE1A00005],
            'transient_custom_marker':0x100,'stored_or_byte_truncated':False})
    # Photo/movie summary calls use their own native selectors and one native icon lookup.
    for site, target, role in ((0x531B2404,0x532A1280,'photo'),(0x531B2B34,0x532A14BC,'movie'),
        (0x531B2410,0x533A6868,'settings_icon'),(0x531B2B40,0x533A6868,'settings_icon')):
        if patch.word(site) != _branch(site,target,link=True):
            raise ValueError('native 1.20 settings selector/icon call changed')
        patch.set_word(site,_branch(site,symbols['namespace120_'+role],link=True),'1.20 custom '+role)
        hooks.append({'site':hex(site),'target':hex(symbols['namespace120_'+role]),'role':role})
    return {'rows':rows,'symbols':symbols,'initializer':symbols['namespace120_initialize'],
        'owner_offset':owner_offset,'owner_bytes':OWNER_BYTES,'native_maker_codes':list(native_pairs[1::2]),
        'native_raw_property_range':[0,22],'custom_raw_property_range':[23,22+len(rows)],
        'legacy_ambiguous_codes':{'0x8106':34,'0x8107':35},
        'missing_capture_version':'native unknown/default, never inferred from live firmware',
        'code':{'address':hex(origin),'bytes':len(blob),'sha256':sha256(blob).hexdigest()},
        'contracts':contracts,'hooks':hooks,'summary_chains':summary_chains,'hardware':'not_tested'}


def inspect_namespace120(rtos, report):
    """Re-read the actual candidate; normalize only the declared entry branches."""
    for hook in report['hooks']:
        site, target = int(hook['site'],16), int(hook['target'],16)
        expected = _branch(site,target,link=hook['role'] in ('photo','movie','settings_icon'))
        actual,=struct.unpack_from('<I',rtos,site-BASE)
        if actual != expected:
            raise ValueError(f'1.20 namespace {hook["role"]} hook differs')
    for contract in report['contracts']:
        at=int(contract['site'],16)-BASE
        native=bytearray(rtos[at:at+contract['bytes']])
        struct.pack_into('<I',native,0,contract['entry_word'])
        if sha256(native).hexdigest()!=contract['sha256']:
            raise ValueError('1.20 namespace original native body differs: '+contract['name'])
    for chain in report['summary_chains']:
        site=int(chain['getter'],16)
        if list(struct.unpack_from('<2I',rtos,site+4-BASE))!=chain['intermediate_words']:
            raise ValueError('1.20 transient summary marker interval changed')
    code=report['code'];at=int(code['address'],16)-BASE
    if sha256(rtos[at:at+code['bytes']]).hexdigest()!=code['sha256']:
        raise ValueError('1.20 namespace compiled ARM differs')
    native=struct.unpack_from('<44H',rtos,NATIVE_MAKER_TABLE-BASE)
    if list(native[1::2])!=report['native_maker_codes']:
        raise ValueError('1.20 native MakerNote table differs')
    return {'status':'passed','hooks':len(report['hooks']),
            'native_bodies':len(report['contracts']),'hardware':'not_tested'}
