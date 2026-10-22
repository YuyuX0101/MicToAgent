# voice-bridge — 语音接入 Kimi Web 的桥接适配器

把 voice-core 语音核心和 Kimi Web 会话连起来的传声筒。
适配器本身不做对话，只做转发、轮询和状态管理。

## 架构

```
    你说话 → voice-core（ten-vad 分段 + SenseVoice 初稿 / qwen3-asr 精修接力识别 + 热词纠错）
       → POST http://127.0.0.1:9100/v1/chat/completions   ← 本适配器
       → router 选定投递目标（targets/ 适配层，见下）
       → 按发送模式转发给目标（Kimi Web 地址见 config.json 的 kimi_web_base；
         DSH 桌面版自动端口发现）
         · 快速发送：REST 进当前选中会话（无法配图；不抢焦点）
         · 对话框发送 / 输入对话框：粘贴进输入框（可带剪贴板里的图；
           不切窗口、不代点输入框；前台不是目标应用或焦点不在输入框
           则弹 Windows 通知）
         · 新建会话草稿页没有会话 id，快速发送会降级为对话框发送
       → 回复在目标界面里看（语音只进不出，无语音合成）；
         桥接级问题弹 Windows 通知
```

语音与打字完全等价：识别出的文字**原文转发**，不加任何前缀或修饰。
提交即返回（不等目标会话跑完，kimi web 服务端自己对 busy 会话排队，
界面上立刻能看到）；后台盯梢任务（每会话去重，只有一个）在需要人工
拍板时弹一次 Windows 通知。

**新建会话草稿页**：kimi web 的会话是懒创建的——点「新建会话」只是前端
草稿页，发出第一条消息后服务端才建会话、地址栏才有 `/sessions/<id>`。
没有会话 id 时 REST 建不了会话，快速发送会降级为对话框发送：在用户已
把焦点放在输入框的前提下粘贴+回车，让网页自己建会话（工作区/模型/权限
原样生效）。不激活浏览器、不代点输入框；前台不是 Kimi Web 或焦点不在
输入框则弹通知，不重试。

## 文件

- `adapter.py` — FastAPI 服务，监听 `127.0.0.1:9100`，暴露 OpenAI 兼容接口和 `/v1/panel`
- `targets/` — 投递目标适配层（详见下文「投递目标」一节）：`base.py` 协议、
  `kimi_web.py` 现有 Kimi Web 逻辑、`dsh.py` DSH 桌面版、`__init__.py` 注册表 + router
- `panel.pyw` — 控制面板：pywebview 壳（无边框直角窗 + DWM 投影，WebView2 渲染），
  UI 是 `panel-ui/`（React + Tailwind，cir-tabs 风格），构建产物
  `panel-ui/dist` 入库、运行时 file:// 直载；改 UI 后在 `panel-ui` 里
  `npm run build` 重建。运行时切换发送模式与门控；齿轮进设置；X 默认回托盘
- `runtime_state.py` — `send.mode` / `target.mode` / `gate.cmd` / `panel.cmd` 原子读写
- `target_resolver.py` — 目标解析器：Win32 EnumWindows 找 Edge/Chrome 顶层
  窗口，UIA 读地址栏，三态分类：会话页（`localhost:<port>/sessions/<id>`）/
  草稿页（在 kimi web 但没有 id）/ 不在 kimi web。后台线程低频轮询
  （默认 0.7s）记日志和兜底；发送语音文字前同步刷新一次（实测几十毫秒），
  保证刚切换会话就说话也进对会话。另提供 `foreground_kimi_state`：只看
  前台窗口，不激活。
  UIA 不可用（非 Windows / 未装 `uiautomation` 包）时一直返回 None，
  适配器会弹 Windows 通知提醒先选中会话。
  地址栏匹配默认认 `localhost/127.0.0.1:<port>`，`kimi_web_base` 自身
  自动注册为别名；更多私有地址可配 `kimi_web_aliases`（host:port 列表）。
  **多机互通**：REST 投递按地址栏里的 authority 动态路由——浏览器前台
  标签页是本机 kimi web 就投本机，是 Tailscale/局域网地址就投对应机器，
  双标签页随意切换。要投到对端机器，把它的 `http://<ip>:<port>` 加进
  `kimi_web_aliases`（`kimi_web_base` 已是该地址时无需再配）。
  注意 `.env` 的 `KIMI_WEB_BASE` 优先级高于 config.json 的
  `kimi_web_base`——改 base 时两处要一起改，否则 config 改了不生效
- `session_info.py` — 会话名/工作区名解析：地址栏只有会话 id，展示名读
  kimi CLI 本地数据（`session_index.jsonl` → `state.json` 的 title，
  `workspaces.json` 的 name）；只读，面板轮询时改名秒级同步
- `draft_input.py` — 粘贴进输入框：不抢焦点、不代选输入框。前台是 Kimi Web
  且焦点在输入框时，若剪贴板有图则先贴图再贴文字，按模式决定是否回车，
  然后尽量恢复剪贴板。中文不能走模拟按键（输入法组词不可控）。
- `config.json` — Kimi Web 地址、盯梢轮询参数。
  本机私有（已 gitignore），首次使用从 `config.example.json` 复制一份；
  `kimi_web_base` 的端口以你本机 Kimi Web 实际地址为准
- `gate.state` — 录音门控状态文件（mic-gate 原子写入，voice-core 音频回调读取；
  自动生成，勿手改）
- `send.mode` — 发送模式：`fast` / `dialog` / `compose`（本机运行产物，gitignore）
- `target.mode` — 投递目标：`auto` / `kimi_web` / `dsh`（面板切换即时生效，
  覆盖 config.json 的 `target`；本机运行产物，gitignore）
- `hotwords.txt` — 语音识别热词表，本机私有（已 gitignore），
  不存在就直接新建，一行一词。解决同音误听（「会话」被听成「绘画」）
  和英文词乱码（「kimi code」→「chemicde」）。
  格式：每行 `目标词`（拼音同音纠正）或 `错误词=>目标词`（显式替换），
  详见文件头注释。支持热更新：保存后下一句识别就按新词表纠错，不用重启语音助手

## 运行

由桌面「MicToAgent」快捷方式一键启动（仓库根执行
`scripts/create-desktop-shortcut.ps1`），快捷方式直接指向
`.venv\Scripts\pythonw.exe` + `voice-launcher.pyw`（不经过 PowerShell，
启动时不会闪现终端窗口）。快捷方式**只负责启动**：未运行→依次启动本适配器、
voice-core、mic-gate、控制面板；已在运行→打开控制面板。停止走托盘右键
「退出」，或在面板设置里把关闭窗口设为退出（调 `voice-launcher.pyw --stop` 杀全部相关进程）。
`voice-assistant.ps1` 只是兼容入口，转发给 pythonw 执行。
日志在仓库根目录 `logs/voice-bridge.out.log` / `.err.log`。
本机路径可写在仓库根 `.env`（见 `.env.example`），不设则按脚本位置解析。

手动运行（调试用，在仓库根目录）：

```powershell
.\.venv\Scripts\python.exe voice-bridge\adapter.py
```

## 投递目标（targets/ 适配层）

adapter.py 不直接认识具体目标应用：投递目标抽象成 `targets/base.py` 的
`TargetAdapter` 协议（`resolve` / `submit_fast` / `paste` / `is_foreground` /
`panel_state` 等），每个目标一个实现文件——`kimi_web.py` 是现有 Kimi Web
逻辑的封装，`dsh.py` 是 DSH 桌面版；接第三个 agent（Trae、Cursor 之类）
只加一个文件。`targets/__init__.py` 的 router 负责选定目标：

- `target.mode` 运行时文件（面板切换，即时生效）覆盖 config.json 的
  `target`；都未设置时默认 `kimi_web`（行为与接入 DSH 前一致）
- 手动模式（`kimi_web` / `dsh`）：不看前台，直接用指定目标，离线弹通知
- `auto`：前台窗口是 DSH → 投 DSH；否则走 Kimi 现有逻辑（含 Tailscale
  远端标签页动态路由，不碰）；kimi 离线而 DSH 在线时改投 DSH

「收到文字时立即 resolve() 锁定目标」的铁律对所有目标生效；盯梢
（watcher）只挂 kimi——DSH 提交后不盯梢（v1 决策；SessionSummary 有
`running` 布尔字段，v2 可用轮询 session/list 实现盯梢）。

### DSH 桌面版（targets/dsh.py）

DSH 桌面版是 Electron 壳，本机认证 Host 端口 OS 随机分配、没有地址栏，
接入机制与 Kimi Web 完全不同：

- **端口发现**：iphlpapi TCP 表枚举 127.0.0.1 监听端口，按进程镜像路径
  反查（`dsh.process_path_hints`，桌面版优先于 `dsh web` 的 3080）；
  兜底**并发**扫 401 指纹（`dsh web authentication required`，单口超时
  1.5s——串行 3s 扫几十个端口会在 DSH 重启瞬间拖到近十秒，实测事故）。
  结果缓存 30s，连接失败/401 作废重探；`dsh.port` 可手动钉死（排障用）。
  **固定端口**：桌面版默认 `--port 0`（OS 随机），可在
  `~/.dsh/profiles/desktop/cordis.patch.yml` 加
  `- id: webserver` / `config: {host: 127.0.0.1, port: 58628}` 钉死
  （patch 按 id 整行替换 config；profile 级只对桌面版生效，不影响
  `dsh web`/headless；Electron 壳读的是实际绑定端口，钉死不影响壳）。
  钉死后 MicToAgent 侧无需改配置，进程路径反查会直接找到它
- **认证**：读 `~/.dsh/.credentials.yaml` 的 browser-session secret
  （`dsh.home` / `DSH_HOME` 可覆盖；文件只读，极简缩进解析，不引 pyyaml），
  本地自签浏览器会话 Cookie（HMAC-SHA256 签的是 base64url 编码后的 body
  字符串）。**secret 绝不进日志/通知/面板**；带 Cookie 的请求只发
  127.0.0.1（authority 强校验）
- **当前会话**：DSH 服务端不知道用户在看哪个会话，用前台窗口标题反查——
  剥掉 `dsh.window_title_suffix`（默认 ` — DeepSeek Harness`）得到标题，
  在 session/list 缓存（≤5s，后台低频轮询刷新）里精确匹配：唯一命中用之；
  多个同名不猜，弹通知提示固定 `dsh.pinned_session_id`；零命中回退
  pinned，也没有则弹通知「先在 DSH 里点一下会话，或在面板固定目标」
- **投递**：`POST /api/session/prompt`（`mode: "queue"`，服务端排队），
  一次请求一次发送、绝不重发（`requestId` 每次新生成，服务端去重只是
  保险）；HTTP 200 + `result.ok=true` 才算成功，`ok=false` 读出业务错误
  弹通知；401 作废端口缓存后放弃并通知（不是重发）
- **投递后体验**：消息送达后 DSH 窗口**不会自动切到该会话**；用户正看着
  它时新消息实时显示，否则要自己点开（与 Kimi Web 行为一致，无需处理）
- **粘贴模式**：复用 draft_input 泛化主流程（剪贴板有图先贴图——DSH
  桌面版官方支持贴图），跳过浏览器特异的 omnibox 检查；发送键可配
  `dsh.submit_key`（默认 `enter`，可选 `ctrl+enter` / `shift+enter`）

新增配置键（`config.json`；`config.example.json` 是无注释模板，说明以本节为准）：

- `target`：`auto` / `kimi_web` / `dsh`，默认未设置时按 `kimi_web`
- `dsh.home`：DSH 数据目录（默认 `~/.dsh`，尊重 `DSH_HOME` 环境变量）
- `dsh.port`：手动钉死 Host 端口（默认 null = 自动发现）
- `dsh.pinned_session_id`：固定目标会话 id（标题反查失败的兜底）
- `dsh.window_title_suffix`：窗口标题后缀（默认 ` — DeepSeek Harness`）
- `dsh.process_path_hints`：进程镜像路径匹配片段列表（两台机器安装路径
  不同，各配各的）
- `dsh.submit_key`：粘贴模式的发送键（默认 `enter`）

**风险与降级**：`/api/session/*` 是 DSH 内部 RPC 非公开 API，升级可能改
端点/参数——所有 DSH API 细节只出现在 `targets/dsh.py`，适配失败时弹通知
（「DSH 没有收下这句话…」），绝不静默丢字。若 DSH 改用 OS keychain 存
凭据导致读不到 secret，fast 模式失效，改用粘贴模式（对话框发送/输入对话框
不受影响）。同名会话用 `dsh.pinned_session_id` 兜底。`dsh web`（3080）与
桌面版并存时按进程镜像路径区分，桌面版优先。

## 关键实现细节（踩过的坑）

- **依赖**：跟随选中会话需要 `uiautomation`，系统通知需要 `windows-toasts`
  （`pip install uiautomation windows-toasts`，仅 Windows）。
  读地址栏本身很快（实测约 25ms），慢的是用 UIA 遍历顶层窗口（十几秒），
  所以窗口枚举走纯 Win32 `EnumWindows`（毫秒级），只有命中的浏览器窗口才进 UIA。
  备选方案我们都验过：Kimi Web 的 `/api/v1/connections` 只给扁平订阅列表，
  分不清"正在看哪个"；websocket subscribe_v2 的 transcript 流服务端不对外暴露；
  `gui/store` 前端没在用。地址栏 URL 是唯一可靠信号。
- **建会话时传 `agent_config` 会被服务端忽略**（返回的会话 `agent_config` 只剩
  `{"model":""}`），必须再 `POST /api/v1/sessions/{id}/profile` 才能真正设置
  model / permission_mode / thinking。可用 `GET /sessions/{id}/status` 验证生效。
- **盯梢轮询要防竞态**：prompt 提交后 turn 不会立刻 start，此时 `busy=false`，
  直接判「不 busy 即完成」会竞态误报。盯梢任务先等 `busy||main_turn_active`
  变 true（给 120s 排队时间），再等它变 false。盯梢按会话去重：同一会话
  只有一个盯梢任务，连续说多句话时靠 `_submit_seq` 计数判断队列里还有没有
  新指令，拍板通知同一次只弹一回。
- **voice-core 启动预热**会发一条 `Hello`，适配器对它直接返回固定文案，不转发给目标会话。
- **语音提交非幂等，绝不能重发**：voice-core 的发送线程一次请求一次发送、
  超时不重发——早先适配器等目标会话跑完任务才返回，默认 20s 读超时必然
  中途断连，客户端超时重试会把同一句话重复发给目标会话（实测一句话发 3 次）。
  现在适配器已改为提交即返回（默认 20s 超时足够），重发逻辑在任何一层都
  不能加回来——任何网络抖动下的重试都会造成重复发送。
- Git Bash 的 `curl -d` 会损坏中文 JSON，脚本里发中文一律用 Python + httpx。

## 接口

- `POST /v1/chat/completions` — OpenAI 兼容，支持 `stream: true`（SSE）和非流式
- `GET /health` — 存活检查
- `GET /ready` — 端到端就绪（voice-core 预热文本已到达）
- `GET /v1/panel` / `POST /v1/panel` — 控制面板：发送模式、投递目标、门控、当前会话；POST `{"send_mode":"dialog"}` 或 `{"target":"auto"}` 或 `{"mute":true}`

## 录音门控（mic-gate.pyw + voice-core 音频回调）

门控位置在**音频入口**：voice-core 的音频回调在麦克风帧进 VAD 前检查
`voice-bridge/gate.state`——"0"（或文件不存在）直接丢弃帧，"1"（常听）和
"2"（按住说话）放行真实音频。状态文件由 mic-gate 原子写入
（mtime 缓存读取，音频回调里几乎零开销）。

默认**关门**（静音）：语音助手启动后，任何声音都进不了 VAD——环境音、
聊天声都不会被误当成指令。一个「右 Ctrl」两种用法，**都立即生效**：

- **点按 = 常听开关**：按一下开、再按一下关。常听模式按收音节奏切段
  提交（停顿 300ms 软切送识别，宽限从话音结束算起共 1.2 秒——即软切后
  再静默约 0.9 秒没开口就提交；宽限内继续说算同一轮，片段按序拼接成一条）；
  **关门时若还有在途语音或等待提交的回合，立即切段并提交发送**——
  关门 = "我说完了，马上发"
- **长按 = 按住说话**：按住期间照常软切送识别（说 30 秒也只需等最后一个
  片段的识别耗时）但**不提交**，中间停顿再久、想一会儿再说也全部收录；
  松开立即断音，按住期间说的所有话作为**一整轮**立即处理提交（跳过
  宽限等待）。正在常听中长按 右 Ctrl，松开后**连常听一起关掉**并
  立即提交——说完即收工

识别走**双引擎接力**（`stt_engine="dual"` 默认）：每个软切片段先送
SenseVoice 出初稿（快，音落字出），继续说就给前序片段排 qwen3-asr 精修
覆盖暂存文本；提交只等初稿不等精修，最后一片永远是 SenseVoice 初稿。
完整机制（切段/宽限/屏障/线程模型/契约）见 `voice-core.py` 文件头注释，
那是权威描述。

同一个键同时配在 `gate_ptt_keys` 和 `gate_toggle_keys` 里就是这种双模键
（点按/长按以 0.4 秒分界）；只配在其中一个列表里的键保持单一功能。

单实例保护：重复启动的 mic-gate 会立即退出（两个实例同时轮询会导致
一次按键触发两次切换，等于没切）。

系统托盘有状态图标（绿=收音中，红=静音，预渲染双态图标由
`scripts/build_icon.py` 生成），只变图、不弹通知。开门且 VAD 检出真实
语音时图标播放说话动画（ping-pong 循环 `tray_talk_*` 帧；信号 = voice-core
维护的 `speak.state` mtime 新鲜度，说话一停自动回静态图标）。
左键打开控制面板；右键「打开控制面板 / 退出」（退出用 pythonw 调
voice-launcher `--stop` 杀掉全部进程，不经过 PowerShell）。面板上的静音
走 `gate.cmd`，由 mic-gate 自己改内部状态再写 `gate.state`，不要直接改
状态文件。按键检测用 Win32 GetAsyncKeyState 轮询（不依赖全局钩子
的事件名，避免按键名在不同键盘/驱动下不稳定导致松开事件丢失）。按键可在
`config.json` 的 `gate_ptt_keys` / `gate_toggle_keys` 调整（键名列表，可绑
多个键，可选键名见 `mic-gate.pyw` 里的 `VK_MAP`）。

## 控制面板（panel.pyw）

启动就绪后弹出。窗口是 WebView2（pywebview 壳），UI 为 React + Tailwind
（`panel-ui/`， cir-tabs 风格：云白卡片、墨色选中块、扁平无阴影）；
主页合并麦克风门控与三种发送方式（分段选择器切换，悬停出说明气泡）；
左下角齿轮进设置（开机自启、静默启动、启动进度卡、关闭窗口是回托盘还是退出）。
「静默启动」只是不弹面板；再关掉「启动进度卡」才是完全静默（启动失败时
进度卡仍会弹出报错，不会无声无息失败）。
X 默认隐藏到托盘；托盘左键会把已打开的面板提到最前。真正退出走托盘右键，或把关闭行为改成「退出进程」。

前端与 Python 壳之间走 pywebview js_api（状态轮询、模式/门控/偏好切换全部
由 Python 中转）；Python 再调适配器 `/v1/panel`，适配器不在线时回落读
状态文件。改 UI：`cd panel-ui && npm install && npm run build`，`dist/`
直接入库，运行面板的机器不需要 Node。

三种发送方式：**快速发送**（REST，无法配图）、**对话框发送**（粘贴+回车，
可配图）、**输入对话框**（只粘贴不回车）。写 `send.mode`，下一句生效。
粘贴两种都要求你自己把目标应用放在前台并点好输入框。
主页另有「投递目标」选择器（自动 / Kimi Web / DSH，写 `target.mode`），
自动模式下选择器下方实时显示「现在投给谁」。

## 启动向导（voice-launcher.pyw）

桌面快捷方式 = **只启动**：未运行时双击启动，已在运行时再双击打开控制面板；
**停止走托盘右键**（或面板设置里把关闭窗口设为退出；调 voice-launcher `--stop` 杀掉全部进程）。

启动时弹出进度卡片（pywebview/WebView2，与控制面板同一套 React+Tailwind
cir-tabs UI；页面是 panel-ui 的第二个入口 `launcher.html`，产物随 dist 入库），
实时展示四个阶段：

1. 启动桥接适配器
2. 加载运行依赖库（sherpa-onnx 导入 + 开始加载模型，等 `[stt] loading engine`）
3. 加载语音识别模型（等 err.log 里 `[stt] engine ready`）
4. 连接目标会话（确认适配器 `/ready` 返回 true）

四个阶段用**横向阶段点 + 短标签**展示（待办=灰点，进行中=墨点呼吸脉冲，
完成=实心墨点，失败=红点），当前阶段全名显示在状态行。整体进度用节点
下方的**线性轨道**展示——浅灰轨道 + 墨色填充，阶段间缓动过渡不跳变。
**全部就绪后卡片自动关闭，并拉起托盘和控制面板**。
卡片右上角有最小化（—）和关闭（×）按钮：最小化 = 正常收到任务栏（点任务栏
图标恢复，窗口在后台继续启动）；关闭 = 杀掉已拉起的进程并退出启动（任务栏
右键「关闭窗口」同理）。单实例保护：启动过程中重复双击不会起
第二套进程。

停止时（托盘右键「退出」，或面板把关闭窗口设为退出）直接结束全部进程，不弹提示窗。
