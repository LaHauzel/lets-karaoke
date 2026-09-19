@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
python -c "import sys; assert sys.version_info >= (3,11), 'Python 3.11+ required'"
if errorlevel 1 exit /b 1
python -c "import torch, torchaudio; assert torch.cuda.is_available()"
if errorlevel 1 python -m pip install torch==2.9.0 torchaudio==2.9.0 torchvision==0.24.0 --index-url https://download.pytorch.org/whl/cu128
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python src\check_environment.py
if errorlevel 1 pause
