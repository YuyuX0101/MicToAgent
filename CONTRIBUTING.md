# 参与贡献

先说一句：**提 issue 也是贡献**。bug、功能建议、使用问题都欢迎直接开
[Issue](https://github.com/YuyuX0101/MicToAgent/issues/new/choose)，不用先问、不用犹豫。
唯一的例外是安全漏洞，请走 [`SECURITY.md`](SECURITY.md) 的私下渠道。

欢迎修 bug、补测试、改文档。改语音链路或适配器前请先读：

- [`voice-core.py`](voice-bridge/voice-core.py) 文件头（切段 / 宽限 / 双引擎的权威描述）
- [`voice-bridge/README.md`](voice-bridge/README.md)（适配器、门控、启动器踩坑）

## 环境

Windows 10+。在仓库根目录：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest
copy voice-bridge\config.example.json voice-bridge\config.json
copy .env.example .env
```

单测不加载识别模型：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

## 约定

- **语音提交非幂等，任何一层都不得加超时重发。** 重发 = 目标会话收到重复消息。
- 识别推理抛错必须按空串回流，识别线程不能死。
- 日志锚点不可改：`[stt] loading engine=` 和 `[stt] engine ready`（启动向导靠它们探进度）。
- Git Bash 的 `curl -d` 会损坏中文 JSON，脚本发中文一律用 Python + httpx。
- 不要把本机 `config.json`、`.env`、`logs/`、热词里的个人误听样本硬塞进仓库。
- 最小改动：只改需求需要的部分，避免无关重构。

发 PR 前请在本机跑通 `pytest tests/ -q`。请遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
