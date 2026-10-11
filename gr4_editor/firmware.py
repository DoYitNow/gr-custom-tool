# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Read and rebuild the local firmware container without accessing a camera.

Binary identity is SHA256, not the camera's public ``1.11`` label.  Native crop and picture resources are read from user input.
Camera UserData values are never inferred from firmware bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import struct
from typing import Any

from .codec.frames import (
    FrameError, _decode_body, _literal_chunks, decode_payload,
    framed_container, section_rows, _aligned_target,
)
from .codec.container import inspect_container, repair_last_word, sum32
from .update_policy import inspect_update_policy

BASE = 0x53000000
VERSION_RUNTIME = 0x53FE50F8
VERSION_FALLBACK_MOVW = 0x53A78498
VERSION_FALLBACK_MOV = 0x53A784BC


class FirmwareError(ValueError):
    """An import/rebuild cannot be proved valid by this offline adapter."""


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _version(word: int) -> tuple[int, int, int, int]:
    return tuple(word.to_bytes(4, 'big'))


def _version_text(version) -> str:
    return '.'.join(map(str, version))


def _version_word(value) -> int:
    if isinstance(value, int):
        if not 0 <= value <= 0xFFFFFFFF:
            raise FirmwareError('version is outside the four-byte field')
        return value
    if isinstance(value, str):
        value = value.split('.')
    try:
        parts = tuple(int(x) for x in value)
    except (TypeError, ValueError) as exc:
        raise FirmwareError('version must contain four byte-sized numbers') from exc
    if len(parts) != 4 or any(not 0 <= x <= 255 for x in parts):
        raise FirmwareError('version must contain four byte-sized numbers')
    return int.from_bytes(bytes(parts), 'big')


def _half(word: int) -> int:
    return ((word >> 4) & 0xF000) | (word & 0xFFF)


def _history_identity(digest: str) -> dict | None:
    """Identify the known official input without a private research archive."""
    if digest == 'a2f664dfca034059eb0fd6e18ab08684c326b4a034d85c164dad7e1ec9b5655f':
        return {'kind': 'official', 'version': '1.11.10.7'}
    return None


@dataclass
class FirmwareImage:
    path: Path | None
    candidate: bytes
    decoded: bytes
    rtos: bytes
    icon_bytes: bytes
    sections: list[dict]
    container: dict
    version: tuple[int, int, int, int]
    sha256: str
    history: dict | None
    editable: bool
    layout_proof: dict

    @property
    def known_stage_id(self) -> str | None:
        return self.history.get('stage_id') if self.history else None

    @property
    def internal_version(self) -> str:
        return _version_text(self.version)

    @property
    def editability(self) -> dict:
        if self.editable:
            if self.layout_proof.get('generic_passthrough'):
                reason = 'Verified firmware container; feature layout is not registered, so generation preserves imported resources'
            elif self.layout_proof.get('editor_registry_verified'):
                reason = 'Verified demo metadata and firmware container'
            else:
                reason = 'Verified backend RTOS and supported layout'
        elif self.history and self.history.get('kind') == 'official':
            reason = 'Official firmware: inspection only'
        else:
            reason = 'Inspection only: input is not the supported official file or a demo candidate'
        return {'editable': self.editable, 'reason': reason}


class _Memory:
    """Static ROM plus the proven startup initialized-data copy; no BSS."""
    def __init__(self, rtos: bytes):
        self.data = rtos
        self.source, self.destination, self.length = struct.unpack('<3I', self.read(BASE + 0x450, 12))
        if (self.source, self.destination, self.length) != (0x5437B640, 0x55000000, 0x57480):
            raise FirmwareError('unknown startup data layout')

    def read(self, address: int, size: int) -> bytes:
        if size < 0:
            raise FirmwareError('negative resource length')
        if 0x55000000 <= address and hasattr(self, 'destination'):
            if not self.destination <= address <= address + size <= self.destination + self.length:
                raise FirmwareError(f'resource requires non-static RAM {address:#x}')
            address = self.source + address - self.destination
        offset = address - BASE
        if not 0 <= offset <= offset + size <= len(self.data):
            raise FirmwareError(f'resource is outside RTOS {address:#x}+{size:#x}')
        return self.data[offset:offset + size]

    def word(self, address: int) -> int:
        return struct.unpack('<I', self.read(address, 4))[0]

    def pair(self, low: int, high: int) -> int:
        lo, hi = self.word(low), self.word(high)
        if lo & 0x0FF00000 != 0x03000000 or hi & 0x0FF00000 != 0x03400000:
            raise FirmwareError('table address does not use MOVW/MOVT')
        if (lo >> 12) & 15 != (hi >> 12) & 15:
            raise FirmwareError('table address registers differ')
        return _half(lo) | _half(hi) << 16


def _component_versions(decoded: bytes, sections: list[dict], memory: _Memory) -> dict:
    by_name = {row['name']: row for row in sections}
    cpu, sric = by_name['CPU'], by_name['SRIC']
    dsp = decoded[cpu['start'] + 16:cpu['end']]
    sric_bytes = decoded[sric['start'] + 16:sric['end']]
    values = {
        'runtime_cpu': memory.word(VERSION_RUNTIME),
        'dsp': struct.unpack_from('>I', dsp, 0x90)[0],
        'sric': struct.unpack_from('<I', sric_bytes, 0x83D4)[0],
    }
    movw, mov = memory.word(VERSION_FALLBACK_MOVW), memory.word(VERSION_FALLBACK_MOV)
    if movw & 0xFFFFFF00 != 0xE3000A00 or mov & 0xFFFFFF00 != 0xE3A00000:
        raise FirmwareError('unknown CPU fallback version instructions')
    values['fallback_cpu'] = 0x010B0000 | _half(movw)
    values['fallback_build'] = mov & 255
    if sum32(sric_bytes) != 0:
        raise FirmwareError('SRIC checksum is invalid')
    if sum(x[0] for x in struct.iter_unpack('>I', dsp)) & 0xFFFFFFFF:
        raise FirmwareError('DSP checksum is invalid')
    return values


def _load_bytes(candidate: bytes, path: Path | None = None) -> FirmwareImage:
    try:
        info = inspect_container(candidate)
        if not (
            info['additive_sum32'] == 0 and info['project'] == 636 and
            info['header_project_copy'] == info['footer_project_copy'] == 0x027C027C and
            info['header_marker'] == info['footer_marker'] == 0xA55A5AA5 and
            info['firmware_type'] == 2 and info['header_record_count'] == 3 and
            info['metadata_length'] == 384
        ):
            raise FirmwareError('unsupported or damaged firmware container header/checksum')
        decoded, frames = decode_payload(candidate[128:info['payload_end']], info['decoded_size'])
        sections = section_rows(decoded)
        names = [row['name'] for row in sections]
        expected = ['BOOTPARA', 'DRAMPARA', 'PARTPARA', 'M0', 'BOOT', 'RTOS', 'LINUX',
                    'INITFS', 'SRIC', 'CPU', 'SHELLCMD', 'ICONBIN', 'RES']
        if names != expected:
            raise FirmwareError('unsupported section order')
        by_name = {row['name']: row for row in sections}
        rtos = decoded[by_name['RTOS']['start'] + 16:by_name['RTOS']['end']]
        icon_bytes = decoded[by_name['ICONBIN']['start'] + 16:by_name['ICONBIN']['end']]
        memory = _Memory(rtos)
        values = _component_versions(decoded, sections, memory)
        outer = {
            'outer_cpu': struct.unpack_from('>I', candidate, 0x38)[0],
            'outer_dsp': struct.unpack_from('>I', candidate, 0x50)[0],
            'outer_sric': struct.unpack_from('<I', candidate, 0x4C)[0],
        }
        runtime = values['runtime_cpu']
        for key, value in {**values, **outer}.items():
            if key == 'fallback_build':
                consistent = value == (runtime & 255)
            else:
                consistent = value == runtime
            if not consistent:
                raise FirmwareError(f'inconsistent component version: {key}')
        metadata_versions = []
        for i in range(3):
            offset = info['metadata_start'] + i * 128
            project = struct.unpack_from('<I', candidate, offset)[0]
            word = struct.unpack_from('>I', candidate, offset + 4)[0]
            metadata_versions.append({'project': project, 'version': _version_text(_version(word))})
            if project in (636, 650) and word != runtime:
                raise FirmwareError('component metadata version differs from runtime')
        version = _version(runtime)
        if version[:3] != (1, 11, 10):
            raise FirmwareError('unsupported internal version family')
        digest = _sha(candidate)
        identity = _history_identity(digest)
        # Importing a shared editor output must not require this computer's
        # local build ledger. The backend verifies its RTOS body/footer; the
        # container/section/version checks above still apply to the new input.
        # Keep these imports lazy.  The public demo backend owns crop/shutdown
        # writes, while the research registry backend owns native filter
        # writes.  Either verified backend may mark the image as a feature
        # input; an unknown but structurally valid container remains generic
        # passthrough and is never sent to a native writer.
        backend_verified, registry_metadata = False, None
        try:
            from . import demo_build
            if demo_build.supports_image(rtos):
                backend_verified = True
                registry_metadata = demo_build._metadata(rtos)
        except (ImportError, AttributeError, ValueError, TypeError, struct.error):
            pass
        try:
            from . import slots
            if slots.supports_image(rtos):
                backend_verified = True
                registry_metadata = slots._metadata(rtos)
        except (ImportError, AttributeError, ValueError, TypeError, struct.error):
            pass
        # A structurally valid GR4 container is always importable and can be
        # re-packed while preserving its unregistered resources.  The feature
        # compiler still uses ``backend_source_verified`` below to decide
        # whether crop/end-screen writes are available.  Keeping those two
        # capabilities separate avoids turning an unknown layout into a
        # falsely verified native patch target.
        editable = True
        proof = {
            'container_checksum_valid': True, 'decoded_checksum_valid': True,
            'component_checksums_valid': True, 'component_versions_consistent': True,
            'startup_data_copy_valid': True, 'section_order_valid': True,
            'strict_stream_decode_valid': True, 'frames': len(frames),
            'versions': {k: _version_text(_version(v)) for k, v in {**values, **outer}.items() if k != 'fallback_build'},
            'fallback_build': values['fallback_build'], 'metadata_versions': metadata_versions,
            'backend_source_verified': backend_verified,
            'editor_registry_verified': registry_metadata is not None,
            'editor_registry_schema_version': registry_metadata.get('schema_version') if registry_metadata else None,
            'compiler_base_rtos_sha256': ((registry_metadata.get('compiler_base_sha256', registry_metadata.get('base_rtos_sha256'))
                                          if registry_metadata else _sha(rtos)) if backend_verified else None),
            'hardware_flash_verified': False,
            'generic_passthrough': not backend_verified,
            'update_policy': inspect_update_policy(rtos),
        }
        return FirmwareImage(path, candidate, decoded, rtos, icon_bytes, sections, info,
                             version, digest, identity, editable, proof)
    except FirmwareError:
        raise
    except (ValueError, IndexError, KeyError, struct.error, UnicodeError) as exc:
        raise FirmwareError(f'firmware parse failed: {exc}') from exc


def load_image(path: str | Path) -> FirmwareImage:
    """Import and strictly decode a complete local ``fwdc248b.bin`` file."""
    source = Path(path).expanduser().resolve()
    return _load_bytes(source.read_bytes(), source)


def inspect_firmware(path: str | Path | FirmwareImage) -> dict:
    image = path if isinstance(path, FirmwareImage) else load_image(path)
    return {
        'path': str(image.path), 'sha256': image.sha256,
        'bytes': len(image.candidate), 'decoded_sha256': _sha(image.decoded),
        'rtos_sha256': _sha(image.rtos), 'internal_version': image.internal_version,
        'version': list(image.version), 'public_version': '1.11',
        'history': image.history, 'known_stage_id': image.known_stage_id,
        'editable': image.editable, 'editability': image.editability,
        'sections': image.sections, 'layout_proof': image.layout_proof,
        'update_policy': inspect_update_policy(image.rtos),
        'camera_values_available': False,
    }


def _immediate(word: int) -> int:
    value, rotate = word & 255, ((word >> 8) & 15) * 2
    return ((value >> rotate) | (value << (32 - rotate))) & 0xFFFFFFFF if rotate else value










def _name_resource(memory: _Memory, text_id: int) -> tuple[str, dict]:
    catalog = memory.pair(0x53573500, 0x53573504)
    row = memory.word(catalog + 4)  # English language row, not live UI language
    record = memory.word(row + text_id * 4)
    metadata = memory.read(record, 8)
    if metadata[1] != 1:
        raise FirmwareError('text record is not the known Latin UTF-16 format')
    address = struct.unpack_from('<I', metadata, 4)[0]
    raw = bytearray()
    for i in range(128):
        unit = memory.read(address + i * 2, 2)
        raw.extend(unit)
        if unit == b'\0\0':
            return bytes(raw[:-2]).decode('utf-16-le'), {
                'language': 'English', 'record_address': record, 'text_address': address,
                'record_hex': metadata.hex(), 'hex': bytes(raw).hex(),
                'sha256': _sha(bytes(raw)),
            }
    raise FirmwareError('native text has no bounded terminator')


def _icon_resource(memory: _Memory, image: FirmwareImage, icon_id: int) -> dict:
    catalog = memory.pair(0x533EF604, 0x533EF608)
    descriptor = memory.word(catalog + icon_id * 4)
    raw = memory.read(descriptor, 12)
    kind, width, height, flags, words = struct.unpack('<HHHHI', raw)
    if kind != 1 or not width or not height:
        raise FirmwareError('unsupported icon encoding')
    offset, size = words * 4, width * height * 4
    if not 0 <= offset < offset + size <= len(image.icon_bytes):
        raise FirmwareError('native icon is outside ICONBIN')
    pixels = image.icon_bytes[offset:offset + size]
    return {'descriptor_address': descriptor, 'descriptor_hex': raw.hex(),
            'offset': offset, 'bytes': size, 'width': width, 'height': height,
            'flags': flags, 'pixel_format': 'RGBA8', 'hex': pixels.hex(), 'sha256': _sha(pixels)}






def _rebuild_decoded(image: FirmwareImage, replacements: dict[str, bytes]) -> bytes:
    result = bytearray()
    for row in image.sections:
        if row['name'] == 'RES':
            replacement = replacements.get('RES', image.decoded[row['start']:row['end']])
            if len(replacement) % 4 or len(replacement) < row['end'] - row['start'] or replacement[:8] != b'RES\0\0\0\0\0':
                raise FirmwareError('RES replacement must be a complete aligned non-shrinking section')
            result.extend(replacement)
            break
        payload = replacements.get(row['name'], image.decoded[row['start'] + 16:row['end']])
        if len(payload) % 4:
            raise FirmwareError(f'{row["name"]} payload must be word aligned')
        if len(payload) < row['payload_bytes']:
            raise FirmwareError(f'{row["name"]} shrink is not supported')
        header = bytearray(image.decoded[row['start']:row['start'] + 16])
        struct.pack_into('<I', header, 12, len(payload))
        result.extend(header)
        result.extend(payload)
    result.extend(bytes(4))
    return repair_last_word(result)


def _patch_rtos_version(rtos: bytes, old: int, new: int) -> bytes:
    out = bytearray(rtos)
    for address, before, after in (
        (VERSION_RUNTIME, struct.pack('<I', old), struct.pack('<I', new)),
        (VERSION_FALLBACK_MOVW, struct.pack('<I', 0xE3000A00 | (old & 255)),
         struct.pack('<I', 0xE3000A00 | (new & 255))),
        (VERSION_FALLBACK_MOV, struct.pack('<I', 0xE3A00000 | (old & 255)),
         struct.pack('<I', 0xE3A00000 | (new & 255))),
    ):
        offset = address - BASE
        if bytes(out[offset:offset + 4]) not in (before, after):
            raise FirmwareError(f'caller changed a version instruction unexpectedly: {address:#x}')
        out[offset:offset + 4] = after
    return bytes(out)


def _aligned_resource_target(original, target):
    """Retain frame coordinates while allowing explicit RES record changes."""
    old_rows, new_rows = section_rows(original), section_rows(target)
    if [row['name'] for row in old_rows] != [row['name'] for row in new_rows]:
        raise FirmwareError('resource rebuild changed section order')
    aligned, insertions, changes = bytearray(), {}, []
    for old, new in zip(old_rows, new_rows):
        if old['flags'] != new['flags']:
            raise FirmwareError('resource rebuild changed section flags')
        old_length, new_length = old['end'] - old['start'], new['end'] - new['start']
        if new_length < old_length:
            raise FirmwareError('resource rebuild cannot shrink a section')
        if old['name'] not in ('RTOS', 'ICONBIN', 'RES') and original[old['start']:old['end']] != target[new['start']:new['end']]:
            raise FirmwareError('unexpected resource rebuild modification: ' + old['name'])
        aligned.extend(target[new['start']:new['start'] + old_length])
        if new_length > old_length:
            insertions[old['end']] = target[new['start'] + old_length:new['end']]
            changes.append({'section': old['name'], 'original_boundary': old['end'], 'inserted_bytes': new_length - old_length})
    aligned.extend(target[-4:])
    if len(aligned) != len(original):
        raise FirmwareError('resource rebuild coordinate mapping differs')
    return bytes(aligned), insertions, changes


def _repack_frames(candidate: bytes, original: bytes, target: bytes, growth: bool, resource_changes=False) -> tuple[bytes, dict]:
    """Replay frames against new history; an invalid replay becomes literals."""
    info = inspect_container(candidate)
    source_payload = candidate[128:info['payload_end']]
    recovered, frames = decode_payload(source_payload, len(original))
    if recovered != original:
        raise FirmwareError('repack source stream differs from imported decoded bytes')
    if growth:
        aligned, insertions, changes = (_aligned_resource_target(original, target) if resource_changes else
                                       _aligned_target(original, target))
    else:
        if len(original) != len(target):
            raise FirmwareError('equal-layout target size differs')
        aligned, insertions, changes = target, {}, []
    pending = sorted(insertions)
    encoded, output = bytearray(), bytearray()
    reused = generated = replaced = 0
    for frame in frames:
        pieces, cursor = [], frame.decoded_start
        while pending and pending[0] < frame.decoded_end:
            boundary = pending.pop(0)
            if boundary < cursor:
                raise FirmwareError('invalid section insertion boundary')
            pieces.extend((aligned[cursor:boundary], insertions[boundary]))
            cursor = boundary
        pieces.append(aligned[cursor:frame.decoded_end])
        desired = b''.join(pieces)
        start = len(output)
        raw = source_payload[frame.encoded_start:frame.encoded_end]
        exact = False
        if len(desired) == frame.decoded_end - frame.decoded_start:
            try:
                _decode_body(raw[2:], frame.literal, output, start + len(desired))
                exact = output[start:] == desired
            except FrameError:
                pass
        if exact:
            encoded.extend(raw)
            reused += 1
        else:
            del output[start:]
            literal, count = _literal_chunks(desired)
            encoded.extend(literal)
            output.extend(desired)
            generated += count
            replaced += 1
    if pending:
        raise FirmwareError('unconsumed section insertion')
    encoded.extend(b'\0\0')
    if output != target:
        raise FirmwareError('repack output differs from intended target')
    independent, new_frames = decode_payload(encoded, len(target))
    if independent != target:
        raise FirmwareError('fresh-history decode differs from intended target')
    return bytes(encoded), {'source_frames': len(frames), 'reused_frames': reused,
                            'replaced_frames': replaced, 'generated_literal_frames': generated,
                            'result_frames': len(new_frames), 'section_insertions': changes,
                            'fresh_history_roundtrip_exact': True, 'hardware_decoder_verified': False}


def _outer_version(candidate: bytes, new: int) -> bytes:
    out = bytearray(candidate)
    for offset, endian in ((0x38, '>I'), (0x50, '>I'), (0x4C, '<I')):
        struct.pack_into(endian, out, offset, new)
    info = inspect_container(out)
    for i in range(info['header_record_count']):
        offset = info['metadata_start'] + i * 128
        if struct.unpack_from('<I', out, offset)[0] in (636, 650):
            struct.pack_into('>I', out, offset + 4, new)
    return repair_last_word(out)


def repack_image(image: FirmwareImage, new_rtos: bytes, new_icon_bytes: bytes | None = None,
                 new_version=None, new_res_section: bytes | None = None,
                 allow_older=True) -> tuple[bytes, dict]:
    """Return candidate bytes/report; never write a file, camera or card.

    ``new_icon_bytes`` is the full ICONBIN payload, including existing pixels.
    Version changes are rebuilt first without growth so the growth codec's
    invariant that all other section bytes remain unchanged stays true.
    """
    if not image.editable:
        raise FirmwareError(image.editability['reason'])
    if _sha(image.candidate) != image.sha256:
        raise FirmwareError('FirmwareImage source bytes changed after import')
    old = int.from_bytes(bytes(image.version), 'big')
    new = old if new_version is None else _version_word(new_version)
    if _version(new)[:3] != (1, 11, 10):
        raise FirmwareError('this adapter only rebuilds the 1.11.10.x family')
    if new < old:
        raise FirmwareError('output version cannot be lower than imported firmware')
    if type(allow_older) is not bool:
        raise ValueError('低版本固件支持开关必须是布尔值')
    from .demo_build import prepare_update_policy
    source_policy = inspect_update_policy(image.rtos)
    policy_rtos, policy_installation = prepare_update_policy(new_rtos, allow_older=allow_older)
    target_rtos = _patch_rtos_version(policy_rtos, old, new)
    if len(target_rtos) < len(image.rtos) or len(target_rtos) % 4:
        raise FirmwareError('RTOS replacement must be word aligned and cannot shrink')
    icons = image.icon_bytes if new_icon_bytes is None else bytes(new_icon_bytes)
    if len(icons) < len(image.icon_bytes) or len(icons) % 4:
        raise FirmwareError('ICONBIN replacement must be word aligned and cannot shrink')
    transient = image
    version_report = None
    if old != new:
        sections = {row['name']: row for row in image.sections}
        sric_row, dsp_row = sections['SRIC'], sections['CPU']
        sric = bytearray(image.decoded[sric_row['start'] + 16:sric_row['end']])
        dsp = bytearray(image.decoded[dsp_row['start'] + 16:dsp_row['end']])
        struct.pack_into('<I', sric, 0x83D4, new)
        sric = repair_last_word(sric)
        struct.pack_into('>I', dsp, 0x90, new)
        struct.pack_into('>I', dsp, 0x7C, 0)
        struct.pack_into('>I', dsp, 0x7C, (-sum(x[0] for x in struct.iter_unpack('>I', dsp))) & 0xFFFFFFFF)
        changed = _rebuild_decoded(image, {
            'RTOS': _patch_rtos_version(image.rtos, old, new), 'SRIC': sric, 'CPU': bytes(dsp),
        })
        payload, version_report = _repack_frames(image.candidate, image.decoded, changed, False)
        template = _outer_version(framed_container(image.candidate, payload, changed), new)
        transient = FirmwareImage(None, template, changed,
                                  _patch_rtos_version(image.rtos, old, new), image.icon_bytes,
                                  section_rows(changed), inspect_container(template), _version(new),
                                  _sha(template), image.history, True, image.layout_proof)
    replacements = {'RTOS': target_rtos, 'ICONBIN': icons}
    if new_res_section is not None:
        replacements['RES'] = bytes(new_res_section)
    target = _rebuild_decoded(transient, replacements)
    growth = len(target) != len(transient.decoded)
    payload, feature_report = _repack_frames(transient.candidate, transient.decoded, target, growth, resource_changes=new_res_section is not None)
    candidate = framed_container(transient.candidate, payload, target)
    verified = _load_bytes(candidate)
    policy = inspect_update_policy(verified.rtos)
    if source_policy.get('status') == 'unsupported':
        if (policy.get('status') != 'unsupported' or
                policy.get('gate_sha256') != source_policy.get('gate_sha256')):
            raise FirmwareError('未识别的版本策略在重包时发生变化')
    elif allow_older and not policy.get('installed'):
        raise FirmwareError('rebuilt firmware is missing the verified update policy')
    elif not allow_older and (policy.get('status') != 'verified' or policy.get('policy') != 'factory'):
        raise FirmwareError('rebuilt firmware did not restore the factory update policy')
    if verified.decoded != target or verified.rtos != target_rtos or verified.icon_bytes != icons:
        raise FirmwareError('rebuilt import does not equal requested resources')
    allowed = {'RTOS', 'ICONBIN'} | ({'RES'} if new_res_section is not None else set()) | ({'SRIC', 'CPU'} if old != new else set())
    preserved = []
    for original_row, new_row in zip(image.sections, verified.sections):
        if original_row['name'] not in allowed:
            if image.decoded[original_row['start']:original_row['end']] != target[new_row['start']:new_row['end']]:
                raise FirmwareError('unrequested section changed: ' + original_row['name'])
            preserved.append(original_row['name'])
    return candidate, {
        'source_sha256': image.sha256, 'output_sha256': _sha(candidate),
        'source_stage_id': image.known_stage_id,
        'source_version': image.internal_version, 'output_version': _version_text(_version(new)),
        'decoded_sha256': _sha(target), 'rtos_sha256': _sha(target_rtos),
        'iconbin_sha256': _sha(icons), 'output_bytes': len(candidate),
        'rtos_added_bytes': len(target_rtos) - len(image.rtos),
        'iconbin_added_bytes': len(icons) - len(image.icon_bytes),
        'preserved_sections': preserved, 'version_repack': version_report,
        'feature_repack': feature_report, 'layout_proof': verified.layout_proof,
        'update_policy': policy, 'update_policy_requested': allow_older,
        'update_policy_installation': policy_installation,
        'fresh_history_roundtrip_exact': True, 'hardware_flash_verified': False,
        'requires_local_state_manifest_for_future_editing': not bool(
            verified.layout_proof.get('editor_registry_verified') and
            (verified.layout_proof.get('editor_registry_schema_version') or 0) >= 2),
    }
