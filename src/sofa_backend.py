"""SOFA（Singing-Oriented Forced Aligner）后端：歌词文本 → 字级时间轴。

通用对齐流程：
  歌词文本 → pykakasi(漢字→假名) → 音素序列（模型词表形式：ma→m a、し→sh i、
  つ→c u、ん→n；促音→重复下一辅音、长音→重复元音）
  → SOFA(Dictionary G2P, 整曲 match 模式) → TextGrid 音素时间
  → 音素名两指针匹配 → 音素 → 原文字符聚合 → 字级 (char, start, end, conf)

SOFA 推理使用当前系统 Python，通过子进程调用。
检查点/词典统一放 models\sofa\ja\（随项目迁移）。

已知近似：多摩拉漢字（一字多音）的字内音素分配按假名长度比例，
字内边界可能偏差 ~1 mora；fugashi+unidic 可作为精确化升级路径。
"""
from __future__ import annotations

import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOFA_DIR = ROOT / "tools" / "SOFA"
SOFA_PY = Path(sys.executable)
CKPT = ROOT / "models" / "sofa" / "ja" / "jpn_test2_plus.ckpt"
JA_DICT = ROOT / "models" / "sofa" / "ja" / "japanese-extension-sofa.txt"

# ---------------------------------------------------------------- G2P ----
# 日语 mora → 模型词表音素（multilingual/JA 检查点的声母/韵母形式）
_MORA: dict[str, tuple[str, ...]] = {}
_VOW = "aiueo"
_ROWS = [
    ("k", "かきくけこ"), ("s", "さしすせそ"), ("t", "たちつてと"),
    ("n", "なにぬねの"), ("h", "はひふへほ"), ("m", "まみむめも"),
    ("r", "らりるれろ"), ("g", "がぎぐげご"), ("z", "ざじずぜぞ"),
    ("d", "だぢづでど"), ("b", "ばびぶべぼ"), ("p", "ぱぴぷぺぽ"),
    ("v", "ヴぁヴぃヴぅヴぇヴぉ"),
]
for _c, _row in _ROWS:
    for _v, _ch in zip(_VOW, _row):
        _MORA[_ch] = (_c, _v)
        if _c in ("sh", "ch", "j"):
            continue
        for _s, _sv in (("ゃ", "a"), ("ゅ", "u"), ("ょ", "o")):
            if _v == _sv:
                continue
            _MORA[_ch + _s] = (_c, "y", _sv)     # きゃ → k y a
# 特殊假名覆盖（模型词表形式：し=sh i、つ=c u 等）
_MORA.update({
    "し": ("sh", "i"), "しゃ": ("sh", "a"), "しゅ": ("sh", "u"), "しょ": ("sh", "o"),
    "ち": ("ch", "i"), "ちゃ": ("ch", "a"), "ちゅ": ("ch", "u"), "ちょ": ("ch", "o"),
    "じ": ("j", "i"), "じゃ": ("j", "a"), "じゅ": ("j", "u"), "じょ": ("j", "o"),
    "ぢ": ("j", "i"), "つ": ("c", "u"), "つぁ": ("c", "a"),
    "ふ": ("f", "u"), "ふぁ": ("f", "a"), "ふぉ": ("f", "o"),
    "うぃ": ("w", "i"), "うぇ": ("w", "e"), "てぃ": ("t", "i"), "でぃ": ("d", "i"),
    "とぅ": ("t", "u"), "どぅ": ("d", "u"), "てゅ": ("t", "y", "u"),
    "ァ": ("a",), "ィ": ("i",), "ゥ": ("u",), "ェ": ("e",), "ォ": ("o",),
    "ん": ("n",),
})
# 片假名镜像（平假名 0x3041-0x3096 → 片假名 0x30A1-0x30F6）
for _k in list(_MORA):
    _kt = "".join(chr(ord(c) + 0x60) if 0x3041 <= ord(c) <= 0x3096 else c
                  for c in _k)
    _MORA.setdefault(_kt, _MORA[_k])


def to_phonemes(text: str) -> tuple[list[str], list[int]]:
    """文本 → (音素序列, 每音素的原文字符索引)。

    促音っ→重复下一个辅音；长音ー→重复前一 mora 的元音；标点跳过。
    """
    import pykakasi

    kk = pykakasi.kakasi()
    conv = kk.getConverter()
    phonemes: list[str] = []
    owners: list[int] = []
    offset = 0                        # piece 的 orig 在整行文本中的起点
    for piece in conv.convert(text):
        orig, kana = piece["orig"], piece["kana"]
        # piece 内：假名 → 音素组（记录假名位置；None=促音占位）
        pm: list[tuple[int, tuple[str, ...] | None]] = []
        i = 0
        while i < len(kana):
            two = kana[i:i + 2]
            one = kana[i]
            if two in _MORA and len(two) == 2:
                pm.append((i, _MORA[two])); i += 2
            elif one in _MORA:
                pm.append((i, _MORA[one])); i += 1
            elif one == "ー" and pm:
                pm.append((i, (pm[-1][1][-1],))); i += 1
            else:
                i += 1
        # 促音倍化：'っ' 后紧跟 mora 的首个辅音重复一次（kka → k k a）
        fixed: list[tuple[int, tuple[str, ...]]] = []
        for kpos, phs in pm:
            if phs is None:
                fixed.append((kpos, None))
            elif fixed and fixed[-1][1] is None:
                fixed[-1] = (fixed[-1][0], (phs[0],) + phs)
                fixed.append((kpos, phs))
            else:
                fixed.append((kpos, phs))
        # 输出音素 + 原字符归属
        for kpos, phs in fixed:
            if phs is None:
                continue
            ci = offset + _char_index(orig, kana, kpos)
            for ph in phs:
                phonemes.append(ph)
                owners.append(ci)
        offset += len(orig)
    return phonemes, owners


def _char_index(orig: str, kana: str, kpos: int) -> int:
    """piece 内假名位置 kpos → 原文字符索引（按假名长度比例）。"""
    if n_k := len(kana):
        return min(len(orig) - 1, int(kpos / n_k * len(orig)))
    return 0


# ---------------------------------------------------------------- 可用性 ----
def is_available() -> tuple[bool, str]:
    if not SOFA_PY.exists():
        return False, "当前 Python 解释器不可用"
    if not CKPT.exists():
        return False, "缺少检查点 models/sofa/ja/jpn_test2_plus.ckpt"
    if not JA_DICT.exists():
        return False, "缺少词典 models/sofa/ja/japanese-extension-sofa.txt"
    return True, ""


# ---------------------------------------------------------------- TextGrid ----
def _parse_textgrid(path: Path) -> list[tuple[float, float, str]]:
    """迷你 TextGrid 解析：phones 层的 (xmin, xmax, mark)。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    out: list[tuple[float, float, str]] = []
    cur_name = ""
    pat_int = re.compile(
        r'xmin\s*=\s*([\d.eE+-]+)\s*\n\s*xmax\s*=\s*([\d.eE+-]+)\s*\n'
        r'\s*text\s*=\s*"([^"]*)"')
    for m in re.finditer(r'name\s*=\s*"([^"]*)"|' + pat_int.pattern, text):
        if m.group(1) is not None:
            cur_name = m.group(1)
            continue
        if "phone" in cur_name.lower() or "ph" in cur_name.lower():
            out.append((float(m.group(2)), float(m.group(3)), m.group(4)))
    return out


def _load_conf(work_dir: Path) -> dict[str, str]:
    """confidence/song.csv → {phoneme: 'conf 字符串'}。"""
    conf: dict[str, str] = {}
    cc = work_dir / "confidence" / "song.csv"
    if cc.exists():
        with open(cc, encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 3:
                    conf[row[1].strip()] = row[2]
    return conf


# ---------------------------------------------------------------- 主入口 ----
def sofa_align_lyrics(vocals_wav: str, lyrics_lines: list[str], work_dir: Path,
                      progress=None, mode: str = "match") -> list[dict]:
    """整曲 SOFA 对齐 → 逐行字级时间。

    返回 [{text, start, end, chars: [{ch, start, end, conf}], moras_hit,
    moras_total}]；`vocals_wav` 应为 44.1k 人声干声（demucs stem）。
    """
    avail, why = is_available()
    if not avail:
        raise RuntimeError("SOFA 环境不可用：" + why)
    work_dir = Path(work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # 整曲模式：全部歌词的音素展开为全局序列，owners 记录字符归属
    all_ph: list[str] = []
    all_owners: list[int] = []        # 每音素 → 全局字符索引（-1=SP）
    lines_meta: list[dict] = []
    char_off = 0
    for idx, text in enumerate(lyrics_lines):
        phs, owners = to_phonemes(text)
        n = len(text)
        if len(phs) < 2:
            lines_meta.append({"text": text, "off": char_off, "n": n, "np": 0})
            char_off += n
            continue
        for p, o in zip(phs, owners):
            all_ph.append(p)
            all_owners.append(char_off + o)
        all_ph.append("SP")
        all_owners.append(-1)
        lines_meta.append({"text": text, "off": char_off, "n": n, "np": len(phs)})
        char_off += n

    # 整曲人声（match 模式在全曲内定位歌词）
    shutil.copyfile(vocals_wav, work_dir / "song.wav")
    (work_dir / "song.lab").write_text("song", encoding="utf-8")

    # 任务词典：JA_DICT 保留 mora 键展开 + "song" → 全局音素串
    job_dict = work_dir / "job_dict.txt"
    with open(job_dict, "w", encoding="utf-8") as f:
        f.write(JA_DICT.read_text(encoding="utf-8"))
        f.write("song\t" + " ".join(all_ph) + "\n")

    def prog(p, m):
        if progress:
            progress(p, m)

    prog(0.1, "SOFA 推理中（整曲 match 模式）…")
    cmd = [str(SOFA_PY), str(SOFA_DIR / "infer.py"),
           "--ckpt", str(CKPT), "--folder", str(work_dir),
           "--g2p", "Dictionary", "--dictionary", str(job_dict),
           "--mode", mode, "--out_formats", "textgrid", "--save_confidence"]
    sofa_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, capture_output=True, cwd=str(SOFA_DIR), env=sofa_env)
    log = ((r.stdout or b"") + b"\n===STDERR===\n" +
           (r.stderr or b"")).decode("utf-8", "replace")
    (work_dir / "_infer.log").write_text(log, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"SOFA 推理失败 rc={r.returncode}（详见 _infer.log）")
    prog(0.6, "解析 TextGrid…")

    conf = _load_conf(work_dir)

    # TextGrid 音素时间与全局音素序列两指针匹配
    items = _parse_textgrid(work_dir / "TextGrid" / "song.TextGrid")
    ti = 0
    ph_time: list[tuple[float, float] | None] = [None] * len(all_ph)
    for gi, ph in enumerate(all_ph):
        while ti < len(items) and items[ti][2] != ph:
            ti += 1
        if ti >= len(items):
            break
        ph_time[gi] = (items[ti][0], items[ti][1])
        ti += 1

    results: list[dict] = []
    for meta in lines_meta:
        off, n = meta["off"], meta["n"]
        chars: dict[int, dict] = {}
        hit = 0
        for gi, oi in enumerate(all_owners):
            if not (off <= oi < off + n):
                continue
            pt = ph_time[gi]
            if pt is None:
                continue
            hit += 1
            rec = chars.setdefault(oi - off, {"start": pt[0], "end": pt[1],
                                              "conf": []})
            rec["start"] = min(rec["start"], pt[0])
            rec["end"] = max(rec["end"], pt[1])
            ph_name = all_ph[gi]
            if ph_name in conf:
                rec["conf"].append(conf[ph_name])
        out_chars = []
        for pos, rec in sorted(chars.items()):
            ch = meta["text"][pos] if pos < len(meta["text"]) else "?"
            out_chars.append({"ch": ch, "start": round(rec["start"], 3),
                              "end": round(rec["end"], 3),
                              "conf": round(sum(rec["conf"]) / len(rec["conf"]), 3)
                              if rec["conf"] else None})
        results.append({"text": meta["text"], "start": None, "end": None,
                        "chars": out_chars, "moras_hit": hit,
                        "moras_total": meta["np"]})
    return results
