@echo off
setlocal EnableExtensions
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
echo   [1] Install Whisper subtitle environment
echo       Known-lyrics alignment, subtitle rendering, optional vocal separation
echo   [2] Install concert segmentation minimum environment
echo       Long-video analysis, waveform editing, and FFmpeg export; no GPU/models
echo   [3] Install Qwen full subtitle environment
echo       Whisper plus Qwen ForcedAligner, ASR drafts, and Wav2Vec2
echo   [4] Install SOFA singing alignment environment
echo       Whisper line windows plus SOFA phoneme-level singing alignment
echo   [5] Install full environment
echo       Whisper, Qwen, SOFA, Demucs, and all supported subtitle backends
echo   [6] Download Whisper model              ^(~3 GB^)
echo   [7] Download Qwen models                ^(~6.5 GB^)
echo   [8] Show environment and model status
echo   [9] Start WebUI
echo   [10] Open setup guide
echo   [Q] Quit
echo.
set "answer="
set /p "answer=Choose an option [1-10/Q]: "

if /i "%answer%"=="1" call :profile whisper & goto menu
if /i "%answer%"=="2" call :profile concert & goto menu
if /i "%answer%"=="3" call :profile qwen & goto menu
if /i "%answer%"=="4" call :profile sofa & goto menu
if /i "%answer%"=="5" call :profile full & goto menu
if /i "%answer%"=="6" call :download_whisper & goto menu
if /i "%answer%"=="7" call :download_qwen & goto menu
if /i "%answer%"=="8" call :models & goto menu
if /i "%answer%"=="9" call :launch & goto menu
if /i "%answer%"=="10" call :guide & goto menu
if /i "%answer%"=="q" goto done
echo Invalid option.
timeout /t 2 >nul
goto menu

:profile
cls
if /i "%~1"=="whisper" (
  echo [Whisper subtitle environment]
  echo Function: known-lyrics Whisper/stable-ts alignment, subtitle rendering,
  echo           and optional Demucs vocal separation.
) else if /i "%~1"=="concert" (
  echo [Concert segmentation minimum environment]
  echo Function: long-video audio analysis, interactive boundaries, and FFmpeg export.
  echo           This profile does not install CUDA, Whisper, Qwen, SOFA, or models.
) else if /i "%~1"=="qwen" (
  echo [Qwen full subtitle environment]
  echo Function: Whisper subtitle tools plus Qwen ForcedAligner, ASR drafts,
  echo           and Wav2Vec2 alignment support.
) else if /i "%~1"=="sofa" (
  echo [SOFA singing alignment environment]
  echo Function: Whisper line windows followed by SOFA phoneme-level singing alignment.
) else (
  echo [Full environment]
  echo Function: Whisper, Qwen, SOFA, Demucs, and all supported subtitle backends.
)
echo.
echo The installer will show pip download progress and run a matching check afterwards.
echo.
call setup_profile.bat %~1
set "rc=%errorlevel%"
echo.
if "%rc%"=="0" (echo Profile installation completed.) else (echo Profile installation failed; fix the reported item and retry.)
pause
exit /b

:download_whisper
cls
echo [Whisper model]
echo Function: supplies the local Whisper checkpoint used by the Whisper subtitle
echo           profile and as the timing pre-pass for Qwen/SOFA workflows.
echo Download size depends on the selected checkpoint; large-v3 is about 3 GB.
echo.
python src\fetch_whisper.py --model large-v3
set "rc=%errorlevel%"
echo.
if not "%rc%"=="0" echo Download did not complete. Run this option again to resume.
if "%rc%"=="0" echo Whisper model is ready.
pause
exit /b

:download_qwen
cls
echo [Qwen models]
echo Function: ForcedAligner provides word/character timing; ASR provides lyric
echo           drafts when no lyric file is available.
echo   [A] ForcedAligner       (~1.8 GB)
echo   [B] ASR                 (~4.7 GB)
echo   [C] Both                (~6.5 GB)
echo.
set "model_choice="
set /p "model_choice=Choose a model [A-C]: "
if /i "%model_choice%"=="a" goto qwen_aligner
if /i "%model_choice%"=="b" goto qwen_asr
if /i "%model_choice%"=="c" goto qwen_both
echo Invalid model option.
set "rc=1"
goto qwen_done
:qwen_aligner
python src\fetch_models.py --only aligner
set "rc=%errorlevel%"
goto qwen_done
:qwen_asr
python src\fetch_models.py --only asr
set "rc=%errorlevel%"
goto qwen_done
:qwen_both
python src\fetch_models.py
set "rc=%errorlevel%"
:qwen_done
echo.
if "%rc%"=="0" echo Qwen model download complete.
if not "%rc%"=="0" echo Download did not complete. Run this option again to resume.
pause
exit /b

:models
cls
echo Current environment and model status:
echo.
python src\check_environment.py --profile concert
echo.
python src\fetch_models.py --list
echo.
if exist "models\whisper\large-v3.pt" (echo [OK] Whisper large-v3: models\whisper\large-v3.pt) else (echo [--] Whisper large-v3: not downloaded)
if exist "models\sofa\multilingual\pretrained_multilingual_singing\v1.0.0_multilingual_singing.ckpt" (echo [OK] SOFA checkpoint found.) else (echo [--] SOFA checkpoint: place it under models\sofa\multilingual\pretrained_multilingual_singing\)
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
