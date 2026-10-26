"""Close the custom parameter sidecar on 1.20's new startup USER restore path.

This adds one call before the original native restore; it does not change the
official setting, its branch polarity, UserData schema or the original apply.
"""
from hashlib import sha256
import struct

from .slots import BASE, _branch

SAVE_ENTRY = 0x533F3674
LOAD_ENTRY = 0x533F3254
STARTUP_ENTRY = 0x533F6EA0
STARTUP_RESTORE_CALL = 0x533F7020
ORIGINAL_RESTORE = 0x533F669C
SNAPSHOT_BYTES = 0xB44


def _restore_wrapper_words(address, restore_helper):
    return [0xE92D500F, _branch(address+4, restore_helper, link=True),
            0xE8BD500F, _branch(address+12, ORIGINAL_RESTORE)]


def native_persistence_contract(rtos):
    """Identify full native methods, before the caller chains their entry hooks."""
    for start, length, digest in (
        (LOAD_ENTRY, 0x3E8, '2a799635ad04a7feb465f19582b8adec6d29dfa1199aa68df6468ad72c175463'),
        (SAVE_ENTRY, 0x218, 'ad21cfc4bbcf8bd610afaf501fe1114ffa353e436f24e23b88d889657cf965c3'),
    ):
        if sha256(rtos[start-BASE:start-BASE+length]).hexdigest() != digest:
            raise ValueError(f'1.20 native UserMode persistence method differs at {start:#x}')
    return {
        'save_entry': SAVE_ENTRY, 'save_body': SAVE_ENTRY+4,
        'load_entry': LOAD_ENTRY, 'load_body': LOAD_ENTRY+4,
        'save_abi': 'r0=UserMode object; +4 four registered pointers; +0x14 six saved pointers; +0x2C ten record IDs; +0x3C provider',
        'load_abi': 'r0=UserMode; r1=bare current destination; r2=four registered pointer outputs; r3=six saved pointer outputs',
        'displaced_instruction': 'mov ip,sp; a +4 continuation requires a callable bridge that recreates this instruction',
        'snapshot_bytes': SNAPSHOT_BYTES, 'snapshot_count': 10,
        'original_current_record': 4, 'original_registered_records': [5, 6, 7, 8],
        'original_saved_records': [9, 10, 11, 12, 13, 14],
        'hardware': 'not_tested',
    }


def install_usermode_restore120(patch, restore_helper):
    """restore_helper(mode, selected_bare_snapshot) must obey ARM AAPCS.

    Native startup first calls 533F363C -> LOAD_ENTRY, so a chained registry_load
    has loaded sidecars before this call executes. Raw current byte+0xAEB ==0
    and UserMode index!=0 select this path; the other path keeps current values.
    """
    body = bytes(patch.image[STARTUP_ENTRY-BASE:0x533F7044-BASE])
    if sha256(body).hexdigest() != 'd4b48fcb9270502af6df01bf5671b3ac31eab30ce0465c3244c5b0b12a358732':
        raise ValueError('1.20 startup USER restore control flow differs')
    if patch.word(STARTUP_RESTORE_CALL) != _branch(STARTUP_RESTORE_CALL, ORIGINAL_RESTORE, link=True):
        raise ValueError('1.20 startup USER restore call is not original')
    # r0/r1/r2 still contain mode/selected snapshot/loaded current at the call.
    # Save the complete argument set and ip/LR across the sidecar helper, then
    # tail-call the original method so its result and return address are intact.
    wrapper = patch.append(lambda at: _restore_wrapper_words(at, restore_helper), 16)
    patch.set_word(STARTUP_RESTORE_CALL, _branch(STARTUP_RESTORE_CALL, wrapper, link=True),
                   '1.20 startup USER restore synchronizes loaded custom sidecars before native apply')
    return {'installed': True, 'entry': hex(STARTUP_ENTRY),
            'callsite': hex(STARTUP_RESTORE_CALL), 'wrapper': hex(wrapper),
            'restore_helper': hex(restore_helper), 'original_restore': hex(ORIGINAL_RESTORE),
            'load_precedes_restore': ['0x533F6F88', '0x533F363C', '0x533F3254'],
            'snapshot_flag_offset': '0xAEB', 'runtime_flag_offset': '0xAEF',
            'raw_flag0': 'restore selected registered snapshot when USER index is nonzero',
            'raw_flag1': 'keep loaded current settings',
            'abi': 'preserve r0-r3/ip/LR and8-byte stack alignment; original return value preserved',
            'hardware': 'not_tested'}


def inspect_usermode_restore120(rtos, report):
    try:
        wrapper = int(report['wrapper'], 16)
        helper = int(report['restore_helper'], 16)
        raw = rtos[wrapper-BASE:wrapper-BASE+16]
        if raw != struct.pack('<4I', *_restore_wrapper_words(wrapper, helper)):
            raise ValueError('1.20 USER startup argument-preserving wrapper differs')
        actual, = struct.unpack_from('<I', rtos, STARTUP_RESTORE_CALL-BASE)
        if actual != _branch(STARTUP_RESTORE_CALL, wrapper, link=True):
            raise ValueError('1.20 USER startup call does not use its sidecar wrapper')
        body = bytearray(rtos[STARTUP_ENTRY-BASE:0x533F7044-BASE])
        struct.pack_into('<I', body, STARTUP_RESTORE_CALL-STARTUP_ENTRY,
                         _branch(STARTUP_RESTORE_CALL, ORIGINAL_RESTORE, link=True))
        if sha256(body).hexdigest() != 'd4b48fcb9270502af6df01bf5671b3ac31eab30ce0465c3244c5b0b12a358732':
            raise ValueError('1.20 original USER restore control flow changed outside its one call')
        return {'status': 'verified_static', 'native_control_flow_preserved': True,
                'added_calls': 1, 'hardware': 'not_tested'}
    except (KeyError, TypeError, ValueError, struct.error) as error:
        return {'status': 'invalid', 'reason': str(error), 'hardware': 'not_tested'}
