"""模型存放路径中枢：所有模型/权重统一放项目 models 目录下。

导入本模块即设置好相关环境变量（必须在 torch.hub / transformers /
whisper 任何模型加载之前导入）：

- TORCH_HOME  = models/torch          （demucs 权重：hub/checkpoints/）
- HF_HOME     = models/hf             （transformers/HF 生态缓存）
- WHISPER_DIR = models/whisper        （openai-whisper 权重，download_root 用）

换机迁移时拷贝整个 models 目录即可，无需重新下载。
"""
from pathlib import Path
import os

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"
WHISPER_DIR = MODELS / "whisper"
TORCH_HOME = MODELS / "torch"
HF_HOME = MODELS / "hf"
QWEN_ASR_DIR = MODELS / "Qwen3-ASR-1.7B"
QWEN_ALIGNER_DIR = MODELS / "Qwen3-ForcedAligner-0.6B"

# 环境变量只设不覆盖（若用户已显式指定则尊重用户）
os.environ.setdefault("TORCH_HOME", str(TORCH_HOME))
os.environ.setdefault("HF_HOME", str(HF_HOME))
os.environ.setdefault("HF_HUB_OFFLINE", "0")
