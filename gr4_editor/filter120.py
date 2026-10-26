"""Extend the 1.20 factory filter menu and profile routes without Mono unlock."""
from hashlib import sha256
import struct

from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, _branch, _mov, _target


FACTORY_ORDER = (11, 12, 13, 14, 15, 16, 24, 17, 18, 19, 23, 20, 21, 22,
                 25, 29, 26, 27, 28, 31, 32, 30)
FACTORY_VECTOR = 0x53DE8CD0
NATIVE_EFFECTS_PARENT13_SHA256 = '80c542a83528d5f5bd72b81fd2249f9fe98582f026f112835f13b92fe80c39e0'
NATIVE_EFFECTS_PARENT14_SHA256 = '17759f5065b616af5d9928666379e5d9637d1061a60aaab69c049b95b98108a2'
GATES = {'profile': (0x5393AA94, 0xE3520020),
         'admission': (0x533DE144, 0xE3520000),
         'extraction': (0x5393914C, 0xE352000B),
         'source_admission': (0x53784648, 0xE3510000),
         'detail': (0x531C80E8, 0xE244400B)}


def disable_pictorialist_admission120(patch, *, baseline_rtos):
    """Close new style31 selection while retaining existing captures/read paths."""
    if sha256(baseline_rtos).hexdigest() != NATIVE_EFFECTS_PARENT14_SHA256:
        raise ValueError('closing Pictorialist requires the actual delivered .14 parent')
    site = GATES['admission'][0]
    prior_word = int.from_bytes(baseline_rtos[site-BASE:site-BASE+4], 'little')
    if patch.word(site) != prior_word or prior_word & 0xFF000000 != 0xEA000000:
        raise ValueError('actual .14 admission chain differs')
    prior = _target(site, prior_word)
    target = patch.append(lambda at: [0xE351001F, 0x03A00000, 0x012FFF1E,
                                     _branch(at+12, prior)], 16)
    patch.set_word(site, _branch(site, target), 'close unreleased Pictorialist selection; retain Solarized/custom routes')
    return {'site': hex(site), 'helper': hex(target), 'prior_admission': hex(prior),
        'prior_word': hex(prior_word), 'enabled_styles': [32], 'disabled_styles': [31],
        'existing_capture_metadata_retained': True, 'other_admission_unchanged': True}


def install_native_effects_admission120(patch, *, baseline_rtos, styles=(31, 32)):
    """Admit selected native effects outside the existing custom-style route.

    Keeps the color-body identity and every existing admission fallback. The
    caller separately installs the proven monochrome imaging configuration;
    making a menu row selectable alone is not a pixel-processing guarantee.
    """
    if sha256(baseline_rtos).hexdigest() != NATIVE_EFFECTS_PARENT13_SHA256:
        raise ValueError('native effects require the exact actual 1.20.10.13 parent RTOS')
    styles = tuple(styles)
    if not styles or len(set(styles)) != len(styles) or any(style not in (31, 32) for style in styles):
        raise ValueError('only native Pictorialist31 and Solarized32 may be admitted')
    site, _ = GATES['admission']
    prior_word = patch.word(site)
    if prior_word != int.from_bytes(baseline_rtos[site - BASE:site - BASE + 4], 'little'):
        raise ValueError('the actual .13 admission route has already changed')
    if prior_word & 0xFF000000 != 0xEA000000:
        raise ValueError('install the custom filter admission route before native effects')
    prior = _target(site, prior_word)
    if not BASE <= prior < BASE + len(patch.image):
        raise ValueError('existing admission route is outside this RTOS')

    def generate(at):
        words = [0xE3520000, 0]
        for style in styles:
            if style == 31:
                here = at + len(words) * 4
                # Pictorialist has still/RAW parameters, but no movie state.
                # Match its native mode guard rather than admitting inert UI.
                words += [0xE351001F, _branch(here + 4, here + 24, condition=1),
                          0xE5D0001F, 0xE2500001, 0x13A00001, 0xE12FFF1E]
            else:
                words += [0xE3510000 | style, 0x03A00001, 0x012FFF1E]
        fallback = at + len(words) * 4
        words[1] = _branch(at + 4, fallback, condition=1)
        return words + [_branch(fallback, prior)]

    target = patch.append(generate, 16)
    patch.set_word(site, _branch(site, target), 'admit native31/32 in color mode while retaining all custom filter routes')
    return {'site': hex(site), 'helper': hex(target), 'prior_admission': hex(prior),
            'source_parent_rtos_sha256': NATIVE_EFFECTS_PARENT13_SHA256,
            'prior_word': hex(prior_word), 'styles': list(styles), 'color_mode_argument': 0,
            'model_mode_offset': '0x1f', 'style31_video_admitted': False,
            'custom_admission_retained': True, 'factory_other_modes_retained': True,
            'global_mono_unlock': False, 'product_identity_changed': False,
            'feature_matrix_changed': False, 'imaging_configuration': 'installed separately',
            'hardware': 'not_tested'}


def install_filter_menu120(patch, rows, *, baseline_rtos, native_order=None, menu_order=None, routing_rows=None):
    if sha256(baseline_rtos).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('filter routing requires the exact official 1.20 RTOS')
    if baseline_rtos[FACTORY_VECTOR - BASE:FACTORY_VECTOR - BASE + 22] != bytes(FACTORY_ORDER):
        raise ValueError('native 1.20 factory order differs')
    identities = [row['identity'] for row in (rows if routing_rows is None else routing_rows)]
    styles = [row['identity']['style_id'] for row in rows]
    all_styles = [item['style_id'] for item in identities]
    if (len(set(all_styles)) != len(all_styles) or len(set(styles)) != len(styles) or
            not set(styles).issubset(all_styles) or any(not 34 <= style <= 90 for style in all_styles)):
        raise ValueError('custom styles overlap the native 1.20 namespace')
    native_order = list(FACTORY_ORDER if native_order is None else native_order)
    if len(set(native_order)) != len(native_order) or any(style not in FACTORY_ORDER for style in native_order):
        raise ValueError('Invalid native 1.20 filter visibility order')
    order = [*native_order, *styles]
    if menu_order is not None:
        if len(set(menu_order)) != len(menu_order) or set(menu_order) != set(order):
            raise ValueError('1.20 menu order differs from its visible native/custom identities')
        order = list(menu_order)
    vector = patch.append(bytes(order), 16)

    def edit(site, expected, value, reason):
        if patch.word(site) != expected:
            raise ValueError(f'1.20 filter contract differs at {site:#x}')
        patch.set_word(site, value, reason)

    for low, high in ((0x533DE1F0, 0x533DE1F8), (0x533840FC, 0x53384100)):
        edit(low, _mov(4, FACTORY_VECTOR & 65535), _mov(4, vector & 65535), '1.20 complete factory plus custom order low')
        edit(high, _mov(4, FACTORY_VECTOR >> 16, high=True), _mov(4, vector >> 16, high=True), '1.20 complete factory plus custom order high')
    for site, prefix in ((0x533DE208, 0xE2847000), (0x53384108, 0xE2847000),
                         (0x533DE330, 0xE3A00000), (0x533CFA00, 0xE3A00000),
                         (0x533DE374, 0xE2853000)):
        edit(site, prefix | 22, prefix | len(order), '1.20 filter vector count/capacity')
    # Both RAW selector vectors and their UI row pool reserve the full list,
    # the three Custom groups and Original, independently of a cold model.
    capacity = len(order) + 4
    for site, prefix, old, new in (
        (0x533840C8, 0xE3530000, 25, capacity - 1),
        (0x533840EC, 0xE3530000, 25, capacity - 1),
        (0x53384264, 0xE3A00000, 26, capacity),
        (0x533842AC, 0xE3A00000, 26, capacity),
        (0x5338429C, 0xE2863000, 26, capacity),
        (0x533842E4, 0xE2863000, 26, capacity),
        (0x53379C6C, 0xE3A00000, 26, capacity)):
        edit(site, prefix | old, prefix | new, '1.20 RAW selector capacity including native Palette and custom groups')

    entries = {}
    for role, (site, expected) in GATES.items():
        def generate(at, role=role, site=site, expected=expected):
            # Detail's displaced SUB has no S bit: preserve incoming flags as
            # well as ip while selecting the Standard widget for custom rows.
            words = [0xE92D1000, 0xE10FC000, 0xE92D1000] if role == 'detail' else []
            detail_exits = []
            for item in identities:
                here = at + len(words) * 4
                register = {'profile': 2, 'admission': 1, 'extraction': 2,
                            'source_admission': 1, 'detail': 4}[role]
                words.append(0xE3500000 | register << 16 | item['style_id'])
                if role == 'profile':
                    words += [_mov(3, item['profile_id'], condition=0),
                              _branch(here + 8, 0x5393AAA8, condition=0)]
                elif role == 'admission':
                    words += [0x03520000, 0x03A00001, 0x012FFF1E]
                elif role in ('extraction', 'source_admission'):
                    words += [_branch(here + 4, 0x53939298 if role == 'extraction' else 0x53784758, condition=0)]
                else:
                    words += [0x03500001, 0x03A04000, 0]
                    detail_exits.append(len(words) - 1)
            if role == 'detail':
                cleanup = [0xE8BD1000, 0xE128F00C, 0xE8BD1000]
                fallback = at + len(words) * 4
                words += cleanup + [expected, _branch(fallback + 16, site + 4)]
                custom = at + len(words) * 4
                words += cleanup + [_branch(custom + 12, site + 4)]
                for index in detail_exits:
                    words[index] = _branch(at + index * 4, custom, condition=0)
                return words
            return words + [expected, _branch(at + len(words) * 4 + 4, site + 4)]
        target = patch.append(generate, 16)
        edit(site, expected, _branch(site, target), '1.20 custom filter ' + role)
        entries[role] = hex(target)
    return {'order': order, 'vector': hex(vector), 'entries': entries,
            'raw_selector_capacity': capacity, 'native_style_domain': [0, 33],
            'native_mono_unlock': False, 'factory_fallbacks_retained': True,
            'hardware': 'not_tested'}
