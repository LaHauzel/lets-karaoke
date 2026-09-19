# 本地依赖与许可证审查

审查日期：2026-09-19。以下结论用于仓库发布规划，不替代针对具体分发方式的法律意见。

## 结论

没有发现任何依赖要求本项目必须使用 MIT。MIT 只是本项目可以选择的一种许可证。

当前真正需要在发布包中说明的是：

1. `pykakasi` 为 GPL-3.0-or-later，并被 SOFA 日文音素路径直接导入。若把这条路径和项目代码作为一个可分发程序一起发布，项目许可证和分发方式需要满足 GPL 的兼容要求。最稳妥的做法是把 SOFA/pykakasi 作为可选后端，或在确定采用宽松许可证前先替换该路径。
2. `tools/SOFA/` 内的源码带有 MIT 许可证；SOFA 的检查点和字典是另外的模型/数据资产，不能仅凭源码许可证推断其权重可以随仓库分发。
3. Qwen3 ASR 和 ForcedAligner 模型卡声明为 Apache-2.0，但模型权重、训练数据和上游模型卡的附加条款仍需随下载来源核对。权重不进入本仓库。
4. 本机 FFmpeg 是启用 GPL 的 Gyan 构建，并且包含 GPL 组件。项目只调用 PATH 中的 `ffmpeg`/`ffprobe`，不随仓库分发该二进制；若未来打包 FFmpeg，需要同时遵守该构建的 GPL、LGPL 和组件通知要求。
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

## 对本项目许可证的建议

如果保留 `pykakasi` 为默认且不可分离的运行路径，不建议直接宣称整个项目采用 MIT；应先完成 GPL 兼容性决定。若将 SOFA 日文后端、`pykakasi` 和对应权重改成可选扩展，并让默认 Whisper/Qwen 路径不导入它们，项目主体再选择 MIT 会更简单。无论选择哪种软件许可证，都不能覆盖歌曲、歌词、模型权重或 FFmpeg 二进制的独立条款。
