# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Shared XMP calibration publishing and background task lifecycle.

Firmware import and compilation stay with the host editor. Both product
services use this workflow so render results, stale task rejection and
resource metadata follow the same rules.
"""
from copy import deepcopy
from pathlib import Path
import threading
import traceback

from .store import sha256, write_json


class CalibrationWorkflow:
    def calibration_output(self, job_id, filename):
        """Read an image or report produced in this task's fit directory."""
        if (not isinstance(job_id, str) or len(job_id) != 24 or
                any(char not in "0123456789abcdef" for char in job_id) or
                not isinstance(filename, str) or not filename or "\\" in filename or Path(filename).name != filename):
            raise ValueError("校色输出标识无效")
        target = self.store.root / "calibration" / "jobs" / job_id / "fit" / filename
        if not target.is_file() or target.suffix.lower() not in (".jpg", ".jpeg", ".png", ".json"):
            raise ValueError("尚无该校色输出")
        return target

    @staticmethod
    def _preserve_completed_calibration(row, recent):
        """Keep a completed fit when an older browser draft is saved."""
        completed = recent.get("calibration") or {}
        incoming = row.get("calibration") or {}
        source, latest_source = row.get("xmp") or {}, recent.get("xmp") or {}
        if (completed.get("status") != "fitted_offline" or recent.get("pending_color_conversion") or
                not row.get("pending_color_conversion") or not source.get("sha256") or
                source.get("sha256") != latest_source.get("sha256")):
            return False
        engine = source.get("render_engine") or incoming.get("render_engine") or "offline"
        latest_engine = latest_source.get("render_engine") or completed.get("render_engine") or "offline"
        if engine != latest_engine:
            return False
        if incoming.get("id") and completed.get("id") and incoming["id"] != completed["id"]:
            return False
        row["calibration"] = deepcopy(completed)
        row["pending_color_conversion"] = False
        for bank, latest in zip(row.get("resources", {}).get("banks", []), recent.get("resources", {}).get("banks", [])):
            for key in ("matrix", "gamma", "multi_main"):
                for field in ("hex", "sha256"):
                    if field in latest[key]:
                        bank[key][field] = latest[key][field]
            if "calibration" in latest:
                bank["calibration"] = deepcopy(latest["calibration"])
        return True

    def _bind_calibration_source(self, job, project):
        job["firmware_path"] = project["input_path"]
        job["firmware_sha256"] = project["input"]["sha256"]
        if job.get("id"):
            write_json(self.store.root / "calibration" / "jobs" / job["id"] / "job.json", job)

    def calibration_state(self, project_id, filter_id):
        project = self.store.get_project(project_id)
        selected = next((row for row in project["filters"] if str(row["id"]) == str(filter_id)), None)
        if not selected:
            raise ValueError("找不到目标滤镜")
        job = selected.get("calibration") or {}
        if (selected.get("pending_color_conversion") and job.get("id") and
                job.get("render_engine") == "lightroom" and
                job.get("status") in ("awaiting_lightroom", "rendering_lightroom", "rendered")):
            with self.store.lock:
                running = (str(project_id), str(filter_id)) in self._calibration_workers
            if not running:
                try:
                    job = self.calibration.poll_render(job["id"])
                except Exception as exc:
                    job = {**job, "status": "calibration_failed", "message": "自动校色失败：" + str(exc)}
                    write_json(self.store.root / "calibration/jobs" / job["id"] / "job.json", job)
                if self._publish_calibration(project_id, filter_id, job) and job.get("status") == "rendered":
                    self._queue_calibration(project_id, filter_id)
                project = self.store.get_project(project_id)
                selected = next((row for row in project["filters"] if str(row["id"]) == str(filter_id)), None)
                if not selected:
                    raise ValueError("找不到目标滤镜")
        resources = (selected.get("calibration") or {}).get("resources")
        resource_sha256 = {}
        if isinstance(resources, dict):
            for key in ("matrix", "gamma", "multi"):
                value = resources.get(key)
                if isinstance(value, str):
                    try:
                        resource_sha256[key] = sha256(bytes.fromhex(value))
                    except ValueError:
                        continue
        return {"calibration": selected.get("calibration"), "pending": bool(selected.get("pending_color_conversion")),
                "resources": resources, "resource_sha256": resource_sha256,
                "capabilities": self.calibration.status()}

    def _queue_calibration(self, project_id, filter_id):
        key = (str(project_id), str(filter_id))
        with self.store.lock:
            if key in self._calibration_workers:
                return
            self._calibration_workers.add(key)
        threading.Thread(target=self._calibration_worker, args=key, daemon=True).start()

    def _publish_calibration(self, project_id, filter_id, job):
        with self.store.lock:
            project = self.store.get_project(project_id)
            selected = next((row for row in project["filters"] if str(row["id"]) == str(filter_id)), None)
            if not selected or selected.get("xmp", {}).get("sha256") != job["xmp_sha256"]:
                return False
            current_id = (selected.get("calibration") or {}).get("id")
            if current_id and job.get("id") != current_id:
                return False  # A mode change created a different task for the same XMP.
            if selected.get("calibration") == job and (job.get("status") not in ("fitted_offline", "calibration_failed") or not selected.get("pending_color_conversion")):
                return True
            selected["calibration"] = deepcopy(job)
            if job.get("status") == "fitted_offline":
                banks = selected.get("resources", {}).get("banks", [])
                if not banks:
                    raise ValueError("目标槽位没有可编辑的颜色资源描述符")
                resources = job["resources"]
                hashes = {key: sha256(bytes.fromhex(resources[key])) for key in ("matrix", "gamma", "multi")}
                for bank in banks:
                    for source, target in (("matrix", "matrix"), ("gamma", "gamma"), ("multi", "multi_main")):
                        bank[target].update(hex=resources[source], sha256=hashes[source])
                    bank.setdefault("calibration", {}).update(
                        source_xmp_sha256=job.get("xmp_sha256"),
                        render_engine=job.get("render_engine", "offline"), approximate=True,
                        target_approximate=job.get("target_approximate", job.get("render_engine", "offline") == "offline"))
                selected["pending_color_conversion"] = False
            elif job.get("status") == "calibration_failed":
                selected["pending_color_conversion"] = False
            self.store.save_project(project)
            return True

    def _calibration_worker(self, project_id, filter_id):
        job = None
        expected_xmp_sha256 = None
        expected_engine = None
        try:
            project = self.store.get_project(project_id)
            selected = next(row for row in project["filters"] if str(row["id"]) == str(filter_id))
            expected_xmp_sha256 = selected["xmp"].get("sha256")
            engine = selected["xmp"].get("render_engine") or "offline"
            expected_engine = engine
            job = self.calibration.create_job(project_id, filter_id, selected["xmp"]["path"], render_engine=engine)
            self._bind_calibration_source(job, project)
            if job.get("status") != "fitted_offline":
                if engine == "offline":
                    job.update(status="rendering_offline", message="正在本地套用 XMP")
                    if not self._publish_calibration(project_id, filter_id, job):
                        return
                    job = self.calibration.start_offline(job["id"])
                elif engine == "lightroom":
                    if job.get("status") != "rendered":
                        job = self.calibration.start_render(job["id"], launch=False)
                        if not self._publish_calibration(project_id, filter_id, job):
                            return
                        job = self.calibration.poll_render(job["id"])
                    if not self._publish_calibration(project_id, filter_id, job):
                        return
                    if job.get("status") not in ("rendered", "fitted_offline"):
                        return  # The UI polls the bridge and queues fitting after its receipt is ready.
                else:
                    raise ValueError("未支持的校色渲染链路")
                if job.get("status") != "fitted_offline":
                    job.update(status="fitting", message="正在拟合机内颜色资源")
                    if not self._publish_calibration(project_id, filter_id, job):
                        return
                    job = self.calibration.fit_job(job["id"])
            self._publish_calibration(project_id, filter_id, job)
        except Exception as exc:
            if job is not None:
                job.update(status="calibration_failed", message="自动校色失败：" + str(exc))
                if job.get("id"):
                    try:
                        write_json(self.store.root / "calibration/jobs" / job["id"] / "job.json", job)
                    except OSError:
                        traceback.print_exc()
                try:
                    self._publish_calibration(project_id, filter_id, job)
                except Exception:
                    traceback.print_exc()
            else:
                with self.store.lock:
                    project = self.store.get_project(project_id)
                    selected = next((row for row in project["filters"] if str(row["id"]) == str(filter_id)), None)
                    if (selected and selected.get("xmp") and
                            selected["xmp"].get("sha256") == expected_xmp_sha256 and
                            (selected["xmp"].get("render_engine") or "offline") == expected_engine):
                        selected["calibration"] = {**(selected.get("calibration") or {}),
                                                   "status": "calibration_failed",
                                                   "message": "自动校色失败：" + str(exc)}
                        selected["pending_color_conversion"] = False
                        self.store.save_project(project)
        finally:
            replacement = False
            with self.store.lock:
                self._calibration_workers.discard((str(project_id), str(filter_id)))
                try:
                    project = self.store.get_project(project_id)
                    selected = next((row for row in project["filters"] if str(row["id"]) == str(filter_id)), None)
                    replacement = bool(selected and selected.get("xmp") and selected.get("pending_color_conversion")
                                       and (selected["xmp"].get("sha256") != (job or {}).get("xmp_sha256", expected_xmp_sha256)
                                            or (selected.get("calibration") or {}).get("id") != (job or {}).get("id")))
                except (OSError, ValueError, KeyError):
                    pass
            if replacement:
                self._queue_calibration(project_id, filter_id)
