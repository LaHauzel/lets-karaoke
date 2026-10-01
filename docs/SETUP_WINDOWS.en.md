# Windows setup and troubleshooting

Use this guide with the root-level setup_guide.bat. The assistant offers five independent environment profiles, explains the purpose of each step, shows installer/download progress, and runs a matching diagnostic after installation.

## Profiles

The menu order is the recommended feature order:

| Option | Profile | Function | GPU/CUDA | Models |
| --- | --- | --- | --- | --- |
| 1 | Whisper subtitles | Known-lyrics Whisper/stable-ts alignment, subtitle rendering, optional Demucs separation | Required | Whisper |
| 2 | Concert segmentation minimum | Long-video analysis, speech/music review markers, waveform editing with boundary snapping, and FFmpeg segment export | Not required | Small YAMNet model (~15.4 MiB, CPU) |
| 3 | Qwen full subtitles | Whisper plus Qwen ForcedAligner, ASR lyric drafts, and Wav2Vec2 support | Required | Whisper, ForcedAligner, ASR |
| 4 | SOFA singing alignment | Whisper line windows followed by SOFA phoneme-level singing alignment | Required | Whisper, SOFA checkpoint |
| 5 | Full environment | Whisper, Qwen, SOFA, Demucs, and all supported subtitle backends | Required | Whisper, Qwen, SOFA, small YAMNet classifier |

Large subtitle models remain a separate step. Option 2 also downloads a small local YAMNet audio classifier (~15.4 MiB) for speech/music review markers. It uses the CPU and never changes cut boundaries automatically. Concert-only setup still skips CUDA, Whisper, Qwen, and SOFA.

## Prerequisites

1. Install 64-bit Python 3.11.x and verify that the command-line python points to it. The installer rejects other versions because the model dependency combination has not been validated on them.
2. Install FFmpeg and add the directory containing ffmpeg.exe and ffprobe.exe to PATH.
3. NVIDIA drivers and working CUDA are required for profiles 1, 3, 4, and 5. Profile 2 does not need a GPU.
4. Keep enough disk space for dependencies, model caches, and exports. pip and the model downloaders print live progress.

Verify the base tools:

~~~bat
python --version
ffmpeg -version
ffprobe -version
~~~

## Recommended installation

From the repository directory, run:

~~~bat
setup_guide.bat
~~~

The default scripts install into that interpreter. For an isolated environment:

~~~bat
setup_venv.bat whisper
webui_venv.bat
~~~

Replace `whisper` with the required profile. Use `.venv\Scripts\python.exe` for downloads and diagnostics, or activate `.venv\Scripts\activate.bat` first. GPU setup reuses an existing installation only after checking PyTorch/torchaudio 2.9.0, CUDA 12.8, and an actual GPU tensor operation.

Choose the profile that matches the work:

- Concert segmentation only: choose **[2]**. It installs requirements-concert.txt and the small speech/music classifier for the concert tab.
- Known-lyrics Whisper alignment: choose **[1]**, then choose **[6]** to download a Whisper checkpoint.
- Qwen alignment or lyric drafts without a lyric file: choose **[3]**, then download the Whisper and Qwen models with **[6]** and **[7]**.
- SOFA alignment: choose **[4]**, download Whisper with **[6]**, and place the SOFA checkpoint manually.
- All features: choose **[5]**, then download the models you need.

The profiles can also be run directly:

~~~bat
call setup_profile.bat whisper
call setup_profile.bat concert
call setup_profile.bat qwen
call setup_profile.bat sofa
call setup_profile.bat full
~~~

setup.bat remains a compatibility entry point for the full environment:

~~~bat
setup.bat
~~~

## Model downloads

In the setup assistant:

- **[6] Download Whisper model** downloads large-v3 to models\whisper\large-v3.pt. It is used by the Whisper subtitle profile and as the timing pre-pass for Qwen/SOFA.
- **[7] Download Qwen models** lets you choose ForcedAligner (about 1.8 GB), ASR (about 4.7 GB), or both. The downloader prints file progress and reuses completed files when run again.
- SOFA does not currently have one reliable automatic download source. Place v1.0.0_multilingual_singing.ckpt at:

~~~text
models\sofa\multilingual\pretrained_multilingual_singing\v1.0.0_multilingual_singing.ckpt
~~~

Equivalent command-line operations:

~~~bat
python src\fetch_whisper.py --model large-v3
python src\fetch_models.py --only aligner
python src\fetch_models.py --only asr
python src\fetch_models.py
python src\fetch_models.py --list
~~~

## Diagnostics and startup

Option **[8]** checks the base media tools, model status, and local disk space. A profile can also be checked directly:

~~~bat
python src\check_environment.py --profile concert
python src\check_environment.py --profile whisper
python src\check_environment.py --profile qwen
python src\check_environment.py --profile sofa
python src\check_environment.py --profile full
~~~

FAIL means that a dependency, FFmpeg, or CUDA requirement must be fixed. Missing model weights are reported as WARN so that a profile can be installed before downloading optional models.

Start the one local backend service:

~~~bat
webui.bat
~~~

The default URL is http://127.0.0.1:7870/. Karaoke and concert segmentation are independent tabs served by the same process. Do not start multiple WebUI instances.

## Troubleshooting

### Wrong Python version

~~~bat
python --version
where python
~~~

Install the tested Python 3.11.x and reopen the terminal so the installer and launcher use the same interpreter. If the default `python` is another version (for example 3.12), keep it and use a virtual environment:

~~~bat
winget install --id Python.Python.3.11 -e --scope user
setup_venv.bat whisper
~~~

`setup_venv.bat` creates `.venv` through `py -3.11`. If it reports that an existing `.venv` is not 3.11, rename or delete that folder and retry. The Python installer may move 3.11 to the front of the user PATH, changing the default `python` for other projects; confirm with `python --version`.

### FFmpeg is not found

Add FFmpeg's bin directory to PATH, reopen the terminal, and verify that ffmpeg -version and ffprobe -version both run. `winget install --id Gyan.FFmpeg -e` is recommended; winget only updates newly opened terminals, including a fresh setup assistant window.

### "is not recognized as an internal or external command" at startup

Older `webui.bat` versions contained UTF-8 Chinese comments that cmd misparsed under the GBK (936) code page. The script is now ASCII-only; keep `.bat` files ASCII when editing them.

### CUDA is unavailable

Only Whisper, Qwen, SOFA, and full profiles require CUDA. For concert segmentation alone, use **[2]**. For the other profiles, confirm that the NVIDIA driver works and rerun the profile installer to install the CUDA 12.8 PyTorch wheels.

### Interrupted downloads

Run the same download option again. Completed files in the model directory are reused. If the network is restricted, fix proxy or mirror access before retrying.

### Port conflict

The default port is 7870. Stop the old webui.py process or run:

~~~bat
python src\webui.py --port 8000
~~~

Run the regression suite with:

~~~bat
test.bat
~~~

The tests use repository-generated synthetic media and do not read personal files.
