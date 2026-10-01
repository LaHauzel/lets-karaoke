@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=whisper"
python -c "import sys; assert sys.version_info[:2] == (3,11), 'Use the tested Python 3.11 installation'"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 exit /b 1
)
call ".venv\Scripts\activate.bat"
call setup_profile.bat %PROFILE%
exit /b %errorlevel%
