# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Allocate supported internal versions for the demo candidate family."""

FAMILY = (1, 11, 10)
FIRST_GENERATED_BUILD = 10


def parse_version(value):
    if type(value) is int:
        if not 0 <= value <= 0xFFFFFFFF:
            raise ValueError("固件版本超出四字节范围")
        return tuple((value >> shift) & 255 for shift in (24, 16, 8, 0))
    if isinstance(value, (tuple, list)):
        parts = tuple(value)
    else:
        try:
            parts = tuple(int(part) for part in str(value).split("."))
        except ValueError as exc:
            raise ValueError("固件版本应为四段数字，如 1.11.10.28") from exc
    if len(parts) != 4 or any(type(part) is not int or not 0 <= part <= 255 for part in parts):
        raise ValueError("固件版本应为四段 0–255 的数字")
    return parts


def version_text(value):
    return ".".join(map(str, parse_version(value)))


def version_word(value):
    result = 0
    for part in parse_version(value):
        result = (result << 8) | part
    return result


def recorded_versions(ledger=()):
    """Use this user's records; internal research history is not a version floor."""
    values = [parse_version(row["version"]) for row in ledger if row.get("version")]
    return [value for value in values if value[:3] == FAMILY]


def allocate_version(source, ledger=(), requested=None):
    source = parse_version(source)
    if source[:3] != FAMILY:
        raise ValueError("目前只支持 1.11.10.* 内部版本；跨版本布局需单独适配")
    used = recorded_versions(ledger)
    # Reserve the lower official/App range, then increment the local maximum.
    minimum = max([FIRST_GENERATED_BUILD - 1, source[3]] + [value[3] for value in used]) + 1
    if minimum > 255:
        raise ValueError("当前版本第四段已满，不能未经适配自动进位")
    value = FAMILY + (minimum,)
    if requested is not None:
        value = parse_version(requested)
        if value[:3] != FAMILY or value[3] < minimum:
            raise ValueError(f"版本号必须不低于 {version_text(FAMILY + (minimum,))}")
    return version_text(value)
