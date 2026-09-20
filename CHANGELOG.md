# 更新记录

## Unreleased

- 按功能拆分 Windows 部署 profile：Whisper 字幕、演唱会切割最小环境、Qwen 完整字幕、SOFA 歌声对齐和完整环境；安装助手按该顺序显示用途、依赖和模型下载步骤。
- 新增 `requirements-concert.txt`、`requirements-base.txt`、`requirements-whisper.txt`、`requirements-qwen.txt`、`requirements-sofa.txt` 和 `requirements-full.txt`，并保留 `requirements.txt` 作为完整环境聚合入口。
- 新增 Whisper checkpoint 下载器和 profile-aware 环境检查；演唱会切割 profile 不安装 CUDA、字幕模型或 GPU 依赖。

- 新增 `setup_guide.bat` 交互式 Windows 安装助手、详细环境诊断和安装排查指南；新增英文 README。

- 新增工作台标签页：原字幕工坊保持独立，切换不重载页面或丢失表单。
- 新增演唱会切割：本地路径读取、流式音量/频谱候选边界分析、边界试听、时间编辑、拆分合并、快速/精确导出、取消与历史恢复。声学候选需人工复核，不自动识别歌名。
- 新增使用合成视频的切割与 HTTP 回归测试，纳入 `test.bat`。
- 修复切割记录重新分析时的前端 JSON 解析报错；新增按当前灵敏度重新分析入口，并在接口返回非 JSON 时显示具体请求状态。
- 修复本地外部浏览器请求校验和 HTTP/1.1 请求体未消费造成的偶发 403/400；兼容 localhost、127.0.0.1 和本机回环来源。
- 统一本地后台服务为 `127.0.0.1:7870`，停止重复的 `18888` 实例并更新启动脚本默认端口。
- 在音量轴旁新增“删除边界并合并”按钮，可合并选中边界或播放位置附近的边界，并立即同步时间表。
- 整理为可公开发布的本地歌词对齐与字幕渲染工具。
- 保留系统 Python、模型缓存、可解释对齐诊断、锚点调整和历史记录功能。
- 测试数据仅使用仓库内生成的合成音频，不包含真实歌曲、歌词、视频或本地运行结果。
