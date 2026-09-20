"""Detailed local environment diagnostic for the Windows WebUI."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


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


def model_status(directory: Path) -> tuple[bool, float]:
    if not directory.exists():
        return False, 0.0
    files = [p for p in directory.rglob("*") if p.is_file()]
    complete = (directory / "config.json").is_file() and any(
        p.name.endswith((".safetensors", ".bin")) or ".safetensors.index.json" in p.name
        for p in files
    )
    return complete, sum(p.stat().st_size for p in files) / (1024 ** 3)


def main() -> int:
    failures = 0
    warnings = 0
    print("lets-karaoke 环境检查")
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
    packages = (
        ("numpy", "numpy"), ("scipy", "scipy"), ("soundfile", "soundfile"),
        ("librosa", "librosa"), ("torch", "torch"), ("torchaudio", "torchaudio"),
        ("whisper", "whisper"), ("stable_whisper", "stable-ts"),
        ("demucs", "demucs"), ("qwen_asr", "qwen-asr"),
        ("transformers", "transformers"), ("huggingface_hub", "huggingface-hub"),
        ("pykakasi", "pykakasi"), ("rapidfuzz", "rapidfuzz"),
    )
    for module_name, display_name in packages:
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

    print("\n模型与磁盘")
    for name, directory, estimate in (
        ("ForcedAligner", MODELS / "Qwen3-ForcedAligner-0.6B", "约 1.8 GB"),
        ("ASR", MODELS / "Qwen3-ASR-1.7B", "约 4.7 GB"),
    ):
        complete, size = model_status(directory)
        if complete:
            line(name, f"就绪，{size:.2f} GiB（{directory}）", "PASS")
        else:
            line(name, f"缺失或不完整，下载量 {estimate}（{directory}）", "WARN")
            warnings += 1
    usage = shutil.disk_usage(ROOT)
    free_gib = usage.free / (1024 ** 3)
    disk_state = "PASS" if free_gib >= 10 else "WARN"
    line("可用磁盘", f"{free_gib:.1f} GiB", disk_state)
    if free_gib < 10:
        warnings += 1

    print("\n" + "=" * 72)
    if failures:
        print(f"检查未通过：{failures} 项必须修复，{warnings} 项提醒。")
        print("先运行 setup_guide.bat 的“安装或修复依赖”，再重新检查。")
        return 1
    if warnings:
        print(f"基础环境已就绪：{warnings} 项提醒。可从 setup_guide.bat 下载缺失模型。")
    else:
        print("环境完整，可以启动 WebUI。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
