# -*- coding: utf-8 -*-
"""已知歌词文本 -> 逐字时间轴（whisper/stable-ts 强制对齐）。

适用场景
--------
**演唱 / 带伴奏** 的音频。Qwen3-ForcedAligner 在这类音频上整体失效
（无论文本对错都输出大量零宽单元，且零宽比率对文本正确性无判别力，
见 skill ``local-forced-align`` 坑 14）；whisper 的交叉注意力 DTW 对演唱明显更稳。

用法
----
    python src/whisper_align.py --audio out/example/source_audio.wav \
        --lyrics out/example/lyrics.txt --lang Japanese \
        --out out/example/lyrics.lrc

产出一套三件：增强 LRC（可直接喂 ``pipeline.py``）、plain.txt、srt。

后处理（实测必要）
------------------
1. **行内断档重排**：whisper 偶尔把个别词甩过器乐段（同句的词一个在 solo 前、
   一个在 solo 后）。句内词间隔 > ``GAP_MAX`` 秒几乎必然是错锚 —— 整句按
   「全局中位字速」从首词起点连续重排。不要用能量阈值判：现场人声 stem
   混着观众噪声，能量法失真。
2. **前向扫描**：保证行不重叠且每行有最短时长，否则结尾密集的 hook 段
   会出现 0.04s 的闪现行。
3. **空格保留**：char 粒度下把空格挂到前一个字符的显示文本上，
   否则英文行渲染成 ``Ihearadoor``（配合 asr_lyrics.to_enhanced_lrc 的不 strip 修复）。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from asr_lyrics import AsrLine, Segment, to_enhanced_lrc, to_plain, to_srt  # noqa: E402
import model_paths  # noqa: E402,F401  （whisper 权重指向 models\whisper\）

GAP_MAX = 3.0        # 句内词间隔超过这个秒数视为错锚
MAX_W_DUR = 1.5      # 单词合理时长上限（用于筛「健康词」估字速）
MIN_LINE_DUR = 0.30  # 每行最短时长（秒）
MIN_CHAR_RATE = 0.09 # 每字符至少占的秒数（按行字数折算最短时长，防闪现行）

# —— 超长行检测（whisper 把整句塞进器乐段的典型症状）——
OVER_LONG_FACTOR = 2.0   # 实际时长 > max(OVER_LONG_MIN, 字速×字数×FACTOR) 判为超长
OVER_LONG_MIN = 8.0
PROB_OK = 0.60           # 行平均词概率高于此 → 起点可信（只修拖尾）；否则整句错锚
SUSPECT_PROB = 0.60      # 低于此的行进诊断，提示人工复核
MAX_TAIL_GAIN = 12.0     # 拖音补偿的行尾最大延长（秒）

# —— 启发式规则开关（WebUI「高级选项」透传到这里）——
DEFAULT_RULES = {
    "boundary_limit": 0.6,   # 自动模式：只在此距离内微调首尾声学边界
    "reflow_gapped": True,   # 句内断档重排（句尾甩远 → 锚回起点）
    "fix_over_long": True,   # 超长行兜底（无内部断档的连续型拉伸）
    "start_snap": True,      # 起点吸附到人声出现点
    "tail_clamp": True,      # 行尾越界收回（人声结束后不再显示）
    "tail_extend": True,     # 拖音补偿（人声还在唱时行尾延长）
    "relocate_lowconf": True,  # 低置信行重定位（搬进未认领的人声区间）
    "clamp_tails_per_line": True,  # 逐行尾部收回（人声停下即不再滚动）
    "enforce_timing": True,  # 行不重叠 + 最短时长（安全网，建议常开）
    "gap_max": GAP_MAX,          # 句内断档判定阈值（秒）
    "tail_gain": MAX_TAIL_GAIN,  # 拖音延长上限（秒）
    "extend_min_p": 0.50,        # 拖音补偿准入：行平均词概率低于此值不做延长
    "char_dur_max": 2.0,         # 单字时长上限（秒）——防「整行全亮还挂着」
    "vocal_thr": 0.12,           # 人声活跃阈值（峰值的比例）
    "rel_drop_db": 6.0,          # 相对能量剔除：短段比活跃中位低这么多 dB → 不算人声
    "drop_beyond_vocals": True,  # 起点已落在最后一段人声之后的行 → 丢弃（现场没唱）
    "tx_crosscheck": True,       # 转写交叉验证：干声自由转写，转不出歌词的发声段不算人声
    "tx_min_match": 0.34,        # 转写段与歌词的重叠系数 ≥ 此值 → 认为在唱歌词
    "tx_prune_min": 1.0,         # 非歌词发声段与包络区间交叠 ≥ 此秒数才剪
    "tx_max_seg": 8.0,           # 只剪短于 this 的不匹配段（长段多半是转写质量问题）
    "tx_cap_frac": 0.10,         # 保险丝：候选剪除总量超过包络的此比例 → 整体放弃
    "zeroconf_collapse": True,   # 行内词平均概率 ≈ 0 → 收缩成最短窗口（不匀速铺开）
    "zeroconf_p": 0.10,          # 「零证据」行的概率阈值
    "min_line_dur": MIN_LINE_DUR,  # 行最短时长（秒）
    "char_rate_min": MIN_CHAR_RATE,  # 每字符最短时长（秒）
    "conf_high": 0.75,   # 行平均词概率 ≥ 此值 → 高置信（位置可信）
    "conf_low": 0.60,    # 行平均词概率 < 此值 → 低置信（位置不可信）
    "ev_prob": 0.60,     # 「证据词」概率阈值
    "ev_dur": 0.15,      # 「证据词」最短时长（排除 0.02s 的退化闪点）
}


def _norm_rules(rules: dict | None) -> dict:
    """合并用户规则与默认值；未知键忽略。"""
    r = dict(DEFAULT_RULES)
    if rules:
        for k, v in rules.items():
            if k in r:
                r[k] = v
    return r


def postprocess_lines(lines, intervals, rules=None, profile="automatic"):
    """唯一后处理入口，供网页、命令行和回归评估共用。"""
    from alignment_policy import bounded_refine, resolve_rules
    r = resolve_rules(DEFAULT_RULES, rules, profile)
    if profile == "automatic":
        return bounded_refine(lines, intervals, r)
    report = {"mode": "legacy"}
    # Balanced mode snaps starts first; global tail clipping runs LAST, so
    # minimum-duration enforcement cannot push subtitles past the vocal end.
    guide_rules = {**r, "tail_clamp": False} if profile == "balanced" else r
    report["vocal"] = vocal_guide(lines, intervals, rules=guide_rules)
    report["relocate"] = relocate_lowconf(lines, intervals, rules=r)
    report["collapse"] = collapse_zeroconf(lines, rules=r)
    report["extend"] = extend_tails(lines, intervals, rules=r)
    if r["enforce_timing"]:
        enforce_timing(lines, min_dur=r["min_line_dur"], char_rate=r["char_rate_min"])
    report["cap"] = cap_char_durations(lines, rules=r)
    report["clamp"] = clamp_tails(lines, intervals, rules=r)
    if profile == "balanced":
        report["final_tail"] = vocal_guide(lines, intervals, rules={**r, "start_snap": False})
        report["mode"] = "balanced"
    return report


def _iter_words(line: AsrLine):
    """把字符级 Segment 按概率边界合回词级单元，yield (start, prob, end)。

    words_to_segments 会把一个词拆成单字符（同 prob），据此无损合并；
    但 _respan/enforce_timing 重铺时间后词内字符时长可能不均，这里只
    用 prob 边界近似还原词边界。
    """
    cur = None
    for s in line.segments:
        if cur is None or abs(s.prob - cur[1]) > 1e-9:
            if cur is not None:
                yield tuple(cur)
            cur = [s.start, s.prob, s.end]
        else:
            cur[2] = s.end
    if cur is not None:
        yield tuple(cur)


def _has_evidence(line: AsrLine, R: dict) -> bool:
    """行内是否存在「证据词」（概率高且时长足，非退化闪点）——词级判定。

    它的位置就值得信（示例证据词@100.2 p=0.68、另一个证据词@287 p=0.79）；
    全是 p<0.2 的行整句都是猜的（'an unrecorded lyric line'@101 p≈0.02）。
    注意必须在词级判定：字符级会把 0.45s 的词拆成 4 个 0.11s 字符而漏判。
    """
    return any(p >= R["ev_prob"] and (e - s) >= R["ev_dur"]
               for s, p, e in _iter_words(line))


def _vis_len(line: AsrLine) -> int:
    """去空白后的可见字符数。"""
    n = sum(s.visible_len for s in line.segments)
    return n or len(line.text)


def _nsp(s: str) -> str:
    return "".join(s.split())


# ==========================================================================
# 纯后处理（不依赖 whisper，可单测）
# ==========================================================================

def group_words_to_lines(words: list[list], lines: list[str]) -> tuple[list[list], int]:
    """按「去空白后字符数配平」把词分到歌词行。返回 (分组, 已覆盖词数)。"""
    lens = [len(_nsp(t)) for t in lines]
    groups: list[list] = [[] for _ in lines]
    k, acc = 0, 0
    for w in words:
        if k >= len(lines):
            break
        groups[k].append(w)
        acc += len(_nsp(w[0]))
        while acc >= lens[k] and k < len(lines) - 1:
            acc -= lens[k]
            k += 1
    return groups, sum(len(g) for g in groups)


def estimate_rate(words: list[list]) -> float:
    """全局字速：健康词（时长 <= MAX_W_DUR）的 秒/字 中位数。"""
    rates = []
    for w in words:
        v, d = len(_nsp(w[0])), w[2] - w[1]
        if v and d <= MAX_W_DUR:
            rates.append(d / v)
    return statistics.median(rates) if rates else 0.18


def _word_evidence(grp: list[list], lo: float, hi: float) -> bool:
    """词级证据判定：[lo, hi] 时间范围内是否存在 p≥0.6 且时长≥0.15s 的词。"""
    return any(w[3] >= 0.6 and (w[2] - w[1]) >= 0.15
               for w in grp if lo <= w[1] <= hi)


def reflow_if_gapped(grp: list[list], rate: float, gap_max: float = GAP_MAX) -> bool:
    """句内断档 > gap_max 则整句从首词起点连续重排。返回是否重排。

    才有证据词、前侧没有时，起点是假的、证据在断档后——此时不做起点重排
    （返回 False），把行交给 relocate_lowconf 的证据锚定分支处理。
    """
    gaps = [grp[i + 1][1] - grp[i][2] for i in range(len(grp) - 1)]
    if not gaps or max(gaps) <= gap_max:
        return False
    k = gaps.index(max(gaps))
    g0 = grp[k][2]          # 断档前侧结束
    g1 = grp[k + 1][1]      # 断档后侧起点
    if _word_evidence(grp, g1, grp[-1][2] + 1.0) \
            and not _word_evidence(grp, grp[0][1] - 1.0, g0):
        return False        # 证据只在断档后侧 → 保留原样，交给重定位锚定
    cur = grp[0][1]
    for w in grp:
        v = max(len(_nsp(w[0])), 1)
        w[1], w[2] = cur, cur + rate * v
        cur = w[2]
    return True


def fix_over_long(grp: list[list], rate: float) -> str | None:
    """超长行修复：实际时长远超「字速×字数」→ 整句被塞进了器乐段。

    锚定侧用**行平均词概率**决定（whisper 给的、Qwen 对齐器没有的信号）：
      * 平均概率 >= PROB_OK：起点是真的，只是尾巴拖进器乐段 → 锚**起点**；
      * 平均概率 <  PROB_OK：整句都是错的 → 锚**行尾**（跟着下一句走，
        实测能复现人工校对的位置，如 "a lyric line" → 282.6s）。
    返回 'start' / 'end' / None（未触发）。
    """
    chars = sum(len(_nsp(w[0])) for w in grp)
    if not chars or len(grp) < 2:
        return None
    dur = grp[-1][2] - grp[0][1]
    if dur <= max(OVER_LONG_MIN, rate * chars * OVER_LONG_FACTOR):
        return None
    avg = statistics.mean(w[3] for w in grp)
    side = "start" if avg >= PROB_OK else "end"
    cur = grp[0][1] if side == "start" else grp[-1][2] - rate * chars
    for w in grp:
        v = max(len(_nsp(w[0])), 1)
        w[1], w[2] = cur, cur + rate * v
        cur = w[2]
    return side


def words_to_segments(grp: list[list]) -> list[Segment]:
    """词 -> 逐字 Segment（空格挂到前一个字符的显示文本上）。"""
    segs: list[Segment] = []
    pending = ""
    for w in grp:
        t, st, en = w[0], w[1], w[2]
        pb = float(w[3]) if len(w) > 3 else 1.0
        vis = _nsp(t)
        n = len(vis)
        if not n:
            continue
        span = max(en - st, 0.02)
        m = 0
        for ch in t:
            if ch.isspace():
                pending += ch
                continue
            a = st + span * m / n
            b = st + span * (m + 1) / n
            if segs:
                segs[-1].text += pending
            pending = ""
            segs.append(Segment(ch, a, max(b, a + 0.03), pb))
            m += 1
    return segs


def enforce_timing(lines: list[AsrLine], min_dur: float = MIN_LINE_DUR,
                   char_rate: float = MIN_CHAR_RATE) -> list[int]:
    """前向扫描：行不重叠，且每行至少 ``max(min_dur, char_rate*字数)`` 秒。

    返回被拉到最短时长的行号 —— 这些行多半是 whisper 锚定失败被压缩的，
    需要人工复核。
    """
    prev_end = 0.0
    clamped: list[int] = []
    for i, r in enumerate(lines):
        if r.start < prev_end + 0.02:
            d = prev_end + 0.02 - r.start
            r.start += d
            r.end = max(r.end + d, r.start)
            for s in r.segments:
                s.start += d
                s.end += d
        need = max(min_dur, char_rate * max(len(_nsp(r.text)), 1))
        if r.end < r.start + need:
            grow = r.start + need - r.end
            r.end += grow
            for s in r.segments:
                if s.end >= r.end - grow - 1e-6:
                    s.end += grow
            clamped.append(i)
        prev_end = r.end
    return clamped


# ==========================================================================
# 人声能量引导（可选）：把「人声什么时候在唱」当硬约束
# ==========================================================================

def vocal_intervals(voc_path: str | Path, thr_factor: float = 0.12,
                    min_gap_s: float = 0.6, min_len_s: float = 0.8,
                    rel_drop_db: float = 6.0,
                    short_s: float = 4.0) -> list[tuple[float, float]]:
    """从分离出的人声 stem 算「有人在唱」的区间（秒）。

    阈值用峰值的比例而不是中位数：现场人声 stem 混着观众噪声，
    中位数会被抬高导致漏检。min_gap_s 内的静音并入、短于 min_len_s 的活跃视为噪声。

    **相对能量剔除（``rel_drop_db`` / ``short_s``，recent validation 新增）**：峰值比例阈值
    351.4–353.7s 是观众在吼，但它在干声里仍有 -28.5dB（阈值 -35dB），被当成"有人在唱"
    判据：**段内中位电平比全曲活跃帧中位电平低 ``rel_drop_db`` 以上、且段长 < ``short_s``**
    → 剔除。实测该段 -7.2dB / 2.3s，真唱段全在 ±1.2dB 内；加"短"这一条是为了
    不误伤「安静的段落」（整段偏轻但很长，那是真唱）。
    """
    import numpy as np
    import soundfile as sf

    x, sr = sf.read(str(voc_path), always_2d=True)
    x = x.mean(axis=1)
    hop = int(0.05 * sr)
    n = len(x) // hop
    env = np.array([float(np.sqrt(np.mean(x[i * hop:(i + 1) * hop] ** 2))) for i in range(n)])
    thr = float(env.max()) * thr_factor
    act = env > thr

    def smooth(a: np.ndarray, max_gap: int, min_len: int) -> np.ndarray:
        a = a.copy()
        i = 0
        while i < len(a):
            if not a[i]:
                j = i
                while j < len(a) and not a[j]:
                    j += 1
                if 0 < i < len(a) and (j - i) <= max_gap:
                    a[i:j] = True
                i = j
            else:
                i += 1
        i = 0
        while i < len(a):
            if a[i]:
                j = i
                while j < len(a) and a[j]:
                    j += 1
                if (j - i) < min_len:
                    a[i:j] = False
                i = j
            else:
                i += 1
        return a

    act = smooth(act, int(min_gap_s / 0.05), int(min_len_s / 0.05))
    out: list[tuple[float, float]] = []
    i = 0
    while i < len(act):
        if act[i]:
            j = i
            while j < len(act) and act[j]:
                j += 1
            out.append((i * 0.05, j * 0.05))
            i = j
        else:
            i += 1

    if rel_drop_db > 0 and out:
        db = 20 * np.log10(np.maximum(env, 1e-9))
        act_db = db[act]
        if act_db.size:
            med = float(np.median(act_db))
            keep: list[tuple[float, float]] = []
            for a, b in out:
                if (b - a) >= short_s:          # 长段视为真唱（可能是安静的段落）
                    keep.append((a, b))
                    continue
                seg = db[int(a / 0.05):int(b / 0.05)]
                seg = seg[seg > 20 * np.log10(thr)]
                if not seg.size or float(np.median(seg)) - med >= -abs(rel_drop_db):
                    keep.append((a, b))
            out = keep
    return out


def _active_at(intervals: list[tuple[float, float]], t: float) -> bool:
    return any(a <= t < b for a, b in intervals)


# —— 转写交叉验证（recent validation）：能量包络只懂"有人在出声"，不懂"出的是不是歌词"。
#    自由转写是系统里唯一能同时说"这里有像词的声音"和"它是什么"的层。 ——
def _norm_tx(s: str) -> str:
    return re.sub(r"[^\w]+", "", s, flags=re.UNICODE).lower()


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else set()


def transcribe_check(voc_path: str | Path, lyric_lines: list[str],
                     language: str = "Japanese", model=None,
                     device: str = "cuda", model_size: str = "large-v3",
                     min_match: float = 0.34) -> dict:
    """干声**自由转写**（非强对齐），把转写段分成「在唱歌词 / 非歌词人声」两组。

    匹配判据：转写段与歌词行（含相邻两行拼接）的 **bigram 重叠系数**
    ``|∩| / min(|A|,|B|)`` ≥ ``min_match``。不用字符集/包含匹配——日语汉字与假名
    不一致（激しく揺れる vs 激しくゆらめいた）、英语 hook 共享字母太多，都会误判；
    bigram 重叠对两种语言都稳（实测：激しく揺れる→0.40 ✓，"We can't"→0.20 ✗）。

    用途：``other``（转写出来但与歌词无关的发声段 = 吼叫/即兴/和声/幻觉）
    90–102s 转写 "We can't / I'm not lying to you"（p 最高 1.00，与歌词不符）
    102–109s 吼叫段**转写为空**（空≠非人声，所以只能剪"有转写且不匹配"的部分）
    165–171s 幻觉「ご視聴ありがとうございました」→ 与歌词不匹配 → 被剪 ✓
    """
    from whisper_compat import patch_whisper_triton

    patch_whisper_triton()          # Windows 上 triton 崩溃 → 强制走 CPU 回退
    model = model or get_model(model_size, device)
    res = model.transcribe(str(voc_path), language=language, word_timestamps=False)

    norm_lines = [_norm_tx(l) for l in (lyric_lines or [])]
    refs = [l for l in norm_lines if len(l) >= 2]
    refs += [norm_lines[i] + norm_lines[i + 1]
             for i in range(len(norm_lines) - 1)
             if len(norm_lines[i]) >= 2 and len(norm_lines[i + 1]) >= 2]
    ref_bi = [_bigrams(r) for r in refs]

    lyric_rng: list[tuple[float, float]] = []
    other_rng: list[tuple[float, float]] = []
    for s in res.get("segments", []):
        a, b = float(s.get("start", 0) or 0), float(s.get("end", 0) or 0)
        t = _norm_tx(s.get("text") or "")
        if not t or b - a < 0.3:
            continue
        bi = _bigrams(t)
        best = 0.0
        for rb in ref_bi:
            if not bi or not rb:
                continue
            inter = len(bi & rb)
            if inter:
                best = max(best, inter / min(len(bi), len(rb)))
        (lyric_rng if best >= min_match else other_rng).append((a, b))
    return {"lyric": lyric_rng, "other": other_rng,
            "n_seg": len(lyric_rng) + len(other_rng)}


def subtract_ranges(intervals: list[tuple[float, float]],
                    cuts: list[tuple[float, float]],
                    min_overlap_s: float = 1.0) -> list[tuple[float, float]]:
    """从包络区间里减掉 ``cuts``（交叠 < ``min_overlap_s`` 的不动，避免误伤）。"""
    out: list[tuple[float, float]] = []
    for a, b in intervals or []:
        cur = [(a, b)]
        for ca, cb in cuts or []:
            nxt = []
            for x, y in cur:
                if cb <= x or ca >= y or min(y, cb) - max(x, ca) < min_overlap_s:
                    nxt.append((x, y))
                    continue
                if ca > x:
                    nxt.append((x, min(y, ca)))
                if cb < y:
                    nxt.append((max(x, cb), y))
            cur = [(x, y) for x, y in nxt if y > x]
        out.extend(cur)
    return [(x, y) for x, y in out if y - x >= 0.3]


def prune_intervals_by_tx(intervals: list[tuple[float, float]],
                          other: list[tuple[float, float]],
                          min_overlap_s: float = 1.0,
                          max_seg_s: float = 8.0,
                          cap_frac: float = 0.10) -> tuple[list[tuple[float, float]], str]:
    """把「非歌词发声段」从包络里剪掉 —— 带**双重保险丝**。

    - 只剪**短**（< ``max_seg_s``）的不匹配段：转写把多句唱词合并成一段长转写很常见，
      长段"与歌词不匹配"多半是转写质量问题（漏词/串词），不是真没人唱歌词；
    - 候选剪除总量超过包络总长的 ``cap_frac``（默认 10%）→ **整体放弃**。

    教训（一次未启用保险丝的运行）：一次转写只出 51 段、17 段不匹配
    （其中长段多），直接把包络从 200.6s 剪到 74.4s → 包络大面积塌陷，
    尾部 8 行歌词又被 drop_beyond_vocals 整行丢掉。转写逐次波动很大，
    没有保险丝的剪除等于把时间轴交给骰子。
    """
    if not other or not intervals:
        return list(intervals or []), "无非歌词段"
    total_env = sum(b - a for a, b in intervals)
    cuts = [c for c in other if (c[1] - c[0]) <= max_seg_s]
    skipped_long = len(other) - len(cuts)
    if not cuts:
        return list(intervals), f"跳过：不匹配段全部 >{max_seg_s}s（{skipped_long} 段）"
    cand = sum(min(b, cb) - max(a, ca)
               for a, b in intervals for ca, cb in cuts
               if min(b, cb) - max(a, ca) > 0)
    if cand > total_env * cap_frac:
        return (list(intervals),
                f"跳过：候选剪除 {cand:.1f}s 超过包络 {total_env:.1f}s 的 "
                f"{cap_frac:.0%}（保险丝）")
    pruned = subtract_ranges(intervals, cuts, min_overlap_s=min_overlap_s)
    note = (f"剪除 {total_env - sum(b - a for a, b in pruned):.1f}s"
            f"（{len(cuts)} 段，长段跳过 {skipped_long}）")
    return pruned, note


def collapse_zeroconf(lines: list[AsrLine], rules: dict | None = None) -> dict:
    """零证据行收缩：行内词平均概率 ≈ 0 → 收缩成最短窗口，不做匀速铺开。

    （'I' 2.86s + 四个词挤成 0.02s），经规则层匀速铺开成 4.08s 滚过 Toshi 的吼叫。
    p≈0 = 对齐器明说"没有证据"，铺开就是猜；收缩成 ≤0.6s 的闪现，
    把"要不要/放哪里"留给人工微调。
    """
    R = _norm_rules(rules)
    rep: dict = {"collapsed": []}
    if not R.get("zeroconf_collapse", True):
        return rep
    p0 = float(R.get("zeroconf_p") or 0.10)
    for i, ln in enumerate(lines):
        if ln.prob < p0 and (ln.end - ln.start) > 0.6:
            _respan(ln, ln.start, ln.start + 0.6)
            rep["collapsed"].append(i)
    return rep


def _respan(line: AsrLine, s: float, e: float) -> None:
    """把一行整体重铺到 [s, e]（字符按原相对位置线性映射）。"""
    old_s, old_e = line.start, line.end
    od = max(old_e - old_s, 1e-6)
    nd = max(e - s, 0.05)
    for seg in line.segments:
        r1 = (seg.start - old_s) / od
        r2 = (seg.end - old_s) / od
        seg.start = s + nd * r1
        seg.end = s + nd * r2
        if seg.end <= seg.start:
            seg.end = seg.start + 0.03
    line.start, line.end = s, e


def _extend_tail(line: AsrLine, new_end: float) -> None:
    """行尾延长到 new_end：额外时长全部给最后一个字（拖音正是尾音在唱）。"""
    if new_end <= line.end + 0.05:
        return
    line.end = new_end
    if line.segments:
        line.segments[-1].end = max(new_end, line.segments[-1].end)


def vocal_guide(lines: list[AsrLine], intervals: list[tuple[float, float]],
                rules: dict | None = None) -> dict:
    """人声能量引导（结构自检后的位置修正）：

    a) **尾部越界**：行尾越过最后一段人声 → 收回到 last_end。
    b) **起点落在人声静音区**：行起点inactive、且行内存在更晚的人声起点

    拖音补偿与低置信行重定位独立为 :func:`extend_tails` / :func:`relocate_lowconf`，
    编排顺序：vocal_guide → relocate_lowconf → extend_tails → enforce_timing
    （重定位先行，拖音延长才不会被低置信行的假起点封顶）。
    """
    R = _norm_rules(rules)
    rep: dict = {"tail_clamped": [], "start_snapped": [], "beyond_vocals": [],
                 "tail_extended": [], "beyond_dropped": []}
    if not intervals:
        return rep
    last_end = intervals[-1][1]
    ons = [a for a, _ in intervals]
    drop: list[int] = []
    for i, ln in enumerate(lines):
        s, e = ln.start, ln.end
        if R["tail_clamp"] and s >= last_end - 0.2:
            rep["beyond_vocals"].append(i)
            drop.append(i)
            continue
        if R["tail_clamp"] and e > last_end + 0.2:
            _respan(ln, s, last_end)
            rep["tail_clamped"].append(i)
            s, e = ln.start, ln.end
        if R["start_snap"] and not _active_at(intervals, s):
            nxt = next((a for a in ons if s + 0.2 < a < e - 0.3), None)
            if nxt is not None:
                _respan(ln, nxt, e)
                rep["start_snapped"].append(i)
    # 「最后一行之后还有人声就回收」的姊妹规则：起点已经落在最后一段人声**之后**
    # 关掉它则保留这些行（旧行为，仅标记 beyond_vocals 交人工裁决）。
    if R.get("drop_beyond_vocals", True) and drop:
        for i in reversed(drop):
            del lines[i]
        rep["beyond_dropped"] = drop
    return rep


def relocate_lowconf(lines: list[AsrLine], intervals: list[tuple[float, float]],
                     rules: dict | None = None) -> dict:
    """低置信行重定位：整句被 whisper「猜」到错误位置的行，搬进未认领的人声区间。

    判据（recent validation 双案例实测设计）：
    - 行平均词概率 ≥ conf_high → 高置信，位置可信，并认领其所在人声区间的
      「行尾之后部分」（拖音归属，供 extend_tails 使用）；
    - < conf_low → 低置信。此时看行内有无「证据词」（p≥ev_prob 且时长≥ev_dur）：
      * 有证据词 → 位置部分可信：锚定到最后一个证据词的行尾（start 按字速回推）；
      * 无证据词 → 整句是猜的：在「前一行末尾 ~ 后续锚点」窗口内，把行放进
        步进式第一个装得下的未认领区间；都装不下则压缩进窗口内最后一个槽位。

    `sample line`@240.08（全词 p≤0.01，压在上一句拖音里）→ 246.8 槽位；
    `a lyric line`（尾词 'on' p=0.79/1.14s）→ 锚定到 ≈283.2-288.2
    """
    R = _norm_rules(rules)
    rep: dict = {"relocated": [], "evidence_anchored": [], "no_slot": []}
    if not intervals or not R["relocate_lowconf"] or not lines:
        return rep

    rate = max(0.18, R["char_rate_min"])

    def claim_end(ln: AsrLine) -> float:
        """行所在人声区间的结束（拖音归属），找不到所在区间则用行尾。"""
        for a, b in intervals:
            if a - 0.05 <= ln.start < b:
                return b
        return ln.end

    claimed: list[list[float]] = []
    for ln in lines:
        if ln.prob >= R["conf_high"]:
            claimed.append([ln.start, claim_end(ln)])

    def unclaimed(lo: float, hi: float) -> list[tuple[float, float]]:
        segs: list[tuple[float, float]] = []
        for a, b in intervals:
            a2, b2 = max(a, lo), min(b, hi)
            if b2 - a2 <= 0.1:
                continue
            cur = a2
            for ca, cb in sorted(claimed):
                ca2, cb2 = max(ca, a2), min(cb, b2)
                if cb2 <= cur or ca2 >= b2:
                    continue
                if ca2 > cur:
                    segs.append((cur, ca2))
                cur = max(cur, cb2)
            if cur < b2 - 0.1:
                segs.append((cur, b2))
        merged: list[tuple[float, float]] = []
        for a, b in segs:
            if merged and a - merged[-1][1] < 0.05:
                merged[-1] = (merged[-1][0], b)
            else:
                merged.append((a, b))
        return merged

    for i, ln in enumerate(lines):
        if ln.prob >= R["conf_low"]:
            continue  # 高/中置信不动
        prev_end = lines[i - 1].end if i else 0.0
        nxt = None
        for j in range(i + 1, len(lines)):
            if lines[j].prob >= R["conf_low"]:
                nxt = lines[j].start
                break
            if _has_evidence(lines[j], R):
                # 低置信但有证据：其 line.start 本身是猜的，用第一个证据词的起点
                ev = min(s for s, p, e in _iter_words(lines[j])
                         if p >= R["ev_prob"] and (e - s) >= R["ev_dur"])
                nxt = ev
                break
        hi = nxt if nxt is not None else intervals[-1][1] + 60.0

        if _has_evidence(ln, R):
            # 有证据词：先看位置是否合理（落在人声活跃区间内且行时长不超标）
            # —— 合理则保留（实测 L20/L24："sample repeated lyric line"@87.7/
            # "sample line"@100.2 本来就唱对了，乱锚反而变差）；
            # 仅当行时长严重超标（whisper 把词摊进间奏）才锚定到证据词尾。
            reasonable = (_active_at(intervals, ln.start)
                          and ln.duration <= rate * _vis_len(ln) * 1.8)
            if reasonable:
                claimed.append([ln.start, claim_end(ln)])
                continue
            ev_end = max(e for s, p, e in _iter_words(ln)
                         if p >= R["ev_prob"] and (e - s) >= R["ev_dur"])
            new_start = max(prev_end + 0.02, ev_end - rate * _vis_len(ln))
            if ev_end - new_start > 0.3:
                _respan(ln, new_start, ev_end)
                rep["evidence_anchored"].append(i)
            claimed.append([ln.start, ln.end])
            continue

        need = max(ln.duration, rate * _vis_len(ln), 0.5)
        slots = unclaimed(prev_end + 0.02, hi)
        if not slots:
            rep["no_slot"].append(i)
            continue
        fit = next(((a, b) for a, b in slots if b - a >= need - 0.05), None)
        if fit is None:
            fit = slots[-1]   # 都装不下 → 压缩进最靠近后续锚点的槽位
        a, b = fit
        new_end = b if b - a < need else a + need
        _respan(ln, a, max(a + 0.5, new_end))
        claimed.append([a, b])   # 整槽认领：后续低置信行去下一个槽位
        rep["relocated"].append(i)
    return rep


def extend_tails(lines: list[AsrLine], intervals: list[tuple[float, float]],
                 rules: dict | None = None) -> dict:
    """拖音补偿：行尾之后人声还在唱（1.2s 内接上活跃区间）→ 行尾延到该区间结束，
    额外时长全给最后一个字（拖音正是尾音在唱）。

    音素发声结束，不含拖住的元音。必须在 relocate_lowconf 之后跑
    （否则会被低置信行的假起点封顶，早期测试中拖音未修成）。

    ⚠ 准入条件（``extend_min_p``，recent validation 新增）：行内**词级平均概率**低于
    demucs 出血（伴奏漏进人声 stem）造成的假活跃区间里，被延长 5.1s 全压在
    最后一个字上 → 画面上「明明没人唱，字已经全亮还挂了 5 秒」。
    没有声学证据的延长就是猜，不如不猜。
    """
    R = _norm_rules(rules)
    rep: dict = {"tail_extended": [], "tail_skipped_lowconf": []}
    if not intervals or not R["tail_extend"]:
        return rep
    for i, ln in enumerate(lines):
        if ln.prob < float(R.get("extend_min_p") or 0):
            rep["tail_skipped_lowconf"].append(i)
            continue
        e = ln.end
        nxt_start = lines[i + 1].start if i + 1 < len(lines) else float("inf")
        run = next(((a, b) for a, b in intervals if b > e + 0.05 and a <= e + 1.2), None)
        if run:
            new_end = min(run[1], nxt_start - 0.02, e + R["tail_gain"])
            if new_end > e + 0.3:
                _extend_tail(ln, new_end)
                rep["tail_extended"].append(i)
    return rep


def cap_char_durations(lines: list[AsrLine],
                       rules: dict | None = None) -> dict:
    """单字时长上限（``char_dur_max``，默认 2.0s）——最后一道物理约束。

    为什么需要：卡拉OK 的字幕时长是由**逐字**时长决定的。任何一条把「行尾
    延到某处」的规则（extend_tails / enforce_timing 的最短时长）都会把多出来的
    时间塞给最后一个字，一个字占 5 秒在画面上就是「无人声还在滚/还挂着」。

    只在**字时长**层收口，不碰行起点、不做反向的「最短可见时长」
    （后者会制造重叠，见 karaoke-ass-subtitles skill）。超出部分顺延给后面的字，
    行尾自然缩短；跨行单调性交给随后的 enforce_timing。
    """
    R = _norm_rules(rules)
    rep: dict = {"char_capped": []}
    cap = float(R.get("char_dur_max") or 0)
    if cap <= 0:
        return rep
    for i, ln in enumerate(lines):
        if not ln.segments:
            continue
        changed = False
        for k, seg in enumerate(ln.segments):
            if seg.end - seg.start <= cap + 1e-6:
                continue
            seg.end = seg.start + cap
            changed = True
            for nxt in ln.segments[k + 1:]:     # 后面的字顺延，保持行内单调
                if nxt.start < seg.end:
                    shift = seg.end - nxt.start
                    nxt.start += shift
                    nxt.end += shift
        if changed:
            ln.start = min(s.start for s in ln.segments)
            ln.end = max(s.end for s in ln.segments)
            rep["char_capped"].append(i)
    return rep


def merge_lines_by_confidence(primary: list[AsrLine], primary_rows: list[int],
                              alt: list[AsrLine], alt_rows: list[int],
                              n_rows: int) -> tuple[list[AsrLine], dict]:
    """双路取优：同一份歌词、两套对齐（如「人声干声」与「原始混音」）逐行取优。

    判据用 ``AsrLine.prob``（该行词级平均概率，见 :func:`build_lines`）——
    取概率**严格更高**的一路，打平保留 primary（避免无意义抖动）。

    逐行取优后 **0.625**（+0.022）；64 行里 16 行取自混音、29 行取自干声、
    19 行打平。代价是两路各跑一次对齐（+25~30s）。

    ``primary_rows`` / ``alt_rows`` 由 ``build_lines`` 的 ``diag["row_index"]`` 给出，
    保证按**歌词行号**对齐（两路的行数可能不同，不能按下标硬配）。
    """
    pa = dict(zip(primary_rows, primary))
    pb = dict(zip(alt_rows, alt))
    out: list[AsrLine] = []
    from_alt: list[int] = []
    for i in range(n_rows):
        a, b = pa.get(i), pb.get(i)
        if a is not None and b is not None:
            if b.prob > a.prob:
                out.append(b)
                from_alt.append(i)
            else:
                out.append(a)
        elif a is not None:
            out.append(a)
        elif b is not None:
            out.append(b)
            from_alt.append(i)
    return out, {"n_alt": len(from_alt), "alt_rows": from_alt,
                 "p_primary": round(statistics.mean(x.prob for x in primary), 4)
                 if primary else 0.0,
                 "p_merged": round(statistics.mean(x.prob for x in out), 4)
                 if out else 0.0}


def merge_lines_monotonic(primary, primary_rows, alt, alt_rows, n_rows):
    """Choose a globally coherent route before considering confidence.

    Independent per-line confidence selection can jump backwards between two
    repeated choruses. This two-state dynamic program minimizes reversals and
    overlap first, then maximizes confidence; no song-specific thresholds.
    """
    pa, pb = dict(zip(primary_rows, primary)), dict(zip(alt_rows, alt))
    candidates = [(i, [(source, line) for source, line in
                       ((0, pa.get(i)), (1, pb.get(i))) if line is not None])
                  for i in range(n_rows) if i in pa or i in pb]
    layers = []
    for position, (_, options) in enumerate(candidates):
        layer = []
        for source, line in options:
            if not layers:
                layer.append(((0, 0.0, -line.prob), None))
                continue
            choices = []
            previous = candidates[position - 1][1]
            for j, (_, prev) in enumerate(previous):
                cost = layers[-1][j][0]
                choices.append(((cost[0] + int(line.start < prev.start),
                                 cost[1] + max(0, prev.end - line.start),
                                 cost[2] - line.prob), j))
            layer.append(min(choices, key=lambda value: value[0]))
        layers.append(layer)
    if not layers:
        return [], {"mode": "monotonic", "n_alt": 0, "row_index": [], "alt_rows": []}
    selected = min(range(len(layers[-1])), key=lambda j: layers[-1][j][0])
    final_cost = layers[-1][selected][0]
    chosen = []
    for i in reversed(range(len(layers))):
        row, options = candidates[i]
        source, line = options[selected]
        chosen.append((row, source, line))
        selected = layers[i][selected][1]
    chosen.reverse()
    return [line for _, _, line in chosen], {
        "mode": "monotonic", "n_alt": sum(source for _, source, _ in chosen),
        "row_index": [row for row, _, _ in chosen],
        "alt_rows": [row for row, source, _ in chosen if source],
        "unavoidable_reversals": final_cost[0], "unavoidable_overlap_s": round(final_cost[1], 3),
    }


def clamp_tails(lines: list[AsrLine], intervals: list[tuple[float, float]],
                rules: dict | None = None) -> dict:
    """逐行尾部收回（最后一道清扫，在 extend_tails / enforce_timing 之后）：

    **判据**：行尾落在静音里（不属于任何活跃区间）→ 收回到「行尾之前最后一个
    活跃区间的结束」。

    歌词还在滚。只按「行尾是否在静音」判定，避免误伤「行尾落在下一句活跃区间
    内」的正当延长（如 'a lyric line' 行尾 288.28 落在 285.4-309.25 ✓
    不能收回——早期按「所属区间」判定把它切成 0.4s 闪行，是错的）。
    """
    R = _norm_rules(rules)
    rep: dict = {"tail_clamped_per_line": []}
    if not intervals or not R.get("clamp_tails_per_line", True):
        return rep
    for i, ln in enumerate(lines):
        if _active_at(intervals, min(ln.end, intervals[-1][1] + 0.01)):
            continue                       # 行尾仍有人声 → 保留
        # 行尾在静音里：收到「行尾之前最后一个活跃区间」的结束
        prev_end = max((b for a, b in intervals if a < ln.end - 0.05),
                       default=ln.start)
        if prev_end > ln.start + 0.2 and ln.end > prev_end + 0.3:
            _respan(ln, ln.start, prev_end)
            rep["tail_clamped_per_line"].append(i)
    return rep


def build_lines(words: list[list], lines: list[str],
                rules: dict | None = None) -> tuple[list[AsrLine], dict]:
    """词级时间戳 -> 逐字 AsrLine 列表（含全部后处理，规则见 ``DEFAULT_RULES``）。"""
    R = _norm_rules(rules)
    groups, covered = group_words_to_lines(words, lines)
    rate = estimate_rate(words)
    out: list[AsrLine] = []
    rows_out: list[int] = []      # out[k] 对应的歌词行号（双路取优按行合并要用）
    reflowed = overlong = 0
    suspect: list[int] = []
    for i, (txt, grp) in enumerate(zip(lines, groups)):
        if not grp:
            continue
        rows_out.append(i)
        # 句尾一个字甩到十几秒外、前几个字的位置是对的——此时内部大断档就是证据，
        # 应锚起点重排；而 fix_over_long 的平均概率会把「只有尾巴错」误判成「整句错」
        # （首尾概率一平均恰好卡在阈值附近）。
        # fix_over_long 只兜底「没有内部断档、但整行被拉长」的连续型错锚。
        if R["reflow_gapped"] and reflow_if_gapped(grp, rate, R["gap_max"]):
            reflowed += 1
        elif R["fix_over_long"] and fix_over_long(grp, rate):
            overlong += 1
        segs = words_to_segments(grp)
        if not segs:
            continue
        p_avg = statistics.mean(w[3] for w in grp) if grp else 1.0
        out.append(AsrLine(txt, min(s.start for s in segs),
                           max(s.end for s in segs), segs, p_avg))
        if grp and p_avg < SUSPECT_PROB:
            suspect.append(i)
    clamped = (enforce_timing(out, min_dur=R["min_line_dur"],
                              char_rate=R["char_rate_min"])
               if R["enforce_timing"] else [])
    diag = {"words": len(words), "lines": len(out), "covered_words": covered,
            "rate_s_per_char": round(rate, 4), "reflowed_lines": reflowed,
            "overlong_fixed_lines": overlong, "clamped_lines": clamped,
            "suspect_lines": suspect, "rules": R, "row_index": rows_out}
    return out, diag


# ==========================================================================
# whisper 对齐
# ==========================================================================

_MODEL_CACHE: dict = {}
_MODEL_LOCK = threading.Lock()      # WebUI 允许并发任务：加载必须串行（recent validation）


def get_model(model_size: str = "large-v3", device: str = "cuda"):
    r"""加载（并缓存）whisper 模型。WebUI 常驻进程用它避免每个任务重载 ~3GB 权重。

    权重统一放项目 models\whisper\（model_paths 设定），随项目文件夹迁移。
    """
    from model_paths import WHISPER_DIR

    key = (model_size, device)
    with _MODEL_LOCK:          # 并发任务会各自 load 一次（显存翻倍 + 加载期偶发崩溃）
        if key not in _MODEL_CACHE:
            import whisper

            WHISPER_DIR.mkdir(parents=True, exist_ok=True)
            _MODEL_CACHE[key] = whisper.load_model(
                model_size, device=device, download_root=str(WHISPER_DIR))
    return _MODEL_CACHE[key]


def align_words(audio: str | Path, text: str, language: str = "Japanese",
                model_size: str = "large-v3", device: str = "cuda",
                model=None, progress=None) -> list[list]:
    """跑 stable-ts 对齐，返回 ``[[word, start, end, probability], ...]``。

    ``progress(frac, msg)``：粗粒度回调（0=开始加载模型，0.45=模型就绪，
    0.55=开始对齐，1=对齐完成），供 WebUI 的进度条使用。
    """
    from whisper_compat import patch_whisper_triton

    patched = patch_whisper_triton()
    if patched:
        print(f"[compat] 已包装 triton 入口: {patched}", flush=True)

    def say(f: float, m: str) -> None:
        if progress:
            progress(f, m)

    say(0.0, f"加载 whisper {model_size}…")
    model = model or get_model(model_size, device)
    say(0.45, "模型就绪，开始对齐…")

    from stable_whisper.alignment import align as st_align

    t0 = time.time()
    res = st_align(model=model, audio=str(audio), text=text, language=language)
    print(f"[align] 耗时 {time.time() - t0:.1f}s", flush=True)
    say(1.0, f"对齐完成（{time.time() - t0:.1f}s）")

    words: list[list] = []
    for s in res.segments:
        for w in (s.words or []):
            t = (getattr(w, "word", "") or "")
            if not t.strip():
                continue
            st, en = float(getattr(w, "start", 0.0)), float(getattr(w, "end", 0.0))
            pb = float(getattr(w, "probability", 1.0) or 0.0)
            words.append([t, st, max(en, st + 0.02), pb])
    return words


def main() -> int:
    ap = argparse.ArgumentParser(description="已知歌词文本 -> 逐字时间轴（whisper 对齐）")
    ap.add_argument("--audio", help="音频（wav）；与 --media 二选一")
    ap.add_argument("--media", help="视频/音频，自动抽成 44.1k 立体声 wav")
    ap.add_argument("--lyrics", required=True, help="歌词文本（.txt，纯文本按行）")
    ap.add_argument("--lang", default="Japanese", help="whisper 语言名（Japanese/English/Chinese）")
    ap.add_argument("--model", default="large-v3", help="whisper 模型（~/.cache/whisper 下的名字）")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--out", required=True, help="增强 LRC 输出路径（同时产出 .plain.txt / .srt / .json）")
    ap.add_argument("--vocals", help="分离人声 stem（wav）；给了就启用人声能量引导，"
                                     "把「人声什么时候在唱」当硬约束")
    ap.add_argument("--profile", choices=["balanced", "automatic", "legacy"], default="balanced")
    ap.add_argument("--rules", help="规则 JSON（键见 DEFAULT_RULES，如 "
                                    "'{\"tail_extend\": false, \"gap_max\": 2.5}'）")
    args = ap.parse_args()

    lines = [l.strip() for l in Path(args.lyrics).read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if not lines:
        print("歌词为空", flush=True)
        return 1
    text = "\n".join(lines)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.audio:
        audio = str(Path(args.audio).resolve())
    else:
        from pipeline import extract_wav

        audio = str(out_path.parent / (out_path.stem + "__44k.wav"))
        extract_wav(args.media, audio, sr=44100, mono=False)
    print(f"[input] 音频 {audio}  歌词 {len(lines)} 行", flush=True)

    words = align_words(audio, text, language=args.lang,
                        model_size=args.model, device=args.device)
    out_path.with_suffix(".json").write_text(
        json.dumps({"audio": audio, "lyrics": args.lyrics, "lang": args.lang,
                    "model": args.model, "words": words}, ensure_ascii=False),
        encoding="utf-8")

    user_rules: dict | None = None
    if getattr(args, "rules", None):
        try:
            user_rules = json.loads(args.rules)
        except Exception as e:  # noqa: BLE001
            print(f"[rules] 解析失败，忽略: {e}", flush=True)

    from alignment_policy import resolve_rules
    user_rules = resolve_rules(DEFAULT_RULES, user_rules, args.profile)
    asr_lines, diag = build_lines(words, lines, rules=user_rules)
    print(f"[post] {diag}", flush=True)

    iv = []
    if args.vocals:
        try:
            iv = vocal_intervals(args.vocals,
                                 thr_factor=float((user_rules or {}).get("vocal_thr", 0.12)),
                                 rel_drop_db=float((user_rules or {}).get("rel_drop_db", 6.0)))
            if (user_rules or {}).get("tx_crosscheck", True):
                tx = transcribe_check(args.vocals, lines, language=args.lang,
                                      device=args.device, model_size=args.model,
                                      min_match=float((user_rules or {})
                                                      .get("tx_min_match", 0.34)))
                iv, tx_note = prune_intervals_by_tx(
                    iv, tx["other"],
                    min_overlap_s=float((user_rules or {}).get("tx_prune_min", 1.0)),
                    max_seg_s=float((user_rules or {}).get("tx_max_seg", 8.0)),
                    cap_frac=float((user_rules or {}).get("tx_cap_frac", 0.10)))
                print(f"[tx-check] 转写 {tx['n_seg']} 段：唱歌词 {len(tx['lyric'])} / "
                      f"非歌词 {len(tx['other'])}｜{tx_note}", flush=True)
            sung = sum(b - a for a, b in iv)
            print(f"[vocal-guide] 人声活跃 {len(iv)} 段 / {sung:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[vocal-guide] 失败，跳过: {type(e).__name__}: {e}", flush=True)
    else:
        print("[vocal-guide] 未提供 --vocals，跳过人声能量引导", flush=True)

    diag["postprocess"] = postprocess_lines(asr_lines, iv, user_rules, args.profile)
    out_path.with_suffix(".diagnostics.json").write_text(json.dumps(diag, ensure_ascii=False, indent=2), encoding="utf-8")
    out_path.write_text(to_enhanced_lrc(asr_lines), encoding="utf-8")
    out_path.with_suffix(".plain.txt").write_text(to_plain(asr_lines), encoding="utf-8")
    out_path.with_suffix(".srt").write_text(to_srt(asr_lines), encoding="utf-8")
    print(f"[done] {out_path}", flush=True)
    for r in asr_lines:
        print(f"  {r.start:7.2f} ~ {r.end:7.2f}  {r.text[:40]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
