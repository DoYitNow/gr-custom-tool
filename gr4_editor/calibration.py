"""XMP material inventory and reusable camera calibration jobs."""

from datetime import datetime, timezone
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import base64
import tempfile
import threading
import xml.etree.ElementTree as ET

from .bootstrap import DATA_ROOT
from . import calibration_assets
from .store import write_json, sha256

CRS = "http://ns.adobe.com/camera-raw-settings/1.0/"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
BUNDLED_CALIBRATION_ROOT = calibration_assets.ASSET_ROOT
BUNDLED_CALIBRATION_MANIFEST = BUNDLED_CALIBRATION_ROOT / "samples.json"


def _zero_setting(value):
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def inspect_xmp(data):
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("XMP 不是有效的 XML 文件") from exc
    descriptions = list(root.iter("{" + RDF + "}Description"))
    if not descriptions:
        raise ValueError("XMP 缺少 RDF Description")
    fields = {}
    sequences = {}
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.startswith("{" + CRS + "}"):
                fields[key.split("}", 1)[1]] = value
        if node.tag.startswith("{" + CRS + "}"):
            key = node.tag.split("}", 1)[1]
            values = [child.text for child in node.iter("{" + RDF + "}li") if child.text]
            if values:
                sequences[key] = values
            elif node.text and node.text.strip():
                fields[key] = node.text.strip()
    if not fields and not sequences:
        raise ValueError("文件没有 Adobe Camera Raw 设置")
    names = []
    for name in list(fields) + list(sequences):
        if name == "SharpenEdgeMasking" and (_zero_setting(fields.get(name)) or _zero_setting(fields.get("Sharpness"))):
            continue  # Zero masking or disabled sharpening has no edge-mask effect.
        if any(word in name for word in ("Mask", "Correction", "Retouch", "LensBlur")):
            names.append(name)
    unsupported = sorted(set(names))
    reference = None
    for look in root.iter("{" + CRS + "}Look"):
        description = look.find("{" + RDF + "}Description")
        if description is not None and description.get("{" + CRS + "}Stubbed", "").lower() == "true":
            reference = {"name": description.get("{" + CRS + "}Name"),
                         "uuid": description.get("{" + CRS + "}UUID")}
            break
    profile = fields.get("PresetType") == "Look" or any(key.startswith("RGBTable") or key.startswith("LookTable") for key in fields)
    reference_only = bool(reference and not profile)
    message = "已登记 XMP；等待标准样本进行颜色转换"
    if unsupported:
        message = "含局部或内容相关调整，当前机内颜色模型不能完整表达"
    elif reference_only:
        message = (f"预设仅引用颜色配置文件「{reference['name'] or '未命名'}」，未包含颜色数据；"
                   f"请提供对应完整 Look/Profile XMP（UUID：{reference['uuid'] or '未知'}）")
    return {
        "sha256": sha256(data), "kind": "profile" if profile else "develop_preset",
        "uuid": fields.get("UUID"), "camera_profile": fields.get("CameraProfile"),
        "process_version": fields.get("ProcessVersion"), "fields": fields,
        "sequences": sequences, "unsupported_fields": unsupported, "look_reference": reference,
        "renderable": not unsupported and not reference_only,
        "message": message,
    }


class Calibration:
    def __init__(self, root=DATA_ROOT):
        self.root = Path(root).resolve()
        self.config_path = self.root / "calibration" / "dataset.json"
        self.dataset_error = ""
        self._dataset_lock = threading.RLock()

    def _bootstrap_bundled_dataset(self):
        """Copy the optional checked-in sample set into user data once.

        A user's existing ``dataset.json`` is always authoritative.  The
        bundled set is only a first-run convenience for a fresh data root;
        it never replaces a configured or even a broken user dataset.
        """
        try:
            if self.config_path.exists() or not BUNDLED_CALIBRATION_MANIFEST.is_file():
                return False
            manifest = json.loads(BUNDLED_CALIBRATION_MANIFEST.read_text(encoding="utf-8"))
            rows = manifest["samples"]
            heldout = Path(str(manifest["heldout"])).stem
            if (manifest.get("schema_version") != 1 or
                    manifest.get("kind") != "bundled-gr-iv-calibration-samples" or
                    not isinstance(rows, list) or len(rows) < 3 or
                    len({Path(str(row["name"])).stem for row in rows}) != len(rows) or
                    heldout not in {Path(str(row["name"])).stem for row in rows}):
                raise ValueError("内置样片清单格式无效")
            asset_root = BUNDLED_CALIBRATION_ROOT.resolve()
            destination_root = self.root / "calibration" / "samples"
            copied = []
            for row in rows:
                raw_name = str(row["name"])
                name = Path(raw_name)
                relative = Path(str(row["path"]))
                if (name.name != raw_name or name.suffix.lower() != ".dng" or
                        relative.is_absolute() or ".." in relative.parts):
                    raise ValueError("内置样片文件名无效")
                source = (asset_root / relative).resolve()
                if asset_root not in source.parents or not source.is_file():
                    raise ValueError("缺少内置样片：" + name.name)
                expected = str(row["sha256"]).lower()
                if (len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected) or
                        row.get("role") != ("holdout" if name.stem == heldout else "development")):
                    raise ValueError("内置样片清单校验无效：" + name.name)
                data = source.read_bytes()
                if sha256(data) != expected:
                    raise ValueError("内置样片 SHA256 不匹配：" + name.name)
                destination = destination_root / (expected + ".DNG")
                if destination.exists():
                    if sha256(destination.read_bytes()) != expected:
                        raise ValueError("用户样片副本 SHA256 不匹配：" + name.name)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary_path = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as temporary:
                            temporary_path = Path(temporary.name)
                            temporary.write(data)
                        temporary_path.replace(destination)
                    finally:
                        if temporary_path is not None:
                            temporary_path.unlink(missing_ok=True)
                copied_row = deepcopy(row)
                copied_row["path"] = str(destination)
                copied_row["name"] = name.name
                copied.append(copied_row)
            record = {
                "schema_version": 1,
                "heldout": heldout,
                "samples": copied,
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "camera": manifest.get("camera", "RICOH GR IV"),
                "source": "bundled",
                "source_description": manifest.get("source", "仓库内置校色样片"),
            }
            # A unique temporary file keeps the initial dataset atomic even
            # if another editor process is reading the same user directory.
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=self.config_path.parent, suffix=".tmp", mode="w",
                                                 encoding="utf-8", delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                    json.dump(record, temporary, ensure_ascii=False, indent=2)
                    temporary.write("\n")
                if self.config_path.exists():
                    return False
                temporary_path.replace(self.config_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            self.dataset_error = "内置校色样片未就绪：" + str(exc)
            return False

    def dataset(self):
        with self._dataset_lock:
            return self._dataset()

    def _dataset(self):
        self.dataset_error = ""
        # A checked-in sample set is opt-in by its presence in the product
        # assets, and only initializes a brand-new user data directory.
        self._bootstrap_bundled_dataset()
        if self.config_path.is_file():
            try:
                record = json.loads(self.config_path.read_text(encoding="utf-8"))
                rows = record["samples"]
                stems = [Path(row["name"]).stem for row in rows]
                if len(rows) < 3 or len(set(stems)) != len(stems) or record["heldout"] not in stems:
                    raise ValueError("至少需要三张不同的 DNG，并指定独立检查样片")
                for row in rows:
                    if not Path(row["path"]).is_file():
                        raise ValueError("找不到已登记样片：" + row["name"])
                return record
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.dataset_error = "自备样片未就绪：" + str(exc)
                return None
        if not self.dataset_error:
            self.dataset_error = "请先选择至少 3 张 DNG，并指定独立检查样片"
        return None

    def register(self, directory, heldout):
        directory = Path(directory).expanduser().resolve()
        files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix.lower() == ".dng")
        if len(files) < 3:
            raise ValueError("标准样本集至少需要三张 DNG，并预先指定一张独立检查样片")
        if heldout not in [path.stem for path in files]:
            raise ValueError("独立检查样片必须来自该 DNG 样本集")
        rows = []
        for path in files:
            metadata = read_dng_metadata(path)
            if "RICOH" not in metadata["make"].upper() or "GR IV" not in metadata["model"].upper():
                raise ValueError(f"{path.name} 不是支持的相机 DNG")
            data = path.read_bytes()
            destination = self.root / "calibration" / "samples" / (sha256(data) + ".DNG")
            rows.append({"name": path.name, "path": str(destination), "sha256": sha256(data), "role": "holdout" if path.stem == heldout else "development", "metadata": metadata})
        with self._dataset_lock:
            for path, row in zip(files, rows):
                destination = Path(row["path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    destination.write_bytes(path.read_bytes())
            result = {"schema_version": 1, "heldout": heldout, "samples": rows, "registered_at": datetime.now(timezone.utc).isoformat(), "camera": "RICOH GR IV"}
            write_json(self.config_path, result)
        return result

    def register_uploads(self, samples, heldout):
        if not isinstance(samples, list) or len(samples) < 3:
            raise ValueError("至少选择 3 张 DNG")
        names = [row["name"] for row in samples]
        if (len(set(names)) != len(names) or
                any(Path(name).name != name or "\\" in name or Path(name).suffix.lower() != ".dng" for name in names)):
            raise ValueError("样片须为不同名称的 DNG 文件")
        with tempfile.TemporaryDirectory(prefix="firmware-editor-dng-") as folder:
            for row in samples:
                try:
                    data = base64.b64decode(row["base64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("DNG 上传内容无效") from exc
                (Path(folder) / row["name"]).write_bytes(data)
            return self.register(folder, heldout)

    def status(self):
        from .lightroom_bridge import status as lightroom_status
        dataset = self.dataset()
        dependencies = {}
        for name in ("rawpy", "numpy", "cv2", "PIL"):
            try:
                importlib.import_module(name)
                dependencies[name] = True
            except (ImportError, OSError, ValueError, RuntimeError):
                dependencies[name] = False
        assets = calibration_assets.status()
        available = all(dependencies.values()) and assets["ready"]
        bridge = lightroom_status(self.root)
        lightroom_available = bool(dataset and available and bridge["classic_installed"])
        message = (self.dataset_error if not dataset else
                   "缺少本地颜色转换依赖，请重新运行启动器" if not all(dependencies.values()) else
                   assets["message"] if not assets["ready"] else "本地转换已就绪")
        return {"dataset_ready": bool(dataset), "samples": len(dataset["samples"]) if dataset else 0,
                "heldout": dataset["heldout"] if dataset else None,
                "sample_names": [row["name"] for row in dataset["samples"]] if dataset else [],
                "dataset_source": (dataset.get("source", "custom") if dataset else None),
                "dependencies": dependencies, "materials": assets,
                "ready": bool(dataset and available), "offline_available": bool(dataset and available),
                "default_render_engine": "offline", "message": message,
                "lightroom_available": lightroom_available,
                "lightroom_ready": bool(lightroom_available and bridge["lightroom_bridge"] == "connected"),
                "classic_installed": bridge["classic_installed"], "classic": bridge["classic"],
                "lightroom_bridge": bridge["lightroom_bridge"], "bridge_status": bridge["bridge_status"],
                "bridge_validated": bridge["bridge_validated"], "lightroom": bridge}

    def _job(self, job_id):
        if len(job_id) != 24 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("校色任务标识无效")
        folder = self.root / "calibration/jobs" / job_id
        return folder, json.loads((folder / "job.json").read_text(encoding="utf-8"))

    def start_render(self, job_id, launch=False):
        """Queue this job for Lightroom Classic; leave launching Classic to the user."""
        from .lightroom_bridge import (NATIVE_RENDER_RECIPE_VERSION, install_plugin,
                                       prepare_request, complete_request)
        folder, job = self._job(job_id)
        if job.get("render_engine") != "lightroom":
            raise ValueError("本地转换任务不能提交到 Lightroom")
        if job.get("status") == "fitted_offline":
            return job
        pending = folder / "render-request.json"
        result = folder / "render-result.json"

        def archive_attempt():
            previous = json.loads(pending.read_text(encoding="utf-8"))
            nonce = str(previous.get("nonce") or "")
            if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
                nonce = sha256(pending.read_bytes())[:24]
            archive = folder / "attempts" / nonce
            archive.mkdir(parents=True, exist_ok=True)
            for name in ("render-request.json", "render-request.lua", "render-result.json", "render-receipt.json"):
                path = folder / name
                if path.is_file():
                    path.replace(archive / name)

        if pending.is_file():
            previous = json.loads(pending.read_text(encoding="utf-8"))
            if previous.get("render_recipe_version") != NATIVE_RENDER_RECIPE_VERSION:
                archive_attempt()
            elif result.is_file():
                try:
                    receipt = complete_request(self.root, job)
                except ValueError:
                    # Preserve the rejected output for diagnosis; the next
                    # request uses a fresh nonce and fresh export directory.
                    archive_attempt()
                else:
                    if receipt:
                        job.update(status="rendered", message="Lightroom 目标已校验，等待软件域拟合",
                                   render_recipe_version=receipt["render_recipe_version"],
                                   render_nonce=receipt["nonce"],
                                   render_boundary=receipt["boundary"], target_approximate=False,
                                   approximate=True)
                        write_json(folder / "job.json", job)
                        return job
        install_plugin(self.root)
        request = prepare_request(self.root, job)
        job.update(status="awaiting_lightroom", message="已提交 Lightroom Classic 渲染队列，请打开 Classic 并加载插件",
                   render_nonce=request["nonce"], render_recipe_version=NATIVE_RENDER_RECIPE_VERSION,
                   approximate=True)
        write_json(folder / "job.json", job)
        return job

    def poll_render(self, job_id):
        """Advance a queued Lightroom job after the bridge exports and validates JPEGs."""
        from .lightroom_bridge import complete_request, status as lightroom_status
        folder, job = self._job(job_id)
        if job.get("render_engine") != "lightroom":
            raise ValueError("本地转换任务没有 Lightroom 渲染队列")
        if job.get("status") in ("rendered", "fitted_offline"):
            return job
        receipt = complete_request(self.root, job)
        if receipt:
            job.update(status="rendered", message="Lightroom 已套用 XMP 并导出全部目标 JPEG，等待软件域拟合",
                       render_recipe_version=receipt["render_recipe_version"],
                       render_nonce=receipt["nonce"],
                       render_boundary=receipt["boundary"], target_approximate=False,
                       approximate=True)
        elif lightroom_status(self.root)["lightroom_bridge"] == "connected":
            job.update(status="rendering_lightroom", message="Lightroom 桥接已连接，正在渲染样片")
        else:
            job.update(status="awaiting_lightroom", message="等待 Lightroom Classic 加载插件并渲染样片")
        write_json(folder / "job.json", job)
        return job



    def start_offline(self, job_id):
        """Render the supplied Look locally; no Adobe process or settings writes."""
        from .offline_look import inspect_support, load_base_profile
        from .offline_raw import render_target
        from .fitting import _numeric_backend
        folder, job = self._job(job_id)
        if job.get("render_engine") != "offline":
            raise ValueError("这不是本地转换任务")
        if job.get("status") == "fitted_offline":
            return job
        data = Path(job["xmp_path"]).read_bytes()
        if sha256(data) != job["xmp_sha256"]:
            raise ValueError("本地转换的 XMP 哈希已变化")
        base_profile = load_base_profile()
        support = inspect_support(data, base_profile=base_profile)
        if not support["supported"]:
            raise ValueError(support["reason"])
        width = int(job.get("fit_width", 1550))
        if width < 64:
            raise ValueError("本地转换尺寸太小")
        backend = _numeric_backend()
        rows = []
        for sample in job["dataset"]["samples"]:
            stem = Path(sample["name"]).stem
            path = Path(job["photos"]) / (stem + ".DNG")
            camera = backend.camera_metadata(path)
            if camera["sha256"] != sample["sha256"]:
                raise ValueError("本地转换的 DNG 哈希已变化：" + stem)
            linear, geometry = backend.render_dng(path, width, camera["baseline_exposure_ev"])
            target, target_geometry = render_target(
                data, path, width, camera["baseline_exposure_ev"], backend=backend,
                base_profile=base_profile)
            source_path = folder / "photos" / (stem + ".linear.npy")
            target_path = folder / "photos" / (stem + ".target.npy")
            backend.np.save(source_path, linear.astype(backend.np.float32), allow_pickle=False)
            backend.np.save(target_path, target.astype(backend.np.float32), allow_pickle=False)
            rows.append({"stem": stem, "dng_sha256": camera["sha256"],
                         "linear_file": source_path.name, "linear_sha256": sha256(source_path.read_bytes()),
                         "target_file": target_path.name, "target_sha256": sha256(target_path.read_bytes()),
                         "shape": list(target.shape), "geometry": geometry,
                         "target_raw_geometry": target_geometry})
        receipt = {"schema_version": 1, "status": "rendered", "job_id": job_id,
                   "render_engine": "offline", "render_recipe_version": support["recipe_version"],
                   "xmp_sha256": job["xmp_sha256"], "width": width, "samples": rows,
                   "application": {"name": "固件编辑器本地 Look 转换器", "version": support["recipe_version"]},
                   "support": support, "approximate": True,
                   "boundary": "Local LibRaw/Look approximation; not Lightroom or measured camera output."}
        write_json(folder / "render-receipt.json", receipt)
        job.update(status="rendered", message="本地目标已生成，等待拟合机内颜色资源",
                   render_recipe_version=support["recipe_version"], render_boundary=receipt["boundary"],
                   target_approximate=True, approximate=True)
        write_json(folder / "job.json", job)
        return job

    def create_job(self, project_id, filter_id, xmp_path, render_engine="offline"):
        if render_engine not in ("offline", "lightroom"):
            raise ValueError("颜色转换方式无效")
        xmp_data = Path(xmp_path).read_bytes()
        inventory = inspect_xmp(xmp_data)
        if not inventory["renderable"]:
            fields = inventory["unsupported_fields"]
            raise ValueError(inventory["message"] + ("：" + ", ".join(fields) if fields else ""))
        # Validate the selected renderer before returning an ``awaiting_dng``
        # job, otherwise unsupported XMP would fail later in a worker.
        if render_engine == "offline":
            from .offline_look import inspect_support, load_base_profile
            support = inspect_support(xmp_data, base_profile=load_base_profile())
            if not support["supported"]:
                raise ValueError(support["reason"])
            render_recipe_version = support["recipe_version"]
        else:
            from .lightroom_bridge import NATIVE_RENDER_RECIPE_VERSION, profile_settings
            profile_settings(xmp_data)
            render_recipe_version = NATIVE_RENDER_RECIPE_VERSION
        dataset = self.dataset()
        if not dataset:
            return {"status": "awaiting_dng", "message": "XMP 已保存。" + self.dataset_error,
                    "xmp_sha256": inventory["sha256"], "render_engine": render_engine}
        from .color_fit import FIT_RECIPE_VERSION
        identity = project_id + filter_id + inventory["sha256"] + json.dumps(dataset, sort_keys=True)
        identity += f"\0native-fit-v{FIT_RECIPE_VERSION}\0"
        try:
            _, gamma = calibration_assets.load_gamma_base()
        except (OSError, ValueError, TypeError):
            gamma = {}  # Material-free routing fixtures can still create jobs.
        if gamma.get("base_curve_sha256"):
            identity += "\0native-gamma-" + gamma["base_curve_sha256"] + "\0"
        if render_engine == "offline":
            identity += f"\0offline-look-v{support['recipe_version']}\0" + sha256(json.dumps(support, sort_keys=True).encode())
        else:
            identity += f"\0native-render-v{NATIVE_RENDER_RECIPE_VERSION}\0"
        job_id = sha256(identity.encode())[:24]
        folder = self.root / "calibration" / "jobs" / job_id
        if (folder / "job.json").is_file():
            return json.loads((folder / "job.json").read_text(encoding="utf-8"))
        folder.mkdir(parents=True, exist_ok=True)
        inputs = folder / "photos"
        inputs.mkdir(exist_ok=True)
        for row in dataset["samples"]:
            source = Path(row["path"])
            data = source.read_bytes()
            if sha256(data) != row["sha256"]:
                raise ValueError("标准样本已变化，请重新登记")
            # Historical numerical routines expect uppercase DNG suffixes.
            (inputs / (Path(row["name"]).stem + ".DNG")).write_bytes(data)
        shutil.copyfile(xmp_path, folder / "source.xmp")
        job = {"id": job_id, "project_id": project_id, "filter_id": filter_id,
               "render_engine": render_engine, "fit_recipe_version": FIT_RECIPE_VERSION,
               "render_recipe_version": render_recipe_version,
               "status": "awaiting_offline" if render_engine == "offline" else "awaiting_lightroom",
               "message": "等待本地颜色转换" if render_engine == "offline" else "等待 Lightroom Classic 渲染标准样片",
               "xmp_sha256": inventory["sha256"], "inventory": inventory, "photos": str(inputs), "xmp_path": str(folder / "source.xmp"), "output": str(folder / "fit"), "heldout": dataset["heldout"], "dataset": dataset}
        write_json(folder / "job.json", job)
        return job

    def fit_job(self, job_id):
        if len(job_id) != 24 or any(char not in "0123456789abcdef" for char in job_id):
            raise ValueError("校色任务标识无效")
        folder = self.root / "calibration" / "jobs" / job_id
        job = json.loads((folder / "job.json").read_text(encoding="utf-8"))
        receipt_path = folder / "render-receipt.json"
        if not receipt_path.is_file():
            raise ValueError("尚未完成渲染，缺少可拟合的目标材料")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") != "rendered" or receipt.get("xmp_sha256") != job["xmp_sha256"]:
            raise ValueError("渲染记录与当前 XMP 不匹配")
        engine = job.get("render_engine", "offline")
        if receipt.get("render_engine", "offline") != engine:
            raise ValueError("渲染记录的转换方式与任务不匹配")
        if receipt.get("job_id") != job_id or receipt.get("render_recipe_version") != job.get("render_recipe_version"):
            raise ValueError("渲染回执与校色任务或配方版本不匹配")
        if engine == "lightroom" and receipt.get("nonce") != job.get("render_nonce"):
            raise ValueError("Lightroom 渲染回执与本次请求不匹配")
        for sample in job["dataset"]["samples"]:
            suffix = ".target.npy" if engine == "offline" else ".jpg"
            target = Path(job["photos"]) / (Path(sample["name"]).stem + suffix)
            if not target.is_file():
                raise ValueError("缺少目标材料：" + target.name)
        # Current fitter explicitly validates Look UUID/table/capture pairing.
        # A develop-only preset needs a separate, validated adapter; do not
        # silently feed it through the profile-specific historical fitter.
        if job["inventory"]["kind"] != "profile":
            raise ValueError("普通 Develop 预设已登记；通用拟合适配尚待样本验证，不能套用旧 Look 专用拟合器")
        from .fitting import run_job
        try:
            result = run_job(job, receipt)
        except Exception as exc:
            import traceback
            (folder / "fit.log").write_text(traceback.format_exc(), encoding="utf-8")
            job.update(status="fit_failed", message="颜色拟合失败：" + str(exc))
            write_json(folder / "job.json", job)
            raise ValueError(job["message"]) from exc
        job.update(status="fitted_offline",
                   message="已生成本地近似颜色候选" if engine == "offline" else "已用 Lightroom 目标生成软件拟合候选，待实机验证",
                   target_approximate=(engine == "offline"), approximate=True,
                   resources=result["resources"], fit_report=result["fit_report"])
        write_json(folder / "job.json", job)
        validation_file = "offline-validation.json" if engine == "offline" else "bridge-validation.json"
        write_json(self.root / "calibration" / validation_file, {"job_id": job_id, "xmp_sha256": job["xmp_sha256"], "render_engine": engine,
                   "render_recipe_version": receipt.get("render_recipe_version"),
                   "application": receipt.get("application"), "status": "render_and_fit_passed_offline",
                   "boundary": "软件域拟合候选；尚未验证相机实际输出与色彩误差",
                   "validated_at": datetime.now(timezone.utc).isoformat()})
        return job


def read_dng_metadata(path):
    # TIFF/DNG metadata parsing uses the same Pillow implementation as the
    # historical material audit; no image pixels are rewritten here.
    from PIL import Image
    import rawpy
    with Image.open(path) as image:
        tags = image.tag_v2
        result = {"make": str(tags.get(271, "")), "model": str(tags.get(272, "")), "ifd_width": image.width, "ifd_height": image.height}
    with rawpy.imread(str(path)) as raw:
        result.update(raw_width=raw.sizes.raw_width, raw_height=raw.sizes.raw_height,
                      visible_width=raw.sizes.width, visible_height=raw.sizes.height)
    return result
