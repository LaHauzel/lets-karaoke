# -*- coding: utf-8 -*-
"""whisper 在 Windows 上的 triton 兼容补丁。

whisper.timing 的 triton 内核入口（``median_filter_cuda`` / ``dtw_cuda``）只捕获
``RuntimeError``；Windows 上 triton 编译会抛 ``FileNotFoundError``（缓存目录 /
内核元数据问题），导致整个对齐流程崩溃。

把这两个入口包一层：任何异常都转成 ``RuntimeError``，让 whisper 走它自带的
CPU 回退（unfold+sort 的中值滤波 / numba 版 DTW）。速度可接受：356s 现场
音频 + large-v3 约 19 分钟。
"""
from __future__ import annotations


def patch_whisper_triton() -> list[str]:
    """包装 whisper 的 triton 入口，返回实际打了补丁的符号名列表。"""
    try:
        import whisper.timing as wt
    except Exception:
        return []
    try:
        import whisper.triton_ops as wto
    except Exception:
        wto = None

    def _reraise(fn):
        def inner(*a, **k):
            try:
                return fn(*a, **k)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"triton kernel failed: {e}") from e

        return inner

    targets = [(wt, ("dtw_cuda", "median_filter_cuda"))]
    if wto is not None:
        targets.append((wto, ("median_filter_cuda", "dtw_cuda")))
    patched: list[str] = []
    for mod, names in targets:
        for n in names:
            if hasattr(mod, n):
                setattr(mod, n, _reraise(getattr(mod, n)))
                patched.append(f"{mod.__name__}.{n}")
    return patched
