# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Prepare this checkout's Python 3.12 environment, then open the local editor."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import threading
import venv
import webbrowser


ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'
REQUIREMENTS = ROOT / 'requirements.txt'
PYPI = 'https://pypi.org/simple'
RUNTIME_MODULES = ('PIL', 'numpy', 'rawpy', 'cv2', 'capstone', 'unicorn')


def locked_requirements(path=REQUIREMENTS):
    entries = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        requirement, _, marker = line.partition(';')
        if marker:
            intel_mac = sys.platform == 'darwin' and platform.machine() == 'x86_64'
            supported = {
                'sys_platform != "darwin" or platform_machine != "x86_64"': not intel_mac,
                'sys_platform == "darwin" and platform_machine == "x86_64"': intel_mac,
            }
            marker = marker.strip()
            if marker not in supported:
                raise ValueError('依赖文件含尚未支持的平台条件：' + marker)
            if not supported[marker]:
                continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', requirement.strip())
        if not match:
            raise ValueError('依赖文件必须包含精确版本：' + line)
        entries[match.group(1)] = match.group(2)
    return entries


def environment_python(directory=VENV):
    return directory / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def installed_versions_match(expected):
    for package, version in expected.items():
        try:
            if importlib.metadata.version(package) != version:
                return False
        except importlib.metadata.PackageNotFoundError:
            return False
    return True


def verify_runtime():
    if not installed_versions_match(locked_requirements()):
        raise ValueError('Python 依赖版本与 requirements.txt 不一致，请从 start.cmd/start.sh 启动以准备环境')
    for name in RUNTIME_MODULES:
        try:
            importlib.import_module(name)
        except (ImportError, OSError) as exc:
            raise ValueError('无法加载 Python 依赖 ' + name + '：' + str(exc)) from exc
    return {package: importlib.metadata.version(package) for package in locked_requirements()}


def prepare_environment():
    python = environment_python()
    marker = VENV / '.requirements.sha256'
    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    if not python.is_file():
        print('首次启动：创建 .venv（Python 3.12）', flush=True)
        venv.EnvBuilder(with_pip=True).create(VENV)
    probe = 'import sys; from launcher import installed_versions_match, locked_requirements; sys.exit(0 if sys.version_info[:2] == (3,12) and installed_versions_match(locked_requirements()) else 1)'
    matches = subprocess.run([str(python), '-X', 'utf8', '-c', probe], cwd=ROOT).returncode == 0
    if not matches or not marker.is_file() or marker.read_text(encoding='ascii').strip() != digest:
        print('准备固定版本依赖（从官方 PyPI 下载，首次需要联网）', flush=True)
        subprocess.run([str(python), '-X', 'utf8', '-m', 'pip', '--isolated', 'install',
                        '--index-url', PYPI, '--only-binary=:all:', '--disable-pip-version-check',
                        '-r', str(REQUIREMENTS)], check=True, cwd=ROOT)
        subprocess.run([str(python), '-X', 'utf8', '-c', 'from launcher import verify_runtime; verify_runtime()'], check=True, cwd=ROOT)
        marker.write_text(digest + '\n', encoding='ascii')
    return python


def local_server(editor, port):
    """Bind first; fall back to an available port if the preferred one is busy."""
    from http.server import ThreadingHTTPServer
    from gr4_editor.server import make_handler
    # Windows SO_REUSEADDR can bind a second HTTP server to a live port.
    # An exclusive socket keeps the fallback and opened URL unambiguous.
    class LocalHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = False

        def server_bind(self):
            if os.name == 'nt':
                import socket
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()

    try:
        return LocalHTTPServer(('127.0.0.1', port), make_handler(editor))
    except OSError:
        if port == 0:
            raise
        return LocalHTTPServer(('127.0.0.1', 0), make_handler(editor))


def serve(port=8787, *, browser=True):
    from gr4_editor.service import Editor
    from gr4_editor.bootstrap import DATA_ROOT
    editor = Editor(DATA_ROOT)
    server = local_server(editor, port)
    url = 'http://127.0.0.1:' + str(server.server_port)
    print('固件编辑器：' + url, flush=True)
    print('关闭本窗口或按 Ctrl+C 停止服务。', flush=True)
    if browser:
        timer = threading.Timer(.2, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='固件编辑器首次配置与本地启动')
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--data-dir', type=Path, help='本地用户数据目录，也可设置 GR4_EDITOR_DATA_DIR')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--check', action='store_true', help='准备并检查 Python 环境后退出')
    parser.add_argument('--check-toolchain', action='store_true', help='执行临时 ARM 编译/链接预检后退出')
    parser.add_argument('--clang', type=Path, help='选择 LLVM/NDK 的 clang 文件或 bin 目录')
    parser.add_argument('--lld', type=Path, help='独立 ld.lld 文件或 bin 目录（可选）')
    args = parser.parse_args(argv)
    if sys.version_info[:2] != (3, 12):
        parser.error('请安装 64 位 Python 3.12，并使用 start.cmd 或 python3.12 启动')
    if sys.maxsize <= 2**32:
        parser.error('请安装 64 位 Python 3.12')
    if not 0 <= args.port <= 65535:
        parser.error('端口必须为 0–65535')
    if args.data_dir:
        os.environ['GR4_EDITOR_DATA_DIR'] = str(args.data_dir.expanduser().resolve())
    try:
        if Path(sys.prefix).resolve() != VENV.resolve():
            python = prepare_environment()
            forwarded = sys.argv[1:] if argv is None else argv
            return subprocess.call([str(python), '-X', 'utf8', str(Path(__file__).resolve()), *forwarded], cwd=ROOT)
        versions = verify_runtime()
        if args.clang or args.lld or args.check_toolchain:
            from gr4_editor import toolchain
            if args.lld and not args.clang:
                parser.error('--lld 应与 --clang 一起配置')
            if args.clang:
                proof = toolchain.save_config(None, args.clang, args.lld)
            else:
                proof = toolchain.status()
            print(json.dumps(proof, ensure_ascii=False, indent=2), flush=True)
            if not proof['ready']:
                return 1
            if args.check_toolchain:
                return 0
        if args.check:
            print(json.dumps({'ready': True, 'python': sys.version.split()[0], 'dependencies': versions}, ensure_ascii=False, indent=2))
            return 0
        serve(args.port, browser=not args.no_browser)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('启动失败：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
