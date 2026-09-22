"""本地 WebUI —— 上传 → 生成卡拉OK → 微调 → 导出

运行环境：系统默认 Python 3.11+。WebUI 使用标准库 HTTP 服务。

能力
----
  POST /api/run        multipart 上传并启动任务（后台线程），立即返回 job id
  GET  /api/events     SSE 推送进度与日志
  POST /api/cancel     取消任务
  GET  /api/align      取回逐行/逐 token 时间轴，供人工微调
  POST /api/rerender   应用偏移与样式改动后重新出片（不重新对齐，秒级）
  GET  /files/<job>/<name>   带 Range 支持的文件服务（供 <video> 拖动进度）
  GET  /api/meta       可用字体、模型、默认参数

用法：
  python src/webui.py                 # 默认 http://127.0.0.1:7870
  python src/webui.py --port 8000 --no-open
"""

from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

ROOT = _HERE.parent
ASSETS = _HERE / "webui_assets"
OUT_ROOT = ROOT / "out" / "webui"
MAX_BODY = 3 * 1024 ** 3          # 3 GB 上传上限

from alignment_policy import resolve_rules, schema
from whisper_align import DEFAULT_RULES, postprocess_lines

import model_paths  # noqa: E402,F401  （模型统一在项目 models\ 下，须早于模型加载）

# 明确核实过存在、且确实含 CJK 字形的字体（family 名，非文件名）
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/msyh.ttc", "Microsoft YaHei"),
    ("C:/Windows/Fonts/simhei.ttf", "SimHei"),
    ("C:/Windows/Fonts/Deng.ttf", "DengXian"),
    ("C:/Windows/Fonts/simsun.ttc", "SimSun"),
    ("C:/Windows/Fonts/NotoSansSC-VF.ttf", "Noto Sans SC"),
    ("C:/Windows/Fonts/NotoSansJP-VF.ttf", "Noto Sans JP"),
    ("C:/Windows/Fonts/YuGothR.ttc", "Yu Gothic"),
    ("C:/Windows/Fonts/meiryo.ttc", "Meiryo"),
    ("C:/Windows/Fonts/msgothic.ttc", "MS Gothic"),
    ("C:/Windows/Fonts/malgun.ttf", "Malgun Gothic"),
    ("C:/Windows/Fonts/STXIHEI.TTF", "STXihei"),
    ("C:/Windows/Fonts/FZYTK.TTF", "FZYaoti"),
]

DEFAULT_OPTIONS = {
    "font": "Microsoft YaHei",
    "font_size": 66,
    "sung_color": "#FFD24A",
    "unsung_color": "#FFFFFF",
    "outline": 3.0,
    "margin_v": 118,
    "next_line": True,
    "lead_ms": 320,
    "tail_ms": 320,
    "min_gap_ms": 12,
}


# ==========================================================================
# 任务状态
# ==========================================================================


@dataclass
class Job:
    id: str
    dir: Path
    state: str = "queued"        # queued|running|done|error|cancelled
    progress: float = 0.0
    logs: list[str] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    cancel_req: bool = False
    created: float = field(default_factory=time.time)
    asr_lrc: str | None = None   # 无歌词文件时自动转写出的歌词草稿
    asr_info: dict | None = None
    whisper_lrc: str | None = None   # whisper 对齐产出的逐字增强 LRC
    whisper_info: dict | None = None
    sofa_info: dict | None = None    # SOFA 对齐诊断

    def add(self, frac: float, msg: str) -> None:
        self.progress = max(0.0, min(1.0, float(frac)))
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"{stamp}  {msg}")


JOBS: dict[str, Job] = {}
LOCK = threading.Lock()
EDIT_LOCK = threading.Lock()


def saved_directory(identifier):
    from local_history import resolve
    return resolve(identifier, ROOT / 'out', OUT_ROOT)


class _JobPhase:
    """Forward job data while mapping a chained prepass into overall progress."""
    def __init__(self, job, offset, scale):
        object.__setattr__(self, "_job", job)
        object.__setattr__(self, "_offset", offset)
        object.__setattr__(self, "_scale", scale)

    def __getattr__(self, name):
        return getattr(self._job, name)

    def __setattr__(self, name, value):
        setattr(self._job, name, value)

    def add(self, fraction, message):
        self._job.add(self._offset + self._scale * fraction, message)


# ==========================================================================
# multipart
# ==========================================================================


def parse_multipart(body: bytes, boundary: bytes):
    """解析 multipart/form-data。stdlib 的 cgi 模块在 3.13 已移除，故自带。"""
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk.startswith(b"--"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        name = fname = None
        for line in head.split(b"\r\n"):
            if not line.lower().startswith(b"content-disposition"):
                continue
            s = line.decode("utf-8", "replace")
            m = re.search(r'name="([^"]*)"', s)
            if m:
                name = m.group(1)
            # RFC 5987：非 ASCII 文件名走 filename*=UTF-8''%E4%B8%AD...
            m2 = re.search(r"filename\*=([^;]+)", s)
            if m2:
                v = m2.group(1).strip()
                if "''" in v:
                    v = v.split("''", 1)[1]
                fname = urllib.parse.unquote(v, encoding="utf-8")
            else:
                m1 = re.search(r'filename="([^"]*)"', s)
                if m1:
                    fname = m1.group(1)
        if name is None:
            continue
        if fname:
            files[name] = (fname, data)
        else:
            fields[name] = data.decode("utf-8", "replace")
    return fields, files


def safe_name(n: str) -> str:
    n = Path(n).name
    n = re.sub(r"[^\w\u4e00-\u9fff.\-\s]+", "_", n).strip("._ ")
    return n[:120] or "file"


# ==========================================================================
# 任务执行
# ==========================================================================


def _run_job(job: Job, cfg_kwargs: dict) -> None:
    from pipeline import ModelCache, PipelineConfig, AssOptions, run

    # 进度条分配：SOFA/whisper 对齐占前 35%，ASR 草稿占前 45%（三者互斥）
    need_sofa = bool(cfg_kwargs.get("sofa_align"))
    need_whisper = bool(cfg_kwargs.get("whisper_align"))
    need_asr = (not need_sofa and not need_whisper
                and bool(cfg_kwargs.get("asr_lyrics")) and not (
                    cfg_kwargs.get("lyrics_path")
                    or (cfg_kwargs.get("lyrics_text") or "").strip()))
    base = 0.35 if (need_sofa or need_whisper) else (0.75 if need_asr else 0.0)

    def prog(frac: float, msg: str) -> None:
        with LOCK:
            job.add(base + (1.0 - base) * frac, msg)

    def cancel() -> bool:
        return job.cancel_req

    with LOCK:
        job.state = "running"
        job.add(0.0, "任务启动")
    try:
        if need_sofa:
            if cancel():
                raise RuntimeError("cancelled")
            _sofa_prepass(job, cfg_kwargs, cancel)
        elif need_whisper:
            if cancel():
                raise RuntimeError("cancelled")
            _whisper_prepass(job, cfg_kwargs, cancel)
        if need_asr:
            if cancel():
                raise RuntimeError("cancelled")
            _asr_prepass(job, cfg_kwargs, cancel)
            # Concert benchmark: Qwen's text draft is useful, but its singing
            # timestamps often collapse. Automatically realign the draft using
            # the same Whisper route as supplied lyrics, without a manual step.
            if cancel():
                raise RuntimeError("cancelled")
            cfg_kwargs["lyrics_text"] = (job.dir / "asr_lyrics.plain.txt").read_text(encoding="utf-8")
            cfg_kwargs.setdefault("alignment_profile", "balanced")
            cfg_kwargs.setdefault("align_dual", True)
            cfg_kwargs.setdefault("align_on_vocals", True)
            cfg_kwargs.setdefault("vocal_guide", True)
            with LOCK:
                job.add(0.45, "[ASR] 草稿已生成；自动用 Whisper 重新对齐，不采信 Qwen 演唱时间戳")
            _whisper_prepass(_JobPhase(job, 0.45, 0.30 / 0.35), cfg_kwargs, cancel)
            with LOCK:
                job.asr_info = {**(job.asr_info or {}), "timing_source": "whisper",
                                "lyrics_verified": False, "raw_alignment_discarded": True}

        ass = AssOptions(**_ass_kwargs(cfg_kwargs.get("options") or {}))
        cfg = PipelineConfig(
            media=cfg_kwargs["media"],
            lyrics_path=cfg_kwargs.get("lyrics_path"),
            lyrics_text=cfg_kwargs.get("lyrics_text") or "",
            audio_track=cfg_kwargs.get("audio_track"),
            vocal_mode=cfg_kwargs.get("vocal_mode", "remove"),
            lang=cfg_kwargs.get("lang", "auto"),
            separate=bool(cfg_kwargs.get("separate", True)),
            demucs_model=cfg_kwargs.get("demucs", "htdemucs_ft"),
            backend=cfg_kwargs.get("backend", "qwen"),
            device=cfg_kwargs.get("device", "cuda"),
            out_dir=str(OUT_ROOT),
            job_name=job.id,
            timed_mode=cfg_kwargs.get("timed_mode", "warp"),
            encoder=cfg_kwargs.get("encoder", "auto"),
            quality=int(cfg_kwargs.get("quality", 21)),
            ass=ass,
        )
        res = run(cfg, progress=prog, cancel=cancel, cache=_CACHE)
        with LOCK:
            if res.ok:
                job.state = "done"
                job.progress = 1.0
                job.result = _result_payload(job, res.video, res.ass, res.srt,
                                            res.align_json, res.stats)
            else:
                job.state = "cancelled" if cancel() else "error"
                job.error = res.error
                for line in res.log[-6:]:
                    job.logs.append(line)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            job.state = "cancelled" if cancel() else "error"
            job.error = f"{type(e).__name__}: {e}"
            job.logs.append(traceback.format_exc()[-1200:])
    finally:
        (job.dir/'history_status.json').write_text(json.dumps({
            'state':job.state, 'error':job.error, 'created':job.created,
            'result':job.result, 'logs':job.logs}, ensure_ascii=False), encoding='utf-8')



def _vc_thr(rules: dict | None) -> float:
    """人声活跃阈值（峰值比例）——规则可覆盖，默认 0.12。"""
    try:
        return float((rules or {}).get("vocal_thr", 0.12))
    except (TypeError, ValueError):
        return 0.12


def _vc_rel(rules: dict | None) -> float:
    """相对能量剔除阈值（dB）——规则可覆盖，默认 6.0；设 0 关闭。"""
    try:
        return float((rules or {}).get("rel_drop_db", 6.0))
    except (TypeError, ValueError):
        return 6.0


def _audio44(job: Job, cfg_kwargs: dict, tag: str = "align",
             frac: float = 0.03) -> Path:
    """44.1kHz 立体声母音频 —— whisper / SOFA 两个预对齐阶段共用一份。

    以前每个阶段各抽一份（whisper_44k.wav / sofa_44k.wav，pipeline 又抽
    source_audio.wav），统一到
    ``in/audio_44k.wav``，并兼容复用旧任务目录里的旧文件名。
    """
    from pipeline import extract_wav

    ind = job.dir / "in"
    ind.mkdir(parents=True, exist_ok=True)
    for name in ("audio_44k.wav", "whisper_44k.wav", "sofa_44k.wav"):
        p = ind / name
        if p.exists():
            return p
    p = ind / "audio_44k.wav"
    with LOCK:
        job.add(frac, f"[{tag}] 抽取音频 44.1kHz 立体声…")
    extract_wav(cfg_kwargs["media"], p, sr=44100, mono=False)
    return p


def _vocals_stem(job: Job, audio: Path, cfg_kwargs: dict,
                 tag: str = "align", frac: float = 0.05) -> tuple[Path, Path]:
    """人声 / 伴奏干声（demucs 一个任务只跑一次，落在 ``in/vg/``）。

    对齐（干声更干净）、人声能量包络、``vocal_mode=remove`` 的输出音轨
    三者共用同一份；pipeline 也会复用这里的产物，不再重复分离。
    """
    from pipeline import separate_stems

    vg = job.dir / "in" / "vg"
    voc, acc = vg / "vocals.wav", vg / "accompaniment.wav"
    if voc.exists() and acc.exists():
        return voc, acc          # SOFA 预分离之后 whisper 阶段直接复用
    with LOCK:
        job.add(frac, f"[{tag}] 分离人声（demucs，约 30-60s）…")
    v, a = separate_stems(str(audio), vg, cfg_kwargs.get("demucs", "htdemucs_ft"),
                          cfg_kwargs.get("device", "cuda"))
    return Path(v), Path(a)


def _sofa_prepass(job: Job, cfg_kwargs: dict, cancel) -> None:
    """SOFA（歌声专用）细化：whisper 行窗口内用音素级对齐替换字级时间。

    基准：SOFA 整曲 match 模式在带长间奏/重复副歌的歌上
    不可靠，必须依赖 whisper 的行窗口。流程 = whisper 行窗口 → 每行切人声段
    → SOFA 音素级对齐 → 字级时间替换（窗口内精修）。使用当前系统 Python。
    """
    from sofa_backend import sofa_align_lyrics
    from asr_lyrics import AsrLine, Segment, to_enhanced_lrc, to_plain, to_srt
    from whisper_align import (cap_char_durations, clamp_tails, collapse_zeroconf,
                               enforce_timing, extend_tails,
                               prune_intervals_by_tx, relocate_lowconf,
                               transcribe_check, vocal_guide, vocal_intervals)

    lyrics = (cfg_kwargs.get("lyrics_text") or "").strip()
    if not lyrics and cfg_kwargs.get("lyrics_path"):
        raw = Path(cfg_kwargs["lyrics_path"]).read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "cp932", "latin-1"):
            try:
                lyrics = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    rows = [l.strip() for l in lyrics.splitlines() if l.strip()]
    if not rows:
        raise RuntimeError("SOFA 对齐需要歌词文本，但歌词为空")

    with LOCK:
        job.add(0.02, "[sofa] 歌词 {0} 行（whisper 行窗口 + SOFA 音素细化）".format(len(rows)))

    # 人声干声（SOFA 对齐在干声上进行；与 whisper 阶段共用同一份抽取与分离）
    if cancel():
        raise RuntimeError("cancelled")
    audio44 = _audio44(job, cfg_kwargs, tag="sofa")
    vocals, _acc = _vocals_stem(job, audio44, cfg_kwargs, tag="sofa")

    # 第一步：whisper 行窗口（沿用 _whisper_prepass 的全部逻辑）
    # 注：音频抽取与 demucs 分离由 _audio44/_vocals_stem 统一，
    # SOFA 与 whisper 阶段共用同一份，不再各跑一遍。
    _whisper_prepass(job, cfg_kwargs, cancel)
    if cancel():
        raise RuntimeError("cancelled")
    # 逐行窗口取自 whisper 的字级数据（对齐音频与 SOFA 阶段完全相同）
    srt = (job.dir / "whisper_lyrics.srt")
    if not srt.exists():
        raise RuntimeError("whisper 预处理未产出 whisper_lyrics.srt")
    entries = re.findall(r"(\d+):(\d+):(\d+)[,.](\d+) --> (\d+):(\d+):(\d+)[,.](\d+)"
                         r"\n(.*?)(?:\n\n|$)", srt.read_text(encoding="utf-8")
                         .replace("\r\n", "\n"), re.S)
    win = []
    for h1, m1, s1, ms1, h2, m2, s2, ms2, text in entries:
        st = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        en = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        win.append((st, en, text.strip()))

    # 第二步：每行窗口内 SOFA 音素级细化
    import soundfile as sf
    from sofa_backend import to_phonemes
    seg_dir = job.dir / "in" / "sofa_refine"
    if seg_dir.exists():
        shutil.rmtree(seg_dir, ignore_errors=True)
    seg_dir.mkdir(parents=True, exist_ok=True)
    wav, sr = sf.read(str(vocals), always_2d=True)

    dict_lines = []
    seg_meta: dict[int, tuple] = {}
    n_cut = 0
    for idx, (st, en, text) in enumerate(win):
        phs, owners = to_phonemes(text)
        if len(phs) < 2:
            continue                          # 英语行等无音素：留给 whisper 兜底
        token = f"line{idx:03d}"
        a = int(max(0, st - 0.2) * sr)
        b = int(min(en + 0.2, len(wav) / sr) * sr)
        if b - a < int(0.3 * sr):
            continue
        data, _sr = sf.read(str(vocals), start=a, stop=b, always_2d=True)
        sf.write(str(seg_dir / f"{token}.wav"), data, _sr)
        # .lab 必须写 token 名（词典的键），不能写音素序列——
        # Dictionary G2P 按 word 查词典展开音素
        (seg_dir / f"{token}.lab").write_text(token, encoding="utf-8")
        dict_lines.append(f"{token}\t{' '.join(phs)}")
        seg_meta[idx] = (st, phs, owners)
        n_cut += 1
    (seg_dir / "ja_job_dict.txt").write_text("\n".join(dict_lines), encoding="utf-8")
    with LOCK:
        job.add(0.35, f"[sofa] 细化分段 {n_cut} 段")

    # 第三步：SOFA 推理（窗口内 force 对齐，窗口已含完整演唱）
    # 使用 multilingual 检查点；行分段和 mora 音素由同一份字典生成。
    ckpt = ROOT / "models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt"
    jdict = seg_dir / "ja_job_dict.txt"
    cmd = [sys.executable,
           str(ROOT / "tools/SOFA/infer.py"), "--ckpt", str(ckpt),
           "--folder", str(seg_dir), "--g2p", "Dictionary",
           "--dictionary", str(jdict), "--mode", "force",
           "--out_formats", "textgrid", "--save_confidence"]
    sofa_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run(cmd, capture_output=True, cwd=str(ROOT / "tools/SOFA"),
                       env=sofa_env)
    (seg_dir / "_infer.log").write_text(
        (r.stdout or b"").decode("utf-8", "replace") + "\n===STDERR===\n" +
        (r.stderr or b"").decode("utf-8", "replace"), encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"SOFA 细化推理失败 rc={r.returncode}")

    # 第四步：解析 TextGrid → 用音素时间替换 whisper 字级时间
    import textgrid as tg
    rep = {"refined": 0, "failed": []}
    dmap = {}
    for ln in jdict.read_text(encoding="utf-8").splitlines():
        k, v = ln.split("\t", 1)
        dmap[k.strip()] = v.strip().split(" ")
    conf_csv = seg_dir / "confidence" / "ja_job_dict.csv"
    conf = {}
    if conf_csv.exists():
        for row in csv.reader(open(conf_csv, encoding="utf-8", errors="replace")):
            if len(row) >= 3:
                conf[row[1].strip()] = row[2]

    # 第四步：解析 TextGrid → 按 owner 聚合到字符（每字符只出现一次）
    import textgrid as tg
    rep = {"refined": 0, "failed": [], "fallback": [],
           "rules": resolve_rules(DEFAULT_RULES, cfg_kwargs.get("rules"),
                                  cfg_kwargs.get("alignment_profile", "balanced"))}
    conf_csv = seg_dir / "confidence" / "ja_job_dict.csv"
    conf = {}
    if conf_csv.exists():
        for row in csv.reader(open(conf_csv, encoding="utf-8", errors="replace")):
            if len(row) >= 3:
                conf[row[1].strip()] = row[2]

    refined: dict[int, dict] = {}
    for idx, meta in seg_meta.items():
        st, phs, owners = meta
        tgp = seg_dir / "TextGrid" / f"line{idx:03d}.TextGrid"
        if not tgp.exists():
            rep["failed"].append(idx + 1)
            continue
        grid = tg.TextGrid.fromFile(str(tgp))
        tier = next((t for t in grid.tiers if "phone" in t.name.lower()
                     or "ph" in t.name.lower()), grid.tiers[-1])
        items = [(iv.minTime, iv.maxTime, iv.mark) for iv in tier]
        ti = 0
        spans = []
        for ph in phs:
            while ti < len(items) and items[ti][2] != ph:
                ti += 1
            if ti >= len(items):
                break
            spans.append((items[ti][0], items[ti][1]))
            ti += 1
        if len(spans) < max(2, int(0.5 * len(phs))):
            rep["failed"].append(idx + 1)
            continue
        # 音素 → 字符（owners 是每音素的原文字符索引，天然去重）
        char_spans: dict[int, list] = {}
        for si, (s0, e0) in enumerate(spans):
            ci = owners[si] if si < len(owners) else 0
            rec = char_spans.setdefault(ci, [s0, e0])
            rec[0] = min(rec[0], s0)
            rec[1] = max(rec[1], e0)
        # 缺口填充：未被任何音素覆盖的字符用相邻插值
        n_char = len(win[idx][2])
        known = sorted(char_spans)
        filled: dict[int, tuple[float, float]] = {}
        for ci in range(n_char):
            if ci in char_spans:
                s0, e0 = char_spans[ci]
                filled[ci] = (max(s0, e0 - 1.2) if e0 - s0 > 1.5 else s0, e0)
                continue
            prev = max([c for c in known if c < ci], default=None)
            nxt = min([c for c in known if c > ci], default=None)
            if prev is not None and nxt is not None:
                ps, ns = char_spans[prev][1], char_spans[nxt][0]
                mid = ps + (ns - ps) * (ci - prev) / (nxt - prev)
                filled[ci] = (mid, mid + 0.05)
            elif prev is not None:
                filled[ci] = (char_spans[prev][1], char_spans[prev][1] + 0.05)
            elif nxt is not None:
                filled[ci] = (char_spans[nxt][0] - 0.05, char_spans[nxt][0])
        refined[idx] = filled
        rep["refined"] += 1

    # 最终行表：SOFA 成功的行用 SOFA 字级；失败的（含英语行）用 whisper 窗口均分兜底
    asr_lines = []
    for idx, (st, en, text) in enumerate(win):
        if idx in refined:
            segs = [Segment(text[ci], st + s0, st + e0, 1.0)
                    for ci, (s0, e0) in sorted(refined[idx].items())
                    if ci < len(text)]
        else:
            rep["fallback"].append(idx + 1)
            n = max(1, len(text))
            segs = [Segment(text[ci], st + (en - st) * ci / n,
                            st + (en - st) * (ci + 1) / n, 0.6)
                    for ci in range(len(text))]
        if not segs:
            continue
        asr_lines.append(AsrLine(text, segs[0].start, segs[-1].end, segs, 1.0))
    # 与 whisper 路线同一套尾部清扫（SOFA 行也有窗口外溢风险）
    try:
        iv2 = vocal_intervals(str(vocals), thr_factor=_vc_thr(rep.get("rules")),
                              rel_drop_db=_vc_rel(rep.get("rules")))
        if (rep.get("rules") or {}).get("tx_crosscheck", True):
            tx = transcribe_check(str(vocals), rows, language=cfg_kwargs.get("lang", "ja"),
                                  device=cfg_kwargs.get("device", "cuda"),
                                  min_match=float((rep.get("rules") or {})
                                                  .get("tx_min_match", 0.34)))
            iv2, _tx_note = prune_intervals_by_tx(
                iv2, tx["other"],
                min_overlap_s=float((rep.get("rules") or {}).get("tx_prune_min", 1.0)),
                max_seg_s=float((rep.get("rules") or {}).get("tx_max_seg", 8.0)),
                cap_frac=float((rep.get("rules") or {}).get("tx_cap_frac", 0.10)))
        postprocess_lines(asr_lines, iv2, cfg_kwargs.get("rules"),
                          cfg_kwargs.get("alignment_profile", "balanced"))
    except Exception:  # noqa: BLE001
        pass

    lrc = to_enhanced_lrc(asr_lines)
    (job.dir / "sofa_lyrics.lrc").write_text(lrc, encoding="utf-8")
    (job.dir / "sofa_lyrics.plain.txt").write_text(to_plain(asr_lines), encoding="utf-8")
    (job.dir / "sofa_lyrics.srt").write_text(to_srt(asr_lines), encoding="utf-8")
    cfg_kwargs["lyrics_path"] = str(job.dir / "sofa_lyrics.lrc")
    cfg_kwargs["lyrics_text"] = ""
    with LOCK:
        job.sofa_info = {"refined": rep["refined"], "failed": rep["failed"],
                         "fallback": rep["fallback"], "lines": len(asr_lines)}
        job.add(0.35, f"[sofa] 细化完成 {rep['refined']}/{len(win)} 行"
                      + (f"（whisper 兜底 {len(rep['fallback'])} 行）"
                         if rep["fallback"] else ""))
    diag_path = ROOT / "out" / "webui" / job.id / "sofa_diag.json"
    diag_path.parent.mkdir(parents=True, exist_ok=True)
    diag_path.write_text(
        json.dumps(job.sofa_info, ensure_ascii=False, indent=1), encoding="utf-8")


def _whisper_prepass(job: Job, cfg_kwargs: dict, cancel) -> None:
    """whisper 对齐已知歌词 -> 逐字增强 LRC（演唱/现场场景推荐）。

    产出的增强 LRC 会让后续 pipeline 走「直接采信时间戳」路径（不再声学对齐），
    因此渲染结果与 CLI 完全一致。

    干声本来就为能量包络准备，等于零额外成本；分离不可用时自动退回原始混音。
    """
    from pipeline import detect_language
    from whisper_align import (align_words, build_lines, cap_char_durations,
                               clamp_tails, collapse_zeroconf, enforce_timing,
                               extend_tails, get_model,
                               merge_lines_by_confidence,
                               prune_intervals_by_tx, relocate_lowconf,
                               transcribe_check, vocal_guide, vocal_intervals)
    from asr_lyrics import to_enhanced_lrc, to_plain, to_srt

    lang = cfg_kwargs.get("lang", "auto")
    model_size = cfg_kwargs.get("whisper_model", "large-v3")
    use_vg = bool(cfg_kwargs.get("vocal_guide", True))
    use_align_vocals = bool(cfg_kwargs.get("align_on_vocals", True))
    use_dual = bool(cfg_kwargs.get("align_dual", True))
    profile = cfg_kwargs.get("alignment_profile", "balanced")
    user_rules = resolve_rules(DEFAULT_RULES, cfg_kwargs.get("rules"), profile)

    # 取歌词文本（文本框或文件）
    lyrics = (cfg_kwargs.get("lyrics_text") or "").strip()
    if not lyrics and cfg_kwargs.get("lyrics_path"):
        raw = Path(cfg_kwargs["lyrics_path"]).read_bytes()
        for enc in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "cp932", "latin-1"):
            try:
                lyrics = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    rows = [l.strip() for l in lyrics.splitlines() if l.strip()]
    if not rows:
        raise RuntimeError("whisper 对齐需要歌词文本，但歌词为空")

    # 语言：显式指定优先，否则从歌词文本推断（比让 whisper 猜可靠）
    lang_map = {"zh": "Chinese", "en": "English", "ja": "Japanese"}
    wlang = lang_map.get(lang)
    if wlang is None:
        wlang, _ = detect_language("\n".join(rows))
    with LOCK:
        job.add(0.02, f"[whisper] 歌词 {len(rows)} 行 / 语言 {wlang} / 模型 {model_size}"
                      + ("（含人声能量引导）" if use_vg else ""))

    def wprog(f: float, m: str) -> None:
        if cancel():
            raise RuntimeError("cancelled")
        with LOCK:
            job.add(0.06 + (0.10 if use_dual else 0.24) * max(0.0, min(1.0, f)),
                    "[whisper] " + m)

    def wprog2(f: float, m: str) -> None:
        if cancel():
            raise RuntimeError("cancelled")
        with LOCK:
            job.add(0.17 + 0.10 * max(0.0, min(1.0, f)), "[whisper·混音] " + m)

    # 44.1k 立体声 wav（与 CLI 一致；与 SOFA 阶段、pipeline 共用同一份）
    if cancel():
        raise RuntimeError("cancelled")
    audio = _audio44(job, cfg_kwargs, tag="whisper")

    # 干声：对齐输入 + 能量包络共用同一份。
    # 对齐喂干声的依据（internal acoustic comparison）：平均词概率 0.627→0.670、
    # 低置信词 −17%、对齐耗时 −25%（干净录音上打平）。机理是伴奏不再抢 whisper
    # 对齐头的注意力——混音路有 11 个词起点落进「无人声」段（最远一个差 44.6s，
    # 落在 4.5s 间奏里且 p=0.00），干声路放回真实乐句起点。
    voc = None
    if use_vg or use_align_vocals or use_dual:   # 任一开关要干声，就只分离一次
        if cancel():
            raise RuntimeError("cancelled")
        try:
            voc, _acc = _vocals_stem(job, audio, cfg_kwargs, tag="vocal-guide",
                                     frac=0.05)
        except Exception as e:  # noqa: BLE001
            voc = None
            with LOCK:
                job.add(0.05, f"[vocal-guide] 分离失败，对齐退回原始混音：{e}")
    align_audio = voc if (voc is not None and (use_align_vocals or use_dual)) else audio
    with LOCK:
        job.add(0.05, "[whisper] 对齐输入 = "
                      + ("人声干声" if align_audio is not audio else "原始混音")
                      + ("（两路取优：另一路再跑一次混音）" if use_dual and voc else ""))

    words = align_words(align_audio, "\n".join(rows), language=wlang,
                        model_size=model_size, device=cfg_kwargs.get("device", "cuda"),
                        progress=wprog)
    (job.dir / "whisper_words.json").write_text(
        json.dumps(words, ensure_ascii=False), encoding="utf-8")

    asr_lines, diag = build_lines(words, rows, rules=user_rules)
    from alignment_diagnostics import snapshot, explain
    evidence_primary = snapshot(asr_lines, diag['row_index'])
    evidence_alt = {}

    # 两路取优：干声 + 混音各跑一次，逐行取词平均概率更高的一路（实测 +0.022 平均 p）
    dual_rep = None
    if use_dual and voc is not None and align_audio is voc:
        if cancel():
            raise RuntimeError("cancelled")
        alt_words = align_words(audio, "\n".join(rows), language=wlang,
                                model_size=model_size,
                                device=cfg_kwargs.get("device", "cuda"),
                                progress=wprog2)
        (job.dir / "whisper_words_mix.json").write_text(
            json.dumps(alt_words, ensure_ascii=False), encoding="utf-8")
        alt_lines, alt_diag = build_lines(alt_words, rows, rules=user_rules)
        evidence_alt = snapshot(alt_lines, alt_diag['row_index'])
        asr_lines, dual_rep = merge_lines_by_confidence(
            asr_lines, diag.get("row_index") or [], alt_lines,
            alt_diag.get("row_index") or [], len(rows))
        diag["dual"] = dual_rep
    with LOCK:
        job.add(0.30, f"[whisper] 后处理：{diag}"
                      + (f"｜两路取优：{dual_rep['n_alt']} 行取自混音" if dual_rep else ""))

    evidence_indices = dict(zip((id(l) for l in asr_lines), sorted(set(evidence_primary) | set(evidence_alt))))
    evidence_before = {id(l): (l.start, l.end) for l in asr_lines}
    vg_rep = None
    rl_rep = et_rep = None
    iv = []
    if use_vg and voc is not None:
        try:
            iv = vocal_intervals(voc, thr_factor=_vc_thr(user_rules),
                                 rel_drop_db=_vc_rel(user_rules))
            # 转写交叉验证：干声自由转写，与歌词对不上的发声段（吼叫/即兴/幻觉）
            # 从包络里剪掉——包络只懂"有人在出声"，转写才认识"词"。
            tx_rep = None
            if (user_rules or {}).get("tx_crosscheck", True):
                tx = transcribe_check(voc, rows, language=wlang,
                                      device=cfg_kwargs.get("device", "cuda"),
                                      model_size=model_size,
                                      min_match=float((user_rules or {})
                                                      .get("tx_min_match", 0.34)))
                iv, tx_note = prune_intervals_by_tx(
                    iv, tx["other"],
                    min_overlap_s=float((user_rules or {}).get("tx_prune_min", 1.0)),
                    max_seg_s=float((user_rules or {}).get("tx_max_seg", 8.0)),
                    cap_frac=float((user_rules or {}).get("tx_cap_frac", 0.10)))
                tx_rep = (f"转写 {tx['n_seg']} 段：唱歌词 {len(tx['lyric'])} / "
                          f"非歌词 {len(tx['other'])}｜{tx_note}")
            sung = sum(b - a for a, b in iv)
            with LOCK:
                job.add(0.33, f"[vocal-guide] 人声活跃 {len(iv)} 段 / {sung:.1f}s"
                              + (f"｜[{tx_rep}]" if tx_rep else ""))
        except Exception as e:  # noqa: BLE001
            with LOCK:
                job.add(0.34, f"[vocal-guide] 失败跳过：{type(e).__name__}: {e}")

    vg_rep = postprocess_lines(asr_lines, iv, user_rules, profile)
    (job.dir / 'alignment_evidence.json').write_text(json.dumps(
        explain(asr_lines, evidence_primary, evidence_alt, evidence_before, evidence_indices, iv, len(rows)),
        ensure_ascii=False), encoding='utf-8')
    diag["postprocess"] = vg_rep
    diag["alignment_profile"] = profile
    diag["suspect_lines"] = [i for i, line in enumerate(asr_lines)
                              if line.prob < user_rules["conf_low"]]
    with LOCK:
        job.add(0.34, f"[postprocess] {vg_rep}")
    lrc = to_enhanced_lrc(asr_lines)
    (job.dir / "whisper_lyrics.lrc").write_text(lrc, encoding="utf-8")
    (job.dir / "whisper_lyrics.plain.txt").write_text(to_plain(asr_lines), encoding="utf-8")
    (job.dir / "whisper_lyrics.srt").write_text(to_srt(asr_lines), encoding="utf-8")
    cfg_kwargs["lyrics_path"] = str(job.dir / "whisper_lyrics.lrc")
    # 关键：清掉文本框里的纯文本歌词。pipeline._load_lyrics 里 lyrics_text 优先于
    # lyrics_path —— 不清的话 whisper 产物会被忽略，管线拿纯文本重新跑 Qwen 对齐，
    # 在演唱上塌缩。
    cfg_kwargs["lyrics_text"] = ""
    with LOCK:
        job.whisper_lrc = lrc
        job.whisper_info = {"language": wlang, "model": model_size,
                            "vocal_guide": bool(vg_rep), "user_rules": user_rules,
                            "vocal_guide_report": vg_rep, "relocate_report": rl_rep,
                            "tail_report": et_rep, **diag}
        job.add(0.35, "[whisper] 逐字时间轴已生成（后续渲染直接采信，不再声学对齐）")
        if user_rules:
            off = [k for k, v in user_rules.items() if v is False]
            if off:
                job.add(0.35, "[whisper] 已停用规则: " + ", ".join(off))
        for i in diag.get("suspect_lines", [])[:8]:
            job.add(0.35, f"[whisper] ⚠ 第 {i + 1} 行置信度偏低，建议在第 4 步微调确认")


def _asr_prepass(job: Job, cfg_kwargs: dict, cancel) -> None:
    """本地 ASR 转写生成歌词草稿，并把它写进 cfg_kwargs['lyrics_path']。"""
    from asr_lyrics import generate_lyrics

    lang = cfg_kwargs.get("lang", "auto")
    with LOCK:
        job.add(0.01, "[ASR] 无歌词文件：启用本地 Qwen3-ASR-1.7B 转写"
                      + (f"（语言 = {lang}）" if lang != "auto"
                         else "（语言未指定，自动判别；实测可能误判，建议手动选语言）"))

    def aprog(p: float, msg: str) -> None:
        if cancel():
            raise RuntimeError("cancelled")
        with LOCK:
            job.add(0.01 + 0.43 * max(0.0, min(1.0, p)), "[ASR] " + msg)

    asr = _ensure_cache(cfg_kwargs.get("device", "cuda")).asr(lang)
    res = generate_lyrics(
        cfg_kwargs["media"], asr,
        gap_s=float(cfg_kwargs.get("asr_gap", 0.75)),
        max_chars=int(cfg_kwargs.get("asr_max_chars", 30)),
        work_dir=job.dir / "in",
        progress=aprog,
    )
    jd = job.dir
    (jd / "asr_lyrics.lrc").write_text(res.lrc, encoding="utf-8")
    (jd / "asr_lyrics.plain.txt").write_text(res.plain, encoding="utf-8")
    (jd / "asr_lyrics.srt").write_text(res.srt, encoding="utf-8")
    cfg_kwargs["lyrics_path"] = str(jd / "asr_lyrics.lrc")
    with LOCK:
        job.asr_lrc = res.lrc
        job.asr_info = {"language": res.language, **res.diag}
        job.add(0.44, "[ASR] " + res.summary())
        if res.diag.get("collapse_ratio", 0) > 0.5:
            job.add(0.44, "[ASR] ⚠ 零宽单元占比偏高：该曲目对齐退化，逐字高亮不可信，"
                          "建议以草稿校对后重跑，或改用现成歌词文件")
        job.add(0.44, "[ASR] 歌词草稿已存为 asr_lyrics.lrc（可在结果区下载校对）")


_CACHE = None


def _ensure_cache(device: str = "cuda"):
    global _CACHE
    if _CACHE is None:
        from pipeline import ModelCache
        _CACHE = ModelCache(device)
    return _CACHE


def _ass_kwargs(o: dict) -> dict:
    def col(v, dflt):
        if not v:
            return dflt
        h = str(v).lstrip("#")
        try:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        except Exception:
            return dflt

    d = DEFAULT_OPTIONS
    return {
        "font": o.get("font") or d["font"],
        "font_size": int(o.get("font_size") or d["font_size"]),
        "sung_color": col(o.get("sung_color"), (255, 210, 74)),
        "unsung_color": col(o.get("unsung_color"), (255, 255, 255)),
        "outline": float(o.get("outline") or d["outline"]),
        "margin_v": int(o.get("margin_v") or d["margin_v"]),
        "next_line": bool(o.get("next_line", d["next_line"])),
        "lead_ms": int(o.get("lead_ms") or d["lead_ms"]),
        "tail_ms": int(o.get("tail_ms") or d["tail_ms"]),
        "min_gap_ms": int(o.get("min_gap_ms") or d["min_gap_ms"]),
    }


def _result_payload(job: Job, video, ass, srt, align_json, stats) -> dict:
    def rel(p):
        return Path(p).name if p else None

    asr_files = [n for n in ("asr_lyrics.lrc", "asr_lyrics.plain.txt", "asr_lyrics.srt")
                 if (job.dir / n).exists()]
    whisper_files = [n for n in ("whisper_lyrics.lrc", "whisper_lyrics.plain.txt",
                                 "whisper_lyrics.srt", "whisper_words.json")
                     if (job.dir / n).exists()]
    sofa_files = [n for n in ("sofa_lyrics.lrc", "sofa_lyrics.plain.txt",
                              "sofa_lyrics.srt", "sofa_diag.json")
                  if (job.dir / n).exists()]
    return {
        "job": job.id,
        "video": rel(video), "ass": rel(ass), "srt": rel(srt),
        "align": rel(align_json), "stats": stats,
        "asr_files": asr_files, "asr_info": job.asr_info,
        "whisper_files": whisper_files, "whisper_info": job.whisper_info,
        "sofa_files": sofa_files, "sofa_info": job.sofa_info or {},
    }


# ==========================================================================
# HTTP
# ==========================================================================


class Handler(BaseHTTPRequestHandler):
    server_version = "lets-karaoke"
    protocol_version = "HTTP/1.1"

    # ---- 基础 ----------------------------------------------------------
    def log_message(self, fmt, *a):   # 静音默认访问日志
        pass

    def _send(self, code: int, ctype: str, body: bytes, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        if n > MAX_BODY:
            raise ValueError(f"请求体过大（{n / 1024 ** 3:.1f} GB）")
        buf = bytearray()
        left = n
        while left > 0:
            chunk = self.rfile.read(min(1 << 20, left))
            if not chunk:
                break
            buf += chunk
            left -= len(chunk)
        return bytes(buf)

    # ---- GET -----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path.startswith('/api/concert/') or u.path.startswith('/concert-files/'):
                from concert_web import dispatch_get
                return dispatch_get(self, u.path, q)
            if u.path in ("/", "/index.html"):
                p = ASSETS / "workspace.html"
                return self._send(200, "text/html; charset=utf-8", p.read_bytes())
            if u.path in ('/karaoke', '/concert', '/concert.js', '/concert.css'):
                name = {'/karaoke': 'index.html', '/concert': 'concert.html'}.get(u.path, u.path[1:])
                ctype = 'text/javascript' if name.endswith('.js') else 'text/css' if name.endswith('.css') else 'text/html'
                return self._send(200, ctype + '; charset=utf-8', (ASSETS / name).read_bytes())
            if u.path == "/api/meta":
                return self._json({
                    "fonts": [n for f, n in FONT_CANDIDATES if Path(f).exists()],
                    "defaults": DEFAULT_OPTIONS,
                    "demucs": ["htdemucs_ft", "htdemucs", "mdx_extra"],
                    "langs": ["auto", "zh", "en", "ja"],
                    "vocoders": ["qwen", "wav2vec2"],
                    "rule_schema": schema(DEFAULT_RULES),
                    "rule_profiles": {p: resolve_rules(DEFAULT_RULES, profile=p)
                                      for p in ("balanced", "automatic", "legacy")},
                })
            if u.path in ('/diagnostics.js', '/diagnostics.css'):
                return self._send(200, 'text/javascript; charset=utf-8' if u.path.endswith('.js') else 'text/css; charset=utf-8', (ASSETS/u.path[1:]).read_bytes())
            if u.path == "/api/events":
                return self._sse(q.get("job", [""])[0])
            if u.path == "/api/align":
                return self._align(q.get("job", [""])[0],
                                   q.get("v", [""])[0])
            if u.path == "/api/jobs":
                with LOCK:
                    return self._json(sorted(
                        ({"job": j.id, "state": j.state, "created": j.created,
                          "result": j.result} for j in JOBS.values()),
                        key=lambda x: -x["created"]))
            if u.path == '/api/history':
                from local_history import scan
                return self._json(scan(ROOT / 'out'))
            if u.path == '/api/versions':
                from local_history import describe
                return self._json(describe(saved_directory(q.get('job', [''])[0]), ROOT / 'out'))
            if u.path.startswith("/files/"):
                return self._file(u.path[len("/files/"):])
            return self._json({"error": "not found", "path": u.path}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---- POST ----------------------------------------------------------
    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path.startswith('/api/concert/'):
                from concert_web import dispatch_post
                return dispatch_post(self, u.path)
            if u.path == "/api/run":
                return self._api_run()
            if u.path == "/api/rerender":
                return self._api_rerender()
            if u.path == "/api/cancel":
                d = json.loads(self._body() or b"{}")
                with LOCK:
                    j = JOBS.get(d.get("job", ""))
                    if j:
                        j.cancel_req = True
                        j.logs.append("收到取消请求…")
                return self._json({"ok": True})
            if u.path == '/api/history/delete':
                d = json.loads(self._body() or b'{}')
                identifiers = d.get('jobs') or []
                with LOCK:
                    active = [j.id for j in JOBS.values() if j.state in ('queued','running')]
                from local_history import delete
                return self._json({'ok': True, 'removed': delete(ROOT / 'out', identifiers, OUT_ROOT, active)})
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": f"{type(e).__name__}: {e}\n"
                                 + traceback.format_exc()[-800:]}, 500)

    # ---- 实现 ----------------------------------------------------------
    def _api_run(self):
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._json({"error": "需要 multipart/form-data"}, 400)
        body = self._body()
        fields, files = parse_multipart(body, m.group(1).encode("latin-1"))

        if "media" not in files:
            return self._json({"error": "请上传视频或音频文件"}, 400)

        job_id = time.strftime("%m%d_%H%M%S") + "_" + os.urandom(2).hex()
        jd = OUT_ROOT / job_id
        indir = jd / "in"
        indir.mkdir(parents=True, exist_ok=True)

        kwargs: dict = {}
        media = indir / safe_name(files["media"][0])
        media.write_bytes(files["media"][1])
        kwargs["media"] = str(media)

        if "audio_track" in files and files["audio_track"][1]:
            at = indir / safe_name(files["audio_track"][0])
            at.write_bytes(files["audio_track"][1])
            kwargs["audio_track"] = str(at)

        lyrics_text = (fields.get("lyrics_text") or "").strip()
        if "lyrics_file" in files and files["lyrics_file"][1]:
            lf = indir / safe_name(files["lyrics_file"][0])
            lf.write_bytes(files["lyrics_file"][1])
            kwargs["lyrics_path"] = str(lf)
        if lyrics_text:
            kwargs["lyrics_text"] = lyrics_text

        # 无歌词文件兜底：本地 ASR 自动转写成歌词草稿
        asr_mode = fields.get("asr") not in (None, "", "0", "false", "False")
        kwargs["asr_lyrics"] = asr_mode
        if asr_mode:
            kwargs.pop("lyrics_path", None)
            kwargs["lyrics_text"] = ""
            lyrics_text = ""
            kwargs["asr_gap"] = float(fields.get("asr_gap") or 0.75)
            kwargs["asr_max_chars"] = int(fields.get("asr_max_chars") or 30)

        # SOFA 对齐（歌声专用）：需要已有歌词文本；与 whisper 互斥，SOFA 优先
        kwargs["sofa_align"] = (fields.get("sofa")
                                not in (None, "", "0", "false", "False"))
        if kwargs["sofa_align"]:
            kwargs["whisper_align"] = False

        # whisper 对齐（演唱/现场推荐）：需要已有歌词文本；SOFA 开启时让位
        kwargs["whisper_align"] = (fields.get("whisper")
                                   not in (None, "", "0", "false", "False"))
        if kwargs["whisper_align"] and kwargs["sofa_align"]:
            kwargs["whisper_align"] = False
        if kwargs["whisper_align"] or asr_mode:
            kwargs["whisper_model"] = fields.get("whisper_model") or "large-v3"
            kwargs["vocal_guide"] = (fields.get("vocal_guide", "1")
                                     not in (None, "", "0", "false", "False"))
            # 对齐喂干声（默认开）：与「人声能量引导」共用同一次分离
            kwargs["align_on_vocals"] = (fields.get("align_on_vocals", "1")
                                         not in (None, "", "0", "false", "False"))
            # 两路取优（默认开）：干声 + 混音各跑一次，逐行取证据更强的一路
            kwargs["align_dual"] = (fields.get("align_dual", "1")
                                    not in (None, "", "0", "false", "False"))
        # Both alignment routes share the same validated defaults and overrides.
        if kwargs["whisper_align"] or kwargs["sofa_align"] or asr_mode:
            kwargs["alignment_profile"] = fields.get("alignment_profile") or "balanced"
            try:
                raw_rules = json.loads(fields.get("rules") or "{}")
                kwargs["rules"] = resolve_rules(DEFAULT_RULES, raw_rules,
                                                 kwargs["alignment_profile"])
            except (ValueError, TypeError) as exc:
                return self._json({"error": str(exc)}, 400)

        if not kwargs.get("lyrics_path") and not lyrics_text and not asr_mode:
            return self._json(
                {"error": "请粘贴/上传歌词；若确实没有歌词文件，"
                          "请勾选「无歌词文件（本地 ASR 自动转写）」"}, 400)

        for k in ("vocal_mode", "lang", "demucs", "backend", "timed_mode", "encoder"):
            if fields.get(k):
                kwargs[k] = fields[k]
        kwargs["quality"] = int(fields.get("quality") or 21)
        kwargs["separate"] = (fields.get("separate", "1") not in ("0", "false", ""))
        kwargs["device"] = fields.get("device") or "cuda"
        try:
            kwargs["options"] = json.loads(fields.get("options") or "{}")
        except Exception:
            kwargs["options"] = {}

        job = Job(id=job_id, dir=jd)
        with LOCK:
            JOBS[job_id] = job
            job.add(0.0, f"已接收：{media.name}")
        _ensure_cache(kwargs["device"])
        t = threading.Thread(target=_run_job, args=(job, kwargs), daemon=True)
        t.start()
        return self._json({"job": job_id})

    def _sse(self, job_id: str):
        with LOCK:
            job = JOBS.get(job_id)
        if job is None:
            return self._json({"error": "unknown job"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        idx = 0
        try:
            while True:
                with LOCK:
                    new = job.logs[idx:]
                    idx = len(job.logs)
                    payload = {"state": job.state, "progress": round(job.progress, 4),
                               "lines": new, "result": job.result, "error": job.error,
                               "asr_lrc": job.asr_lrc, "whisper_lrc": job.whisper_lrc,
                               "whisper_info": job.whisper_info}
                    terminal = job.state in ("done", "error", "cancelled")
                self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False)
                                  + "\n\n").encode("utf-8"))
                self.wfile.flush()
                if terminal and not new:
                    break
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _align(self, job_id: str, ver: str = ""):
        with LOCK:
            job = JOBS.get(job_id)
        jd = job.dir if job else saved_directory(job_id)
        if ver:
            if not ver.isdigit():
                return self._json({'error': '无效版本号'}, 400)
            p = jd / f"align_v{ver}.json"
        else:
            p = jd / "align.json"
            if not p.exists():
                cands = sorted(jd.glob("align_v*.json"))
                if cands:
                    p = cands[-1]
        if not p.exists():
            return self._json({"error": "对齐结果不存在"}, 404)
        data = json.loads(p.read_text(encoding="utf-8"))
        if (jd/'job.json').exists():
            settings = json.loads((jd/'job.json').read_text(encoding='utf-8')).get('options', {})
            data['options'] = {**settings, **(data.get('options') or {})}
        evidence = jd / 'alignment_evidence.json'
        from alignment_diagnostics import attach
        data = attach(data, json.loads(evidence.read_text(encoding='utf-8')) if evidence.exists() else {})
        return self._json(data)

    def _api_rerender(self):
        from pipeline import RestyleRequest, restyle
        d = json.loads(self._body() or b"{}")
        job_id = d.get("job") or ""
        with LOCK:
            job = JOBS.get(job_id)
        jd = job.dir if job else saved_directory(job_id)
        if not (jd / "job.json").exists():
            return self._json({"error": "任务目录缺少 job.json，无法重渲染"}, 400)
        with LOCK:
            if any(j.state in ('queued','running') for j in JOBS.values()):
                return self._json({'error':'生成任务仍在运行，请完成后再调整历史结果'},409)
        req = RestyleRequest(
            base_version=int(d.get('base_version') or 0),
            anchor_row=d.get('anchor_row'),
            line_bounds=d.get('line_bounds') or {},
            line_insertion=d.get('line_insertion'),
            line_offsets_ms={int(k): float(v) for k, v in
                             (d.get("line_offsets") or {}).items()},
            token_offsets_ms={str(k): float(v) for k, v in
                              (d.get("token_offsets") or {}).items()},
            overrides=d.get("options") or {},
        )
        logs: list[str] = []

        def prog(f, m):
            logs.append(f"[{f * 100:5.1f}%] {m}")

        if not EDIT_LOCK.acquire(blocking=False):
            return self._json({'error': '另一个调整任务仍在运行，请完成后重试'}, 409)
        try:
            out = restyle(jd, req, progress=prog)
        except Exception as e:  # noqa: BLE001
            return self._json({"error": f"{type(e).__name__}: {e}\n"
                                        + traceback.format_exc()[-800:]}, 500)
        finally:
            EDIT_LOCK.release()
        for k in ("video", "ass", "srt"):
            if out.get(k):
                out[k] = Path(out[k]).name
        out["logs"] = logs
        return self._json(out)

    def _file(self, rel: str):
        parts = [urllib.parse.unquote(x) for x in rel.split("/") if x]
        if len(parts) != 2 or any(x in ("..", ".") for x in parts):
            return self._json({"error": "bad path"}, 400)
        job_id, name = parts
        jd = saved_directory(job_id)
        p = (jd / safe_name(name)).resolve()
        # 防目录穿越
        if not p.is_relative_to(jd) or not p.is_file():
            return self._json({"error": "not found"}, 404)

        size = p.stat().st_size
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if p.suffix.lower() in (".ass", ".srt"):
            ctype = "text/plain; charset=utf-8"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        code = 200
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                end = min(end, size - 1)
                code = 206
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download" in (self.path or ""):
            self.send_header("Content-Disposition",
                             f'attachment; filename="{p.name}"')
        self.end_headers()
        with open(p, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)


def _warmup(device: str) -> None:
    """后台预热对齐模型。

    实测：任务线程里首次加载 Qwen 要 ~17s（含 CUDA 上下文与内核初始化），
    期间进度条会从 50% 静默卡到 60%。放到启动时的后台线程里预热，
    首个任务就能直接进入对齐。
    """
    try:
        from pipeline import MODELS_QWEN
        if not MODELS_QWEN.exists():
            return
        t0 = time.time()
        _ensure_cache(device).aligner("qwen", "zh")
        print(f"[warmup] Qwen3-ForcedAligner 预热完成（{time.time() - t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] 跳过：{type(e).__name__}: {e}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="lets-karaoke 本地 WebUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-warmup", action="store_true", help="跳过模型预热")
    ap.add_argument("--device", default="cuda")
    args = ap.parse_args(argv)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    if not (ASSETS / "index.html").exists():
        print(f"!! 缺少界面文件: {ASSETS / 'index.html'}")
        return 1

    if not args.no_warmup:
        threading.Thread(target=_warmup, args=(args.device,), daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"lets-karaoke WebUI  ->  {url}")
    print(f"输出目录            ->  {OUT_ROOT}")
    print("Ctrl+C 退出")
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
