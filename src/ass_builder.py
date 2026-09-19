"""ASS 卡拉OK字幕编译器 + 渲染行为自检

职责：
  1. 把「逐 token 时间轴」编译成带 \\kf 渐变扫描的 ASS 事件
     —— 含预亮（pre-roll）、防重叠、超长行自动缩字号
  2. 导出纯行级 SRT
  3. --selftest：渲染合成字幕并逐帧像素采样，反查 libass 的真实行为
     —— Primary/Secondary 谁是「已唱色」、\\kf 扫描到位的时刻、
        无文本的 \\k 标签是否推进 karaoke 计时器

关于「同一帧显示多行歌词」bug 的根治思路
--------------------------------------------------
对齐层已经保证 token 单调不重叠（P0 实测 overlap=0），所以这类 bug 只可能
出在字幕层。本模块用两条硬约束把它按死在生成阶段：

  A. 每个歌词行只生成 **一个** 卡拉OK事件；
  B. 所有卡拉OK事件的时间窗两两不重叠（end_i <= start_{i+1} - min_gap），
     而不是依赖播放器「谁在上面盖住谁」。

「下一句预览」是不同角色的事件（Dim 样式 + 更低 MarginV），它允许与当前行
同窗共存，但绝不参与卡拉OK扫描，因此不会产生同帧重复高亮。

颜色约定（libass 实测，见 --selftest 输出）
--------------------------------------------------
  PrimaryColour   = \\kf 扫过「之后」的颜色 —— 已唱 / 高亮色
  SecondaryColour = \\kf 扫过「之前」的颜色 —— 未唱 / 底色
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# 默认参数
# --------------------------------------------------------------------------

DEFAULT_LEAD_MS = 320      # 事件提前于首个 token 起始出现（预亮窗口）
DEFAULT_TAIL_MS = 320      # 末个 token 结束后保持全高亮的时长
DEFAULT_MIN_GAP_MS = 12    # 相邻事件之间的最小空档，保证不重叠
MIN_EVENT_MS = 80          # 事件最短存活时间
BASE_HEIGHT = 1080         # 字号基准高度，其他分辨率按比例缩放


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class KaraokeToken:
    """一个卡拉OK单元（中文/日文=字，英文=词）。"""

    text: str          # 规范 token（无标点），用于对齐匹配
    disp: str          # 实际渲染文本：本 token + 紧跟其后的标点/空格
    start: float       # 秒
    end: float         # 秒
    unit: int | None = None   # 所属对齐单元；同单元连续 token 可合并高亮


@dataclass
class KaraokeLine:
    raw: str = ""
    tokens: list[KaraokeToken] = field(default_factory=list)
    head: str = ""          # 首个 token 之前的标点/空格
    start: float = 0.0
    end: float = 0.0
    # 由编译器回填
    ev_start: float = 0.0
    ev_end: float = 0.0

    @property
    def text(self) -> str:
        return self.head + "".join(t.disp for t in self.tokens)


@dataclass
class AssOptions:
    font: str = "Microsoft YaHei"
    font_size: int = 66
    sung_color: tuple[int, int, int] = (74, 210, 255)     # 已唱：#FFD24A 金
    unsung_color: tuple[int, int, int] = (255, 255, 255)  # 未唱：白
    outline_color: tuple[int, int, int] = (16, 16, 16)
    outline: float = 3.0
    shadow: float = 0.0
    bold: bool = True
    margin_v: int = 118              # 当前行距底部
    margin_h: int = 60
    next_line: bool = True           # 显示下一句预览
    next_scale: float = 0.62
    next_margin_v: int = 46
    next_color: tuple[int, int, int] = (176, 184, 196)
    lead_ms: int = DEFAULT_LEAD_MS
    tail_ms: int = DEFAULT_TAIL_MS
    min_gap_ms: int = DEFAULT_MIN_GAP_MS
    auto_shrink: bool = True         # 超长行自动缩字号，避免换行撞到预览行
    safe_width_ratio: float = 0.92
    group_same_unit: bool = False    # 把落在同一对齐单元内的连续 token 合成一次扫描
                                     # （日文推荐：nagisa 常把「渡って」并成一个单元，
                                     #   单元内逐字定位精度有限，整单元高亮更稳）


# --------------------------------------------------------------------------
# 颜色与文本转义
# --------------------------------------------------------------------------


def ass_color(rgb: tuple[int, int, int], alpha: int = 0) -> str:
    """(r,g,b) -> ASS 的 &HAABBGGRR。"""
    r, g, b = rgb
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def esc_text(s: str) -> str:
    """ASS 正文转义：花括号会开启/结束 override 块，必须中和。"""
    return s.replace("\\", "＼").replace("{", "（").replace("}", "）")


def cs(seconds: float) -> int:
    """秒 -> 厘秒（\\k/\\kf 的单位）。"""
    return max(1, int(round(seconds * 100.0)))


def est_width(s: str, font_size: float) -> float:
    """粗估文本像素宽：CJK 约 1.0 em，拉丁/数字约 0.55 em。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o > 0x2E80:          # CJK / 假名 / 全角
            w += 1.0
        elif ch in " .,:;!|'":
            w += 0.30
        else:
            w += 0.55
    return w * font_size


# --------------------------------------------------------------------------
# 时间窗求解（防重叠的核心）
# --------------------------------------------------------------------------


def solve_windows(lines: list[KaraokeLine], opt: AssOptions) -> None:
    """为每个行求解事件时间窗，就地写回 ev_start / ev_end。

    两趟扫描：
      第一趟 定 start：行首提前 lead_ms，但不得侵入上一行的结束 + min_gap
      第二趟 定 end  ：行尾延后 tail_ms，但不得侵入下一行的 start - min_gap
    这样得到的事件窗集合一定是两两不重叠的（同一方向推挤，单调）。
    """
    n = len(lines)
    if n == 0:
        return
    lead = opt.lead_ms / 1000.0
    tail = opt.tail_ms / 1000.0
    gap = opt.min_gap_ms / 1000.0

    for i, ln in enumerate(lines):
        want = ln.start - lead
        if i == 0:
            ln.ev_start = max(0.0, want)
        else:
            ln.ev_start = max(want, lines[i - 1].ev_start + MIN_EVENT_MS / 1000.0,
                              ln.start - lead)
            # 真正的下界由上一行的 ev_end 决定，这里先给一个保守值，
            # 第二趟结束后再统一前推修正（见下方 forward fix）
            ln.ev_start = max(ln.ev_start, 0.0)

    for i, ln in enumerate(lines):
        want = ln.end + tail
        if i + 1 < n:
            ln.ev_end = min(want, lines[i + 1].start - lead - gap)
        else:
            ln.ev_end = want
        # 至少活得够 MIN_EVENT_MS
        if ln.ev_end < ln.ev_start + MIN_EVENT_MS / 1000.0:
            ln.ev_end = ln.ev_start + MIN_EVENT_MS / 1000.0

    # 前向修正：保证 ev_start_i >= ev_end_{i-1} + gap
    for i in range(1, n):
        lo = lines[i - 1].ev_end + gap
        if lines[i].ev_start < lo:
            lines[i].ev_start = lo
            if lines[i].ev_end < lines[i].ev_start + MIN_EVENT_MS / 1000.0:
                lines[i].ev_end = lines[i].ev_start + MIN_EVENT_MS / 1000.0
    # 反向修正：保证 ev_end_i <= ev_start_{i+1} - gap
    for i in range(n - 2, -1, -1):
        hi = lines[i + 1].ev_start - gap
        if lines[i].ev_end > hi:
            lines[i].ev_end = hi
            if lines[i].ev_start > lines[i].ev_end - MIN_EVENT_MS / 1000.0:
                lines[i].ev_start = max(0.0, lines[i].ev_end - MIN_EVENT_MS / 1000.0)


def check_windows(lines: list[KaraokeLine], min_gap_ms: float = 1.0) -> dict:
    """自检：事件窗是否真的不重叠、token 是否单调。"""
    gap = min_gap_ms / 1000.0
    overlaps, nonmono = [], []
    for i in range(len(lines) - 1):
        if lines[i].ev_end > lines[i + 1].ev_start + 1e-6:
            overlaps.append(i)
    for i, ln in enumerate(lines):
        prev = -1e9
        for t in ln.tokens:
            if t.start + 1e-6 < prev:
                nonmono.append(i)
                break
            prev = t.end
    bad_dur = [i for i, ln in enumerate(lines) if ln.ev_end <= ln.ev_start]
    return {
        "lines": len(lines),
        "overlap_pairs": overlaps,
        "nonmonotonic_lines": sorted(set(nonmono)),
        "zero_duration_events": bad_dur,
        "ok": not overlaps and not nonmono and not bad_dur,
    }


# --------------------------------------------------------------------------
# 逐行 → ASS 文本
# --------------------------------------------------------------------------


def _segments(ln: KaraokeLine, group: bool) -> list[list[KaraokeToken]]:
    """把 token 序列切成扫描段。

    group=True 时，落在同一对齐单元内的连续 token 合成一段，整段一次扫完。
    日文场景下这能规避「一个单元被模型切成多字、字间时刻只能靠插值猜」的
    误差 —— 实测 nagisa 会把「渡って」并成一单元，而该单元内的逐字时刻
    最多可偏 300ms；整单元高亮则只依赖单元边界（那是模型的可靠输出）。
    """
    segs: list[list[KaraokeToken]] = []
    for t in ln.tokens:
        if (group and t.unit is not None and segs
                and segs[-1][0].unit == t.unit):
            segs[-1].append(t)
        else:
            segs.append([t])
    return segs


def _karaoke_body(ln: KaraokeLine, opt: AssOptions, ev_start: float,
                  play_res_x: int, font_size: int) -> str:
    """生成一行卡拉OK正文：{\\k 预亮}{\\kf d0}甲{\\kf d1}乙...

    时长 = 相邻扫描段「起点之差」（末段取到行尾），因此：
      - 整行扫描连续无空洞，不会出现高亮卡顿；
      - 标点被并入前一段的扫描区间，观感自然。
    累积时刻用整数厘秒累加器换算，避免逐段四舍五入造成漂移。
    """
    toks = ln.tokens
    if not toks:
        return esc_text(ln.text)

    parts: list[str] = []

    # 超长行自动缩字号，防止换行后撞到下一句预览
    fs = font_size
    if opt.auto_shrink:
        limit = play_res_x * opt.safe_width_ratio
        w = est_width(ln.text, font_size)
        if w > limit and w > 0:
            fs = max(20, int(font_size * limit / w))
    if fs != font_size:
        parts.append(r"{\fs%d}" % fs)

    # 预亮：首个 token 开唱前，先以底色把整行显示出来。
    # 无正文的 \k 标签同样推进 libass 的 karaoke 计时器（--selftest 已验证），
    # 所以这一步不需要任何占位字符。
    head_pre = max(0.0, toks[0].start - ev_start)
    emitted = 0
    if head_pre > 0.005:
        emitted = cs(head_pre)
        parts.append(r"{\k%d}" % emitted)

    if ln.head:
        parts.append(esc_text(ln.head))

    segs = _segments(ln, opt.group_same_unit)
    acc = head_pre
    for i, seg in enumerate(segs):
        nxt = segs[i + 1][0].start if i + 1 < len(segs) else ln.end
        acc += max(0.0, nxt - seg[0].start)
        total = cs(acc)                    # 自事件起点起的累积厘秒
        if total <= emitted:
            total = emitted + 1
        d = total - emitted
        emitted = total
        parts.append(r"{\kf%d}" % d)
        parts.append("".join(esc_text(t.disp if t.disp else t.text) for t in seg))

    return "".join(parts)


def _fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def build_ass(lines: list[KaraokeLine], opt: AssOptions,
              width: int = 1920, height: int = 1080) -> str:
    """编译完整的 ASS 文件内容。"""
    scale = height / BASE_HEIGHT
    fs = max(14, int(opt.font_size * scale))
    mv = int(opt.margin_v * scale)
    mh = int(opt.margin_h * scale)
    outline = opt.outline * scale
    shadow = opt.shadow * scale
    next_fs = max(12, int(fs * opt.next_scale))
    next_mv = int(opt.next_margin_v * scale)

    sung = ass_color(opt.sung_color)
    unsung = ass_color(opt.unsung_color)
    dim = ass_color(opt.next_color)
    ol = ass_color(opt.outline_color)
    bold = -1 if opt.bold else 0

    head = [
        "[Script Info]",
        "; 由 lets-karaoke 生成",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Primary=已唱（\kf 扫过之后），Secondary=未唱（扫过之前）
        f"Style: LYRIC,{opt.font},{fs},{sung},{unsung},{ol},&H00000000,"
        f"{bold},0,0,0,100,100,0,0,1,{outline:.1f},{shadow:.1f},2,{mh},{mh},{mv},1",
        f"Style: NEXT,{opt.font},{next_fs},{dim},{dim},{ol},&H00000000,"
        f"{bold},0,0,0,100,100,0,0,1,{max(0.0, outline - 1 * scale):.1f},"
        f"{shadow:.1f},2,{mh},{mh},{next_mv},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text",
    ]

    n = len(lines)
    events: list[str] = []
    for i, ln in enumerate(lines):
        if not ln.tokens:
            continue
        body = _karaoke_body(ln, opt, ln.ev_start, width, fs)
        events.append(
            f"Dialogue: 0,{_fmt_time(ln.ev_start)},{_fmt_time(ln.ev_end)},"
            f"LYRIC,,0,0,0,,{body}"
        )
        if opt.next_line and i + 1 < n:
            nxt = lines[i + 1]
            txt = esc_text(nxt.raw or nxt.text)
            events.append(
                f"Dialogue: -1,{_fmt_time(ln.ev_start)},{_fmt_time(ln.ev_end)},"
                f"NEXT,,0,0,0,,{txt}"
            )
    return "\n".join(head + events) + "\n"


def build_srt(lines: list[KaraokeLine]) -> str:
    """行级 SRT（便于快速预览 / 校对）。"""
    out, idx = [], 0
    for ln in lines:
        if not ln.tokens:
            continue
        idx += 1
        out.append(str(idx))
        out.append(f"{_srt_time(ln.start)} --> {_srt_time(ln.end)}")
        out.append(ln.raw or ln.text)
        out.append("")
    return "\n".join(out)


def _srt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - int(sec)) * 1000))
    if ms == 1000:
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


# --------------------------------------------------------------------------
# 自检：用像素说话
# --------------------------------------------------------------------------

_PURE_RED = (255, 0, 0)
_PURE_GREEN = (0, 255, 0)


def _selftest_ass() -> str:
    """构造一个行为可完全预测的字幕：两段，边界已知。"""
    # token A: 1.00-2.00s   token B: 2.00-3.00s
    # 事件窗 0.80-3.30（lead=0.20s pre-roll，tail=0.30s）
    opt = AssOptions(
        font="Arial", font_size=120, outline=0.0, shadow=0.0, bold=False,
        sung_color=_PURE_GREEN, unsung_color=_PURE_RED,
        next_line=False, auto_shrink=False, lead_ms=200, tail_ms=300,
    )
    ln = KaraokeLine(raw="AB", head="", start=1.0, end=3.0, tokens=[
        KaraokeToken(text="A", disp="A", start=1.00, end=2.00),
        KaraokeToken(text="B", disp="B", start=2.00, end=3.00),
    ])
    solve_windows([ln], opt)
    return build_ass([ln], opt, width=900, height=300), ln


def _count(px, rgb, tol=70):
    r, g, b = rgb
    m = ((px[..., 0].astype(int) - r).__abs__() < tol) & \
        ((px[..., 1].astype(int) - g).__abs__() < tol) & \
        ((px[..., 2].astype(int) - b).__abs__() < tol)
    return int(m.sum())


def selftest(outdir: Path) -> int:
    """渲染合成字幕 → 逐帧采样 → 反查 libass 真实行为。

    采样点：
      t=0.50  事件尚未开始          → 应当什么都没有
      t=0.90  事件已开始但未开唱    → 只应有「未唱色」
      t=1.50  正在扫 A              → 已唱色与未唱色同时存在
      t=2.50  正在扫 B              → 已唱色与未唱色同时存在
      t=3.10  已扫完                → 应当全是「已唱色」
    """
    import numpy as np

    outdir.mkdir(parents=True, exist_ok=True)
    ass_text, ln = _selftest_ass()
    (outdir / "selftest.ass").write_text(ass_text, encoding="utf-8-sig")

    W, H, FPS, DUR = 900, 300, 25, 3.6
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={DUR}",
        "-vf", "ass=selftest.ass",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "selftest.raw",
    ]
    r = subprocess.run(cmd, cwd=str(outdir), capture_output=True)
    if r.returncode != 0:
        print("!! ffmpeg 渲染失败：")
        print(r.stderr.decode("utf-8", "replace")[-2000:])
        return 1

    buf = (outdir / "selftest.raw").read_bytes()
    frames = np.frombuffer(buf, dtype=np.uint8)
    need = W * H * 3
    nfr = frames.size // need
    frames = frames[: nfr * need].reshape(nfr, H, W, 3)

    probe = [(0.50, "事件前"), (0.90, "预亮期(未开唱)"),
             (1.50, "扫过 A 中"), (2.50, "扫过 B 中"), (3.10, "扫完之后")]

    R: list[str] = []
    R.append(f"渲染 {nfr} 帧（{W}x{H}@{FPS}），事件窗 "
             f"{ln.ev_start:.2f}-{ln.ev_end:.2f}s，"
             f"token A 1.00-2.00 / B 2.00-3.00")
    R.append("")
    R.append(f"{'时刻':>6} {'说明':<16} {'红(unsung)':>11} {'绿(sung)':>10}")
    R.append("-" * 50)
    stats = {}
    for t, label in probe:
        i = min(nfr - 1, int(round(t * FPS)))
        px = frames[i]
        red = _count(px, _PURE_RED)
        grn = _count(px, _PURE_GREEN)
        stats[t] = (red, grn)
        R.append(f"{t:>6.2f} {label:<16} {red:>11} {grn:>10}")

    R.append("")
    verdict = []
    # 1) 事件开始前必须空白
    verdict.append(("事件前无像素", stats[0.50][0] + stats[0.50][1] == 0))
    # 2) 预亮期：有字，且全部是「未唱色」—— 证明无正文的 \k 确实推进了计时器
    verdict.append(("预亮期显示未唱色文字",
                    stats[0.90][0] > 50 and stats[0.90][1] < 5))
    # 3) 扫描中：两色共存 —— 证明 \kf 是渐变扫描而非瞬间切换
    verdict.append(("扫 A 时两色共存",
                    stats[1.50][0] > 20 and stats[1.50][1] > 20))
    verdict.append(("扫 B 时两色共存",
                    stats[2.50][0] > 20 and stats[2.50][1] > 20))
    # 4) 扫完：全绿
    verdict.append(("扫完后全为已唱色",
                    stats[3.10][1] > 50 and stats[3.10][0] < 5))

    ok = all(v for _, v in verdict)
    for name, v in verdict:
        R.append(f"  [{'PASS' if v else 'FAIL'}] {name}")
    R.append("")
    if stats[0.90][0] > 50 and stats[3.10][1] > 50:
        R.append("实测颜色语义（libass）：")
        R.append("  PrimaryColour   = 扫过之后 = 已唱高亮色（本例绿）")
        R.append("  SecondaryColour = 扫过之前 = 未唱底色（本例红）")
        R.append("  -> 与本模块 build_ass 的 Style 写法一致。")
    else:
        R.append("!! 颜色语义与预期不符，需检查 Style 里 Primary/Secondary 的取值。")
    R.append("")
    R.append("SELFTEST " + ("PASS" if ok else "FAIL"))

    text = "\n".join(R)
    (outdir / "selftest_report.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0 if ok else 1


def demo(outdir: Path | None = None) -> int:
    """生成一份可视化对照：把 selftest 的采样点导成 PNG。"""
    import numpy as np
    from PIL import Image

    outdir = outdir or Path("out/ass_selftest")
    rc = selftest(outdir)
    if rc != 0:
        return rc
    W, H, FPS = 900, 300, 25
    buf = (outdir / "selftest.raw").read_bytes()
    a = np.frombuffer(buf, dtype=np.uint8)
    n = a.size // (W * H * 3)
    a = a[: n * W * H * 3].reshape(n, H, W, 3)
    shots = []
    for t in (0.50, 0.90, 1.50, 2.50, 3.10):
        i = min(n - 1, int(round(t * FPS)))
        shots.append(a[i])
    strip = np.concatenate(shots, axis=1)
    p = outdir / "selftest_strip.png"
    Image.fromarray(strip).save(p)
    # 裸 RGB 转储有几十 MB，导出对照图后就地清理
    (outdir / "selftest.raw").unlink(missing_ok=True)
    print(f"对照图: {p}")
    print(f"报告:   {outdir / 'selftest_report.txt'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ASS 卡拉OK编译器")
    ap.add_argument("--selftest", action="store_true", help="渲染并逐帧验证 libass 行为")
    ap.add_argument("--demo", action="store_true", help="selftest 并导出采样对照图")
    ap.add_argument("--out", default="out/ass_selftest")
    args = ap.parse_args(argv)
    if args.demo:
        return demo(Path(args.out))
    if args.selftest:
        return selftest(Path(args.out))
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
