# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Small local JSON projects and content-addressed inputs; no database needed."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from .bootstrap import DATA_ROOT


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class Store:
    def __init__(self, root=DATA_ROOT):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()

    def identifier(self, value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,90}", value):
            raise ValueError("项目或构建标识无效")
        return value

    def object(self, data, suffix=".bin"):
        digest = sha256(data)
        target = self.root / "objects" / (digest + suffix)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return target, digest

    def project_path(self, project_id):
        return self.root / "projects" / self.identifier(project_id) / "project.json"

    def save_project(self, project):
        with self.lock:
            project["updated_at"] = timestamp()
            write_json(self.project_path(project["id"]), project)
        return deepcopy(project)

    def get_project(self, project_id):
        with self.lock:
            path = self.project_path(project_id)
            if not path.is_file():
                raise ValueError("找不到这个项目，请先导入固件")
            return json.loads(path.read_text(encoding="utf-8"))

    def new_id(self, prefix):
        return prefix + "-" + uuid.uuid4().hex[:16]

    def ledger(self):
        with self.lock:
            path = self.root / "build-ledger.json"
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []

    def register_build(self, build):
        with self.lock:
            records = self.ledger()
            records.append(build)
            write_json(self.root / "build-ledger.json", records)

    def build(self, build_id):
        return next((row for row in self.ledger() if row["id"] == self.identifier(build_id)), None)
