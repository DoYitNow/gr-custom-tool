# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Firmware history is a forest of complete BIN identities, not draft names."""

from copy import deepcopy
import json
from pathlib import Path
import re
import struct

from .store import sha256, timestamp, write_json
from .versions import parse_version, version_text, version_word


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("固件 SHA-256 无效")
    return value


def _projects(editor):
    directory = editor.store.root / "projects"
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*/project.json"))]


def _paths(editor, digest, projects=None):
    paths = [editor.store.root / "objects" / (digest + ".bin")]
    paths.extend(Path(row["firmware_path"]) for row in editor.store.ledger()
                 if row.get("sha256") == digest and row.get("firmware_path"))
    paths.extend(Path(row["input_path"]) for row in (projects if projects is not None else _projects(editor))
                 if row.get("input", {}).get("sha256") == digest and row.get("input_path"))
    return paths


def resolve_firmware(editor, digest):
    """Resolve a stored complete BIN and check its identity before using it."""
    digest = _digest(digest)
    for path in _paths(editor, digest):
        if path.is_file():
            if sha256(path.read_bytes()) != digest:
                raise ValueError("历史固件文件的 SHA-256 与记录不一致")
            return path
    raise ValueError("找不到这份历史固件的完整 BIN")


def history_index(editor, current_sha=None):
    """Expose known edges only; equal version labels never imply inheritance."""
    if current_sha is not None:
        current_sha = _digest(current_sha)
    with editor.store.lock:
        projects, records, nodes = _projects(editor), editor.store.ledger(), {}

        def node(digest):
            digest = _digest(digest)
            return nodes.setdefault(digest, {
                "sha256": digest, "version": None, "filename": "fwdc248b.bin",
                "created_at": None, "kind": "import", "parent_sha256": None,
                "build_id": None, "download_url": None, "manifest_url": None,
            })

        for project in projects:
            source = project.get("input", {})
            if not source.get("sha256"):
                continue
            entry = node(source["sha256"])
            entry.update(version=source.get("version"), filename=source.get("filename", entry["filename"]))
            created = project.get("created_at")
            if created and (not entry["created_at"] or created < entry["created_at"]):
                entry["created_at"] = created
        for record in records:
            entry = node(record["sha256"])
            parent = record.get("parent_sha256")
            if parent:
                node(parent)
            entry.update(version=record.get("version"), created_at=record.get("created_at"),
                         kind="rollback" if record.get("restored_from_sha256") else "build",
                         parent_sha256=parent, build_id=record["id"],
                         download_url=record.get("download_url"), manifest_url=record.get("manifest_url"))
            for key in ("rollback_from_sha256", "restored_from_sha256"):
                if record.get(key):
                    entry[key] = record[key]
        for entry in nodes.values():
            entry["available"] = any(path.is_file() for path in _paths(editor, entry["sha256"], projects))
            entry["download_url"] = ("/api/history/download?sha256=" + entry["sha256"]
                                     if entry["available"] else None)
            entry["current"] = entry["sha256"] == current_sha
            digest, seen = entry["sha256"], set()
            while digest in nodes and nodes[digest].get("parent_sha256"):
                if digest in seen:
                    raise ValueError("固件历史的父链存在循环")
                seen.add(digest)
                digest = nodes[digest]["parent_sha256"]
            entry["root_sha256"] = digest
        return {"current_sha256": current_sha,
                "nodes": sorted(nodes.values(), key=lambda row: (row.get("created_at") or "", row["sha256"]), reverse=True)}


def open_firmware(editor, digest):
    """Read the selected BIN into a fresh editable draft, without copying recipes."""
    with editor.store.lock:
        path = resolve_firmware(editor, digest)
        entry = next((row for row in history_index(editor)["nodes"] if row["sha256"] == digest), None)
        return editor.import_firmware(path.read_bytes(), entry["filename"] if entry else path.name)


def _verify_program(source, restored, source_rtos=None, allow_older=None):
    """Verify original content plus declared versions and update policy."""
    from .firmware import _patch_rtos_version, repair_last_word
    from .demo_build import prepare_update_policy
    from .update_policy import inspect_update_policy
    if source_rtos is None:
        source_rtos = source.rtos
    source_policy = inspect_update_policy(source_rtos)
    restored_policy = inspect_update_policy(restored.rtos)
    if allow_older is None:
        if source_policy.get("status") == "unsupported":
            # The unknown policy is preserved byte-for-byte; use the normal
            # request value only to satisfy the transform API, then skip the
            # verified-policy equality check below.
            allow_older = True
        else:
            if restored_policy.get("status") != "verified":
                raise ValueError("恢复固件的低版本策略无法识别")
            allow_older = bool(restored_policy.get("installed"))
    old, new = version_word(source.version), version_word(restored.version)
    if [row["name"] for row in source.sections] != [row["name"] for row in restored.sections]:
        raise ValueError("恢复固件改变了节布局")
    preserved, changed = [], []
    policy_rtos, _ = prepare_update_policy(source_rtos, allow_older=allow_older)
    for before, after in zip(source.sections, restored.sections):
        name = before["name"]
        original = source.decoded[before["start"]:before["end"]]
        actual = restored.decoded[after["start"]:after["end"]]
        expected = bytearray(original)
        if name == "RTOS":
            expected[16:] = _patch_rtos_version(policy_rtos, old, new)
            struct.pack_into("<I", expected, 12, len(expected) - 16)
        elif name == "SRIC":
            struct.pack_into("<I", expected, 16 + 0x83D4, new)
            expected[16:] = repair_last_word(expected[16:])
        elif name == "CPU":
            struct.pack_into(">I", expected, 16 + 0x90, new)
            struct.pack_into(">I", expected, 16 + 0x7C, 0)
            checksum = (-sum(value[0] for value in struct.iter_unpack(">I", expected[16:]))) & 0xFFFFFFFF
            struct.pack_into(">I", expected, 16 + 0x7C, checksum)
        if bytes(expected) != actual:
            raise ValueError("恢复固件出现版本、校验和与声明的更新策略以外的修改：" + name)
        (changed if original != actual else preserved).append(name)
    if source.icon_bytes != restored.icon_bytes:
        raise ValueError("恢复固件改变了 ICONBIN 像素")
    policy = restored_policy
    if source_policy.get("status") == "unsupported":
        if (policy.get("status") != "unsupported" or
                policy.get("gate_sha256") != source_policy.get("gate_sha256")):
            raise ValueError("未识别的低版本策略在恢复时发生变化")
    elif policy.get("status") != "verified" or policy.get("installed") is not allow_older:
        raise ValueError("恢复固件的低版本策略读回不一致")
    return {"status": "passed", "only_version_and_checksums_changed": policy_rtos == source.rtos,
            "demo_recovery_metadata_appended": len(source_rtos) > len(source.rtos),
            "original_program_preserved_except_declared_policy": True,
            "only_declared_changes": True,
            "allowed_changes": ["component_versions", "checksums", "update_policy", "demo_recovery_metadata"],
            "update_policy": policy,
            "changed_sections": changed, "preserved_sections": preserved,
            "iconbin_sha256": sha256(restored.icon_bytes), "hardware": "not_tested"}


def rollback_firmware(editor, target_sha, current_project_id, version=None, allow_older=True):
    """Repackage the selected old program at a new number; never recompile it."""
    from .firmware import load_image, repack_image, inspect_firmware
    from .crops import read_crops, recipe
    from .demo_build import _metadata, wrap_official_input
    from .shutdown import read_shutdown
    from .shutdown_compiler import read_menu_resources

    with editor.store.lock:
        target_sha = _digest(target_sha)
        if version is not None:
            parse_version(version)
        project = editor.store.get_project(current_project_id)
        current_sha = _digest(project["input"]["sha256"])
        target_path = resolve_firmware(editor, target_sha)
        target_entry = next((row for row in history_index(editor)["nodes"] if row["sha256"] == target_sha), None)
        source = load_image(target_path)
        current = source if current_sha == target_sha else load_image(resolve_firmware(editor, current_sha))
        _, original_manifest = editor._generated_record(target_sha)
        if not source.editable:
            raise ValueError("这份历史固件尚未匹配可重包布局，只能查看或下载")
        # The current input may have arrived from another computer and be newer
        # than the local ledger; it must still be a version allocation floor.
        next_version = editor._allocate_version(source.version, version, current=current.version)
        generic = not bool(source.layout_proof.get("backend_source_verified"))
        try:
            before_crops, before_crop_inspection = read_crops(source.rtos)
        except (ValueError, KeyError, IndexError, struct.error):
            before_crops, before_crop_inspection = [], {"native_readback": "unknown"}
        before_shutdown = read_shutdown(source)
        try:
            source_metadata = _metadata(source.rtos) or {}
        except (ValueError, TypeError, KeyError, struct.error):
            source_metadata = {}
        before_menu = read_menu_resources(source, source_metadata) if not generic else None
        target_rtos = (wrap_official_input(source.rtos, source.icon_bytes)
                       if not generic and not source_metadata else source.rtos)
        candidate, package_report = repack_image(source, target_rtos, new_version=version_word(next_version),
                                                 allow_older=allow_older)
        build_id = editor.store.new_id("build")
        folder = editor.store.root / "builds" / build_id
        folder.mkdir(parents=True, exist_ok=True)
        firmware_path = folder / "fwdc248b.bin"
        firmware_path.write_bytes(candidate)
        restored = load_image(firmware_path)
        if not restored.editable:
            raise ValueError("恢复候选未匹配 demo 布局")
        preservation = _verify_program(source, restored, target_rtos, allow_older)
        if generic:
            crops, crop_inspection, shutdown_menu = before_crops, before_crop_inspection, None
        else:
            crops, crop_inspection = read_crops(restored.rtos)
            if recipe(crops) != recipe(before_crops) or [row.get("geometry") for row in crops] != [row.get("geometry") for row in before_crops]:
                raise ValueError("恢复固件的裁切原生读回发生变化")
            if crop_inspection.get("native_readback") != before_crop_inspection.get("native_readback"):
                raise ValueError("恢复固件的裁切读回状态发生变化")
            shutdown = read_shutdown(restored)
            restored_metadata = _metadata(restored.rtos) or {}
            shutdown_menu = read_menu_resources(restored, restored_metadata)
            old_res = next(row for row in source.sections if row["name"] == "RES")
            new_res = next(row for row in restored.sections if row["name"] == "RES")
            res_shift = new_res["start"] - old_res["start"]
            expected_resources, expected_menu = deepcopy(before_shutdown["resources"]), deepcopy(before_menu)
            for rows in (expected_resources, expected_menu["presets"] if expected_menu else []):
                for row in rows:
                    for field in ("offset", "record_offset"):
                        if field in row:
                            row[field] += res_shift
            if shutdown["resources"] != expected_resources or shutdown_menu != expected_menu:
                raise ValueError("恢复固件的关机资源读回发生变化")
        try:
            metadata = _metadata(restored.rtos) or {}
        except (ValueError, TypeError, KeyError, struct.error):
            metadata = {}
        if metadata.get("crop_icons"):
            from .crop_icons import verify_crop_icons
            preservation["crop_icons"] = verify_crop_icons(restored, metadata["crop_icons"])
        created = timestamp()
        manifest = deepcopy(original_manifest) if original_manifest else {"schema_version": 1}
        manifest.update(id=build_id, project_id=current_project_id, created_at=created, version=next_version,
                        operation="rollback", source={"filename": target_entry["filename"] if target_entry else target_path.name, "sha256": target_sha,
                                                     "version": version_text(source.version), "known_stage_id": source.known_stage_id},
                        sha256=sha256(candidate), parent_sha256=target_sha,
                        rollback_from_sha256=current_sha, restored_from_sha256=target_sha,
                        restored_from_version=version_text(source.version), edition="demo-crop-shutdown",
                        crops=crops, crop_inspection=crop_inspection, shutdown_menu=shutdown_menu,
                        package_report=package_report, restoration_report=preservation,
                        update_policy=package_report["update_policy"],
                        inspection=inspect_firmware(restored),
                        repack_provenance=editor.source_provenance(),
                        validation={"offline": "passed", "hardware": "not_tested", "camera_or_card_written": False,
                                    "scope": ("fresh container/component/version decode and update policy readback; original program and resource bytes preserved except declared version/checksum, update policy and demo recovery metadata; native crop/shutdown readback; no camera update, flash or boot result"
                                              if not generic else
                                              "fresh container/component/version decode and update policy readback; imported resources preserved; no feature-layout write; no camera update, flash or boot result")})
        if not original_manifest:
            manifest.update(recipe_crops=deepcopy(before_crops),
                            patch_report={"operation": "restore_complete_program_without_compiler"})
        manifest_path = folder / "manifest.json"
        write_json(manifest_path, manifest)
        record = {"id": build_id, "project_id": current_project_id, "created_at": created,
                  "version": next_version, "sha256": manifest["sha256"], "parent_sha256": target_sha,
                  "operation": "rollback", "rollback_from_sha256": current_sha,
                  "restored_from_sha256": target_sha, "restored_from_version": version_text(source.version),
                  "firmware_path": str(firmware_path), "manifest_path": str(manifest_path), "status": "offline_candidate",
                  "download_url": f"/api/download?build_id={build_id}&kind=firmware",
                  "manifest_url": f"/api/download?build_id={build_id}&kind=manifest"}
        editor.store.register_build(record)
        return {**record, "report": {"restoration": preservation, "package": package_report}}
