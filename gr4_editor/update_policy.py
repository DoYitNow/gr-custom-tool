"""Allow older component versions in the known GR IV RTOS CheckVersion gate.

Only the eight comparison branches change. Candidate/current readers, missing
file rejection and the original return path remain. Callers must repair any
editor registry footer and firmware-container checksums after applying this.
This module does not establish peripheral, flash or boot acceptance.
"""
from hashlib import sha256
import struct

BASE = 0x53000000
GATE_ENTRY = 0x5383171C
GATE_BYTES = 0x270
FACTORY_GATE_SHA256 = '664636e5d27cec4aed6cbe08c7dc038785628aede04032dccacbe924d7596954'
COMPARISONS = (
    (0x538317EC, 0x3A000026, 0xE1A00000, 'both', 'cpu'),
    (0x53831800, 0x3A000021, 0xE1A00000, 'both', 'dsp'),
    (0x53831888, 0x2A000033, 0xEA000033, 'full', 'cpu'),
    (0x538318DC, 0x3AFFFFEA, 0xE1A00000, 'dsp', 'dsp'),
    (0x5383190C, 0x3AFFFFDE, 0xE1A00000, 'sric', 'sric'),
    (0x5383194C, 0x3AFFFFCE, 0xE1A00000, 'body', 'cpu'),
    (0x5383196C, 0x3AFFFFC6, 0xE1A00000, 'full', 'dsp'),
    (0x53831980, 0x3AFFFFC1, 0xE1A00000, 'full', 'sric'),
)


def inspect_update_policy(rtos: bytes) -> dict:
    """Recognize a complete factory or allow-older gate; report other bytes."""
    start = GATE_ENTRY - BASE
    gate = bytes(rtos[start:start + GATE_BYTES])
    proof = {
        'schema_version': 1, 'status': 'unsupported', 'policy': 'unknown',
        'installed': None, 'entry': hex(GATE_ENTRY), 'gate_bytes': GATE_BYTES,
        'gate_sha256': sha256(gate).hexdigest(), 'comparison_count': len(COMPARISONS),
        'hardware': 'not_tested',
        'scope': 'Known RTOS version comparisons only; peripheral, flash and boot acceptance unverified',
    }
    if len(gate) != GATE_BYTES:
        return {**proof, 'reason': '固件缺少完整的版本比较函数'}
    normalized = bytearray(gate)
    states, patches = [], []
    for address, original, relaxed, path, component in COMPARISONS:
        offset = address - GATE_ENTRY
        word = struct.unpack_from('<I', gate, offset)[0]
        if word not in (original, relaxed):
            return {**proof, 'reason': f'版本比较指令无法识别: {address:#x}'}
        states.append(word == relaxed)
        struct.pack_into('<I', normalized, offset, original)
        patches.append({'address': hex(address), 'path': path, 'component': component,
                        'factory_word': hex(original), 'policy_word': hex(relaxed),
                        'effective_word': hex(word)})
    normalized_sha = sha256(normalized).hexdigest()
    if normalized_sha != FACTORY_GATE_SHA256:
        return {**proof, 'normalized_gate_sha256': normalized_sha,
                'reason': '版本比较函数与已验证的原厂指令不一致'}
    if any(states) and not all(states):
        return {**proof, 'normalized_gate_sha256': normalized_sha,
                'reason': '版本比较函数只有部分低版本判定被修改'}
    installed = all(states)
    return {**proof, 'status': 'verified',
            'policy': 'allow_older' if installed else 'factory', 'installed': installed,
            'normalized_gate_sha256': normalized_sha, 'patches': patches,
            'validation': 'byte_exact_known_gate', 'modified_instruction_bytes': 32 if installed else 0}


def apply_update_policy(rtos: bytes) -> tuple[bytes, dict]:
    """Patch the known gate, or retain an already verified allow-older gate."""
    before = inspect_update_policy(rtos)
    if before['status'] != 'verified':
        raise ValueError(before['reason'])
    result = bytearray(rtos)
    for address, _original, relaxed, _path, _component in COMPARISONS:
        struct.pack_into('<I', result, address - BASE, relaxed)
    output = bytes(result)
    proof = inspect_update_policy(output)
    return output, {**proof, 'source_policy': before['policy'],
                    'source_gate_sha256': before['gate_sha256'],
                    'changed_instruction_count': 0 if before['installed'] else len(COMPARISONS)}


def remove_update_policy(rtos: bytes) -> tuple[bytes, dict]:
    """Restore the reviewed factory version-comparison branches.

    The editor's compiler starts from a factory canonical RTOS, but direct
    repack callers may supply an already patched image.  Keeping this inverse
    beside ``apply_update_policy`` makes the UI switch deterministic for both
    cases and still rejects an unrecognised/partially patched gate.
    """
    before = inspect_update_policy(rtos)
    if before['status'] != 'verified':
        raise ValueError(before['reason'])
    result = bytearray(rtos)
    for address, original, _relaxed, _path, _component in COMPARISONS:
        struct.pack_into('<I', result, address - BASE, original)
    output = bytes(result)
    proof = inspect_update_policy(output)
    return output, {**proof, 'source_policy': before['policy'],
                    'source_gate_sha256': before['gate_sha256'],
                    'changed_instruction_count': len(COMPARISONS) if before['installed'] else 0}
