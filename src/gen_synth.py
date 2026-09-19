"""P0 验证 · 合成真值测试集生成

思路：
  逐 token 用本地 TTS 合成 -> 按幅度阈值裁掉首尾静音 -> 按已知偏移拼接。
  这样每个 token 的起止时间由构造过程精确决定，是**确定性真值**，
  不存在人工标注误差。

TTS 后端：
  默认 sapi —— Windows SAPI5（完全离线，不依赖任何在线 API）。
  可选 edge —— edge-tts（联网），需显式 `--tts edge`。

两个难度档：
  gapped   token 之间留 120 ms 静音  —— 边界分离，容易
  legato   token 之间零间隔          —— 连续音频，更接近演唱时的边界定位难度

局限（必须如实说明）：
  合成集测的是「语音域 + 孤立音节」的边界定位能力，不等于歌声域表现。
  它的作用是校验评测管线端到端正确 + 给出语音域的量级参考。

歌词文本为自撰无版权短句，仅用于测试。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_tts import BACKENDS, resolve_voice_name, synth_array  # noqa: E402
from p0_common import (  # noqa: E402
    LANGS,
    SR,
    TokenSpan,
    dump_json,
    tokenize,
    write_wav,
)

# 自撰测试歌词（无版权问题）
LYRICS: dict[str, list[str]] = {
    "zh": ["东风送来清凉", "小船划过池塘", "月色照在桥上"],
    "en": ["the morning light is warm", "we walk along the shore", "and listen to the rain"],
    "ja": ["朝の光が", "空を渡って", "風が吹いてる"],
}

GAP_GAPPED = 0.120  # 秒
GAP_LEGATO = 0.0


def _tts_one(text: str, lang: str, backend: str) -> np.ndarray | None:
    """合成单个 token，返回 16 kHz 单声道 float32（已裁静音）。失败返回 None。"""
    for attempt in range(3):
        try:
            a = synth_array(text, lang, backend=backend)
            if a.size == 0:
                raise RuntimeError("decoded empty")
            return a
        except Exception:
            if attempt == 2:
                return None
    return None


def _make_bed(n: int, sr: int = SR, seed: int = 20260912) -> np.ndarray:
    """合成一段「类音乐」伴奏床：和弦 + 低音 + 打击点。

    目的不是好听，而是让它在频谱上与语音重叠，从而真实地干扰对齐模型。
    和弦进行按 4 秒一小节循环。
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float32) / sr
    bed = np.zeros(n, dtype=np.float32)

    # 和弦进行（半音偏移，A 小调情绪）
    prog = [(57, 60, 64), (55, 59, 62), (53, 57, 60), (55, 59, 64)]
    bar = 4.0

    for ci, chord in enumerate(prog):
        m = ((t >= ci * bar) & (t < (ci + 1) * bar))
        if not m.any():
            continue
        seg = t[m]
        for note in chord:
            f = 440.0 * (2 ** ((note - 69) / 12.0))
            env = 0.5 - 0.5 * np.cos(2 * np.pi * np.clip((seg - ci * bar) / bar, 0, 1))
            bed[m] += (0.16 * env * np.sin(2 * np.pi * f * seg)).astype(np.float32)
            bed[m] += (0.07 * env * np.sin(2 * np.pi * 2 * f * seg)).astype(np.float32)  # 二次谐波
        # 低音
        fb = 440.0 * (2 ** ((chord[0] - 12 - 69) / 12.0))
        bed[m] += (0.20 * np.sin(2 * np.pi * fb * seg)).astype(np.float32)

    # 打击点：每 0.5 秒一个衰减噪声脉冲，模拟鼓组
    if n > 0:
        beat = 0.5
        ln = int(0.08 * sr)
        n_beats = int(float(t[-1]) / beat) + 1
        for k in range(n_beats):
            s = int(k * beat * sr)
            if s + ln >= n:
                break
            noise = rng.normal(0, 1, ln).astype(np.float32)
            decay = np.exp(-np.arange(ln) / (0.02 * sr)).astype(np.float32)
            bed[s:s + ln] += 0.22 * noise * decay

    peak = float(np.max(np.abs(bed))) or 1.0
    return bed / peak


def _mix_at_snr(speech: np.ndarray, bed: np.ndarray, snr_db: float) -> np.ndarray:
    """把 bed 混进 speech，使语音相对伴奏的功率比为 snr_db。"""
    n = max(speech.size, bed.size)
    sp = np.zeros(n, dtype=np.float32)
    bd = np.zeros(n, dtype=np.float32)
    sp[: speech.size] = speech
    bd[: bed.size] = bed

    ps = float(np.mean(sp ** 2)) or 1e-12
    pb = float(np.mean(bd ** 2)) or 1e-12
    target_pb = ps / (10 ** (snr_db / 10.0))
    bd = bd * np.sqrt(target_pb / pb)

    out = sp + bd
    peak = float(np.max(np.abs(out))) or 1.0
    if peak > 0.99:
        out = out * (0.99 / peak)
    return out.astype(np.float32)


def _concat(tokens: list[np.ndarray], gap: float, tail_silence: float = 0.30) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """按 gap 拼接，返回音频与每个 token 的 (start, end)。"""
    gap_n = int(round(gap * SR))
    parts: list[np.ndarray] = []
    spans: list[tuple[float, float]] = []
    cur = 0
    for i, clip in enumerate(tokens):
        if i > 0 and gap_n > 0:
            parts.append(np.zeros(gap_n, dtype=np.float32))
            cur += gap_n
        start = cur / SR
        parts.append(clip)
        cur += clip.size
        spans.append((start, cur / SR))
    parts.append(np.zeros(int(round(tail_silence * SR)), dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32), spans


def build_lang(lang: str, out_dir: Path, backend: str = "sapi", concurrency: int = 4, snr_db: float = 4.0) -> dict | None:
    spec = LANGS[lang]
    lines = LYRICS[lang]
    per_line: list[list[str]] = [tokenize(x, spec) for x in lines]
    flat: list[str] = [t for ln in per_line for t in ln]

    voice = resolve_voice_name(lang, backend)
    print(f"[{lang}] {len(flat)} tokens, tts={backend}, voice={voice}")

    # 去重后合成：同一 token 只合成一次，既快又保证同词音长完全一致
    uniq = list(dict.fromkeys(flat))
    with cf.ThreadPoolExecutor(max_workers=concurrency) as ex:
        got = list(ex.map(lambda t: _tts_one(t, lang, backend), uniq))
    cache: dict[str, np.ndarray] = {t: a for t, a in zip(uniq, got) if a is not None}
    results = [cache.get(t) for t in flat]

    clips: list[np.ndarray] = []
    kept: list[str] = []
    for t, a in zip(flat, results):
        if a is None:
            print(f"  !! TTS failed, dropped token: {t!r}")
            continue
        clips.append(a)
        kept.append(t)

    if len(kept) < 6:
        print(f"  !! too few usable tokens ({len(kept)}), skip language")
        return None

    # 丢词后不再维持行结构，直接按扁平 token 序列输出（评测不依赖行结构）
    base = out_dir / lang
    base.mkdir(parents=True, exist_ok=True)

    out: dict = {
        "lang": lang,
        "lang_name": spec.name,
        "tts_backend": backend,
        "voice": voice,
        "granularity": spec.granularity,
        "n_tokens": len(kept),
        "n_unique_tokens": len(uniq),
        "truncated": len(flat) - len(kept),
        "lyrics_lines": lines,
        "tokens": kept,
        "variants": {},
    }

    for gap_name, gap in (("gapped", GAP_GAPPED), ("legato", GAP_LEGATO)):
        speech, spans = _concat(clips, gap)
        truth_payload = [
            {"text": ts.text, "start": round(ts.start, 4), "end": round(ts.end, 4)}
            for ts in (TokenSpan(text=t, start=s, end=e) for t, (s, e) in zip(kept, spans))
        ]
        bed = _make_bed(speech.size, SR)
        conds = {
            "clean": speech,
            "mix": _mix_at_snr(speech, bed, snr_db),
        }
        for cond, audio in conds.items():
            vname = f"{gap_name}_{cond}"
            wav_path = base / f"{lang}_{vname}.wav"
            write_wav(wav_path, audio)
            out["variants"][vname] = {
                "gap_seconds": gap,
                "bed": None if cond == "clean" else f"synthetic_bed@{snr_db}dB",
                "audio": str(wav_path),
                "duration": round(audio.size / SR, 4),
                "truth": truth_payload,
            }
            print(f"  -> {wav_path.name}  dur={audio.size / SR:.2f}s  tokens={len(truth_payload)}")

    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 P0 合成真值测试集（本地 TTS）")
    ap.add_argument("--out", default="data/synth", help="输出目录")
    ap.add_argument("--langs", nargs="+", default=["zh", "en", "ja"])
    ap.add_argument("--tts", choices=BACKENDS, default="sapi", help="TTS 后端，默认本地 sapi")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--snr-db", type=float, default=4.0, help="合成伴奏相对语音的信噪比（dB）")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "sample_rate": SR,
        "source": f"{args.tts} per-token synthesis"
        + (" (local, offline)" if args.tts == "sapi" else " (online)"),
        "langs": {},
    }

    for lang in args.langs:
        r = build_lang(lang, out_dir, backend=args.tts, concurrency=args.concurrency, snr_db=args.snr_db)
        if r:
            manifest["langs"][lang] = r

    dump_json(out_dir / "manifest.json", manifest)
    total = sum(v["n_tokens"] for v in manifest["langs"].values())
    print(f"\nmanifest -> {out_dir / 'manifest.json'}")
    print(f"total tokens: {total}, langs: {list(manifest['langs'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
