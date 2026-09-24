# lets-karaoke

New to the project? See the [illustrated user guide (Chinese)](docs/USER_GUIDE.md) for installation, model downloads, subtitle editing, and concert segmentation.

[中文](README.md)

A local-first toolkit for lyric alignment, karaoke subtitle rendering, and concert video segmentation on Windows. Media processing uses local models and FFmpeg; no online API is required.

> **Project status:** The project is usable locally and continues to evolve. Review generated timings and concert boundaries before exporting.

## Features

- Word- and character-level alignment for supplied lyrics with Whisper/stable-ts.
- ASR draft generation when no lyric file is available, followed by manual correction and re-alignment.
- Alignment diagnostics, low-confidence markers, anchor editing, version history, and fast re-rendering.
- Enhanced LRC, SRT, and ASS output, with optional FFmpeg subtitle burn-in.
- Acoustic boundary suggestions for long concert videos based on volume dips and spectral changes.
- Interactive waveform and time-table editing: seek, drag boundaries, split at the playhead, merge adjacent segments, and edit timestamps.
- Multiple sensitivity versions per concert record, with batch export to MKV or MP4.
- One local backend service shared by the two independent WebUI tabs.

## Limitations

- Concert segmentation is an acoustic heuristic. It does not identify song titles, transcribe lyrics, or classify speech, applause, and music. A candidate boundary is not a song count or probability.
- Applause, stage talk, continuous accompaniment, pauses inside a song, and medleys can produce false or missed boundaries. Listen to each boundary before exporting.
- Fast export copies the original streams and writes MKV. Keyframe placement can cause a clip to start slightly before the requested time. Use precise export when exact cuts are required; it re-encodes and takes longer.
- Browser preview depends on the source codec. A file that cannot be previewed in the browser may still be analyzable and exportable through FFmpeg.

## Requirements

- Windows 11 (the primary validation environment).
- Python 3.11 or later.
- FFmpeg and FFprobe available on `PATH`.
- Sufficient disk space for dependencies, model caches, and exports. Model weights are not included in this repository.

Whisper, Qwen, SOFA, and full profiles require an NVIDIA GPU, working CUDA, and matching PyTorch/torchaudio; the concert segmentation minimum profile does not need a GPU. The GPU installer uses CUDA 12.8 wheels. Run the diagnostic before starting:

```bat
python src\check_environment.py
```

## Installation

Use the system-default Python 3.11. The batch scripts install into and run the `python` executable resolved from the current shell; they do not create a virtual environment or depend on ComfyUI.

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
| 2 | Concert segmentation minimum | Long-video analysis, waveform editing, FFmpeg segment export | No GPU or models |
| 3 | Qwen full subtitles | Whisper plus Qwen alignment, ASR drafts, and Wav2Vec2 | GPU; Whisper/Qwen models |
| 4 | SOFA singing alignment | Whisper line windows and SOFA phoneme-level alignment | GPU; Whisper/SOFA checkpoint |
| 5 | Full environment | All subtitle backends and concert segmentation | GPU; download models as needed |

See [Windows setup and troubleshooting](docs/SETUP_WINDOWS.en.md). If you only need concert segmentation, choose option 2 instead of installing the full environment.

Download the local models when needed:

```bat
python src\fetch_whisper.py --model large-v3
python src\fetch_models.py --list
python src\fetch_models.py
```

The Whisper checkpoint is stored under `models/whisper`. The ForcedAligner model is required for Qwen supplied-lyrics alignment; the ASR model is needed for lyric drafts without a lyric file. The SOFA checkpoint must currently be placed at `models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt`. Model sources and names are defined in `src/fetch_models.py`.

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

- `--host`: bind address; defaults to `127.0.0.1`.
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
5. Save a version and restore, re-render, or delete it from history.

When lyrics are unavailable, use automatic transcription to create a draft. Correct the text and line breaks before using known-lyrics alignment for the final result.

## Concert segmentation workflow

1. Enter the full path to a local video. Supported containers include MP4, MKV, MOV, AVI, WebM, M4V, TS, and MTS. The file must contain readable video, duration, and an audio stream.
2. Set the minimum candidate length and sensitivity (conservative, balanced, or sensitive), then start analysis. The analyzer extracts low-rate audio summaries one second at a time and does not load GPU models.
3. Review candidates in the table and player. The waveform supports seeking, dragging blue boundaries, splitting at the playhead, and deleting a boundary to merge adjacent segments. Edits immediately update the time table.
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
