"""诊断工具：转储 Qwen 对日文的原始单元切分，并与合成集真值逐字对照。
用途：验证「多字单元内字符落到单元之外」这一固有限制。
用法：python tests/probe_ja_units.py
"""
import json, sys, unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

man = json.loads((ROOT / "data/synth/manifest.json").read_text(encoding="utf-8"))
ja = man["langs"]["ja"]
truth = ja["variants"]["gapped_clean"]["truth"]

from align_backends import QwenAligner
from p0_common import load_audio_16k, SR

audio = load_audio_16k(ROOT / ja["variants"]["gapped_clean"]["audio"])
al = QwenAligner(model_dir=str(ROOT / "models/Qwen3-ForcedAligner-0.6B"), device="cuda")

tokens = ja["tokens"]
texts = {
    "join(原样)": "".join(tokens),
    "空格分隔": " ".join(tokens),
    "全角空格": "\u3000".join(tokens),
}

out = []
out.append(f"真值（{len(truth)} 个 token）:")
for i, t in enumerate(truth):
    out.append(f"  {i:>2} {t['text']:<3} {t['start']:>7.3f} - {t['end']:>7.3f}  ({t['end']-t['start']:.3f})")

for label, txt in texts.items():
    out.append("")
    out.append("=" * 90)
    out.append(f"## 输入文本 [{label}] = {txt!r}")
    try:
        units = al.align(audio, txt.split() if " " in txt or "\u3000" in txt else tokens, "ja")
    except Exception as e:
        out.append(f"  align 失败: {type(e).__name__}: {e}")
        continue
    out.append(f"  -> {len(units)} 个单元")
    for i, u in enumerate(units):
        out.append(f"  {i:>2} {u.text!r:<12} {u.start:>7.3f} - {u.end:>7.3f}  ({u.end-u.start:.3f})")
    # 与真值比：若单元数 == token 数则能逐字比
    if len(units) == len(truth):
        err = [abs(u.start - t["start"]) * 1000 for u, t in zip(units, truth)]
        out.append(f"  逐字起始 MAE = {sum(err)/len(err):.1f} ms")

Path(ROOT / "out" / "p1_e2e" / "ja_raw_units.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
