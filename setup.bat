@echo off
cd /d "%~dp0"
call setup_profile.bat full
if errorlevel 1 pause
exit /b %errorlevel%
