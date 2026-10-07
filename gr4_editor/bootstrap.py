# Copyright (C) 2026 DoYitNow
# SPDX-License-Identifier: GPL-2.0-only

"""Program resources and per-user local data, without research paths."""
import os
from pathlib import Path
import sys

EDITOR_ROOT = PROJECT_ROOT = Path(__file__).resolve().parents[1]


def default_data_root():
    override = os.environ.get('GR4_EDITOR_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    return base / 'FirmwareEditorDemo'


DATA_ROOT = default_data_root()
