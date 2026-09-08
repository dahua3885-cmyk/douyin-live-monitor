# 直播监控台 · Douyin Live Monitor

在自己的 Windows 电脑上查看抖音开播状态、记录人数变化、保存直播录像，并用本地语音模型留下可搜索的话术档案。

[![Tests](https://github.com/dahua3885-cmyk/douyin-live-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/dahua3885-cmyk/douyin-live-monitor/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

**当前版本：0.4.2 · 源码版 · Windows 10/11 x64**

## 可以做什么

| 功能 | 当前行为 |
| --- | --- |
| 多直播间监控 | 添加直播间、主播主页或分享链接；同一入口支持多行和文件导入、去重；支持批量开始、暂停、移除 |
| 数据趋势 | 开播状态、在线人数、点赞等可取得的指标；标记数据过期、采集失败与中断，缺失数据不连成正常趋势 |
| 视频录制 | 默认关闭，每个账号单独开关；首次开启必须选择保存文件夹 |
| 全局录像设置 | 所有账号共用保存目录、分段时长、累计时长上限、画质和 MP4 转换设置 |
| 分段与 MP4 | 默认每 30 分钟保存一段 TS；片段完成后可生成 MP4，并保留原始 TS |
| 画质与时长 | 首选画质不可用时降档；累计时长默认不限，支持 180 分钟等快捷选项及 1–720 分钟自定义 |
| 本地转写 | faster-whisper / Whisper small，CPU INT8；按约 30 秒音频片段转写，无转写 API 费用 |
| 直播档案 | 查看每场的录像、话术、人数走势、采集中断；历史采集片段可按账号和日期聚合查看 |
| 搜索与定位 | 搜索话术，点击时间定位对应 MP4；没有可播放视频时提供已保存音频 |
| 结构拆解 | 按关键词规则生成开场、讲解、互动、转化及重复轮次草稿，附原话与时间证据 |
| 异常提醒 | 页面显示录制开始、停止、失败、磁盘不足等状态；飞书群机器人推送可选 |
| 后台运行 | Windows 登录后启动，进程异常退出后尝试恢复；可手动停止或关闭自启 |

结构拆解是**规则辅助草稿**，需要人工复核。转写速度取决于 CPU 和同时转写的直播间数量，30 秒是音频切片时长，不是保证的最终延迟。平台页面或接口变化、登录状态和网络也会影响采集。

## 安装

首次安装需要联网。本仓库提供应用源码，不包含 Python、FFmpeg、浏览器二进制或模型权重。

1. 安装 [Python](https://www.python.org/downloads/windows/) 3.11 或以上的 64 位版本（推荐 3.12），安装时选择加入 PATH。
2. 从 [FFmpeg 官方下载入口](https://ffmpeg.org/download.html) 选择 Windows 构建，将其 `bin` 目录加入 PATH；确认 `ffmpeg -version` 和 `ffprobe -version` 都能执行。
3. 下载本仓库 ZIP 并完整解压，或执行以下命令。在项目文件夹打开 PowerShell：

```powershell
git clone https://github.com/dahua3885-cmyk/douyin-live-monitor.git
cd douyin-live-monitor
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

已有 Edge 或 Chrome 时，采集器也会尝试使用它们。安装 Chromium 可以提供备用浏览器。无需 Node.js 或前端构建。

需要话术转写时，下载一次本地模型：

```powershell
.\.venv\Scripts\python.exe scripts/download_model.py
```

模型下载到 `models/whisper-small/`，需要数百 MB 空间。若网络无法访问模型源，可从 [模型仓库](https://huggingface.co/Systran/faster-whisper-small) 下载完整文件并放在该目录。也可通过 `LIVE_TRANSCRIBE_MODEL_DIR` 指定已有模型目录。转写进程运行时使用离线模式。

## 启动与使用

双击 **启动直播监控台.cmd**，或执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

浏览器打开 `http://127.0.0.1:18765/`。此启动方式会启用**后台守护和 Windows 登录后自启**：优先注册当前用户计划任务，权限不足时使用当前用户启动文件夹。双击 **关闭登录后自启.cmd** 可以取消自启；双击 **停止直播监控台.cmd** 会暂停监控并停止本次后台运行，历史数据保留。

只想临时运行、不设置后台或自启，可在终端执行：

```powershell
.\.venv\Scripts\python.exe server.py
```

此模式关闭终端即停止服务。浏览器页面关闭不会停止后台模式。关机、睡眠、注销或网络断开期间无法采集；恢复后也不能补录已错过的内容。

1. 点击「添加直播间」，粘贴一个或多个链接，每行一个。
2. 如提示登录或验证，使用工具打开的浏览器完成登录，然后重试采集。
3. 打开需要跟踪账号的「监控」开关。
4. 点击直播间列表工具栏的「录像设置」，在弹窗中配置一次全局设置，选择保存文件夹，再按账号开启录像。新参数用于下一次录制，正在录制的片段沿用开始时设置。保存成功后弹窗关闭；取消或按 Esc 关闭会放弃尚未保存的参数修改。
5. 在「直播话术」开启转写；在「直播档案」查看历史、搜索话术、定位视频与查看结构草稿。
6. 如需通知，在「飞书提醒设置」填写自己的群机器人 Webhook，可选签名密钥，再选择事件并启用。点击测试会实际向已保存的群发送一条消息。

第一次直接点击账号的录像开关，也会打开完整的全局「录像设置」。选择文件夹后先返回设置窗口，点击「保存并开启录像」才启用该账号；取消会放弃尚未保存的目录与参数草稿，不会开始录像。完成一次全局配置后，其他账号沿用同一套设置。

### 分段与累计时长有什么区别？

分段时长控制每个视频文件有多长，累计上限控制本场一共录多久。例如分段 30 分钟、累计 180 分钟，会保存约 6 个文件，属于同一场档案。分段便于及时播放、搬运和处理长直播，并减少意外中断对尚未完成文件的影响；它不能保证断电时最后一段无损。累计上限选「不限」可一直录到下播、手动停止或异常停录。

### 数据保存在哪里？

- `data/monitor.sqlite3`：账号配置、采集数据、直播档案、转写文本、飞书设置等。
- `data/transcription-audio/`：用于转写和回听的音频片段。
- 自己选择的视频保存文件夹：录像 TS、生成的 MP4，按账号组织。
- `data/` 下的浏览器资料和日志：登录状态、运行诊断、守护程序配置等。

**不导出也会保留话术和档案**。移除账号会隐藏账号，保留既有档案。删除数据目录或清理媒体文件会造成资料丢失；备份时先停止程序，再备份整个 `data/` 和外部录像文件夹。

本机存储不代表没有网络连接：直播采集访问抖音，首次依赖和模型安装访问下载源，启用飞书推送后会向自己的机器人发送所选事件。转写音频不上传到转写 API。飞书地址、签名密钥和浏览器登录状态属于本地敏感数据，不要上传数据目录或用于公开截图。

### 自定义端口或数据目录

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start.ps1 -Port 18766 -DataDir D:\LiveMonitorData
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\stop.ps1 -Port 18766 -DataDir D:\LiveMonitorData
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\disable-autostart.ps1 -Port 18766 -DataDir D:\LiveMonitorData
```

三个命令需使用一致的端口和数据目录。也可以设置 `LIVE_MONITOR_DATA`。启动脚本优先使用项目 `.venv`，未找到时使用 PATH 中的 Python。

## 常见问题

- **页面提示无法连接本地服务**：先重新启动程序；查看 `data/service.log`、`data/server.stderr.log` 和 `data/supervisor.log`。端口占用时换端口。浏览器页面自身无法让已退出的后台复活。
- **话术下载失败**：确认本地服务在线后，在页面重试导出；历史文本仍保存在数据库中。
- **没有录像**：检查账号录像开关、全局保存目录、直播是否正在进行、FFmpeg 和磁盘空间。默认不录制，也不录整个电脑桌面；录制的是直播音视频流。
- **转写提示缺少模型**：先运行下载脚本；确认模型目录中有 `model.bin`、`config.json`、`tokenizer.json`。
- **录制自动停止**：检查累计时长上限、下播、网络或磁盘状态。当前逻辑在剩余空间低于 5 GB 时提醒，低于 512 MB 时停录。
- **历史直播显示为多个片段**：新档案使用采集到的平台场次证据；旧数据缺少场次证据时按账号和日期聚合展示，不凭空推断精确下播时间。

## 开发与验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据库、合成页面数据和模拟通知，不需要真实直播账号、模型下载或飞书 Webhook。仓库配置了 Windows GitHub Actions。详情见 [开发说明](docs/development.md)。

## 许可

应用源码使用 [MIT License](LICENSE)。外部依赖、模型及浏览器各自遵守上游许可，见 [第三方组件说明](THIRD_PARTY_NOTICES.md)。本项目不是抖音官方产品。请只采集、保存和使用自己有权处理的内容，遵守平台规则及相关权利要求。
