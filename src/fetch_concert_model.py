"""Download and verify the small Apache-2.0 YAMNet model used in concert review."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "models" / "concert"
REVISION = "ac2ca3bd45d12ec1f19f1144205ea529b4e9dedf"
BASE_URL = f"https://huggingface.co/zeropointnine/yamnet-onnx/resolve/{REVISION}/"
FILES = {
    "yamnet.onnx": (16_093_603, "1510041dce24a2e9e84ec546807ac408ae496da6d1ed41bc3ccba649623f8e19"),
    "yamnet_class_map.csv": (14_096, "cdf24d193e196d9e95912a2667051ae203e92a2ba09449218ccb40ef787c6df2"),
}


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid(path: Path, expected_size: int, expected_hash: str) -> bool:
    return path.is_file() and path.stat().st_size == expected_size and file_hash(path) == expected_hash


def download(name: str, expected_size: int, expected_hash: str, force: bool = False) -> bool:
    target = DEST / name
    if not force and valid(target, expected_size, expected_hash):
        print(f"[skip] {name} 已存在并通过 SHA-256 校验")
        return True

    DEST.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    partial.unlink(missing_ok=True)
    request = urllib.request.Request(BASE_URL + name, headers={"User-Agent": "lets-karaoke/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            total = int(response.headers.get("Content-Length") or expected_size)
            done = 0
            with partial.open("wb") as stream:
                while True:
                    block = response.read(1024 * 256)
                    if not block:
                        break
                    stream.write(block)
                    done += len(block)
                    percent = min(100, int(done * 100 / max(1, total)))
                    print(f"\r[download] {name}: {percent:3d}%  {done/1024/1024:.2f}/{total/1024/1024:.2f} MiB", end="", flush=True)
        print()
        if not valid(partial, expected_size, expected_hash):
            raise ValueError(f"{name} 下载文件大小或 SHA-256 不匹配")
        os.replace(partial, target)
        print(f"[ok] {target}")
        return True
    except Exception as exc:
        print(f"\n[fail] {name}: {type(exc).__name__}: {exc}")
        partial.unlink(missing_ok=True)
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download the optional concert speech/music classifier")
    parser.add_argument("--status", action="store_true", help="check files without downloading")
    parser.add_argument("--force", action="store_true", help="download again even if verified files exist")
    args = parser.parse_args(argv)

    if args.status:
        ok = all(valid(DEST / name, size, digest) for name, (size, digest) in FILES.items())
        print(f"YAMNet 音频分类模型：{'已安装' if ok else '缺失或校验失败'} ({DEST})")
        return 0 if ok else 1

    print("YAMNet 用于区分讲话与音乐，约 15.4 MiB；只在本机 CPU 上运行。")
    result = all(download(name, size, digest, args.force) for name, (size, digest) in FILES.items())
    if result:
        print("演唱会讲话/音乐分类模型已就绪。")
        return 0
    print("模型未完成。基础边界分析仍可用；可修复网络后重新运行此脚本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
