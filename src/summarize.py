"""P0 验证 · 结果汇总

读取 out/p0/results.json，产出控制台表格 + summary.md，并给出关键判定。

术语对齐（重要）：
  item 的 id 形如 synth_{lang}_{cond}，cond ∈ {gapped_clean, gapped_mix,
  legato_clean, legato_mix}；而 pipeline 只有两种取值：mix / vocals。
  即：cond 描述「素材条件」，pipeline 描述「处理路径」。

用法：
  python src/summarize.py --results out/p0/results.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

CONDS = ("gapped_clean", "gapped_mix", "legato_clean", "legato_mix")


def fmt(v, nd=1, suffix=""):
    if v is None:
        return "—"
    if isinstance(v, str):
        return v
    return f"{v:.{nd}f}{suffix}"


def cond_of(item: dict) -> str:
    i = item["id"]
    for c in CONDS:
        if i.endswith(c):
            return c
    return item.get("note", "") or "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default="out/p0/results.json")
    ap.add_argument("--out", default="out/p0/summary.md")
    args = ap.parse_args()

    data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    items = data["items"]

    rows = []
    for it in items:
        cond = cond_of(it)
        for pname, prec in it["pipelines"].items():
            for be in ("qwen", "wav2vec2"):
                r = prec.get(be)
                if not r:
                    continue
                base = {"id": it["id"], "lang": it["lang"], "cond": cond, "pipeline": pname, "backend": be}
                if r.get("error"):
                    rows.append({**base, "error": r["error"]})
                    continue
                m = r.get("metrics") or {}
                d = r.get("diag") or {}
                rows.append({
                    **base,
                    "n": m.get("n"), "n_truth": m.get("n_truth"), "n_pred": m.get("n_pred"),
                    "start_mae": m.get("start_mae_ms"), "start_med": m.get("start_median_ms"),
                    "start_p90": m.get("start_p90_ms"), "start_bias": m.get("start_bias_ms"),
                    "hit25": m.get("start_hit25"), "hit50": m.get("start_hit50"),
                    "hit100": m.get("start_hit100"), "end_mae": m.get("end_mae_ms"),
                    "overlap": d.get("n_overlap"), "nonmono": d.get("n_nonmonotonic"),
                    "coverage": d.get("coverage"), "oov": r.get("oov"), "rtf": r.get("rtf"),
                })

    # 聚合： (lang, cond, pipeline, backend) -> [rows]
    agg: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r.get("error") or r.get("start_mae") is None:
            continue
        agg[(r["lang"], r["cond"], r["pipeline"], r["backend"])].append(r)

    def mean(key_tuple, field):
        v = agg.get(key_tuple, [])
        vals = [x[field] for x in v if x.get(field) is not None]
        return sum(vals) / len(vals) if vals else None

    L: list[str] = []
    L.append("# P0 对齐验证 · 结果汇总\n")
    L.append(f"- 设备：`{data.get('device')}`")
    L.append(f"- 样本数：{data.get('n_items')}")
    L.append(f"- 结果行数：{len(rows)}\n")

    # ---------- 明细 ----------
    L.append("## 一、明细\n")
    L.append("| 样本 | 语言 | 条件 | 管线 | 后端 | n/真值 | 起点MAE | 中位 | P90 | 偏差 | ≤25ms | ≤50ms | ≤100ms | 终点MAE | 重叠 | 非单调 | OOV | RTF |")
    L.append("|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        if r.get("error"):
            L.append(f"| {r['id']} | {r['lang']} | {r['cond']} | {r['pipeline']} | {r['backend']} | "
                     f"| 失败 | | | | | | | | | | | {r['error']} |")
            continue
        L.append(
            f"| {r['id']} | {r['lang']} | {r['cond']} | {r['pipeline']} | {r['backend']} | "
            f"{r.get('n')}/{r.get('n_truth')} | {fmt(r.get('start_mae'))} | {fmt(r.get('start_med'))} | "
            f"{fmt(r.get('start_p90'))} | {fmt(r.get('start_bias'))} | {fmt(r.get('hit25'))}% | "
            f"{fmt(r.get('hit50'))}% | {fmt(r.get('hit100'))}% | {fmt(r.get('end_mae'))} | "
            f"{r.get('overlap')} | {r.get('nonmono')} | "
            f"{r.get('oov') if r.get('oov') is not None else '—'} | {fmt(r.get('rtf'), 3)} |"
        )

    # ---------- 聚合 ----------
    L.append("\n## 二、聚合（起点 MAE 均值 / ≤50ms 命中率均值）\n")
    L.append("| 语言 | 条件 | 管线 | 后端 | 样本 | 起点MAE(ms) | ≤50ms | 终点MAE(ms) |")
    L.append("|---|---|---|---|---:|---:|---:|---:|")
    for k in sorted(agg):
        v = agg[k]
        mae = sum(x["start_mae"] for x in v) / len(v)
        h50 = [x["hit50"] for x in v if x.get("hit50") is not None]
        emae = [x["end_mae"] for x in v if x.get("end_mae") is not None]
        L.append(
            f"| {k[0]} | {k[1]} | {k[2]} | {k[3]} | {len(v)} | {mae:.1f} | "
            f"{(sum(h50)/len(h50)):.1f}% | {(sum(emae)/len(emae)):.1f} |"
        )

    langs = sorted({r["lang"] for r in rows})
    backends = [b for b in ("qwen", "wav2vec2") if any(r["backend"] == b for r in rows)]

    # ---------- 判定 ----------
    L.append("\n## 三、关键判定\n")

    L.append("### 1) 伴奏干扰：clean → mix 的精度损失（pipeline=mix）\n")
    L.append("| 语言 | 后端 | clean MAE | mix MAE | 增量 | 结论 |")
    L.append("|---|---|---:|---:|---:|---|")
    for lang in langs:
        for be in backends:
            c = mean((lang, "gapped_clean", "mix", be), "start_mae")
            m = mean((lang, "gapped_mix", "mix", be), "start_mae")
            if c is not None and m is not None:
                d = m - c
                tag = "伴奏显著干扰" if d > 20 else ("有影响" if d > 5 else "影响可忽略")
                L.append(f"| {lang} | {be} | {c:.1f} | {m:.1f} | {d:+.1f} | {tag} |")

    L.append("\n### 2) 分离收益：mix 直接对齐 vs 分离后人声对齐（cond=gapped_mix）\n")
    L.append("| 语言 | 后端 | mix 管线 | vocals 管线 | 增益 | 结论 |")
    L.append("|---|---|---:|---:|---:|---|")
    for lang in langs:
        for be in backends:
            m = mean((lang, "gapped_mix", "mix", be), "start_mae")
            v = mean((lang, "gapped_mix", "vocals", be), "start_mae")
            if m is not None and v is not None:
                g = m - v
                tag = "**分离有益**" if g > 3 else ("分离无收益" if g > -3 else "**分离反而更差**")
                L.append(f"| {lang} | {be} | {m:.1f} | {v:.1f} | {g:+.1f} | {tag} |")

    L.append("\n### 3) 连续音频难度：gapped → legato（pipeline=mix, clean）\n")
    L.append("| 语言 | 后端 | gapped MAE | legato MAE | 增量 |")
    L.append("|---|---|---:|---:|---:|")
    for lang in langs:
        for be in backends:
            g = mean((lang, "gapped_clean", "mix", be), "start_mae")
            l = mean((lang, "legato_clean", "mix", be), "start_mae")
            if g is not None and l is not None:
                L.append(f"| {lang} | {be} | {g:.1f} | {l:.1f} | {l-g:+.1f} |")

    L.append("\n### 4) 覆盖率与结构异常（pipeline=mix）\n")
    L.append("| 样本 | 后端 | 重叠 | 非单调 | 覆盖率 |")
    L.append("|---|---|---:|---:|---:|")
    for r in rows:
        if r.get("error") or r["pipeline"] != "mix":
            continue
        L.append(f"| {r['id']} | {r['backend']} | {r.get('overlap')} | {r.get('nonmono')} | {fmt(r.get('coverage'), 3)} |")

    L.append("\n### 5) 跨后端一致性（无真值场景的代理指标，pipeline=mix）\n")
    L.append("| 样本 | 中位差(ms) | P90(ms) | ≤50ms 一致率 |")
    L.append("|---|---:|---:|---:|")
    any_ag = False
    for it in items:
        ag = it["pipelines"].get("mix", {}).get("agreement")
        if ag and ag.get("ok"):
            any_ag = True
            L.append(f"| {it['id']} | {ag['median_abs_diff_ms']} | {ag['p90_abs_diff_ms']} | {ag['within_50ms_pct']}% |")
    if not any_ag:
        L.append("| — | — | — | — |")

    L.append("\n### 6) 失败与异常\n")
    errs = [r for r in rows if r.get("error")]
    if errs:
        for r in errs:
            L.append(f"- `{r['id']}` / {r['pipeline']} / {r['backend']}：{r['error']}")
    else:
        L.append("- 无")

    L.append("\n### 7) wav2vec2 词表覆盖（OOV 越高，该语言结论越不可信）\n")
    for lang in langs:
        v = [r for r in rows if r["lang"] == lang and r["backend"] == "wav2vec2" and r.get("oov") is not None]
        if v:
            tot = sum(x["n_truth"] or 0 for x in v)
            oov = sum(x["oov"] for x in v)
            L.append(f"- `{lang}`：OOV {oov} / {tot} token（{oov/max(1,tot)*100:.1f}%）")

    txt = "\n".join(L)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(txt, encoding="utf-8")

    # 控制台只打印聚合与判定，不刷明细
    idx = txt.find("## 二、聚合")
    print(txt[idx:] if idx > 0 else txt)
    print(f"\n-> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
