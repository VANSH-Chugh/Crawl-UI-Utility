@echo off
setlocal
cd /d "%~dp0"
set "CIVICTABLE_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%~dp0.venv\Scripts\python.exe" set "CIVICTABLE_PYTHON=%~dp0.venv\Scripts\python.exe"
if exist "%CIVICTABLE_PYTHON%" (
  "%CIVICTABLE_PYTHON%" server.py
) else (
  python server.py
)
if errorlevel 1 (
  echo.
  echo Unable to start CivicTable. Install Python 3.10 or later and check README.md.
  pause
)
