# 根据人工批注制作修订版

`src/feedback_revision.py` 把一个版本上的人工复核决定应用到一个 `align.json`，输出**独立**的修订版时间轴和反馈记录。它不修改原始对齐、不改变成品库审批，也不会自动通过任何发布检查。

## 命令

```powershell
python src\feedback_revision.py --align out\my-concert\repair\r1\01_Song\align.json `
  --decisions out\my-concert\feedback-r1.json --output out\my-concert\revisions-r1\01_Song
```

- `--align`：已对齐的 `align.json`；时间必须对应被复核的那个版本，不能把新版时间套到最早片段上。
- `--decisions`：人工决定 JSON，格式见下。
- `--output`：独立输出目录，不能与 `align.json` 所在目录相同；目录不存在时会创建。

命令成功时打印 JSON 摘要（输出目录、行数、未解决行号、是否需要继续核对），退出码 `0`；校验失败时只打印错误信息，退出码 `1`，且不写输出文件。

## decisions.json

```json
{
  "review": {"file_path": "out\\reviews\\r1.mp4", "file_signature": "123456:1717999999000000000"},
  "start": 12.5,
  "end": 300.0,
  "available_duration": 320.0,
  "omissions":  [{"row": 8, "text": "现场未唱的整句", "reason": "人工听看确认跳过"}],
  "replacements": [{"row": 12, "line": {"raw": "同一句歌词", "head": "", "start": 130.0, "end": 133.0, "tokens": [{"text": "同一句歌词", "disp": "同一句歌词", "start": 130.0, "end": 133.0}]}}],
  "quarantine": [{"row": 20, "text": "无法定位的句子", "reason": "两条对齐路线都未定位"}]
}
```

- `review` 可选。提供时先校验该文件的大小与修改时间签名；文件已变化则拒绝本次修订，需要重新绑定复核记录。
- `start` / `end`：可用媒体范围（秒），用于裁掉未唱的前奏、尾奏或片源外部分；必须落在可播放范围内，且不能切断仍保留的歌词。
- `available_duration`：片源实际可用时长，用于确认新的结尾没有超出素材。
- `omissions`：确认现场未唱的行，必须同时给出原行号、**完全匹配**的歌词文本和人工依据。未被 ASR 识别到不构成删除依据。
- `replacements`：用同一文本的实测时间替换某行时间，文本必须一致，不能借此改词。
- `quarantine`：已知参考行但无法可靠定位。该行从显示时间轴中移除，但完整保留在反馈记录中，并**继续阻断**整曲导出；它与「确认未唱」不同。

复读、补尾与删句必须逐项记录依据；网络上的标准时长只能通过 `duration_reference()` 记为提示，不能直接决定裁切点。

## 输出

- `align.json`：修订后的对齐结果；`options`、`health` 等字段沿用输入，时间轴已按 `start` 平移。
- `feedback.json`：与修订版 `align.json` 中的 `feedback` 对象一致。

`feedback` 字段说明：

| 字段 | 含义 |
| --- | --- |
| `timeline_offset` / `end` / `available_duration` | 本次修订采用的媒体范围（秒） |
| `source_duration` | 输入对齐的原始时长，用于追溯裁切 |
| `rows` | 旧行号 → 新行号映射；含 `text`、`omitted`、`disposition`（`retained` / `confirmed_omission` / `unresolved`）、`reason`、`timing_replaced` |
| `unresolved_rows` | 被隔离的行：`source_row`、完整歌词行、原因 |
| `requires_reference_review` | 是否仍有人工无法定位的行，为真时必须继续复核 |
| `human_approved` | 恒为 `false`；修订版仍需人工审批 |

## 与其他流程的关系

- 修订版成品注册到成品库时使用 `repair_delivery.register(..., media=..., source_start=..., source_path=...)`，让字幕编辑器打开匹配的源片段；原审批仍绑定旧版。
- 隔离行会通过 `repair_validation.validate_render()` 阻断整曲导出，直到这些行被解决或明确记录。
- 本工具不删除歌词、不改写审批、不上传任何数据。
