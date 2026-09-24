"""P1 生产管线：媒体 → 人声分离 → 强制对齐 → 卡拉OK字幕 → 成片

设计要点
--------
1. **对齐源与输出音轨解耦**
   P0 实测：伴奏会吃掉 13~26ms 的对齐精度。所以即便用户要保留原唱，
   对齐也跑在分离出的干人声上；而输出音轨按用户选择取原音 / 伴奏 / 独立音轨。
   两者互不影响。

2. **单元到 token 的回映射**
   对齐模型的输出粒度不等于我们的 token 粒度：
     - 中文  : Qwen 常给出单字，偶有「词」单元
     - 日文  : nagisa 会把「渡って」并成一个单元（3 字 -> 1 单元）
     - 英文  : 一般是词
   这里统一用「字符序列 difflib 对齐」把单元时间轴投回 token 时间轴，
   单元内多字符按等分插值。不做这套映射的话，日文的逐字高亮会整段错位。

3. **歌词输入三种形态**
   - 纯文本（无时间戳）           -> 全局对齐
   - LRC / SRT（行级时间戳）      -> 对齐 + 分段线性时间规整（warp），
                                     把行边界吸附到给定时刻，消掉累积漂移
   - 增强型 LRC（<mm:ss.xx> 逐词） -> 直接采信，跳过对齐（快且最准）

4. **本地优先**：Qwen3-ForcedAligner 从本地目录加载，Demucs 权重走本地缓存，
   默认置 HF_HUB_OFFLINE=1 避免联网校验拖慢冷启动。
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Iterable

# 允许 `python src/pipeline.py` 与 `python -m src.pipeline` 两种跑法
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import numpy as np  # noqa: E402

import model_paths  # noqa: E402,F401  （设定 TORCH_HOME/HF_HOME，须在模型加载前）

from p0_common import (  # noqa: E402
    LANGS, SR, TokenSpan, load_audio_16k, probe_duration, tokenize, write_wav,
)
from ass_builder import (  # noqa: E402
    AssOptions, KaraokeLine, KaraokeToken, build_ass, build_srt, check_windows,
    solve_windows,
)

ROOT = _HERE.parent
MODELS_QWEN = ROOT / "models" / "Qwen3-ForcedAligner-0.6B"
MODELS_ASR = ROOT / "models" / "Qwen3-ASR-1.7B"

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


def _noop_progress(frac: float, msg: str) -> None:  # pragma: no cover
    print(f"[{frac * 100:5.1f}%] {msg}", flush=True)


class Cancelled(RuntimeError):
    pass


# ==========================================================================
# 歌词解析
# ==========================================================================


@dataclass
class LyricLine:
    text: str = ""
    start: float | None = None      # 行级时间戳（秒）
    end: float | None = None
    end_explicit: bool = False      # end 是文件里写明的（SRT）还是推导的（LRC）
    word_times: list[tuple[str, float, float]] | None = None  # 增强 LRC


@dataclass
class LyricDoc:
    lines: list[LyricLine] = field(default_factory=list)
    timed: bool = False             # 是否带行级时间戳
    word_timed: bool = False        # 是否带逐词时间戳

    @property
    def text_lines(self) -> list[str]:
        return [l.text for l in self.lines]


_LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
# 逐词时间戳 <mm:ss.xx>；扩展语法 <mm:ss.xx~mm:ss.xx> 可显式给出终点。
# ASR 转写天然知道每词起止，不显式带上就会被「下一词起点」推断掉，
# 在 karaoke 里表现为每行末字闪一下（见 _resolve_word_times）。
_LRC_WORD = re.compile(
    r"<(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)(?:~(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?))?>"
)
_LRC_META = re.compile(r"^\[(ti|ar|al|by|offset|re|ve|length|kana):", re.I)
_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
# 除纯器乐/段落标记，如 [Chorus] （全角/半角括号）
_SECTION = re.compile(r"^\s*[\[\(【（].{1,24}?[\]\)】）]\s*$")


def _to_sec(m: str, s: str) -> float:
    return int(m) * 60.0 + float(s.replace(":", "."))


def parse_lyrics(raw: str) -> LyricDoc:
    """自动识别 纯文本 / LRC / 增强 LRC / SRT。"""
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return LyricDoc()

    # --- SRT -------------------------------------------------------------
    if _SRT_TIME.search(raw):
        doc = LyricDoc(timed=True)
        blocks = re.split(r"\n\s*\n", raw.strip())
        for b in blocks:
            lines = [x for x in b.split("\n") if x.strip()]
            if not lines:
                continue
            m = _SRT_TIME.search(b)
            if not m:
                continue
            g = m.groups()
            st = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3].ljust(3, "0")) / 1000
            en = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7].ljust(3, "0")) / 1000
            body = "\n".join(lines[lines.index([x for x in lines if m.group(0) in x][0]) + 1:])
            body = _strip_section(body.strip())
            if body:
                doc.lines.append(LyricLine(text=body, start=st, end=en,
                                           end_explicit=True))
        return _finalize(doc)

    # --- LRC / 增强 LRC --------------------------------------------------
    if _LRC_TIME.search(raw):
        doc = LyricDoc(timed=True)
        entries: list[tuple[float, str, list[tuple[str, float, float]] | None]] = []
        for ln in raw.split("\n"):
            if _LRC_META.match(ln):
                continue
            stamps = list(_LRC_TIME.finditer(ln))
            if not stamps:
                continue
            body = _LRC_TIME.sub("", ln).strip()
            words = None
            if _LRC_WORD.search(body):
                # 标准增强 LRC 的语义：<t> 为「它后面」的文本计时。
                # 旧实现取的是标记「前面」的文本，并把标记字符串本身当成词，
                # 结果逐词时间戳全部作废（difflib 匹配不上 → 退化成按行等比摊派）。
                matches = list(_LRC_WORD.finditer(body))
                words = []
                for k, wm in enumerate(matches):
                    st = _to_sec(wm.group(1), wm.group(2))
                    en = _to_sec(wm.group(3), wm.group(4)) if wm.group(3) else 0.0
                    nxt = matches[k + 1].start() if k + 1 < len(matches) else len(body)
                    seg = body[wm.end():nxt].strip()
                    if seg:
                        words.append((seg, st, en))
                lead = body[:matches[0].start()].strip()
                if lead:
                    # 首个标记之前的游离文本：继承它的起点，避免丢字
                    words.insert(0, (lead, words[0][1] if words else 0.0,
                                     words[0][1] if words else 0.0))
                body = _LRC_WORD.sub("", body).strip()
                words = _resolve_word_times(words)
            body = _strip_section(body)
            if not body:
                continue
            for sm in stamps:
                entries.append((_to_sec(sm.group(1), sm.group(2)), body, words))
        entries.sort(key=lambda e: e[0])
        for i, (t, body, words) in enumerate(entries):
            end = entries[i + 1][0] if i + 1 < len(entries) else None
            doc.lines.append(LyricLine(text=body, start=t, end=end, word_times=words))
        doc.word_timed = all(l.word_times for l in doc.lines) and bool(doc.lines)
        return _finalize(doc)

    # --- 纯文本 ----------------------------------------------------------
    doc = LyricDoc(timed=False)
    for ln in raw.split("\n"):
        ln = _strip_section(ln.strip())
        if ln:
            doc.lines.append(LyricLine(text=ln))
    return _finalize(doc)


def _resolve_word_times(items: list[tuple[str, float, float]]):
    """增强 LRC 逐词项收尾：显式给了终点就用它，没给才用下一个词的起点推断。

    标准增强 LRC 的 ``<t>`` 只带起点，终点必须靠「下一个标记的起点」推出来；
    但这会把每行**末词**推成极短片段（旧实现固定 60ms），在 karaoke 里表现为
    每行末字闪一下。ASR 生成的歌词天然知道每个词的终点，用
    ``<start~end>`` 扩展语法带上即可无损通过这里。
    """
    if not items:
        return None
    starts = [t for _, t, _ in items]
    out: list[tuple[str, float, float]] = []
    for i, (txt, st, en) in enumerate(items):
        if en <= st:
            nxt = next((s for s in starts[i + 1:] if s > st), None)
            en = nxt if nxt is not None else st + 0.12
        if en <= st:
            en = st + 0.12
        out.append((txt, st, en))
    # 合并相邻但未被标记分隔的片段
    return [(t.strip(), a, b) for t, a, b in out if t.strip()]


def _strip_section(s: str) -> str:
    return "" if _SECTION.match(s) else s


def _finalize(doc: LyricDoc) -> LyricDoc:
    doc.lines = [l for l in doc.lines if l.text.strip()]
    if doc.timed and doc.lines and doc.lines[-1].end is None:
        doc.lines[-1].end = None      # 末行终点未知，交给 warp/对齐决定
    return doc


# ==========================================================================
# 语言判定
# ==========================================================================

_KANA = re.compile(r"[\u3040-\u309F\u30A0-\u30FF]")
_HANGUL = re.compile(r"[\uAC00-\uD7AF]")
_CJK = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")


def detect_language(text: str) -> tuple[str, dict]:
    """按语言特征字符占比判定。日文优先（含假名即可定日文）。"""
    n_kana = len(_KANA.findall(text))
    n_hang = len(_HANGUL.findall(text))
    n_cjk = len(_CJK.findall(text))
    n_lat = len(re.findall(r"[A-Za-z]", text))
    total = max(1, n_kana + n_hang + n_cjk + n_lat)
    score = {"ja": n_kana, "ko": n_hang, "zh": n_cjk, "en": n_lat}
    detail = {k: round(v / total, 3) for k, v in score.items()}
    # 假名存在 -> 日文（日文歌词即使汉字很多也必有假名）
    if n_kana > 0:
        return "ja", detail
    if n_cjk > 0 and n_cjk >= n_lat:
        return "zh", detail
    if n_lat > 0:
        return "en", detail
    return "zh", detail


# ==========================================================================
# token <-> 显示拼接
# ==========================================================================


def scan_segments(line: str, tokens: list[str]):
    """把一行原文切成 [头部填充] + [各 token 的显示文本（含其后标点）]。

    用最大匹配从左到右扫描，因此重复字符（如「啦啦啦」「ABC ABC」）也能正确
    归属，不会像基于字典的做法那样互相串位。

    返回 (head, disps)；无法消费全部 token 时返回 None。
    """
    ti, used = 0, 0
    seq: list[tuple[int | None, str]] = []
    for ch in line:
        if ti < len(tokens) and used < len(tokens[ti]) and ch == tokens[ti][used]:
            seq.append((ti, ch))
            used += 1
            if used == len(tokens[ti]):
                ti, used = ti + 1, 0
        else:
            seq.append((None, ch))
    if ti != len(tokens):
        return None

    head = ""
    disps = [""] * len(tokens)
    last: int | None = None
    for idx, ch in seq:
        if idx is None:
            if last is None:
                head += ch
            else:
                disps[last] += ch
        else:
            disps[idx] += ch
            last = idx
    return head, disps


def build_display(line: str, tokens: list[str]) -> tuple[str, list[str]]:
    """优先用原文；NFKC 后再试一次；都失败则退回「token 直接拼接」。"""
    got = scan_segments(line, tokens)
    if got:
        return got
    norm = unicodedata.normalize("NFKC", line)
    if norm != line:
        got = scan_segments(norm, tokens)
        if got:
            return got
    return "", list(tokens)


# ==========================================================================
# 单元 -> token 时间映射
# ==========================================================================


def _chars(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    return "".join(c for c in s if not c.isspace())


@dataclass
class _Unit:
    text: str
    start: float
    end: float
    off: int = 0      # 在拼接串中的起始下标


def map_units_to_tokens(units: list[TokenSpan], tokens: list[str],
                        audio_dur: float | None = None) -> tuple[list[TokenSpan | None], dict]:
    """把对齐单元时间轴投回 token 时间轴。

    两边都拼成字符序列后用 difflib 找匹配块；单元内多字符按等分插值。
    返回 (与 tokens 等长的 span 列表[可能含 None], 诊断信息)。
    """
    diag: dict = {"mode": "difflib", "unit_ratio": 0.0}
    if not units or not tokens:
        return [None] * len(tokens), {"mode": "empty"}

    T = "".join(_chars(t) for t in tokens)
    R_parts = [_chars(u.text) for u in units]
    R = "".join(R_parts)
    if not T or not R:
        return [None] * len(tokens), {"mode": "empty-text"}

    # token 字符下标 -> token 序号
    tok_of: list[int] = []
    for i, t in enumerate(tokens):
        tok_of.extend([i] * len(_chars(t)))
    # 单元字符下标 -> (单元序号, 单元内第几个字符, 单元字符数)
    unit_of: list[tuple[int, int, int]] = []
    for ui, u in enumerate(units):
        k = len(_chars(u.text))
        unit_of.extend([(ui, r, k) for r in range(k)])

    sm = difflib.SequenceMatcher(None, R, T, autojunk=False)
    t2r: list[int | None] = [None] * len(T)
    matched = 0
    for blk in sm.get_matching_blocks():
        for off in range(blk.size):
            t2r[blk.b + off] = blk.a + off
            matched += 1
    diag["unit_ratio"] = round(matched / len(T), 4)

    if diag["unit_ratio"] < 0.5:
        # 匹配太差，直接按字符数比例摊到整段音频
        diag["mode"] = "proportional-fallback"
        if audio_dur is None:
            return [None] * len(tokens), diag
        n = len(T)
        out: list[TokenSpan | None] = []
        for i, t in enumerate(tokens):
            a = sum(len(_chars(x)) for x in tokens[:i]) / n * audio_dur
            b = (sum(len(_chars(x)) for x in tokens[:i + 1])) / n * audio_dur
            out.append(TokenSpan(text=t, start=a, end=max(b, a + 0.02)))
        return out, diag

    def t_start(ri: int) -> float:
        ui, r, k = unit_of[ri]
        u = units[ui]
        return u.start + (u.end - u.start) * (r / k)

    def t_end(ri: int) -> float:
        ui, r, k = unit_of[ri]
        u = units[ui]
        return u.start + (u.end - u.start) * ((r + 1) / k)

    spans: list[TokenSpan | None] = []
    cur = 0
    for i, tok in enumerate(tokens):
        k = len(_chars(tok))
        idxs = [j for j in range(cur, cur + k) if j < len(T)]
        cur += k
        hit = [t2r[j] for j in idxs if t2r[j] is not None]
        if not hit:
            spans.append(None)
            continue
        spans.append(TokenSpan(text=tok, start=t_start(hit[0]), end=t_end(hit[-1]),
                               unit=unit_of[hit[0]][0]))
    return spans, diag


def _repair_collapsed(out: list[TokenSpan], hi: float) -> int:
    """修复对齐器输出的「零时长 / 近零时长」token 段。

    实测证据：Qwen 在日文句尾会吐出 0 时长单元
    （'てる' 4.800 - 4.800），使末尾若干字完全没有时间。
    这类坍缩是模型噪声，不是真实演唱信息，因此用「本行 token 时长的中位数」
    作为步长做**有界补时**：只向后延展，不移动已定位的起点，因此：

      * 不会破坏单调性（补时上限被下一个 token 的起点夹住）；
      * 不会贪心地吞掉整段器乐尾巴（每个 token 最多补一个中位时长）。

    返回被修复的 token 数。
    """
    n = len(out)
    if n == 0:
        return 0
    durs = sorted(s.end - s.start for s in out if s.end - s.start > 0.001)
    med = durs[len(durs) // 2] if durs else 0.2
    thresh = max(0.02, 0.25 * med)

    fixed = 0
    i = 0
    while i < n:
        if out[i].end - out[i].start >= thresh:
            i += 1
            continue
        j = i
        while j < n and out[j].end - out[j].start < thresh:
            j += 1
        run = j - i
        start = out[i].start
        if j < n:
            room = max(0.0, out[j].start - start)
        else:
            room = max(0.0, min(hi, start + med * run) - start)
        step = room / run if room > 0 else med
        step = max(0.02, min(step, med))
        for k in range(run):
            out[i + k].start = start + step * k
            out[i + k].end = out[i + k].start + step
        fixed += run
        i = j
    return fixed


def _fill_and_monotonic(spans: list[TokenSpan | None], tokens: list[str],
                        bound: tuple[float, float]) -> list[TokenSpan]:
    """补全缺失 token、修复坍缩段、压掉重叠，保证输出严格单调不重叠。"""
    lo, hi = bound
    n = len(tokens)
    # 1) 缺值：用左右邻居的间隙均分
    i = 0
    while i < n:
        if spans[i] is not None:
            i += 1
            continue
        j = i
        while j < n and spans[j] is None:
            j += 1
        left = spans[i - 1].end if i > 0 and spans[i - 1] else lo
        right = spans[j].start if j < n and spans[j] else hi
        if right <= left:
            right = left + 0.05 * (j - i)
        step = (right - left) / max(1, j - i)
        for k in range(i, j):
            spans[k] = TokenSpan(text=tokens[k], start=left + step * (k - i),
                                 end=left + step * (k - i + 1))
        i = j

    out = [s for s in spans if s is not None]

    # 2) 严格单调化：起点不得早于前一个终点。
    #    注意：**不要**为「最短可见时长」再把起点往前推 —— 那样会把 token
    #    推回前一个 token 的区间内，重新制造重叠（这是 v0.3.0 的真实 bug）。
    prev_end = lo
    for s in out:
        if s.start < prev_end:
            s.start = prev_end
        if s.end < s.start:
            s.end = s.start
        prev_end = s.end

    # 3) 修复塌缩段，然后重新单调化（补时只向后延展，正常应为空操作）
    _repair_collapsed(out, hi)
    prev_end = lo
    for s in out:
        if s.start < prev_end:
            s.start = prev_end
        if s.end < s.start:
            s.end = s.start
        prev_end = s.end
    return out


# ==========================================================================
# 时间规整（timed lyrics）
# ==========================================================================


def make_warp(anchors: Iterable[tuple[float, float]]):
    """构造单调分段线性映射 src->dst。anchors 需按 src 升序且 dst 也升序。"""
    pts = sorted((a, b) for a, b in anchors)
    # 去掉破坏单调性的锚点
    clean: list[tuple[float, float]] = []
    for a, b in pts:
        if clean and (a <= clean[-1][0] or b <= clean[-1][1]):
            continue
        clean.append((a, b))
    if len(clean) < 2:
        return (lambda t: t), clean

    xs = [p[0] for p in clean]
    ys = [p[1] for p in clean]

    def f(t: float) -> float:
        if t <= xs[0]:
            k = (ys[1] - ys[0]) / (xs[1] - xs[0])
            return max(0.0, ys[0] + (t - xs[0]) * k)
        if t >= xs[-1]:
            k = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
            return ys[-1] + (t - xs[-1]) * k
        i = bisect.bisect_right(xs, t) - 1
        k = (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i])
        return ys[i] + (t - xs[i]) * k

    return f, clean


# ==========================================================================
# 媒体探测 / 音频 / 分离 / 渲染
# ==========================================================================


def probe_media(path: str | Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    r = subprocess.run(cmd, capture_output=True, check=True)
    info = json.loads(r.stdout.decode("utf-8", "replace"))
    out = {"duration": 0.0, "has_video": False, "has_audio": False,
           "width": 0, "height": 0, "fps": 0.0, "vcodec": "", "acodec": ""}
    try:
        out["duration"] = float(info.get("format", {}).get("duration") or 0.0)
    except Exception:
        pass
    for st in info.get("streams", []):
        if st.get("codec_type") == "video" and not out["has_video"]:
            out["has_video"] = True
            out["width"] = int(st.get("width") or 0)
            out["height"] = int(st.get("height") or 0)
            out["vcodec"] = st.get("codec_name") or ""
            fr = st.get("avg_frame_rate") or "0/0"
            try:
                a, b = fr.split("/")
                out["fps"] = float(a) / float(b) if float(b) else 0.0
            except Exception:
                out["fps"] = 0.0
        elif st.get("codec_type") == "audio" and not out["has_audio"]:
            out["has_audio"] = True
            out["acodec"] = st.get("codec_name") or ""
    return out


def extract_wav(src: str | Path, dst: str | Path, sr: int = SR,
                mono: bool = True) -> str:
    """任何音/视频 -> PCM16 WAV。"""
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(src)]
    if mono:
        cmd += ["-ac", "1"]
    cmd += ["-ar", str(sr), "-vn", "-c:a", "pcm_s16le", str(dst)]
    subprocess.run(cmd, capture_output=True, check=True)
    return str(dst)


def separate_stems(src_audio: str | Path, out_dir: Path, model: str = "htdemucs_ft",
                   device: str = "cuda") -> tuple[str, str]:
    """跑 Demucs 两轨分离，返回 (vocals, accompaniment) 的 WAV 路径。

    用官方 CLI（python -m demucs.separate）而不是手搓 apply_model：
    它自带分块/重叠/归一化处理，且按模型原生 44.1kHz 立体声出干声，
    成片音质不受 16kHz 单声道对齐链路的影响。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / "_stems"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    cmd = [sys.executable, str(Path(__file__).with_name("demucs_runner.py")),
           "-n", model, "--two-stems=vocals",
           "-d", device, "-o", str(tmp), str(src_audio)]
    env = dict(os.environ)
    env["PYTHONPATH"] = ""
    r = subprocess.run(cmd, capture_output=True, env=env, cwd=str(out_dir))
    stem_dir = tmp / model / Path(src_audio).stem
    voc, acc = stem_dir / "vocals.wav", stem_dir / "no_vocals.wav"
    if r.returncode != 0 or not voc.exists() or not acc.exists():
        tail = (r.stderr or b"").decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"Demucs 分离失败（rc={r.returncode}）:\n{tail}")
    v = out_dir / "vocals.wav"
    a = out_dir / "accompaniment.wav"
    shutil.move(str(voc), str(v))
    shutil.move(str(acc), str(a))
    shutil.rmtree(tmp, ignore_errors=True)
    return str(v), str(a)


def _enc_args(encoder: str, quality: int) -> list[str]:
    common = ["-pix_fmt", "yuv420p"]
    if encoder == "x264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)] + common
    return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
            "-cq", str(quality), "-b:v", "0", "-g", "120"] + common


def render_video(media: str | Path, ass_name: str, ass_dir: Path, out_path: Path,
                 audio_track: str | None, media_info: dict,
                 encoder: str = "auto", quality: int = 21,
                 audio_copy_ok: bool = False) -> str:
    """把 ASS 烧进画面。cwd 设在字幕所在目录，避免 Windows 盘符冒号在
    filtergraph 里的转义地狱（ass=xxx：':' 和 '\\' 都要转义）。

    注意：正因为 cwd 被切到 ``ass_dir``，除 ``ass_name`` 之外的所有路径都
    必须先绝对化——否则相对路径的 ``out_path``（CLI 默认 ``--out out`` 就是
    相对的）会被 ffmpeg 解析成 ``ass_dir/out/...``，报 "No such file or directory"。
    """
    media = str(Path(media).resolve())
    if audio_track:
        audio_track = str(Path(audio_track).resolve())
    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    enc = encoder
    if enc == "auto":
        enc = "nvenc"
    vf = f"ass={ass_name}" if " " not in ass_name else f"ass='{ass_name}'"

    def base(e: str) -> list[str]:
        c = ["ffmpeg", "-y", "-nostdin", "-v", "error"]
        if media_info.get("has_video"):
            c += ["-i", str(media)]
        else:
            c += ["-f", "lavfi", "-i",
                  f"color=c=0x0E1116:s=1920x1080:r=30"]
            c += ["-i", str(media)]
        if audio_track:
            c += ["-i", str(audio_track)]
        c += ["-vf", vf]
        if media_info.get("has_video"):
            c += ["-map", "0:v:0"]
            c += ["-map", ("1:a:0" if audio_track else "0:a:0")]
        else:
            c += ["-map", "0:v:0", "-map", ("2:a:0" if audio_track else "1:a:0")]
        c += ["-shortest"]
        c += _enc_args(e, quality)
        if audio_copy_ok and not audio_track:
            c += ["-c:a", "copy"]
        else:
            c += ["-c:a", "aac", "-b:a", "256k"]
        c += ["-movflags", "+faststart", str(out_path)]
        return c

    tries = [enc] + ([("x264" if enc != "x264" else "nvenc")] if encoder == "auto" else [])
    last = ""
    for e in tries:
        cmd = base(e)
        r = subprocess.run(cmd, capture_output=True, cwd=str(ass_dir))
        if r.returncode == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return e
        last = (r.stderr or b"").decode("utf-8", "replace")[-1200:]
        out_path.unlink(missing_ok=True)
    raise RuntimeError(f"渲染失败:\n{last}")


# ==========================================================================
# 模型缓存
# ==========================================================================


class ModelCache:
    """进程内复用对齐/分离/转写模型，让 WebUI 的第二次任务不必重新加载。"""

    def __init__(self, device: str = "cuda"):
        self.device = device
        self._aligners: dict[str, object] = {}
        self._asr: dict[str, object] = {}

    def aligner(self, backend: str, lang: str):
        key = f"{backend}:{lang}"
        if key not in self._aligners:
            from align_backends import QwenAligner, Wav2Vec2Aligner
            if backend == "qwen":
                self._aligners[key] = QwenAligner(
                    model_dir=str(MODELS_QWEN) if MODELS_QWEN.exists() else None,
                    device=self.device)
            else:
                name = LANGS[lang].wav2vec2_model
                if not name:
                    raise RuntimeError(f"wav2vec2 后端不支持语言 {lang}")
                self._aligners[key] = Wav2Vec2Aligner(
                    name, device=self.device, cache_dir=str(ROOT / "models" / "hf"))
        return self._aligners[key]

    def asr(self, language: str | None = None):
        """Qwen3-ASR-1.7B（无歌词文件时转写生成歌词草稿）。权重缺失时给出明确指引。

        ``language`` 是 UI 的 lang 代码（zh/en/ja），**按语言分别缓存**：
        实测不指定语言会把日文歌误判成英文并大量幻觉，所以这个参数必须传。
        """
        key = f"asr:{language or 'auto'}"
        if key not in self._asr:
            from asr_lyrics import LocalAsr, qwen_language

            self._asr[key] = LocalAsr(
                asr_dir=MODELS_ASR if MODELS_ASR.exists() else None,
                aligner_dir=MODELS_QWEN if MODELS_QWEN.exists() else None,
                device=self.device,
                language=qwen_language(language),
            )
        return self._asr[key]


# ==========================================================================
# 配置与结果
# ==========================================================================


@dataclass
class PipelineConfig:
    media: str = ""
    lyrics_text: str = ""
    lyrics_path: str | None = None
    audio_track: str | None = None       # 独立音轨，用于替换原音
    vocal_mode: str = "remove"           # keep | remove
    lang: str = "auto"                   # auto | zh | en | ja
    separate: bool = True
    demucs_model: str = "htdemucs_ft"
    backend: str = "qwen"                # qwen | wav2vec2
    device: str = "cuda"
    out_dir: str = "out"
    job_name: str = ""
    timed_mode: str = "warp"             # warp | ignore
    encoder: str = "auto"                # auto | nvenc | x264
    quality: int = 21
    ass: AssOptions = field(default_factory=AssOptions)


@dataclass
class PipelineResult:
    ok: bool = False
    job_dir: str = ""
    video: str | None = None
    ass: str | None = None
    srt: str | None = None
    align_json: str | None = None
    vocals: str | None = None
    accompaniment: str | None = None
    stats: dict = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ==========================================================================
# 主流程
# ==========================================================================


def _load_lyrics(cfg: PipelineConfig) -> tuple[str, str]:
    if cfg.lyrics_text and cfg.lyrics_text.strip():
        return cfg.lyrics_text, "<inline>"
    if cfg.lyrics_path:
        p = Path(cfg.lyrics_path)
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "cp932", "latin-1"):
            try:
                return raw.decode(enc), str(p)
            except UnicodeDecodeError:
                continue
        raise RuntimeError(f"无法解码歌词文件: {p}")
    raise RuntimeError("缺少歌词：请提供歌词文本或 .txt/.lrc/.srt 文件")


def run(cfg: PipelineConfig, progress: ProgressFn | None = None,
        cancel: CancelFn | None = None, cache: ModelCache | None = None) -> PipelineResult:
    prog = progress or _noop_progress
    res = PipelineResult()
    t00 = time.perf_counter()

    def step(frac: float, msg: str):
        if cancel and cancel():
            raise Cancelled("用户已取消")
        prog(frac, msg)
        res.log.append(f"[{time.perf_counter() - t00:7.2f}s] {msg}")

    try:
        # ---------------------------------------------------------- 0 准备
        if not cfg.media or not Path(cfg.media).exists():
            raise RuntimeError(f"媒体文件不存在: {cfg.media}")
        job = cfg.job_name or f"{Path(cfg.media).stem}_{time.strftime('%m%d_%H%M%S')}"
        # 绝对化：渲染阶段 ffmpeg 的 cwd 会被切到字幕目录，相对路径会解析错地方
        job_dir = Path(cfg.out_dir).resolve() / _safe(job)
        job_dir.mkdir(parents=True, exist_ok=True)
        res.job_dir = str(job_dir)

        os.environ.setdefault("HF_HUB_OFFLINE", "1")

        step(0.02, "探测媒体信息")
        info = probe_media(cfg.media)
        if not info["has_audio"]:
            raise RuntimeError("输入文件没有音轨，无法对齐")
        src_for_audio = cfg.audio_track or cfg.media
        if not info["has_video"]:
            step(0.04, "输入为纯音频，将合成纯色背景视频")

        step(0.06, "解析歌词")
        raw_lyrics, src_name = _load_lyrics(cfg)
        doc = parse_lyrics(raw_lyrics)
        if not doc.lines:
            raise RuntimeError("歌词解析后为空，请检查内容格式")
        if not (job_dir/'input_lyrics.json').exists():
            (job_dir/'input_lyrics.json').write_text(json.dumps(
                [line.text for line in doc.lines], ensure_ascii=False), encoding='utf-8')
        joined = "\n".join(l.text for l in doc.lines)
        if cfg.lang == "auto":
            lang, detail = detect_language(joined)
        else:
            lang, detail = cfg.lang, {"forced": 1.0}
        spec = LANGS.get(lang)
        if spec is None:
            raise RuntimeError(f"不支持的语言: {lang}")
        step(0.08, f"识别语言 = {lang}（kana/cjk/latin 占比 {detail}）"
                   f"，歌词 {len(doc.lines)} 行，来源 {src_name}")

        # ---------------------------------------------------------- 1 音轨
        # 预对齐阶段（whisper / SOFA prepass）已抽过同一份 44.1k 立体声 → 直接复用，
        # 否则用旧路径 source_audio.wav 自抽一份（CLI 单独跑 pipeline 时就是这条路）。
        # 注意：用户给了独立音轨时不能复用，写入目标必须是 source_audio.wav，
        # 否则会把预对齐用的原始混音覆盖掉。
        if cfg.audio_track:
            full_wav = job_dir / "source_audio.wav"
            step(0.10, "抽取用户提供的独立音轨")
            extract_wav(cfg.audio_track, full_wav, sr=44100, mono=False)
        else:
            pre44 = next((p for p in (job_dir / "in" / "audio_44k.wav",
                                      job_dir / "in" / "whisper_44k.wav",
                                      job_dir / "in" / "sofa_44k.wav") if p.exists()), None)
            full_wav = pre44 or (job_dir / "source_audio.wav")
            if pre44:
                step(0.10, f"复用预对齐阶段抽取的音轨（{pre44.name}）")
            else:
                step(0.10, "抽取原始音轨（44.1kHz 立体声）")
                extract_wav(cfg.media, full_wav, sr=44100, mono=False)

        sep_src = str(full_wav)
        do_sep = cfg.separate and not cfg.audio_track
        # 预对齐阶段已经分离过一次（in/vg/）→ 复用它，别再跑一遍 demucs
        pre_voc, pre_acc = job_dir / "in" / "vg" / "vocals.wav", \
            job_dir / "in" / "vg" / "accompaniment.wav"
        reuse = do_sep and pre_voc.exists() and pre_acc.exists()
        if cfg.audio_track:
            step(0.12, "已提供独立音轨，跳过人声分离")
        elif not cfg.separate:
            step(0.12, "按要求跳过人声分离，对齐直接跑在原始混音上")
        elif reuse:
            step(0.12, "复用预对齐阶段分离好的人声/伴奏（跳过重复分离）")

        if reuse:
            voc, acc = str(pre_voc), str(pre_acc)
            res.vocals, res.accompaniment = voc, acc
            align_src = voc
        elif do_sep:
            step(0.14, f"人声分离中（{cfg.demucs_model}，首次运行较慢）")
            voc, acc = separate_stems(full_wav, job_dir, cfg.demucs_model, cfg.device)
            res.vocals, res.accompaniment = voc, acc
            align_src = voc
        else:
            align_src = sep_src

        # 对齐用的 16k 单声道
        align_16k = job_dir / "align_input.wav"
        step(0.42, "准备 16kHz 单声道对齐输入")
        extract_wav(align_src, align_16k, sr=SR, mono=True)
        audio = load_audio_16k(align_16k)
        audio_dur = len(audio) / SR
        step(0.45, f"对齐音频 {audio_dur:.2f}s（来自 {Path(align_src).name}）")

        # ---------------------------------------------------------- 2 分词
        line_tokens: list[list[str]] = [tokenize(l.text, spec) for l in doc.lines]
        line_tokens = [t if t else [] for t in line_tokens]
        kept = [i for i, t in enumerate(line_tokens) if t]
        if not kept:
            raise RuntimeError("歌词分词后为空（可能全是标点/符号）")
        flat: list[str] = []
        for i in kept:
            flat.extend(line_tokens[i])
        step(0.47, f"共 {len(flat)} 个 token（{spec.granularity} 粒度），"
                   f"有效行 {len(kept)}/{len(doc.lines)}")

        # ---------------------------------------------------------- 3 对齐
        spans: list[TokenSpan | None] = [None] * len(flat)
        diag: dict = {}
        used_word_times = False
        if doc.word_timed and cfg.timed_mode != "ignore":
            step(0.50, "检测到增强型 LRC 逐词时间戳，直接采信（跳过声学对齐）")
            used_word_times = True
            cur = 0
            for i in kept:
                lt = line_tokens[i]
                wt = doc.lines[i].word_times or []
                units = [TokenSpan(text=t, start=a, end=b) for t, a, b in wt]
                got, d = map_units_to_tokens(units, lt, audio_dur)
                for k, s in enumerate(got):
                    spans[cur + k] = s
                cur += len(lt)
            diag = {"mode": "enhanced-lrc"}
        else:
            step(0.50, f"加载对齐模型（{cfg.backend}）")
            mc = cache or ModelCache(cfg.device)
            aligner = mc.aligner(cfg.backend, lang)
            step(0.60, f"强制对齐 {len(flat)} tokens / {audio_dur:.1f}s 音频")
            raw_units = aligner.align(audio, flat, lang)
            step(0.78, f"对齐产出 {len(raw_units)} 个单元，回映射到 token 粒度")
            spans, diag = map_units_to_tokens(raw_units, flat, audio_dur)

        spans = _fill_and_monotonic(spans, flat, (0.0, max(audio_dur, 0.1)))
        step(0.82, f"token 时间轴整理完成（映射方式 {diag.get('mode')}，"
                   f"字符匹配率 {diag.get('unit_ratio', '-')}）")

        # ---------------------------------------------------------- 4 分行
        # 保留 (原歌词行号 -> KaraokeLine) 的显式映射，warp 阶段要用它取锚点；
        # 不用「按文本反查」，否则重复行会张冠李戴。
        lines: list[KaraokeLine] = []
        origin: list[int] = []
        cur = 0
        for i in kept:
            lt = line_tokens[i]
            seg = spans[cur:cur + len(lt)]
            cur += len(lt)
            head, disps = build_display(doc.lines[i].text, lt)
            ktoks = [KaraokeToken(text=lt[j], disp=disps[j],
                                  start=seg[j].start, end=seg[j].end,
                                  unit=seg[j].unit)
                     for j in range(len(lt))]
            lines.append(KaraokeLine(
                raw=doc.lines[i].text, tokens=ktoks, head=head,
                start=ktoks[0].start, end=ktoks[-1].end))
            origin.append(i)

        # ---------------------------------------------------------- 5 warp
        warp_info = {"applied": False}
        if doc.timed and cfg.timed_mode == "warp" and not used_word_times:
            anchors: list[tuple[float, float]] = []
            for m, di in enumerate(origin):
                ln = doc.lines[di]
                if ln.start is None:
                    continue
                anchors.append((lines[m].start, ln.start))
                # 只用「文件里写明的」行尾当锚点；LRC 的 end 是从下一行起点
                # 推出来的，拿它当锚会把末字硬拉到下一行开唱，反而失真。
                if ln.end_explicit and ln.end:
                    anchors.append((lines[m].end, ln.end))
            f, clean = make_warp(anchors)
            if len(clean) >= 2:
                for ln in lines:
                    for t in ln.tokens:
                        t.start, t.end = f(t.start), f(t.end)
                    ln.start, ln.end = ln.tokens[0].start, ln.tokens[-1].end
                warp_info = {"applied": True, "anchors": len(clean),
                             "explicit_ends": any(doc.lines[i].end_explicit for i in origin)}
                step(0.85, f"时间规整：以 {len(clean)} 个行级锚点吸附到歌词时间戳")

        # ---------------------------------------------------------- 6 字幕
        opt = cfg.ass
        solve_windows(lines, opt)
        health = check_windows(lines, opt.min_gap_ms)
        ass_text = build_ass(lines, opt,
                             width=info["width"] or 1920,
                             height=info["height"] or 1080)
        ass_path = job_dir / "karaoke.ass"
        ass_path.write_text(ass_text, encoding="utf-8-sig")
        srt_path = job_dir / "lyrics.srt"
        srt_path.write_text(build_srt(lines), encoding="utf-8-sig")
        res.ass, res.srt = str(ass_path), str(srt_path)
        step(0.88, f"字幕生成完成（{len(lines)} 行）；结构自检 "
                   f"{'通过' if health['ok'] else '异常 ' + json.dumps(health, ensure_ascii=False)}")

        aj = job_dir / "align.json"
        aj.write_text(json.dumps({
            "job": job, "media": cfg.media, "lang": lang,
            "align_source": Path(align_src).name,
            "audio_duration": round(audio_dur, 4),
            "mapping": diag, "warp": warp_info, "health": health,
            "options": {
                "font": opt.font, "font_size": opt.font_size,
                "sung_color": list(opt.sung_color),
                "unsung_color": list(opt.unsung_color),
                "next_line": opt.next_line, "line_count": opt.line_count,
                "position_x": opt.position_x, "position_y": opt.position_y,
                "lead_ms": opt.lead_ms,
                "tail_ms": opt.tail_ms, "min_gap_ms": opt.min_gap_ms,
                "group_same_unit": opt.group_same_unit,
            },
            "lines": [{
                "raw": ln.raw, "start": round(ln.start, 4), "end": round(ln.end, 4),
                "ev_start": round(ln.ev_start, 4), "ev_end": round(ln.ev_end, 4),
                "head": ln.head,
                "tokens": [{"text": t.text, "disp": t.disp, "unit": t.unit,
                            "start": round(t.start, 4), "end": round(t.end, 4)}
                           for t in ln.tokens],
            } for ln in lines],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        res.align_json = str(aj)
        from alignment_review import attach_review
        review = attach_review(job_dir, json.loads(aj.read_text(encoding='utf-8')),
                               job_dir/'acceptance.json')['acceptance']
        step(.89, f"自动验收：{review['label']}（输入 {review['input_lines']} 行，输出 {review['output_lines']} 行）")

        # ---------------------------------------------------------- 7 输出音轨
        out_audio: str | None = None
        if cfg.audio_track:
            out_audio = str(full_wav)
            step(0.90, "输出音轨 = 用户提供的独立音轨")
        elif cfg.vocal_mode == "remove":
            if do_sep:
                out_audio = res.accompaniment
                step(0.90, "输出音轨 = 分离所得伴奏")
            else:
                step(0.90, "!! 要求去人声但未做分离，输出将保留原唱")
        else:
            step(0.90, "输出音轨 = 原始音轨（保留人声）")

        # ---------------------------------------------------------- 8 渲染
        # 先把重渲染所需的全部上下文固化到 job.json，之后微调只需读盘，
        # 不必再碰模型，也不必让调用方记住任何参数。
        (job_dir / "job.json").write_text(json.dumps({
            "job": job, "media": str(Path(cfg.media).resolve()),
            "media_info": info,
            "out_audio": str(Path(out_audio).resolve()) if out_audio else None,
            "align_source": str(Path(align_src).resolve()),
            "lang": lang,
            "encoder_choice": cfg.encoder, "quality": cfg.quality,
            "options": {
                "font": opt.font, "font_size": opt.font_size,
                "sung_color": list(opt.sung_color),
                "unsung_color": list(opt.unsung_color),
                "outline": opt.outline, "margin_v": opt.margin_v,
                "next_line": opt.next_line, "line_count": opt.line_count,
                "position_x": opt.position_x, "position_y": opt.position_y,
                "lead_ms": opt.lead_ms,
                "tail_ms": opt.tail_ms, "min_gap_ms": opt.min_gap_ms,
                "group_same_unit": opt.group_same_unit,
            },
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        acopy = (out_audio is None and (info["acodec"] in ("aac", "mp3", "ac3", "eac3")))
        out_video = job_dir / f"{_safe(job)}_karaoke.mp4"
        step(0.93, "烧录字幕并合成成片")
        used_enc = render_video(cfg.media, ass_path.name, job_dir, out_video,
                               out_audio, info, cfg.encoder, cfg.quality, acopy)
        res.video = str(out_video)
        res.ok = True
        res.stats = {
            "elapsed_sec": round(time.perf_counter() - t00, 2),
            "audio_sec": round(audio_dur, 2),
            "lines": len(lines),
            "tokens": len(flat),
            "lang": lang,
            "align_source": Path(align_src).name,
            "mapping": diag,
            "warp": warp_info,
            "health": health,
            "separated": do_sep,
            "output_audio": Path(out_audio).name if out_audio else "original",
            "encoder": used_enc,
            "media": info,
            "rtf": round((time.perf_counter() - t00) / max(audio_dur, 0.01), 3),
        }
        step(1.00, f"完成：{out_video.name}（用时 {res.stats['elapsed_sec']}s，"
                   f"RTF {res.stats['rtf']}，编码器 {used_enc}）")
    except Cancelled as e:
        res.error = str(e)
        res.log.append("已取消")
    except Exception as e:  # noqa: BLE001
        import traceback
        res.error = f"{type(e).__name__}: {e}"
        res.log.append("失败：" + res.error)
        res.log.append(traceback.format_exc()[-2500:])
    return res


def _safe(name: str) -> str:
    s = re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", name).strip("._")
    return s[:80] or "job"


# ==========================================================================
# 微调重渲染（不重新对齐）
# ==========================================================================


@dataclass
class RestyleRequest:
    line_offsets_ms: dict[int, float] = field(default_factory=dict)   # 行号 -> 毫秒偏移
    token_offsets_ms: dict[str, float] = field(default_factory=dict)  # "行号:token号" -> 毫秒
    overrides: dict = field(default_factory=dict)
    base_version: int = 0   # 0=原始；正整数=在已渲染版本上继续修改
    anchor_row: int | None = None
    line_bounds: dict = field(default_factory=dict)  # row -> absolute start/end seconds
    line_insertion: dict | None = None
    retry_suspects: bool = False


def _ass_opt_from(opts: dict) -> AssOptions:
    def col(v, dflt):
        if v is None:
            return dflt
        if isinstance(v, (list, tuple)):
            return tuple(int(x) for x in v[:3])
        h = str(v).lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    base = AssOptions()
    show_following = bool(opts.get("next_line", int(opts.get('line_count', base.line_count)) > 1))
    line_count = max(1, min(3, int(opts.get('line_count', 2 if show_following else 1))))
    if not show_following:
        line_count = 1
    return AssOptions(
        font=opts.get("font") or base.font,
        font_size=int(opts.get("font_size") or base.font_size),
        sung_color=col(opts.get("sung_color"), base.sung_color),
        unsung_color=col(opts.get("unsung_color"), base.unsung_color),
        outline=float(opts.get("outline", base.outline)),
        margin_v=int(opts.get("margin_v") or base.margin_v),
        next_line=show_following,
        line_count=line_count,
        position_x=max(0, min(100, int(opts.get('position_x', base.position_x)))),
        position_y=max(0, min(100, int(opts.get('position_y', base.position_y)))),
        lead_ms=int(opts.get("lead_ms") or base.lead_ms),
        tail_ms=int(opts.get("tail_ms") or base.tail_ms),
        min_gap_ms=int(opts.get("min_gap_ms") or base.min_gap_ms),
        group_same_unit=bool(opts.get("group_same_unit", base.group_same_unit)),
    )


def load_job(job_dir: str | Path, version: int = 0) -> tuple[dict, list[KaraokeLine]]:
    """从 job.json + align.json 还原出可编辑的字幕对象。"""
    jd = Path(job_dir)
    job = json.loads((jd / "job.json").read_text(encoding="utf-8"))
    if not isinstance(version, int) or version < 0:
        raise ValueError('版本号必须为非负整数')
    aj = json.loads((jd / (f"align_v{version}.json" if version else "align.json")).read_text(encoding="utf-8"))
    if version:
        job['options'] = {**(job.get('options') or {}), **(aj.get('options') or {})}
    lines: list[KaraokeLine] = []
    for ln in aj["lines"]:
        toks = [KaraokeToken(text=t["text"], disp=t["disp"], unit=t.get("unit"),
                             start=t["start"], end=t["end"])
                for t in ln["tokens"]]
        lines.append(KaraokeLine(raw=ln["raw"], tokens=toks, head=ln.get("head", ""),
                                 start=ln["start"], end=ln["end"]))
    # head 兜底：由 raw 与 token 显示文本反推，保证重渲染时标点不丢
    for ln in lines:
        if not ln.head:
            joined = "".join(t.disp for t in ln.tokens)
            if ln.raw and joined and ln.raw != joined and ln.raw.endswith(joined):
                ln.head = ln.raw[: len(ln.raw) - len(joined)]
    return job, lines


def _insert_manual_line(text: str, start: float, end: float, lang: str) -> KaraokeLine:
    text = text.strip()
    if not text:
        raise ValueError('新增歌词不能为空')
    spec = LANGS.get(lang) or LANGS['ja']
    values = tokenize(text, spec)
    if not values:
        raise ValueError('新增歌词没有可对齐的字词')
    head, displays = build_display(text, values)
    span = end - start
    tokens = [KaraokeToken(value, displays[i], start + span*i/len(values),
                            start + span*(i+1)/len(values))
              for i, value in enumerate(values)]
    return KaraokeLine(raw=text, head=head, tokens=tokens, start=start, end=end)


def restyle(job_dir: str | Path, req: RestyleRequest | None = None,
            progress: ProgressFn | None = None) -> dict:
    """在既有对齐结果上应用人工微调并重新出片。

    只做「重建 ASS + 重跑 ffmpeg」，不加载任何模型 —— 秒级完成，
    因此适合在 WebUI 里反复试听微调。
    """
    prog = progress or _noop_progress
    req = req or RestyleRequest()
    jd = Path(job_dir)
    job, lines = load_job(jd, req.base_version)
    retry_evidence = None
    retry_report = None
    if req.retry_suspects:
        if req.anchor_row is not None or req.line_insertion or req.line_bounds or req.line_offsets_ms or req.token_offsets_ms:
            raise ValueError('请先保存或撤销手动调整，再单独运行疑难句重试')
        from alignment_review import attach_review
        from alignment_retry import retry_lines
        from asr_lyrics import AsrLine, Segment
        current = json.loads((jd/(f'align_v{req.base_version}.json' if req.base_version else 'align.json')).read_text(encoding='utf-8'))
        evidence = attach_review(jd, current)['diagnostics']
        scores = [r.get('confidence') for r in evidence.get('lines', [])]
        retry_config = json.loads((jd/'retry_config.json').read_text(encoding='utf-8')) if (jd/'retry_config.json').exists() else {}
        acoustic = [AsrLine(l.raw,l.start,l.end,[Segment(t.disp,t.start,t.end) for t in l.tokens],
                           scores[i] if i<len(scores) and scores[i] is not None else 0) for i,l in enumerate(lines)]
        refined, retry_evidence, retry_report = retry_lines(acoustic, evidence,
            [jd/'in/vg/vocals.wav',jd/'in/audio_44k.wav',Path(job['media'])],
            float(job['media_info']['duration']), {'ja':'Japanese','zh':'Chinese','en':'English'}.get(job.get('lang'),job.get('lang')),
            model=retry_config.get('model','large-v3'),device=retry_config.get('device','cuda'),progress=prog)
        if not retry_report['accepted']:
            return {'ok':True,'unchanged':True,'retry':retry_report}
        for i,(old,new) in enumerate(zip(acoustic,refined)):
            if old is not new:
                lines[i] = KaraokeLine(raw=new.text,start=new.start,end=new.end,
                    tokens=[KaraokeToken(text=t.text,disp=t.text,start=t.start,end=t.end) for t in new.segments])

    prog(0.05, "载入对齐结果")
    for i, ln in enumerate(lines):
        d = req.line_offsets_ms.get(i, req.line_offsets_ms.get(str(i), 0.0)) / 1000.0
        if d:
            for t in ln.tokens:
                t.start += d
                t.end += d
        for j, t in enumerate(ln.tokens):
            for key in (f"{i}:{j}", f"{i}_{j}"):
                if key in req.token_offsets_ms:
                    dd = req.token_offsets_ms[key] / 1000.0
                    t.start += dd
                    t.end += dd
    # 逐 token 调整后可能逆序，重新整理
    for i, ln in enumerate(lines):
        if (req.anchor_row is not None or req.retry_suspects) and not req.line_offsets_ms.get(i, req.line_offsets_ms.get(str(i), 0)) and not any(
                req.token_offsets_ms.get(f'{i}:{j}', req.token_offsets_ms.get(f'{i}_{j}', 0)) for j in range(len(ln.tokens))):
            continue  # Human-anchor runs must preserve unchanged prefix timings exactly.
        prev = -1e9
        for t in ln.tokens:
            if t.start < prev:
                span = t.end - t.start
                t.start, t.end = prev, prev + span
            prev = t.end
        ln.start, ln.end = ln.tokens[0].start, ln.tokens[-1].end

    for key, bounds in req.line_bounds.items():
        index = int(key)
        if not 0 <= index < len(lines):
            raise ValueError('无效歌词行号')
        ln = lines[index]
        start, end = float(bounds['start']), float(bounds['end'])
        duration = float(job['media_info'].get('duration', float('inf')))
        if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= duration:
            raise ValueError(f'第 {index+1} 句：起点和终点应在音频范围内，且起点小于终点')
        old_start, old_end = ln.start, ln.end
        if old_end <= old_start or not ln.tokens:
            raise ValueError(f'第 {index+1} 句原始时间无效，无法调整')
        factor = (end-start)/(old_end-old_start)
        for token in ln.tokens:
            token.start = start + (token.start-old_start)*factor
            token.end = start + (token.end-old_start)*factor
        ln.start, ln.end = start, end

    insertion = req.line_insertion
    if insertion is not None:
        if not isinstance(insertion, dict):
            raise ValueError('新增歌词参数无效')
        after = insertion.get('after_row')
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after < len(lines):
            raise ValueError('请选择新增歌词插入位置')
        text = str(insertion.get('text') or '').strip()
        mode = insertion.get('mode')
        if mode == 'manual':
            start, end = float(insertion.get('start')), float(insertion.get('end'))
            duration = float(job['media_info'].get('duration', float('inf')))
            if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= duration:
                raise ValueError('新增歌词起止时间无效')
            newline = _insert_manual_line(text, start, end, job.get('lang','ja'))
            lines.insert(after+1, newline)
        elif mode == 'auto':
            # Placeholder is replaced together with the remaining suffix.
            newline = _insert_manual_line(text, lines[after].end, min(lines[after].end+.1, float(job['media_info']['duration'])), job.get('lang','ja'))
            lines.insert(after+1, newline)
            from anchor_realign import realign_suffix
            lines = realign_suffix(job, lines, after, prog)
        else:
            raise ValueError('新增歌词定位方式无效')

    if req.anchor_row is not None and not insertion:
        from anchor_realign import realign_suffix
        lines = realign_suffix(job, lines, req.anchor_row, prog)
    opt = _ass_opt_from({**(job.get("options") or {}), **req.overrides})
    solve_windows(lines, opt)
    health = check_windows(lines, opt.min_gap_ms)

    ver = 1
    while (jd / f"karaoke_v{ver}.ass").exists():
        ver += 1
    prog(0.25, f"重建字幕（第 {ver} 版）")
    info = job["media_info"]
    ass_path = jd / f"karaoke_v{ver}.ass"
    ass_path.write_text(build_ass(lines, opt, width=info["width"] or 1920,
                                  height=info["height"] or 1080),
                        encoding="utf-8-sig")
    srt_path = jd / f"lyrics_v{ver}.srt"
    srt_path.write_text(build_srt(lines), encoding="utf-8-sig")

    out_audio = job.get("out_audio")
    acopy = out_audio is None and info.get("acodec") in ("aac", "mp3", "ac3", "eac3")
    out_video = jd / f"{job['job']}_karaoke_v{ver}.mp4"
    prog(0.5, "重新烧录成片")
    used_enc = render_video(job["media"], ass_path.name, jd, out_video,
                           out_audio, info,
                           job.get("encoder_choice", "auto"),
                           int(job.get("quality", 21)), acopy)
    prog(1.0, f"完成：{out_video.name}")

    (jd / f"align_v{ver}.json").write_text(json.dumps({
        "version": ver,
        "base_version": req.base_version,
        "anchor_row": req.anchor_row,
        "line_bounds": req.line_bounds,
        "line_insertion": req.line_insertion,
        "review_evidence": retry_evidence,
        "retry": retry_report,
        "operation": 'local_retry' if req.retry_suspects else (("insert_" + str(req.line_insertion.get('mode'))) if req.line_insertion else ("anchor_realign" if req.anchor_row is not None else "retime")),
        "created": time.time(),
        "health": health,
        "offsets": {str(k): v for k, v in req.line_offsets_ms.items()},
        "token_offsets": req.token_offsets_ms,
        "options": {**(job.get('options') or {}), **req.overrides},
        "lines": [{
            "raw": ln.raw, "start": round(ln.start, 4), "end": round(ln.end, 4),
            "ev_start": round(ln.ev_start, 4), "ev_end": round(ln.ev_end, 4),
            "head": ln.head,
            "tokens": [{"text": t.text, "disp": t.disp, "unit": t.unit,
                        "start": round(t.start, 4), "end": round(t.end, 4)}
                       for t in ln.tokens],
        } for ln in lines],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    from alignment_review import attach_review
    attach_review(jd, json.loads((jd/f'align_v{ver}.json').read_text(encoding='utf-8')),
                  jd/f'acceptance_v{ver}.json')
    return {"ok": True, "version": ver, "video": str(out_video),
            "ass": str(ass_path), "srt": str(srt_path), "health": health,
            "encoder": used_enc}


# ==========================================================================
# CLI
# ==========================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P1 卡拉OK字幕生产管线（本地模型，无在线 API）")
    ap.add_argument("--media", required=True, help="视频或音频文件")
    ap.add_argument("--lyrics", help="歌词文件 .txt/.lrc/.srt")
    ap.add_argument("--lyrics-text", help="直接给歌词文本")
    ap.add_argument("--audio-track", help="独立音轨（伴奏或干声），替换原音")
    ap.add_argument("--vocal-mode", choices=["keep", "remove"], default="remove")
    ap.add_argument("--lang", choices=["auto", "zh", "en", "ja"], default="auto")
    ap.add_argument("--backend", choices=["qwen", "wav2vec2"], default="qwen")
    ap.add_argument("--no-separate", action="store_true", help="跳过人声分离")
    ap.add_argument("--demucs", default="htdemucs_ft")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--timed-mode", choices=["warp", "ignore"], default="warp")
    ap.add_argument("--encoder", choices=["auto", "nvenc", "x264"], default="auto")
    ap.add_argument("--quality", type=int, default=21)
    ap.add_argument("--font", default="Microsoft YaHei")
    ap.add_argument("--font-size", type=int, default=66)
    ap.add_argument("--sung", default="FFD24A", help="已唱高亮色 RRGGBB")
    ap.add_argument("--unsung", default="FFFFFF", help="未唱底色 RRGGBB")
    ap.add_argument("--no-next", action="store_true", help="不显示下一句预览")
    ap.add_argument("--group-units", action="store_true",
                    help="同对齐单元内的连续字合并为一次高亮（日文推荐）")
    ap.add_argument("--lead-ms", type=int, default=320)
    ap.add_argument("--tail-ms", type=int, default=320)
    ap.add_argument("--out", default="out")
    ap.add_argument("--name", default="")
    args = ap.parse_args(argv)

    def rgb(h: str):
        h = h.lstrip("#")
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

    cfg = PipelineConfig(
        media=args.media, lyrics_path=args.lyrics, lyrics_text=args.lyrics_text or "",
        audio_track=args.audio_track, vocal_mode=args.vocal_mode, lang=args.lang,
        backend=args.backend, separate=not args.no_separate,
        demucs_model=args.demucs, device=args.device, timed_mode=args.timed_mode,
        encoder=args.encoder, quality=args.quality, out_dir=args.out, job_name=args.name,
        ass=AssOptions(font=args.font, font_size=args.font_size,
                       sung_color=rgb(args.sung), unsung_color=rgb(args.unsung),
                       next_line=not args.no_next, group_same_unit=args.group_units,
                       lead_ms=args.lead_ms, tail_ms=args.tail_ms),
    )
    res = run(cfg)
    print(json.dumps({"ok": res.ok, "video": res.video, "error": res.error,
                      "stats": res.stats}, ensure_ascii=False, indent=2))
    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
