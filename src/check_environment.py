"""Profile-aware local environment diagnostics for the Windows WebUI."""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"

PACKAGE_INFO = {
    "numpy": ("numpy", "numpy"),
    "scipy": ("scipy", "scipy"),
    "soundfile": ("soundfile", "soundfile"),
    "librosa": ("librosa", "librosa"),
    "torch": ("torch", "torch"),
    "torchaudio": ("torchaudio", "torchaudio"),
    "whisper": ("whisper", "openai-whisper"),
    "stable_whisper": ("stable_whisper", "stable-ts"),
    "demucs": ("demucs", "demucs"),
    "qwen_asr": ("qwen_asr", "qwen-asr"),
    "transformers": ("transformers", "transformers"),
    "huggingface_hub": ("huggingface_hub", "huggingface-hub"),
    "pykakasi": ("pykakasi", "pykakasi"),
    "textgrid": ("textgrid", "textgrid"),
    "lightning": ("lightning", "lightning"),
    "tensorboardX": ("tensorboardX", "tensorboardX"),
    "h5py": ("h5py", "h5py"),
    "einops": ("einops", "einops"),
    "yaml": ("yaml", "PyYAML"),
    "matplotlib": ("matplotlib", "matplotlib"),
    "pandas": ("pandas", "pandas"),
    "rapidfuzz": ("rapidfuzz", "rapidfuzz"),
}

BASE_PACKAGES = ("numpy", "scipy")
WHISPER_PACKAGES = BASE_PACKAGES + (
    "soundfile", "librosa", "torch", "torchaudio", "whisper", "stable_whisper", "demucs",
)
QWEN_PACKAGES = WHISPER_PACKAGES + ("qwen_asr", "transformers", "huggingface_hub")
SOFA_PACKAGES = WHISPER_PACKAGES + (
    "pykakasi", "textgrid", "lightning", "tensorboardX", "h5py", "einops",
    "yaml", "matplotlib", "pandas",
)
FULL_PACKAGES = tuple(dict.fromkeys(QWEN_PACKAGES + SOFA_PACKAGES + ("rapidfuzz",)))

PROFILES = {
    "whisper": {
        "label": "Whisper 字幕环境",
        "purpose": "已有歌词的 Whisper/stable-ts 对齐、字幕渲染和可选人声分离",
        "packages": WHISPER_PACKAGES,
        "cuda": True,
        "disk_gib": 8,
        "models": ("whisper",),
    },
    "concert": {
        "label": "演唱会切割最小环境",
        "purpose": "本地长视频音轨分析、边界编辑和 FFmpeg 分段导出",
        "packages": BASE_PACKAGES,
        "cuda": False,
        "disk_gib": 2,
        "models": (),
    },
    "qwen": {
        "label": "Qwen 完整字幕环境",
        "purpose": "Whisper 字幕环境加 Qwen ForcedAligner、ASR 草稿和 Wav2Vec2 后端",
        "packages": QWEN_PACKAGES,
        "cuda": True,
        "disk_gib": 12,
        "models": ("whisper", "aligner", "asr"),
    },
    "sofa": {
        "label": "SOFA 歌声对齐环境",
        "purpose": "Whisper 行窗口加 SOFA 音素级歌声对齐",
        "packages": SOFA_PACKAGES,
        "cuda": True,
        "disk_gib": 12,
        "models": ("whisper", "sofa"),
    },
    "full": {
        "label": "完整环境",
        "purpose": "Whisper、Qwen、SOFA、Demucs 和全部字幕后端",
        "packages": FULL_PACKAGES,
        "cuda": True,
        "disk_gib": 20,
        "models": ("whisper", "aligner", "asr", "sofa"),
    },
}


def line(label: str, value: str, state: str = "INFO") -> None:
    print(f"[{state:<5}] {label}: {value}")


def command_version(command: str) -> str | None:
    try:
        result = subprocess.run(
            [command, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (result.stdout or result.stderr).splitlines()
    return first[0].strip() if first else "可执行，但未返回版本"


def qwen_complete(directory: Path) -> tuple[bool, float]:
    if not directory.exists():
        return False, 0.0
    files = [p for p in directory.rglob("*") if p.is_file()]
    complete = (directory / "config.json").is_file() and any(
        p.name.endswith((".safetensors", ".bin")) or ".safetensors.index.json" in p.name
        for p in files
    )
    return complete, sum(p.stat().st_size for p in files) / (1024 ** 3)


def model_status(kind: str) -> tuple[bool, str]:
    if kind == "whisper":
        directory = MODELS / "whisper"
        files = list(directory.glob("*.pt")) if directory.exists() else []
        return bool(files), f"{len(files)} 个 Whisper 权重（{directory}）"
    if kind == "aligner":
        ok, size = qwen_complete(MODELS / "Qwen3-ForcedAligner-0.6B")
        return ok, f"{size:.2f} GiB（{MODELS / 'Qwen3-ForcedAligner-0.6B'}）"
    if kind == "asr":
        ok, size = qwen_complete(MODELS / "Qwen3-ASR-1.7B")
        return ok, f"{size:.2f} GiB（{MODELS / 'Qwen3-ASR-1.7B'}）"
    if kind == "sofa":
        path = MODELS / "sofa" / "multilingual" / "pretrained_multilingual_singing" / "v1.0.0_multilingual_singing.ckpt"
        return path.is_file(), str(path)
    raise ValueError(f"unknown model kind: {kind}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lets-karaoke profile-aware environment check")
    parser.add_argument(
        "--profile", choices=sorted(PROFILES), default="full",
        help="whisper, concert, qwen, sofa, or full",
    )
    args = parser.parse_args(argv)
    profile = PROFILES[args.profile]
    failures = 0
    warnings = 0

    print(f"lets-karaoke 环境检查 · {profile['label']}")
    print(f"用途: {profile['purpose']}")
    print(f"项目目录: {ROOT}")
    print("=" * 72)

    if sys.version_info >= (3, 11):
        line("Python", f"{sys.version.split()[0]} ({sys.executable})", "PASS")
    else:
        line("Python", f"需要 3.11+，当前 {sys.version.split()[0]}", "FAIL")
        failures += 1

    if shutil.which("pip") or shutil.which("python"):
        line("pip", "可用", "PASS")
    else:
        line("pip", "未找到", "FAIL")
        failures += 1

    print("\nPython 依赖")
    for key in profile["packages"]:
        module_name, display_name = PACKAGE_INFO[key]
        try:
            module = importlib.import_module(module_name)
            version = getattr(module, "__version__", "已安装")
            line(display_name, str(version), "PASS")
        except Exception as exc:
            line(display_name, f"不可用（{type(exc).__name__}: {exc}）", "FAIL")
            failures += 1

    print("\n媒体工具")
    for command in ("ffmpeg", "ffprobe"):
        version = command_version(command)
        if version:
            line(command, version, "PASS")
        else:
            line(command, "未找到。请安装 FFmpeg 并加入 PATH", "FAIL")
            failures += 1

    if profile["cuda"]:
        print("\nCUDA")
        try:
            torch = importlib.import_module("torch")
            cuda_version = getattr(torch.version, "cuda", None) or "未知"
            if torch.cuda.is_available():
                count = torch.cuda.device_count()
                for index in range(count):
                    props = torch.cuda.get_device_properties(index)
                    memory = props.total_memory / (1024 ** 3)
                    line(f"GPU {index}", f"{props.name}, {memory:.1f} GiB", "PASS")
                line("CUDA runtime", f"{cuda_version}，{count} 个设备", "PASS")
            else:
                line("CUDA", f"不可用（PyTorch CUDA={cuda_version}）", "FAIL")
                failures += 1
        except Exception as exc:
            line("CUDA", f"检查失败（{type(exc).__name__}: {exc}）", "FAIL")
            failures += 1
    else:
        line("CUDA", "此 profile 不需要 GPU", "PASS")

    print("\n模型")
    for kind in profile["models"]:
        ok, detail = model_status(kind)
        if ok:
            line(kind, f"就绪，{detail}", "PASS")
        else:
            line(kind, f"缺失或未下载，{detail}", "WARN")
            warnings += 1
    if not profile["models"]:
        line("模型", "此 profile 不需要模型权重", "PASS")

    usage = shutil.disk_usage(ROOT)
    free_gib = usage.free / (1024 ** 3)
    disk_state = "PASS" if free_gib >= profile["disk_gib"] else "WARN"
    line("可用磁盘", f"{free_gib:.1f} GiB（建议至少 {profile['disk_gib']} GiB）", disk_state)
    if free_gib < profile["disk_gib"]:
        warnings += 1

    print("\n" + "=" * 72)
    if failures:
        print(f"检查未通过：{failures} 项必须修复，{warnings} 项提醒。")
        print("可重新运行 setup_guide.bat 中对应的 profile。")
        return 1
    if warnings:
        print(f"依赖环境已就绪：{warnings} 项提醒。可以继续下载对应模型。")
    else:
        print("环境完整，可以使用该 profile 的功能。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
