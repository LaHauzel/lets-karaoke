"""P0 验证主运行器

对比维度：
  管线  mix     —— 原始音频（含伴奏）直接对齐
        vocals  —— 先 htdemucs_ft 分离人声，再对齐
  后端  qwen    —— Qwen3-ForcedAligner-0.6B
        wav2vec2—— 语言相关 wav2vec2 CTC + torchaudio forced_align

数据来源：
  合成集（有确定性真值）→ 输出完整误差指标
  用户音频（无真值）    → 输出跨后端一致性 + 单调性/重叠诊断，并导出逐字时间戳供抽查

用法：
  python src/run_p0.py --synth data/synth --out out/p0
  python src/run_p0.py --songs data/songs --out out/p0_songs
  python src/run_p0.py --make-songs-template data/songs
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

# 模型权重都已落地到 models/，禁止再去 huggingface.co 做联网校验。
# 网络不通时 huggingface_hub 会重试 5 次、每次 read timeout 10s，纯粹浪费几分钟。
os.environ.setdefault("HF_HUB_OFFLINE", "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from align_backends import DemucsSeparator, QwenAligner, Wav2Vec2Aligner  # noqa: E402
from p0_common import (  # noqa: E402
    LANGS,
    AlignResult,
    TokenSpan,
    boundary_metrics,
    dump_json,
    load_audio_16k,
    tokenize,
)


# --------------------------------------------------------------------------
# 数据装载
# --------------------------------------------------------------------------


def items_from_synth(synth_dir: Path, langs: list[str] | None, variants: list[str] | None) -> list[dict]:
    mf = synth_dir / "manifest.json"
    if not mf.exists():
        raise SystemExit(f"找不到 {mf}，请先运行 gen_synth.py")
    data = json.loads(mf.read_text(encoding="utf-8"))
    out = []
    for lang, L in data["langs"].items():
        if langs and lang not in langs:
            continue
        for vname, V in L["variants"].items():
            if variants and vname not in variants:
                continue
            out.append(
                {
                    "id": f"synth_{lang}_{vname}",
                    "lang": lang,
                    "source": "synth",
                    "tokens": list(L["tokens"]),
                    "audio": V["audio"],
                    "truth": [TokenSpan(**t) for t in V["truth"]],
                    "has_accompaniment": V["bed"] is not None,
                    "note": f"gap={V['gap_seconds']}s bed={V['bed']}",
                }
            )
    return out


def items_from_songs(songs_dir: Path, langs: list[str] | None) -> list[dict]:
    sf = songs_dir / "songs.json"
    if not sf.exists():
        raise SystemExit(f"找不到 {sf}，先用 --make-songs-template 生成模板并放入歌曲")
    specs = json.loads(sf.read_text(encoding="utf-8"))
    out = []
    for s in specs:
        if s.get("skip"):
            continue
        lang = s["lang"]
        if langs and lang not in langs:
            continue
        spec = LANGS[lang]
        audio = s["audio"]
        if not Path(audio).exists():
            print(f"  !! 跳过 {s['id']}：音频不存在 {audio}")
            continue
        ly = s.get("lyrics_file")
        if ly and Path(ly).exists():
            text = Path(ly).read_text(encoding="utf-8")
        else:
            text = s.get("lyrics", "")
        tokens: list[str] = []
        for line in text.splitlines():
            tokens.extend(tokenize(line, spec))
        if not tokens:
            print(f"  !! 跳过 {s['id']}：未解析出任何 token")
            continue
        out.append(
            {
                "id": s["id"],
                "lang": lang,
                "source": "song",
                "tokens": tokens,
                "audio": audio,
                "truth": None,
                "has_accompaniment": True,
                "note": s.get("note", ""),
            }
        )
    return out


SONGS_TEMPLATE = [
    {
        "id": "cn_01_slow",
        "lang": "zh",
        "audio": "D:/PUT/YOUR/FILE/here.mp3",
        "lyrics_file": "",
        "lyrics": "",
        "note": "慢歌·长音多",
        "skip": True,
    },
    {
        "id": "cn_02_fast",
        "lang": "zh",
        "audio": "",
        "lyrics_file": "",
        "lyrics": "",
        "note": "快歌·密集咬字",
        "skip": True,
    },
    {
        "id": "en_01",
        "lang": "en",
        "audio": "",
        "lyrics_file": "",
        "lyrics": "",
        "note": "英文流行",
        "skip": True,
    },
    {
        "id": "ja_01",
        "lang": "ja",
        "audio": "",
        "lyrics_file": "",
        "lyrics": "",
        "note": "日文·假名多",
        "skip": True,
    },
    {
        "id": "ja_02_kanji",
        "lang": "ja",
        "audio": "",
        "lyrics_file": "",
        "lyrics": "",
        "note": "日文·汉字多(multi-mora 风险)",
        "skip": True,
    },
    {
        "id": "cn_03_repeat",
        "lang": "zh",
        "audio": "",
        "lyrics_file": "",
        "lyrics": "",
        "note": "重复副歌 3 遍",
        "skip": True,
    },
]


# --------------------------------------------------------------------------
# 诊断指标
# --------------------------------------------------------------------------


def diagnostics(spans: list[TokenSpan]) -> dict:
    if len(spans) < 2:
        return {"n_overlap": 0, "n_nonmonotonic": 0, "coverage": 0.0}
    ov = sum(1 for i in range(1, len(spans)) if spans[i].start < spans[i - 1].end - 1e-9)
    nm = sum(1 for i in range(1, len(spans)) if spans[i].start < spans[i - 1].start - 1e-9)
    dur = spans[-1].end - spans[0].start
    speech = sum(max(0.0, s.end - s.start) for s in spans)
    return {
        "n_overlap": ov,
        "n_nonmonotonic": nm,
        "coverage": round(speech / dur, 4) if dur > 0 else 0.0,
    }


def _span_fields(s) -> tuple[str, float]:
    """兼容 TokenSpan 对象与已序列化的 dict（results.json 里存的是 dict）。"""
    if isinstance(s, dict):
        return str(s.get("text", "")), float(s.get("start", 0.0))
    return s.text, s.start


def agreement(a: list, b: list) -> dict:
    n = min(len(a), len(b))
    d = []
    for i in range(n):
        ta, sa = _span_fields(a[i])
        tb, sb = _span_fields(b[i])
        if ta != tb:
            continue
        d.append(abs(sa - sb) * 1000.0)
    if not d:
        return {"n": 0, "ok": False}
    arr = np.array(d)
    return {
        "n": len(d),
        "ok": True,
        "mean_abs_diff_ms": round(float(arr.mean()), 2),
        "median_abs_diff_ms": round(float(np.median(arr)), 2),
        "p90_abs_diff_ms": round(float(np.percentile(arr, 90)), 2),
        "within_50ms_pct": round(float((arr <= 50).mean() * 100), 1),
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="P0 对齐验证主运行器")
    ap.add_argument("--synth", default=None, help="合成集目录（含 manifest.json）")
    ap.add_argument("--songs", default=None, help="用户音频目录（含 songs.json）")
    ap.add_argument("--out", default="out/p0")
    ap.add_argument("--langs", nargs="*", default=None)
    ap.add_argument("--variants", nargs="*", default=None)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--demucs", default="htdemucs_ft", help="htdemucs（单模型）或 htdemucs_ft（4 模型微调版，默认）")
    ap.add_argument("--models", default="models")
    ap.add_argument("--no-qwen", action="store_true")
    ap.add_argument("--no-wav2vec2", action="store_true")
    ap.add_argument("--no-vocals", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--make-songs-template", default=None)
    args = ap.parse_args()

    if args.make_songs_template:
        d = Path(args.make_songs_template)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "songs.json"
        if p.exists():
            print(f"已存在，未覆盖: {p}")
        else:
            dump_json(p, SONGS_TEMPLATE)
            print(f"模板已生成: {p}\n填入 audio / lyrics 后把 skip 改为 false 再运行。")
        return 0

    items: list[dict] = []
    if args.synth:
        items += items_from_synth(Path(args.synth), args.langs, args.variants)
    if args.songs:
        items += items_from_songs(Path(args.songs), args.langs)
    if not items:
        raise SystemExit("没有可运行的样本（检查 --synth / --songs）")
    if args.limit:
        items = items[: args.limit]

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 后端惰性加载 ----
    qwen = None
    w2v: dict[str, Wav2Vec2Aligner | None] = {}
    sep = None
    cache = Path(args.models)
    cache.mkdir(parents=True, exist_ok=True)
    hf_cache = str(cache / "hf")

    def get_qwen():
        nonlocal qwen
        if qwen is None:
            print("[load] Qwen3-ForcedAligner ...", flush=True)
            t0 = time.perf_counter()
            qwen = QwenAligner(model_dir=str(cache / "Qwen3-ForcedAligner-0.6B"), device=args.device)
            print(f"[load] qwen ok  api={qwen.api.get('call_sig')}  {time.perf_counter()-t0:.1f}s", flush=True)
        return qwen

    def get_w2v(lang: str):
        if lang not in w2v:
            name = LANGS[lang].wav2vec2_model
            if not name:
                w2v[lang] = None
            else:
                print(f"[load] wav2vec2 {name} ...", flush=True)
                t0 = time.perf_counter()
                try:
                    w2v[lang] = Wav2Vec2Aligner(name, device=args.device, cache_dir=hf_cache)
                    print(f"[load] w2v[{lang}] ok  {time.perf_counter()-t0:.1f}s", flush=True)
                except Exception as e:
                    print(f"[load] w2v[{lang}] FAILED: {type(e).__name__}: {e}", flush=True)
                    w2v[lang] = None
        return w2v[lang]

    def get_sep():
        nonlocal sep
        if sep is None:
            print(f"[load] {args.demucs} ...", flush=True)
            t0 = time.perf_counter()
            sep = DemucsSeparator(args.demucs, device=args.device)
            print(f"[load] demucs ok  {time.perf_counter()-t0:.1f}s", flush=True)
        return sep

    results = {
        "device": args.device,
        "n_items": len(items),
        "items": [],
    }

    for it in items:
        print(f"\n=== {it['id']}  lang={it['lang']}  tokens={len(it['tokens'])}  {it['note']}")
        audio = load_audio_16k(it["audio"])
        print(f"    audio {audio.size/16000:.2f}s")

        pipelines: dict[str, np.ndarray] = {"mix": audio}
        if args.no_vocals:
            pass
        elif not it.get("has_accompaniment", True):
            # clean 条件本身就没有伴奏，分离没有对象，跳过以省时
            print("    无伴奏（clean 条件），跳过人声分离")
        else:
            try:
                t0 = time.perf_counter()
                voc, _ = get_sep().separate_vocals(audio)
                dt = time.perf_counter() - t0
                pipelines["vocals"] = voc
                print(f"    demucs separated in {dt:.1f}s")
            except Exception as e:
                print(f"    demucs FAILED: {type(e).__name__}: {e}")

        rec = {"id": it["id"], "lang": it["lang"], "source": it["source"], "note": it["note"],
               "n_tokens": len(it["tokens"]), "pipelines": {}}

        for pname, paudio in pipelines.items():
            prec = {}
            spans_by_backend: dict[str, list[TokenSpan]] = {}

            if not args.no_qwen:
                try:
                    t0 = time.perf_counter()
                    spans = get_qwen().align(paudio, it["tokens"], it["lang"])
                    wall = time.perf_counter() - t0
                    ar = AlignResult("qwen", pname, it["lang"], spans, paudio.size / 16000, wall)
                    spans_by_backend["qwen"] = spans
                    prec["qwen"] = {"metrics": boundary_metrics(it["truth"], spans) if it["truth"] else None,
                                    "diag": diagnostics(spans), **ar.to_dict()}
                    print(f"    [{pname}/qwen] n={len(spans)} {wall:.1f}s")
                except Exception as e:
                    prec["qwen"] = {"error": f"{type(e).__name__}: {e}"}
                    print(f"    [{pname}/qwen] ERROR {type(e).__name__}: {e}")

            if not args.no_wav2vec2:
                al = get_w2v(it["lang"])
                if al is None:
                    prec["wav2vec2"] = {"error": "no model for this language / load failed"}
                else:
                    try:
                        t0 = time.perf_counter()
                        spans = al.align(paudio, it["tokens"], it["lang"])
                        wall = time.perf_counter() - t0
                        ar = AlignResult("wav2vec2", pname, it["lang"], spans, paudio.size / 16000, wall)
                        spans_by_backend["wav2vec2"] = spans
                        prec["wav2vec2"] = {
                            "metrics": boundary_metrics(it["truth"], spans) if it["truth"] else None,
                            "diag": diagnostics(spans),
                            "oov": al.oov,
                            "oov_rate": round(al.oov / max(1, al.total) * 100, 1),
                            **ar.to_dict(),
                        }
                        print(f"    [{pname}/wav2vec2] n={len(spans)} oov={al.oov} {wall:.1f}s")
                    except Exception as e:
                        prec["wav2vec2"] = {"error": f"{type(e).__name__}: {e}"}
                        print(f"    [{pname}/wav2vec2] ERROR {type(e).__name__}: {e}")

            if len(spans_by_backend) == 2:
                prec["agreement"] = agreement(spans_by_backend["qwen"], spans_by_backend["wav2vec2"])

            rec["pipelines"][pname] = prec

        results["items"].append(rec)

    # ---- 管线间一致性（mix vs vocals）----
    for rec in results["items"]:
        for be in ("qwen", "wav2vec2"):
            ra = rec["pipelines"].get("mix", {}).get(be, {})
            rb = rec["pipelines"].get("vocals", {}).get(be, {})
            sa = ra.get("spans")
            sb = rb.get("spans")
            if sa and sb:
                rec.setdefault("pipeline_diff", {})[be] = agreement(sb, sa)

    dump_json(out_dir / "results.json", results)
    print(f"\n结果 -> {out_dir/'results.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
