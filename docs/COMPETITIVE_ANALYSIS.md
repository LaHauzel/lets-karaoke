# lets-karaoke 竞品与发展方向分析

调查日期：2026-10-01。结论来自公开文档、仓库和源码审阅，未安装所有竞品，也未做同一批歌曲的跨产品准确率或耗时测试。价格按调查时公开页面记录，可能调整；宣传指标不能直接用来比较现场歌唱对齐质量。

## 1. 当前定位

本项目已经覆盖一条较完整的本地制作链路：演唱会长视频分曲及 MC/音乐边界复核 → 导出歌曲片段 → 人声处理、识别或提供歌词 → 多路径歌词对齐 → 自动验收与逐句证据检查 → 波形试听、人工锚点、插入漏句及版本重渲染 → 保留现场画面的卡拉 OK 视频和字幕文件。

最值得继续发展的方向是 **面向中文 Windows 用户的现场演唱会处理与歌唱对齐修复**。本地运行、隐私和无订阅本身已经有多个竞品实现。真正应当证明的是：在重复副歌、长间奏、合唱、串烧、歌词缺失和舞台噪声等场景中，能否减少严重漂移，以及让用户更快完成修正。

当前属于有实用能力的原型／早期工具。已有多后端、诊断和人工校正工作流，但没有证据支持“准确率领先所有竞品”或“独有完整流程”等说法。

## 2. 完整卡拉 OK 制作工具

| 工具 | 运行与费用 | 主要能力 | 对本项目的意义 |
| --- | --- | --- | --- |
| [karaoke-gen](https://github.com/nomadkaraoke/karaoke-gen) | MIT；本地 CLI，也有 Google Cloud、AudioShake、RunPod 等可选服务 | 人声分离、歌词来源和人工校对、审阅界面、4K 视频、MP4/MKV/CDG+MP3/TXT+MP3/LRC/ASS/分轨；目录及 CSV 批量 | 最直接的开源竞品。功能与交付格式更广，不能描述为必须依赖付费云服务 |
| [AI Karaoke Video Creator](https://www.powerkaraoke.com/src/prod-ai-karaoke-video-creator.php) | Windows 10/11；USD 149 永久许可，含一年更新；旧版升级 USD 59 | 全部 AI 本地运行，NVIDIA 可选；人声处理、自动同步、逐词波形微调、慢速试听、合唱、模板、批量、4K 多种视频输出 | 直接的商业竞品。成熟编辑体验与稳定安装比泛泛的“本地 AI”更值得对标 |
| [Karaoke Builder Studio 5.1](https://www.karaokebuilder.com/kbstudio.php) | Windows；USD 99 一次购买，官方称后续更新永久免费；Audio Toolkit 等另售 | 手动逐词／音节点按同步、精确调整、慢速播放、Unicode、合唱、CDG/MP3G/视频 | 适合对标人工定时效率、格式、模板及成片工具；未确认其有 AI 歌唱对齐或长演唱会分曲 |
| [KaraFun Studio](https://www.karafun.com/karaokeeditor/) | 已于 2011-11-30 停止发行、销售、更新及支持 | 历史卡拉 OK 编辑器 | 不能作为仍活跃的制作软件计入；当前 KaraFun Player／在线歌库属于另一类产品 |

karaoke-gen 的本地 `whisper-timestamped` 路径支持 CPU/CUDA/Apple Silicon，另有人工审阅流程。其 [批量命令实现](https://github.com/nomadkaraoke/karaoke-gen/blob/main/karaoke_gen/utils/bulk_cli.py) 支持目录或 CSV 工作清单。旧 `karaoke-generator` 已归档并指向新项目；`python-lyrics-transcriber` 也已归档，功能合入 karaoke-gen，不应再算作两个独立活跃竞品。

PowerKaraoke 的 [产品导览](https://www.powerkaraoke.com/src/prod-ai-karaoke-video-creator-tour.php) 展示自动同步后的逐词起止调整、波形、页式／覆盖式／滚动等显示方式和批量。试用期 10 天、价格页称批量在试用中禁用；试用有水印、人声处理限首分钟、不能保存 PK3。旧 Karaoke Video Creator 已被新 AI 产品替代。国际字符支持不等于已验证的各语言歌唱 ASR 质量。

karaoke-gen 仓库内云端计费代码有“每 10 分钟一个 credit”和超过 60 分钟的服务限制，但这不足以推出其线上美元定价，也不是本地 CLI 的统一限制。

## 3. 对齐、字幕与人工定时工具

| 工具 | 维护与技术 | 与本项目重合 | 边界 |
| --- | --- | --- | --- |
| [WhisperX](https://github.com/m-bain/whisperX) | BSD-2-Clause；活跃；Whisper ASR + wav2vec2 强制对齐；CPU/GPU/批处理 | 多语言逐词时间戳，含中文／日语对齐模型 | 对齐组件，不是完整卡拉 OK 制作界面。官方吞吐宣传需要其具体硬件和批量条件；ASS 输出在 v3 文档中仍是 TODO |
| [stable-ts](https://github.com/jianfch/stable-ts) | MIT；仓库已归档，开发无限期暂停 | align/refine、词级时间戳、`karaoke=True` ASS、SRT/VTT/ASS/TSV/TXT/JSON | 本项目使用它，维护状态是现实依赖风险，需要固定版本、兼容层与替代后端 |
| [Aegisub](https://aegisub.org/docs/latest/karaoke_timing_tutorial/) | 成熟跨平台人工字幕编辑器；原始代码有 BSD 类许可，官方包含 FFTW 的构建按 GPLv2 发布 | 单词／音节卡拉 OK 定时、ASS 效果、波形／频谱、Lua 自动化 | 核心不提供完整 AI 分离和自动生成链路；精细人工编辑仍是优秀参照 |
| [Subtitle Edit](https://github.com/SubtitleEdit/subtitleedit) | MIT；活跃；Windows/macOS/Linux，本地核心与可选云服务 | 多种识别和强制对齐后端、音频处理、批量、质量报告、字幕编辑 | 不能再笼统描述为仅普通语音识别。某些卡拉 OK 效果只是按字符／单词分摊整句时长，须与声学逐词对齐区分 |

Subtitle Edit 当前 [语音转文字文档](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/features/speech-to-text.md) 已覆盖 WhisperX、Qwen3ASRCPP、CrispASR 等路径。多后端或“质量报告”都不是本项目天然独占的卖点。

Aegisub 的 [音频编辑说明](https://aegisub.org/docs/latest/audio/) 和定时教程可用来设计快捷键、快速分词、循环试听与精确边界编辑。应当先提供可靠 ASS 往返工作流，而不是为了覆盖所有特效重做成熟字幕编辑器。

## 4. UltraStar 音高游戏制作工具

| 工具 | 许可与定位 | 能力 | 与本项目的差别 |
| --- | --- | --- | --- |
| [UltraStar Deluxe](https://github.com/UltraStar-Deluxe/USDX) / [UltraStar Creator](https://github.com/UltraStar-Deluxe/UltraStar-Creator) | GPLv2；歌唱评分游戏／Qt 手工制曲工具 | 节拍、音符、音高及歌词；Creator 跨平台定时 | UltraStar TXT 是带节拍和音高的游戏格式，与本项目的纯文本歌词 TXT 不同 |
| [UltraSinger](https://github.com/rakuri255/UltraSinger) | MIT；本地 CPU/GPU，也可 Colab | 人声、WhisperX、SwiftF0、音高量化 → UltraStar TXT/MIDI/音符 | 更适合歌唱游戏。音节划分中的插值不等同于真实逐音节声学对齐 |
| [Karedi](https://github.com/Nianna/Karedi) | GPLv3；JavaFX UltraStar 人工编辑器 | 点按音符、音调、BPM、串烧和格式校验 | 不提供完整 AI 卡拉 OK 视频链路，但有成熟游戏制曲交互 |

UltraSinger 默认语言列表包含英、法、德、西、意、日、中、荷、乌、葡。这只表明配置支持，不能作为各种歌曲类型准确率的结论。若本项目未来做 UltraStar 输出，必须增加 BPM、音高、音符和对应验收，不能只换一个 TXT 后缀。

## 5. 长视频切分工具

| 工具 | 能力 | 与本项目的关系 |
| --- | --- | --- |
| [LosslessCut](https://github.com/mifi/lossless-cut) | GPLv2；本地跨平台 FFmpeg 切分；静音／黑帧／画面切换检测，章节／轨道／关键帧、EDL/CSV 等交换格式。GitHub 版本免费，商店付费可选 | 可作为人工复核和切分交换工具。官方 README 仍注明尚不支持跨多个文件的完整批量导出。没有自动歌词制作 |
| [PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | BSD-3-Clause；画面场景检测，CLI/API、Windows/Docker，配合 FFmpeg 切片 | 画面切镜头与歌曲边界是不同信号，不能用镜头数量替代曲目分段。没有自动歌词生成 |

本项目演唱会模式的价值在音乐／讲话与现场结构判断、可解释的候选边界以及边界试听。仍需要标准曲目真值集测量遗漏、过切和 MC 误入歌曲，不能只用代码测试证明分曲质量。

## 6. 云端视频与动态字幕产品

| 产品 | 公开价格（USD） | 能力与限制 |
| --- | --- | --- |
| [Kapwing](https://www.kapwing.com/tools/make/lyric-video) | [Pro](https://www.kapwing.com/pricing) 年付折算 16/人/月、月付 24；Business 年付 50、月付 64 | 云端歌词视频、字幕、Paint 逐词动画、模板、协作和版本；MP4/SRT/VTT/TXT。免费版有水印、720p、4 分钟限制；Pro 最长 120 分钟、4K、6GB 上传，AI 有额度 |
| [VEED](https://www.veed.io/tools/auto-subtitle-generator-online) | 免费＋订阅；主 [价格页](https://www.veed.io/pricing) 动态金额未取得可靠可比值，故不写具体单价 | 云端 ASR、人工字幕时间、逐词卡拉 OK 风格、高亮和视频烧录，导出 SRT/VTT/TXT。99.9% 属宣传值，未形成现场歌曲对照实验 |
| [Descript](https://www.descript.com/captions) | [Hobbyist](https://www.descript.com/pricing) 年付 16/月、月付 24；Creator 24/35；Business 50/65，按人计；免费 0 | 云端文字式视频编辑、动态逐词字幕、Create Clips、文件批量导出、协作。官方 25 种源音频转写语言列表不含中文与日语；61 种翻译／30 种配音语言不能当作源 ASR 支持 |

Kapwing 有 [自定义卡拉 OK 视频教程](https://www.kapwing.com/resources/how-to-make-a-custom-karaoke-video-online-2/)，包含人声拆分、人工／自动歌词与逐词动画。它的 100+ 翻译语言也不是 100+ 语言的现场歌唱识别质量承诺。

这些产品主要优势是编辑、模板、协作和分享。词级动态字幕通常针对讲话或一般媒体，并不能直接推出复杂现场歌唱的强制对齐能力。

## 7. 差异与目前的短板

### 可以形成优势的部分

1. 保留舞台原画面，从长演唱会分曲一路进入歌词制作与同一套人工复核工具。
2. 对现场歌词、重复段落、漏唱和错字做明确诊断，提供锚点固定、分段重对齐、局部重试及漏句插入。
3. 面向中文 Windows 环境管理本地模型、显存和多个对齐路径，提供可审阅的证据及版本记录。
4. 将自动验收提示直接连到歌词行、波形和试听，让用户知道从哪里开始修正。

上述优势是方向和当前实现组合，尚不是经跨产品验证的质量领先结论。

### 必须正视的不足

1. **缺少真实歌唱基准。** 暂无公开统一真值集，无法量化各后端在中文、日语、重复副歌、Rap、合唱、长间奏、串烧等场景的起止误差和灾难性漂移。
2. **安装与复现成本。** 多模型、多依赖、GPU 架构、FFmpeg、不同后端和许可证均需可靠版本组合；普通用户不能被迫自行诊断每种环境失败。
3. **任务安全与恢复。** 原有批量状态主要停留在浏览器，SSE 断连和同步编辑容易造成等待、误操作及状态丢失。本轮加入持久队列、刷新恢复、异步编辑、取消及草稿恢复。浏览器已验证双文件提交后刷新仍继续生成、异步暂存新增歌词，以及 raw 任务 ID 与历史别名之间的草稿恢复；11 项前端状态机回归覆盖断连、重复完成、取消、恢复和编号一致性。编辑取消等未逐项人工点测，真实歌词质量也不由合成流程测试证明。
4. **编辑效率仍有差距。** 快捷键、快速字词拆分、细粒度时长、批量文本操作、撤销、可比较版本等都值得与 Aegisub 和商业工具做任务耗时对比。
5. **交付格式和模板有限。** CDG/MP3G、合唱分角色、主题模板、片头片尾、UltraStar 音高格式、云协作等仍是其他工具的长项；是否补齐应按目标用户选择。
6. **依赖维护风险。** stable-ts 已归档，需要兼容层、固定版本、替代实现和模型更新验证策略。
7. **验收结论的解释边界。** 防重叠、结构覆盖、声学证据和模型一致性提示不能保证歌词语义正确或歌唱时间完全准确，必须让用户可听、可改、可追溯。

## 8. 建议路线

### 第一阶段：可靠完成任务

- 在干净 Windows 环境验证安装、启动、模型下载、FFmpeg、GPU 识别与离线使用；提供固定依赖组合和可导出的诊断包。
- 验收持久队列、刷新恢复、服务重启中断、批量取消、编辑取消、草稿恢复、版本隔离和活动记录禁删。
- 对超长文件、磁盘不足、损坏媒体、上传中断、模型下载失败等形成明确可恢复反馈。

### 第二阶段：用实际歌曲证明质量

- 建立有授权、可复现的中文为主基准；覆盖日语、长间奏、重复副歌、Rap、合唱、串烧、缺行、错字及 MC 段。
- 记录逐词与逐句起止误差、缺失行数、严重漂移比例、人工修正分钟数、总耗时、峰值显存及首次安装时间。
- 用同样素材比较本项目各后端，以及 karaoke-gen／WhisperX／人工 Aegisub 基线。商业工具若参与，需要记录版本、参数与试用限制。
- 自动拒绝会明显破坏已确认锚点、字符顺序或覆盖的候选，保存判定依据，而不是依靠“看起来置信度更高”。

### 第三阶段：提高人工修正效率

- 完善波形、键盘、循环试听、字词编辑、撤销、差异查看与诊断跳转。
- 先做 ASS 与 Aegisub、分曲清单与 LosslessCut EDL/CSV 的可靠交换，再决定是否补模板、合唱或游戏音高能力。
- 将一个歌曲片段的输入、配置、模型版本、对齐证据、草稿和输出形成可移动工作包。

### 第四阶段：交付与商业化

- 把稳定安装、现场问题修复、明确模型支持、任务恢复和用户支持作为产品价值。
- 用“完成一场演唱会需要多少人工时间”验证价值，再讨论价格和授权。
- 如果目标仍是个人本地工具，可明确排除 CDG、UltraStar 或云协作，集中维护可靠的现场视频工作流。

## 9. 结论

本次调研中的主要开源直接对手是 karaoke-gen，成熟商业参照是 PowerKaraoke。Aegisub、Subtitle Edit 与 LosslessCut 更适合作为可互操作的工具和局部体验基线。项目应当围绕复杂现场歌唱的对齐恢复、中文 Windows 的可靠交付以及少量人工修正完成长演唱会，建立可测量的差异。

后续宣传或 README 比较应使用“支持哪些明确流程”和“在什么基准上取得什么结果”，避免无依据的“唯一”“最高精度”“完全自动”或把翻译语言、宣传准确率和普通语音吞吐当作歌唱质量指标。
