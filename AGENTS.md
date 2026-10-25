# AGENTS.md

Guidance for AI coding agents working in this repository.
Human-facing docs are authoritative: `README.md` (Chinese), `README.en.md` (English), `docs/`.

## What this project is

`lets-karaoke` is a **local-first Windows application** for:
- aligning existing lyrics (TXT / LRC / SRT) to audio/video and generating karaoke subtitles (enhanced LRC, SRT, ASS; burned-in MP4 or soft-subtitle MKV);
- optional ASR drafts, LRCLIB lyric lookup, alignment diagnostics/anchors, version history, and human review of finished videos;
- segmenting long concert videos by volume/timbre boundaries and exporting selected segments;
- batch repair/revision workflows over existing alignment jobs (`repair_*.py`, `feedback_revision.py`, `multitrack.py`).

Stack: Python 3.11 (backend, standard-library `ThreadingHTTPServer`), vanilla HTML/CSS/JS frontend (no framework, no bundler, no build step), FFmpeg/FFprobe as external programs, and optional PyTorch model backends (Whisper/stable-ts, Qwen3 forced aligner, SOFA Japanese phoneme alignment, Demucs vocals, YAMNet speech/music hints). Model weights are **not** in the repo.

## Environment

- **Primary target: Windows 11.** Paths, batch launchers, and GPU/WDDM handling are Windows-first; CI runs on `windows-latest`.
- **Python 3.11.x 64-bit** is the only supported installer version (`setup_*.bat` reject other versions). Use `py -3.11` if the default `python` is something else.
- If `.venv\` exists, the project prefers `.venv\Scripts\python.exe`; `webui.bat`, `test.bat`, and the setup scripts prepend it to `PATH`. Running tests directly should use that interpreter.
- **FFmpeg and FFprobe must be on `PATH`** (many tests actually invoke them).
- **Node.js 22+** is required for the frontend regression tests; `test.bat` fails if `node` is missing.
- CPU PyTorch is sufficient for the offline suite; GPU/model downloads are only needed for end-to-end checks.

## Commands

```bat
setup_guide.bat                          :: interactive installer
setup_venv.bat whisper|concert|qwen|sofa|full   :: profile-specific deps into .venv
webui.bat                                :: starts http://127.0.0.1:7870
test.bat                                 :: compileall + offline Python/JS regression suite
```

```bat
python src\webui.py --port 8000 --no-open   :: direct start
python src\check_environment.py             :: profile-aware environment check
```

Individual tests (run from the repo root):

```bat
.venv\Scripts\python.exe -m unittest tests.alignment_policy_test -v
.venv\Scripts\python.exe tests\alignment_policy_test.py
node tests\lyrics_format_test.js
```

GPU/model end-to-end checks are **not** part of the default suite and must not run unless a task needs them (they download/load models and need CUDA):
`python tests\e2e_p1.py --device cuda` (Qwen profile), `python tests\system_smoke_test.py --lang zh [--asr]` (Whisper profile).

## Repository layout

```text
src/
  webui.py                 stdlib HTTP server, subtitle workspace routes, job queue/SSE
  concert_web.py           concert jobs, versions, history, export routes
  review_web.py            delivery library + human-review routes
  pipeline.py              main karaoke pipeline (media -> vocals -> alignment -> subtitles -> video)
  align_backends.py        alignment backend selection/wrappers (Whisper, Qwen, SOFA, CTC)
  whisper_align.py         forced alignment for known lyrics (whisper/stable-ts)
  asr_lyrics.py            local ASR draft + line splitting
  song/alignment helpers   alignment_policy.py, alignment_retry.py, alignment_diagnostics.py,
                           alignment_review.py, anchor_realign.py, lyric_waveform.py
  subtitles                ass_builder.py, lyrics_search.py (LRCLIB), p0_common.py
  concert_splitter.py      bounded-memory analysis and FFmpeg segment export
  concert_sound_classifier.py, fetch_concert_model.py   optional YAMNet speech/music hints
  review_* and repair_*    catalog/store/workflow/native, repair pipeline/report/delivery
  revision_policy.py, feedback_revision.py, multitrack.py, archive_library.py
  publish_export.py        platform-ready submission packages (FFmpeg, cover, publish.json, hashes)
  publish_web.py           publish workspace routes, preflight and background generation
  process_runner.py, http_support.py, local_history.py, model_paths.py, whisper_compat.py
  webui_assets/            all frontend HTML/CSS/JS (karaoke, concert, review/library, publish)
tests/                     offline unittest modules (*_test.py), Node tests (*_test.js), GPU e2e scripts
data/synth/                checked-in synthetic audio + manifest used by tests
tools/                     vendored optional backends (SOFA, llama-cpp binaries) + smoke helpers
models/                    local model cache (gitignored)
out/                       local task state, exports, history (gitignored)
```

Things are not a package: modules import each other as **top-level modules** (`import whisper_align`), and every entry point inserts `src/` into `sys.path`. `python src\foo.py`, `python -m src.foo`, and `python -m unittest tests.x_test` are all expected to work.

## Architecture invariants

- One local server process serves the four workspaces (`/karaoke` or `/`, `/concert`, `/review`, `/publish`); they share the job queue and local storage but keep independent page state. Do not start multiple WebUI instances.
- Media processing, model inference, and storage are local. The only outbound calls are optional LRCLIB lyric search/preview (`src/lyrics_search.py`): fixed `https://lrclib.net`, TLS verification on, no proxy/redirects, no media paths or lyrics sent. Do not add other outbound requests or weaken these limits. Publish preparation (`/publish`) generates local packages only: it must not log in to a platform, store cookies, or upload automatically.
- The server is unauthenticated and loopback-only by default; remote binding requires `--allow-remote` and should not be loosened.
- Task state is persisted under `out/webui/<job>/`; concert records/exports under `out/concert/`, review data under `out/concert-collection/`. Generated state must stay out of git.
- Subprocesses (FFmpeg, model workers) must go through the existing helpers (`process_runner.py`) so cancellation, deadlines, and Windows process-tree cleanup keep working.

## Coding conventions

### Python
- Python 3.11, standard library first; most modules use `from __future__ import annotations`.
- UTF-8 source; Chinese and English docstrings/comments coexist (older pipeline modules are largely Chinese, newer repair/review modules English). User-facing UI text is Chinese.
- Preserve existing public behavior and error text: HTTP/regression tests assert status codes, JSON shapes, cancellation, recovery, and safety limits.
- Keep unit tests offline and deterministic: no model downloads, no network, no personal media; use synthetic media in `data/synth/` and test doubles for model inference.
- Avoid new dependencies; if one is unavoidable, add it to the correct `requirements-<profile>.txt` and keep `requirements-full.txt`/`requirements.txt` consistent (pinned range style). `python -m pip check` is part of CI.

### Frontend
- Plain HTML/CSS/JS served by the stdlib server from `src/webui_assets/`. No build step, framework, or bundler — do not introduce one.
- Shared logic lives in `.js` files using the UMD wrapper (`module.exports` for Node tests, `root.X` in the browser). Keep DOM code and testable logic separated.
- Reuse the existing dark studio theme (`theme.css`, workspace shell, workflow bar); check narrow (390 px) layouts when touching UI.
- Route handlers live in `webui.py` / `concert_web.py` / `review_web.py`; add assets to the allow-lists there rather than reading arbitrary paths.

### Tests
- Name files `tests/<topic>_test.py` / `tests/<topic>_test.js`.
- `tests/run_tests.py` discovers only `*_test.py` modules that define a `unittest.TestCase` subclass at module level; follow that pattern.
- HTTP tests may start the real local server; FFmpeg/libass behavior should be exercised for real where the suite already does so.
- JS tests are plain `node:assert/strict` scripts that print `PASS ...`; no jest/mocha.
- Do not regenerate `data/synth/` unless explicitly asked (it changes the checked-in ground truth).

### Batch scripts
- Keep `.bat` files ASCII-only where noted (`webui.bat`): `cmd` reads them with the system code page (e.g. GBK), so non-ASCII bytes break parsing. Use `chcp 65001` / `PYTHONUTF8=1` for output instead.

## Git, CI, and docs

- Conventional commit messages: `feat(scope): ...`, `fix: ...`, `docs: ...`, `test: ...`.
- CI is `.github/workflows/tests.yml` (Windows, Python 3.11, Node 22, CPU Torch, FFmpeg) and runs `test.bat`. Keep it green.
- Update `CHANGELOG.md` (`Unreleased`) and, for meaningful verification claims, `docs/VALIDATION.md` (record scope and what was *not* verified — do not overstate accuracy or security guarantees).
- Never commit `out/`, `models/`, `.venv/`, `.local-audits/`, model weights, or any user media/lyrics/subtitles; `.gitignore` covers them.

## Before finishing a change

1. Run `test.bat` (or at least `python -m compileall -q src tools` plus the affected Python and Node tests).
2. For HTTP/UI changes, start the server (`webui.bat`) and exercise the affected route/workspace; keep one instance only.
3. Note explicitly what could not be verified locally (GPU/model routes, Windows installer paths, browser-only behavior).
4. Confirm no generated artifacts or personal media were added to git.
