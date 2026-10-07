#!/bin/sh
# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

set -eu
cd "$(CDPATH= cd "$(dirname "$0")" && pwd)"
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 -X utf8 launcher.py "$@"
fi
exec python3 -X utf8 launcher.py "$@"
