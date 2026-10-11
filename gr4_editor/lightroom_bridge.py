"""Lightroom Classic installation discovery and hash-bound render requests.

The Lua adapter renders copies through the Classic SDK. Only a newly completed,
validated request is promoted to the JPEG pairs consumed by the fitter.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from .store import sha256, write_json

PLUGIN_NAME = "GR4FirmwareBridge.lrplugin"
CRS = "{http://ns.adobe.com/camera-raw-settings/1.0/}"
RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
IMPORT_LOCK = threading.Lock()
NATIVE_RENDER_RECIPE_VERSION = 3
PLUGIN_VERSION = "0.3.0"
PRODUCT_ASSETS = Path(__file__).with_name("assets") / "lightroom"


def discover_installation():
    """Use installed-product evidence; do not infer Classic from Adobe folders."""
    found = []
    if os.name == "nt":
        import winreg
        keys = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")]
        for hive, key in keys:
            try:
                with winreg.OpenKey(hive, key) as parent:
                    for index in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as entry:
                                def value(name):
                                    try:
                                        return str(winreg.QueryValueEx(entry, name)[0])
                                    except OSError:
                                        return ""
                                name = value("DisplayName")
                                if "lightroom" not in name.lower():
                                    continue
                                location = Path(value("InstallLocation"))
                                icon = value("DisplayIcon").split(",")[0].strip('"')
                                candidates = [location / "Lightroom.exe", location / "Adobe Lightroom Classic/Lightroom.exe"]
                                if icon:
                                    icon_path = Path(icon)
                                    candidates += [icon_path, *[p / "Lightroom.exe" for p in icon_path.parents]]
                                executable = next((p for p in candidates if p.name.lower() == "lightroom.exe" and p.is_file()), None)
                                found.append({"name": name, "kind": "classic" if "classic" in name.lower() else "cloud",
                                              "version": value("DisplayVersion"), "executable": str(executable) if executable else None,
                                              "evidence": "Windows installed-product registry and executable existence"})
                        except OSError:
                            continue
            except OSError:
                continue
    classic = next((row for row in found if row["kind"] == "classic" and row["executable"]), None)
    return {"classic": classic, "installed_products": found, "supported": bool(classic)}


def status(root):
    """Report this product bridge's heartbeat without installing or starting it."""
    installation = discover_installation()
    status_path = Path(root) / "calibration/lightroom-status.json"
    bridge = {}
    connected = False
    try:
        bridge = json.loads(status_path.read_text(encoding="utf-8"))
        connected = bool(bridge.get("status") in ("connected", "rendering")
                         and bridge.get("plugin_version") == PLUGIN_VERSION
                         and 0 <= time.time() - status_path.stat().st_mtime < 30)
    except (OSError, ValueError, TypeError):
        pass
    validated = False
    try:
        validation = json.loads((Path(root) / "calibration/bridge-validation.json").read_text(encoding="utf-8"))
        validated = bool(validation.get("render_engine") == "lightroom"
                         and validation.get("render_recipe_version") == NATIVE_RENDER_RECIPE_VERSION
                         and validation.get("status") == "render_and_fit_passed_offline")
    except (OSError, ValueError, TypeError):
        pass
    return {"classic_installed": installation["supported"], "classic": installation["classic"],
            "lightroom_bridge": "connected" if connected else "not_connected", "bridge": bridge,
            "bridge_validated": validated,
            "bridge_status": "connected" if connected else "installed_not_connected" if installation["supported"] else "classic_not_installed",
            "render_recipe_version": NATIVE_RENDER_RECIPE_VERSION, "plugin_version": PLUGIN_VERSION}


def _lua(value):
    """Serialize data as a Lua table without interpreting XMP as source code."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Lua 5.1 has decimal byte escapes, not JSON \u escapes.
        return '"' + "".join("\\%03d" % byte if byte < 32 or byte in (34, 92) else chr(byte)
                              for byte in value.encode("utf-8")) + '"'
    if isinstance(value, list):
        return "{" + ",".join(_lua(item) for item in value) + "}"
    if isinstance(value, dict):
        return "{" + ",".join("[" + _lua(str(key)) + "]=" + _lua(item) for key, item in value.items()) + "}"
    raise TypeError("Unsupported bridge request value")


def _lua_bytes(value):
    # Non-ASCII bytes above were preserved as codepoints so encode latin-1.
    return ("return " + _lua(value) + "\n").encode("latin-1")


def profile_settings(data):
    """Retain Look payloads and curves; application is verified in Classic."""
    root = ET.fromstring(data)
    description = next((node for node in root.iter(RDF + "Description") if CRS + "PresetType" in node.attrib), None)
    if description is None or description.get(CRS + "PresetType") != "Look":
        raise ValueError("当前自动桥接仅接受 Look/Profile XMP")
    attributes = {key[len(CRS):]: value for key, value in description.attrib.items() if key.startswith(CRS)}
    identifier = attributes.get("UUID")
    if not identifier:
        raise ValueError("Look/Profile 缺少 UUID")
    name_node = description.find(CRS + "Name")
    names = list(name_node.iter(RDF + "li")) if name_node is not None else []
    name = next((node.text for node in names if node.text), identifier)
    metadata = {"PresetType", "Cluster", "UUID", "Copyright", "ContactInfo", "CameraModelRestriction"}
    parameters = {}
    for key, value in attributes.items():
        if key in metadata or key.startswith("Supports") or key.startswith("Requires"):
            continue
        if value in ("True", "False"):
            value = value == "True"
        parameters[key] = value
    for node in description:
        if not node.tag.startswith(CRS):
            continue
        key = node.tag[len(CRS):]
        values = [item.text for item in node.iter(RDF + "li") if item.text]
        if "ToneCurve" in key and values:
            parameters[key] = [float(part.strip()) for value in values for part in value.split(",")]
    baseline = {"CameraProfile": "Adobe Standard", "WhiteBalance": "As Shot", "ProcessVersion": attributes.get("ProcessVersion", "11.0"),
                "Exposure2012": 0, "Contrast2012": 0, "Highlights2012": 0, "Shadows2012": 0,
                "Whites2012": 0, "Blacks2012": 0, "Texture": 0, "Clarity2012": 0,
                "Dehaze": 0, "Vibrance": 0, "Saturation": 0, "ToneCurveName2012": "Linear"}
    for key in ("ToneCurvePV2012", "ToneCurvePV2012Red", "ToneCurvePV2012Green", "ToneCurvePV2012Blue"):
        baseline[key] = [0, 0, 255, 255]
    baseline["Look"] = {"Name": name, "UUID": identifier, "Amount": 1,
                        "SupportsAmount": attributes.get("SupportsAmount") == "True", "Parameters": parameters}
    return {"uuid": identifier, "name": name, "settings": baseline,
            "boundary": "SDK Look table application must pass UUID readback and exported JPEG metadata audit on this installed Classic version"}


def plugin_location():
    if not os.environ.get("APPDATA"):
        raise ValueError("当前环境未提供 Windows APPDATA，不能自动安装 Classic 插件")
    return Path(os.environ["APPDATA"]) / "Adobe/Lightroom/Modules" / PLUGIN_NAME


def install_profile(data):
    """Keep the full profile in Adobe's shared XMP settings directory."""
    profile = profile_settings(data)
    if not os.environ.get("APPDATA"):
        raise ValueError("当前环境未提供 Adobe 用户设置目录")
    folder = Path(os.environ["APPDATA"]) / "Adobe/CameraRaw/Settings/GR4 Firmware Editor"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ("GR4_" + sha256(data)[:24] + ".xmp")
    if not path.is_file() or path.read_bytes() != data:
        path.write_bytes(data)
    return path


def install_render_preset(data):
    """Use Adobe's native XMP parser to load the Look's big-table dictionary.

    A settings table passed to addDevelopPresetForPlugin does not register a
    previously unseen RGBTable. A native Normal preset referencing the installed
    profile does; its SDK ID can differ from the XML UUID, so lookup uses its file.
    """
    profile = profile_settings(data)
    source = ET.fromstring(data)
    original = next(node for node in source.iter(RDF + "Description") if CRS + "PresetType" in node.attrib)
    root = ET.Element("{adobe:ns:meta/}xmpmeta")
    description = ET.SubElement(ET.SubElement(root, RDF + "RDF"), RDF + "Description", {RDF + "about": ""})
    identifier = sha256(f"GR4 native render preset v{NATIVE_RENDER_RECIPE_VERSION}\0".encode() + data)[:32].upper()
    description.set(CRS + "PresetType", "Normal")
    description.set(CRS + "UUID", identifier)
    description.set(CRS + "Version", original.get(CRS + "Version", "14.5"))
    description.set(CRS + "HasSettings", "True")
    for key, value in original.attrib.items():
        if key.startswith(CRS + "Supports") or key.startswith(CRS + "Table_"):
            description.set(key, value)
    for key, value in profile["settings"].items():
        if key == "Look":
            continue
        if isinstance(value, list):
            sequence = ET.SubElement(ET.SubElement(description, CRS + key), RDF + "Seq")
            for index in range(0, len(value), 2):
                ET.SubElement(sequence, RDF + "li").text = f"{value[index]:g}, {value[index + 1]:g}"
        else:
            description.set(CRS + key, str(value))
    for key, value in (("Name", "固件编辑器 Look " + sha256(data)[:16]), ("Group", "固件编辑器")):
        alt = ET.SubElement(ET.SubElement(description, CRS + key), RDF + "Alt")
        ET.SubElement(alt, RDF + "li", {"{http://www.w3.org/XML/1998/namespace}lang": "x-default"}).text = value
    look = ET.SubElement(ET.SubElement(description, CRS + "Look"), RDF + "Description")
    for key, value in (("Name", profile["name"]), ("UUID", profile["uuid"]), ("Amount", "1")):
        look.set(CRS + key, value)
    parameters = ET.SubElement(ET.SubElement(look, CRS + "Parameters"), RDF + "Description")
    for key, value in profile["settings"]["Look"]["Parameters"].items():
        if key.startswith("Table_"):
            continue
        if isinstance(value, list):
            sequence = ET.SubElement(ET.SubElement(parameters, CRS + key), RDF + "Seq")
            for index in range(0, len(value), 2):
                # Classic 15.6 drops Look curves written as "17.0, 19.0".
                # Preserve the integer point syntax used in native Adobe XMP.
                ET.SubElement(sequence, RDF + "li").text = f"{value[index]:g}, {value[index + 1]:g}"
        else:
            parameters.set(CRS + key, ("True" if value else "False") if isinstance(value, bool) else str(value))
    path = install_profile(data).with_name(f"GR4_render_v{NATIVE_RENDER_RECIPE_VERSION}_" + sha256(data)[:24] + ".xmp")
    encoded = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if not path.is_file() or path.read_bytes() != encoded:
        path.write_bytes(encoded)
    return path


def import_native_presets(profile_path, preset_path):
    """Refresh a running Classic through its own Import Profiles/Presets dialog.

    The public SDK has no XMP file-import API. On Windows, the adapter invokes
    the observed Classic menu and fills its standard file dialog. If Classic is
    closed, startup discovers these files without this UI refresh.
    """
    if os.name != "nt":
        return
    script = PRODUCT_ASSETS / "import-presets.ps1"
    with IMPORT_LOCK:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-ProfilePath", str(profile_path), "-PresetPath", str(preset_path)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=35)
        if result.returncode:
            raise ValueError("Lightroom profile/预设加载失败：" + (result.stderr or result.stdout)[-1200:])


def install_plugin(root):
    installation = discover_installation()
    if not installation["supported"]:
        raise ValueError("尚未发现可验证的 Lightroom Classic 安装")
    destination = plugin_location()
    source = PRODUCT_ASSETS / PLUGIN_NAME
    if not (source / "Info.lua").is_file():
        raise ValueError("产品包缺少 Lightroom 桥接插件资源")
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.glob("*.lua"):
        shutil.copyfile(path, destination / path.name)
    config = {"jobs_directory": str((Path(root) / "calibration/jobs").resolve()),
              "status_path": str((Path(root) / "calibration/lightroom-status.json").resolve())}
    (destination / "Config.lua").write_bytes(_lua_bytes(config))
    record = {"status": "installed_not_connected", "plugin_path": str(destination),
              "classic": installation["classic"], "config": config,
              "installed_at": datetime.now(timezone.utc).isoformat()}
    write_json(Path(root) / "calibration/lightroom-installation.json", record)
    return record


def prepare_request(root, job):
    folder = Path(root) / "calibration/jobs" / job["id"]
    pending = folder / "render-request.json"
    if pending.is_file():
        previous = json.loads(pending.read_text(encoding="utf-8"))
        if (previous.get("schema_version") != 3
                or previous.get("render_recipe_version") != NATIVE_RENDER_RECIPE_VERSION):
            raise ValueError("Lightroom 渲染配方已更新，请归档旧请求后重新提交")
        return previous
    data = Path(job["xmp_path"]).read_bytes()
    if sha256(data) != job["xmp_sha256"]:
        raise ValueError("XMP 已变化，无法提交渲染请求")
    profile = profile_settings(data)
    profile_path = install_profile(data)
    preset_path = install_render_preset(data)
    import_native_presets(profile_path, preset_path)
    nonce = uuid.uuid4().hex
    output = folder / "lightroom-output" / nonce
    output.mkdir(parents=True, exist_ok=False)
    samples = []
    for row in job["dataset"]["samples"]:
        path = Path(job["photos"]) / (Path(row["name"]).stem + ".DNG")
        if sha256(path.read_bytes()) != row["sha256"]:
            raise ValueError("渲染输入 DNG 哈希与登记材料不一致")
        samples.append({"name": path.stem, "path": str(path), "sha256": row["sha256"], "role": row["role"]})
    request = {"schema_version": 3, "render_recipe_version": NATIVE_RENDER_RECIPE_VERSION, "job_id": job["id"], "nonce": nonce,
               "created_at": datetime.now(timezone.utc).isoformat(), "xmp_sha256": job["xmp_sha256"],
               "xmp_path": job["xmp_path"], "profile_path": str(profile_path), "profile_uuid": profile["uuid"], "develop_settings": profile["settings"],
               "native_preset_filename": preset_path.name, "native_preset_sha256": sha256(preset_path.read_bytes()),
               "profile_tables": {key: value for key, value in profile["settings"]["Look"]["Parameters"].items() if key in ("RGBTable", "LookTable")},
               "samples": samples, "output_directory": str(output), "result_path": str(folder / "render-result.json"),
               "export": {"format": "JPEG", "color_space": "sRGB", "quality": 1, "resize": False, "metadata": "all"},
               "application_validation": profile["boundary"]}
    write_json(pending, request)
    lua_pending = folder / "render-request.lua.tmp"
    lua_pending.write_bytes(_lua_bytes(request))
    lua_pending.replace(folder / "render-request.lua")
    return request


def complete_request(root, job):
    """Validate new exports and publish a receipt; preexisting pairs cannot pass."""
    folder = Path(root) / "calibration/jobs" / job["id"]
    result_path = folder / "render-result.json"
    if not result_path.is_file():
        return None
    request = json.loads((folder / "render-request.json").read_text(encoding="utf-8"))
    if (request.get("schema_version") != 3
            or request.get("render_recipe_version") != NATIVE_RENDER_RECIPE_VERSION):
        raise ValueError("桥接请求版本已更新，需要重新导出并验证实际颜色表")
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if result.get("status") != "rendered":
        raise ValueError("Lightroom 自动渲染失败：" + result.get("message", "未知错误"))
    if (result.get("render_engine") != "lightroom"
            or result.get("render_recipe_version") != NATIVE_RENDER_RECIPE_VERSION
            or result.get("application", {}).get("plugin_version") != PLUGIN_VERSION):
        raise ValueError("Lightroom 插件或渲染配方已更新，请重新加载插件后再提交")
    if any(result.get(key) != request[key] for key in ("job_id", "nonce", "xmp_sha256", "profile_uuid")):
        raise ValueError("Lightroom 回执不属于当前渲染请求")
    if result.get("profile_tables") != request["profile_tables"]:
        raise ValueError("Lightroom 实际应用的颜色表与源 XMP 不匹配")
    native_path = Path(result.get("native_preset_path", ""))
    if (native_path.name.lower() != request["native_preset_filename"].lower()
            or not native_path.is_file() or sha256(native_path.read_bytes()) != request["native_preset_sha256"]):
        raise ValueError("Lightroom 实际原生预设与本次 XMP 渲染配方不匹配")
    if sha256(Path(job["xmp_path"]).read_bytes()) != request["xmp_sha256"]:
        raise ValueError("渲染后 XMP 已变化")
    exports = {row["name"]: row for row in result.get("outputs", [])}
    if set(exports) != {row["name"] for row in request["samples"]}:
        raise ValueError("Lightroom 导出结果没有覆盖全部登记样片")
    from PIL import Image
    records = []
    output = Path(request["output_directory"]).resolve()
    for source in request["samples"]:
        row = exports[source["name"]]
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(output) or path.suffix.lower() not in (".jpg", ".jpeg"):
            raise ValueError("Lightroom 目标不在本次新建导出目录")
        if row.get("look_uuid") != request["profile_uuid"]:
            raise ValueError("Lightroom 应用的 Look UUID 与源 XMP 不匹配")
        if sha256(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("渲染期间输入 DNG 已变化")
        data = path.read_bytes()
        with Image.open(path) as image:
            if image.format != "JPEG":
                raise ValueError("Lightroom 目标不是有效 JPEG")
            width, height = image.size
        records.append({"name": source["name"], "dng_sha256": source["sha256"],
                        "jpeg_sha256": sha256(data), "bytes": len(data), "width": width, "height": height,
                        "look_uuid": row["look_uuid"], "source": str(path), "role": source["role"]})
    for row in records:
        destination = Path(job["photos"]) / (row["name"] + ".jpg")
        shutil.copyfile(row["source"], destination)
        row["target"] = str(destination)
    receipt = {"schema_version": 1, "status": "rendered", "job_id": job["id"], "nonce": request["nonce"],
               "render_engine": "lightroom", "render_recipe_version": NATIVE_RENDER_RECIPE_VERSION,
               "approximate": False,
               "boundary": "Lightroom Classic SDK exports; fitting remains a software-domain camera color hypothesis.",
               "xmp_sha256": request["xmp_sha256"], "profile_uuid": request["profile_uuid"],
               "application": result.get("application"), "completed_at": result.get("completed_at"),
               "native_preset_path": str(native_path), "native_preset_sha256": request["native_preset_sha256"],
               "native_preset_sdk_uuid": result.get("native_preset_sdk_uuid"),
               "validated_at": datetime.now(timezone.utc).isoformat(), "outputs": records,
               "develop_settings": request["develop_settings"], "export": request["export"],
               "producer": "Lightroom Classic SDK + hash validation; no previous JPEGs reused"}
    write_json(folder / "render-receipt.json", receipt)
    return receipt
