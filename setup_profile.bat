@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\python.exe" set "PATH=%~dp0.venv\Scripts;%PATH%"
chcp 65001 >nul
set "PYTHONUTF8=1"

set "PROFILE=%~1"
if /i "%PROFILE%"=="whisper" goto profile_ok
if /i "%PROFILE%"=="concert" goto profile_ok
if /i "%PROFILE%"=="qwen" goto profile_ok
if /i "%PROFILE%"=="sofa" goto profile_ok
if /i "%PROFILE%"=="full" goto profile_ok
echo Usage: setup_profile.bat whisper^|concert^|qwen^|sofa^|full
exit /b 2

:profile_ok
set "REQUIREMENTS=requirements-%PROFILE%.txt"
if not exist "%REQUIREMENTS%" (
  echo Missing %REQUIREMENTS%.
  exit /b 2
)

echo.
echo ================================================================
echo Installing profile: %PROFILE%
echo Requirements: %REQUIREMENTS%
echo ================================================================
echo.

python -c "import sys; print('Python: '+sys.executable); assert sys.version_info[:2] == (3,11), 'The supported installer requires Python 3.11.x'"
if errorlevel 1 goto fail

if /i "%PROFILE%"=="concert" goto install_concert

echo [1/3] Checking CUDA PyTorch...
python -c "import torch, torchaudio; assert torch.__version__.split('+')[0] == '2.9.0' and torchaudio.__version__.split('+')[0] == '2.9.0'; assert torch.cuda.is_available(); assert torch.version.cuda == '12.8'; assert torch.ones(1, device='cuda').item() == 1"
if errorlevel 1 (
  echo [2/3] Installing PyTorch 2.9.0 + CUDA 12.8...
  python -m pip install torch==2.9.0 torchaudio==2.9.0 torchvision==0.24.0 --index-url https://download.pytorch.org/whl/cu128
  if errorlevel 1 goto fail
) else (
  echo [2/3] CUDA PyTorch is already available.
)

:install_profile
echo [3/3] Installing profile dependencies. pip will show download progress.
python -m pip install -r "%REQUIREMENTS%"
if errorlevel 1 goto fail
if /i "%PROFILE%"=="full" (
  echo.
  echo Installing the optional concert speech/music classifier weights...
  python src\fetch_concert_model.py
  if errorlevel 1 echo Model download failed; the concert tab will keep basic boundary analysis.
)

echo.
echo Profile installation finished. Running the matching environment check...
python src\check_environment.py --profile %PROFILE%
set "rc=%errorlevel%"
if not "%rc%"=="0" goto fail
echo.
echo Profile %PROFILE% is ready.
endlocal
exit /b 0

:install_concert
echo [1/3] Installing the concert segmentation minimum dependencies. pip will show download progress.
python -m pip install -r "%REQUIREMENTS%"
if errorlevel 1 goto fail
echo [2/3] Downloading the optional 15.4 MiB YAMNet speech/music classifier with progress.
python src\fetch_concert_model.py
if errorlevel 1 echo Model download failed; basic song-boundary analysis remains available.
echo [3/3] Running the concert segmentation environment check...
python src\check_environment.py --profile concert
set "rc=%errorlevel%"
if not "%rc%"=="0" goto fail
echo.
echo Profile %PROFILE% is ready. No GPU is required; YAMNet improves speech/music review markers.
endlocal
exit /b 0

:fail
echo.
echo Profile %PROFILE% did not finish successfully.
echo Fix the reported problem and run the same profile again.
set "rc=%errorlevel%"
if "%rc%"=="0" set "rc=1"
endlocal & exit /b %rc%
