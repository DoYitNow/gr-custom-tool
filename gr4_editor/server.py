# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Local HTTP interface; all mutations remain in ignored project data."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .bootstrap import EDITOR_ROOT
from .service import Editor


def make_handler(editor):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def response(self, status, body, content_type="application/json; charset=utf-8", filename=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if filename:
                self.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
            self.end_headers()
            self.wfile.write(body)

        def arguments(self):
            parts = urlsplit(self.path)
            return parts.path, {key: values[-1] for key, values in parse_qs(parts.query).items()}

        def body(self):
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 256 * 1024 * 1024:
                raise ValueError("上传文件为空或超过 256 MiB")
            return self.rfile.read(size)

        def do_GET(self):
            try:
                path, query = self.arguments()
                if path == "/api/status":
                    return self.response(200, editor.status())
                if path == "/api/settings":
                    return self.response(200, editor.settings())
                if path == "/api/history":
                    return self.response(200, editor.firmware_history(query.get('project_id')))
                if path == "/api/history/download":
                    target = editor.history_firmware_path(query['sha256'])
                    return self.response(200, target.read_bytes(), "application/octet-stream", "fwdc248b.bin")
                if path == "/api/project":
                    return self.response(200, editor.project(query["id"]))
                if path == "/api/export":
                    project = editor.project(query["id"])
                    return self.response(200, project, filename="demo-project.json")
                if path == "/api/shutdown-export":
                    return self.response(200, editor.export_shutdown(query["project_id"]),
                                         "application/zip", "shutdown-preparation.zip")
                if path == "/api/download":
                    build = editor.store.build(query["build_id"])
                    if not build:
                        raise ValueError("找不到构建记录")
                    kind = query.get("kind", "firmware")
                    if kind not in ("firmware", "manifest"):
                        raise ValueError("下载类型无效")
                    target = Path(build["firmware_path"] if kind == "firmware" else build["manifest_path"])
                    return self.response(200, target.read_bytes(), "application/octet-stream" if kind == "firmware" else "application/json; charset=utf-8", target.name)
                if path.startswith("/api/"):
                    return self.response(404, {"error": "接口不存在"})
                target = (EDITOR_ROOT / "web" / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
                if not target.is_relative_to((EDITOR_ROOT / "web").resolve()) or not target.is_file():
                    return self.response(404, "找不到页面", "text/plain; charset=utf-8")
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                return self.response(200, target.read_bytes(), content_type + ("; charset=utf-8" if target.suffix in (".html", ".css", ".js") else ""))
            except (ValueError, KeyError, OSError) as exc:
                return self.response(400, {"error": str(exc)})

        def do_POST(self):
            try:
                path, query = self.arguments()
                # Requests are issued by this same local page. Other origins
                # cannot mutate projects through a browser cross-origin form.
                origin = self.headers.get("Origin")
                if origin and origin != "http://" + self.headers.get("Host", ""):
                    return self.response(403, {"error": "请从本地编辑器页面操作"})
                raw = self.body()
                if path == "/api/import":
                    return self.response(200, editor.import_firmware(raw, query.get("name", "fwdc248b.bin")))
                if path == "/api/shutdown-image":
                    return self.response(200, editor.import_shutdown_image(
                        query["project_id"], query.get("target_id"), raw, query.get("name", "image.png"), query.get("fit", "contain"),
                        item_id=query.get('item_id'), item_name=query.get('item_name')))
                data = json.loads(raw)
                if path == "/api/project":
                    result = editor.save(data["project_id"], data.get("title"), crops=data.get("crops"),
                                         shutdown=data.get("shutdown"),
                                         update_policy_draft=data.get("update_policy_draft"),
                                         build_options=data.get("build_options"))
                elif path == "/api/history/open":
                    result = editor.open_history_firmware(data['sha256'])
                elif path == "/api/history/rollback":
                    result = editor.rollback_history_firmware(data['sha256'], data['project_id'], data.get('version') or None,
                                                             data.get('allow_older', True))
                elif path == "/api/shutdown-item":
                    result = editor.add_shutdown_item(data.get('project_id') or query['project_id'], data.get('name'),
                                                     data.get('source_id'), data.get('asset_id'))
                elif path == "/api/crop-preview":
                    result = editor.crop_preview(data["project_id"], data["ratio"])
                elif path == "/api/project-import":
                    result = editor.import_project(data)
                elif path == "/api/build":
                    result = editor.build(data["project_id"], data.get("version") or None,
                                          data.get("allow_older"))
                elif path == "/api/toolchain":
                    from .toolchain import save_config
                    result = save_config(editor.store.root, data["clang"], data.get("lld") or None)
                else:
                    return self.response(404, {"error": "接口不存在"})
                return self.response(200, result)
            except (ValueError, KeyError, OSError, TypeError) as exc:
                return self.response(400, {"error": str(exc)})
            except Exception as exc:
                # Preserve a failed operation as a failure; do not return a
                # candidate download after a backend or packaging exception.
                import traceback
                traceback.print_exc()
                return self.response(500, {"error": "处理失败：" + str(exc)})

    return Handler


def serve(port=8787, root=None):
    editor = Editor(root) if root else Editor()
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(editor))
    print(f"固件编辑器：http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
