@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" set "PATH=%~dp0.venv\Scripts;%PATH%"
set PYTHONUTF8=1

python -m compileall -q src tools
if errorlevel 1 exit /b 1

python -X utf8 tests\run_tests.py
if errorlevel 1 exit /b 1

echo Offline regression tests passed. GPU end-to-end checks are separate.
