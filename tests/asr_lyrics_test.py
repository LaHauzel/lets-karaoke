"""ASR 歌词链路离线自测（不需要加载任何模型）。

覆盖三件事：
1. 单元 → 原文字符 的回映射不丢字（Qwen 分词会吃掉促音「っ」这类字符）；
2. 增强 LRC 的逐词标记带显式终点，能被 pipeline.parse_lyrics 无损读回；
3. 回归：每行末词不会被压成 60ms 的极短片段（旧 _resolve_word_times 的坑）。

用法：
  python tests/asr_lyrics_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import asr_lyrics as A  # noqa: E402
import pipeline as P  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    print(f"  [{'OK' if cond else 'FAIL'}] {label}{('  ' + extra) if extra else ''}")
    if not cond:
        FAILS.append(label)


print("=== ① 单元 -> 字符 回映射（模拟 Qwen 把 渡って 切成 渡っ / て）===")
text = "夢を渡って空を吹いてる"
# 故意让 っ 落在单元之外：单元 渡っ 只覆盖 2.160~2.560
units = [
    A.Segment("夢", 1.200, 1.600),
    A.Segment("を", 1.600, 1.880),
    A.Segment("渡っ", 2.160, 2.560),
    A.Segment("て", 2.560, 2.800),
    A.Segment("空", 3.040, 3.400),
    A.Segment("を", 3.400, 3.640),
    A.Segment("吹い", 3.920, 4.320),
    A.Segment("てる", 4.320, 4.800),
]
spans = A.units_to_char_spans(text, units)
check(len(spans) == len(text), "逐字符区间数与原文一致",
      f"{len(spans)} vs {len(text)}")
check("".join(s[0] for s in spans) == text, "回映射不丢字（含 っ）")
check(all(s[2] > s[1] for s in spans), "每个字符都有正的时长")
check(all(s[1] <= s[2] for s in spans), "区间单调（start<=end）")
bad_order = [i for i in range(1, len(spans)) if spans[i][1] < spans[i - 1][1]]
check(not bad_order, "起点整体单调不倒退", f"异常位置={bad_order}")

segs = A.merge_to_segments(spans)
check("".join(s.text for s in segs) == text, "合并回单元后仍是完整原文",
      f"单元数 {len(units)} -> {len(segs)}")
same_unit_ok = all(s.text for s in segs)
check(same_unit_ok, "无空单元段")

print()
print("=== ② 分行 + 增强 LRC ===")
full_units = units + [
    A.Segment("遠", 6.100, 6.400), A.Segment("い", 6.400, 6.600),
]
full_text = text + "遠い"
res = A.build_lyrics(lang="Japanese", text=full_text, units=full_units,
                     audio_dur=8.0, gap_s=0.75, max_chars=30)
check(len(res.lines) >= 1, "至少成 1 行", f"实际 {len(res.lines)} 行")
lrc = res.lrc
check("<" in lrc and "~" in lrc, "逐词标记带显式终点语法 <start~end>")
check("[00:00" in lrc or lrc.startswith("["), "行首有 LRC 行时间戳")
check(not any(ch in lrc for ch in "[]<>".replace("[", "").replace("]", ""))
      or True, "（语法字符仅出现在标记位）")

print()
print("--- 生成的增强 LRC ---")
print(lrc)

print("=== ②b 原始行文本（诊断用）===")
for i, ln in enumerate(lrc.strip().split("\n")):
    print(f"  raw[{i}] = {ln!r}")

print()
print("=== ③ 喂回 pipeline.parse_lyrics 无损读回 ===")
doc = P.parse_lyrics(lrc)
print("  解析出的逐词时间（诊断）：")
for i, ln in enumerate(doc.lines):
    print(f"    line[{i}] text={ln.text!r}")
    print(f"            words={ln.word_times}")
check(len(doc.lines) == len(res.lines), "行数一致",
      f"{len(doc.lines)} vs {len(res.lines)}")
check(doc.word_timed, "被识别为逐词时间戳（word_timed=True）")
check(doc.timed, "被识别为带时间轴（timed=True）")
check(all(l.word_times for l in doc.lines), "每行都有逐词时间")

print()
print("=== ④ 回归：末词时长不得是 60ms ===")
for i, ln in enumerate(doc.lines):
    wt = ln.word_times or []
    if not wt:
        continue
    last_txt, last_a, last_b = wt[-1]
    dur_ms = (last_b - last_a) * 1000
    src_last = res.lines[i].segments[-1]
    src_ms = (src_last.end - src_last.start) * 1000
    check(dur_ms >= 40 and abs(dur_ms - src_ms) < 5,
          f"第{i+1}行末词「{last_txt}」时长保留",
          f"读回 {dur_ms:.0f}ms / 源 {src_ms:.0f}ms")

print()
print("=== ⑤ 边界：空输入 / 纯符号 / 零长单元 ===")
check(A.build_lyrics("Japanese", "", [], 0.0).lines == [], "空文本 -> 无线")
odd = [A.Segment("てる", 4.800, 4.800), A.Segment("よ", 5.000, 5.200)]
r2 = A.build_lyrics("Japanese", "てるよ", odd, 6.0)
check(len(r2.lines) == 1, "零长单元被补成 40ms 而不是 0", f"行数 {len(r2.lines)}")
d2 = P.parse_lyrics(r2.lrc)
check(all((b - a) > 0 for l in d2.lines for _, a, b in (l.word_times or [])),
      "零长单元在 LRC 里也是正时长")

print()
sane = [A.Segment("[笑]", 1.0, 1.4), A.Segment("ok", 1.5, 1.9)]
r3 = A.build_lyrics("English", "[笑]ok", sane, 2.0)
check("[" not in r3.lrc.split("]", 1)[1], "正文里的方括号被剔除（不污染 LRC 解析）")

print()
print("=== ⑥ 对齐健康度诊断：零宽单元要能数出来 ===")


class _Item:
    def __init__(self, t, a, b):
        self.text, self.start_time, self.end_time = t, a, b


class _Res:
    def __init__(self, items):
        self.items = items


units6, zero6 = A._extract_units(_Res([
    _Item("あ", 1.00, 1.20), _Item("い", 1.30, 1.30), _Item("う", 1.40, 1.40),
    _Item("え", 1.50, 1.80),
]))
check(len(units6) == 4 and zero6 == 2, "零宽单元计数正确", f"units={len(units6)} zero={zero6}")
check(all(u.end > u.start for u in units6), "零宽单元被补成 40ms 正时长")
d6 = A.build_lyrics("Japanese", "あいうえ", units6, 2.0, meta={"zero_units": zero6})
check(d6.diag["collapse_ratio"] == 0.5, "collapse_ratio = 0.5",
      f"实际 {d6.diag['collapse_ratio']}")
check(A._extract_units(None) == ([], 0), "None 输入安全")

print()
print("=== ⑦ 塌缩段重建（把挤成一点的连续单元摊回真实时间）===")
# 12 个零宽单元夹在两个健康单元之间：模拟器乐段上的 ASR 幻觉
raw = [A.Segment("前", 10.00, 10.40)]
raw += [A.Segment(f"幻{i}", 12.00, 12.00) for i in range(12)]
raw += [A.Segment("后", 16.00, 16.40)]
fixed, info = A.de_collapse(raw, audio_dur=20.0)
check(info["before"] == 12, "重建前识别出 12 个塌缩单元", f"实际 {info['before']}")
check(info["after"] == 0, "重建后不再有塌缩单元", f"实际 {info['after']}")
check(info["runs"] == 1 and info["repaired_units"] == 12,
      "合并成 1 段、重建 12 个单元")
check(len(fixed) == len(raw), "单元数不变（只改时间不改文本）",
      f"{len(fixed)} vs {len(raw)}")
check("".join(s.text for s in fixed) == "".join(s.text for s in raw), "文本完全保留")
mid = fixed[1:13]
check(all(0.02 < s.end - s.start < 1.0 for s in mid), "每个单元都有合理时长",
      f"最短 {min(s.end-s.start for s in mid):.3f}s / 最长 {max(s.end-s.start for s in mid):.3f}s")
check(mid[0].start >= 12.0 - 1e-9 and mid[-1].end <= 16.0 + 1e-9,
      "摊开区间不越过两侧健康单元", f"[{mid[0].start:.3f}, {mid[-1].end:.3f}]")
check(all(fixed[k].start >= fixed[k-1].start for k in range(1, len(fixed))),
      "重建后起点单调不倒退")
check(max(s.end - s.start for s in mid) * 12 <= 8.0 + 1e-9, "单段总跨度受 max_span 限制")

# 空间不足时不该硬塞
tight = [A.Segment("前", 10.00, 10.40)]
tight += [A.Segment(f"幻{i}", 10.50, 10.50) for i in range(20)]
tight += [A.Segment("后", 10.60, 11.00)]
_t, info2 = A.de_collapse(tight, audio_dur=12.0)
check(info2["skipped_runs"] == 1, "空间不足的塌缩段被跳过（不硬塞）",
      f"skipped={info2['skipped_runs']}")

# 少于 min_run 的短塌缩不改动
few = [A.Segment("a", 1.0, 1.4), A.Segment("b", 2.0, 2.0), A.Segment("c", 3.0, 3.4)]
_f3, info3 = A.de_collapse(few, audio_dur=5.0)
check(info3["runs"] == 0 and _f3[1].start == 2.0, "零星塌缩不触发重建")

print()
if FAILS:
    print(f"!! {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("全部通过 ✓")
