# 开发说明

## 模块

| 文件 | 职责 |
| --- | --- |
| `server.py` | 本机 HTTP API、基础数据库、采集调度、生命周期 |
| `collector.py` | Playwright 采集、直播间链接解析、页面响应提取 |
| `archive.py` | 场次档案、旧数据迁移、历史聚合、结构规则草稿 |
| `media.py` / `recorder.py` | 录制进程、分段、时长上限、MP4 转换、媒体播放 |
| `speech.py` / `asr_worker.py` | 音频切片、离线模型子进程、结果落库 |
| `features.py` / `storage.py` | 扩展 API、全局偏好、文件夹选择、导出 |
| `notifications.py` | 飞书签名、事件发送、去重重试、发送记录 |
| `supervisor.py` / `process_job.py` | 进程守护、Windows 子进程清理 |
| `static/` | 无构建步骤的 HTML、CSS、JavaScript 界面 |

API 和 UI 默认仅绑定 `127.0.0.1`，并有来源检查。这是单用户桌面工具，没有多人权限系统；不要直接改成公网服务。

## 验证

Python 测试：`python -m unittest discover -s tests -v`。

前端语法：`node --check static/app.js`、`node --check static/features.js`。Node 仅用于开发检查，运行应用不需要它。

测试覆盖账号增删、软删除恢复、数据过期、URL 校验、采集解析、录制限制、全局设置迁移、转写存储、媒体访问、通知和场次归档等。真实平台可用性、长时间录制表现及模型速度仍需要在实际环境中观察；合成测试不等同于线上采集保证。主要支持并验证 Windows，其他系统没有完整桌面启动验收。

开发时使用独立端口和临时数据目录，避免读取或改动正在使用的数据。例如 `python server.py --port 18766 --data-dir ./data/dev`。不要在测试中访问真实 Webhook。

## 提交与发布

只提交源码、合成测试和公共说明。不得提交数据库、真实转写、录音录像、登录资料、带签名的流地址、Webhook、私有需求文档或本机日志。`.gitignore` 是辅助措施，提交前还应查看 `git diff --cached`。

本次发布为源码版。运行时、模型和媒体工具由使用者自行安装；机器专用的内部打包脚本不在源码发行范围内。如要制作包含第三方二进制的安装包，需要单独整理对应版本的上游许可、通知和再分发材料。

下载模型脚本仅在显式执行时联网；应用转写进程始终使用本地模型。若改用其他模型，应同步修改下载说明和模型查找逻辑。
