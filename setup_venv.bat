@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=whisper"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(sys.version_info[:2] != (3,11))" >nul 2>&1
  if errorlevel 1 (
    echo The existing .venv was not created with Python 3.11.
    echo Rename or delete the .venv folder, then run this script again.
    exit /b 1
  )
  goto install_profile
)

rem Prefer python on PATH when it is 3.11; otherwise ask the py launcher for 3.11.
set "BASE_PY="
python -c "import sys; raise SystemExit(sys.version_info[:2] != (3,11))" >nul 2>&1 && set "BASE_PY=python"
if not defined BASE_PY (
  py -3.11 -c "import sys" >nul 2>&1 && set "BASE_PY=py -3.11"
)
if not defined BASE_PY (
  echo Python 3.11 was not found on PATH or through the py launcher.
  echo Install it with: winget install --id Python.Python.3.11 -e --scope user
  exit /b 1
)

echo Creating .venv with %BASE_PY%
%BASE_PY% -m venv .venv
if errorlevel 1 exit /b 1

:install_profile
call ".venv\Scripts\activate.bat"
call setup_profile.bat %PROFILE%
exit /b %errorlevel%
