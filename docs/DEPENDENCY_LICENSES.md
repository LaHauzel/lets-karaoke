# 本地依赖与许可证审查

审查日期：2026-09-24。以下结论用于仓库发布规划，不替代针对具体分发方式的法律意见。

## 结论

项目自有代码已选择 MIT，许可文本见仓库根目录的 `LICENSE`。没有发现任何依赖要求整个项目必须使用 MIT；同时，MIT 不会改变依赖、模型权重、外部程序或用户媒体各自的许可证。

当前真正需要在发布包中说明的是：

1. `pykakasi` 为 GPL-3.0-or-later，并被可选的 SOFA 日文音素路径直接导入。若分发包含该路径和 pykakasi 的组合程序，需要审查并满足 GPL 对该分发方式的要求；项目自有代码的 MIT 许可不会重新许可 pykakasi。
2. `tools/SOFA/` 内的源码带有 MIT 许可证；SOFA 的检查点和字典是另外的模型/数据资产，不能仅凭源码许可证推断其权重可以随仓库分发。
3. Qwen3 ASR 和 ForcedAligner 模型卡声明为 Apache-2.0，但模型权重、训练数据和上游模型卡的附加条款仍需随下载来源核对。权重不进入本仓库。
4. FFmpeg/FFprobe 是外部程序。具体构建可能启用 GPL 组件；项目未在仓库中分发 FFmpeg 二进制。若未来打包，需按所选构建核对 GPL、LGPL 和组件通知要求。
5. 用户提供的音频、视频和歌词不属于软件依赖许可证范围，代码许可证不授予这些素材的再分发权。

## 直接依赖概览

| 组件 | 当前版本/来源 | 许可证线索 | 发布处理 |
|---|---|---|---|
| PyTorch / torchaudio / torchvision | 2.9.0+cu128 / 当前系统安装 | BSD-3-Clause 及各自上游附带许可 | 不打包 wheel；安装时遵循 PyTorch 官方渠道 |
| NumPy / SciPy 生态 | requirements 与系统安装 | BSD、ISC、MIT、LGPL/GPL exception 等组合 | 保留上游版权与通知；不复制二进制到仓库 |
| soundfile | 0.13.1 | BSD-3-Clause | 允许宽松分发 |
| librosa | 0.11.0 | ISC | 允许宽松分发 |
| Demucs | 4.0.1 | MIT（代码） | 模型权重单独核对，不入库 |
| openai-whisper | 20250625 | MIT（代码） | 模型文件单独核对，不入库 |
| stable-ts | 2.19.1 | MIT | 允许宽松分发 |
| qwen-asr / transformers / huggingface-hub | 0.0.6 / 4.57.6 / 0.34.4 | Apache-2.0 | 保留 Apache 通知；模型条款另行核对 |
| pykakasi | 2.3.0 | GPL-3.0-or-later | 需要 GPL 兼容性审查；建议改为可选依赖 |
| Lightning | 2.6.6 | Apache-2.0 | 保留 Apache 通知 |
| einops / PyYAML | 0.8.1 / 6.0.2 | MIT | 允许宽松分发 |
| matplotlib / pandas | 3.10.5 / 2.3.1 | Matplotlib License / BSD-3-Clause | 保留各自版权和许可文本 |
| textgrid / tensorboardX / h5py / rapidfuzz | 当前系统安装 | 包元数据不完整或需查上游 | 发布前按实际 wheel 与上游仓库补齐通知 |

`requirements.txt` 记录的是运行依赖，不等于第三方许可证清单。正式发布时应生成一份锁定环境的 SBOM/许可证报告，并将所有依赖的 NOTICE 文件放进发布包或发布页面。

项目模块与第三方组件的用户说明见 [`docs/LICENSES.md`](LICENSES.md)。本表根据当前 requirements 和已记录的环境概览整理，不是完整的锁定环境 SBOM；包版本、平台和安装配置会影响传递依赖。正式打包前，应针对实际构建再次扫描并核对各上游许可证及 NOTICE。
