# 验证矩阵

本文记录可重复的通用验证范围，不包含真实歌曲、歌词、视频或个人机器路径。

## 2026-10-01 验证记录

| 范围 | 本轮结果 | 证明范围 |
| --- | --- | --- |
| `test.bat` | 146项Python回归全部通过；独立环境相同146项通过；另运行ASR离线自检 | 逻辑、真实HTTP/FFmpeg/libass、历史/队列恢复、进程树回收；本机没有跳过 |
| Node前端 | 11/11状态机回归；Python测试入口会调用 | SSE终态、轮询/恢复、取消、任务/历史别名；不是全部页面自动点击测试 |
| Windows CI | [GitHub Actions通过](https://github.com/LaHauzel/lets-karaoke/actions/runs/36805697316)：146项中145项通过、1项可选YAMNet测试跳过；ASR自检通过 | 独立Windows/CPU/Python3.11/Node22环境；不证明完整GPU安装与真实歌声质量 |
| GPU合成 | 8/8通过质量门槛，最差起点P90约211.6ms | Qwen管线，三种语言、TXT/LRC、两种混音分离 |
| 补充流程 | 中文ASR→Whisper、日语Whisper→SOFA完成，SOFA细化3行；网页英文Whisper两项完成 | 各路线能处理并出片，不代表真实歌声准确率 |
| 演唱会模块 | 30项回归在系统Python和独立环境通过 | 本机真实YAMNet全段/分批一致、真实FFmpeg取消/恢复、迁移并发、缓存和版本；新增403/415同连接回归 |
| 浏览器 | 两文件提交后刷新续跑、异步插入、历史别名草稿恢复、未提交文字/上次记录恢复、生成v1和回退v0 | 关键路径人工自动化操作；编辑取消等另由回归覆盖 |

### 第二台机器：按 README 从零部署

| 范围 | 结果 | 说明 |
| --- | --- | --- |
| 部署 | 默认 `python` 为 3.12 时，`setup_venv.bat whisper` 经 `py -3.11` 创建 3.11.9 `.venv`；whisper 与 concert profile 检查全部 PASS | Windows 11、RTX 3060 Laptop 6 GiB、驱动 555.99、FFmpeg 9.0.2（winget）；CUDA 12.8 wheel 在该驱动上 `cuda` 运算正常 |
| `test.bat` | 150项Python回归全部通过（含新增远程监听、错误信息与能力探测回归），Node 状态机回归通过 | 在 `.venv` 中运行，无跳过；ASR 离线自检通过 |
| GPU | `system_smoke_test.py --lang zh`（Whisper base + Demucs）完成；`e2e_p1.py` 在未装 Qwen 时按预期报缺少 `qwen_asr` | 未安装 Qwen/SOFA，相关路线未在此机验证 |
| 浏览器 | 未安装的 SOFA/ASR/Qwen/wav2vec2 选项置灰并说明原因；空提交在输入区就地提示；演唱会页读取旧版记录正常 | 人工自动化检查 |

本机为Windows11、Python3.11.8、PyTorch/torchaudio2.9.0+cu128、RTX5090 D v2约24GiB。GitHub Actions[首轮运行](https://github.com/LaHauzel/lets-karaoke/actions/runs/36803959928)暴露了新CPU集成测试使用`libx264`时误选NVENC的问题；规范编码器别名并加入未知名称回归后，[修复提交df57a45的远端运行](https://github.com/LaHauzel/lets-karaoke/actions/runs/36805697316)已成功。CI不下载大型模型或个人媒体，146项中仅本机可选YAMNet整段/分批模型测试因缺少权重/运行库跳过；此项已在本机两套环境通过。

离线测试依赖：`python -m pip install -r requirements-test.txt`；需要FFmpeg/FFprobe，前端回归需要Node.js22。没有Node时本地会明确跳过，CI会安装Node执行。CPU回归不要求PyTorch/CUDA。

## 带歌词流程

命令：

```bat
python tests\e2e_p1.py --device cuda
```

覆盖 8 个用例：

音频不随Git提交。新克隆先准备本机中文/英文/日文SAPI语音，运行 `python src\gen_synth.py --tts sapi` 生成合成集与匹配的manifest；生成会更新现有合成集，应保存需要比较的旧版真值。更换语音或重新生成后要记录来源，不能直接混比旧结果。默认生成完全离线，可选联网edge后端需显式选择。

| 语言 | 纯文本 | LRC | 混音 | 人声分离 |
| --- | --- | --- | --- | --- |
| 中文 | ✓ | ✓ | ✓ | ✓ |
| 英文 | ✓ | ✓ | — | — |
| 日文 | ✓ | ✓ | ✓ | ✓ |

每个用例都会检查：

- 音频或视频输入是否能完整处理
- 歌词行和 token 是否全部映射
- 字幕结构是否存在重叠、逆序或零时长事件
- ASS、SRT 和视频是否生成
- 起点误差的平均值、中位数、P90 和命中率

现在成功退出还要求输出行数与输入一致、所有truth token完整匹配、`health.ok`、ASS/SRT/对齐JSON/视频非空，并默认要求合成token起点P90≤250ms。`--max-start-p90-ms`可调整这个回归预算；`--only`没有匹配用例会拒绝运行，不会报告0/0成功。

当前合成验证结果为 8/8 成功。合成语音用于验证管线和边界计算，不能替代真实歌声质量结论。

## 无歌词 ASR 流程

命令：

```bat
python tests\system_smoke_test.py --lang zh --asr
python tests\system_smoke_test.py --lang en --asr
python tests\system_smoke_test.py --lang ja --asr
```

每种语言都会检查：

1. 本地 ASR 是否生成 LRC、纯文本和 SRT 草稿；
2. 草稿是否自动进入 Whisper 二次对齐；
3. 是否生成诊断信息、结构健康信息和最终视频；
4. ASR 语言、单元数量、覆盖率和零宽单元比例是否返回。

ASR 结果是可编辑初稿。最终发布前应先校正文案和分行，再按“已有歌词”路线重新对齐。

## SOFA 路线

```bat
python tests\system_smoke_test.py --lang ja --sofa
```

该路线先用 Whisper 确定行窗口，再用 SOFA 做音素级细化。当前测试确认：

- SOFA 推理可以在 Windows 系统 Python 环境中完成；
- torchaudio 无法调用 TorchCodec 时会回退 librosa；
- 诊断文件和最终视频能够正常生成；
- SOFA 是可选细化路径，默认仍建议先使用 Whisper 并查看诊断。
- 当前网页G2P仅支持日语；显式中文/英文拒绝，auto必须探测为日语。TextGrid相对时钟按实际裁音起点恢复，包含前导padding被截短的回归。

## 在线时间轴歌词

在线 LRC 只用于临时验证解析器对不同语言、行时间和逐词时间标签的兼容性；歌词和音频不会写入仓库，也不作为算法准确率的真值。

## 开放许可外部音频

在仓库外使用 Wikimedia Commons 上许可明确的中文、英文和日文音频做了补充验证。每种语言都分别运行了：

- 已知歌词：Whisper 双路对齐、人声引导、字幕渲染；
- 无歌词：本地 ASR 草稿、Whisper 二次对齐、字幕渲染。

流程完成和生成视频不代表质量验收通过。真实音频还必须检查输入／输出行数、被删除或压缩的歌词、低置信行和零宽单元；尤其不能因低置信提示较少就忽略严重丢行。SOFA 可作为可选的音素细化路径，其收益需要独立对照验证。

外部素材仅用于临时验证，不进入仓库、测试夹具或发布包。

## 解释指标

- `start_mae_ms`：预测起点与合成真值起点的平均绝对误差。
- `start_p90_ms`：90% token 起点误差不超过的范围。
- `start_hit50`：起点误差不超过 50ms 的 token 百分比。
- `health.ok`：字幕结构通过单调性、重叠和零时长检查，不代表歌唱语义完全正确。
