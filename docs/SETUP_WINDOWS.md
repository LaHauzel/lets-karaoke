# Windows 安装与故障排查指南

这份指南适用于仓库根目录的 `setup_guide.bat`。它提供一个交互菜单，负责环境检查、依赖安装、模型下载和 WebUI 启动。

## 推荐流程

1. 安装 Python 3.11+，并确保命令行中的 `python` 指向该版本。
2. 安装 FFmpeg，将包含 `ffmpeg.exe` 和 `ffprobe.exe` 的目录加入系统 `PATH`。
3. 双击 `setup_guide.bat`，或在仓库目录运行：

   ```bat
   setup_guide.bat
   ```

4. 选择“安装或修复 Python 依赖”。
5. 选择“详细检查环境”。必须修复标记为 `FAIL` 的项目；模型显示为 `WARN` 时可以在下一步下载。
6. 选择“下载 ForcedAligner 模型”。这是已有歌词对齐所需的模型，约 1.8 GB。
7. 如果需要无歌词自动转写，再选择“下载 ASR 模型”，约 4.7 GB。
8. 选择“启动 WebUI”，在 <http://127.0.0.1:7870/> 打开工作台。

## 菜单说明

| 选项 | 用途 |
| --- | --- |
| 详细检查环境 | 检查 Python 版本、依赖导入、FFmpeg/FFprobe、CUDA/GPU、模型状态和可用磁盘 |
| 安装或修复 Python 依赖 | 安装 CUDA 版 PyTorch/torchaudio 和 `requirements.txt` |
| 下载 ForcedAligner 模型 | 下载 Qwen3-ForcedAligner-0.6B |
| 下载 ASR 模型 | 下载 Qwen3-ASR-1.7B |
| 下载全部模型 | 按顺序下载两个模型 |
| 查看模型状态 | 不下载，只列出模型是否完整及占用空间 |
| 启动 WebUI | 启动唯一的本地后台服务 |
| 打开安装指南 | 在记事本中打开本文件 |

模型下载由 `src/fetch_models.py` 执行。它会优先尝试 ModelScope，再尝试 Hugging Face 镜像和官方源；底层下载器显示文件进度，并可重复运行以继续未完成的下载。

## 系统要求

- Windows 11。
- Python 3.11 或更高版本。
- NVIDIA GPU 和可用 CUDA 驱动。当前安装脚本安装 CUDA 12.8 PyTorch wheels。
- FFmpeg 和 FFprobe 在 `PATH` 中。
- ForcedAligner 至少需要约 1.8 GB 模型空间；同时下载 ASR 时建议预留 10 GB 以上可用空间。
- 网络可以访问模型下载源；如使用代理，请先确认代理能正常下载大文件。

## 检查结果如何处理

### Python 版本失败

在命令行运行：

```bat
python --version
where python
```

如果版本低于 3.11，请安装新版本，并把正确的 Python 放到 `PATH` 前面。重新打开命令行后再运行检查。

### FFmpeg 或 FFprobe 缺失

安装 FFmpeg 的 Windows 构建，将其 `bin` 目录加入系统或用户 `PATH`，然后重新打开命令行。确认：

```bat
ffmpeg -version
ffprobe -version
```

### CUDA 不可用

先确认 NVIDIA 驱动正常，再确认 PyTorch 与 torchaudio 版本匹配。选择“安装或修复 Python 依赖”会按当前脚本重新安装 CUDA 12.8 wheels。若仍失败，请保留环境检查输出和 `python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available())"` 的结果。

### 模型缺失或不完整

模型目录为：

- `models/Qwen3-ForcedAligner-0.6B`
- `models/Qwen3-ASR-1.7B`

重新选择对应下载项即可。下载器会检查 `config.json` 和权重文件；目录存在但文件不完整时会继续补齐。不要把模型目录提交到 Git。

### 下载中断或速度慢

可以安全地重新运行同一个下载项。下载器会复用已经存在的文件。也可以直接运行：

```bat
python src\fetch_models.py --only aligner
python src\fetch_models.py --only asr
python src\fetch_models.py --hf
```

`--hf` 会跳过 ModelScope，优先使用 Hugging Face 路线。若公司或本机代理拦截 HTTPS，请先修复网络配置；不要把代理地址写入仓库文件。

### 端口被占用

默认服务端口是 `7870`。若页面无法打开，先关闭其他旧的 `webui.py` 进程，或使用：

```bat
python src\webui.py --port 8000
```

不要同时启动两个 WebUI 实例，否则会出现端口冲突和历史记录竞争。

## 手动命令

不使用菜单时，可以按下面的顺序执行：

```bat
python src\check_environment.py
call setup.bat
python src\fetch_models.py --list
python src\fetch_models.py --only aligner
webui.bat
```

环境检查会把缺少模型列为提醒，把影响运行的依赖、FFmpeg 或 CUDA 问题列为失败。字幕和演唱会切割任务都在本机运行，历史记录和导出结果写入 `out/`。

