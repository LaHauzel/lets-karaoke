@echo off
rem lets-karaoke 本地 WebUI 启动器
rem 双击即可。默认 http://127.0.0.1:7870
cd /d "%~dp0"
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
