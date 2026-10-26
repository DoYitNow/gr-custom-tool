"""Nine custom scalar closures bound to the audited native 1.20 functions."""
from hashlib import sha256
import struct

from .layout120_userdata import SOURCE_RTOS_SHA256
from .slots import BASE, PARAMETER_IDS, _branch, _clone_scalar, _defaults


def install_scalar120(patch, rows, main_helper, *, bindings, baseline_rtos):
    """Retain each native getter, setter and default path behind custom dispatch.

    The layout is an explicit result of the native role/ABI audit. In particular
    the property setters include Palette-era changes and the UserData offsets
    come from its actual 1.20 constructor, rather than an address delta.
    """
    if sha256(baseline_rtos).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('scalar migration requires the exact official 1.20 RTOS')
    if bindings.get('source_rtos_sha256') != SOURCE_RTOS_SHA256:
        raise ValueError('scalar binding provenance differs from the native base')
    def number(value):
        return int(value, 0) if isinstance(value, str) else value
    layout = {key: ([number(x) for x in value] if isinstance(value, list) else number(value))
              for key, value in bindings['scalar_layout'].items()}
    entries = [number(value) for value in bindings['default_entries']]
    required = ('getters', 'setters', 'property_setters', 'app_get', 'userdata_get',
                'node_get', 'still_movie', 'photo_selector', 'photo_styles',
                'mutator_start', 'wrapper_start', 'wrapper_end')
    if set(layout) != set(required) or len(entries) != 9:
        raise ValueError('scalar migration requires all audited field/API bindings')
    fallbacks = []
    for role in ('getters', 'setters'):
        if len(layout[role]) != 9:
            raise ValueError('all nine scalar fields must be bound')
        for site in layout[role]:
            if patch.word(site) != 0xE1A0C00D:
                raise ValueError(f'native 1.20 {role} prologue differs at {site:#x}')
            bridge = patch.append(lambda at, site=site:
                [0xE1A0C00D, _branch(at + 4, site + 4)], 16)
            patch.set_word(site, _branch(site, bridge), 'preserve native 1.20 scalar continuation')
            fallbacks.append({'role': role, 'entry': hex(site), 'body': hex(site + 4),
                              'bridge': hex(bridge)})
    closures = [_clone_scalar(patch, row['identity']['style_id'], main_helper,
                              native_layout=layout) for row in rows]
    for closure in closures:
        start = int(closure['address'], 16) - BASE
        closure['template_sha256'] = closure['sha256']
        closure['sha256'] = sha256(patch.image[start:start + closure['bytes']]).hexdigest()
    defaults = []
    for field, site in enumerate(entries):
        displaced, = struct.unpack_from('<I', baseline_rtos, site - BASE)
        if patch.word(site) != displaced:
            raise ValueError(f'native 1.20 default entry differs at {site:#x}')
        if displaced != 0xE1A0C00D:
            raise ValueError('default binding does not have the verified native 1.20 MOV ip,sp prologue')
        prior = patch.append(lambda at, site=site, displaced=displaced:
                             [displaced, _branch(at + 4, site + 4)], 16)
        def generate(at, prior=prior, field=field):
            words = []
            for row in rows:
                here = at + len(words) * 4
                style, value = row['identity']['style_id'], _defaults(row)[field]
                words += [0xE3510000 | style, _branch(here + 4, here + 28, condition=1),
                          0xE5D0C01F, 0xE35C0000, _branch(here + 16, prior, condition=1),
                          0xE3A00000 | value, 0xE12FFF1E]
            return words + [_branch(at + len(words) * 4, prior)]
        wrapper = patch.append(generate, 16)
        patch.set_word(site, _branch(site, wrapper), '1.20 custom default ' + PARAMETER_IDS[field])
        defaults.append({'entry': hex(site), 'native_bridge': hex(prior), 'wrapper': hex(wrapper)})
    return {'closures': closures, 'native_fallbacks': fallbacks, 'defaults': defaults,
            'layout': layout, 'hardware': 'not_tested'}
