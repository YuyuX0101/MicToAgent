# MicToAgent

[中文](README.md) | [English](README.en.md)

把麦克风说的话识别成文字，打进当前 agent 会话。语音只进不出，回复在目标界面里看。识别全部在本地跑，不把录音发到第三方云。

目前第一个适配器对接 **本机 Kimi Code Web**（按住/点按 右Ctrl）。

> 非官方项目，与 Moonshot AI 无隶属关系。"Kimi" 商标归 Moonshot AI 所有。本工具不收集账号、不转发语音。

<p align="center">
  <img src="docs/screenshots/panel-home.png" width="280" alt="控制面板" />
  <img src="docs/screenshots/panel-settings.png" width="280" alt="设置页" />
</p>

## 它做什么 / 不做什么

- **做**：Windows 上对着麦克风说话，把识别出的文字打进当前 agent 会话（和你打字等价）。目前对接本机 `kimi web`。
- **不做**：不是 [kimi.com](https://kimi.com) 云端聊天；没有语音合成；没有 macOS / Linux / Firefox 支持。

## 要求

- Windows 10+，Python **3.10+**，Chrome 或 Edge，麦克风
- 已安装 [Kimi Code CLI](https://www.kimi.com/code)
- 本机用下面的推荐命令跑着 `kimi web`（见「对接 Kimi Web」）
- 纯 CPU，不需要 GPU；模型需自行下载（见下）

## 对接 Kimi Web（必读）

**推荐默认**（本机、只给自己用）：

```text
kimi web --dangerous-bypass-auth
```

适配器请求 **不带 Bearer token**，标准带鉴权的 `kimi web` 会 401。所以把关鉴权当成配套启动方式，而不是可选项。

这会关掉 Web 的登录鉴权：任何能访问该端口的人都可以控制会话、文件系统和 shell。只在本机、可信环境使用。

官方默认端口是 **58627**；被占用时会变成 58628、58629…。把 `voice-bridge/config.json` 的 `kimi_web_base`（或仓库根 `.env` 的 `KIMI_WEB_BASE`）改成和浏览器地址栏一致。本项目**不会**自动扫端口。

MicToAgent 自己的适配器监听 `127.0.0.1:9100`，没有鉴权。`config.json` 的 `listen_host` **不要**改成 `0.0.0.0`。

## 安装

```powershell
git clone https://github.com/YuyuX0101/MicToAgent.git
cd MicToAgent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy voice-bridge\config.example.json voice-bridge\config.json
copy .env.example .env
py -3 scripts\download_models.py
```

`.env` 全部可选，克隆后不改也能用；模板里写了默认布局和何时才要改。模型也可以按 [`models/README.md`](models/README.md)（[English](models/README.en.md)）的链接手动下载。GitHub Release 在国内若失败，请自行下载后把目录名保持与说明一致，放到 `models/`。

桌面快捷方式（无终端窗口）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
```

也可以直接跑 `voice-assistant.ps1`（转发给 `voice-launcher.pyw`）。

## 配置

`voice-bridge/config.json` 本机私有（已 gitignore）。首次从 `config.example.json` 复制。

| 键 | 含义 |
| --- | --- |
| `kimi_web_base` | 对齐浏览器地址栏的主机和端口（最常要改） |
| `listen_host` / `listen_port` | 本适配器，保持 `127.0.0.1:9100`，不要改成 `0.0.0.0` |
| `core.stt_engine` | `dual`（默认，SenseVoice 初稿 + qwen3 精修）或 `qwen3_asr` |

其余键见 `config.example.json`。可选：用仓库根 `.env` 覆盖路径或端口（从 GitHub 克隆时**一般不用设** `MICTOAGENT_ROOT`）：

| 变量 | 用途 | 不设时 |
| --- | --- | --- |
| `MICTOAGENT_ROOT` | 项目根（`.venv` / `logs`） | 本仓库目录 |
| `MICTOAGENT_MODELS` | 识别模型目录 | `<项目根>/models` |
| `KIMI_WEB_BASE` | 覆盖 `config.json` 里的 Kimi Web 地址 | `config.json` 的 `kimi_web_base` |
| `KIMI_CLI` | 长段整理用的 kimi 可执行文件（可选；没有则长段原文提交） | `%USERPROFILE%\.kimi-code\bin\kimi.exe` |

## 使用

1. 本机先执行 `kimi web --dangerous-bypass-auth`，用 Chrome 或 Edge 打开，**点中一个会话**（或停在新建会话草稿页）。
2. 双击桌面「MicToAgent」。启动卡片走完后默认弹出控制面板和托盘图标 = 可用。设置里可开「静默启动」，下次只出现托盘。
3. 概览里选发送方式：**快速发送**（直达会话，无法配图）、**对话框发送**（粘贴并回车，可配图）、**输入对话框**（只放进输入框，不回车）。后两种要求前台已是 Kimi Web 且光标在输入框，否则会弹通知，不会自己切窗口。
4. **点按 右Ctrl** = 常听开关；**长按 右Ctrl** = 按住说话，松开提交。
5. 关面板默认回到托盘；托盘左键或再点快捷方式会把面板提到最前。停止：托盘右键，或在设置里把「关闭窗口」设为退出进程。日志在 `logs/`（每次启动覆盖）。

## 热词

识别听错的词写进 `voice-bridge/hotwords.txt`（一行一词，或 `错误词=>目标词`），热更新，下一句即生效。

## 排错

| 现象 | 先看 |
| --- | --- |
| 启动卡在加载模型 | `models/` 三个目录/文件是否齐全、目录名是否一字不差；`logs/voice-assistant.err.log` |
| 托盘始终不出现 | 同上；以及 `.venv` 是否按 `requirements.txt` 装过 |
| 说话后网页没字 | 浏览器地址栏端口是否等于 `kimi_web_base`；是否选中了会话；Kimi Web 是否在跑 |
| 通知「请先切到 Kimi Web」或「请先点一下输入框」 | 当前是对话框发送 / 输入对话框：请自己切到 Kimi Web 并点输入框，程序不会代切 |
| 快速发送带不上图片 | 这是该模式的限制；要配图请改用对话框发送或输入对话框 |
| 弹出未运行 / 401 | 是不是忘了 `--dangerous-bypass-auth`；地址栏端口是否等于 `kimi_web_base` |
| 开了 Clash / 系统代理后连不上本机 | 适配器只打 `127.0.0.1`，已忽略代理；若仍失败看端口是否对齐 |

**不要**把 `logs/` 全文贴到 issue（里面有识别原文）。模板见 [Issue](https://github.com/YuyuX0101/MicToAgent/issues/new/choose)。

## 架构

```
麦克风 → mic-gate（右Ctrl 门控，写 gate.state）
      → voice-core（ten-vad 分段 → SenseVoice 初稿 / qwen3-asr 精修接力识别 → 热词纠错）
      → adapter（OpenAI 兼容接口 127.0.0.1:9100，按发送模式转发到当前 Kimi 会话）
voice-launcher 一键拉起以上全部进程和控制面板。
```

运行代码全在 `voice-bridge/`。适配器细节见 [`voice-bridge/README.md`](voice-bridge/README.md)。参与开发见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 许可证

代码 [Apache-2.0](LICENSE)。第三方与模型许可见 [NOTICE](NOTICE) 和 [`models/README.md`](models/README.md)（[English](models/README.en.md)）。**ten-vad 是修改版 Apache 2.0，商用有额外限制**，商用前请核对上游 LICENSE。

安全问题见 [`SECURITY.md`](SECURITY.md)。
