"""Read-only environment check, using the Python selected on PATH."""
import importlib
import shutil
import sys


def main():
    print(f"Python: {sys.executable} ({sys.version.split()[0]})")
    errors = []
    for name in ("numpy", "soundfile", "librosa", "torch", "torchaudio",
                 "whisper", "stable_whisper", "demucs", "qwen_asr", "pykakasi"):
        try:
            module = importlib.import_module(name)
            print(f"OK {name}: {getattr(module, '__version__', 'imported')}")
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        print(f"{name}: {found}")
        if not found:
            errors.append(f"{name} missing from PATH")
    try:
        import torch
        if torch.cuda.is_available():
            x = torch.ones(16, device="cuda")
            assert (x @ x).item() == 16
            print(f"CUDA compute OK: {torch.cuda.get_device_name(0)}")
        else:
            errors.append("CUDA unavailable; install matching GPU torch/torchaudio wheels")
    except Exception as exc:
        errors.append(f"CUDA: {exc}")
    for error in errors:
        print(f"FAIL {error}")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
