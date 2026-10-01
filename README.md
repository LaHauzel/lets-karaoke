# lets-karaoke

[English](README.en.md)

**第一次使用？从 [图解使用手册](docs/USER_GUIDE.md) 开始：安装、模型下载、网页操作、对齐纠错与演唱会切割。**

本地运行的歌词对齐、卡拉 OK 字幕生成与演唱会视频分段工具，面向 Windows 用户。媒体处理使用本地模型和 FFmpeg；也可主动联网从 LRCLIB 查找歌词。歌词检索是可选功能，不使用时仍可粘贴、上传歌词或使用本地 ASR。

> **项目状态**：功能可以本地运行，但仍在持续迭代。自动对齐和演唱会分段都应在导出前人工复核。

## 功能

- 使用 Whisper/stable-ts 对已有歌词进行逐词、逐字时间对齐。
- 可选 LRCLIB 歌词检索：按歌名与艺人查找，先预览，再将纯文本或 LRC 导入当前文件；无需 API 密钥。
- 支持选择多个视频/音频文件或整个文件夹，按顺序批量生成；可在逐文件配置区分别设置歌词、对齐语言和字幕样式，每个输入保存为独立历史记录。
- 没有歌词文件时生成 ASR 草稿，再进行人工校对和重新对齐。
- 可选 Qwen/Wav2Vec2 声学对齐；SOFA 音素细化当前仅支持日语，其他语言使用 Whisper。
- 提供对齐诊断、低置信度提示、锚点调整、历史版本和快速重新渲染。
- 字幕可按显示行分别调整画面位置，并自定义行数、字体、字号与高亮颜色；样式可在所选视频的任意帧实时预览。
- 自动验收歌词完整性、整句闪行和字词时间结构，有限重试孤立疑难句；批量结果会汇总需处理与建议复核数量，支持可折叠波形、拖动句首句尾和循环试听。
- 输出增强 LRC、SRT、ASS，并可使用 FFmpeg 将字幕烧录到视频。
- 对长演唱会视频按音量下降和音色结构变化生成候选边界。
- 在音量轴和时间表中试听、定位、拖动边界、将最近的边界吸附到当前时间、拆分片段、合并相邻片段和修改时间。
- 按分析记录保存多个灵敏度版本，并将选中的片段批量导出为 MKV 或 MP4。
- 切割导出片段可一键导入卡拉 OK 字幕工作区继续处理。
- 媒体任务和模型推理在本机运行；两个工作台标签共用一个本地后台服务。
- 统一深色制作工作台、青绿主操作与琥珀提示，提供分组步骤、清晰空状态、响应式布局、可见键盘焦点和减少动效支持。
- 字幕生成和微调进入同一持久队列；刷新网页后可恢复进度，后台重启后自动恢复未开始任务，并可重新运行中断任务。微调支持进度与取消，暂存对齐保存在本机。
- 演唱会导出逐片登记；取消或中断后保留已完成片段，可继续导出剩余片段。

## 当前限制

- 演唱会分段使用音量与音色变化生成候选边界，YAMNet/WebRTC VAD 辅助标记疑似讲话；不识别歌名。候选点不是歌曲数量，分类分数也不是经现场数据校准的概率。
- 掌声、主持、持续伴奏、歌曲内部停顿和串烧可能造成误切或漏切，请在导出前试听并调整时间表。
- 快速导出使用原编码，受视频关键帧限制，片段开头可能包含切点之前的少量画面。需要严格切点时使用精确导出，代价是重新编码和更长的处理时间。
- 播放器能否预览源视频取决于浏览器支持的编码；无法在浏览器播放的视频仍可能被 FFmpeg 分析和导出。
- 自动验收与模型置信度不能证明对齐准确；局部重试只在可靠相邻句之间尝试，连续错位仍需要人工锚点。衡量算法改进请使用独立标注，见 [评测集格式与使用方法](docs/EVALUATION_DATASET.md)。
- 人声能量可辅助限定演唱边界，但现场噪声、弱唱和分离误差会使它失准；它不能代替歌词文本与人声的匹配依据。
- LRCLIB 的收录、歌词文本和 LRC 时间轴可能不完整或对应其他发行版本。导入不等于已对齐；现场改词、重复段落和逐字时间仍需核对。

## 系统要求

- Windows 11（当前主要验证环境）。
- Python 3.11.x 64 位（安装脚本使用此已验证版本；其他版本不在当前安装支持范围）。
- FFmpeg 和 FFprobe，并且二者都在 `PATH` 中。
- 足够的磁盘空间保存依赖、模型缓存和导出文件。模型权重不随仓库提供。

只有 Whisper、Qwen、SOFA 和完整环境需要 NVIDIA GPU、可用 CUDA 和匹配的 PyTorch/torchaudio；演唱会切割最小环境不需要 GPU。当前 GPU 安装脚本使用 CUDA 12.8 wheels。首次运行前可检查环境：

```bat
python src\check_environment.py
```

## 安装

安装与启动脚本默认使用当前命令行中的 Python 3.11，依赖直接安装到该 Python。也可选择独立虚拟环境，不依赖 ComfyUI：

```bat
setup_venv.bat whisper
webui_venv.bat
```

将 `whisper` 换成 `concert`、`qwen`、`sofa` 或 `full` 可选择相应依赖组合。虚拟环境保存在 `.venv/`；其中的模型下载命令也应使用 `.venv\Scripts\python.exe`。存在 `.venv` 时，启动、测试和安装入口都会优先使用它，包括直接运行 `setup_profile.bat` 或兼容入口 `setup.bat`，避免依赖被装到另一个 Python 中。

本机默认 Python 不是 3.11 时（例如已有 3.12 供其他项目使用），推荐使用虚拟环境，无需改动系统默认 Python：

```bat
winget install --id Python.Python.3.11 -e --scope user
winget install --id Gyan.FFmpeg -e
rem 关闭并重新打开命令行，让 winget 写入的 PATH 生效
setup_venv.bat whisper
```

`setup_venv.bat` 先检查并复用已有的 Python 3.11 `.venv`，此时不要求 PATH 中有系统 Python；需要新建时，优先使用 PATH 中的 3.11，找不到时通过 `py -3.11` 创建。已有 `.venv` 不是 3.11 时会提示先改名或删除。注意 Python 安装程序可能把 3.11 放到用户 PATH 最前面，从而改变其他项目使用的默认 `python`，安装后可用 `python --version` 确认，必要时在“编辑账户的环境变量”中调整顺序。首次安装 GPU profile 需要下载约 2.9 GB 的 CUDA 版 PyTorch，pip 在输出被重定向时可能长时间不显示进度。

Windows 用户建议运行 `setup_guide.bat`。它按以下顺序提供可选环境，并在每一步说明功能和显示安装进度：

| 选项 | 环境 | 适用功能 | GPU/模型 |
| --- | --- | --- | --- |
| 1 | Whisper 字幕环境 | 已有歌词对齐、字幕渲染、可选人声分离 | GPU；Whisper 模型 |
| 2 | 演唱会切割最小环境 | 长视频分析、讲话/音乐提示、音量轴编辑、FFmpeg 分段导出 | CPU；约 15.4 MiB YAMNet |
| 3 | Qwen 完整字幕环境 | Whisper 加 Qwen 对齐、ASR 草稿和 Wav2Vec2 | GPU；Whisper/Qwen 模型 |
| 4 | SOFA 歌声对齐环境 | Whisper 行定位和 SOFA 音素级歌声对齐 | GPU；Whisper/SOFA checkpoint |
| 5 | 完整环境 | 全部字幕后端和演唱会切割功能 | GPU；按需下载字幕模型及小型 YAMNet |

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

Whisper 模型下载到 `models/whisper`。演唱会切割 profile 会下载小型 YAMNet 音频分类模型，用于区分疑似讲话与音乐；它只在 CPU 上运行，不会自动改动切点。可以单独检查或重试下载：

```bat
python src\fetch_concert_model.py --status
python src\fetch_concert_model.py
```

使用已有歌词进行 Qwen 对齐时需要 ForcedAligner；没有歌词、需要 ASR 草稿时还需要 ASR。SOFA checkpoint 当前需要手动放到 `models/sofa/multilingual/pretrained_multilingual_singing/v1.0.0_multilingual_singing.ckpt`。模型下载渠道和模型名称以 `src/fetch_models.py` 为准。

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

- `--host`：监听地址，默认 `127.0.0.1`。服务没有登录认证；改为 `0.0.0.0` 或局域网地址时必须同时加 `--allow-remote`，否则拒绝启动，并且只应在可信网络中使用。
- `--port`：监听端口，默认 `7870`。
- `--no-open`：启动后不自动打开浏览器。
- `--no-warmup`：跳过启动时的模型预热。
- `--device`：推理设备，默认 `cuda`。

工作台包含两个独立标签：

- **卡拉 OK 字幕**：音频/视频、歌词、对齐、诊断、人工调整和字幕渲染；也可通过 `/karaoke` 访问。
- **演唱会切割**：长视频分析、音量/音色边界建议，并结合 YAMNet 与 WebRTC VAD 标记疑似讲话/MC 区间供试听复核；支持分段编辑和导出。讲话标记不会自动剪切，仍可能误报；可通过 `/#concert` 或 `/concert` 访问。

两个标签共享本地服务，但页面状态彼此独立。不要同时启动多个 WebUI 实例。

## 卡拉 OK 字幕流程

1. 上传音频或视频，并提供歌词文本或歌词文件（TXT、LRC、SRT 等）；也可使用下方的可选联网检索。
2. 选择语言、对齐后端和输出样式，启动任务并等待完成。
3. 在结果区播放视频，查看逐句时间、置信度和声学依据。
4. 调整起止时间或设置锚点，重新对齐或快速重新渲染。
5. 保存版本，并在历史记录中恢复、重新渲染或删除记录。

没有歌词时可以使用“自动转写”生成草稿。ASR 结果只应作为初稿，建议校正文案和分行后再进行已知歌词对齐。

增强 LRC 会保留明确的逐词起止，包括行尾空时间标记。选择忽略输入时间时会重新对齐，并按设置进行人声分离；Whisper、SOFA 或 ASR 前置流程新生成的时间仍会被后续出片采纳。没有内置音轨的视频可配合独立音轨使用。

人工改字和插入歌词会保存到当前版本的校验基准，后续样式调整和草稿恢复会延续该基准；原始输入保留用于追溯，模型实际漏掉的原句仍会提示。所有歌词来源均不可用时，页面会说明缺少的环境并禁用生成，历史编辑仍可使用。

### 可选：联网查找歌词

1. 在歌词区展开 **联网查找歌词**，输入歌名和可选艺人名，点击 **搜索歌词**。
2. 选择结果并点击 **预览歌词**，核对歌名、艺人、专辑、时长和歌词内容；预览请求只发送所选词库记录编号。
3. 选择导入纯文本或 LRC。导入只更新当前文件，已有歌词时先确认替换；切换文件会清除旧结果，过期响应不会导入到另一个文件。处理中暂不允许导入。
4. 对照实际演出校正内容、重复段和分行，再继续本地对齐。词库 LRC 通常只有行时间，逐字高亮仍需本地对齐。

该功能使用 [LRCLIB](https://lrclib.net/) 的只读接口，无需注册或 API 密钥。词库覆盖、服务可用性和版本匹配没有保证；无法访问时可继续粘贴或上传歌词，或使用已安装的本地 ASR。查看 [数据位置与隐私](#数据位置与隐私) 了解联网范围。

## 演唱会分段流程

1. 输入本机视频的完整路径。支持 MP4、MKV、MOV、AVI、WebM、M4V、TS 和 MTS 容器；视频必须包含可读取的画面、时长和音轨。
2. 设置最短候选片段和灵敏度（保守、均衡或灵敏），点击“分析歌曲边界”。分析按秒提取低采样率音频摘要，不加载 GPU 模型。
3. 在分段表和播放器中试听。音量轴支持点击定位、拖动蓝色边界、将最近的边界吸附到当前时间、在播放位置拆分，以及删除边界合并相邻片段；修改会立即同步时间表。
4. 点击已有记录后，修改灵敏度或最短片段，并使用“按当前灵敏度重新分析此记录”。重新分析会在同一条记录下创建新版本，原版本和已完成导出会保留。
5. 保存时间表，选择片段并导出：
   - **快速导出**：复制原编码，输出 MKV，速度快但切点受关键帧影响。
   - **精确切割**：重新编码为 H.264/AAC MP4，切点更准确但耗时更长。
6. 文件位于 `out/concert/<任务编号>/export-<批次编号>/`，每批包含 `manifest.json`；记录和版本保存在对应的 `concert.json` 中。

原视频不会被修改。取消导出只会清理尚未完成的片段，已完成文件仍保留在当前批次。

导出会在发布片段前保存恢复日志，并对成片计算完整 SHA-256；中断后可验证并继续已完成片段，校验会增加一次完整文件读取。来源、计划或文件被改动时会要求新建批次。旧版遗留且没有有效清单或恢复日志的孤立文件无法自动确认来源。

## 数据位置与隐私

- 媒体处理、模型推理和 HTTP 服务均在本机完成；项目不向远程服务上传用户媒体。
- 只有主动搜索或预览歌词时才会查询 LRCLIB：搜索发送填写的歌名和艺人名，预览发送所选记录编号，不附带媒体文件、本地媒体路径或已有歌词。结果可能从短期内存缓存返回。
- 词库请求固定使用 `https://lrclib.net`，保持 TLS 证书验证，不使用系统/环境代理、不跟随重定向，也不接受自定义请求网址。模型与依赖下载仍需要联网；“本地处理”不表示所有功能都不联网。
- 字幕结果默认保存在 `out/webui/`，演唱会记录和导出结果保存在 `out/concert/`。
- 模型缓存位于 `models/`。这些目录已加入 `.gitignore`，不应提交到公开仓库。
- 演唱会记录会保存源视频路径、分析参数、时间表和导出元数据；共享项目目录前请检查并清理 `out/`。

## 测试

测试使用仓库生成的合成音频和合成视频，不读取个人媒体：

```bat
test.bat
```

该脚本会编译 `src/` 和 `tools/`，运行离线对齐、历史版本、HTTP、队列恢复、演唱会切割和前端状态机回归。离线依赖见 `requirements-test.txt`；前端测试需要 Node.js 22，没有 Node 时会明确跳过，CI 会强制安装并运行。完整 GPU 测试需要模型，不包含在默认单元测试中。

GPU 环境下可以分别验证中文、英文和日文的带歌词流程。测试会覆盖纯文本歌词以及带行时间戳的 LRC：

仓库已包含一套合成测试素材与真值（`data/synth/`）；需要重新生成时运行 `python src\gen_synth.py --tts sapi`，它会使用本机中文/英文/日文语音并覆盖现有素材。

```bat
python tests\e2e_p1.py --device cuda
```

`e2e_p1.py` 使用 Qwen ForcedAligner 后端，需要 Qwen profile 和 ForcedAligner 模型；只安装 Whisper profile 时会报缺少 `qwen_asr`。此时可用下面的命令验证 Whisper 带歌词流程（使用 `base` 模型和 Demucs 分离）：

```bat
python tests\system_smoke_test.py --lang zh
```

端到端测试要求完整歌词映射、结构健康、产物存在，并默认限制合成 token 起点 P90 不超过 250ms；可用 `--max-start-p90-ms` 调整回归预算。详情见 [验证矩阵](docs/VALIDATION.md)。

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
  lyrics_search.py      可选 LRCLIB 歌词查询、缓存和请求边界
  webui_assets/         卡拉 OK 与演唱会标签的前端资源
tests/                   单元测试和 HTTP 回归测试
data/synth/              合成测试素材和清单
tools/SOFA/              可选的 SOFA 歌声对齐后端
docs/                    依赖许可证和发布说明
models/                  本地模型缓存（不提交）
out/                     本地历史记录和导出结果（不提交）
```

## 许可证与第三方组件

本项目自行编写的代码以 MIT 许可证开源，许可文本见仓库根目录的 [LICENSE](LICENSE)。各模块的直接依赖、SOFA 可选后端、模型权重和外部工具适用各自的许可证；请查看 [项目与第三方组件许可证说明](docs/LICENSES.md) 和 [依赖许可证审查](docs/DEPENDENCY_LICENSES.md)。打包分发时还需核对目标环境中的完整依赖及其通知文件。

歌曲、歌词、演唱会视频等媒体的版权由使用者自行负责；本项目不授予这些素材的使用或再分发权。

## 贡献与问题反馈

项目能力、技术架构、仍存不足与分阶段改进建议见 [项目全面评估](docs/PROJECT_REVIEW.md)。开源与商业同类产品对照见 [竞品分析](docs/COMPETITIVE_ANALYSIS.md)。

提交 Issue 或 Pull Request 时，请提供：

- Windows、Python、PyTorch、FFmpeg 版本；
- 使用的工作台标签和复现步骤；
- 相关日志或最小化的合成样例；
- 不要上传未经授权的歌曲、歌词、演唱会视频、模型权重或 `out/` 目录。
