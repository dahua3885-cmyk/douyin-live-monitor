# 第三方组件

本仓库的 MIT 许可仅覆盖本项目应用源码，不替代第三方组件的许可。源码仓库不附带第三方二进制或模型权重。

运行依赖及其上游项目：

- [Python](https://www.python.org/)：Python 运行时，查看其发行包中的许可。
- [aiohttp](https://github.com/aio-libs/aiohttp)：本地 HTTP 服务。
- [Playwright](https://github.com/microsoft/playwright)：浏览器自动化；浏览器及其第三方组件另有许可通知。
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)：本地语音识别，包含 CTranslate2 等传递依赖。
- [Hugging Face Hub](https://github.com/huggingface/huggingface_hub)：显式模型下载脚本使用的客户端。
- [Whisper](https://github.com/openai/whisper) / [转换后的 small 模型](https://huggingface.co/Systran/faster-whisper-small)：使用模型前请阅读上游模型卡和许可。
- [FFmpeg](https://ffmpeg.org/legal.html)：外部命令行工具，不附带于此源码仓库；具体许可取决于所安装构建的配置。

Python 包及其传递依赖的完整许可随实际安装版本提供。再分发运行时、浏览器、模型或 FFmpeg 时，应保留对应许可，并履行该具体构建的再分发要求。
