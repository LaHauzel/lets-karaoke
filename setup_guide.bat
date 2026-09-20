@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
title lets-karaoke setup assistant

:menu
cls
echo.
echo ================================================================
echo   lets-karaoke Windows setup assistant
echo ================================================================
echo.
echo   [1] Detailed environment check
echo   [2] Install or repair Python dependencies
echo   [3] Download ForcedAligner model  ^(~1.8 GB^)
echo   [4] Download ASR model            ^(~4.7 GB^)
echo   [5] Download all models
echo   [6] Show model status
echo   [7] Start WebUI
echo   [8] Open setup guide
echo   [Q] Quit
echo.
set "answer="
set /p "answer=Choose an option [1-8/Q]: "

if /i "%answer%"=="1" call :check & goto menu
if /i "%answer%"=="2" call :install & goto menu
if /i "%answer%"=="3" call :download aligner & goto menu
if /i "%answer%"=="4" call :download asr & goto menu
if /i "%answer%"=="5" call :download all & goto menu
if /i "%answer%"=="6" call :models & goto menu
if /i "%answer%"=="7" call :launch & goto menu
if /i "%answer%"=="8" call :guide & goto menu
if /i "%answer%"=="q" goto done
echo Invalid option.
timeout /t 2 >nul
goto menu

:check
cls
echo [1/1] Checking Python, packages, FFmpeg, CUDA, models, and disk...
echo Missing models are warnings; missing runtime requirements are failures.
echo.
python src\check_environment.py
echo.
pause
exit /b

:install
cls
echo [1/3] Installing or repairing GPU PyTorch, torchaudio, and dependencies...
echo pip will display download progress. This can take several minutes.
echo.
call setup.bat
set "rc=%errorlevel%"
echo.
if not "%rc%"=="0" (
  echo Installation did not complete. Fix the reported item and try again.
) else (
  echo Dependencies installed. Run the detailed check next.
)
pause
exit /b

:download
cls
echo.
if /i "%~1"=="all" (
  echo [download] Fetching all models. Existing complete models are skipped.
  echo            The two models require about 6.5 GB in total.
  echo.
  python src\fetch_models.py
) else (
  echo [download] Fetching the %~1 model. Existing files are reused.
  echo.
  python src\fetch_models.py --only %~1
)
set "rc=%errorlevel%"
echo.
if not "%rc%"=="0" echo Download did not complete. Run this option again to resume.
if "%rc%"=="0" echo Download complete.
pause
exit /b

:models
cls
echo Current model status:
echo.
python src\fetch_models.py --list
echo.
pause
exit /b

:launch
cls
echo Starting the single local WebUI service.
echo Default URL: http://127.0.0.1:7870/
echo Press Ctrl+C in this window to stop the service.
echo.
call webui.bat
echo.
pause
exit /b

:guide
if exist "docs\SETUP_WINDOWS.md" (
  start "" notepad.exe "%~dp0docs\SETUP_WINDOWS.md"
) else (
  echo Missing docs\SETUP_WINDOWS.md.
  pause
)
exit /b

:done
echo.
echo Setup assistant closed.
endlocal
exit /b 0
