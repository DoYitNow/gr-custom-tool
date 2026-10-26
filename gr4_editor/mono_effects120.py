"""Add the existing gray matrix path to color-camera native styles31/32.

The original complete .13 producer runs first, including its custom-filter
profile bridge. Only these two styles on a non-Mono model set output+0x79.
Native Mono models, all other styles, Solarized types and their resource
descriptors keep their original paths. There is no owner or global state.
"""
from hashlib import sha256
import struct

from .slots import BASE, _branch


BASELINE_SHA256 = '80c542a83528d5f5bd72b81fd2249f9fe98582f026f112835f13b92fe80c39e0'
PRODUCER = 0x5393A9E0
PRODUCER_BYTES = 0x890
MONO_PREDICATE = 0x53400DF4
MONO_PREDICATE_BYTES = 0x28
CALLERS = (0x5393B4B4, 0x5393B88C, 0x5393BE74)
ENTRY_WORD = 0xE1A0C00D


def _wrapper(at):
    words = [
        0xE92D4070,  # push {r4,r5,r6,lr}; 8-byte aligned, per-call storage
        0xE10F6000,  # mrs r6,cpsr
        0xE281C902,  # add ip,r1,#0x8000
        0xE5DC42AC,  # ldrb r4,[ip,#0x2ac] native still/movie/RAW context mode
        0xE59CC2D0,  # ldr ip,[ip,#0x2d0] native SourceImage base
        0xE3540000,  # cmp r4,#0
        0x128CCA71,  # addne ip,ip,#0x71000
        0x128CCD0A,  # addne ip,ip,#0x280
        0xE5DC579C,  # ldrb r5,[ip,#0x79c] capture the frame's native style
        0xE1A04002,  # mov r4,r2; retain this invocation's output object
        0xE128F006,  # msr cpsr_f,r6; original incoming NZCVQ
        ENTRY_WORD,  # mov ip,sp; replay the displaced native prologue
        _branch(at+48, PRODUCER+4, link=True),
        0xE10F6000,  # mrs r6,cpsr; preserve native return NZCVQ
        0xE92D500F,  # push {r0-r3,ip,lr}; original volatile return registers
        0xE355001F,  # cmp r5,#31
        0x13550020,  # cmpne r5,#32
        _branch(at+68, at+92, condition=1),  # other styles return unchanged
        _branch(at+72, MONO_PREDICATE, link=True),  # actual read-only model test
        0xE3500000,  # cmp r0,#0
        0x03A00001,  # moveq r0,#1; genuine Mono models keep their native output
        0x05C40079,  # strbeq r0,[r4,#0x79]
        0xE1A00000,  # nop; all exits join before native flags/register restore
        0xE128F006,  # msr cpsr_f,r6
        0xE8BD500F,  # pop {r0-r3,ip,lr}
        0xE8BD4070,  # pop {r4,r5,r6,lr}
        0xE12FFF1E,  # bx lr
    ]
    return words


def install_mono_effects120(patch, *, baseline_rtos):
    """Wrap the exact .13 producer after the caller has removed its footer.

    baseline_rtos is the complete immutable .13 RTOS used for verification.
    Only one original entry word changes; the producer body, its three callers,
    native Mono predicate and every profile/gamma resource stay byte-identical.
    """
    if sha256(baseline_rtos).hexdigest() != BASELINE_SHA256:
        raise ValueError('gray bridge requires the exact delivered1.20.10.13 RTOS')
    spans = ((PRODUCER, PRODUCER_BYTES), (MONO_PREDICATE, MONO_PREDICATE_BYTES))
    for address, length in spans:
        offset = address-BASE
        if bytes(patch.image[offset:offset+length]) != baseline_rtos[offset:offset+length]:
            raise ValueError(f'native .13 gray-bridge contract differs at {address:#x}')
    if patch.word(PRODUCER) != ENTRY_WORD:
        raise ValueError('gray producer has no verified MOV ip,sp entry')
    for caller in CALLERS:
        if patch.word(caller) != _branch(caller, PRODUCER, link=True):
            raise ValueError('gray producer caller differs from the native .13 chain')
    origin = patch.append(_wrapper, 16)
    blob = bytes(patch.image[origin-BASE:origin-BASE+len(_wrapper(origin))*4])
    if origin+len(blob) >= 0x55000000:
        raise ValueError('gray bridge is outside the executable ROM mapping')
    after = _branch(PRODUCER, origin)
    patch.set_word(PRODUCER, after, 'native31/32 color-only gray matrix after original .13 producer')
    offset = PRODUCER+4-BASE
    return {'schema_version': 120, 'installed': True, 'baseline_rtos_sha256': BASELINE_SHA256,
        'hook': {'address': hex(PRODUCER), 'before': hex(ENTRY_WORD), 'after': hex(after)},
        'append_ranges': [{'address': hex(origin), 'bytes': len(blob), 'sha256': sha256(blob).hexdigest()}],
        'entry': hex(origin), 'bytes': len(blob), 'sha256': sha256(blob).hexdigest(),
        'original_producer_body': {'address': hex(PRODUCER+4), 'bytes': PRODUCER_BYTES-4,
            'sha256': sha256(baseline_rtos[offset:offset+PRODUCER_BYTES-4]).hexdigest()},
        'mono_predicate': {'address': hex(MONO_PREDICATE), 'bytes': MONO_PREDICATE_BYTES,
            'sha256': sha256(baseline_rtos[MONO_PREDICATE-BASE:MONO_PREDICATE-BASE+MONO_PREDICATE_BYTES]).hexdigest()},
        'native_callers': [{'address': hex(at), 'word': hex(_branch(at, PRODUCER, link=True))} for at in CALLERS],
        'styles': [31, 32], 'output_offset': '0x79', 'output_value': 1,
        'native_mono_models_unchanged': True, 'global_mono_unlock': False,
        'owner_bytes': 0, 'mutable_global_bytes': 0,
        'abi': 'Native input/return NZCVQ, r0-r3/ip results, r4-r11, caller LR and SP retained; 8-byte call alignment',
        'scope': 'Shared native producer used by photo/movie/RAW; original profiles34/35/36/37 and .13 custom dispatch retained',
        'hardware': 'not_tested'}


def inspect_mono_effects120(rtos, report):
    """Inspect the final assembled bytes; no installer or emulation substitutes."""
    try:
        hook = report['hook']
        site, expected = int(hook['address'], 16), int(hook['after'], 16)
        if struct.unpack_from('<I', rtos, site-BASE)[0] != expected:
            raise ValueError('gray producer entry differs')
        if expected != _branch(site, int(report['entry'], 16)):
            raise ValueError('gray entry no longer selects the recorded wrapper')
        for item in (*report['append_ranges'], report['original_producer_body'], report['mono_predicate']):
            offset = int(item['address'], 16)-BASE
            if sha256(rtos[offset:offset+item['bytes']]).hexdigest() != item['sha256']:
                raise ValueError('gray wrapper or preserved native body differs')
        for item in report['native_callers']:
            if struct.unpack_from('<I', rtos, int(item['address'], 16)-BASE)[0] != int(item['word'], 16):
                raise ValueError('native photo/movie/RAW caller differs')
        return {'status': 'verified_static', 'native_callers_verified': len(CALLERS),
                'original_producer_body_and_mono_predicate': 'byte_identical', 'hardware': 'not_tested'}
    except (KeyError, ValueError, TypeError, struct.error) as error:
        return {'status': 'invalid', 'reason': str(error), 'hardware': 'not_tested'}
