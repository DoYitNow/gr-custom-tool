# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Start the local editor, or inspect/build a project from the command line."""

import argparse
import json
import os
from pathlib import Path

from gr4_editor.bootstrap import DATA_ROOT


def main():
    parser = argparse.ArgumentParser(description="固件编辑器")
    parser.add_argument("--data-dir", type=Path, default=DATA_ROOT)
    commands = parser.add_subparsers(dest="command")
    server = commands.add_parser("serve", help="启动本地编辑界面")
    server.add_argument("--port", type=int, default=8787)
    inspect = commands.add_parser("inspect", help="只读检查固件")
    inspect.add_argument("firmware", type=Path)
    import_command = commands.add_parser("import", help="创建本地编辑项目")
    import_command.add_argument("firmware", type=Path)
    build = commands.add_parser("build", help="构建已保存项目")
    build.add_argument("project_id")
    build.add_argument("--version")
    args = parser.parse_args()
    # Match toolchain settings and all default modules to the chosen data root.
    os.environ['GR4_EDITOR_DATA_DIR'] = str(args.data_dir.resolve())
    from gr4_editor import bootstrap
    bootstrap.DATA_ROOT = args.data_dir.resolve()
    if args.command in (None, "serve"):
        from gr4_editor.server import serve
        return serve(getattr(args, "port", 8787), args.data_dir)
    from gr4_editor.service import Editor
    editor = Editor(args.data_dir)
    if args.command == "inspect":
        from gr4_editor.firmware import inspect_firmware, load_image
        from gr4_editor.crops import read_crops, capabilities
        image = load_image(args.firmware)
        result = inspect_firmware(image)
        result["crops"], result["crop_inspection"] = read_crops(image.rtos)
        result["crop_capabilities"] = capabilities(result["crop_inspection"])
    elif args.command == "import":
        result = editor.import_firmware(args.firmware.read_bytes(), args.firmware.name)
    elif args.command == "build":
        result = editor.build(args.project_id, args.version)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
