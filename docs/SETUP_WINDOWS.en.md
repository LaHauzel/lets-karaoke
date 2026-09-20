# Windows setup and troubleshooting

Use this guide with the root-level `setup_guide.bat`. The assistant provides an interactive menu for diagnostics, dependency installation, model downloads, and WebUI startup.

## Recommended setup

1. Install Python 3.11 or later and ensure `python` resolves to it.
2. Install FFmpeg and add the directory containing `ffmpeg.exe` and `ffprobe.exe` to `PATH`.
3. Run `setup_guide.bat` from the repository directory.
4. Choose **Install or repair Python dependencies**.
5. Choose **Detailed environment check**. Fix every `FAIL`; missing models are reported as `WARN`.
6. Download the **ForcedAligner** model (about 1.8 GB) for supplied-lyrics alignment.
7. Download the **ASR** model (about 4.7 GB) when you need automatic lyric drafts.
8. Choose **Start WebUI** and open <http://127.0.0.1:7870/>.

## Menu options

| Option | Purpose |
| --- | --- |
| Detailed environment check | Checks Python, imports, FFmpeg/FFprobe, CUDA/GPU, models, and free disk space |
| Install or repair Python dependencies | Installs CUDA PyTorch/torchaudio and `requirements.txt` |
| Download ForcedAligner model | Downloads Qwen3-ForcedAligner-0.6B |
| Download ASR model | Downloads Qwen3-ASR-1.7B |
| Download all models | Downloads both models |
| Show model status | Reports model completeness without downloading |
| Start WebUI | Starts the single local backend service |
| Open setup guide | Opens the Chinese guide included with the batch assistant |

Model downloads are handled by `src/fetch_models.py`. It tries ModelScope, the Hugging Face mirror, and the official source in that order. The underlying downloaders show progress and can be run again to resume incomplete downloads.

## Troubleshooting

### Python version

Run:

```bat
python --version
where python
```

Install Python 3.11+ and reopen the terminal if the version is too old.

### FFmpeg

After adding FFmpeg to `PATH`, reopen the terminal and verify:

```bat
ffmpeg -version
ffprobe -version
```

### CUDA

Confirm that the NVIDIA driver is working and that PyTorch and torchaudio are installed as a matching pair. The repair option reinstalls the CUDA 12.8 wheels used by this project.

### Models

The model directories are:

- `models/Qwen3-ForcedAligner-0.6B`
- `models/Qwen3-ASR-1.7B`

Run the matching download option again if a download was interrupted. The downloader reuses existing files and checks `config.json` plus weight files.

### Slow or interrupted downloads

You can run these commands directly:

```bat
python src\fetch_models.py --only aligner
python src\fetch_models.py --only asr
python src\fetch_models.py --hf
```

The `--hf` option skips ModelScope and prefers the Hugging Face route. Fix proxy or network access outside the repository rather than committing proxy settings.

### Port conflicts

The default port is `7870`. Stop old `webui.py` processes or start an alternate port:

```bat
python src\webui.py --port 8000
```

Run only one WebUI instance at a time.

## Manual setup

```bat
python src\check_environment.py
call setup.bat
python src\fetch_models.py --list
python src\fetch_models.py --only aligner
webui.bat
```

