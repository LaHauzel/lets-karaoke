# lets-karaoke

[English](README.en.md)

**第一次使用？从 [图解使用手册](docs/USER_GUIDE.md) 开始：安装、模型下载、网页操作、对齐纠错与演唱会切割。**

本地运行的歌词对齐、卡拉 OK 字幕生成与演唱会视频分段工具，面向 Windows 用户。项目使用本地模型和 FFmpeg 处理媒体，不依赖在线 API。

> **项目状态**：功能可以本地运行，但仍在持续迭代。自动对齐和演唱会分段都应在导出前人工复核。

## 功能

- 使用 Whisper/stable-ts 对已有歌词进行逐词、逐字时间对齐。
- 没有歌词文件时生成 ASR 草稿，再进行人工校对和重新对齐。
- 提供对齐诊断、低置信度提示、锚点调整、历史版本和快速重新渲染。
- 输出增强 LRC、SRT、ASS，并可使用 FFmpeg 将字幕烧录到视频。
- 对长演唱会视频按音量下降和音色结构变化生成候选边界。
- 在音量轴和时间表中试听、定位、拖动边界、拆分片段、合并相邻片段和修改时间。
- 按分析记录保存多个灵敏度版本，并将选中的片段批量导出为 MKV 或 MP4。
- 所有任务在本机运行；两个工作台标签共用一个本地后台服务。

## 当前限制

- 演唱会分段使用声学启发式分析，不进行歌名识别、歌词转写或语音/掌声分类。候选点不是歌曲数量，也不是识别概率。
- 掌声、主持、持续伴奏、歌曲内部停顿和串烧可能造成误切或漏切，请在导出前试听并调整时间表。
- 快速导出使用原编码，受视频关键帧限制，片段开头可能包含切点之前的少量画面。需要严格切点时使用精确导出，代价是重新编码和更长的处理时间。
- 播放器能否预览源视频取决于浏览器支持的编码；无法在浏览器播放的视频仍可能被 FFmpeg 分析和导出。

## 系统要求

- Windows 11（当前主要验证环境）。
- Python 3.11 或更高版本。
- FFmpeg 和 FFprobe，并且二者都在 `PATH` 中。
- 足够的磁盘空间保存依赖、模型缓存和导出文件。模型权重不随仓库提供。

只有 Whisper、Qwen、SOFA 和完整环境需要 NVIDIA GPU、可用 CUDA 和匹配的 PyTorch/torchaudio；演唱会切割最小环境不需要 GPU。当前 GPU 安装脚本使用 CUDA 12.8 wheels。首次运行前可检查环境：

```bat
python src\check_environment.py
```

## 安装

建议使用系统默认 Python 3.11。安装与启动脚本使用当前命令行中的 `python`，依赖直接安装到该 Python，不创建虚拟环境，不依赖 ComfyUI。

Windows 用户建议运行 `setup_guide.bat`。它按以下顺序提供可选环境，并在每一步说明功能和显示安装进度：

| 选项 | 环境 | 适用功能 | GPU/模型 |
| --- | --- | --- | --- |
| 1 | Whisper 字幕环境 | 已有歌词对齐、字幕渲染、可选人声分离 | GPU；Whisper 模型 |
| 2 | 演唱会切割最小环境 | 长视频分析、音量轴编辑、FFmpeg 分段导出 | 无 GPU、无模型 |
| 3 | Qwen 完整字幕环境 | Whisper 加 Qwen 对齐、ASR 草稿和 Wav2Vec2 | GPU；Whisper/Qwen 模型 |
| 4 | SOFA 歌声对齐环境 | Whisper 行定位和 SOFA 音素级歌声对齐 | GPU；Whisper/SOFA checkpoint |
| 5 | 完整环境 | 全部字幕后端和演唱会切割功能 | GPU；按需下载全部模型 |

完整排查步骤见 [Windows 安装与故障排查指南](docs/SETUP_WINDOWS.md)。只做演唱会切割时选择第 2 项即可，不需要部署完整环境。

```bat
git clone https://github.com/LaHauzel/lets-karaoke.git
cd lets-karaoke
setup_guide.bat
```

`setup.bat` 是兼容入口，会安装完整环境。按功能安装时也可以直接运行 `call setup_profile.bat whisper|concert|qwen|sofa|full`。`requirements.txt` 是完整环境的聚合入口；轻量部署应使用 profile 脚本。请先安装 FFmpeg，并确认 `ffmpeg -version` 和 `ffprobe -version` 均可执行。

模型可以按需下载到项目的 `models/` 目录：

```bat
python src\fetch_whisper.py --model large-v3
python src\fetch_models.py --list
python src\fetch_models.py
```

Whisper 模型下载到 `models/whisper`。使用已有歌词进行 Qwen 对齐时需要 ForcedAligner；没有歌词、需要 ASR 草稿时还需要 ASR。SOFA checkpoint 当前需要手动放到 `models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt`。模型下载渠道和模型名称以 `src/fetch_models.py` 为准。

## 启动 WebUI

```bat
webui.bat
```

默认地址为 <http://127.0.0.1:7870/>。服务只监听本机回环地址，网页、字幕任务和演唱会分段任务由同一个后台进程提供。

也可以直接启动：

```bat
python src\webui.py --no-open
python src\webui.py --port 8000 --no-open
```

常用参数：

- `--host`：监听地址，默认 `127.0.0.1`。
- `--port`：监听端口，默认 `7870`。
- `--no-open`：启动后不自动打开浏览器。
- `--no-warmup`：跳过启动时的模型预热。
- `--device`：推理设备，默认 `cuda`。

工作台包含两个独立标签：

- **卡拉 OK 字幕**：音频/视频、歌词、对齐、诊断、人工调整和字幕渲染；也可通过 `/karaoke` 访问。
- **演唱会切割**：长视频分析、候选边界复核、分段编辑和导出；也可通过 `/#concert` 或 `/concert` 访问。

两个标签共享本地服务，但页面状态彼此独立。不要同时启动多个 WebUI 实例。

## 卡拉 OK 字幕流程

1. 上传音频或视频，并提供歌词文本或歌词文件（TXT、LRC、SRT 等）。
2. 选择语言、对齐后端和输出样式，启动任务并等待完成。
3. 在结果区播放视频，查看逐句时间、置信度和声学依据。
4. 调整起止时间或设置锚点，重新对齐或快速重新渲染。
5. 保存版本，并在历史记录中恢复、重新渲染或删除记录。

没有歌词时可以使用“自动转写”生成草稿。ASR 结果只应作为初稿，建议校正文案和分行后再进行已知歌词对齐。

## 演唱会分段流程

1. 输入本机视频的完整路径。支持 MP4、MKV、MOV、AVI、WebM、M4V、TS 和 MTS 容器；视频必须包含可读取的画面、时长和音轨。
2. 设置最短候选片段和灵敏度（保守、均衡或灵敏），点击“分析歌曲边界”。分析按秒提取低采样率音频摘要，不加载 GPU 模型。
3. 在分段表和播放器中试听。音量轴支持点击定位、拖动蓝色边界、在播放位置拆分，以及删除边界合并相邻片段；修改会立即同步时间表。
4. 点击已有记录后，修改灵敏度或最短片段，并使用“按当前灵敏度重新分析此记录”。重新分析会在同一条记录下创建新版本，原版本和已完成导出会保留。
5. 保存时间表，选择片段并导出：
   - **快速导出**：复制原编码，输出 MKV，速度快但切点受关键帧影响。
   - **精确切割**：重新编码为 H.264/AAC MP4，切点更准确但耗时更长。
6. 文件位于 `out/concert/<任务编号>/export-<批次编号>/`，每批包含 `manifest.json`；记录和版本保存在对应的 `concert.json` 中。

原视频不会被修改。取消导出只会清理尚未完成的片段，已完成文件仍保留在当前批次。

## 数据位置与隐私

- 媒体处理、模型推理和 HTTP 服务均在本机完成；项目不向远程服务上传用户媒体。
- 字幕结果默认保存在 `out/webui/`，演唱会记录和导出结果保存在 `out/concert/`。
- 模型缓存位于 `models/`。这些目录已加入 `.gitignore`，不应提交到公开仓库。
- 演唱会记录会保存源视频路径、分析参数、时间表和导出元数据；共享项目目录前请检查并清理 `out/`。

## 测试

测试使用仓库生成的合成音频和合成视频，不读取个人媒体：

```bat
test.bat
```

该脚本会编译 `src/` 和 `tools/`，然后运行对齐策略、历史版本、HTTP 接口和演唱会切割等回归测试。完整的 GPU 端到端测试需要已安装依赖并准备好模型，不包含在默认单元测试脚本中。

GPU 环境下可以分别验证中文、英文和日文的带歌词流程。测试会覆盖纯文本歌词以及带行时间戳的 LRC：

```bat
python tests\e2e_p1.py --device cuda
```

无歌词流程会先用本地 ASR 生成草稿，再自动交给 Whisper 做二次对齐。可以按语言单独运行：

```bat
python tests\system_smoke_test.py --lang zh --asr
python tests\system_smoke_test.py --lang en --asr
python tests\system_smoke_test.py --lang ja --asr
```

ASR 草稿应先人工校对，再作为已知歌词重新对齐；草稿文本和自动分行不应直接视为最终结果。

## 项目结构

```text
src/
  webui.py              本地 HTTP 服务和字幕工作台路由
  concert_splitter.py   音频特征提取、边界建议和 FFmpeg 导出
  concert_web.py        演唱会任务、版本和历史记录接口
  webui_assets/         卡拉 OK 与演唱会标签的前端资源
tests/                   单元测试和 HTTP 回归测试
data/synth/              合成测试素材和清单
tools/SOFA/              可选的 SOFA 歌声对齐后端
docs/                    依赖许可证和发布说明
models/                  本地模型缓存（不提交）
out/                     本地历史记录和导出结果（不提交）
```

## 许可证与第三方组件

当前仓库尚未声明统一的软件许可证，请不要默认将代码视为 MIT、Apache-2.0 或其他开源许可证。第三方依赖、SOFA 源码、模型权重、FFmpeg 构建和用户媒体分别受其各自条款约束。发布或再分发前，请阅读 [依赖许可证审查](docs/DEPENDENCY_LICENSES.md)，并为实际部署环境生成完整的依赖清单。

歌曲、歌词、演唱会视频等媒体的版权由使用者自行负责；本项目不授予这些素材的使用或再分发权。

## 贡献与问题反馈

提交 Issue 或 Pull Request 时，请提供：

- Windows、Python、PyTorch、FFmpeg 版本；
- 使用的工作台标签和复现步骤；
- 相关日志或最小化的合成样例；
- 不要上传未经授权的歌曲、歌词、演唱会视频、模型权重或 `out/` 目录。
