@echo off
rem lets-karaoke local WebUI launcher. Double-click to start; default http://127.0.0.1:7870
rem Keep this file ASCII-only: cmd parses .bat files with the system code page (e.g. GBK/936).
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" set "PATH=%~dp0.venv\Scripts;%PATH%"
set PYTHONPATH=
set PYTHONUTF8=1
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
if "%~1"=="" (
  python "src\webui.py" --port 7870
) else (
  python "src\webui.py" %*
)
if errorlevel 1 pause
