"""Compile the maintained custom-parameter sidecar for the native 1.20 lifecycle.

The shared RAW owner must reserve the returned layout. No separate allocation
or old .80 tail is assumed. Original 1.20 UserMode payloads retain their B44
bytes and the newly introduced startup restore path receives the same sidecar.
"""
from hashlib import sha256
import struct

from .layout120_userdata import USERDATA120, SOURCE_RTOS_SHA256
from .shutdown120 import NATIVE_APIS
from .slots import BASE, _branch, _compile_state, _target
from .usermode120 import native_persistence_contract, install_usermode_restore120


OWNER_CELL = 0x55089688
OWNER_GETTER = 0x53240C98
HOOKS = (
    (0x533F6C80, 'save', (5, 7), 0xE1A01004),
    (0x533F6D34, 'save', (5, 7), 0xE1A01004),
    (0x533F6910, 'save', (4, 6), 0xEAFFFFEB),
    (0x533F694C, 'save', (4, 6), 0xEAFFFFDC),
    (0x533F6864, 'restore', (6, 7), 0xE1A01007),
    (0x533F6880, 'restore', (6, 7), 0xE1A01007),
    (0x533F6998, 'reset', (5, 8), 0xE5D53000),
    (0x533F3058, 'reset', (5, 4), 0xE24BD018),
)


class _StateBindings120:
    """Roles separately identified in the native field/persistence audits."""
    addresses = {
        0x53156140: USERDATA120.native_memcpy,
        0x53156768: USERDATA120.native_memset,
        0x538E09B0: 0x539084BC,
        0x550885B4: OWNER_CELL,
        0x5323F078: OWNER_GETTER,
        0x5323F018: USERDATA120.current_getter,
        0x53A81480: NATIVE_APIS['APDATA_FACTORY'],
        0x53A7E208: NATIVE_APIS['APDATA_LENGTH'],
        0x53A7E2B8: NATIVE_APIS['APDATA_EXISTS'],
        0x53A7E434: NATIVE_APIS['APDATA_READ'],
        0x53A7E518: NATIVE_APIS['APDATA_WRITE'],
    }

    def resolve(self, old):
        try:
            return self.addresses[old]
        except KeyError as exc:
            raise ValueError(f'1.20 registry state contains an unaudited native address: {old:#x}') from exc


def compile_registry_state120(patch, rows, *, state_offset, baseline_rtos):
    if sha256(baseline_rtos).hexdigest() != SOURCE_RTOS_SHA256:
        raise ValueError('registry migration requires the exact original 1.20 RTOS')
    if type(state_offset) is not int or state_offset < 0x1000 or state_offset % 4:
        raise ValueError('registry state requires an aligned tail after native RAW fields')
    contract = native_persistence_contract(baseline_rtos)
    layouts = {}
    for index, row in enumerate(rows):
        style = row['identity']['style_id']
        if not 34 <= style <= 90:
            raise ValueError('custom sidecar style is outside its retained APData namespace')
        start = state_offset + index * 468
        layouts[style] = {'style_id': style, 'main_offset': start, 'mirror_offset': start + 36,
                          'shadow_offset': start + 72, 'record': 16 + style - 34,
                          'bytes': 396, 'snapshot_count': 10}
    symbols, code = _compile_state(patch, rows, layouts,
        address_profile=_StateBindings120(), native_overrides={0xC3F: USERDATA120.mirror_photo_styles_offset},
        original_save_body=contract['save_body'], original_load_body=contract['load_body'])
    return symbols, {'code': code, 'layouts': layouts, 'state_offset': state_offset,
        'state_bytes': len(rows) * 468, 'state_end': state_offset + len(rows) * 468,
        'owner_cell': OWNER_CELL, 'owner_getter': OWNER_GETTER,
        'native_persistence': contract, 'hardware': 'not_tested'}


def install_registry_state120(patch, rows, *, state_offset, baseline_rtos):
    symbols, report = compile_registry_state120(patch, rows,
        state_offset=state_offset, baseline_rtos=baseline_rtos)
    for site, kind, arguments, displaced in HOOKS:
        if patch.word(site) != displaced:
            raise ValueError(f'1.20 registry snapshot contract differs at {site:#x}')
        helper = symbols['registry_slot_' + kind]

        def make(at, site=site, helper=helper, arguments=arguments, displaced=displaced):
            words = [0xE92D500F, 0xE1A00000 | arguments[0], 0xE1A01000 | arguments[1],
                     _branch(at + 12, helper, link=True), 0xE8BD500F]
            if displaced & 0xFF000000 == 0xEA000000:
                words.append(_branch(at + 20, _target(site, displaced)))
            else:
                words.extend((displaced, _branch(at + 24, site + 4)))
            return words

        wrapper = patch.append(make, 16)
        patch.set_word(site, _branch(site, wrapper), '1.20 ten-snapshot custom sidecar ' + kind)
    site = 0x533F6A30
    if patch.word(site) != _branch(site, USERDATA120.native_memcpy, link=True):
        raise ValueError('1.20 registered/saved snapshot copy contract differs')
    wrapper = patch.append(lambda at: [0xE1A03005, _branch(at + 4, symbols['registry_slot_copy'])], 16)
    patch.set_word(site, _branch(site, wrapper, link=True), '1.20 native B44 snapshot copy and custom sidecars')
    for role in ('save', 'load'):
        site = report['native_persistence'][role + '_entry']
        if patch.word(site) != 0xE1A0C00D:
            raise ValueError('1.20 UserMode persistence entry already changed')
        patch.set_word(site, _branch(site, symbols['registry_' + role]), '1.20 current and custom APData ' + role)
    report['startup_restore'] = install_usermode_restore120(patch, symbols['registry_slot_restore'])
    report['symbols'] = symbols
    report['initializer'] = symbols['registry_initialize_owner']
    report['save_wrapper'] = symbols['registry_save']
    report['load_wrapper'] = symbols['registry_load']
    report['snapshot_hook_count'] = len(HOOKS) + 2  # Native copy and new startup path.
    return report
