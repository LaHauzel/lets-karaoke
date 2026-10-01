"""P1 端到端验证：拿 P0 合成集的精确真值给整条管线打分。

每个用例做四件事：
  1. 用真值音轨 + 纯色背景造一个真 mp4（走完整 ffmpeg 输入输出链路）
  2. 生成歌词（纯文本 / LRC）
  3. 跑 src/pipeline.py
  4. 用 manifest 里的逐 token 真值算边界误差，并检查字幕结构自检结果

用例矩阵刻意覆盖三条容易翻车的路径：
  - 带伴奏 -> 走 Demucs 分离再对齐
  - 日文   -> 检验 nagisa 多字单元回映射到逐字
  - LRC    -> 检验行级时间规整（warp）

用法：
  python tests/e2e_p1.py                # 全跑
  python tests/e2e_p1.py --only zh_mix  # 只跑某个用例
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

from p0_common import LANGS, boundary_metrics, tokenize  # noqa: E402
from pipeline import (  # noqa: E402
    AssOptions, ModelCache, PipelineConfig, parse_lyrics, run,
)

DATA = ROOT / "data" / "synth"
OUT = ROOT / "out" / "p1_e2e"

# (用例名, 语言, 变体, 是否分离, 歌词形态)
CASES = [
    ("zh_clean", "zh", "gapped_clean", False, "text"),
    ("zh_mix", "zh", "gapped_mix", True, "text"),
    ("en_clean", "en", "gapped_clean", False, "text"),
    ("ja_clean", "ja", "gapped_clean", False, "text"),
    ("ja_mix", "ja", "legato_mix", True, "text"),
    ("zh_lrc", "zh", "gapped_clean", False, "lrc"),
    ("en_lrc", "en", "gapped_clean", False, "lrc"),
    ("ja_lrc", "ja", "gapped_clean", False, "lrc"),
]


def load_manifest() -> dict:
    return json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))


def line_token_counts(lines: list[str], lang: str) -> list[int]:
    spec = LANGS[lang]
    return [len(tokenize(x, spec)) for x in lines]


def make_lrc(lines: list[str], counts: list[int], truth: list[dict]) -> str:
    """按真值生成 LRC（行起点 = 该行首 token 的真实起点）。"""
    out, k = [], 0
    for txt, n in zip(lines, counts):
        t = truth[k]["start"]
        k += n
        m = int(t // 60)
        s = t % 60
        out.append(f"[{m:02d}:{s:05.2f}]{txt}")
    return "\n".join(out)


def make_video(audio: Path, dst: Path, w=1280, h=720, fps=30) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", f"color=c=0x16202A:s={w}x{h}:r={fps}",
           "-i", str(audio),
           "-map", "0:v", "-map", "1:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
           "-shortest", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dst


def score(truth: list[dict], produced: list[dict]) -> dict:
    """按位置比文本、按文本比时间（口径与 P0 一致）。"""
    from p0_common import TokenSpan
    T = [TokenSpan(text=x["text"], start=x["start"], end=x["end"]) for x in truth]
    P = [TokenSpan(text=x["text"], start=x["start"], end=x["end"]) for x in produced]
    return boundary_metrics(T, P)


def quality_failures(record, expected_lines, expected_tokens, max_start_p90_ms=250):
    """Synthetic regression gates, separate from real-singing accuracy claims."""
    failures = []
    if not record.get('pipeline_ok', record.get('ok')):
        return ['pipeline_failed']
    metrics = record.get('metrics', {})
    if record.get('output_lines') != expected_lines:
        failures.append('line_count_mismatch')
    if metrics.get('n_pred') != expected_tokens or metrics.get('n') != expected_tokens or metrics.get('skipped', 0):
        failures.append('incomplete_token_mapping')
    if not record.get('health', {}).get('ok'):
        failures.append('invalid_subtitle_structure')
    p90 = metrics.get('start_p90_ms', math.inf)
    if not math.isfinite(p90) or p90 > max_start_p90_ms:
        failures.append('start_p90_exceeds_budget')
    if not record.get('outputs_exist'):
        failures.append('missing_output')
    return failures


def run_case(name: str, lang: str, variant: str, separate: bool, lyr_mode: str,
             man: dict, cache: ModelCache, max_start_p90_ms=250) -> dict:
    d = man["langs"][lang]
    v = d["variants"][variant]
    audio = ROOT / v["audio"]
    truth = v["truth"]
    lines = d["lyrics_lines"]
    counts = line_token_counts(lines, lang)

    work = OUT / name
    work.mkdir(parents=True, exist_ok=True)
    video = make_video(audio, work / "input.mp4")
    lyrics = ("\n".join(lines) if lyr_mode == "text"
              else make_lrc(lines, counts, truth))
    (work / ("lyrics.lrc" if lyr_mode == "lrc" else "lyrics.txt")).write_text(
        lyrics, encoding="utf-8")

    cfg = PipelineConfig(
        media=str(video), lyrics_text=lyrics,
        vocal_mode="remove" if separate else "keep",
        lang=lang, separate=separate, out_dir=str(OUT), job_name=name,
        ass=AssOptions(font="Microsoft YaHei", font_size=60, next_line=True),
    )
    t0 = time.perf_counter()
    res = run(cfg, progress=None, cancel=None, cache=cache)
    wall = time.perf_counter() - t0

    rec = {"case": name, "lang": lang, "variant": variant,
           "separate": separate, "lyrics": lyr_mode, "ok": res.ok, 'pipeline_ok':res.ok,
           "error": res.error, "wall_sec": round(wall, 2),
           "job_dir": res.job_dir, "video": res.video}
    if not res.ok:
        return rec

    aj = json.loads(Path(res.align_json).read_text(encoding="utf-8"))
    produced = [t for ln in aj["lines"] for t in ln["tokens"]]
    rec["metrics"] = score(truth, produced)
    rec["health"] = aj["health"]
    rec["mapping"] = aj["mapping"]
    rec["warp"] = aj["warp"]
    rec["stats"] = res.stats
    rec['output_lines'] = len(aj['lines'])
    rec['outputs_exist'] = all(p and Path(p).is_file() and Path(p).stat().st_size > 0
        for p in (res.video,res.ass,res.srt,res.align_json))

    # 行级：比较每行起点
    k, lerr = 0, []
    for ln, n in zip(aj["lines"], counts):
        if ln['tokens']:
            lerr.append(ln["tokens"][0]["start"] - truth[k]["start"])
        k += n
    lerr = np.abs(np.array(lerr)) * 1000
    rec["line_start_mae_ms"] = round(float(lerr.mean()), 1) if len(lerr) else None
    rec["video_mb"] = round(Path(res.video).stat().st_size / 1024 / 1024, 2)
    rec["video_exists"] = Path(res.video).exists()
    rec['quality_failures'] = quality_failures(rec,len(lines),len(truth),max_start_p90_ms)
    rec['ok'] = not rec['quality_failures']
    if not rec['ok']:
        rec['error'] = ', '.join(rec['quality_failures'])
    return rec


def main(argv=None) -> int:
    global OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--device", default="cuda")
    ap.add_argument('--max-start-p90-ms',type=float,default=250,
                    help='Synthetic start-time P90 regression budget; not a singing-quality claim')
    ap.add_argument('--out-dir',type=Path,default=OUT)
    args = ap.parse_args(argv)
    if not math.isfinite(args.max_start_p90_ms) or args.max_start_p90_ms <= 0:
        ap.error('--max-start-p90-ms must be finite and positive')
    cases = [c for c in CASES if (not args.only or args.only in c[0])]
    if not cases:
        ap.error('--only did not match any case')
    OUT = args.out_dir.resolve()
    OUT.mkdir(parents=True, exist_ok=True)
    man = load_manifest()
    cache = ModelCache(args.device)

    recs = []
    print(f"{'用例':<10} {'后端口径':<26} {'起点MAE':>8} {'中位':>7} "
          f"{'P90':>7} {'≤50ms':>7} {'行MAE':>7} {'结构':>5} {'耗时':>7}")
    print("-" * 92)
    for name, lang, variant, sep, lm in cases:
        try:
            r = run_case(name, lang, variant, sep, lm, man, cache, args.max_start_p90_ms)
        except Exception as e:  # noqa: BLE001
            r = {"case": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        recs.append(r)
        if r.get("ok"):
            m = r["metrics"]
            tag = f"{lang}/{variant}/{'分离' if sep else '不分离'}/{lm}"
            print(f"{name:<10} {tag:<26} {m['start_mae_ms']:>8.1f} "
                  f"{m['start_median_ms']:>7.1f} {m['start_p90_ms']:>7.1f} "
                  f"{m['start_hit50']:>6.0f}% {r['line_start_mae_ms']:>7.1f} "
                  f"{'OK' if r['health']['ok'] else 'BAD':>5} "
                  f"{r['wall_sec']:>6.1f}s")
        else:
            print(f"{name:<10} !! 失败: {r.get('error')}")
        (OUT / "results.json").write_text(
            json.dumps(recs, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for r in recs if r.get("ok"))
    print("-" * 92)
    print(f"成功 {ok}/{len(recs)}；结果 -> {OUT / 'results.json'}")
    return 0 if ok == len(recs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
