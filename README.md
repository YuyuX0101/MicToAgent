# MicToAgent

[中文](README.md) | [English](README.en.md)

把麦克风说的话识别成文字，打进当前 agent 会话。语音只进不出，回复在目标界面里看。识别全部在本地跑，不把录音发到第三方云。

目前对接 **本机 Kimi Code Web**：**点按 右Ctrl** 开关常听，**长按 右Ctrl** 按住说话。

> 非官方项目，与 Moonshot AI 无隶属关系。"Kimi" 商标归 Moonshot AI 所有。本工具不收集账号、不转发语音。

<p align="center">
  <img src="docs/screenshots/panel-home.png" width="280" alt="控制面板" />
  <img src="docs/screenshots/panel-settings.png" width="280" alt="设置页" />
</p>

## 亮点

- **说完即出字**：双引擎接力识别——话音片段先由 SenseVoice 秒出初稿，qwen3-asr 后台精修覆盖；提交只等初稿，零等待。中间停顿思考不会被切成两条消息，自动拼接成完整一句。
- **三种发送模式，面板一键切换**：**快速发送** = 直达当前会话，全程零打扰（但不能配图）；**对话框发送** = 粘贴并回车，可配图；**输入对话框** = 只放进输入框，发送前随便改。鼠标悬停按钮即有说明。
- **无缝跟随会话**：你在 Kimi Web 里点选哪个会话，语音就进哪个会话——换会话不需要任何操作，接着说就行。面板实时显示当前工作区与会话名，你在网页里改会话名，面板一秒内同步。
- **一个键两种说法**：**点按 右Ctrl** 开关常听，适合连续多轮免按键；**长按 右Ctrl** 按住说话，松手即发。
- **越用越准的热词**：听错的词写进 `voice-bridge/hotwords.txt`（一行一词），下一句识别就按新词表纠正，不用重启。程序员词汇、项目专有名词都能教它。
- **语音不出本机**：识别全部本地完成（纯 CPU，不需要 GPU），录音不发往任何第三方云。

## 设备要求

识别模型在本地跑，装机前确认一下：

| 项 | 要求 |
| --- | --- |
| 系统 | Windows 10 / 11（64 位）；面板依赖 WebView2 运行时（Win11 自带，Win10 一般已预装） |
| CPU | 纯 CPU 推理，**不需要 GPU**；建议 4 核及以上（识别默认占 4 线程） |
| 内存 | 可用 ≥ 4 GB（两个识别模型常驻内存约 1.5 GB） |
| 磁盘 | 模型约 1.2 GB，连同 Python 依赖预留约 2 GB |
| 其他 | Python **3.10+**、Chrome 或 Edge、麦克风、已装 [Kimi Code CLI](https://www.kimi.com/code) |

## 快速开始

**第 1 步 · 启动 Kimi Web**（在终端执行，保持运行）

```text
kimi web --dangerous-bypass-auth
```

> ⚠️ 这会关掉 Web 的登录鉴权：任何能访问该端口的人都可以控制会话、文件系统和 shell。只在本机、可信环境使用。MicToAgent 的适配器请求不带 token，关鉴权是配套启动方式，不是可选项。

**第 2 步 · 安装**（PowerShell）

```powershell
git clone https://github.com/YuyuX0101/MicToAgent.git
cd MicToAgent
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy voice-bridge\config.example.json voice-bridge\config.json
py -3 scripts\download_models.py
```

**第 3 步 · 创建桌面图标并启动**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
```

双击桌面「MicToAgent」，启动卡片走完、托盘图标出现 = 可以用了。在 Kimi Web 里**点中一个会话**，按住 **右Ctrl** 说一句话试试。

模型也可以按 [`models/README.md`](models/README.md)（[English](models/README.en.md)）手动下载：GitHub Release 在国内若失败，自行下载后保持目录名一致放进 `models/`。

## 用起来

- **发送方式**（控制面板随时切）：**快速发送** = 直达会话，无法配图；**对话框发送** = 粘贴并回车，可配图；**输入对话框** = 只放进输入框等你检查。后两种要求前台已是 Kimi Web 且光标在输入框，程序不会替你切窗口，条件不满足会弹通知。
- **热词纠错**：识别听错的词写进 `voice-bridge/hotwords.txt`（一行一词，或 `错误词=>目标词`），热更新，下一句生效。
- **停止**：托盘右键「退出」；或在面板设置里把「关闭窗口」设为退出进程。关面板默认只是回托盘。
- 想开机不弹窗：设置里开「静默启动」（不弹面板）和「启动进度卡」（关闭后完全静默）。
- 日志在 `logs/`（每次启动覆盖），排查问题要在重启前看。

## 出问题先看这里

| 现象 | 先看 |
| --- | --- |
| 启动卡在加载模型 | `models/` 三个目录/文件是否齐全、目录名一字不差；`logs/voice-assistant.err.log` |
| 托盘始终不出现 | 同上；以及 `.venv` 是否按 `requirements.txt` 装过 |
| 说话后网页没字 | 浏览器地址栏端口是否等于 `kimi_web_base`（见下）；是否选中了会话；Kimi Web 是否在跑 |
| 通知「请先切到 Kimi Web」或「请先点一下输入框」 | 当前是对话框发送 / 输入对话框：请自己切到 Kimi Web 并点输入框，程序不会代切 |
| 快速发送带不上图片 | 这是该模式的限制；要配图请改用对话框发送或输入对话框 |
| 弹出未运行 / 401 | 是不是忘了 `--dangerous-bypass-auth`；端口是否对齐 |
| 开了 Clash / 系统代理后连不上本机 | 适配器只打 `127.0.0.1`，已忽略代理；若仍失败看端口是否对齐 |

**端口对不上**：`kimi web` 官方默认端口是 **58627**，被占用会变 58628、58629…。把 `voice-bridge/config.json` 的 `kimi_web_base`（或仓库根 `.env` 的 `KIMI_WEB_BASE`）改成和浏览器地址栏一致。本项目不会自动扫端口。

提 issue 时**不要**贴 `logs/` 全文（里面有识别原文）。

## 配置（一般不用动）

`voice-bridge/config.json` 本机私有（已 gitignore），首次从 `config.example.json` 复制：

| 键 | 含义 |
| --- | --- |
| `kimi_web_base` | 对齐浏览器地址栏的主机和端口（最常要改） |
| `listen_host` / `listen_port` | 本适配器，保持 `127.0.0.1:9100`，**不要**改成 `0.0.0.0`（适配器没有鉴权） |
| `core.stt_engine` | `dual`（默认，SenseVoice 初稿 + qwen3 精修）或 `qwen3_asr` |

其余键见 `config.example.json`。仓库根 `.env` 全部可选（从 GitHub 克隆时一般不用设 `MICTOAGENT_ROOT`）：

| 变量 | 用途 | 不设时 |
| --- | --- | --- |
| `MICTOAGENT_ROOT` | 项目根（`.venv` / `logs`） | 本仓库目录 |
| `MICTOAGENT_MODELS` | 识别模型目录 | `<项目根>/models` |
| `KIMI_WEB_BASE` | 覆盖 `config.json` 里的 Kimi Web 地址 | `config.json` 的 `kimi_web_base` |
| `KIMI_CLI` | 长段整理用的 kimi 可执行文件（可选；没有则长段原文提交） | `%USERPROFILE%\.kimi-code\bin\kimi.exe` |

## 架构

```
麦克风 → mic-gate（右Ctrl 门控，写 gate.state）
      → voice-core（ten-vad 分段 → SenseVoice 初稿 / qwen3-asr 精修接力识别 → 热词纠错）
      → adapter（OpenAI 兼容接口 127.0.0.1:9100，按发送模式转发到当前 Kimi 会话）
voice-launcher 一键拉起以上全部进程和控制面板。
```

运行代码全在 `voice-bridge/`。适配器细节见 [`voice-bridge/README.md`](voice-bridge/README.md)，参与开发见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 反馈

- **bug、建议、使用问题**：直接开 [Issue](https://github.com/YuyuX0101/MicToAgent/issues/new/choose)，欢迎。
- **安全漏洞**：请不要开公开 issue，走 [`SECURITY.md`](SECURITY.md) 里的私下渠道。

## 许可证

代码 [Apache-2.0](LICENSE)。第三方与模型许可见 [NOTICE](NOTICE) 和 [`models/README.md`](models/README.md)（[English](models/README.en.md)）。**ten-vad 是修改版 Apache 2.0，商用有额外限制**，商用前请核对上游 LICENSE。
