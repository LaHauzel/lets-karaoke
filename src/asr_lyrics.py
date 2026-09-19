"""无歌词文件时的歌词来源：本地 Qwen3-ASR 转写 → 分行 → 增强 LRC

为什么需要这个模块
------------------
P1 流水线是「歌词驱动」的：强制对齐必须有目标文本才能给出时间戳。
用户手上常常只有一段视频（现场演出、翻唱、Live），没有歌词文件——
这时唯一的本地解法是先做语音转写，拿到歌词草稿，再喂给流水线。

设计要点
--------
1. **全本地**：Qwen3-ASR-1.7B 与 Qwen3-ForcedAligner-0.6B 都从 models/
   加载，零联网。转写时带 ``return_time_stamps=True``，ASR 的 generate
   与对齐共用同一套内部流程，一次就拿到「文本 + 每个单元的起止」。
2. **单元 → 原文字符的回映射**（关键）：Qwen 的分词单元会「吃掉」部分
   字符（实测日文 ``渡って`` → 单元 ``渡っ`` / ``て``，``っ`` 不在任何单元
   边界内）。直接拼单元文本会丢字，所以用 difflib 按字符对齐回原文，
   落入「插入区」的字符继承相邻单元的时间区间 —— 显示文本与转写完全一致。
3. **分行依据**：静音间隔（主）/ 句读标点 / 最大字数（兜底）。
   日文歌词往往完全没有标点，所以间隔是第一判据。
4. **输出增强 LRC，逐词标记带显式终点** ``<start~end>``：
   pipeline.parse_lyrics 解析后会走「直接采信、跳过声学对齐」的分支
   （见 pipeline.run 中 used_word_times），因此不会再对齐第二遍。
   注意标准增强 LRC 的 ``<t>`` 只给起点，终点靠「下一个标记起点」推断，
   会把每行末词压成极短片段 —— 所以这里必须显式带终点。

用法
----
    python src/asr_lyrics.py --media "C:/path/video.mp4"          # 打印歌词草稿
    python src/asr_lyrics.py --media x.mp4 --lrc out/x.lrc        # 落盘 LRC
    python src/asr_lyrics.py --media x.mp4 --dry-run              # 只抽音频/看时长
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

SR = 16000
ROOT = Path(__file__).resolve().parent.parent
MODELS_ASR = ROOT / "models" / "Qwen3-ASR-1.7B"
MODELS_QWEN = ROOT / "models" / "Qwen3-ForcedAligner-0.6B"

# UI 语言代码 -> Qwen 语言名（与 align_backends.QwenAligner 里的映射保持一致）
QWEN_LANG = {"zh": "Chinese", "en": "English", "ja": "Japanese"}


def qwen_language(code: str | None) -> str | None:
    """把 UI 的 ``lang`` 代码（zh/en/ja/auto）转成 Qwen 语言名；auto -> None。

    **别把 None 当无所谓**：实测同一首日文歌，强制 ``Japanese`` 转出 911 字、
    行级时间可用；不指定（自动判别）被误判成 ``English``，转出 2280 字幻觉、
    零宽单元从 63% 涨到 84%。指定语言是这条链路里最便宜也最有效的一个开关。
    """
    if not code or code in ("auto", "Auto", "AUTO"):
        return None
    return QWEN_LANG.get(code, None)


# 句读标点：命中即倾向断行（日文歌词常无标点，所以只是辅助判据）
_PUNCT_END = "。、！？…‥ー—,.;:!?)]）】」』"
# LRC 语法字符：出现在正文里会污染解析，直接剔除
_LRC_UNSAFE = re.compile(r"[\[\]<>]")
# 纯括号内容的段落标记，如 (間奏)
_SECTION_ONLY = re.compile(r"^[\s(\[【（].{0,20}?[\s)\]】）]*$")
_LATIN = re.compile(r"[A-Za-z0-9']+")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f]")


def _is_cjk(ch: str) -> bool:
    return bool(_CJK.match(ch))


def sanitize(text: str) -> str:
    """剔除会破坏 LRC 语法的字符，并压掉多余空白。"""
    return _LRC_UNSAFE.sub("", text).strip()


# ==========================================================================
# 数据结构
# ==========================================================================


@dataclass
class Segment:
    """一个对齐单元（可能是 1 个或多个字符），带真实起止。"""

    text: str
    start: float
    end: float
    prob: float = 1.0   # 来源词的概率（证据词判定用）

    @property
    def visible_len(self) -> int:
        return len(re.sub(r"\s", "", self.text))


@dataclass
class AsrLine:
    text: str
    start: float
    end: float
    segments: list[Segment] = field(default_factory=list)
    prob: float = 1.0   # 行平均词概率（置信分级用；无词级数据时视为可信）

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class AsrLyricsResult:
    language: str
    raw_text: str
    lines: list[AsrLine]
    lrc: str
    plain: str
    srt: str
    diag: dict = field(default_factory=dict)

    def summary(self) -> str:
        d = self.diag
        return (
            f"语言={self.language or '?'}  单元={d.get('units', 0)}  "
            f"零宽单元={d.get('zero_units', 0)}({d.get('collapse_ratio', 0)*100:.0f}%)  "
            f"重建={d.get('collapse_repaired', 0)}/{d.get('collapse_before', 0)}"
            f"→{d.get('collapse_after', 0)}  "
            f"成行={len(self.lines)}  时长={d.get('audio_dur', 0):.1f}s  "
            f"推理={d.get('infer_sec', 0):.1f}s"
        )


# ==========================================================================
# 单元 -> 原文字符 的回映射
# ==========================================================================


def units_to_char_spans(text: str, units: list[Segment]) -> list[tuple[str, float, float, int]]:
    """把「单元序列」映射到「原文每个字符」的时间区间。

    返回 ``[(char, start, end, unit_index), ...]``，长度等于 ``len(text)``。

    做法与 ``pipeline.map_units_to_tokens`` 同思路但面向字符：
      * 先把每个单元按字符均分，得到 ``joined``（各单元文本直接拼接）上
        逐字符的等价时间；
      * 用 difflib 在 ``joined`` 与 ``text`` 之间求字符级匹配块；
      * equal 块长度必然相等，直接 1:1 搬运；
      * replace 块按时间区间均摊；insert 块（单元没覆盖到的字符，如空格、
        被分词吃掉的促音 ``っ``）留空，最后继承相邻单元。
    """
    if not text or not units:
        return []

    joined = "".join(u.text for u in units)
    c_start: list[float] = []
    c_end: list[float] = []
    c_unit: list[int] = []
    for i, u in enumerate(units):
        n = max(1, len(u.text))
        span = max(0.0, u.end - u.start)
        for k in range(len(u.text)):
            c_start.append(u.start + span * k / n)
            c_end.append(u.start + span * (k + 1) / n)
            c_unit.append(i)

    spans: list[tuple[str, float, float, int] | None] = [None] * len(text)
    last_unit = 0

    sm = difflib.SequenceMatcher(None, joined, text, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                ci = i1 + k
                spans[j1 + k] = (text[j1 + k], c_start[ci], c_end[ci], c_unit[ci])
                last_unit = c_unit[ci]
        elif tag == "replace" and i2 > i1:
            t0, t1 = c_start[i1], c_end[i2 - 1]
            n = j2 - j1
            ui = c_unit[i1]
            for k in range(n):
                spans[j1 + k] = (text[j1 + k], t0 + (t1 - t0) * k / n,
                                 t0 + (t1 - t0) * (k + 1) / n, ui)
            last_unit = c_unit[i2 - 1]
        # insert: 先留空，交给下面的邻居继承

    out: list[tuple[str, float, float, int]] = []
    prev: tuple[float, float, int] | None = None
    for i, sp in enumerate(spans):
        if sp is None:
            nxt = next((s for s in spans[i + 1:] if s is not None), None)
            if prev is not None:
                sp = (text[i], prev[0], prev[1], prev[2])
            elif nxt is not None:
                sp = (text[i], nxt[1], nxt[2], nxt[3])
            else:
                sp = (text[i], 0.0, 0.04, last_unit)
        prev = (sp[1], sp[2], sp[3])
        out.append(sp)
    return out


def merge_to_segments(char_spans: list[tuple[str, float, float, int]]) -> list[Segment]:
    """把逐字符区间按 unit_index 合并回单元，保证「同单元整段一起扫」。"""
    segs: list[Segment] = []
    cur_ui: int | None = None
    for ch, st, en, ui in char_spans:
        if segs and cur_ui == ui:
            segs[-1].text += ch
            segs[-1].end = max(segs[-1].end, en)
        else:
            segs.append(Segment(text=ch, start=st, end=en))
            cur_ui = ui
    return segs


# ==========================================================================
# 塌缩段重建
# ==========================================================================


def de_collapse(
    segments: list[Segment],
    floor: float = 0.045,
    min_run: int = 3,
    max_span: float = 8.0,
    audio_dur: float | None = None,
) -> tuple[list[Segment], dict]:
    """把「连续被压成零宽」的单元段按锚点重新摊开。

    为什么会塌缩
    ------------
    ASR 在**器乐段/长音**上会产生幻觉文本（实测：同一句英文连出三遍、同一时刻
    重复），这段文本在音频里没有对应发声，强制对齐器只能把它们全挤到一个点上，
    于是整段单元的宽度变成 0（本模块补成 40ms 下限）。直接渲染的表现是：十几个
    字在一瞬间掠过，而后面一句被硬拉到几秒之外。

    这里做的事
    ----------
    找连续塌缩段，用「段首起点 → 下一个健康单元的起点」当区间，按等分重新摊开，
    并把步长上限锚定到健康单元的中位时长。**这是插值不是真值**，只保证时间轴
    连贯可读，不修正被认错的字。区间长度上限 ``max_span`` 秒，避免一段幻觉把
    后面几十秒的正常内容挤掉；没有足够空间时不修，交给字幕层的防重叠求解处理。
    """
    n = len(segments)
    if n == 0:
        return segments, {"runs": 0, "repaired_units": 0, "before": 0, "after": 0}

    dur = lambda s: max(0.0, s.end - s.start)          # noqa: E731
    collapsed = [dur(s) <= floor for s in segments]
    before = sum(collapsed)

    healthy = sorted(d for d in (dur(s) for s in segments) if d > floor)
    step = healthy[len(healthy) // 2] if healthy else 0.18
    step = min(max(step, 0.08), 0.60)

    out = list(segments)
    runs = repaired = skipped = 0
    i = 0
    while i < n:
        if not collapsed[i]:
            i += 1
            continue
        j = i
        while j < n and collapsed[j]:
            j += 1
        run_len = j - i
        if run_len >= min_run:
            span_start = out[i].start
            if j < n:
                span_end = out[j].start
            else:
                span_end = span_start + step * run_len
                if audio_dur:
                    span_end = min(span_end, float(audio_dur))
            span = span_end - span_start
            if span >= run_len * floor:
                use_step = min(span, max_span) / run_len
                t = span_start
                for k in range(i, j):
                    out[k] = Segment(text=out[k].text, start=t, end=t + use_step)
                    t += use_step
                runs += 1
                repaired += run_len
            else:
                skipped += 1          # 空间不足，不硬塞
        i = j

    # 守门：重建后仍保持整体单调
    for k in range(1, len(out)):
        if out[k].start < out[k - 1].end:
            out[k] = Segment(text=out[k].text,
                             start=out[k - 1].end,
                             end=max(out[k - 1].end, out[k].end))

    after = sum(1 for s in out if dur(s) <= floor)
    return out, {"runs": runs, "repaired_units": repaired, "skipped_runs": skipped,
                 "before": before, "after": after, "step": step}


# ==========================================================================
# 分行
# ==========================================================================


def split_lines(
    segments: list[Segment],
    gap_s: float = 0.75,
    max_chars: int = 30,
    min_chars: int = 6,
) -> list[AsrLine]:
    """按静音间隔 / 句读 / 最大字数把单元序列切成歌词行。"""
    lines: list[AsrLine] = []
    cur: list[Segment] = []

    def flush() -> None:
        nonlocal cur
        if not cur:
            return
        text = sanitize("".join(s.text for s in cur))
        if text and not _SECTION_ONLY.match(text):
            lines.append(AsrLine(text=text, start=cur[0].start,
                                 end=cur[-1].end, segments=list(cur)))
        cur = []

    for i, seg in enumerate(segments):
        prev = segments[i - 1] if i else None
        if prev is not None:
            gap = seg.start - prev.end
            cur_chars = sum(s.visible_len for s in cur)
            # ① 明显静音 -> 断行
            if gap >= gap_s and cur_chars >= min_chars:
                flush()
            # ② 句读收尾 -> 断行
            elif cur and cur[-1].text.rstrip().endswith(tuple(_PUNCT_END)) \
                    and cur_chars >= min_chars:
                flush()
            # ③ 太长 -> 就近断行
            elif cur_chars >= max_chars:
                flush()
        cur.append(seg)
    flush()
    return lines


# ==========================================================================
# 序列化
# ==========================================================================


def _fmt(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60):02d}:{t % 60:05.2f}"


def to_enhanced_lrc(lines: list[AsrLine]) -> str:
    """增强 LRC：``[行起点]<词起~词终>词...``。

    逐词标记带显式终点，pipeline 解析后可直接采信、跳过二次对齐。
    """
    out: list[str] = []
    for ln in lines:
        parts: list[str] = []
        for s in ln.segments:
            # 不 strip：段内/段尾空格要保留（英文歌词 "I hear" 不能变成 "Ihear"）。
            # parse_lyrics 的行文本 body 会原样保留这些空格，build_display 再把它们
            # 归还给前一个 token 的显示文本。
            t = _LRC_UNSAFE.sub("", s.text)
            if not t.strip():
                continue
            end = max(s.end, s.start + 0.01)
            parts.append(f"<{_fmt(s.start)}~{_fmt(end)}>{t}")
        if not parts:
            continue
        if not sanitize(ln.text):
            continue
        out.append(f"[{_fmt(ln.start)}]" + "".join(parts))
    return "\n".join(out) + ("\n" if out else "")


def to_plain(lines: list[AsrLine]) -> str:
    return "\n".join(sanitize(l.text) for l in lines if sanitize(l.text)) + "\n"


def to_srt(lines: list[AsrLine]) -> str:
    def ts(t: float) -> str:
        t = max(0.0, t)
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms == 1000:
            s, ms = s + 1, 0
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = []
    for i, ln in enumerate(lines, 1):
        txt = sanitize(ln.text)
        if not txt:
            continue
        blocks.append(f"{i}\n{ts(ln.start)} --> {ts(max(ln.end, ln.start + 0.1))}\n{txt}\n")
    return "\n".join(blocks)


# ==========================================================================
# ASR 后端
# ==========================================================================


class LocalAsr:
    """Qwen3-ASR-1.7B + Qwen3-ForcedAligner-0.6B 的本地转写封装。

    ``transcribe()`` 一次返回整段文本与全部对齐单元（含起止秒）。

    关于 ``align_chunk_sec``
    -----------------------
    qwen_asr 在 ``return_time_stamps=True`` 时按 ``MAX_FORCE_ALIGN_INPUT_SECONDS``
    （库默认 **180 秒**）切块再逐块对齐。这个长度对「干净朗读」够用，对**音乐**
    远远过长：实测 356 秒的 J-ROCK 现场，180 秒块内对齐器直接退化——大量单元
    宽度为 0（被本模块补成 40ms），整段被挤成一条线。降到 30 秒后单元恢复成
    正常的连续区间。切块用能量最低点当边界，所以不会从词中间切。
    """

    def __init__(
        self,
        asr_dir: str | Path | None = None,
        aligner_dir: str | Path | None = None,
        device: str = "cuda",
        language: str | None = None,
        align_chunk_sec: float = 30.0,
        context: str = "",
    ):
        import qwen_asr  # noqa: F401  (导入即完成 AutoConfig/AutoModel 注册)

        self.asr_dir = Path(asr_dir) if asr_dir else MODELS_ASR
        self.aligner_dir = Path(aligner_dir) if aligner_dir else MODELS_QWEN
        self.device = device
        self.language = language
        self.align_chunk_sec = float(align_chunk_sec)
        self.context = context or ""
        if not self.asr_dir.exists():
            raise FileNotFoundError(
                f"ASR 权重缺失：{self.asr_dir}\n  先跑  python src/fetch_models.py --only asr")
        if not self.aligner_dir.exists():
            raise FileNotFoundError(
                f"对齐权重缺失：{self.aligner_dir}\n  先跑  python src/fetch_models.py --only aligner")

        from qwen_asr import Qwen3ASRModel

        kwargs: dict = {}
        try:
            import torch

            kwargs["dtype"] = torch.bfloat16
            if device.startswith("cuda"):
                kwargs["device_map"] = device
        except Exception:
            pass
        t0 = time.time()
        self.model = Qwen3ASRModel.from_pretrained(
            str(self.asr_dir),
            forced_aligner=str(self.aligner_dir),
            max_inference_batch_size=8,
            **kwargs,
        )
        self.load_sec = time.time() - t0

    # ------------------------------------------------------------------
    def transcribe(self, audio, context: str | None = None) -> tuple[str, str, list[Segment], dict]:
        """audio: 路径 / (ndarray, sr) 元组。返回 (语言, 文本, 单元, 诊断)。

        诊断字段：``infer_sec``（generate+对齐合计，一次调用无法拆分）、
        ``chunks``、``zero_units``（对齐器返回零宽度的单元数）。

        ``context``
        ----------
        可选的「提示文本」，交给 ASR 作为解码上下文。**实测这是无歌词场景的
        正确解法**：不给上下文时，模型在器乐段自由发挥会吐出完全无关的文本
        （英文幻觉句、乱码专名）；把已知的正确歌词作为 context 传进去后，
        转录结果会向歌词收敛，配合本就正常的分块对齐，得到的时间轴才可用。
        """
        import qwen_asr.inference.qwen3_asr as qa

        ctx = self.context if context is None else context
        t0 = time.time()
        old = qa.MAX_FORCE_ALIGN_INPUT_SECONDS
        qa.MAX_FORCE_ALIGN_INPUT_SECONDS = self.align_chunk_sec
        try:
            res = self.model.transcribe(
                audio,
                context=ctx,
                language=self.language,
                return_time_stamps=True,
            )
        finally:
            qa.MAX_FORCE_ALIGN_INPUT_SECONDS = old
        infer_sec = time.time() - t0

        r = res[0] if isinstance(res, (list, tuple)) else res
        text = (getattr(r, "text", "") or "").strip()
        lang = getattr(r, "language", "") or (self.language or "")
        units, zero = _extract_units(getattr(r, "time_stamps", None))
        meta = {
            "infer_sec": infer_sec,
            "zero_units": zero,
            "align_chunk_sec": self.align_chunk_sec,
            "context_chars": len(ctx or ""),
        }
        return lang, text, units, meta


def _extract_units(ts) -> tuple[list[Segment], int]:
    """把 ForcedAlignResult(.items) 归一成 Segment 列表；同时统计零宽单元。"""
    if ts is None:
        return [], 0
    items = getattr(ts, "items", None)
    if items is None and isinstance(ts, (list, tuple)):
        items = list(ts)
    units: list[Segment] = []
    zero = 0
    for it in items or []:
        txt = getattr(it, "text", None)
        st = getattr(it, "start_time", None)
        en = getattr(it, "end_time", None)
        if txt is None and isinstance(it, dict):
            txt, st, en = it.get("text"), it.get("start_time"), it.get("end_time")
        if txt is None or st is None:
            continue
        st, en = float(st), float(en if en is not None else st)
        if en <= st:
            zero += 1
            en = st + 0.04      # 40ms 分辨率下限：模型给出的零长单元
        units.append(Segment(text=str(txt), start=st, end=en))
    return units, zero


# ==========================================================================
# 顶层入口
# ==========================================================================


def build_lyrics(
    lang: str,
    text: str,
    units: list[Segment],
    audio_dur: float,
    gap_s: float = 0.75,
    max_chars: int = 30,
    meta: dict | None = None,
    repair: bool = True,
) -> AsrLyricsResult:
    meta = meta or {}
    char_spans = units_to_char_spans(text, units)
    segments = merge_to_segments(char_spans)
    fix: dict = {"runs": 0, "repaired_units": 0, "before": 0, "after": 0}
    if repair:
        segments, fix = de_collapse(segments, audio_dur=audio_dur)
    lines = split_lines(segments, gap_s=gap_s, max_chars=max_chars)
    covered = len(char_spans)
    zero = int(meta.get("zero_units", 0))
    diag = {
        "units": len(units),
        "chars": covered,
        "text_chars": len(text),
        "coverage": (covered / len(text)) if text else 0.0,
        "audio_dur": audio_dur,
        "infer_sec": meta.get("infer_sec", 0.0),
        "align_chunk_sec": meta.get("align_chunk_sec", None),
        # 对齐健康度：对齐器直接吐出的零宽单元占比（重建前）
        "zero_units": zero,
        "collapse_ratio": (zero / len(units)) if units else 0.0,
        # 塌缩段重建的规模（重建前/后仍处下限的段数）
        "collapse_runs": fix.get("runs", 0),
        "collapse_repaired": fix.get("repaired_units", 0),
        "collapse_before": fix.get("before", 0),
        "collapse_after": fix.get("after", 0),
        "median_step": fix.get("step", 0.0),
        "chars_per_sec": (covered / audio_dur) if audio_dur else 0.0,
        "gap_s": gap_s,
        "max_chars": max_chars,
    }
    return AsrLyricsResult(
        language=lang, raw_text=text, lines=lines,
        lrc=to_enhanced_lrc(lines), plain=to_plain(lines), srt=to_srt(lines),
        diag=diag,
    )


def generate_lyrics(
    media: str | Path,
    asr: LocalAsr,
    gap_s: float = 0.75,
    max_chars: int = 30,
    work_dir: str | Path | None = None,
    progress=None,
    context: str | None = None,
) -> AsrLyricsResult:
    """视频/音频 -> 歌词草稿（增强 LRC）。音频抽成 16k 单声道喂给 ASR。"""
    from pipeline import extract_wav, probe_media

    media = Path(media)
    info = probe_media(media)
    wav = Path(work_dir or media.parent) / f"{media.stem}__asr16k.wav"

    def say(p, msg):
        if progress:
            progress(p, msg)

    say(0.05, "抽取 16k 单声道音频（ASR 输入）")
    extract_wav(media, wav, sr=SR, mono=True)

    ctx = asr.context if context is None else context
    hint = f"，已注入歌词上下文 {len(ctx)} 字" if ctx else ""
    say(0.20, f"本地 ASR 转写中（Qwen3-ASR-1.7B，对齐分块 {asr.align_chunk_sec:.0f}s{hint}）…")
    lang, text, units, meta = asr.transcribe(str(wav), context=ctx)
    say(0.85, f"转写完成：{len(units)} 个单元 / {len(text)} 字，开始分行")

    res = build_lyrics(
        lang=lang, text=text, units=units,
        audio_dur=info.get("duration", 0.0),
        gap_s=gap_s, max_chars=max_chars, meta=meta,
    )
    say(0.95, f"歌词生成完成：{len(res.lines)} 行"
              f"（零宽单元 {res.diag['zero_units']}/{res.diag['units']}，"
              f"重建 {res.diag['collapse_repaired']} 段）")
    return res


def run_asr_lyrics(
    media: str | Path,
    out_lrc: str | Path | None = None,
    device: str = "cuda",
    language: str | None = None,
    gap_s: float = 0.75,
    max_chars: int = 30,
    align_chunk_sec: float = 30.0,
    progress=None,
    context: str | None = None,
) -> AsrLyricsResult:
    asr = LocalAsr(device=device, language=language, align_chunk_sec=align_chunk_sec,
                   context=context or "")
    res = generate_lyrics(media, asr, gap_s=gap_s, max_chars=max_chars,
                          work_dir=str(Path(out_lrc).parent) if out_lrc else None,
                          progress=progress, context=context)
    if out_lrc:
        p = Path(out_lrc)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(res.lrc, encoding="utf-8")
        p.with_suffix(".plain.txt").write_text(res.plain, encoding="utf-8")
        p.with_suffix(".srt").write_text(res.srt, encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="本地 ASR 生成歌词草稿（无歌词文件场景）")
    ap.add_argument("--media", required=True, help="视频或音频路径")
    ap.add_argument("--lrc", help="增强 LRC 输出路径（同时产出 .plain.txt / .srt）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--lang", default=None, help="强制语言，如 Japanese；留空自动判别")
    ap.add_argument("--gap", type=float, default=0.75, help="断行静音阈值（秒）")
    ap.add_argument("--max-chars", type=int, default=30, help="单行最大字数")
    ap.add_argument("--align-chunk", type=float, default=30.0,
                    help="对齐分块秒数；音乐建议 20~40，库默认 180 会退化")
    ap.add_argument("--context-file", help="把已知歌词作为上下文喂给 ASR（强烈建议：无歌词场景下"
                                          "不给上下文会产生大量幻觉）")
    ap.add_argument("--dry-run", action="store_true", help="只抽音频看时长，不加载模型")
    args = ap.parse_args()

    if args.dry_run:
        from pipeline import extract_wav, probe_media

        info = probe_media(args.media)
        print("媒体信息:", {k: v for k, v in info.items() if k != "acodec"})
        wav = Path(args.media).parent / (Path(args.media).stem + "__asr16k.wav")
        extract_wav(args.media, wav, sr=SR, mono=True)
        print("16k 单声道 WAV:", wav, f"{wav.stat().st_size/1e6:.1f} MB")
        return 0

    t0 = time.time()
    ctx = ""
    if args.context_file:
        ctx = Path(args.context_file).read_text(encoding="utf-8")
        print(f"已读取歌词上下文: {args.context_file}（{len(ctx)} 字）", flush=True)
    res = run_asr_lyrics(
        args.media, out_lrc=args.lrc, device=args.device,
        language=args.lang, gap_s=args.gap, max_chars=args.max_chars,
        align_chunk_sec=args.align_chunk, context=ctx,
        progress=lambda p, m: print(f"[{p*100:3.0f}%] {m}", flush=True),
    )
    print("\n" + res.summary())
    print(f"总耗时 {time.time() - t0:.1f}s（含模型加载）")
    print("\n--- 歌词草稿（前 40 行） ---")
    for ln in res.lines[:40]:
        print(f"  {_fmt(ln.start):>8s} ~ {_fmt(ln.end):<8s} {ln.text}")
    if len(res.lines) > 40:
        print(f"  … 其余 {len(res.lines) - 40} 行")
    if args.lrc:
        print(f"\n已写出: {args.lrc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
