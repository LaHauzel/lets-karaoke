@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run setup_venv.bat whisper^|concert^|qwen^|sofa^|full first.
  exit /b 1
)
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" src\webui.py %*
