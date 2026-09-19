@echo off
rem lets-karaoke 本地 WebUI 启动器
rem 双击即可。默认 http://127.0.0.1:18888
rem 注意：7870/7873 等端口落在 Windows 排除端口范围（WinError 10013），
rem       可用 netsh interface ipv4 show excludedportrange protocol=tcp 查询后再换。
cd /d "%~dp0"
set PYTHONPATH=
set PYTHONUTF8=1
set no_proxy=127.0.0.1,localhost
set NO_PROXY=127.0.0.1,localhost
if "%~1"=="" (
  python "src\webui.py" --port 18888
) else (
  python "src\webui.py" %*
)
if errorlevel 1 pause
