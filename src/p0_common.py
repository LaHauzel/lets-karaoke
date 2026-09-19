"""P0 验证 · 公共模块

职责：
  - 三语言（中/英/日）配置与分词
  - 音频读取（统一 16 kHz 单声道 float32）
  - 定时真值 / 对齐结果的误差指标

设计约定：
  - 所有时间单位统一为「秒」（float）
  - 分词粒度对齐卡拉OK实践：中文/日文按字符，英文按词
"""

from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

SR = 16000  # 对齐模型统一采样率

# --------------------------------------------------------------------------
# 语言配置
# --------------------------------------------------------------------------

# 中日文保留的表意/假名区间
_CJK_RANGES = (
    (0x3040, 0x309F),  # 平假名
    (0x30A0, 0x30FF),  # 片假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK 统一表意
    (0xF900, 0xFAFF),  # CJK 兼容表意
)


@dataclass
class LangSpec:
    code: str
    name: str
    granularity: str  # "char" | "word"
    tts_voice: str  # 仅在线 edge-tts 后端使用；本地 sapi 语音由 local_tts 按语言自动选
    wav2vec2_model: str | None = None  # 基线后端使用；None 表示该后端不支持
    note: str = ""


LANGS: dict[str, LangSpec] = {
    "zh": LangSpec(
        code="zh",
        name="中文",
        granularity="char",
        tts_voice="zh-CN-XiaoxiaoNeural",
        wav2vec2_model="jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn",
        note="按字切分（字≈音节），与 ASS \\k 逐字高亮一致",
    ),
    "en": LangSpec(
        code="en",
        name="英文",
        granularity="word",
        tts_voice="en-US-AriaNeural",
        wav2vec2_model="facebook/wav2vec2-base-960h",
        note="按词切分",
    ),
    "ja": LangSpec(
        code="ja",
        name="日文",
        granularity="char",
        tts_voice="ja-JP-NanamiNeural",
        wav2vec2_model="jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
        note="按字符切分；注意一个汉字可能唱成多个摩拉，是多摩拉字的风险点",
    ),
}


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def tokenize(text: str, spec: LangSpec) -> list[str]:
    """按语言粒度切分为 token 列表，丢弃标点与空白。"""
    text = unicodedata.normalize("NFKC", text)

    if spec.granularity == "char":
        out = []
        for ch in text:
            if ch.isspace():
                continue
            cat = unicodedata.category(ch)
            if cat.startswith("P") or cat.startswith("S"):
                continue  # 标点/符号
            if spec.code == "ja":
                # 日文额外允许长音符
                if ch in ("ー", "・"):
                    continue
            if is_cjk(ch) or (spec.code == "en"):
                out.append(ch)
            elif spec.code == "ja" and ch.isascii() and ch.isalpha():
                out.append(ch)  # 日文里的拉丁字母（如歌词里的英文单词）保留
        return out

    # word 粒度
    parts = re.split(r"[\s\u3000]+", text.strip())
    out = []
    for p in parts:
        p = p.strip()
        p = p.strip("".join(chr(c) for c in range(0x21, 0x2F)))
        p = re.sub(r"^[^\w']+|[^\w']+$", "", p)
        if p:
            out.append(p)
    return out


# --------------------------------------------------------------------------
# 音频读写
# --------------------------------------------------------------------------


def ffmpeg_exe() -> str:
    return "ffmpeg"


def load_audio_16k(path: str | Path) -> np.ndarray:
    """任何格式 -> 16 kHz 单声道 float32 numpy。走 ffmpeg，避免 libsndfile 的 mp3/flac 支持问题。"""
    cmd = [
        ffmpeg_exe(), "-nostdin", "-v", "error",
        "-i", str(path),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-ac", "1", "-ar", str(SR),
        "-",
    ]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def write_wav(path: str | Path, audio: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), audio, sr, subtype="PCM_16")


def probe_duration(path: str | Path) -> float:
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    return float(subprocess.run(cmd, capture_output=True, check=True).stdout.decode().strip())


def trim_silence(
    audio: np.ndarray,
    sr: int = SR,
    rel_db: float = -42.0,
    pad_ms: float = 8.0,
) -> np.ndarray:
    """按相对幅度阈值裁掉首尾静音，首尾各留 pad_ms 余量。

    用于合成测试集制造精确已知的边界。阈值取相对峰值，避免绝对值在不同
    音量素材上失效。
    """
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return audio
    thr = peak * (10 ** (rel_db / 20.0))
    idx = np.where(np.abs(audio) > thr)[0]
    if idx.size == 0:
        return audio
    pad = int(sr * pad_ms / 1000.0)
    a = max(0, int(idx[0]) - pad)
    b = min(audio.size, int(idx[-1]) + 1 + pad)
    return audio[a:b]


# --------------------------------------------------------------------------
# 时间轴数据结构与指标
# --------------------------------------------------------------------------


@dataclass
class TokenSpan:
    text: str
    start: float
    end: float
    # 该 token 落到哪个对齐单元上。日文里一个单元常含多个字
    # （如 nagisa 把「渡っ」并成一个），据此可以选做「整单元高亮」。
    unit: int | None = None


@dataclass
class AlignResult:
    backend: str
    pipeline: str
    lang: str
    spans: list[TokenSpan] = field(default_factory=list)
    audio_seconds: float = 0.0
    wall_seconds: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "backend": self.backend,
            "pipeline": self.pipeline,
            "lang": self.lang,
            "audio_seconds": round(self.audio_seconds, 3),
            "wall_seconds": round(self.wall_seconds, 3),
            "rtf": round(self.wall_seconds / self.audio_seconds, 4) if self.audio_seconds else None,
            "error": self.error,
            "spans": [{"text": s.text, "start": round(s.start, 4), "end": round(s.end, 4)} for s in self.spans],
        }


def boundary_metrics(truth: list[TokenSpan], pred: list[TokenSpan]) -> dict:
    """逐 token 边界误差。

    只用起始边界做主体统计（卡拉OK高亮由起始时刻驱动），同时给出结束边界
    的 MAE 作为参考。文本不一致的 token 会被跳过并计入 skipped。
    """
    n = min(len(truth), len(pred))
    starts, ends, skipped = [], [], 0
    for i in range(n):
        if truth[i].text != pred[i].text:
            skipped += 1
            continue
        starts.append(pred[i].start - truth[i].start)
        ends.append(pred[i].end - truth[i].end)

    if not starts:
        return {"n": 0, "skipped": skipped, "ok": False}

    s = np.array(starts, dtype=np.float64) * 1000.0  # ms
    e = np.array(ends, dtype=np.float64) * 1000.0

    def block(x: np.ndarray, tag: str) -> dict:
        ax = np.abs(x)
        return {
            f"{tag}_mae_ms": float(np.mean(ax)),
            f"{tag}_median_ms": float(np.median(ax)),
            f"{tag}_p90_ms": float(np.percentile(ax, 90)),
            f"{tag}_bias_ms": float(np.mean(x)),
            f"{tag}_hit25": float(np.mean(ax <= 25) * 100),
            f"{tag}_hit50": float(np.mean(ax <= 50) * 100),
            f"{tag}_hit100": float(np.mean(ax <= 100) * 100),
        }

    out = {"n": len(starts), "skipped": skipped, "ok": True, "n_truth": len(truth), "n_pred": len(pred)}
    out.update(block(s, "start"))
    out.update(block(e, "end"))
    return out


def dump_json(path: str | Path, obj) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
