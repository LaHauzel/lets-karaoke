# lets-karaoke

New to the project? See the [illustrated user guide (Chinese)](docs/USER_GUIDE.md) for installation, model downloads, subtitle editing, and concert segmentation.

[中文](README.md)

A local-first toolkit for lyric alignment, karaoke subtitle rendering, and concert video segmentation on Windows. Media processing uses local models and FFmpeg; no online API is required.

> **Project status:** The project is usable locally and continues to evolve. Review generated timings and concert boundaries before exporting.

## Features

- Word- and character-level alignment for supplied lyrics with Whisper/stable-ts.
- Batch processing for multiple video/audio files or an entire folder; select each queue item to assign its own lyrics, alignment language, and subtitle style, with a separate history record for every input.
- ASR draft generation when no lyric file is available, followed by manual correction and re-alignment.
- Optional Qwen/Wav2Vec2 acoustic alignment. The current SOFA phoneme refinement route supports Japanese only; use Whisper for other languages.
- Alignment diagnostics, low-confidence markers, anchor editing, version history, and fast re-rendering.
- Structural acceptance checks flag missing lyrics, lines that flash past, and invalid word timings; batch results summarize items needing correction or review.
- Per-line subtitle positioning, adjustable line count, fonts, sizes, and highlight colors, with a live preview on any frame of the selected video.
- Enhanced LRC, SRT, and ASS output, with optional FFmpeg subtitle burn-in.
- Acoustic boundary suggestions for long concert videos based on volume dips and spectral changes, with YAMNet and WebRTC VAD speech/music review markers. Markers never cut media automatically and may still flag singing or crowd interaction.
- Interactive waveform and time-table editing: seek, drag boundaries, snap the nearest boundary to the current time, split at the playhead, merge adjacent segments, and edit timestamps.
- Multiple sensitivity versions per concert record, with batch export to MKV or MP4.
- Import an exported concert segment into the karaoke subtitle workspace with one click.
- One local backend service shared by the two independent WebUI tabs.
- Generation and timeline edits share a durable processing queue, with progress recovery after page reload, automatic recovery of pending jobs after restart, explicit retries for interrupted jobs, cancellable edits, and local draft storage.
- Concert export records completed clips incrementally and can resume remaining clips after cancellation or interruption.

## Limitations

- Concert boundaries use volume and timbre heuristics, with YAMNet/WebRTC VAD speech review markers. Song titles are not recognized. Candidate counts are not song counts; classifier scores are not calibrated concert probabilities.
- Applause, stage talk, continuous accompaniment, pauses inside a song, and medleys can produce false or missed boundaries. Listen to each boundary before exporting.
- Fast export copies the original streams and writes MKV. Keyframe placement can cause a clip to start slightly before the requested time. Use precise export when exact cuts are required; it re-encodes and takes longer.
- Browser preview depends on the source codec. A file that cannot be previewed in the browser may still be analyzable and exportable through FFmpeg.
- Vocal energy can help bound a sung passage, but crowd noise, quiet vocals, and separation errors make it unreliable as a substitute for matching the lyrics to the voice. Acceptance checks and model confidence are not accuracy estimates; review uncertain lines by listening.

## Requirements

- Windows 11 (the primary validation environment).
- Python 3.11.x, 64-bit (the tested installer target; other versions are outside the supported installation profile).
- FFmpeg and FFprobe available on `PATH`.
- Sufficient disk space for dependencies, model caches, and exports. Model weights are not included in this repository.

Whisper, Qwen, SOFA, and full profiles require an NVIDIA GPU, working CUDA, and matching PyTorch/torchaudio; the concert segmentation minimum profile does not need a GPU. The GPU installer uses CUDA 12.8 wheels. Run the diagnostic before starting:

```bat
python src\check_environment.py
```

## Installation

By default, batch scripts install into the Python 3.11 executable resolved from the current shell. An isolated environment is also available and does not depend on ComfyUI:

```bat
setup_venv.bat whisper
webui_venv.bat
```

Replace `whisper` with `concert`, `qwen`, `sofa`, or `full` as needed. The environment lives in `.venv/`; use `.venv\Scripts\python.exe` for model downloads when using this option. When `.venv` exists, `webui.bat`, `test.bat`, and `setup_guide.bat` use it automatically.

If the default `python` is not 3.11 (for example 3.12 used by other projects), use the virtual environment instead of changing the system default:

```bat
winget install --id Python.Python.3.11 -e --scope user
winget install --id Gyan.FFmpeg -e
rem Close and reopen the terminal so the PATH written by winget takes effect
setup_venv.bat whisper
```

`setup_venv.bat` prefers a 3.11 `python` on PATH and otherwise creates `.venv` through `py -3.11`; it stops with a message if an existing `.venv` was not created with 3.11. The Python installer may move 3.11 to the front of the user PATH and change the default `python` for other projects; check `python --version` afterwards. The first GPU profile install downloads a ~2.9 GB CUDA PyTorch wheel, and pip may show no progress while output is redirected.

```bat
git clone https://github.com/LaHauzel/lets-karaoke.git
cd lets-karaoke
setup_guide.bat
```

`setup.bat` is a compatibility entry point for the full environment. For a focused install, run `call setup_profile.bat whisper|concert|qwen|sofa|full`. `requirements.txt` aggregates the full environment; use the profile script for a minimal deployment. Install FFmpeg separately and verify:

```bat
ffmpeg -version
ffprobe -version
```

For an interactive Windows setup flow, run `setup_guide.bat`. It presents these profiles in order and explains the function of each step while showing installer progress:

| Option | Profile | Use case | GPU/models |
| --- | --- | --- | --- |
| 1 | Whisper subtitles | Known-lyrics alignment, subtitle rendering, optional separation | GPU; Whisper model |
| 2 | Concert segmentation minimum | Long-video analysis, speech/music review markers, waveform editing, FFmpeg segment export | CPU; 15.4 MiB YAMNet |
| 3 | Qwen full subtitles | Whisper plus Qwen alignment, ASR drafts, and Wav2Vec2 | GPU; Whisper/Qwen models |
| 4 | SOFA singing alignment | Whisper line windows and SOFA phoneme-level alignment | GPU; Whisper/SOFA checkpoint |
| 5 | Full environment | All subtitle backends and concert segmentation | GPU; subtitle models as needed, small YAMNet classifier |

See [Windows setup and troubleshooting](docs/SETUP_WINDOWS.en.md). If you only need concert segmentation, choose option 2 instead of installing the full environment. Project scope, remaining gaps, and comparisons are documented in the Chinese [project review](docs/PROJECT_REVIEW.md) and [competitive analysis](docs/COMPETITIVE_ANALYSIS.md).

Download the local models when needed:

```bat
python src\fetch_whisper.py --model large-v3
python src\fetch_models.py --list
python src\fetch_models.py
```

The Whisper checkpoint is stored under `models/whisper`. The concert profile downloads a small YAMNet audio classifier for speech-versus-music review markers. It runs on the CPU and never changes cut boundaries automatically. Check or retry its download with:

```bat
python src\fetch_concert_model.py --status
python src\fetch_concert_model.py
```

The ForcedAligner model is required for Qwen supplied-lyrics alignment; the ASR model is needed for lyric drafts without a lyric file. The SOFA checkpoint must currently be placed at `models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt`. Model sources and names are defined in `src/fetch_models.py`.

## Start the WebUI

```bat
webui.bat
```

Open <http://127.0.0.1:7870/>. The service binds to the local loopback interface by default. The same process serves the browser UI, subtitle jobs, and concert segmentation jobs.

You can also start it directly:

```bat
python src\webui.py --no-open
python src\webui.py --port 8000 --no-open
```

Common options:

- `--host`: bind address; defaults to `127.0.0.1`. The service has no authentication: a non-loopback address such as `0.0.0.0` also requires `--allow-remote`, otherwise the server refuses to start. Use it only on trusted networks.
- `--port`: listen port; defaults to `7870`.
- `--no-open`: do not open a browser automatically.
- `--no-warmup`: skip model warmup at startup.
- `--device`: inference device; defaults to `cuda`.

The workspace has two independent tabs:

- **Karaoke subtitles:** media input, lyrics, alignment, diagnostics, manual editing, and subtitle rendering. Direct route: `/karaoke`.
- **Concert segmentation:** long-video analysis, boundary review, segment editing, and export. Direct routes: `/#concert` and `/concert`.

The tabs share the local service but keep their page state separate. Do not run multiple WebUI instances at the same time.

## Karaoke subtitle workflow

1. Upload an audio or video file and provide lyrics as text or TXT/LRC/SRT.
2. Choose the language, alignment backend, and output style, then start the job.
3. Review line timings, confidence, and acoustic evidence in the result area.
4. Adjust timestamps or set anchors, then re-align or re-render.
5. Save a version, restore or re-render it from history, or delete the entire history record.

When lyrics are unavailable, use automatic transcription to create a draft. Correct the text and line breaks before using known-lyrics alignment for the final result.

## Concert segmentation workflow

1. Enter the full path to a local video. Supported containers include MP4, MKV, MOV, AVI, WebM, M4V, TS, and MTS. The file must contain readable video, duration, and an audio stream.
2. Set the minimum candidate length and sensitivity (conservative, balanced, or sensitive), then start analysis. The analyzer extracts low-rate audio summaries one second at a time and does not load GPU models.
3. Review candidates in the table and player. The waveform supports seeking, dragging blue boundaries, snapping the nearest boundary to the playhead, splitting at the playhead, and deleting a boundary to merge adjacent segments. Edits immediately update the time table.
4. Select an existing record to change sensitivity or minimum length. “Re-analyze this record” creates a new version under the same record and preserves earlier versions and completed exports.
5. Save the time table, select segments, and choose an export mode:
   - **Fast export:** stream copy to MKV; fast, but keyframes affect the exact cut.
   - **Precise export:** H.264/AAC re-encode to MP4; more accurate, but slower.
6. Exports are stored under `out/concert/<job-id>/export-<batch-id>/` with a `manifest.json`. The record, versions, and edits are stored in its `concert.json`.

The source video is never modified. Cancelling an export removes unfinished clips but keeps completed files in the batch.

## Data and privacy

- Media processing, model inference, and the HTTP service run locally; user media is not uploaded to a remote service.
- Subtitle jobs are stored under `out/webui/`; concert records and exports are stored under `out/concert/`.
- Model caches are stored under `models/`. These paths are ignored by Git and should not be committed.
- Concert records include the source path, analysis parameters, segment table, and export metadata. Review and remove `out/` before sharing a project directory.

## Tests

The repository includes a synthetic sample set and ground truth under `data/synth/`; `python src\gen_synth.py --tts sapi` regenerates it with local Chinese, English, and Japanese SAPI voices and overwrites the existing files. `tests\e2e_p1.py` uses the Qwen ForcedAligner backend and needs the Qwen profile and model; with only the Whisper profile, run `python tests\system_smoke_test.py --lang zh` to check the Whisper known-lyrics route. Integration success requires full text coverage, valid subtitle structure, nonempty outputs, and a default synthetic token start P90 budget of 250ms; see the [validation matrix](docs/VALIDATION.md).

The test suite uses repository-generated synthetic audio and video and does not read personal media:

```bat
test.bat
```

The script compiles `src/` and `tools/`, then runs alignment, history, HTTP, and concert segmentation regression tests. Full GPU end-to-end tests require installed dependencies and prepared model weights and are not part of the default unit-test script.

## Project layout

```text
src/
  webui.py              local HTTP service and workspace routes
  concert_splitter.py   acoustic features, boundary suggestions, FFmpeg export
  concert_web.py        concert jobs, versions, and history APIs
  webui_assets/         karaoke and concert tab assets
tests/                  unit and HTTP regression tests
data/synth/             synthetic test media and manifests
tools/SOFA/             optional singing alignment backend
docs/                   dependency and release documentation
models/                 local model cache (not committed)
out/                    local history and exports (not committed)
```

## License and third-party components

Project-authored code is released under the MIT License; see the repository-root [LICENSE](LICENSE). Modules, direct dependencies, the optional SOFA backend, model weights, and external tools retain their own licenses. See [Project and third-party licenses](docs/LICENSES.md) and the [dependency license review](docs/DEPENDENCY_LICENSES.md). Before distributing a bundled application, verify the full dependency inventory and include the required notices for that build.

Users are responsible for the rights to their songs, lyrics, concert videos, and other media. This project does not grant rights to use or redistribute those materials.

## Contributing and issue reports

When opening an issue or pull request, include:

- Windows, Python, PyTorch, and FFmpeg versions;
- the workspace tab and reproducible steps;
- relevant logs or a minimal synthetic sample;
- no unlicensed songs, lyrics, concert videos, model weights, or `out/` directory.
