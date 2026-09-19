"""P0 验证 · 本地 TTS 后端

原则：**默认完全离线**，不依赖任何在线 API。

后端：
  sapi  —— Windows SAPI5（默认）。使用系统已安装的语音合成引擎，
           中/英/日各有一个 Desktop 语音可用，零下载、零联网。
  edge  —— edge-tts（联网）。仅作为可选对照，需显式 `--tts edge` 才启用，
           不作为默认路径。

用法：
  from local_tts import synth, list_sapi_voices
  synth("东风", "zh", "out.wav")            # 默认 sapi
  synth("东风", "zh", "out.wav", "edge")    # 显式走在线

自检：
  python src/local_tts.py --selftest --out data/_tts_selftest
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from p0_common import SR, load_audio_16k, trim_silence  # noqa: E402

# SAPI 语言标签（十六进制字符串，LCID）
SAPI_LANG_TAGS: dict[str, tuple[str, ...]] = {
    "zh": ("804", "1004", "404"),   # 简体 / 繁体 / 通用
    "en": ("409", "809", "9"),      # en-US / en-GB / en
    "ja": ("411",),
}

# 语音名关键词偏好（靠前者优先）。Desktop 语音是 SAPI5 可直接驱动的那一批。
SAPI_VOICE_HINT: dict[str, tuple[str, ...]] = {
    "zh": ("huihui", "kangkang", "yaoyao", "chinese"),
    "en": ("zira", "david", "mark", "hazel", "english"),
    "ja": ("haruka", "ichiro", "ayumi", "sayaka", "japanese"),
}

# edge-tts 语音（仅当显式选用在线后端时使用）
EDGE_VOICES: dict[str, str] = {
    "zh": "zh-CN-XiaoxiaoNeural",
    "en": "en-US-AriaNeural",
    "ja": "ja-JP-NanamiNeural",
}

_tls = threading.local()


# --------------------------------------------------------------------------
# Windows SAPI5
# --------------------------------------------------------------------------


def _new_sapi_voice():
    """在当前线程创建 SpVoice。COM 需按线程初始化。"""
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    return win32com.client.Dispatch("SAPI.SpVoice")


def _sapi():
    v = getattr(_tls, "voice", None)
    if v is None:
        v = _new_sapi_voice()
        _tls.voice = v
    return v


def list_sapi_voices() -> list[dict]:
    """列出系统可用语音。返回 [{desc, name, language, id}]。"""
    try:
        sp = _sapi()
        out = []
        for tok in sp.GetVoices():
            try:
                lang = tok.GetAttribute("Language")
            except Exception:
                lang = ""
            out.append(
                {
                    "desc": tok.GetDescription(),
                    "name": tok.GetAttribute("Name"),
                    "language": str(lang),
                    "id": tok.Id,
                }
            )
        return out
    except Exception:
        return []


def pick_sapi_voice(lang: str) -> object | None:
    """按语言挑选最合适的 SAPI 语音 token。找不到返回 None。"""
    sp = _sapi()
    tags = SAPI_LANG_TAGS.get(lang, ())
    hints = SAPI_VOICE_HINT.get(lang, ())

    cands: list[tuple[int, object]] = []
    for tok in sp.GetVoices():
        try:
            ltag = str(tok.GetAttribute("Language") or "").lower().lstrip("x")
        except Exception:
            ltag = ""
        try:
            desc = (tok.GetDescription() or "").lower()
        except Exception:
            desc = ""

        score = -1
        for i, t in enumerate(tags):
            if ltag.endswith(t.lower()) or ltag == t.lower():
                score = 100 - i
                break
        if score < 0:
            continue
        for j, h in enumerate(hints):
            if h in desc:
                score += 10 - j
                break
        cands.append((score, tok))

    if not cands:
        return None
    cands.sort(key=lambda x: x[0], reverse=True)
    return cands[0][1]


def sapi_synth(text: str, lang: str, out_wav: str | Path, rate: int = 0, volume: int = 100) -> Path:
    """用 SAPI 合成到 WAV 文件。完全离线。"""
    import win32com.client

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)

    sp = _sapi()
    tok = pick_sapi_voice(lang)
    if tok is None:
        raise RuntimeError(f"no SAPI voice for lang={lang}")
    sp.Voice = tok
    sp.Rate = int(rate)
    sp.Volume = int(volume)

    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    # SSFMCreateForWrite = 3
    stream.Open(str(out_wav), 3, False)
    old = sp.AudioOutputStream
    sp.AudioOutputStream = stream
    try:
        sp.Speak(text)  # 同步阻塞
    finally:
        sp.AudioOutputStream = old
        stream.Close()

    if not out_wav.exists() or out_wav.stat().st_size < 1024:
        raise RuntimeError(f"sapi produced empty file for {text!r}")
    return out_wav


# --------------------------------------------------------------------------
# edge-tts（可选，联网）
# --------------------------------------------------------------------------


def edge_synth(text: str, lang: str, out_wav: str | Path, voice: str | None = None) -> Path:
    """用 edge-tts 合成（需要联网）。输出统一转成 16 kHz 单声道 WAV。"""
    import asyncio
    import os
    import tempfile

    import edge_tts

    from p0_common import write_wav

    out_wav = Path(out_wav)
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    v = voice or EDGE_VOICES.get(lang, "en-US-AriaNeural")

    async def _run(tmp_mp3: Path):
        await edge_tts.Communicate(text, v).save(str(tmp_mp3))

    fd, name = tempfile.mkstemp(prefix="p0edge_", suffix=".mp3")
    os.close(fd)
    tmp = Path(name)
    try:
        asyncio.run(_run(tmp))
        audio = load_audio_16k(tmp)
        write_wav(out_wav, audio, SR)
    finally:
        tmp.unlink(missing_ok=True)
    return out_wav


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

BACKENDS = ("sapi", "edge")


def synth(text: str, lang: str, out_wav: str | Path, backend: str = "sapi", voice: str | None = None) -> Path:
    if backend == "sapi":
        return sapi_synth(text, lang, out_wav)
    if backend == "edge":
        return edge_synth(text, lang, out_wav, voice)
    raise ValueError(f"unknown tts backend: {backend}")


def synth_array(text: str, lang: str, backend: str = "sapi", voice: str | None = None) -> np.ndarray:
    """合成并直接返回 16 kHz float32（已裁首尾静音）。

    注意：临时文件名必须每次唯一。若按 (text, lang) 做哈希，重复 token
    （如英文的 "the" 出现多次）会在并发下争用同一路径，导致写入/删除竞态。
    """
    import os
    import tempfile

    fd, name = tempfile.mkstemp(prefix="p0tts_", suffix=".wav")
    os.close(fd)
    tmp = Path(name)
    try:
        synth(text, lang, tmp, backend, voice)
        return trim_silence(load_audio_16k(tmp), SR)
    finally:
        tmp.unlink(missing_ok=True)


def resolve_voice_name(lang: str, backend: str = "sapi") -> str:
    if backend == "edge":
        return EDGE_VOICES.get(lang, "?")
    tok = pick_sapi_voice(lang)
    try:
        return tok.GetDescription() if tok is not None else "N/A"
    except Exception:
        return "N/A"


# --------------------------------------------------------------------------


def _selftest(out_dir: Path) -> int:
    samples = {
        "zh": "东风送来清凉",
        "en": "the morning light",
        "ja": "朝の光が",
    }
    print("=== SAPI voices on this machine ===")
    for v in list_sapi_voices():
        print(f"  [{v['language']:>7}] {v['desc']}")

    print("\n=== per-lang synthesis ===")
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for lang, text in samples.items():
        try:
            dst = out_dir / f"{lang}.wav"
            synth(text, lang, dst, "sapi")
            a = load_audio_16k(dst)
            dur = a.size / SR
            peak = float(np.max(np.abs(a))) if a.size else 0.0
            rms = float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0
            print(f"  {lang}: voice={resolve_voice_name(lang)!r} dur={dur:.2f}s peak={peak:.3f} rms={rms:.4f} -> {dst.name}")
        except Exception as e:
            print(f"  {lang}: FAILED {type(e).__name__}: {e}")
            rc = 1
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 TTS 自检")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default="data/_tts_selftest")
    a = ap.parse_args()
    if a.selftest:
        return _selftest(Path(a.out))
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
