"""Load the fixed numeric calibration resources shipped with the application.

These resources have separate provenance from the project's original code;
see docs/CALIBRATION.md and licenses/adobe-dng-sdk.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct

from .bootstrap import PROJECT_ROOT

ASSET_ROOT = Path(__file__).resolve().parent / 'assets' / 'calibration'
GAMMA_PURPOSE = 'Constructor-default style34/hardware0 Gamma fitting-domain curve'
GAMMA_LIMITATION = 'A fixed research-derived numeric fitting baseline, not a measured camera ISP input curve.'


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(path: Path, purpose: str) -> tuple[dict, dict]:
    if not path.is_file():
        raise ValueError('缺少程序校色资源：' + path.name)
    raw = path.read_bytes()
    record = json.loads(raw.decode('utf-8'))
    if not isinstance(record, dict):
        raise ValueError('校色资源 JSON 必须为对象：' + path.name)
    resolved = path.resolve()
    display = str(resolved.relative_to(PROJECT_ROOT)).replace('\\', '/') if resolved.is_relative_to(PROJECT_ROOT) else str(resolved)
    return record, {'path': display, 'sha256': _sha(raw), 'bytes': len(raw),
                    'purpose': purpose, 'requires_local_file': False}


def load_gamma_base() -> tuple[list[int], dict]:
    """Read the unchanged 256-node constructor curve."""
    record, dependency = _record(ASSET_ROOT / 'constructor-gamma.json', GAMMA_PURPOSE)
    curve, selected = record.get('base_curve'), record.get('selected_fixture', {})
    if (record.get('schema_version') != 1 or record.get('kind') != 'native-constructor-gamma'
            or selected != {'style': 34, 'hardware_index': 0, 'original_constructor_default': True, 'base_scalar': 8192}):
        raise ValueError('程序 Gamma 资源格式或选择条件无效')
    if (not isinstance(curve, list) or len(curve) != 256
            or any(type(value) is not int or not 0 <= value <= 65535 for value in curve)
            or any(left > right for left, right in zip(curve, curve[1:]))):
        raise ValueError('Gamma 基线不是有效的 256 节点递增曲线')
    curve_sha = _sha(struct.pack('<256H', *curve))
    if curve_sha != record.get('base_curve_sha256'):
        raise ValueError('程序 Gamma 节点 SHA256 不匹配')
    dependency.update(base_curve_sha256=curve_sha, selected_fixture=selected,
                      limitations=GAMMA_LIMITATION, research_fixture=False)
    return curve, dependency


def load_sdk_tone(tone_path=None) -> tuple[dict, dict]:
    """Read Adobe DNG SDK ACR3 tone samples; an explicit local override is optional."""
    path = Path(tone_path) if tone_path is not None else ASSET_ROOT / 'acr3-default-tone.json'
    record, dependency = _record(path, 'Adobe DNG SDK ACR3 default tone samples')
    samples = record.get('samples')
    if (record.get('kind') != 'dng-sdk-acr3-default' or not isinstance(samples, list) or len(samples) != 1025
            or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in samples)
            or samples[0] != 0 or samples[-1] != 1
            or any(left > right for left, right in zip(samples, samples[1:]))):
        raise ValueError('DNG SDK 默认色调数据无效')
    if _sha(struct.pack('<1025f', *samples)) != record.get('samples_sha256'):
        raise ValueError('DNG SDK 色调样本 SHA256 不匹配')
    if tone_path is not None:
        dependency['requires_local_file'] = True
    return record, dependency


def status() -> dict:
    try:
        _, gamma = load_gamma_base()
        tone_record, tone = load_sdk_tone()
        tone.update(source_sha256=tone_record.get('source_sha256'), samples_sha256=tone_record['samples_sha256'])
        return {'ready': True, 'message': '校色 Gamma 与默认色调资源已就绪', 'gamma': gamma, 'tone': tone}
    except (OSError, ValueError, TypeError, struct.error) as exc:
        return {'ready': False, 'message': str(exc)}
