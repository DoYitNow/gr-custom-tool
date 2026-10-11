"""Calibration lifecycle failures and LR waiting without an Adobe process."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gr4_editor.calibration_workflow import CalibrationWorkflow
from gr4_editor.store import Store, sha256


class WorkflowHost(CalibrationWorkflow):
    def __init__(self, root):
        self.store = Store(root)
        self.calibration = Mock()
        self.calibration.status.return_value = {}
        self._calibration_workers = set()


class CalibrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.host = WorkflowHost(directory.name)
        self.job = {"id": "job-current", "xmp_sha256": "current", "render_engine": "offline",
                    "status": "awaiting_offline"}
        self.host.store.save_project({"id": "project-test", "input_path": "test.bin", "input": {"sha256": "firmware"},
                                      "filters": [{"id": "filter-test", "name": "Latest name",
                                                   "xmp": {"sha256": "current", "path": "preset.xmp", "render_engine": "offline"},
                                                   "calibration": deepcopy(self.job), "pending_color_conversion": True,
                                                   "resources": {"banks": [{"matrix": {"hex": "00"}, "gamma": {"hex": "00"},
                                                                            "multi_main": {"hex": "00"}} for _ in range(3)]}}]})

    def row(self):
        return self.host.store.get_project("project-test")["filters"][0]

    def save_row(self, row):
        project = self.host.store.get_project("project-test")
        project["filters"][0] = row
        self.host.store.save_project(project)

    def test_calibration_output_reads_the_actual_fit_directory(self):
        job_id = "ab" * 12
        folder = self.host.store.root / "calibration" / "jobs" / job_id
        fit = folder / "fit"
        fit.mkdir(parents=True)
        for filename in ("preview.jpg", "target.jpeg", "plot.png", "fit-report.json"):
            with self.subTest(filename=filename):
                (folder / filename).write_bytes(b"wrong directory")
                (fit / filename).write_bytes(b"fit output")
                target = self.host.calibration_output(job_id, filename)
                self.assertEqual(target, fit / filename)
                self.assertEqual(target.read_bytes(), b"fit output")

    def test_calibration_output_rejects_missing_unsafe_and_unsupported_files(self):
        job_id = "ab" * 12
        folder = self.host.store.root / "calibration" / "jobs" / job_id
        fit = folder / "fit"
        fit.mkdir(parents=True)
        (folder / "preview.jpg").write_bytes(b"not a fit result")
        (fit / "command.exe").write_bytes(b"unsupported")
        for filename in ("preview.jpg", "missing.json", "command.exe"):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(ValueError, "尚无该校色输出"):
                    self.host.calibration_output(job_id, filename)
        for filename in ("../preview.jpg", "..\\preview.jpg", "fit/preview.jpg", ""):
            with self.subTest(filename=filename):
                with self.assertRaisesRegex(ValueError, "标识无效"):
                    self.host.calibration_output(job_id, filename)
        for bad_id in (None, "ab" * 11, "g" * 24, "AB" * 12, "../" + "a" * 21):
            with self.subTest(job_id=bad_id):
                with self.assertRaisesRegex(ValueError, "标识无效"):
                    self.host.calibration_output(bad_id, "preview.jpg")

    def test_fit_updates_all_bank_hashes_and_reports_same_hashes_to_browser(self):
        self.job.update(status="fitted_offline", resources={"matrix": "0102", "gamma": "0304", "multi": "0506"})
        self.assertTrue(self.host._publish_calibration("project-test", "filter-test", self.job))
        row = self.row()
        self.assertEqual(row["name"], "Latest name")
        self.assertFalse(row["pending_color_conversion"])
        for bank in row["resources"]["banks"]:
            self.assertEqual(bank["matrix"]["sha256"], sha256(b"\x01\x02"))
            self.assertEqual(bank["gamma"]["sha256"], sha256(b"\x03\x04"))
            self.assertEqual(bank["multi_main"]["sha256"], sha256(b"\x05\x06"))
            self.assertTrue(bank["calibration"]["approximate"])
            self.assertTrue(bank["calibration"]["target_approximate"])
        state = self.host.calibration_state("project-test", "filter-test")
        self.assertEqual(state["resource_sha256"]["matrix"], row["resources"]["banks"][0]["matrix"]["sha256"])

    def test_job_creation_failure_is_persisted_and_releases_worker(self):
        self.host.calibration.create_job.side_effect = ValueError("unsupported XMP")
        self.host._calibration_workers.add(("project-test", "filter-test"))
        with patch.object(self.host, "_queue_calibration") as queue:
            self.host._calibration_worker("project-test", "filter-test")
        queue.assert_not_called()
        row = self.row()
        self.assertEqual(row["calibration"]["status"], "calibration_failed")
        self.assertIn("unsupported XMP", row["calibration"]["message"])
        self.assertFalse(row["pending_color_conversion"])
        self.assertEqual(row["resources"]["banks"][0]["matrix"]["hex"], "00")
        self.assertFalse(self.host._calibration_workers)

    def test_replacement_during_failed_creation_keeps_new_xmp_and_queues_it(self):
        def replace_then_fail(*_args, **_kwargs):
            row = self.row()
            row["xmp"]["sha256"] = "replacement"
            row["calibration"]["id"] = "job-new"
            self.save_row(row)
            raise ValueError("old task failed")
        self.host.calibration.create_job.side_effect = replace_then_fail
        with patch.object(self.host, "_queue_calibration") as queue:
            self.host._calibration_worker("project-test", "filter-test")
        queue.assert_called_once_with("project-test", "filter-test")
        self.assertEqual(self.row()["calibration"]["status"], "awaiting_offline")
        self.assertTrue(self.row()["pending_color_conversion"])

    def test_stale_worker_exits_before_render_and_requeues_current_job(self):
        self.host.calibration.create_job.return_value = {**self.job, "id": "job-old", "xmp_sha256": "old"}
        with patch.object(self.host, "_queue_calibration") as queue:
            self.host._calibration_worker("project-test", "filter-test")
        self.host.calibration.start_offline.assert_not_called()
        self.host.calibration.fit_job.assert_not_called()
        queue.assert_called_once_with("project-test", "filter-test")

    def test_engine_switch_during_failed_creation_keeps_same_xmp_new_job_and_requeues_it(self):
        def switch_engine_then_fail(*_args, **_kwargs):
            row = self.row()
            row["xmp"]["render_engine"] = "lightroom"
            row["calibration"].update(id="job-lightroom", render_engine="lightroom", status="awaiting_lightroom")
            self.save_row(row)
            raise ValueError("offline task failed")
        self.host.calibration.create_job.side_effect = switch_engine_then_fail
        self.host._calibration_workers.add(("project-test", "filter-test"))
        with patch.object(self.host, "_queue_calibration") as queue:
            self.host._calibration_worker("project-test", "filter-test")
        queue.assert_called_once_with("project-test", "filter-test")
        row = self.row()
        self.assertEqual(row["xmp"]["sha256"], "current")
        self.assertEqual(row["calibration"]["status"], "awaiting_lightroom")
        self.assertEqual(row["calibration"]["id"], "job-lightroom")
        self.assertTrue(row["pending_color_conversion"])
        self.assertFalse(self.host._calibration_workers)

    def test_cached_fit_clears_pending_only_after_its_bank_resources_are_published(self):
        self.job.update(status="fitted_offline", resources={"matrix": "0102", "gamma": "0304", "multi": "0506"})
        row = self.row()
        row["calibration"] = deepcopy(self.job)
        self.save_row(row)
        self.assertTrue(self.row()["pending_color_conversion"])
        self.assertEqual(self.row()["resources"]["banks"][0]["matrix"]["hex"], "00")
        self.host.calibration.create_job.return_value = deepcopy(self.job)
        self.host._calibration_worker("project-test", "filter-test")
        self.host.calibration.start_offline.assert_not_called()
        self.host.calibration.fit_job.assert_not_called()
        row = self.row()
        self.assertFalse(row["pending_color_conversion"])
        for bank in row["resources"]["banks"]:
            self.assertEqual(bank["matrix"]["hex"], "0102")
            self.assertEqual(bank["matrix"]["sha256"], sha256(b"\x01\x02"))

    def test_saved_browser_draft_keeps_completed_colors_and_its_own_other_edits(self):
        stale = self.row()
        stale.update(name="Browser rename", enabled=False, parameters=[{"id": "Saturation", "default": 7}])
        stale["resources"]["icon"] = {"hex": "edited-icon"}
        self.job.update(status="fitted_offline", resources={"matrix": "0102", "gamma": "0304", "multi": "0506"})
        self.host._publish_calibration("project-test", "filter-test", self.job)
        recent = self.row()
        self.assertTrue(self.host._preserve_completed_calibration(stale, recent))
        self.assertEqual(stale["calibration"], recent["calibration"])
        self.assertFalse(stale["pending_color_conversion"])
        self.assertEqual(stale["resources"]["banks"], recent["resources"]["banks"])
        self.assertEqual(stale["name"], "Browser rename")
        self.assertFalse(stale["enabled"])
        self.assertEqual(stale["parameters"], [{"id": "Saturation", "default": 7}])
        self.assertEqual(stale["resources"]["icon"], {"hex": "edited-icon"})
        stale["calibration"]["message"] = "changed draft"
        self.assertNotIn("message", recent["calibration"])

    def test_completed_fit_does_not_overwrite_another_engine_xmp_or_task(self):
        stale = self.row()
        self.job.update(status="fitted_offline", resources={"matrix": "0102", "gamma": "0304", "multi": "0506"})
        self.host._publish_calibration("project-test", "filter-test", self.job)
        recent = self.row()
        for changed in ("engine", "xmp", "task"):
            with self.subTest(changed=changed):
                row = deepcopy(stale)
                if changed == "engine":
                    row["xmp"]["render_engine"] = "lightroom"
                elif changed == "xmp":
                    row["xmp"]["sha256"] = "replacement"
                else:
                    row["calibration"]["id"] = "job-new"
                unchanged = deepcopy(row)
                self.assertFalse(self.host._preserve_completed_calibration(row, recent))
                self.assertEqual(row, unchanged)

    def test_cached_fit_not_yet_applied_cannot_clear_saved_draft_pending(self):
        row = self.row()
        recent = self.row()
        recent["calibration"].update(status="fitted_offline", resources={"matrix": "01", "gamma": "02", "multi": "03"})
        unchanged = deepcopy(row)
        self.assertFalse(self.host._preserve_completed_calibration(row, recent))
        self.assertEqual(row, unchanged)

    def prepare_lightroom(self):
        self.job.update(render_engine="lightroom", status="awaiting_lightroom")
        row = self.row()
        row["xmp"]["render_engine"] = "lightroom"
        row["calibration"] = deepcopy(self.job)
        self.save_row(row)
        return deepcopy(self.job)

    def test_lightroom_worker_queues_without_launching_and_exits_while_waiting(self):
        job = self.prepare_lightroom()
        self.host.calibration.create_job.return_value = job
        self.host.calibration.start_render.return_value = deepcopy(job)
        self.host.calibration.poll_render.return_value = deepcopy(job)
        self.host._calibration_workers.add(("project-test", "filter-test"))
        with patch.object(self.host, "_queue_calibration") as queue:
            self.host._calibration_worker("project-test", "filter-test")
        self.host.calibration.start_render.assert_called_once_with("job-current", launch=False)
        self.host.calibration.fit_job.assert_not_called()
        queue.assert_not_called()
        self.assertTrue(self.row()["pending_color_conversion"])
        self.assertFalse(self.host._calibration_workers)

    def test_lightroom_receipt_poll_queues_fitting(self):
        job = self.prepare_lightroom()
        self.host.calibration.poll_render.return_value = {**job, "status": "rendered"}
        with patch.object(self.host, "_queue_calibration") as queue:
            state = self.host.calibration_state("project-test", "filter-test")
        queue.assert_called_once_with("project-test", "filter-test")
        self.assertEqual(state["calibration"]["status"], "rendered")
        self.assertTrue(state["pending"])

    def test_lightroom_rejected_receipt_becomes_failed_without_stale_resources(self):
        self.prepare_lightroom()
        self.host.calibration.poll_render.side_effect = ValueError("receipt nonce mismatch")
        state = self.host.calibration_state("project-test", "filter-test")
        self.assertEqual(state["calibration"]["status"], "calibration_failed")
        self.assertFalse(state["pending"])
        self.assertEqual(self.row()["resources"]["banks"][0]["matrix"]["hex"], "00")

    def test_rendered_lightroom_job_fits_without_restarting_its_request(self):
        job = self.prepare_lightroom()
        job["status"] = "rendered"
        self.host.calibration.create_job.return_value = job
        self.host.calibration.fit_job.return_value = {**job, "status": "fitted_offline", "target_approximate": False,
                                                       "resources": {"matrix": "01", "gamma": "02", "multi": "03"}}
        self.host._calibration_worker("project-test", "filter-test")
        self.host.calibration.start_render.assert_not_called()
        self.host.calibration.fit_job.assert_called_once_with("job-current")
        bank = self.row()["resources"]["banks"][0]
        self.assertEqual(bank["calibration"]["render_engine"], "lightroom")
        self.assertTrue(bank["calibration"]["approximate"])
        self.assertFalse(bank["calibration"]["target_approximate"])


if __name__ == "__main__":
    unittest.main()
