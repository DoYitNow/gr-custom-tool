@echo off
REM Copyright (C) 2026 DoYitNow
REM SPDX-License-Identifier: GPL-2.0-only

setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3.12 -X utf8 launcher.py %*
) else (
    python -X utf8 launcher.py %*
)
if errorlevel 1 (
    echo.
    echo Setup failed. Install 64-bit Python 3.12 and check the message above.
    pause
    exit /b 1
)
