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
- 输出增强 LRC、SRT、ASS；视频可选择烧录硬字幕 MP4，或保留原画面并内封 ASS 字幕轨的 MKV。微调导出也可切换，默认沿用当前版本。
- 双语修复导出默认把四种软字幕组合合并为一个多轨 MKV（两路音轨、两路 ASS 字幕轨），减少重复文件；成品复核页可用不同选轨身份分别审批。
- 成品复核工作台：按项目查看成片、记录问题行与时间点、保存审批；修订历史保留旧版证据，旧审批仍绑定原片。
- 发布准备工作台：从已审批成品生成 B站/抖音投稿包（MP4、封面、标题/简介/标签/分区/版权/话题/可见范围等字段、`publish.json` 与上传信息），只生成本地文件，不登录账号、不自动上传。
- 对长演唱会视频按音量下降和音色结构变化生成候选边界。
- 在音量轴和时间表中试听、定位、拖动边界、将最近的边界吸附到当前时间、拆分片段、合并相邻片段和修改时间。
- 按分析记录保存多个灵敏度版本，并将选中的片段批量导出为 MKV 或 MP4。
- 切割导出片段可一键导入卡拉 OK 字幕工作区继续处理。
- 媒体任务和模型推理在本机运行；四个工作台标签共用一个本地后台服务。
- 统一深色制作工作台、青绿主操作与琥珀提示，提供分组步骤、清晰空状态、响应式布局、可见键盘焦点和减少动效支持。
- 字幕生成和微调进入同一持久队列；刷新网页后可恢复进度，后台重启后自动恢复未开始任务，并可重新运行中断任务。微调支持进度与取消，暂存对齐保存在本机。
- 演唱会导出逐片登记；取消或中断后保留已完成片段，可继续导出剩余片段。

## 当前限制

网页的“生成与预览 → 字幕输出方式”可选择格式，默认是烧录硬字幕 MP4；“人工微调对齐 → 本次导出”可继承当前版本或切换格式。内封 MKV 的字幕可以关闭，网页预览可能不显示 ASS 效果，需下载后使用支持 ASS 的播放器。命令行使用 `--output-mode burn` 或 `--output-mode soft`。切换格式不会重新对齐，但烧录画面需要重新编码。

- 演唱会分段使用音量与音色变化生成候选边界，YAMNet/WebRTC VAD 辅助标记疑似讲话；不识别歌名。候选点不是歌曲数量，分类分数也不是经现场数据校准的概率。
- 掌声、主持、持续伴奏、歌曲内部停顿和串烧可能造成误切或漏切，请在导出前试听并调整时间表。
- 快速导出使用原编码，受视频关键帧限制，片段开头可能包含切点之前的少量画面。需要严格切点时使用精确导出，代价是重新编码和更长的处理时间。
- 播放器能否预览源视频取决于浏览器支持的编码；无法在浏览器播放的视频仍可能被 FFmpeg 分析和导出。
- 自动验收与模型置信度不能证明对齐准确；局部重试只在可靠相邻句之间尝试，连续错位仍需要人工锚点。衡量算法改进请使用独立标注，见 [评测集格式与使用方法](docs/EVALUATION_DATASET.md)。
- 人声能量可辅助限定演唱边界，但现场噪声、弱唱和分离误差会使它失准；它不能代替歌词文本与人声的匹配依据。
- LRCLIB 的收录、歌词文本和 LRC 时间轴可能不完整或对应其他发行版本。导入不等于已对齐；现场改词、重复段落和逐字时间仍需核对。
- 发布准备只生成本地投稿包与记录，当前版本尚未接入平台账号授权和官方上传接口。平台申请条件、字段、字数与视频规格可能调整，请以官方最新说明和实际投稿页面为准。

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

安装与启动脚本优先使用项目中已有的 `.venv`；没有时使用当前命令行中的 Python 3.11，并将依赖安装到该 Python。建议先用以下命令创建独立虚拟环境，不依赖 ComfyUI：

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

工作台包含四个独立标签：

- **卡拉 OK 字幕**：音频/视频、歌词、对齐、诊断、人工调整和字幕渲染；也可通过 `/karaoke` 访问。
- **演唱会切割**：长视频分析、音量/音色边界建议，并结合 YAMNet 与 WebRTC VAD 标记疑似讲话/MC 区间供试听复核；支持分段编辑和导出。讲话标记不会自动剪切，仍可能误报；可通过 `/#concert` 或 `/concert` 访问。
- **成品复核**：查看成品与对齐置信度，保存歌曲审批、备注、问题行与时间点，比较新旧版本并打开文件所在目录；可通过 `/#review` 或 `/review` 访问。
- **发布准备**：从已审批成品生成 B站/抖音投稿包，预检字段与视频规格，记录本地投稿状态；不登录账号、不自动上传。可通过 `/#publish` 或 `/publish` 访问，详见[发布准备](#发布准备b站--抖音投稿包)。

四个标签共享本地服务，但页面状态彼此独立。不要同时启动多个 WebUI 实例。

四个页面使用同一套深色主题和只读的「切割整场 → 制作字幕 → 成品复核 → 发布准备」流程指示；切换工作区请使用顶部标签。切换标签保留当前编辑区；切割后可导入字幕制作，生成后可打开对应成品版本，复核详情可返回源演唱会的位置，审批通过后可生成投稿包。

## 卡拉 OK 字幕流程

1. 上传音频或视频，并提供歌词文本或歌词文件（TXT、LRC、SRT 等）；也可使用下方的可选联网检索。
2. 选择语言、对齐后端和输出样式，启动任务并等待完成。
3. 在结果区播放视频，查看逐句时间、置信度和声学依据。
4. 调整起止时间或设置锚点，重新对齐或快速重新渲染。
5. 保存版本，并在历史记录中恢复、重新渲染或删除记录。

已有完整逐词时间的增强 LRC 时，可选择 **逐字时间** 直接渲染，不需要安装对齐模型。每行都必须带逐词时间；普通 LRC 的行时间仍需使用“已有歌词”对齐。选择保留原唱或提供独立音轨时不需要 Demucs；去除人声则仍需要人声分离环境。

逐字时间路线直接使用原始/独立音轨，仅在需要分离伴奏时处理音频，不生成未使用的完整 PCM 副本。声学对齐默认使用“保留声学锚点”：低分、长音、间奏或低音量不会触发整行搬移、固定字速重排或删除歌词；均衡/旧策略需显式选择并试听复核。

没有歌词时可以使用“自动转写”生成草稿。ASR 结果只应作为初稿，建议校正文案和分行后再进行已知歌词对齐。

增强 LRC 会保留明确的逐词起止，包括行尾空时间标记。选择忽略输入时间时会重新对齐，并按设置进行人声分离；Whisper、SOFA 或 ASR 前置流程新生成的时间仍会被后续出片采纳。没有内置音轨的视频可配合独立音轨使用。

人工改字和插入歌词会保存到当前版本的校验基准，后续样式调整和草稿恢复会延续该基准；原始输入保留用于追溯，模型实际漏掉的原句仍会提示。未安装的模型路线会说明缺少的环境并禁用；逐字时间渲染和历史编辑仍可使用。

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

分段表支持全选、取消全选与反选，并显示选中总时长和保存状态。未选择片段时不能导出；导出栏在滚动复核时保持可见。相同素材重新分析会说明是否复用了音频缓存。

原视频不会被修改。取消导出只会清理尚未完成的片段，已完成文件仍保留在当前批次。

导出会在发布片段前保存恢复日志，并对成片计算完整 SHA-256；中断后可验证并继续已完成片段，校验会增加一次完整文件读取。来源、计划或文件被改动时会要求新建批次。旧版遗留且没有有效清单或恢复日志的孤立文件无法自动确认来源。

## 数据位置与隐私

- 媒体处理、模型推理和 HTTP 服务均在本机完成；项目不向远程服务上传用户媒体。
- 只有主动搜索或预览歌词时才会查询 LRCLIB：搜索发送填写的歌名和艺人名，预览发送所选记录编号，不附带媒体文件、本地媒体路径或已有歌词。结果可能从短期内存缓存返回。
- 词库请求固定使用 `https://lrclib.net`，保持 TLS 证书验证，不使用系统/环境代理、不跟随重定向，也不接受自定义请求网址。模型与依赖下载仍需要联网；“本地处理”不表示所有功能都不联网。
- 发布准备只在本机生成投稿包并记录「已投稿」状态，不登录平台账号、不自动上传；页面中的平台链接需要你自行点击。发布包和发布记录保存在 `out/publish/`。
- 字幕结果默认保存在 `out/webui/`，演唱会记录和导出结果保存在 `out/concert/`。
- 模型缓存位于 `models/`。这些目录已加入 `.gitignore`，不应提交到公开仓库。
- 演唱会记录会保存源视频路径、分析参数、时间表和导出元数据；共享项目目录前请检查并清理 `out/`。

## 测试

测试使用仓库生成的合成音频和合成视频，不读取个人媒体：

```bat
test.bat
```

该脚本优先使用已激活的虚拟环境，否则使用项目 `.venv`，并编译 `src/` 和 `tools/`，运行离线对齐、历史版本、HTTP、队列恢复、演唱会切割以及所有 `tests/*_test.js` 前端回归。前端测试需要 Node.js 22，缺少时会失败，避免漏测。完整 GPU 测试需要模型，不包含在默认单元测试中。

在干净的 Python 3.11 虚拟环境中，按与 CI 相同的步骤安装测试依赖（另需系统 FFmpeg/FFprobe）：

```powershell
python -m pip install torch==2.9.0 torchaudio==2.9.0 --index-url https://download.pytorch.org/whl/cpu
python -m pip install -r requirements-test.txt
python -m pip check
```

CPU wheel 使用 [PyTorch 官方安装源](https://pytorch.org/get-started/previous-versions/#v290)。测试保留真实的 Torch 张量和 Whisper 分词／音频预处理依赖，推理使用测试替身，不下载模型权重，也不读取个人媒体。

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
  review_web.py         成品库与人工复核标签的本地 HTTP 接口
  review_catalog.py     成品、修订版本和置信度清单
  review_native.py      自动纳入普通字幕流程生成的成品与修订版本
  review_store.py       人工审批、备注、问题时间点的原子保存与冲突保护
  revision_policy.py    裁剪时间轴校验、缓存指纹与边界风险提示
  repair_*.py           可续跑的对齐修复、逐行报告、受控交付与一键批处理
  multitrack.py         多轨 MKV 封装、数据包与时间轴一致性校验
  archive_library.py    成品库无损存储迁移（生成计划与清理候选，不删除原片）
  feedback_revision.py  版本绑定的人工反馈修订与本地 CLI
  publish_export.py     从已审批成品生成 B站/抖音投稿包（FFmpeg、封面、publish.json、哈希）
  publish_web.py        发布准备标签的本地 HTTP 接口与生成任务
  webui_assets/         卡拉 OK、演唱会、人工复核与发布准备标签的前端资源
tests/                   单元测试和 HTTP 回归测试
data/synth/              合成测试素材和清单
tools/SOFA/              可选的 SOFA 歌声对齐后端
docs/                    依赖许可证和发布说明
models/                  本地模型缓存（不提交）
out/                     本地任务历史、发布包和导出结果（不提交）
```

## 成品复核

启动工作台后选择「成品复核」，或打开 `http://127.0.0.1:7870/#review`。支持按演唱会、置信度、视频类型和人工状态筛选；可预览 MP4、定位 Windows 文件、记录问题行／时间点和备注，保存为本地 JSON。审批通过的曲目可在弹窗中直接点「生成投稿包 →」进入发布准备。

点击「修改原文字幕」可继续编辑项目版本。批量成品有对应的 `exports/workflow-links.json` 时，会根据所选版本的对齐数据建立独立修改工作区；预览复用同时间轴的原文带人声版，保留原始交付和审批。新生成的版本会进入成品库，需单独复核。该入口编辑原文及时间轴，双语译文仍通过各批次的翻译记录维护。

普通字幕流程生成的 `out/webui/` 成品会自动进入此标签；缺少独立模型评分时显示「未评分」，不会臆测置信度。额外的批量成品清单读取 `out/concert-collection/exports/track-manifest.json`，修订清单读取 `exports/revisions/manifest.json`。没有清单时显示空库。审批单独保存在 `out/concert-collection/human-reviews.json`，不随导出覆盖；每次保存保留 `.bak`，并通过跨进程锁和版本号防止多页面覆盖。审批绑定具体文件及其签名；替换片源或修订字幕后，新版本需要再次复核，旧版通过记录仍保留。所有数据只在本机处理，不自动上传或投稿。

## 发布准备（B站 / 抖音投稿包）

启动工作台后选择「发布准备」，或打开 `http://127.0.0.1:7870/#publish`。这里只处理「成品复核」中**已标记通过、且文件签名未变化**的曲目。

- 每个平台生成一个本地投稿包：`video.mp4`、`cover.jpg`、`publish.json`（完整字段、来源、审批与哈希）、`upload-info.txt`（可复制到投稿页的标题/简介/标签/话题）、`sha256.txt`。
- 打包选项：原唱/伴奏（源文件有多音轨时）、烧录原文字幕或关闭、重新编码或直接复制（仅 MP4 源）、封面时间、抖音竖屏模糊背景。
- B站字段：标题（≤80 字）、简介（≤250 字）、标签、分区 tid、原创/转载、转载来源、禁止转载、定时发布；封面按官方建议生成 1146×717。
- 抖音字段：标题、文案、话题（最多 5 个）、可见范围、允许下载、位置、合集、@好友、定时发布、原创声明；封面生成 1080×1440，可选用竖屏模糊背景适配横屏素材。
- 生成前会预检：审批签名、字幕/音轨可用性、字段长度和平台视频规格（B站：单文件 ≤4GB、时长 <5 小时、MP4、≤4096×4096、≤120fps、音频 ≤320kbps/2 声道/48kHz；抖音尺寸与时长以页面提示为准）。
- 生成后可在页面预览视频、下载 `publish.json`、复制投稿信息，并「标记为已投稿」（记录链接/稿件号/备注，仅保存在本机）。发布记录写入 `out/publish/`，不覆盖原成品。生成任务在内存中运行，服务重启后需要重新生成；已完成的投稿包会保留。
- **本工具不登录任何平台账号、不自动上传**。当前版本未实现平台账号授权或官方上传接口；生成投稿包后，由用户在平台投稿页面上传。后续若接入官方接口，需要另行核实申请条件和权限，不能把本地预检通过视为平台审核通过。
- 生成的 MP4 为单音轨；多轨 MKV 仍是本地复核交付格式。媒体、审批与 `out/` 都是私有运行数据，不应提交到 Git。

## 批量自动修复既有时间轴

`src/repair_alignment.py` 可复用于演唱会批量任务。输入每首歌的 `align.json`、`alignment_evidence.json`、`input_lyrics.json` 和 `in/audio_44k.wav`；已有 `in/vg/vocals.wav` 时会复用分离人声。输出必须使用独立目录：

在已激活项目环境的仓库根目录执行，一条命令完成修复、短片对照、合格版本导出和成品库登记：

```powershell
python src/repair_batch.py --jobs-root out/my-concert/jobs --repair-root out/my-concert/repair-r1 --catalog-root out/concert-collection/exports --project my-concert --translation-cache out/my-concert/translation-cache.json
```

批处理默认使用多轨软字幕布局；需要旧的四文件格式时追加 `--soft-layout separate`，直接运行 `repair_delivery.py` 也接受同名参数。

也可以按阶段执行，先只查看候选和报告：

```powershell
python src/repair_alignment.py --jobs-root out/my-concert/jobs --output-root out/my-concert/repair --models large-v3 medium
python src/repair_report.py out/my-concert/repair out/concert-collection/exports/repair-runs/my-concert --catalog-root out/concert-collection/exports --project my-concert
```

报告链接默认使用当前网页的主机和端口，WebUI 改用其他端口也能返回对应工作台。直接通过 `file://` 打开离线报告时，可在上述批处理或报告命令追加 `--workspace-url http://localhost:8000/`，指定已启动的工作台；部署在路径前缀下也可使用 `--workspace-url /studio/`。

通过时间结构门槛的候选可继续导出；未通过的仍可制作短片复核。以下命令复用已有伴奏和译文，不重新分离或调用翻译服务：

```powershell
python src/repair_delivery.py out/my-concert/repair out/concert-collection/exports/revisions/my-repair --translation-cache out/my-concert/translation-cache.json --baseline-exports out/concert-collection/exports/my-concert --catalog-root out/concert-collection/exports --project my-concert --revision my-repair
python src/repair_previews.py out/my-concert/repair out/concert-collection/exports/repair-runs/my-concert
# 生成短片后再运行 repair_report.py，即可在报告内查看修复前后对照。
```

默认导出两种字幕布局 × 两种音轨：软字幕四种组合共用一个多轨 MKV（两条音轨、两条 ASS 字幕轨），硬字幕 MP4 仍分别保存。可用 `--formats soft` 或 `--formats hard` 限定格式；直接运行 `repair_delivery.py` 时可用 `--soft-layout separate` 恢复四个独立 MKV。没有提供翻译缓存时仅输出原文两种音轨，日语译文缺失会停止该曲导出，英语保持原文。相同字幕与音频按指纹续跑，伴奏视频复制已编码的画面；新版本注册到成品库后仍需人工复核。已有成品的无损合并、网页选轨及审批保护规则见 [多轨 MKV 与存储整理](docs/multitrack-storage.md)。

流程先检查所有行的短句、长句、句内空档、字词映射和模型分歧，再做不带歌词提示的转写。转写仅使用参考歌词包含的语言；混合语言窗口会检查两种语言，避免依赖错误的旧时间轴。默认用 medium 连续转写、large-v3 与 medium 比较局部对齐，复用已有的大模型转写和可用声源；`--models` 从较大到较小排列，最后一个用于连续转写。候选必须保留文本、满足时间顺序并有转写锚点或额外交叉依据，才会替换独立候选中的对应行。已有成品和人工审批不会被改写。

每次声学计算按音频身份、模型、语言、歌词、时间范围和参数缓存，重新执行可续跑；不会重复人声分离。输出包含候选 ASS/SRT、完整前后对照和未解决问题；报告中的 `review-queue.json` 按现场歌词结构、时间轴阻断、长音与空档列出复核事项，附实际歌词和歌曲内时间。每首最多制作两组修改前后片段，优先覆盖已修问题和未解决的高风险段落。`render_gate` 同时检查原始时间轴与实际显示片段：长空档若已拆开并显示圆点，仅保留试听提示；闪句、重叠、超长拖字和歌词结构疑点仍阻断整曲多版本导出。通过门槛不是人工通过，也不是投稿许可。转写未覆盖的歌词、现场重复次数和新增口号会留给人工复核，不自动删改参考歌词。

### 根据人工批注制作修订版

`feedback_revision.py` 提供版本校验和时间轴转换函数，并带有可直接运行的本地 CLI；完整字段说明、输出格式和安全规则见 [根据人工批注制作修订版](docs/feedback-revision.md)。先用 `verify_reviewed_file()` 验证批注对应文件的大小与修改时间，再按该版本的相对时间裁切；不能把新版的时间直接套到最初的片段。`apply_feedback()` 保留原时间轴，返回独立结果和旧行号到新行号的映射；如果裁切穿过仍保留的歌词、时间逆序或片源不足，会拒绝转换。

```powershell
python src/feedback_revision.py --align out/my-concert/repair/01_Song/align.json --decisions out/my-concert/feedback-r1.json --output out/my-concert/revisions-r1/01_Song
```

现场未唱的歌词需要明确的删句记录（原行号、完全匹配的歌词和人工依据）。现场多唱的重复段可用 `live_reference()` 建立参考顺序，每次重复都要记录声学依据；未被转写识别到本身不构成删除依据。网络上的标准时长或同场专辑时长通过 `duration_reference()` 作为提示记录，不能直接决定裁切点：前奏、独奏、互动、尾奏及发行版剪辑都可能不同。

如果某些参考行无法可靠定位，可通过 `quarantine` 暂停显示其不可信的字幕，同时在 `feedback.unresolved_rows` 中保留完整原行及原因。这与确认未唱不同；调用方必须将这种成品标为仍需修复，不得因显示时间轴通过检查而宣布歌词完整或自动通过审批。

注册裁切或补尾版本时，`repair_delivery.register()` 可显式传入 `media`、`source_start`、`source_path`，使字幕编辑器继续打开匹配的源片段。成品库保留历史修订文件和各自的歌词证据；原审批和问题行仍绑定旧版，新成品需要重新复核。

可用 `--only 1 8 12` 限定编号，或 `--anchors-root <目录>` 提供逐曲 JSON 锚点（文件名为 `<曲目目录名>.json`，记录含一基 `row`、对应 `text`、歌曲内秒数 `start/end`）。人工锚点必须与当前版本一致；冲突会停止，不能用它掩盖缺词。实际对齐精度仍需对照声音验收。

批处理按歌曲启动独立进程，曲目之间完整释放 CUDA 状态。默认连续 180 秒没有新计算进度时终止该工作进程并利用缓存重试一次；仍失败会记录并继续其他歌曲。CPU 慢机器可用 `--idle-timeout` 增大时限。超时和最终失败均保存在本地记录中，不会被算作修复成功。

若本次仍有歌曲对齐失败，已完成候选和旧成片均保留，但批处理会返回非零退出码并停止下游发布，防止把旧候选当作本次成功结果。修复失败原因后可续跑；`--only` 同时限制声学处理、预览与视频导出范围。

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
