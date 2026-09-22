# Windows 安装与故障排查指南

第一次使用请先看 [图解使用手册](USER_GUIDE.md)，其中包含网页截图和完整操作流程。

下文的 profile 指功能依赖组合，直接安装到系统默认 `python`，不会创建独立或虚拟环境。

这份指南适用于仓库根目录的 setup_guide.bat。安装助手按功能提供五个独立环境 profile；每个 profile 都会显示用途、安装进度，并在安装结束后运行对应的环境检查。

## 环境选择

菜单顺序就是推荐的功能顺序：

| 选项 | 环境 | 提供的功能 | GPU/CUDA | 模型 |
| --- | --- | --- | --- | --- |
| 1 | Whisper 字幕环境 | 已有歌词的 Whisper/stable-ts 对齐、字幕渲染、可选 Demucs 人声分离 | 需要 | Whisper |
| 2 | 演唱会切割最小环境 | 长视频音频分析、音量轴交互编辑、FFmpeg 分段导出 | 不需要 | 不需要 |
| 3 | Qwen 完整字幕环境 | Whisper 功能，加 Qwen ForcedAligner、ASR 歌词草稿和 Wav2Vec2 支持 | 需要 | Whisper、ForcedAligner、ASR |
| 4 | SOFA 歌声对齐环境 | Whisper 行窗口，加 SOFA 音素级歌声对齐 | 需要 | Whisper、SOFA checkpoint |
| 5 | 完整环境 | Whisper、Qwen、SOFA、Demucs 和全部字幕后端 | 需要 | Whisper、Qwen、SOFA |

依赖和模型分开安装。选择 profile 只安装 Python 依赖，不会自动下载多个 GB 的模型。这样只做演唱会切割时可以只安装第 2 项，不需要 CUDA、Whisper、Qwen 或 SOFA。

## 前置条件

1. 安装 Python 3.11 或更高版本，并确认命令行中的 python 指向该版本。
2. 安装 FFmpeg，将包含 ffmpeg.exe 和 ffprobe.exe 的目录加入 PATH。
3. 第 1、3、4、5 项需要 NVIDIA 驱动和可用 CUDA；第 2 项不需要 GPU。
4. 首次安装请预留足够磁盘空间。依赖安装和模型下载会显示 pip/下载器的实时进度。

检查基础工具：

~~~bat
python --version
ffmpeg -version
ffprobe -version
~~~

## 推荐安装流程

在仓库目录运行：

~~~bat
setup_guide.bat
~~~

然后按用途选择：

- 只切割演唱会：选择 **[2]**。它安装 requirements-concert.txt，完成后即可启动 WebUI 的演唱会标签。
- 使用已有歌词做 Whisper 对齐：选择 **[1]**，再选择 **[6]** 下载 Whisper checkpoint。
- 使用 Qwen 对齐或无歌词自动转写：选择 **[3]**，再选择 **[6]** 和 **[7]** 下载需要的模型。
- 使用 SOFA：选择 **[4]**，再选择 **[6]**，并手动放置 SOFA checkpoint。
- 需要全部功能：选择 **[5]**，随后按需下载模型。

也可以直接运行 profile：

~~~bat
call setup_profile.bat whisper
call setup_profile.bat concert
call setup_profile.bat qwen
call setup_profile.bat sofa
call setup_profile.bat full
~~~

setup.bat 是兼容入口，等同于安装完整环境：

~~~bat
setup.bat
~~~

## 模型下载

在安装助手中：

- **[6] Download Whisper model**：下载 large-v3 到 models\whisper\large-v3.pt，用于 Whisper 字幕流程，也作为 Qwen/SOFA 的时间定位预处理。
- **[7] Download Qwen models**：可以选择 ForcedAligner（约 1.8 GB）、ASR（约 4.7 GB）或两者。下载器显示文件进度，重复执行会复用已完成文件。
- SOFA checkpoint 当前没有可靠的统一自动下载源。将 v1.0.0_multilingual_singing.ckpt 放到：

~~~text
models\sofa\multilingual\pretrained_multilingual_singing\v1.0.0_multilingual_singing.ckpt
~~~

命令行等价操作：

~~~bat
python src\fetch_whisper.py --model large-v3
python src\fetch_models.py --only aligner
python src\fetch_models.py --only asr
python src\fetch_models.py
python src\fetch_models.py --list
~~~

## 检查和启动

安装助手的 **[8]** 会检查基础媒体工具、模型状态和本地磁盘。也可以针对某个 profile 检查：

~~~bat
python src\check_environment.py --profile concert
python src\check_environment.py --profile whisper
python src\check_environment.py --profile qwen
python src\check_environment.py --profile sofa
python src\check_environment.py --profile full
~~~

FAIL 表示依赖、FFmpeg 或 CUDA 必须修复；缺失模型显示为 WARN，可在模型下载后再次检查。

启动唯一的本地后台服务：

~~~bat
webui.bat
~~~

默认地址是 http://127.0.0.1:7870/。字幕和演唱会切割是同一服务中的两个独立标签，不要同时启动多个 WebUI 实例。

## 常见问题

### Python 版本不对

~~~bat
python --version
where python
~~~

如果低于 3.11，安装新版 Python 并重新打开命令行。

### FFmpeg 找不到

把 FFmpeg 的 bin 目录加入 PATH，重新打开命令行后确认 ffmpeg -version 和 ffprobe -version 都能执行。

### CUDA 不可用

只有 Whisper、Qwen、SOFA 和完整环境需要 CUDA。若只做演唱会切割，请改选 **[2]**。其他 profile 请确认 NVIDIA 驱动正常，并重新运行对应 profile 以安装 CUDA 12.8 PyTorch wheels。

### 下载中断

重复选择相同的下载项即可继续。模型目录中的已完成文件会被复用。网络受限时可先修复代理或镜像访问，再重试。

### 端口被占用

默认端口为 7870。先停止旧的 webui.py 进程，或运行：

~~~bat
python src\webui.py --port 8000
~~~

~~~bat
test.bat
~~~

测试使用仓库生成的合成素材，不读取个人媒体。
