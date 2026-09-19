"""微调重渲染（restyle）回归测试：平移 / 整单元高亮 / 样式覆盖三条路径。
不加载任何模型。用法：python tests/restyle_test.py
"""
import json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pipeline import RestyleRequest, load_job, restyle

JD = ROOT / "out" / "p1_e2e" / "ja_clean"
job, lines = load_job(JD)
out = []
out.append(f"job.json keys: {sorted(job.keys())}")
out.append(f"原始行数: {len(lines)}   偏移前第0行: {lines[0].start:.3f}-{lines[0].end:.3f}")
out.append(f"token[0..3]: " + ", ".join(
    f"{t.text}(unit={t.unit},{t.start:.3f})" for t in lines[0].tokens[:4]))


def kf_count(ass_path):
    txt = Path(ass_path).read_text(encoding="utf-8-sig")
    lyric = [l for l in txt.splitlines() if l.startswith("Dialogue: 0,")]
    return sum(len(re.findall(r"\\kf\d+", l)) for l in lyric), len(lyric)


# ---- ① 仅平移：第 0 行 +150ms ----
r1 = restyle(JD, RestyleRequest(line_offsets_ms={0: 150}))
job2, lines2 = load_job(JD)   # 注意 load_job 读的是 align.json（原始），未变
ass1 = Path(r1["ass"])
k1, n1 = kf_count(ass1)
out.append("")
out.append(f"① 平移 +150ms -> v{r1['version']}  {ass1.name}")
out.append(f"   卡拉OK事件数 {n1}，\\kf 段数 {k1}，health={r1['health']}")
txt = ass1.read_text(encoding="utf-8-sig")
first = [l for l in txt.splitlines() if l.startswith("Dialogue: 0,")][0]
out.append(f"   首行事件: {first[:150]}")

# ---- ② 整单元高亮 ----
r2 = restyle(JD, RestyleRequest(overrides={"group_same_unit": True}, line_offsets_ms={}))
ass2 = Path(r2["ass"])
k2, n2 = kf_count(ass2)
out.append("")
out.append(f"② 整单元高亮 -> v{r2['version']}  {ass2.name}")
out.append(f"   卡拉OK事件数 {n2}，\\kf 段数 {k2}  （应少于 ①的 {k1}）")
txt2 = ass2.read_text(encoding="utf-8-sig")
for l in [x for x in txt2.splitlines() if x.startswith("Dialogue: 0,")]:
    body = l.split(",,", 1)[1]
    hunks = re.findall(r"\{[^}]*\}([^{]*)", body)
    out.append(f"   {[h for h in hunks if h]}")

# ---- ③ 样式覆盖：换字体/配色/关预览 ----
r3 = restyle(JD, RestyleRequest(overrides={
    "font": "Noto Sans JP", "font_size": 80,
    "sung_color": "#00E5A0", "unsung_color": "#DDDDDD",
    "next_line": False, "lead_ms": 500, "tail_ms": 500}))
txt3 = Path(r3["ass"]).read_text(encoding="utf-8-sig")
sty = [l for l in txt3.splitlines() if l.startswith("Style: LYRIC")]
out.append("")
out.append(f"③ 样式覆盖 -> v{r3['version']}")
out.append(f"   {sty[0] if sty else 'NO STYLE'}")
out.append(f"   NEXT 样式存在: {'Style: NEXT' in txt3}（next_line=False 时应仍定义但无事件）")
out.append(f"   NEXT 事件数: {len([l for l in txt3.splitlines() if l.startswith('Dialogue: -1,')])}")
out.append(f"   输出的成片文件：")
for k in ("video", "ass", "srt"):
    p = Path(r3[k])
    out.append(f"     {p.name}  {p.stat().st_size/1024:.1f} KB  exists={p.exists()}")

Path(ROOT / "out" / "p1_e2e" / "restyle_test.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
