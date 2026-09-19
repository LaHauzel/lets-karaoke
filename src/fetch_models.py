"""模型拉取（本地优先策略的入口）

  Qwen3-ForcedAligner-0.6B   强制对齐后端    ~1.8 GB
  Qwen3-ASR-1.7B             语音转写后端    ~4.7 GB（无歌词文件时用它生成歌词）
  wav2vec2 基线模型          由 transformers 首次使用时自动下到 models/hf

下载通道（依次尝试，全部不依赖外网代理）：
  1. ModelScope          —— 国内 CDN，最快，支持断点续传
  2. hf-mirror.com       —— 国内 HF 镜像，走 huggingface_hub 原生协议
  3. huggingface.co      —— 官方源 + 127.0.0.1:7892 代理（仅当本地代理在跑）

用法：
  python src/fetch_models.py                # 拉全部缺失模型
  python src/fetch_models.py --only asr     # 只拉 ASR
  python src/fetch_models.py --hf           # 跳过 ModelScope，直接走 HF 镜像
  python src/fetch_models.py --force        # 已存在也重拉
  python src/fetch_models.py --list         # 只看状态，不下载
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 提前导入，保证 _PROXY_PATCH_TARGETS 里的模块已在 sys.modules 中可被打补丁
import requests  # noqa: F401  (仅为代理补丁服务)
import urllib.request  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"


@dataclass(frozen=True)
class Spec:
    key: str
    repo: str
    directory: Path
    note: str

    @property
    def path(self) -> Path:
        return self.directory

    def have(self) -> bool:
        """权重 + 配置齐了才算有。分片权重也算。"""
        if not self.directory.exists():
            return False
        names = {p.name for p in self.directory.iterdir()}
        has_w = any(
            n.endswith((".safetensors", ".bin")) or ".safetensors.index.json" in n
            for n in names
        )
        return has_w and "config.json" in names

    def size_mb(self) -> float:
        if not self.directory.exists():
            return 0.0
        return sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file()) / 1e6


SPECS: dict[str, Spec] = {
    "aligner": Spec(
        "aligner", "Qwen/Qwen3-ForcedAligner-0.6B",
        MODELS / "Qwen3-ForcedAligner-0.6B", "强制对齐（逐字时间戳）",
    ),
    "asr": Spec(
        "asr", "Qwen/Qwen3-ASR-1.7B",
        MODELS / "Qwen3-ASR-1.7B", "语音转写（无歌词文件时生成歌词草稿）",
    ),
}

_ENV_KEYS = [
    "HF_ENDPOINT", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
    "ALL_PROXY", "all_proxy", "no_proxy", "NO_PROXY",
    "HF_HUB_ENABLE_HF_TRANSFER",
]

# 本机 / WorkBuddy 会把 HTTP_PROXY 指向一个动态端口的本地代理（实测 5499），
# 该代理解不了 huggingface.co，会让所有 HF 下载 502。而 Windows 上
# requests 的 get_environ_proxies() 还会调用 urllib.getproxies() 去读
# 注册表（Internet Settings），**光清环境变量不够**，必须把这个函数也顶掉。
_PROXY_PATCH_TARGETS = [
    ("urllib.request", "getproxies"),
    ("requests.utils", "getproxies"),
    ("requests.utils", "getproxies_environment"),
]


@contextlib.contextmanager
def _clean_net_ctx(extra_env: dict | None = None):
    """临时摘除所有代理来源（环境变量 + 注册表 getproxies），可选注入额外变量。"""
    saved_env = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    if extra_env:
        os.environ.update(extra_env)

    saved_fns = []
    for mod_name, attr in _PROXY_PATCH_TARGETS:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        if hasattr(mod, attr):
            saved_fns.append((mod, attr, getattr(mod, attr)))
            setattr(mod, attr, lambda: {})
    try:
        yield
    finally:
        for mod, attr, fn in saved_fns:
            setattr(mod, attr, fn)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def via_modelscope(spec: Spec) -> bool:
    """ModelScope 国内 CDN，实测最稳（不受本地代理影响）。"""
    print(f"  [modelscope] {spec.repo} -> {spec.directory}", flush=True)
    with _clean_net_ctx():
        # 首选 Python API：`python -m modelscope` 在 1.40 已不是合法入口
        try:
            from modelscope import snapshot_download as ms_download

            ms_download(spec.repo, local_dir=str(spec.directory))
            if spec.have():
                return True
            print("    modelscope API 下载完成但文件不全，回退 CLI", flush=True)
        except Exception as e:
            print(f"    modelscope API 失败: {type(e).__name__}: {str(e)[:160]}", flush=True)

        exe = Path(sys.executable).with_name("modelscope.exe")
        cmd = [str(exe) if exe.exists() else "modelscope", "download",
               "--model", spec.repo, "--local_dir", str(spec.directory)]
        try:
            r = subprocess.run(cmd, env=dict(os.environ))
            return r.returncode == 0 and spec.have()
        except Exception as e:
            print(f"    modelscope CLI 异常: {type(e).__name__}: {e}", flush=True)
            return False


def via_hf(spec: Spec, proxy_first: bool = False) -> bool:
    """两条 HF 通道：国内镜像（直连）/ 官方源+7892 代理。"""
    try:
        from huggingface_hub import snapshot_download
    except Exception as e:
        print(f"    huggingface_hub 不可用: {e}", flush=True)
        return False

    mirror = ("hf-mirror", {"HF_ENDPOINT": "https://hf-mirror.com"})
    official = (
        "hf-official+proxy",
        {"HF_ENDPOINT": "https://huggingface.co",
         "HTTPS_PROXY": "http://127.0.0.1:7892", "HTTP_PROXY": "http://127.0.0.1:7892"},
    )
    routes = [official, mirror] if proxy_first else [mirror, official]

    for label, sets in routes:
        print(f"  [{label}] {spec.repo} -> {spec.directory}", flush=True)
        extra = dict(sets)
        extra["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        with _clean_net_ctx(extra):
            try:
                snapshot_download(
                    repo_id=spec.repo, local_dir=str(spec.directory),
                    max_workers=4, max_retries=3,
                )
                if spec.have():
                    return True
                print("    下载完成但文件不全", flush=True)
            except Exception as e:
                print(f"    {label} 失败: {type(e).__name__}: {str(e)[:160]}", flush=True)
    return False


def fetch(spec: Spec, prefer_hf: bool = False) -> bool:
    if spec.have():
        print(f"[skip] {spec.key}: 已存在 ({spec.size_mb():.0f} MB)")
        return True
    print(f"[fetch] {spec.key}: {spec.note}  预计 {_EXPECT.get(spec.key, '?')}")
    ok = via_hf(spec, proxy_first=True) if prefer_hf else (via_modelscope(spec) or via_hf(spec))
    if ok and spec.have():
        print(f"[ok] {spec.key} = {spec.directory}  ({spec.size_mb():.0f} MB)")
        return True
    print(f"!! {spec.key} 拉取失败：{spec.repo}\n   可手动下载后放入 {spec.directory}")
    return False


_EXPECT = {"aligner": "~1.8 GB", "asr": "~4.7 GB"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hf", action="store_true", help="优先 HuggingFace 镜像（跳过 ModelScope）")
    ap.add_argument("--force", action="store_true", help="已存在也重新拉取")
    ap.add_argument("--only", choices=sorted(SPECS), help="只处理指定模型")
    ap.add_argument("--list", action="store_true", help="只打印状态")
    args = ap.parse_args()

    MODELS.mkdir(parents=True, exist_ok=True)
    (MODELS / "hf").mkdir(parents=True, exist_ok=True)

    keys = [args.only] if args.only else list(SPECS)

    if args.list:
        print(f"{'key':10s} {'状态':8s} {'大小':>10s}  说明")
        for k in keys:
            s = SPECS[k]
            print(f"{k:10s} {'OK' if s.have() else '缺失':8s} {s.size_mb():9.0f}MB  {s.note}")
        return 0

    failed = []
    for k in keys:
        s = SPECS[k]
        if args.force and s.directory.exists():
            print(f"[force] {k}: 重拉（旧文件会被覆盖/补齐，不做删除）")
        if not fetch(s, prefer_hf=args.hf):
            failed.append(k)

    print("\n=== 汇总 ===")
    for k in keys:
        s = SPECS[k]
        print(f"  {k:10s} {'OK' if s.have() else 'FAIL':5s} {s.size_mb():8.0f} MB  {s.directory}")
    print(f"  HF 缓存 = {MODELS/'hf'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
