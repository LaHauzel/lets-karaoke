# 验证矩阵

本文记录可重复的通用验证范围，不包含真实歌曲、歌词、视频或个人机器路径。

## 带歌词流程

命令：

```bat
python tests\e2e_p1.py --device cuda
```

覆盖 8 个用例：

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

## 在线时间轴歌词

在线 LRC 只用于临时验证解析器对不同语言、行时间和逐词时间标签的兼容性；歌词和音频不会写入仓库，也不作为算法准确率的真值。

## 解释指标

- `start_mae_ms`：预测起点与合成真值起点的平均绝对误差。
- `start_p90_ms`：90% token 起点误差不超过的范围。
- `start_hit50`：起点误差不超过 50ms 的 token 百分比。
- `health.ok`：字幕结构通过单调性、重叠和零时长检查，不代表歌唱语义完全正确。
