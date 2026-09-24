# Project and third-party licenses

## Project code

Source code written for this project is released under the MIT License. See the repository-root [`LICENSE`](../LICENSE) for the license text. The MIT license applies to this project's original code; it does not change the license of third-party code, Python packages, model weights, external programs, or user-provided media.

Files or directories that include their own copyright or license notices keep those notices. In particular, the vendored SOFA source under [`tools/SOFA/`](../tools/SOFA/) carries its upstream MIT license in [`tools/SOFA/LICENSE`](../tools/SOFA/LICENSE).

## Project modules and direct dependencies

The requirement files select install profiles. A package may be absent when its profile is not installed.

| Project area | Requirements / component | License information |
|---|---|---|
| Core media and numerical utilities | `requirements-base.txt`: [NumPy](https://github.com/numpy/numpy), [SciPy](https://github.com/scipy/scipy), pywin32 | BSD-family licenses; pywin32 uses the Python Software Foundation license. These are not MIT. |
| Whisper alignment and vocal separation | `requirements-whisper.txt`: SoundFile, librosa, [Demucs](https://github.com/adefossez/demucs) ([MIT license](https://github.com/adefossez/demucs/blob/main/LICENSE)), [openai-whisper](https://github.com/openai/whisper) ([MIT license](https://github.com/openai/whisper/blob/main/LICENSE)), [stable-ts](https://github.com/jianfch/stable-ts) ([MIT license](https://github.com/jianfch/stable-ts/blob/main/LICENSE)) | SoundFile is BSD-3-Clause, librosa is ISC, and the linked tools are MIT. OpenAI states that Whisper's code and model weights are MIT; see its [license section](https://github.com/openai/whisper#license). Check the specific Demucs checkpoint's terms before redistributing weights. |
| Qwen alignment and transcription | `requirements-qwen.txt`: [Qwen3-ASR source](https://github.com/QwenLM/Qwen3-ASR) ([Apache-2.0 license](https://github.com/QwenLM/Qwen3-ASR/blob/main/LICENSE)), [Qwen3-ASR-1.7B model card](https://huggingface.co/Qwen/Qwen3-ASR-1.7B), [Qwen3-ForcedAligner-0.6B model card](https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B), Transformers, Hugging Face Hub | The linked Qwen model cards declare Apache-2.0. Check the model page and files for the exact revision used. |
| SOFA singing alignment | `requirements-sofa.txt`: [upstream SOFA source](https://github.com/qiuqiao/SOFA) ([MIT license](https://github.com/qiuqiao/SOFA/blob/main/LICENSE)); [checkpoint sharing discussions](https://github.com/qiuqiao/SOFA/discussions/categories/pretrained-model-sharing); [pykakasi](https://codeberg.org/miurahr/pykakasi) ([GPL-3.0-or-later license](https://codeberg.org/miurahr/pykakasi/src/branch/main/COPYING)); Lightning, TextGrid, tensorboardX, h5py, einops, PyYAML, matplotlib, pandas | SOFA source is MIT, while pykakasi is GPL-3.0-or-later. Lightning is Apache-2.0; tensorboardX, einops, and PyYAML are MIT; h5py and pandas are BSD-family; matplotlib has its own permissive license. The SOFA checkpoint-sharing page does not establish one license for every checkpoint: check the terms accompanying the exact checkpoint and dictionary. Verify TextGrid's exact distribution and all transitive dependencies before bundling. |
| Concert segmentation | `requirements-concert.txt`; external [FFmpeg/FFprobe](https://ffmpeg.org/legal.html) | Uses the core media and numerical dependencies above. FFmpeg/FFprobe are separate programs; their build options and enabled codecs determine the applicable licenses. |
| Full installation | `requirements-full.txt` and `requirements.txt` | Combines the Whisper, Qwen, and SOFA profiles and their transitive dependencies; it does not make those components MIT. |

The summary above covers project modules and their declared direct dependencies. Requirement ranges can resolve to different versions and transitive dependencies on different systems. Before distributing an installer or bundled application, generate a license inventory for that exact environment and include every required copyright, license, and NOTICE file.

## Models and media

Model code and model weights are separate works. The license for a Python library does not automatically cover a checkpoint downloaded from Hugging Face, ModelScope, or another provider. OpenAI states that Whisper's code and weights are MIT, and the two linked Qwen model cards declare Apache-2.0. The [SOFA project](https://github.com/qiuqiao/SOFA) shares checkpoints through [GitHub Discussions](https://github.com/qiuqiao/SOFA/discussions/categories/pretrained-model-sharing), but that page does not give one blanket license for every uploaded file. For SOFA and Demucs, follow the terms for the exact checkpoint and accompanying dictionary or configuration files. Model files are not included in this repository.

Songs, lyrics, videos, and other user-provided media remain subject to their own rights. The project license grants no rights to those materials.

## Optional GPL component

The SOFA Japanese text-to-phoneme path imports [pykakasi](https://codeberg.org/miurahr/pykakasi), which is [GPL-3.0-or-later](https://codeberg.org/miurahr/pykakasi/src/branch/main/COPYING). This dependency is installed by the SOFA and full profiles, and its upstream source is not copied into this repository. If you distribute an application or bundle that combines this path with pykakasi, review and satisfy the GPL obligations for that distribution. The MIT license for this project's original code does not relicense pykakasi.

## Scope

This file is a practical summary, not a substitute for the license texts distributed by each upstream project. If a summary here conflicts with an upstream license or model terms, follow the upstream terms for that component.
