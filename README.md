# Lets Karaoke

本项目是一个本地运行的歌词对齐与卡拉 OK 字幕生成工具。输入音频、视频和歌词后，程序可以完成音频处理、歌词对齐、置信度诊断、人工锚点修正与字幕渲染。

仓库只包含通用源代码、合成测试数据和文档，不包含真实歌曲、受版权保护歌词、模型权重、视频或任何个人机器上的运行记录。

## 环境

- Windows 11
- 系统 Python（直接安装依赖，不使用项目虚拟环境）
- NVIDIA CUDA 环境可用于加速
- FFmpeg、PyTorch 及其他依赖见 `requirements.txt`
- 模型下载到本地缓存目录，不进入 Git 仓库

运行环境检查：

```bat
python src\check_environment.py
```

安装依赖：

```bat
setup.bat
```

## 使用

启动网页界面：

```bat
webui.bat
```

在网页中上传自己的音频、视频和歌词，生成字幕并查看逐句对齐依据。调整锚点后，可以重新对齐、渲染或保存历史版本。

## 测试

仓库内的测试使用程序生成的合成音频，不依赖个人文件：

```bat
test.bat
```

## 目录

- `src/`：对齐、诊断、渲染和网页界面
- `tests/`：通用单元测试和端到端测试
- `data/synth/`：合成测试音频及清单
- `tools/SOFA/`：SOFA 对齐后端源码
- `docs/DEPENDENCY_LICENSES.md`：依赖许可证说明
- `.gitignore`：模型、媒体、缓存和本地结果的排除规则

## 许可证

代码许可证和第三方依赖说明见 `docs/DEPENDENCY_LICENSES.md`。用户上传媒体的版权由使用者自行负责。
