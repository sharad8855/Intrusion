@echo off
REM ── Launcher for the AI Intrusion & Virtual Tripwire System ──────────
REM Always starts the app with the project's own .venv interpreter,
REM which has every required dependency (APScheduler, OpenVINO, ...).
REM The app itself frees port 8000 if a stale instance is still running.

cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo [start] ERROR: .venv not found at "%VENV_PY%"
    echo [start] Create it and install requirements:
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo [start] launching with %VENV_PY%
"%VENV_PY%" -m backend.main

echo.
echo [start] server stopped.
pause
