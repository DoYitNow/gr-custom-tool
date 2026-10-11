# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""XMP/filter operations shared by the public editor service.

The public service remains the owner of firmware import, generic passthrough,
version allocation and packaging.  This mixin adds the research XMP/calibration
workflow without changing those firmware rules.  Filter writes are only
allowed after the native reader and registry backend both accept the input.
"""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
from pathlib import Path
import base64
import json

from .calibration import Calibration, inspect_xmp
from .calibration_workflow import CalibrationWorkflow
from .store import sha256


WRITABLE_PARAMETER_IDS = frozenset((
    "Saturation", "ColorHue", "ImageKey", "Contrast", "ContrastHighLight",
    "ContrastShadow", "Sharpness", "Shading", "ClarityControl",
))


def _filter_backend(image):
    """Return the verified native filter backend for an imported image."""
    try:
        from . import slots
        if not slots.supports_image(image.rtos):
            return None
        return slots
    except (ImportError, AttributeError, OSError, TypeError, ValueError, KeyError):
        return None


def _decorate_filters(filters):
    """Add small data URLs for the browser without changing native bytes."""
    try:
        from PIL import Image
    except ImportError:
        return filters
    for row in filters or []:
        icon = row.get("resources", {}).get("icon", {})
        raw = icon.get("hex")
        if not raw:
            continue
        try:
            pixels = bytes.fromhex(raw)
            width, height = int(icon.get("width", 40)), int(icon.get("height", 40))
            if len(pixels) != width * height * 4:
                continue
            image = Image.frombytes("RGBA", (width, height), pixels)
            image.thumbnail((40, 40), Image.Resampling.LANCZOS)
            output = BytesIO()
            image.save(output, format="PNG")
            row["icon_data_url"] = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        except (ValueError, TypeError, OSError):
            continue
    return filters


class XmpEditorMixin(CalibrationWorkflow):
    """Methods mixed into :class:`gr4_editor.service.Editor`."""

    def _init_xmp_state(self):
        self.calibration = Calibration(self.store.root)
        self._calibration_workers = set()

    def _filter_rows(self, image):
        backend = _filter_backend(image)
        if backend is None:
            return [], None
        from .filter_reader import read_filters
        rows = read_filters(image)
        return _decorate_filters(rows), backend

    def _filter_capabilities(self, image=None, rows=None):
        backend = _filter_backend(image) if image is not None else None
        if backend is None:
            return {"can_add_filter": False, "can_remove_filter": False,
                    "filter_backend_verified": False, "slot_backend": {},
                    "max_custom_slots": 0, "active_custom_slots": 0}
        capabilities = dict(backend.capabilities())
        rows = rows or []
        active = sum(row.get("kind") == "custom" and row.get("enabled", True) for row in rows)
        limit = int(capabilities.get("maximum_custom_slots", 0) or 0)
        return {"can_add_filter": bool(capabilities.get("newslots_supported") and active < limit),
                "can_remove_filter": bool(capabilities.get("removal_supported")),
                "filter_backend_verified": True, "slot_backend": capabilities,
                "max_custom_slots": limit, "active_custom_slots": active}

    def _ensure_filter_project(self, project, image=None):
        """Migrate a public crop/shutdown project without losing its drafts."""
        if image is None:
            path = project.get("input_path")
            if path and Path(path).is_file():
                from .firmware import load_image
                image = load_image(path)
        rows = project.get("filters")
        if rows is None:
            rows, _ = self._filter_rows(image) if image is not None else ([], None)
            project["filters"] = rows
        if "original_filters" not in project:
            project["original_filters"] = deepcopy(rows)
        project.setdefault("xmp_version", 1)
        project.setdefault("calibration", self.calibration.status() if hasattr(self, "calibration") else {})
        filter_caps = self._filter_capabilities(image, project.get("filters", [])) if image is not None else self._filter_capabilities()
        project.setdefault("filter_capabilities", filter_caps)
        project["filter_capabilities"] = {**filter_caps, **project.get("filter_capabilities", {})}
        # Keep the public service's existing capability keys and expose the
        # filter backend beside them for the UI.
        project.setdefault("capabilities", {})
        project["capabilities"].update(filter_caps)
        return project

    def _save_filters(self, project, filters):
        if filters is None:
            return
        if not isinstance(filters, list):
            raise ValueError("滤镜编辑记录必须是列表")
        backend = _filter_backend_from_project(project)
        if backend is None and filters != project.get("filters", []):
            raise ValueError("当前输入布局未通过滤镜写入验证")
        incoming = deepcopy(filters)
        current = {str(row.get("id")): row for row in project.get("filters", [])}
        for row in incoming:
            self._preserve_completed_calibration(row, current.get(str(row.get("id")), {}))
        project["filters"] = incoming

    def add_filter(self, project_id, template_id, name=None):
        project = self.project(project_id)
        if not project.get("capabilities", {}).get("can_add_filter"):
            raise ValueError("当前布局暂不能增加自定义滤镜")
        template = next((row for row in project.get("filters", []) if str(row.get("id")) == str(template_id)), None)
        if not template:
            raise ValueError("请选择一个可写滤镜作为新槽位模板")
        if not template.get("resources", {}).get("banks"):
            raise ValueError("模板滤镜缺少完整颜色资源")
        item = deepcopy(template)
        item.update(id=self.store.new_id("filter"), kind="custom", name=(name or "新滤镜").strip() or "新滤镜",
                    enabled=True, editable=True, identity={}, template_id=template_id,
                    menu_order=len(project.get("filters", [])), calibration=None,
                    xmp=None, pending_color_conversion=False)
        for parameter in item.get("parameters", []):
            if parameter.get("id") in WRITABLE_PARAMETER_IDS:
                parameter.update(default=4, enabled=True, parameter_enable_supported=True,
                                 editable=True, current_value=None,
                                 default_source="planned new-slot default; verified when built")
                parameter.pop("default_verification", None)
            else:
                parameter.update(enabled=False, parameter_enable_supported=False,
                                 editable=False, current_value=None, default=None,
                                 default_source="native schema only; setter not verified")
        project["filters"].append(item)
        self._ensure_filter_project(project)
        return self.store.save_project(project)

    def remove_filter(self, project_id, filter_id):
        project = self.project(project_id)
        if not project.get("capabilities", {}).get("can_remove_filter"):
            raise ValueError("当前后端尚未开放动态删除")
        selected = next((row for row in project.get("filters", []) if str(row.get("id")) == str(filter_id)), None)
        if not selected:
            raise ValueError("找不到目标滤镜")
        if selected.get("kind") != "custom" or not selected.get("editable"):
            raise ValueError("原厂基础预设当前不能删除")
        project["filters"] = [row for row in project["filters"] if str(row.get("id")) != str(filter_id)]
        return self.store.save_project(project)

    def import_xmp(self, project_id, filter_id, data, filename, render_engine="offline"):
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("XMP 文件超过 16 MiB")
        inventory = inspect_xmp(data)
        project = self.project(project_id)
        selected = next((row for row in project.get("filters", []) if str(row.get("id")) == str(filter_id)), None)
        if not selected:
            raise ValueError("找不到目标滤镜")
        if selected.get("kind") != "custom" or not selected.get("editable"):
            raise ValueError("该原生槽位目前只读，请先新增自定义滤镜")
        if render_engine not in ("offline", "lightroom"):
            raise ValueError("颜色转换方式无效")
        path, digest = self.store.object(data, ".xmp")
        selected["xmp"] = {"filename": Path(filename).name, "path": str(path), "sha256": digest,
                           "inventory": inventory, "render_engine": render_engine}
        job = self.calibration.create_job(project_id, str(filter_id), path, render_engine=render_engine)
        self._bind_calibration_source(job, project)
        selected["calibration"] = job
        selected["pending_color_conversion"] = True
        result = self.store.save_project(project)
        if job.get("id"):
            self._queue_calibration(project_id, str(filter_id))
        return result

    def import_icon(self, project_id, filter_id, data):
        from PIL import Image
        project = self.project(project_id)
        selected = next((row for row in project.get("filters", []) if str(row.get("id")) == str(filter_id)), None)
        if not selected or selected.get("kind") != "custom" or not selected.get("editable"):
            raise ValueError("该滤镜图标只读")
        with Image.open(BytesIO(data)) as image:
            icon = image.convert("RGBA").resize((40, 40), Image.Resampling.LANCZOS)
        selected.setdefault("resources", {}).setdefault("icon", {}).update(
            hex=icon.tobytes().hex(), width=40, height=40, pixel_format="RGBA8",
            sha256=sha256(icon.tobytes()))
        output = BytesIO()
        icon.save(output, format="PNG")
        selected["icon_data_url"] = "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
        return self.store.save_project(project)


    def run_calibration(self, project_id, filter_id, render_engine=None):
        project = self.project(project_id)
        selected = next((row for row in project.get("filters", []) if str(row.get("id")) == str(filter_id)), None)
        if not selected or not selected.get("xmp"):
            raise ValueError("请先为目标滤镜导入 XMP")
        engine = render_engine or selected["xmp"].get("render_engine") or "offline"
        job = self.calibration.create_job(project_id, str(filter_id), selected["xmp"]["path"], render_engine=engine)
        selected["xmp"]["render_engine"] = engine
        self._bind_calibration_source(job, project)
        selected["calibration"] = job
        selected["pending_color_conversion"] = True
        result = self.store.save_project(project)
        if job.get("id"):
            self._queue_calibration(project_id, str(filter_id))
        return result


    def register_calibration(self, samples, heldout):
        result = self.calibration.register_uploads(samples, heldout)
        # XMP jobs imported before the DNG set was available are intentionally
        # retried now that the dataset is complete.
        for path in (self.store.root / "projects").glob("*/project.json"):
            try:
                project = self.store.get_project(path.parent.name)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for row in project.get("filters", []):
                if row.get("xmp") and row.get("pending_color_conversion"):
                    self._queue_calibration(project["id"], row["id"])
        return result

    def filter_build_plan(self, project, image):
        """Compile filter rows for a verified native input, or return None."""
        backend = _filter_backend(image)
        if backend is None:
            if project.get("filters") not in (None, [], project.get("original_filters", [])):
                raise ValueError("未识别的固件布局不能写入滤镜")
            return None
        rows = project.get("filters", [])
        original = project.get("original_filters", rows)
        if any(row.get("pending_color_conversion") and row.get("enabled", True) for row in rows):
            raise ValueError("还有滤镜等待 XMP 颜色转换，不能导出旧颜色")
        if any(row.get("xmp") and row.get("enabled", True) and
               (row.get("calibration") or {}).get("status") == "calibration_failed" for row in rows):
            raise ValueError("有滤镜校色失败，请重新转换后再生成固件")
        from .filter_reader import read_filters
        # The public branch intentionally does not enable the frozen soft-glow
        # experiment.  Crop/shutdown remain on the public compiler path; this
        # plan only writes native filter resources and registry state.
        rtos, icons, report = backend.apply_filter_registry(
            image.rtos, image.icon_bytes, rows, original,
            baseline_rtos=backend.canonical_base(image.rtos),
            baseline_icons=None, crops=None, prior_crops=None, crop_context=None,
            shutdown_plan=None, soft_glow_plan={"enabled": False},
            unlock_native_mono=None)
        # Force a fresh native readback before packaging.
        readback = read_filters(type("Image", (), {"rtos": rtos, "icon_bytes": icons, "editable": True})())
        # Keep the compiler's resolved identities alongside the report.  New
        # custom slots intentionally start with an empty identity and receive
        # a native style id during compilation; callers must validate against
        # this planned result rather than the pre-compile draft.
        report["recipe_filters"] = deepcopy(report.get("recipe_filters", rows))
        report["xmp_calibration"] = {"render_engines": sorted({
            row.get("xmp", {}).get("render_engine", "offline") for row in rows if row.get("xmp")
        }), "hardware": "not_tested"}
        return rtos, icons, report, readback


def _filter_backend_from_project(project):
    """Best-effort backend check using the saved layout proof."""
    proof = project.get("firmware", {}).get("layout_proof", {})
    return object() if proof.get("backend_source_verified") and not proof.get("generic_passthrough") else None
