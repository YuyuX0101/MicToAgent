# voice-bridge — 语音接入 Kimi Web 的桥接适配器

把 speech-to-speech 语音助手和 Kimi Web 的「语音总管」会话连起来的传声筒。
适配器本身**不含任何 LLM 调用**，只做转发、轮询和状态管理。

## 架构

```
你说话 → speech-to-speech (STT 转成文字)
       → POST http://127.0.0.1:9100/v1/chat/completions   ← 本适配器
       → 转发给 Kimi Web（地址见 config.json 的 kimi_web_base）里的「语音总管」会话
       → 总管（完整的 Kimi Code agent）自己决定：
           · 闲聊 → 直接回复
           · 干活 → 用 curl 调 Kimi Web REST API 派单到对应项目的会话
       → 适配器取回总管最后一条回复 → 返回给 speech-to-speech → TTS 读出来
```

总管会话固定在本项目根目录（cwd，由 config.json 的 `hub_cwd` 决定，缺省即项目根）。
总管的人设和派单手册由用户在总管会话目录的 `AGENTS.md` 中自行配置
（本机私有文件，不入库；本项目暂不提供默认模板）。

## 文件

- `adapter.py` — FastAPI 服务，监听 `127.0.0.1:9100`，暴露 OpenAI 兼容接口
- `config.json` — Kimi Web 地址、总管会话配置（模型/权限/思考强度）、轮询参数。
  本机私有（已 gitignore），首次使用从 `config.example.json` 复制一份；
  `kimi_web_base` 的端口以你本机 Kimi Web 实际地址为准；
  `hub_cwd`（总管会话所在目录）缺省自动取项目根目录，无需填写
- `sessions.json` — 总管会话 id 的持久化（自动生成，勿手改）
- `gate.state` — 录音门控状态文件（mic-gate 原子写入，s2s 音频入口补丁读取；
  自动生成，勿手改）

## 运行

由桌面「语音助手」快捷方式一键启动，快捷方式直接指向
`.venv\Scripts\pythonw.exe` + `voice-launcher.pyw`（不经过 PowerShell，
启动时不会闪现终端窗口）。快捷方式**只负责启动**：未运行→依次启动本适配器、
speech-to-speech（`--responses_api_base_url` 指向 `http://127.0.0.1:9100/v1`）、
mic-gate；已在运行→只弹提示卡。停止走托盘右键「退出」（调
`voice-launcher.pyw --stop` 杀全部相关进程）。
`voice-assistant.ps1` 只是兼容入口，转发给 pythonw 执行。
日志在 `E:\workspace\speech-to-kimi-code\logs\voice-bridge.out.log` / `.err.log`。

手动运行（调试用）：

```bash
E:\workspace\speech-to-kimi-code\.venv\Scripts\python.exe adapter.py
```

## 关键实现细节（踩过的坑）

- **建会话时传 `agent_config` 会被服务端忽略**（返回的会话 `agent_config` 只剩
  `{"model":""}`），必须再 `POST /api/v1/sessions/{id}/profile` 才能真正设置
  model / permission_mode / thinking。可用 `GET /sessions/{id}/status` 验证生效。
- **轮询要两段式**：prompt 提交后 turn 不会立刻 start，此时 `busy=false`，
  直接判「不 busy 即完成」会竞态误报。先等 `busy||main_turn_active` 变 true，
  再等它变 false。
- **s2s 启动预热**会发一条 `Hello`，适配器对它直接返回固定文案，不打扰总管。
- Git Bash 的 `curl -d` 会损坏中文 JSON，脚本里发中文一律用 Python + httpx。

## 接口

- `POST /v1/chat/completions` — OpenAI 兼容，支持 `stream: true`（SSE）和非流式
- `GET /health` — 存活检查（含调试门控状态）
- `GET/POST /gate` — **调试保留**的适配器侧门控：`{"open": true|false}`，默认常开
  （真正的录音门控在音频入口，见下节）。关闭时语音文字直接丢弃（返回空回复，
  TTS 无声），预热 Hello 不受影响

## 录音门控（mic-gate.pyw + 音频入口补丁）

门控位置在**音频入口**：`speech-to-kimi-entry.py` 对 s2s 的
`LocalAudioStreamer.run` 做运行时猴子补丁，麦克风帧进 VAD 前检查
`voice-bridge/gate.state`——"0"（或文件不存在）喂静音帧，"1" 放行真实音频。
状态文件由 mic-gate 原子写入（mtime 缓存读取，音频回调里几乎零开销）。

默认**关门**（静音）：语音助手启动后，任何声音都进不了 VAD，包括 TTS 自己
外放的回复——从根上消除回显自问自答。两种开门方式，**都立即生效、没有
宽限期**：

- **按住说话（PTT）**：按住「右 Ctrl」说话，松开立即断音——已经在管道里的
  那句会继续识别并送达，新声音进不来
- **常听开关**：按「F9」切换开/关，同样立即生效

单实例保护：重复启动的 mic-gate 会立即退出（两个实例同时轮询会导致
一次按键触发两次切换，等于没切）。

系统托盘有状态图标（绿=收音中，红=静音），只变色、不弹通知。右键菜单只有
「退出」（直接用 pythonw 调 voice-launcher `--stop` 杀掉全部进程，
不经过 PowerShell）。按键检测用 Win32 GetAsyncKeyState 轮询（不依赖全局钩子
的事件名，避免右 Ctrl 被报成 'ctrl' 导致松开事件丢失）。按键可在
`config.json` 的 `gate_ptt_keys` / `gate_toggle_keys` 调整（键名列表，可绑
多个键，可选键名见 `mic-gate.pyw` 里的 `VK_MAP`）。

## 启动向导（voice-launcher.pyw）

桌面快捷方式 = **只启动**：未运行时双击启动，已在运行时再双击只弹
「正在运行」提示卡；**停止走托盘图标右键「退出」**（调 voice-launcher
`--stop` 杀掉全部进程并弹出同款风格的停止提示卡）。

启动时弹出浅色进度卡片（普通窗口剥掉标题栏实现，**有任务栏按钮**），
实时展示四个阶段：

1. 启动桥接适配器
2. 加载语音识别模型
3. 加载语音合成模型（耗时最长，约 1 分钟，等的是 err.log 里
   `Qwen3TTSHandler warmed up`——模型真正预热完成，而不是端口可连）
4. 连接语音总管（确认适配器 `/ready` 返回 true）

四个阶段用**竖向步骤条**展示（状态图标 + 连接线 + 阶段名）：待办=灰空心环，
进行中=旋转弧（蓝色），完成=绿底白勾，失败=红底白叉；图标全部 PIL 超采样
渲染，无锯齿。整体进度用绕麦克风图标的**进度环**展示——浅灰轨道 + 蓝色弧
从 12 点顺时针填充、末端圆点，阶段间缓动过渡不跳变。**全部就绪后卡片自动
关闭，托盘图标此时才出现**——看到托盘图标 = 可以开始按住右 Ctrl 说话了。
卡片右上角有最小化（—）和关闭（×）按钮：最小化 = 正常收到任务栏（点任务栏
图标恢复，窗口在后台继续启动）；关闭 = 杀掉已拉起的进程并退出启动（任务栏
右键「关闭窗口」同理）。单实例保护 + 10 秒防抖：重复双击不会起第二套进程。

停止时（托盘右键「退出」）会弹出同款卡片提示「语音助手已停止」，
几秒后自动消失。
