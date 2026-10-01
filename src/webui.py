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
import copy
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
import queue
import tempfile
import urllib.parse
import uuid
import webbrowser
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ass_builder import parse_line_positions

ROOT = _HERE.parent
ASSETS = _HERE / "webui_assets"
OUT_ROOT = ROOT / "out" / "webui"
MAX_BODY = 3 * 1024 ** 3          # 3 GB 上传上限
DEFAULT_DEVICE = 'cuda'
UPLOAD_SLOTS = threading.BoundedSemaphore(2)
RESOURCE_LOCK = threading.RLock()
TASK_QUEUE = queue.Queue()
WORKER_LOCK = threading.Lock()
WORKER = None
STOP_WORKER = threading.Event()

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
    "line_count": 2,
    "position_x": 50,
    "position_y": 89,
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
    kind: str = 'generate'
    storage_dir: Path | None = None
    target_job: str | None = None
    attempt_id: str = ''

    def add(self, frac: float, msg: str) -> None:
        self.progress = max(0.0, min(1.0, float(frac)))
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"{stamp}  {msg}")
        self.logs[:] = self.logs[-500:]
        persist_job(self)


JOBS: dict[str, Job] = {}
LOCK = threading.RLock()
EDIT_LOCK = threading.Lock()
EDIT_DRAFTS: dict[str, dict] = {}


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def persist_job(job):
    # Keep the snapshot and replacement ordered with cancellation/retry.
    # Otherwise an older terminal write can overwrite a newly queued attempt.
    with LOCK:
        atomic_json((job.storage_dir or job.dir)/'history_status.json', {
            'job': job.id, 'state': job.state, 'progress': job.progress,
            'error': job.error, 'created': job.created, 'result': job.result,
            'logs': job.logs, 'kind': job.kind, 'target_job': job.target_job,
            'attempt_id': job.attempt_id})


def _queue_worker():
    while not STOP_WORKER.is_set():
        try:
            job, config, attempt_id = TASK_QUEUE.get(timeout=.5)
        except queue.Empty:
            continue
        try:
            with RESOURCE_LOCK:
                if STOP_WORKER.is_set():
                    return  # Leave this request queued on disk for next startup.
                with LOCK:
                    if (JOBS.get(job.id) is not job or job.attempt_id != attempt_id
                            or job.state != 'queued'):
                        continue
                    job.state = 'cancelled' if job.cancel_req else 'running'
                    persist_job(job)
                    cancelled = job.cancel_req
                if cancelled:
                    continue
                if job.kind == 'edit':
                    job.add(0, '开始调整字幕')
                    job.result = perform_edit(config, job)
                    with LOCK:
                        job.state, job.progress = 'done', 1.0
                        persist_job(job)
                else:
                    _run_job(job, copy.deepcopy(config))
                with LOCK:
                    if job.attempt_id == attempt_id and job.state in {'queued', 'running'}:
                        job.state = 'error'
                        job.error = '任务没有返回完成状态'
                        persist_job(job)
        except Exception as exc:
            with LOCK:
                if job.attempt_id == attempt_id:
                    job.state = 'cancelled' if job.cancel_req else 'error'
                    job.error = f'{type(exc).__name__}: {exc}'
                    try:
                        persist_job(job)
                    except OSError:
                        # Disk exhaustion must not kill the only queue worker.
                        traceback.print_exc()
        finally:
            TASK_QUEUE.task_done()
            with LOCK:
                completed = sorted((j for j in JOBS.values() if j.state not in {'queued','running'}), key=lambda j:j.created)
                for old in completed[:-200]:
                    JOBS.pop(old.id, None)


def enqueue_job(job, config):
    global WORKER
    with LOCK:
        attempt_id = uuid.uuid4().hex
        atomic_json((job.storage_dir or job.dir)/'task_request.json', {
            'job':job.id, 'directory':str(job.dir), 'kind':job.kind,
            'created':job.created, 'target_job':job.target_job, 'config':config,
            'attempt_id':attempt_id})
        job.state, job.error, job.cancel_req = 'queued', None, False
        job.attempt_id = attempt_id
        JOBS[job.id] = job
        job.add(0, '已进入本机处理队列')
        TASK_QUEUE.put((job, config, attempt_id))
    with WORKER_LOCK:
        if WORKER is None or not WORKER.is_alive():
            WORKER = threading.Thread(target=_queue_worker, daemon=True, name='karaoke-worker')
            WORKER.start()


def restore_jobs(start_queued=True):
    requests = list(OUT_ROOT.glob('*/task_request.json')) + list((OUT_ROOT/'.tasks').glob('*/task_request.json'))
    pending = []
    for path in requests:
        try:
            request = json.loads(path.read_text(encoding='utf-8'))
            status = json.loads(path.with_name('history_status.json').read_text(encoding='utf-8'))
            directory = Path(request['directory']).resolve()
            if not directory.is_relative_to((ROOT/'out').resolve()) and not directory.is_relative_to(OUT_ROOT.resolve()):
                continue
            job = Job(request['job'], directory, state=status.get('state','interrupted'),
                      created=request.get('created',time.time()), kind=request.get('kind','generate'),
                      storage_dir=path.parent if request.get('kind')=='edit' else None,
                      target_job=request.get('target_job'),
                      attempt_id=status.get('attempt_id', request.get('attempt_id', '')))
            job.result, job.error, job.logs = status.get('result'), status.get('error'), status.get('logs', [])[-500:]
            job.progress = status.get('progress',0)
            if job.state == 'running':
                job.state = 'interrupted'
                job.error = '后台曾中断，可从历史记录重新开始此任务。'
                persist_job(job)
            with LOCK:
                if job.id in JOBS:
                    continue
                JOBS[job.id] = job
            if job.state == 'queued':
                pending.append((job,request['config']))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if start_queued:
        for job, config in sorted(pending,key=lambda pair:pair[0].created):
            enqueue_job(job,config)


def load_draft(identifier):
    if not identifier or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        return None
    if identifier in EDIT_DRAFTS:
        return EDIT_DRAFTS[identifier]
    path = OUT_ROOT/'.drafts'/f'{identifier}.json'
    if not path.is_file():
        return None
    from ass_builder import KaraokeLine, KaraokeToken
    draft = json.loads(path.read_text(encoding='utf-8'))
    draft['lines'] = [KaraokeLine(**{**row,'tokens':[KaraokeToken(**t) for t in row['tokens']]}) for row in draft['lines']]
    EDIT_DRAFTS[identifier] = draft
    return draft


def load_saved_job(identifier):
    """Hydrate an evicted terminal task without restarting its request."""
    if not isinstance(identifier, str) or not identifier:
        return None
    with LOCK:
        if identifier in JOBS:
            return JOBS[identifier]
        if isinstance(identifier, str) and re.fullmatch(r'edit_[0-9a-f]{16}', identifier):
            storage = OUT_ROOT/'.tasks'/identifier
        else:
            storage = saved_directory(identifier)
        try:
            request = json.loads((storage/'task_request.json').read_text(encoding='utf-8'))
            if request.get('job') in JOBS:
                return JOBS[request['job']]
            status = json.loads((storage/'history_status.json').read_text(encoding='utf-8'))
            directory = Path(request['directory']).resolve()
            if not directory.is_relative_to((ROOT/'out').resolve()):
                return None
            kind = request.get('kind', 'generate')
            if kind != 'edit' and directory != storage.resolve():
                return None
            job = Job(request['job'], directory, state=status.get('state', 'interrupted'),
                created=request.get('created', time.time()), kind=kind,
                storage_dir=storage if kind == 'edit' else None,
                target_job=request.get('target_job'),
                attempt_id=status.get('attempt_id', request.get('attempt_id', '')))
            job.result, job.error = status.get('result'), status.get('error')
            job.logs, job.progress = status.get('logs', [])[-500:], status.get('progress', 0)
            if job.state == 'running':
                job.state, job.error = 'interrupted', '后台曾中断，可重新开始此任务。'
                persist_job(job)
            JOBS[job.id] = job
            return job
        except (OSError, ValueError, KeyError, TypeError):
            return None


def discard_draft(identifier):
    EDIT_DRAFTS.pop(identifier, None)
    if re.fullmatch(r'[0-9a-f]{32}', identifier):
        (OUT_ROOT/'.drafts'/f'{identifier}.json').unlink(missing_ok=True)


def perform_edit(data, operation=None):
    from pipeline import RestyleRequest, restyle
    job_id = data.get('job') or ''
    draft_id = str(data.get('draft_id') or '')
    base_version = int(data.get('base_version') or 0)
    with LOCK:
        original = JOBS.get(job_id)
        draft = load_draft(draft_id)
    directory = original.dir if original else saved_directory(job_id)
    if not (directory/'job.json').exists():
        raise ValueError('任务目录缺少 job.json，无法调整')
    if draft_id and (not draft or Path(draft.get('job_dir', '')).resolve() != directory.resolve()
                     or draft.get('base_version') != base_version):
        raise ValueError('暂存修改已失效，请从当前版本重新开始')
    req = RestyleRequest(base_version=base_version, anchor_row=data.get('anchor_row'),
        anchor_rows=data.get('anchor_rows') if 'anchor_rows' in data else None,
        line_bounds=data.get('line_bounds') or {},
        line_texts={int(k):v for k,v in (data.get('line_texts') or {}).items()},
        line_insertion=data.get('line_insertion'),
        draft_insertions=copy.deepcopy(draft.get('insertions',[])) if draft else [],
        retry_suspects=data.get('retry_suspects') is True,
        line_offsets_ms={int(k):float(v) for k,v in (data.get('line_offsets') or {}).items()},
        token_offsets_ms={str(k):float(v) for k,v in (data.get('token_offsets') or {}).items()},
        overrides=data.get('options') or {}, preview_only=data.get('defer_render') is True)
    logs = []
    def progress(frac, message):
        logs.append(f'[{frac*100:5.1f}%] {message}')
        if operation:
            operation.add(frac,message)
    out = restyle(directory, req, progress=progress, initial_lines=copy.deepcopy(draft['lines']) if draft else None,
                  initial_reference=copy.deepcopy(draft.get('acceptance_reference')) if draft else None,
                  cancel=(lambda:operation.cancel_req) if operation else None)
    if req.preview_only:
        lines = out.pop('_draft_lines',None)
        if lines is None:
            raise ValueError('暂存对齐没有返回歌词')
        draft_id = draft_id or uuid.uuid4().hex
        insertions = copy.deepcopy(draft.get('insertions',[])) if draft else []
        if isinstance(data.get('line_insertion'),dict):
            insertions.append(copy.deepcopy(data['line_insertion']))
        value = {'job_id':job_id,'job_dir':str(directory),'base_version':base_version,
                 'lines':lines,'insertions':insertions,'updated':time.time(),
                 'acceptance_reference':copy.deepcopy(out.get('acceptance_reference'))}
        with LOCK:
            EDIT_DRAFTS[draft_id] = value
            atomic_json(OUT_ROOT/'.drafts'/f'{draft_id}.json',{**value,'lines':[asdict(row) for row in lines]})
        out['draft_id'] = draft_id
    elif draft_id:
        with LOCK:
            discard_draft(draft_id)
    for key in ('video','ass','srt'):
        if out.get(key):
            out[key] = Path(out[key]).name
    out['logs'] = logs
    from local_history import invalidate
    invalidate(directory)
    return out


SOFA_CKPT = ROOT / "models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt"


def capabilities() -> dict:
    """按已安装的依赖与模型报告可用功能；只探测模块与文件，不导入模型。"""
    from importlib.util import find_spec

    def has(*modules):
        return all(find_spec(m) is not None for m in modules)

    def cap(ok, reason):
        return {"ok": bool(ok), "reason": "" if ok else reason}

    def complete(directory):
        return (directory / "config.json").is_file() and any(
            p.suffix in (".safetensors", ".bin") for p in directory.glob("*"))

    whisper_dir = model_paths.WHISPER_DIR
    whisper = has("whisper", "stable_whisper", "torch")
    qwen = has("qwen_asr", "transformers")
    return {
        "whisper": cap(whisper,
                       "需要 Whisper 字幕环境：运行 setup_guide.bat 选择 [1]"),
        "whisper_models": sorted(p.stem for p in whisper_dir.glob("*.pt")) if whisper_dir.is_dir() else [],
        "sofa": cap(whisper and has("lightning", "textgrid", "pykakasi") and SOFA_CKPT.is_file(),
                    "需要 Whisper 前置依赖、SOFA 环境（setup_guide.bat 选择 [4]）及 SOFA checkpoint"),
        "asr": cap(whisper and qwen and complete(model_paths.QWEN_ASR_DIR) and complete(model_paths.QWEN_ALIGNER_DIR),
                   "需要 Whisper 前置依赖、Qwen 环境（setup_guide.bat 选择 [3]）及 ASR 与 ForcedAligner 模型"),
        "qwen": cap(qwen and complete(model_paths.QWEN_ALIGNER_DIR),
                    "需要 Qwen 环境并下载 ForcedAligner 模型"),
        "wav2vec2": cap(has("transformers"), "需要 Qwen 环境（含 transformers）"),
    }


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
    from pipeline import ModelCache, PipelineConfig, AssOptions, run, _load_lyrics, parse_lyrics

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
        if cfg_kwargs.get('lyrics_path') or (cfg_kwargs.get('lyrics_text') or '').strip():
            original, _ = _load_lyrics(PipelineConfig(media=cfg_kwargs['media'],
                lyrics_path=cfg_kwargs.get('lyrics_path'), lyrics_text=cfg_kwargs.get('lyrics_text') or ''))
            (job.dir/'input_lyrics.json').write_text(json.dumps(
                [r.text for r in parse_lyrics(original).lines], ensure_ascii=False), encoding='utf-8')
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
            timed_mode="warp" if (need_sofa or need_whisper or need_asr) else cfg_kwargs.get("timed_mode", "warp"),
            encoder=cfg_kwargs.get("encoder", "auto"),
            quality=int(cfg_kwargs.get("quality", 21)),
            ass=ass,
        )
        res = run(cfg, progress=prog, cancel=cancel, cache=_ensure_cache(cfg.device))
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
            persist_job(job)
    except Exception as e:  # noqa: BLE001
        with LOCK:
            job.state = "cancelled" if cancel() else "error"
            job.error = f"{type(e).__name__}: {e}"
            job.logs.append(traceback.format_exc()[-1200:])
            persist_job(job)



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
    extract_wav(cfg_kwargs["media"], p, sr=44100, mono=False, cancel=lambda:job.cancel_req)
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
                          cfg_kwargs.get("device", DEFAULT_DEVICE), cancel=lambda:job.cancel_req)
    return Path(v), Path(a)


def _sofa_prepass(job: Job, cfg_kwargs: dict, cancel) -> None:
    """SOFA（歌声专用）细化：whisper 行窗口内用音素级对齐替换字级时间。

    基准：SOFA 整曲 match 模式在带长间奏/重复副歌的歌上
    不可靠，必须依赖 whisper 的行窗口。流程 = whisper 行窗口 → 每行切人声段
    → SOFA 音素级对齐 → 字级时间替换（窗口内精修）。使用当前启动环境。
    """
    from sofa_backend import require_japanese_language
    from pipeline import parse_lyrics
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
    rows = [line.text for line in parse_lyrics(lyrics).lines]
    if not rows:
        raise RuntimeError("SOFA 对齐需要歌词文本，但歌词为空")
    # The checkpoint is multilingual, but this route's G2P is Japanese only.
    # Resolve auto before Whisper and keep the same language in saved job data
    # so later anchor/retry operations do not re-detect it differently.
    cfg_kwargs["lang"] = require_japanese_language(
        cfg_kwargs.get("lang", "auto"), "\n".join(rows))

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
        # TextGrid clocks start at the first actual cropped sample, including
        # the leading pad (which may be truncated near the beginning of audio).
        seg_meta[idx] = (a / _sr, phs, owners)
        n_cut += 1
    (seg_dir / "ja_job_dict.txt").write_text("\n".join(dict_lines), encoding="utf-8")
    with LOCK:
        job.add(0.35, f"[sofa] 细化分段 {n_cut} 段")

    # 第三步：SOFA 推理（窗口内 force 对齐，窗口已含完整演唱）
    # 使用 multilingual 检查点；行分段和 mora 音素由同一份字典生成。
    ckpt = SOFA_CKPT
    jdict = seg_dir / "ja_job_dict.txt"
    cmd = [sys.executable,
           str(ROOT / "tools/SOFA/infer.py"), "--ckpt", str(ckpt),
           "--folder", str(seg_dir), "--g2p", "Dictionary",
           "--dictionary", str(jdict), "--mode", "force",
           "--out_formats", "textgrid", "--save_confidence"]
    sofa_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    from process_runner import run_process
    r = run_process(cmd, cwd=str(ROOT / 'tools/SOFA'), env=sofa_env, cancel=cancel, timeout=3600)
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
        clip_start, phs, owners = meta
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
        refined[idx] = {ci: (clip_start + s0, clip_start + e0)
                        for ci, (s0, e0) in filled.items()}
        rep["refined"] += 1

    # 最终行表：SOFA 成功的行用 SOFA 字级；失败的（含英语行）用 whisper 窗口均分兜底
    asr_lines = []
    for idx, (st, en, text) in enumerate(win):
        if idx in refined:
            segs = [Segment(text[ci], s0, e0, 1.0)
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
                               merge_lines_monotonic,
                               prune_intervals_by_tx, relocate_lowconf,
                               transcribe_check, vocal_guide, vocal_intervals)
    from asr_lyrics import to_enhanced_lrc, to_plain, to_srt

    lang = cfg_kwargs.get("lang", "auto")
    model_size = cfg_kwargs.get("whisper_model", "large-v3")
    (job.dir/'retry_config.json').write_text(json.dumps({'model':model_size,
        'device':cfg_kwargs.get('device','cuda')}), encoding='utf-8')
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
    from pipeline import parse_lyrics
    rows = [line.text for line in parse_lyrics(lyrics).lines]
    if not (job.dir/'input_lyrics.json').exists():
        (job.dir/'input_lyrics.json').write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')

    # 语言：显式指定优先，否则从歌词文本推断（比让 whisper 猜可靠）
    lang_map = {"zh": "Chinese", "en": "English", "ja": "Japanese"}
    wlang = lang_map.get(lang)
    if wlang is None:
        wlang, _ = detect_language("\n".join(rows))
    if lang == "ja" and not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", "".join(rows)) \
            and re.search(r"[A-Za-z]", "".join(rows)):
        with LOCK:
            job.add(0.02, "[whisper] ⚠ 歌词全为拉丁字母，却选择了日语对齐；英语歌词请选择 English，日语罗马音建议换成日文原文")
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

    # 双路联合选择：逐行按置信度择优会在副歌重复处跳到另一轮演唱，
    # 导致歌词行时间倒退；全局合并先保证顺序与边界连续，再比较模型置信度。
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
        asr_lines, dual_rep = merge_lines_monotonic(
            asr_lines, diag.get("row_index") or [], alt_lines,
            alt_diag.get("row_index") or [], len(rows))
        diag["dual"] = dual_rep
        # 后续诊断需要把每个选中对象映射回输入歌词行；双路可能漏掉不同的行。
        diag["row_index"] = dual_rep["row_index"]
    with LOCK:
        job.add(0.30, f"[whisper] 后处理：{diag}"
                      + (f"｜双路合并：{dual_rep['n_alt']} 行取自混音" if dual_rep else ""))

    evidence_indices = dict(zip((id(l) for l in asr_lines), diag.get("row_index") or []))
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
    evidence = explain(asr_lines, evidence_primary, evidence_alt, evidence_before, evidence_indices, iv, len(rows))
    if cfg_kwargs.get('auto_retry', True):
        import soundfile as sf
        from alignment_retry import retry_lines
        asr_lines, evidence, retry = retry_lines(asr_lines, evidence, [voc, audio],
            sf.info(str(audio)).duration, wlang, model=model_size,
            device=cfg_kwargs.get('device','cuda'), progress=wprog2, cancel=cancel)
        (job.dir/'alignment_retry.json').write_text(json.dumps(retry, ensure_ascii=False, indent=2), encoding='utf-8')
        with LOCK:
            job.add(.34, f"[局部重试] 检查 {retry['attempted']} 句，接受 {retry['accepted']} 句；未达条件保留原结果")
    (job.dir / 'alignment_evidence.json').write_text(json.dumps(evidence, ensure_ascii=False), encoding='utf-8')
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
_CACHES = {}
_CACHE_LOCK = threading.Lock()


def _ensure_cache(device: str = "cuda"):
    global _CACHE
    with _CACHE_LOCK:
        from pipeline import ModelCache
        if device not in _CACHES:
            _CACHES[device] = ModelCache(device)
        _CACHE = _CACHES[device]
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
    show_following = bool(o.get('next_line', int(o.get('line_count', d['line_count'])) > 1))
    line_count = max(1, min(3, int(o.get('line_count', d['line_count'] if show_following else 1))))
    if not show_following:
        line_count = 1
    return {
        "font": o.get("font") or d["font"],
        "font_size": int(o.get("font_size") or d["font_size"]),
        "sung_color": col(o.get("sung_color"), (255, 210, 74)),
        "unsung_color": col(o.get("unsung_color"), (255, 255, 255)),
        "outline": float(o.get("outline", d["outline"])),
        "margin_v": int(o.get("margin_v", d["margin_v"])),
        "next_line": show_following,
        "line_count": line_count,
        "position_x": max(0, min(100, int(o.get('position_x', d['position_x'])))),
        "position_y": max(0, min(100, int(o.get('position_y', d['position_y'])))),
        "line_positions": parse_line_positions(o.get('line_positions')),
        "lead_ms": int(o.get("lead_ms", d["lead_ms"])),
        "tail_ms": int(o.get("tail_ms", d["tail_ms"])),
        "min_gap_ms": int(o.get("min_gap_ms", d["min_gap_ms"])),
        "group_same_unit": bool(o.get('group_same_unit', False)),
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
        "acceptance": (json.loads((job.dir / "acceptance.json").read_text(encoding="utf-8"))
                       if (job.dir / "acceptance.json").exists() else None),
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
        if self.close_connection:
            self.send_header('Connection', 'close')
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
        if n > 1 << 20:
            self.close_connection = True
            raise ValueError('JSON 请求体过大')
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
            from http_support import request_allowed
            if not request_allowed(self):
                return self._json({'error':'仅允许本地工作台访问'},403)
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
                from lyrics_search import metadata as lyrics_search_metadata
                return self._json({
                    "features": {"multi_anchor": True, "strict_timeline_order": True,
                                 'async_edit': True, 'persistent_queue': True, 'lyrics_search': True},
                    "lyrics_search": {**lyrics_search_metadata(),
                                      'privacy': '仅在主动搜索时发送歌名和艺人，不发送媒体或本地歌词'},
                    "fonts": [n for f, n in FONT_CANDIDATES if Path(f).exists()],
                    "defaults": DEFAULT_OPTIONS,
                    "demucs": ["htdemucs_ft", "htdemucs", "mdx_extra"],
                    "langs": ["auto", "zh", "en", "ja"],
                    "vocoders": ["qwen", "wav2vec2"],
                    "rule_schema": schema(DEFAULT_RULES),
                    "rule_profiles": {p: resolve_rules(DEFAULT_RULES, profile=p)
                                      for p in ("balanced", "automatic", "legacy")},
                    "capabilities": capabilities(),
                })
            if u.path in ('/diagnostics.js', '/diagnostics.css', '/lyric_waveform.js', '/review.css', '/task_client.js',
                          '/theme.css', '/karaoke.css', '/lyrics_search.js', '/lyrics_search.css'):
                return self._send(200, 'text/javascript; charset=utf-8' if u.path.endswith('.js') else 'text/css; charset=utf-8', (ASSETS/u.path[1:]).read_bytes())
            if u.path == "/api/events":
                return self._sse(q.get("job", [""])[0])
            if u.path == "/api/align":
                return self._align(q.get("job", [""])[0],
                                   q.get("v", [""])[0])
            if u.path == '/api/waveform':
                from lyric_waveform import waveform
                return self._json(waveform(saved_directory(q.get('job',[''])[0])))
            if u.path == "/api/jobs":
                with LOCK:
                    return self._json(sorted(
                        ({"job": j.id, "state": j.state, "created": j.created,
                          "kind":j.kind, "target_job":j.target_job,
                          "result": j.result, 'error':j.error} for j in JOBS.values()),
                        key=lambda x: -x["created"]))
            if u.path == '/api/history':
                from local_history import scan
                listing = scan(ROOT / 'out')
                with LOCK:
                    active = {str(j.dir.resolve()):j for j in JOBS.values() if j.state in {'queued','running'}}
                for record in listing['records']:
                    job = active.get(str((ROOT/'out'/record['path']).resolve()))
                    if job:
                        record['state'],record['active_job'] = job.state,job.id
                return self._json(listing)
            if u.path == '/api/job':
                job = load_saved_job(q.get('job',[''])[0])
                if job:
                    return self._json({'job':job.id,'state':job.state,'progress':job.progress,
                        'lines':[], 'result':job.result,'error':job.error,'kind':job.kind,
                        'target_job':job.target_job})
                return self._json({'error':'任务不存在或已被清理'},404)
            if u.path == '/api/draft':
                identifier = q.get('draft_id', [''])[0]
                with LOCK:
                    draft = load_draft(identifier)
                if not draft:
                    return self._json({'error':'暂存修改已不存在'},404)
                directory = saved_directory(q.get('job', [''])[0])
                version = int(q.get('base_version', ['0'])[0])
                if directory != Path(draft['job_dir']).resolve() or version != draft['base_version']:
                    return self._json({'error':'暂存修改与当前记录或版本不匹配'},404)
                return self._json({'draft_id':identifier,'base_version':version,
                    'lines':[asdict(row) for row in draft['lines']]})
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
            from http_support import request_allowed, reject_post
            if not request_allowed(self):
                return reject_post(self, {'error':'仅允许本地工作台发起操作'},403)
            required = 'multipart/form-data' if u.path == '/api/run' else 'application/json'
            if required != self.headers.get('Content-Type','').split(';',1)[0].strip().lower():
                return reject_post(self, {'error':'请求类型不正确'},415)
            if u.path.startswith('/api/concert/'):
                from concert_web import dispatch_post
                return dispatch_post(self, u.path)
            if u.path in {'/api/lyrics/search', '/api/lyrics/get'}:
                return self._api_lyrics(u.path)
            if u.path == "/api/run":
                return self._api_run()
            if u.path == "/api/rerender":
                return self._api_rerender()
            if u.path == "/api/draft/discard":
                data = json.loads(self._body() or b"{}")
                draft_id = str(data.get("draft_id") or "")
                with LOCK:
                    discard_draft(draft_id)
                return self._json({"ok": True})
            if u.path == "/api/cancel":
                d = json.loads(self._body() or b"{}")
                with LOCK:
                    j = load_saved_job(d.get("job", ""))
                    if not j:
                        return self._json({'error':'任务不存在'},404)
                    if j.state in {'queued','running'}:
                        j.cancel_req = True
                        j.logs.append("收到取消请求…")
                        if j.state == 'queued':
                            j.state = 'cancelled'
                        persist_job(j)
                return self._json({"ok": True, 'state':j.state})
            if u.path == '/api/resume':
                data = json.loads(self._body() or b'{}')
                with LOCK:
                    job = load_saved_job(data.get('job',''))
                    if not job:
                        return self._json({'error':'未保存可重试任务'},404)
                    directory = job.dir.resolve()
                    if any(j.dir.resolve()==directory and j.state in {'queued','running'} for j in JOBS.values()):
                        return self._json({'error':'此记录已有任务在队列中'},409)
                    if job.state not in {'interrupted','error','cancelled'}:
                        return self._json({'error':'只有中断、失败或取消的任务可以重新开始'},409)
                    request = json.loads(((job.storage_dir or directory)/'task_request.json').read_text(encoding='utf-8'))
                    enqueue_job(job,request['config'])
                return self._json({'job':job.id,'kind':job.kind,'target_job':job.target_job},202)
            if u.path == '/api/history/delete':
                d = json.loads(self._body() or b'{}')
                identifiers = d.get('jobs') or []
                with LOCK:
                    active = [j.dir for j in JOBS.values() if j.state in ('queued','running')]
                    from local_history import delete
                    return self._json({'ok': True, 'removed': delete(ROOT / 'out', identifiers, OUT_ROOT, active)})
            return self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except (ValueError, KeyError, TypeError) as e:
            self.close_connection = True
            self._json({'error':str(e)},400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---- 实现 ----------------------------------------------------------
    def _api_lyrics(self, path):
        from http_support import reject_post
        from lyrics_search import LyricsSearchError, get_lyrics, search
        if self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            return self._json({'error': '歌词查询不支持分块请求'}, 400)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return reject_post(self, {'error': '歌词查询请求长度无效'}, 400)
        if not 0 < length <= 4096:
            return reject_post(self, {'error': '歌词查询请求必须在 4 KiB 以内'}, 413, body_limit=4096)
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + 5
        try:
            body = bytearray()
            while len(body) < length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(length - len(body))
                if not chunk:
                    break
                body.extend(chunk)
            if time.monotonic() >= deadline:
                raise TimeoutError
            if len(body) != length:
                self.close_connection = True
                return self._json({'error': '歌词查询请求不完整，请重试'}, 400)
            data = json.loads(body)
        except TimeoutError:
            self.close_connection = True
            return self._json({'error': '歌词查询请求接收超时，请重试'}, 408)
        except (ValueError, RecursionError):
            return self._json({'error': '歌词查询 JSON 格式无效或嵌套过深'}, 400)
        finally:
            self.connection.settimeout(previous_timeout)
        if not isinstance(data, dict):
            return self._json({'error': '歌词查询参数需为对象'}, 400)
        allowed = {'track_name', 'artist_name'} if path.endswith('/search') else {'id'}
        if set(data) - allowed:
            return self._json({'error': '歌词查询仅接受歌名、艺人名或词库记录编号'}, 400)
        try:
            result = (search(data.get('track_name'), data.get('artist_name', ''))
                      if path.endswith('/search') else get_lyrics(data.get('id')))
            return self._json(result)
        except LyricsSearchError as exc:
            payload = {'error': str(exc)}
            if exc.retry_after is not None:
                payload['retry_after'] = exc.retry_after
            return self._json(payload, exc.status)

    def _api_run(self):
        if not UPLOAD_SLOTS.acquire(blocking=False):
            self.close_connection = True
            return self._json({'error':'正在接收其他媒体，请稍后重试'},429)
        old_timeout = self.connection.gettimeout()
        try:
            staging_root = OUT_ROOT/'.uploads'
            staging_root.mkdir(parents=True,exist_ok=True)
            self.connection.settimeout(60)
            with tempfile.TemporaryDirectory(prefix='upload-',dir=staging_root) as staging:
                return self._api_run_inner(Path(staging))
        finally:
            self.connection.settimeout(old_timeout)
            UPLOAD_SLOTS.release()

    def _api_run_inner(self, staging):
        ctype = self.headers.get("Content-Type") or ""
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if "multipart/form-data" not in ctype or not m:
            return self._json({"error": "需要 multipart/form-data"}, 400)
        length = int(self.headers.get('Content-Length') or 0)
        if not 0 < length <= MAX_BODY:
            self.close_connection = True
            return self._json({'error':'上传总大小必须在 0 到 3 GiB 之间'},413)
        from http_support import receive_multipart
        fields, files = receive_multipart(self.rfile,length,m.group(1).encode('latin-1'),staging)

        if "media" not in files:
            return self._json({"error": "请上传视频或音频文件"}, 400)

        job_id = time.strftime("%m%d_%H%M%S") + "_" + os.urandom(2).hex()
        jd = OUT_ROOT / job_id
        indir = jd / "in"

        kwargs: dict = {}
        media = indir / 'media' / safe_name(files["media"][0])
        kwargs["media"] = str(media)

        if "audio_track" in files and files["audio_track"][1].stat().st_size:
            at = indir / 'audio_track' / safe_name(files["audio_track"][0])
            kwargs["audio_track"] = str(at)

        lyrics_text = (fields.get("lyrics_text") or "").strip()
        if "lyrics_file" in files and files["lyrics_file"][1].stat().st_size:
            lf = indir / 'lyrics' / safe_name(files["lyrics_file"][0])
            kwargs["lyrics_path"] = str(lf)
        if lyrics_text:
            kwargs["lyrics_text"] = lyrics_text

        # 无歌词文件兜底：本地 ASR 自动转写成歌词草稿
        asr_mode = fields.get("asr") not in (None, "", "0", "false", "False")
        kwargs["asr_lyrics"] = asr_mode
        kwargs['auto_retry'] = fields.get('auto_retry', '1') not in ('0','false','False','')
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
        if kwargs.get('sofa_align') and kwargs.get('lang','auto') not in {'auto','ja'}:
            return self._json({'error':'当前 SOFA 音素路线仅支持日语；中文/英文请使用 Whisper。'},400)
        kwargs["quality"] = int(fields.get("quality") or 21)
        kwargs["separate"] = (fields.get("separate", "1") not in ("0", "false", ""))
        kwargs["device"] = fields.get("device") or DEFAULT_DEVICE
        try:
            kwargs["options"] = json.loads(fields.get("options") or "{}")
        except Exception:
            kwargs["options"] = {}

        for field, key in [('media','media'),('audio_track','audio_track'),('lyrics_file','lyrics_path')]:
            if key in kwargs:
                destination = Path(kwargs[key])
                destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.move(str(files[field][1]),str(destination))
        job = Job(id=job_id, dir=jd)
        enqueue_job(job,kwargs)
        return self._json({"job": job_id})

    def _sse(self, job_id: str):
        job = load_saved_job(job_id)
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
                    payload['kind'],payload['target_job'] = job.kind,job.target_job
                    terminal = job.state in ("done", "error", "cancelled", 'interrupted')
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
        from alignment_review import attach_review
        data = attach_review(jd, data)
        return self._json(data)

    def _api_rerender(self):
        data = json.loads(self._body() or b"{}")
        job_id = data.get('job') or ''
        with LOCK:
            original = JOBS.get(job_id)
            directory = original.dir if original else saved_directory(job_id)
            if not (directory/'job.json').is_file():
                return self._json({'error':'任务目录缺少 job.json，无法调整'},400)
            if any(j.dir.resolve()==directory.resolve() and j.state in {'queued','running'} for j in JOBS.values()):
                return self._json({'error':'此记录已有任务在处理'},409)
            if data.get('async') is True:
                identifier = 'edit_'+uuid.uuid4().hex[:16]
                operation = Job(identifier,directory,kind='edit',
                    storage_dir=OUT_ROOT/'.tasks'/identifier,target_job=job_id)
                enqueue_job(operation,data)
                return self._json({'operation_job':identifier},202)
        # Keep the synchronous contract for older clients; use the same resource
        # lock as generation so there is no start/edit race.
        if not RESOURCE_LOCK.acquire(blocking=False):
            return self._json({'error':'处理队列正在使用模型，请使用异步调整'},409)
        try:
            return self._json(perform_edit(data))
        finally:
            RESOURCE_LOCK.release()

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

        from http_support import serve_file
        return serve_file(self,p,download='download' in self.path)


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
        with RESOURCE_LOCK:
            _ensure_cache(device).aligner("qwen", "zh")
        print(f"[warmup] Qwen3-ForcedAligner 预热完成（{time.time() - t0:.1f}s）")
    except Exception as e:  # noqa: BLE001
        print(f"[warmup] 跳过：{type(e).__name__}: {e}")


def main(argv=None) -> int:
    global DEFAULT_DEVICE
    ap = argparse.ArgumentParser(description="lets-karaoke 本地 WebUI")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7870)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-warmup", action="store_true", help="跳过模型预热")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--allow-remote", action="store_true",
                    help="允许监听非回环地址（服务没有登录认证，仅限可信网络）")
    args = ap.parse_args(argv)
    from http_support import _loopback
    if not _loopback(args.host.strip("[]").lower()):
        if not args.allow_remote:
            print(f"!! 拒绝监听 {args.host}：服务没有登录认证，同一网络中的任何人都能上传、删除记录并读取本机视频。")
            print("   确需远程访问时追加 --allow-remote，并只在可信网络中使用。")
            return 2
        print(f"!! 警告：正在监听 {args.host}，未启用任何认证，请勿在公共网络中使用。")
    DEFAULT_DEVICE = args.device
    STOP_WORKER.clear()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    restore_jobs()
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
        print("\n正在释放处理资源…")
    finally:
        STOP_WORKER.set()
        with LOCK:
            for job in JOBS.values():
                if job.state == 'running':
                    job.cancel_req = True
                    try:
                        persist_job(job)
                    except OSError:
                        pass
        from concert_web import shutdown
        shutdown(timeout=5)
        if WORKER:
            WORKER.join(timeout=5)
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
