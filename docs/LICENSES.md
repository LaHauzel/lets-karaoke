# Project and third-party licenses

## Project code

Source code written for this project is released under the MIT License. See the repository-root [`LICENSE`](../LICENSE) for the license text. The MIT license applies to this project's original code; it does not change the license of third-party code, Python packages, model weights, external programs, or user-provided media.

Files or directories that include their own copyright or license notices keep those notices. In particular, the vendored SOFA source under [`tools/SOFA/`](../tools/SOFA/) carries its upstream MIT license in [`tools/SOFA/LICENSE`](../tools/SOFA/LICENSE).

## Project modules and direct dependencies

The requirement files select install profiles. A package may be absent when its profile is not installed.

| Project area | Requirements / component | License information |
|---|---|---|
| Core media and numerical utilities | `requirements-base.txt`: NumPy, SciPy, pywin32 | BSD-family licenses; pywin32 uses the Python Software Foundation license. These are not MIT. |
| Whisper alignment and vocal separation | `requirements-whisper.txt`: SoundFile, librosa, Demucs, openai-whisper, stable-ts | BSD-3-Clause, ISC, and MIT licenses. Demucs code and Whisper tooling are MIT; downloaded model weights have their own terms. |
| Qwen alignment and transcription | `requirements-qwen.txt`: qwen-asr, Transformers, Hugging Face Hub | Apache-2.0. Qwen model cards currently declare Apache-2.0; check the specific model revision and its files before redistribution. |
| SOFA singing alignment | `requirements-sofa.txt`: pykakasi, Lightning, TextGrid, tensorboardX, h5py, einops, PyYAML, matplotlib, pandas; vendored SOFA source | Mixed licenses: pykakasi is GPL-3.0-or-later; Lightning is Apache-2.0; tensorboardX, einops, and PyYAML are MIT; h5py and pandas are BSD-family; matplotlib has its own permissive license; SOFA source is separately MIT-licensed. Verify the exact TextGrid distribution and all transitive dependencies for the chosen release before bundling them. |
| Concert segmentation | `requirements-concert.txt` | Uses the core media and numerical dependencies above. FFmpeg and FFprobe are separate programs installed by the user; their build and enabled codecs determine their licenses. |
| Full installation | `requirements-full.txt` and `requirements.txt` | Combines the Whisper, Qwen, and SOFA profiles and their transitive dependencies; it does not make those components MIT. |

The summary above covers project modules and their declared direct dependencies. Requirement ranges can resolve to different versions and transitive dependencies on different systems. Before distributing an installer or bundled application, generate a license inventory for that exact environment and include every required copyright, license, and NOTICE file.

## Models and media

Model code and model weights are separate works. The license for a Python library does not automatically cover a checkpoint downloaded from Hugging Face, ModelScope, or another provider. Check the license and access terms on the exact model page and revision. Qwen model cards declare Apache-2.0; SOFA, Demucs, Whisper, and other checkpoint terms must be checked at their respective download sources before the weights are redistributed. Model files are not included in this repository.

Songs, lyrics, videos, and other user-provided media remain subject to their own rights. The project license grants no rights to those materials.

## Optional GPL component

The SOFA Japanese text-to-phoneme path imports pykakasi, which is GPL-3.0-or-later. This dependency is installed by the SOFA and full profiles, and its upstream source is not copied into this repository. If you distribute an application or bundle that combines this path with pykakasi, review and satisfy the GPL obligations for that distribution. The MIT license for this project's original code does not relicense pykakasi.

## Scope

This file is a practical summary, not a substitute for the license texts distributed by each upstream project. If a summary here conflicts with an upstream license or model terms, follow the upstream terms for that component.
