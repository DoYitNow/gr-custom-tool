# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Local crop ratios and shutdown pictures; demo edition."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import struct

from .bootstrap import DATA_ROOT, EDITOR_ROOT, PROJECT_ROOT
from .store import Store, sha256, timestamp, write_json
from .versions import allocate_version, version_text, version_word

EDITION = "demo-crop-shutdown"


def _update_policy_draft(value=None, fallback=True):
    """Validate the saved low-version support choice."""
    if value is None:
        value = fallback
    if type(value) is not bool:
        if isinstance(value, dict) and set(value) in ({"allow_older"}, {"schema_version", "allow_older"}):
            value = value.get("allow_older")
        else:
            raise ValueError("低版本固件支持开关必须是布尔值")
    if type(value) is not bool:
        raise ValueError("低版本固件支持开关必须是布尔值")
    return {"schema_version": 1, "allow_older": value}


def _build_options(value=None, fallback=True):
    """Validate the build options sent by the web editor."""
    if value is None:
        return {"allow_older": bool(fallback)}
    if isinstance(value, dict):
        if set(value) != {"allow_older"} or type(value["allow_older"]) is not bool:
            raise ValueError("低版本固件支持开关必须是布尔值")
        return {"allow_older": value["allow_older"]}
    if type(value) is bool:
        return {"allow_older": value}
    raise ValueError("低版本固件支持开关必须是布尔值")


class Editor:
    def __init__(self, root=DATA_ROOT):
        self.store = Store(root)
        self._input_version_cache = {}

    def settings(self):
        from .toolchain import status
        return {"toolchain": status(self.store.root)}

    def status(self):
        projects = []
        for path in sorted((self.store.root / "projects").glob("*/project.json")):
            value = self.store.get_project(path.parent.name)
            if value.get("edition") == EDITION:
                projects.append({key: value.get(key) for key in ("id", "title", "updated_at", "input")})
        return {"version": "0.1.0-demo", "edition": EDITION, "projects": projects,
                "data_directory": str(self.store.root), **self.settings(),
                "builds": self.store.ledger(), "capabilities": self._capabilities(),
                "update_policy": {"policy": "allow_older", "required_for_generated_firmware": False,
                                  "default_allow_older": True, "hardware_verified": False}}

    def firmware_history(self, project_id=None):
        from .history import history_index
        return history_index(self, self.project(project_id)['input']['sha256'] if project_id else None)

    def history_firmware_path(self, digest):
        from .history import resolve_firmware
        return resolve_firmware(self, digest)

    def open_history_firmware(self, digest):
        from .history import open_firmware
        return open_firmware(self, digest)

    def rollback_history_firmware(self, digest, project_id, version=None, allow_older=True):
        from .history import rollback_firmware
        return rollback_firmware(self, digest, project_id, version, allow_older=allow_older)

    def _capabilities(self, image=None, editable=False, feature_backend=None):
        if image is not None:
            editable = image.editable
            feature_backend = image.layout_proof.get("backend_source_verified", False)
        if feature_backend is None:
            feature_backend = bool(editable)
        return {"can_import": True, "can_edit": bool(editable), "can_build": bool(editable),
                "feature_backend_verified": bool(feature_backend),
                "requires_toolchain": bool(feature_backend),
                "crop_supported": True, "shutdown_supported": True, "edition": EDITION}

    def import_firmware(self, data, filename="fwdc248b.bin"):
        from .firmware import load_image, inspect_firmware
        from .crops import read_crops, capabilities
        from .shutdown import read_shutdown, validate_draft
        path, digest = self.store.object(data)
        image = load_image(path)
        if image.sha256 != digest:
            raise ValueError("已保存的输入固件哈希不一致")
        info = inspect_firmware(image)
        info["editor_supported"] = image.editable
        try:
            crops, inspection = read_crops(image.rtos)
        except (ValueError, KeyError, IndexError, struct.error) as exc:
            crops, inspection = [], {
                "native_readback": "unknown", "compiler_supported": False,
                "reason": "裁切布局未识别：" + str(exc), "notes": []}
        crop_capabilities = capabilities(inspection)
        feature_backend = bool(info.get("layout_proof", {}).get("backend_source_verified"))
        if not feature_backend:
            crop_capabilities.update(can_edit=False, can_add=False, can_write=False, draft_only=True,
                                     reason="此输入可导入并生成保留资源的版本/回退候选，但裁切写入布局尚未适配")
        inventory = read_shutdown(image)
        if not feature_backend:
            inventory["capabilities"].update(can_prepare_assets=False, can_build_menu=False,
                                               menu_build_reason="此输入可导入并生成保留资源的版本/回退候选，但关机菜单写入布局尚未适配")
        project = {"schema_version": 1, "edition": EDITION, "id": self.store.new_id("project"),
                   "title": Path(filename).stem, "created_at": timestamp(),
                   "input": {"filename": Path(filename).name, "sha256": digest,
                             "version": version_text(image.version), "known_stage_id": image.known_stage_id},
                   "input_path": str(path), "firmware": info, "capabilities": self._capabilities(image),
                   "can_generate": image.editable, "suggested_version": self._allocate_version(image.version),
                   "builds": self._build_ancestry(digest), "crops": crops, "original_crops": deepcopy(crops),
                   "crop_inspection": inspection, "crop_capabilities": crop_capabilities,
                   "shutdown_inventory": inventory, "shutdown": validate_draft(None, inventory),
                   "update_policy_draft": _update_policy_draft(), "build_options": _build_options()}
        return self.store.save_project(project)

    def project(self, project_id):
        result = self.store.get_project(project_id)
        if result.get("edition") != EDITION:
            raise ValueError("此编辑记录不属于 demo，请导入原始 BIN 建立新记录")
        # Projects imported before generic passthrough support persisted the
        # old read-only flag.  Their strict container proof is enough to
        # upgrade them without re-decoding a large BIN on every page refresh.
        proof = result.get("firmware", {}).get("layout_proof", {})
        if (not result.get("firmware", {}).get("editor_supported") and
                proof.get("container_checksum_valid") and proof.get("section_order_valid")):
            result["firmware"]["editor_supported"] = True
            result["firmware"]["editable"] = True
            proof["generic_passthrough"] = not bool(proof.get("backend_source_verified"))
            result["firmware"]["editability"] = {
                "editable": True,
                "reason": "Verified firmware container; feature layout is not registered, so generation preserves imported resources"
                if proof["generic_passthrough"] else "Verified backend RTOS and supported layout"}
        result["suggested_version"] = self._allocate_version(result["input"]["version"])
        result["can_generate"] = bool(result["firmware"].get("editor_supported"))
        result["capabilities"] = self._capabilities(
            editable=result["can_generate"],
            feature_backend=bool(result.get("firmware", {}).get("layout_proof", {}).get("backend_source_verified")))
        saved = result.get("build_options", result.get("update_policy_draft"))
        options = _build_options(_update_policy_draft(saved)["allow_older"])
        result["build_options"] = options
        result["update_policy_draft"] = _update_policy_draft(options)
        return result

    def import_project(self, data):
        if not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("edition") != EDITION:
            raise ValueError("不是支持的 demo 项目文件")
        source = data.get("input", {})
        digest = source.get("sha256", "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("项目缺少有效的输入固件哈希")
        path = self.store.root / "objects" / (digest + ".bin")
        if not path.is_file():
            raise ValueError("请先导入这个项目对应的输入 BIN")
        project = self.import_firmware(path.read_bytes(), source.get("filename", "fwdc248b.bin"))
        if data.get("crop_identity_context"):
            from .crop_compiler import prepare_crops
            from .demo_build import _metadata
            from .firmware import load_image
            image = load_image(path)
            context = deepcopy(data["crop_identity_context"])
            prepare_crops(image.rtos, _metadata(image.rtos) or {}, data.get("crops", project["crops"]),
                          project["original_crops"], context)
            project["crop_identity_context"] = context
            self.store.save_project(project)
        return self.save(project["id"], data.get("title"), data.get("crops"), data.get("shutdown"),
                         update_policy_draft=data.get("update_policy_draft"),
                         build_options=data.get("build_options"))

    def save(self, project_id, title=None, crops=None, shutdown=None,
             update_policy_draft=None, build_options=None):
        from .shutdown import validate_draft
        with self.store.lock:
            project = self.project(project_id)
            if not project["can_generate"]:
                raise ValueError("此输入未匹配可编辑的 demo 布局")
            if title is not None:
                if not isinstance(title, str) or not title.strip() or len(title) > 120:
                    raise ValueError("项目名称必须为 1–120 个字符")
                project["title"] = title.strip()
            if crops is not None:
                project["crops"] = self._crop_draft(project, crops)
            if shutdown is not None:
                project["shutdown"] = validate_draft(shutdown, project["shutdown_inventory"])
            requested_options = build_options if build_options is not None else update_policy_draft
            if requested_options is None:
                requested_options = project.get("build_options", project.get("update_policy_draft"))
            options = _build_options(_update_policy_draft(requested_options)["allow_older"])
            project["build_options"] = options
            project["update_policy_draft"] = _update_policy_draft(options)
            return self.store.save_project(project)

    def _generated_record(self, digest, seen=None):
        record = next((row for row in self.store.ledger() if row.get("sha256") == digest), None)
        if not record:
            return None, None
        manifest = json.loads(Path(record["manifest_path"]).read_text(encoding="utf-8"))
        if (manifest.get("edition") != EDITION or manifest.get("sha256") != digest
                or manifest.get("validation", {}).get("offline") != "passed"):
            raise ValueError("本地生成记录与 demo 候选不匹配")
        return record, manifest

    def _allocate_version(self, source, requested=None, current=None):
        """Include imported BIN versions as well as generated history."""
        with self.store.lock:
            records = self.store.ledger()
            for path in (self.store.root / "projects").glob("*/project.json"):
                stat = path.stat()
                stamp = (stat.st_mtime_ns, stat.st_size)
                cached = self._input_version_cache.get(path)
                if cached is None or cached[0] != stamp:
                    incoming = self.store.get_project(path.parent.name).get("input", {})
                    cached = self._input_version_cache[path] = (stamp, incoming.get("version"))
                if cached[1]:
                    records.append({"version": cached[1]})
            if current is not None:
                records.append({"version": version_text(current)})
            return allocate_version(source, records, requested)

    def _build_ancestry(self, digest):
        records = {row["sha256"]: row for row in self.store.ledger()}
        result = []
        for _ in range(len(records)):
            record = records.get(digest)
            if not record:
                break
            result.append(deepcopy(record))
            digest = record.get("parent_sha256")
        return list(reversed(result))

    def _shutdown_item(self, draft, name, asset_id=None, fit="contain"):
        from uuid import uuid4
        from .shutdown import MAX_MENU_ITEMS
        if len(draft['items']) >= MAX_MENU_ITEMS:
            raise ValueError(f"目前最多支持 {MAX_MENU_ITEMS} 个自定义关机图案")
        used = {row['selection_id'] for row in draft['items']}
        selection_id = 0
        while selection_id <= 2 or selection_id in used:
            selection_id = uuid4().int & 0xffffffff
        item = {'id': self.store.new_id('shutdown-item'), 'selection_id': selection_id,
                'name': name, 'asset_id': asset_id, 'fit': fit}
        draft['items'].append(item)
        draft['selected_item_id'] = item['id']
        return item

    def add_shutdown_item(self, project_id, name=None, source_id=None, asset_id=None):
        from .shutdown import prepare_asset, validate_draft, _decode_b64, _legacy_item_name
        with self.store.lock:
            project = self.project(project_id)
            if not project['can_generate']:
                raise ValueError('此输入未匹配可编辑的 demo 布局')
            draft = deepcopy(project['shutdown'])
            if source_id and asset_id:
                raise ValueError("请选择一种图片来源")
            fit = 'contain'
            if source_id:
                inventory = project['shutdown_inventory']
                source = next((row for row in inventory['resources'] if row['id'] == source_id), None)
                if not source or not source.get('payload_base64'):
                    raise ValueError("未识别所选原有图案")
                target = next((row for row in inventory['resources'] if row['id'] == 'goodbye'), None)
                target = target or source
                asset = prepare_asset(_decode_b64(source['payload_base64'], 'original picture'),
                                      source['name'] + '.jpg', target, fit)
                draft['assets'].append(asset)
                asset_id = asset['id']
                name = name or source['name']
            elif asset_id:
                asset = next((row for row in draft['assets'] if row['id'] == asset_id), None)
                if not asset:
                    raise ValueError("所选图片素材不存在")
                name, fit = name or _legacy_item_name(asset['name'], '新图案'), asset['fit']
            self._shutdown_item(draft, name or '新图案', asset_id, fit)
            project['shutdown'] = validate_draft(draft, project['shutdown_inventory'])
            return self.store.save_project(project)

    def import_shutdown_image(self, project_id, target_id, data, filename="image.png", fit="contain",
                              item_id=None, item_name=None):
        from .shutdown import prepare_asset, validate_draft, _legacy_item_name
        with self.store.lock:
            project = self.project(project_id)
            if not project['can_generate']:
                raise ValueError('此输入未匹配可编辑的 demo 布局')
            resources = project["shutdown_inventory"]["resources"]
            target_id = target_id or ('goodbye' if any(row['id'] == 'goodbye' for row in resources) else next((row['id'] for row in resources), None))
            target = next((row for row in resources if row["id"] == target_id), None)
            if not target:
                raise ValueError("未从实际输入 BIN 识别此关机资源")
            asset = prepare_asset(data, filename, target, fit)
            source, _ = self.store.object(data, ".image")
            asset["source_path"] = str(source)
            draft = deepcopy(project["shutdown"])
            draft.update(target_id=target_id, selected_asset_id=asset["id"])
            draft["assets"].append(asset)
            if item_id:
                item = next((row for row in draft['items'] if row['id'] == item_id), None)
                if item is None:
                    raise ValueError("所选关机图案不存在")
                item.update(asset_id=asset['id'], fit=fit)
                if item_name is not None:
                    item['name'] = item_name
                draft['selected_item_id'] = item['id']
            else:
                self._shutdown_item(draft, item_name or _legacy_item_name(asset['name'], '新图案'), asset['id'], fit)
            project["shutdown"] = validate_draft(draft, project["shutdown_inventory"])
            return self.store.save_project(project)

    def export_shutdown(self, project_id):
        from .firmware import load_image
        from .shutdown import read_shutdown, replacement_bundle
        with self.store.lock:
            project = self.project(project_id)
            image = load_image(project["input_path"])
            if image.sha256 != project["input"]["sha256"]:
                raise ValueError("输入固件已变化")
            bundle = replacement_bundle(project["shutdown"], read_shutdown(image), project["input"])
            target = self.store.project_path(project_id).parent / "shutdown" / "shutdown-preparation.zip"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bundle)
            return bundle

    def _crop_draft(self, project, crops):
        from .crops import validate_draft, preview_geometry
        from .firmware import load_image
        rows = validate_draft(crops, project)
        pending = [row for row in rows if row.get("kind") == "custom" and row.get("editable") and not row.get("geometry")]
        if pending:
            image = load_image(project["input_path"])
            if image.sha256 != project["input"]["sha256"]:
                raise ValueError("输入固件已变化")
            for row in pending:
                result = preview_geometry(image.rtos, row["requested_ratio"])
                row.update(geometry=result["geometry"], actual_ratio=result["actual_ratio"],
                           geometry_status="draft_not_native", geometry_constraints=result["constraints"])
        return rows

    def crop_preview(self, project_id, ratio):
        from .crops import preview_geometry
        from .firmware import load_image
        project = self.project(project_id)
        if not project["crop_capabilities"]["can_edit"]:
            raise ValueError(project["crop_capabilities"]["reason"])
        image = load_image(project["input_path"])
        if image.sha256 != project["input"]["sha256"]:
            raise ValueError("输入固件已变化")
        return preview_geometry(image.rtos, ratio)

    def build(self, project_id, version=None, allow_older=None):
        from .firmware import load_image, repack_image, inspect_firmware
        from .demo_build import build_features, _metadata, canonical_base, OFFICIAL7_SHA
        from .crop_compiler import prepare_crops
        from .crops import read_crops, recipe
        from .shutdown import read_shutdown, validate_draft
        from .shutdown_compiler import prepare_menu_plan, read_menu_resources, verify_original_res
        from .crop_icons import verify_crop_icons
        with self.store.lock:
            project = self.project(project_id)
            if not project["can_generate"]:
                raise ValueError("该输入未匹配可生成的 demo 布局")
            saved_options = _build_options(project.get("build_options", project.get("update_policy_draft")))
            allow_older = (saved_options["allow_older"] if allow_older is None else
                           _build_options(allow_older)["allow_older"])
            image = load_image(project["input_path"])
            if image.sha256 != project["input"]["sha256"] or not image.editable:
                raise ValueError("输入固件已变化或布局不受支持")
            next_version = self._allocate_version(image.version, version)
            feature_backend = bool(image.layout_proof.get("backend_source_verified"))
            if not feature_backend:
                baseline_shutdown = validate_draft(None, read_shutdown(image))
                if project["crops"] != project["original_crops"] or project["shutdown"] != baseline_shutdown:
                    raise ValueError("当前 BIN 只支持保留资源的版本/回退重包；裁切和关机布局尚未适配")
                rtos, icons = image.rtos, image.icon_bytes
                crop_plan = shutdown_plan = shutdown_res = None
                frozen_crops = crop_inspection = None
                patch_report = {"backend": "generic-preserve", "feature_writes": False,
                                "limitations": ["未注册的输入布局仅保留原有裁切、关机和图标资源"]}
            else:
                crop_plan, source_inspection = prepare_crops(image.rtos, _metadata(image.rtos) or {},
                    project["crops"], project["original_crops"], project.get("crop_identity_context"))
                shutdown_plan, shutdown_res = prepare_menu_plan(image, read_shutdown(image), project["shutdown"])
                rtos, icons, patch_report = build_features(image.rtos, image.icon_bytes,
                    crop_plan=crop_plan, source_crop_inspection=source_inspection, shutdown_plan=shutdown_plan)
                frozen_crops, crop_inspection = read_crops(rtos)
                if crop_inspection.get("native_readback") != "passed":
                    raise ValueError("生成结果的原生裁切读回失败")
            candidate, package_report = repack_image(image, rtos, new_icon_bytes=icons,
                new_version=version_word(next_version), new_res_section=shutdown_res,
                allow_older=allow_older)
            build_id = self.store.new_id("build")
            folder = self.store.root / "builds" / build_id
            folder.mkdir(parents=True, exist_ok=True)
            firmware_path = folder / "fwdc248b.bin"
            firmware_path.write_bytes(candidate)
            packaged = load_image(firmware_path)
            if not packaged.editable:
                raise ValueError("重包后的 demo 候选不能独立识别")
            try:
                packaged_crops, packaged_inspection = read_crops(packaged.rtos)
            except (ValueError, KeyError, IndexError, struct.error) as exc:
                packaged_crops, packaged_inspection = project["crops"], {
                    "native_readback": "unknown", "compiler_supported": False,
                    "reason": "裁切布局未识别：" + str(exc), "notes": []}
            if feature_backend and (recipe(packaged_crops) != recipe(frozen_crops)
                    or [row.get("geometry") for row in packaged_crops] != [row.get("geometry") for row in frozen_crops]):
                raise ValueError("容器重包后的裁切读回不一致")
            try:
                metadata = _metadata(packaged.rtos) or {}
            except (ValueError, TypeError, KeyError, struct.error):
                metadata = {}
            if feature_backend and metadata.get("crop_icons"):
                patch_report["crop_icon_container_readback"] = verify_crop_icons(packaged, metadata["crop_icons"])
            installed_shutdown = read_menu_resources(packaged, metadata) if feature_backend else None
            if feature_backend and bool(installed_shutdown) != bool(shutdown_plan):
                raise ValueError("生成结果的关机菜单状态不一致")
            if feature_backend and shutdown_res is not None:
                patch_report["shutdown_resource_preservation"] = verify_original_res(image, packaged)
            baseline = canonical_base(packaged.rtos) if feature_backend else None
            if feature_backend and sha256(baseline) != OFFICIAL7_SHA:
                raise ValueError("生成候选无法恢复到已适配的官方基线")
            created = timestamp()
            manifest = {"schema_version": 1, "edition": EDITION, "id": build_id, "project_id": project_id,
                        "created_at": created, "version": next_version, "source": project["input"],
                        "sha256": sha256(candidate), "parent_sha256": image.sha256,
                        "compiler_base": ({"rtos_sha256": OFFICIAL7_SHA, "version": "1.11.10.7",
                                           "source": "restored user-supplied official input"}
                                          if feature_backend else
                                          {"rtos_sha256": sha256(image.rtos), "version": image.internal_version,
                                           "source": "preserved imported input; feature layout not registered"}),
                        "recipe_crops": project["crops"], "crops": packaged_crops,
                        "crop_inspection": packaged_inspection, "shutdown_menu": installed_shutdown,
                        "recipe_shutdown": project["shutdown"], "source_provenance": self.source_provenance(),
                        "patch_report": patch_report, "package_report": package_report,
                        "update_policy": package_report["update_policy"],
                        "update_policy_requested": allow_older,
                        "inspection": inspect_firmware(packaged),
                        "validation": {"offline": "passed", "hardware": "not_tested",
                                       "camera_or_card_written": False,
                                       "scope": ("container/version/checksum and update policy roundtrip; native crop readback; embedded shutdown resources; canonical base recovery; no hardware result"
                                                 if feature_backend else
                                                 "container/version/checksum and update policy roundtrip; imported resources preserved; no feature-layout write; no hardware result")}}
            manifest_path = folder / "manifest.json"
            write_json(manifest_path, manifest)
            record = {"id": build_id, "project_id": project_id, "created_at": created, "version": next_version,
                      "sha256": manifest["sha256"], "parent_sha256": image.sha256,
                      "firmware_path": str(firmware_path), "manifest_path": str(manifest_path),
                      "status": "offline_candidate",
                      "download_url": f"/api/download?build_id={build_id}&kind=firmware",
                      "manifest_url": f"/api/download?build_id={build_id}&kind=manifest"}
            self.store.register_build(record)
            project["builds"].append(record)
            if metadata.get("crop_registry"):
                project["crop_identity_context"] = {"source_rtos_sha256": sha256(image.rtos),
                    "records": metadata["crop_registry"], "next_public": metadata["crop_next_public"],
                    "order": metadata["crop_order"]}
            self.store.save_project(project)
            return {**record, "report": {"patch": patch_report, "package": package_report}}

    def source_provenance(self):
        commit, changed = None, None
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True)
            dirty = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT, capture_output=True, text=True)
            commit = result.stdout.strip() if result.returncode == 0 else None
            changed = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
        except FileNotFoundError:
            pass  # A source ZIP does not require installing Git to generate a candidate.
        paths = [path for folder in (EDITOR_ROOT / 'gr4_editor', EDITOR_ROOT / 'web')
                 for path in folder.rglob('*') if path.is_file() and path.suffix in ('.py', '.c', '.h', '.S', '.js', '.css', '.html')]
        paths += [EDITOR_ROOT / name for name in ('run.py', 'launcher.py')]
        import sys
        for module in tuple(sys.modules.values()):
            path = getattr(module, "__file__", None)
            if path:
                path = Path(path).resolve()
                if path.is_relative_to(EDITOR_ROOT) and path.is_file():
                    paths.append(path)
        paths = sorted(set(paths))
        return {"git_commit": commit,
                "working_tree_dirty": changed,
                "python_version": sys.version,
                "executed_editor_files": {str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): sha256(path.read_bytes()) for path in paths if path.is_file()}}
