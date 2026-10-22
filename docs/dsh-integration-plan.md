# DeepSeek Harness（DSH）桌面版接入计划

日期：2026-10-22
目标执行环境：华为笔记本 Travis（`D:\workspace\MicToAgent`，DSH 桌面版 0.2.1-rc.2 装于该机的 `D:\Apps\DeepSeekHarness`，数据目录 `C:\Users\Travis\.dsh`，凭据文件已确认存在。注意：主电脑上的安装路径是 `E:\Deepseek harness`，两台机器路径不同，`dsh.process_path_hints` 各配各的）
状态：§8 第 1–7 步已于 2026-10-22 完成（适配层、router、targets/dsh.py、粘贴泛化、面板 UI、文档；192→197 个测试全绿），待端到端验收（§8 第 8 步，见该步的手动验收项标注）

> 本计划自给自足：所有外部调研结论（DSH Host API、认证机制、端口发现、
> 当前会话信号）都已核实并写死在本文里，执行者不需要重新调研 DSH 源码。
> 若实施中发现与本文不符（DSH 是 0.2.x rc，迭代快），以实际探测为准并回写本文。

## 0. 背景与结论摘要

MicToAgent 目前只对接 Kimi Web：地址栏 URL（`/sessions/<id>`）是当前选中
会话的唯一可靠信号，快速模式走 Kimi Web REST 直投（不抢焦点）。要接入
DSH 桌面版（Electron 壳），核心结论（已核实 DSH 源码，证据见 §7）：

- **投递接口存在且语义友好**：`POST /api/session/prompt`（本机 127.0.0.1
  认证 Host），提交即返回 `{accepted:true}`，服务端排队；`requestId` 服务端
  去重（同一 requestId 重复提交不会插入两遍），与「语音提交非幂等、绝不
  重发」铁律天然兼容。
- **认证可走通（灰色路径）**：无免认证 localhost 通道；但同用户进程可读
  `~/.dsh/.credentials.yaml` 里的浏览器会话 Cookie HMAC 签名密钥
  （`client-connection/browser-session`），本地自签合法 Cookie 即可以
  owner 身份调 API。官方 credentials-local README 承认同 UID 可读。
- **端口随机但可发现**：桌面版 Host 端口 OS 随机分配（不写盘），可从
  TCP 表按进程反查，或扫 loopback 401 指纹。
- **最大差异：没有地址栏**。DSH 服务端不知道用户在看哪个会话；选中状态
  只在渲染进程 localStorage（`dsh.sessions.current`，LevelDB，运行时上锁，
  不读它）。替代信号 = **前台窗口标题**：DSH 把窗口标题设为
  `会话标题 — DeepSeek Harness`，用 Win32 读标题 + `session/list` 反查
  sessionId。

## 1. 总体设计：目标适配层（隔离是硬要求）

新增 `voice-bridge/targets/` 包，把「投递目标」抽象成接口。adapter.py 不再
直接认识 Kimi Web；Kimi Web 逻辑整体迁为一个 target 实现，DSH 是第二个。
以后接第三个 agent（Trae、Cursor 之类）只加一个文件。

```
voice-bridge/targets/
├── __init__.py        # 注册表 + 目标选择路由（router）
├── base.py            # TargetAdapter 协议/基类 + 数据类型
├── kimi_web.py        # 现有逻辑的封装（行为必须零变化）
└── dsh.py             # DSH 桌面版实现
```

`base.py` 接口（命名可微调，语义不可少）：

```python
@dataclass
class ResolvedTarget:
    target_name: str          # "kimi_web" / "dsh"
    address: str              # kimi: base URL；dsh: "127.0.0.1:<port>"
    session_id: str | None    # None = 未选中会话（草稿页/未知）
    session_title: str = ""   # 面板展示用
    can_fast: bool = True     # 该目标当前可否 REST 直投

class TargetAdapter(Protocol):
    name: str
    def start(self) -> None: ...                    # 后台轮询等（kimi 需要，dsh 可空）
    def is_alive(self) -> bool: ...                 # 目标应用在不在线
    def is_foreground(self) -> bool: ...            # 用户当前是否正看着它（auto 路由用）
    def resolve(self) -> ResolvedTarget | None: ... # 同步、快（<100ms），收到文字时调用
    def submit_fast(self, target: ResolvedTarget, text: str) -> str: ...
    def paste(self, text: str, submit: bool) -> str:  # 复用 draft_input 的返回码
    def panel_state(self) -> dict: ...              # 该目标的 /v1/panel 附加字段
```

adapter.py 的 `handle_user_text` 改为：

1. 读 `target.mode`（见 §4）确定用哪个 target（auto 时按前台窗口路由）；
2. **收到文字时立即 `resolve()` 锁定目标**（铁律，现有行为保持）；
3. fast 模式调 `submit_fast`，粘贴模式调 `paste`；
4. 盯梢（watcher）保持 kimi 专用（见 §6 开放问题）。

## 2. Phase 0：探测验证（先做，不通过不往下走）

DSH 是 rc 版本，先在华为本上用独立脚本验证全部假设，再动主代码。

新增 `scripts/probe_dsh.py`（一次性诊断工具，不入主链路，跑完保留备查）：

1. 确认 DSH 桌面版进程在跑（不在跑就先启动它）；
2. 端口发现（算法见 §3.1）→ 打印 `127.0.0.1:<port>`；
3. 读 `~/.dsh/.credentials.yaml` 的 browser-session secret，自签 Cookie
   （算法见 §3.2），`POST /api/session/list` → 打印会话列表（id + 标题）；
4. 读前台 DSH 窗口标题 → 反查 sessionId → 打印命中结果；
5. 新建一个测试会话（`session/create`），post 一条「回复 ok 两个字」，
   在 DSH 窗口里肉眼确认消息到达且排队语义正常；
6. 手动验证 DSH 输入框的 Enter 语义（Enter 直接发送？Shift+Enter 换行？
   还是有设置项）和剪贴板贴图支持——记入本文 §6。

通过标准：1–5 全部成功。任何一步失败，停下来修正本文档的对应结论。

**回写（2026-10-22 实测，rc.2 本机）**：步骤 1–5 全部 PASS。实测端口
12192（OS 随机，每次启动变）；session/list 返回 3 个会话；窗口标题
`安装dsh-pet和dsh-whale-widget插件 — DeepSeek Harness` 反查命中；
session/create + session/prompt 返回 `{"accepted":true}`。两处与下文
原始结论不符，已按 rc.2 编译产物修正 §3.2（HMAC 输入）和 §3.3
（SessionSummary 字段），§6.1 随之销项一半。

## 3. Phase 1：DSH target 实现（`targets/dsh.py`）

### 3.1 端口发现（不引入 psutil，纯 ctypes）

1. `iphlpapi.GetExtendedTcpTable`（AF_INET + TCP_TABLE_OWNER_PID_LISTENER）
   枚举本机监听端口，得到 `127.0.0.1` 上的 (port, pid) 列表；
2. 对每个 pid 用 `OpenProcess` + `QueryFullProcessImageNameW` 取进程镜像路径，
   匹配安装目录（华为本是 `D:\Apps\DeepSeekHarness`，主电脑是
   `E:\Deepseek harness`；统一走可配的 `dsh.process_path_hints`）或
   进程名含 `dsh`/`DeepSeek Harness`；
3. 兜底：对候选端口逐个 `GET /`（不带凭证），响应 401 且正文为固定指纹
   `dsh web authentication required; reopen the URL printed by dsh web.`
   即为命中（证据：`packages/client/connection/src/browser-auth.ts:414-420`）；
4. 结果缓存（TTL 30s），连接失败（connection refused / 401 结构变化）时
   作废重探。配置 `dsh.port` 可手动钉死（排障用）。

**回写（2026-10-22 实测补充）**：兜底指纹扫描必须**并发 + 短超时
（1.5s）**——DSH 重启瞬间路径匹配落空时，串行 3s 超时扫几十个 loopback
端口实测拖到 ~9s。另外端口其实可以固定：桌面 host 的 webserver 是 cordis
插件，`~/.dsh/profiles/desktop/cordis.patch.yml` 加 `- id: webserver` /
`config: {host: 127.0.0.1, port: <固定端口>}` 即可覆盖 `--port 0`
（profile 级 patch 只影响桌面版；Electron 壳读实际绑定端口，不受影响）。
本机已配 58628（紧邻本机 Kimi Web 的 58627，好记）。

注意：若同一机器同时跑了 `dsh web`（3080）和桌面版，两者共享 `~/.dsh`
签名密钥但端口不同、会话写入可能互斥——按进程镜像路径区分，桌面版优先。

### 3.2 认证：自签浏览器会话 Cookie

读 `~/.dsh/.credentials.yaml`（尊重 `DSH_HOME` 环境变量覆盖）。**不引入
pyyaml**：该文件结构简单，写一个缩进感知的极简解析，只取
`records."client-connection/browser-session".payload.secret`（base64url 的
32 字节）。文件只读，绝不写；**secret 绝不进日志、不进 panel、不进提交**。

Cookie 伪造算法（证据：`browser-auth.ts:116-150, 244-290`；**已按 rc.2
编译产物 `dsh-client-connection/lib/index.js:277-578` 实测修正**）：

- authority = `127.0.0.1:<port>`（必须与请求 Host 头完全一致）；
- Cookie 名：`dsh-auth-` + base64url(SHA-256(authority))（去 `=` 填充）；
- payload JSON：`{"version":1,"authority":authority,"issuedAt":<ms>,"expiresAt":<ms>}`，
  有效期 ≤ cookieMaxAgeDays（默认 30 天，我们签 24h 即可）；
- body = base64url(payload 的 UTF-8 字节)（去填充）；
- **签名 = base64url(HMAC-SHA256(secret, body 字符串本身))**——注意输入是
  编码后的 body 文本，不是原始 JSON（服务端对 Cookie 值里的 body 段直接
  重算验签，因此 JSON 键序/空格不影响结果）；
- Cookie 值：`v1.` + body + `.` + 签名；
- 每次 resolve 前检查过期，过期重签（签名是纯计算，微秒级）。
- 时间校验：`issuedAt <= now < expiresAt`，毫秒级整数。

### 3.3 RPC 调用（统一信封）

所有 API 都是 `POST /api/<namespace>/<method>`，JSON 信封（证据：
`packages/client/connection/src/rpc-host.ts:200-235,285-345`；
`packages/api/gateway/src/index.ts:244,1250-1265`）：

```json
{
  "type": "client-request",
  "rpcId": "<任意非空字符串>",
  "method": "session/prompt",
  "payload": { "args": { "<参数名>": { ... } } }
}
```

注意 `args` 是**按参数名**编码。关键方法（证据：
`packages/api/session-controller/src/index.ts:158,275,300,461`；
类型定义 `.../types.ts:267,288,344`）：

| 操作 | 路径 | `payload.args` | 成功返回 `result.value` |
|---|---|---|---|
| 列会话 | `POST /api/session/list` | `{"_request": {}}`（可带 `cursor`） | `{"items":[SessionSummary...]}` |
| 建会话 | `POST /api/session/create` | `{"request": {"cwd"?,"workspaceId"?,"sessionId"?,"agentPreset"?}}` | `{"sessionId",...}` |
| 投消息 | `POST /api/session/prompt` | `{"request": {"requestId","sessionId","mode","content","clientTimeZone"?}}` | `{"accepted":true}` |

- `mode: "queue"`（排队，默认用这个；`"steer"` 是打断当前轮，不用）；
- `content: [{"type":"text","text": "..."}]`；
- `requestId`：每次提交新生成 `uuid4().hex`（服务端按它去重——但我们
  **依然绝不重发**，去重只是兜底保险）；
- 响应：HTTP 200 + `result.ok=true` 才算成功；`result.ok=false` 是业务错误
  （要读出错误信息弹通知）；401 = Cookie 失效/端口错了 → 作废重探一次，
  仍失败则弹通知（**不是重发**，是放弃并告知）；
- 成功响应样例：`{"type":"server-response","rpcId":"...","result":{"ok":true,"value":{"accepted":true}}}`。

**回写（rc.2 实测修正）**：

- `session/list` 的 SessionSummary **没有顶层 `id`/`title`**。字段为
  `sessionId`、`updatedAt`(ms)、`agentAvailable`、`running`、`blank`、
  `formatStatus`、`cwd`、`projections`；标题在
  `projections.values.title`（可能为空串，新会话没标题）。
- `running`（bool）即"agent 正在跑"语义，§6.1 的 DSH 盯梢 v2 可以用
  轮询 session/list 的 `running` 实现，不必碰 WebSocket。
- `/api/*` 认证失败只回 `401 unauthorized`（与 `GET /` 的指纹文案不同，
  两者都是 401 但正文不同，可作区分依据）；`Content-Type: application/json`
  必须带，否则 415。

HTTP 客户端沿用 httpx，`trust_env=False`（同 kimi 路径的代理坑）。

### 3.4 当前会话解析（窗口标题反查）

1. `GetForegroundWindow` + `GetWindowTextW` 读前台窗口标题；
2. 前台进程是 DSH（镜像路径/进程名匹配）才算「正看着 DSH」；
3. 标题剥掉后缀 ` — DeepSeek Harness`（注意是 em dash 带空格，可配
   `dsh.window_title_suffix`），得到候选会话标题；
4. 调 `session/list`，按标题精确匹配取 sessionId：
   - 唯一命中 → 用之；
   - 多个同名会话 → 弹通知提示去面板固定目标（`dsh.pinned_session_id`），
     不猜；
   - 零命中（新会话还没标题/标题被改）→ 回退 `pinned_session_id`；也没有
     → 弹通知「先在 DSH 里点一下会话，或在面板固定目标」；
5. 低频轮询（复用 follow_poll_interval_sec）刷新 session/list 缓存，
   resolve 时同步刷一次标题（毫秒级），session/list 用 ≤5s 缓存即可
   （标题→id 映射不常变）。

「收到文字时锁定目标」铁律不变：`resolve()` 在 `handle_user_text` 开头
同步完成，之后所有操作都用锁定结果。

### 3.5 投递后体验说明（写进 README，不是代码问题）

消息送达后 DSH 窗口**不会自动切到该会话**；用户正看着它时新消息实时
显示（durable user/message 事件），否则要自己点开。这与 Kimi Web 行为
一致（kimi 也不切），无需处理。

## 4. Phase 2：目标选择（router + 面板）

### 4.1 配置与运行时状态

- `config.json` / `config.example.json` 新增：

```json
"target": "auto",
"dsh": {
  "home": null,
  "port": null,
  "pinned_session_id": null,
  "window_title_suffix": " — DeepSeek Harness",
  "process_path_hints": ["Deepseek harness"]
}
```

- `target`: `"auto" | "kimi_web" | "dsh"`，默认 `auto`。
- 新增运行时文件 `voice-bridge/target.mode`（仿 `send.mode`，原子读写进
  `runtime_state.py`）：面板切换即时生效，覆盖 config 的 `target`。
- `GET/POST /v1/panel` 增加 `target` 字段（当前生效目标 + 可选列表）。

### 4.2 auto 路由规则（收到文字时判定，同步、快）

1. 前台窗口进程是 DSH → `dsh`；
2. 否则走 kimi 逻辑（前台/后台地址栏轮询，现有行为不变——包括
   Tailscale 远端标签页投远端的动态路由，完全不碰）；
3. 都不是 → 用 config 的 fallback 顺序：kimi_web 在线选 kimi，否则 dsh
   在线选 dsh，都离线弹通知。

手动模式下不看前台，直接用指定 target（`is_alive` 失败弹通知）。

## 5. Phase 3：粘贴模式泛化（`draft_input.py`）

现状：粘贴前用 `foreground_kimi_state(port)` 判定前台是 Kimi Web（读浏览器
地址栏）。改造：

- 前台判定抽进 target 接口（`is_foreground`）；kimi 实现不变，dsh 实现 =
  前台进程是 DSH；
- `_composer_focused` 的 UIA 焦点判定逻辑（`is_composer_focus`）是通用的
  （Edit/Document/Custom 控件启发式），直接复用；`_TOOLBAR_PX=90` 和 omnibox
  关键词是浏览器特异的，dsh 路径跳过 omnibox 检查即可（Electron 无地址栏）；
- DSH 输入框 Enter 语义由 Phase 0 第 6 步确认：若 Enter 不是发送键，
  `submit=True` 路径改为发送该应用实际的发送键（可配置
  `dsh.submit_key`，默认 enter）；
- 贴图：DSH 桌面版官方支持剪贴板贴图（dsh-desktop 特性列表），
  现有「剪贴板有图先贴图」逻辑理论上直接可用，Phase 0 验证后确认。

## 6. 开放问题与风险（执行中逐项销项）

1. **DSH 盯梢**：kimi 的 watcher 依赖 `GET /sessions/{id}` 的
   busy/pending_interaction 字段；DSH 侧对应物是 WebSocket
   `/api/remote.mux` 的 `session/follow` 流，或 session/list 的
   **`running` 字段（rc.2 实测存在，bool）**。**v1 不做 DSH 盯梢**
   （拍板通知缺失，用户自己看 DSH 窗口），v2 优先评估轮询
   session/list 的 `running`（比 WebSocket 简单）。
2. **内部 API 稳定性**：`/api/session/*` 是应用内部 RPC 非公开 API，
   DSH 升级可能改端点/参数名。隔离方式：所有 DSH API 细节只许出现在
   `targets/dsh.py`；版本适配失败时优雅降级（弹通知「DSH 接口不匹配，
   请更新 MicToAgent」），绝不静默丢字。
3. **Cookie 路径依赖**：依赖「`~/.dsh/.credentials.yaml` 同用户可读」
   的现状；若 DSH 改用 OS keychain 存储，fast 模式失效 → 自动降级为
   粘贴模式并通知（代码里要留这个降级分支）。
4. **同名会话**：标题反查有歧义风险（§3.4 第 4 步），用 pinned 配置兜底。
5. **DSH_HOME 覆盖**：桌面版默认 `~/.dsh`；若用户设了 `DSH_HOME` 或用的
   是社区版（`~/.dsh-desktop`），`dsh.home` 配置项兜底。
6. **多实例**：`dsh web` 与桌面版并存时按进程镜像路径挑端口（§3.1）。
7. **安全边界**：secret/会话内容不写日志；`.credentials.yaml` 只读；
   不向任何非 127.0.0.1 地址发带 Cookie 的请求（authority 强校验）。
8. **华为本现状**：该机 `config.json` 的 `kimi_web_base` 与 `.env` 的
   `KIMI_WEB_BASE` 都指向主电脑 Tailscale 地址（远程模式）。接 DSH 后
   auto 路由仍兼容：看 DSH 窗口投本地 DSH，看浏览器 kimi 标签投远端
   Kimi Web，互不干扰。新增配置键要在华为本的 `config.json` 同步加上。
9. **代码页坑**：华为本系统代码页 GBK——新增/修改的 `.py` 全部 UTF-8
   无 BOM（Python 无所谓），但若产出 `.ps1` 必须 UTF-8 BOM；日志文件
   读写保持 utf-8 显式指定（现有代码风格已是）。

## 7. 证据索引（DSH 源码，GitHub deepseek-ai/deepseek-harness @ master）

- 桌面架构/Electron 转发/随机端口：`apps/desktop/README.md`（"Electron
  forwards application HTTP requests to the authenticated Web Host…
  Desktop listens on an OS-assigned port"）
- 通用 HTTP 层无全局认证：`packages/host/webserver/src/index.ts`（"No
  server-wide authentication"）
- `/api` 路由与信封：`packages/client/connection/src/rpc-host.ts:200-235,
  285-345`；单 args 限制 `packages/api/gateway/src/index.ts:244,1250-1265`
- session 端点：`packages/api/session-controller/src/index.ts:158,275,300,461`；
  类型 `.../types.ts:267,288,344`；排队语义 `.../commands.ts:319-355`
  （`followup()` 入队即返回；`:321` requestId 去重）
- 真实 HTTP 调用样例：`apps/web/tests/smoke-real.e2e.ts` 的 `remoteRpc()`
- 认证与 Cookie 格式：`packages/client/connection/src/browser-auth.ts:14,
  61,116-150,182-199,244-290,262-336,414-420`
- 桌面版 Cookie 由 Electron 主进程持有、不落盘：
  `apps/desktop/src/web-document.ts:58,77`
- 桌面 Host 启动参数（`--no-open --host 127.0.0.1 --port 0`）：
  `apps/desktop-host/src/index.ts:30`
- 窗口标题 = `会话标题 — 产品名`：
  `packages/client/ui-layout/src/client/DocumentTitle.tsx`
- 当前会话只在 localStorage（`dsh.sessions.current`）：
  `packages/client/ui-workspace/src/client/navigation.ts:159-161`；
  Host 无「当前会话」概念：
  `.agents/notes/implemented/architecture/2026-09-15-client-session-references.md:76`

## 8. 实施步骤（顺序执行，每步可独立验证）

1. **Phase 0**：`scripts/probe_dsh.py` 在华为本跑通 §2 全部 6 步。
2. **适配层落地**：新建 `targets/`，把 adapter.py 的 kimi 逻辑迁进
   `targets/kimi_web.py`（纯搬运，行为零变化）；跑 `pytest tests/ -q`
   全绿 + 手工语音冒烟（kimi 三种模式各一句）。
3. **router**：`targets/__init__.py` + `target.mode` 运行时文件 +
   `/v1/panel` 扩展；默认强制 `kimi_web`（行为不变），auto 先写好但
   配置默认值在 DSH 验证完再切。
4. **`targets/dsh.py`**：端口发现 → Cookie → session/list → 标题反查 →
   session/prompt；单元测试 mock httpx（见 §9）。
5. **粘贴泛化**：`draft_input.py` 前台判定抽象 + DSH submit 键。
6. **面板 UI**：`panel-ui/` 主页加目标指示/切换（跟随 cir-tabs 风格），
   `npm run build` 重建 dist（华为本有 Node.js，可就地构建）。
7. **文档**：`voice-bridge/README.md`（新 target、配置键、风险）、根
   `AGENTS.md`（进程契约不变，补 target.mode 一条）、`config.example.json`。
8. **端到端验收**（华为本）：auto 模式下，看 DSH 窗口说一句 → 进 DSH
   当前会话（fast，无焦点直投）；切到浏览器 kimi 标签说一句 → 进远端
   Kimi Web；dialog/compose 模式各验一句；同名会话/未选会话的通知路径
   各验一次。

   **必须由用户手动完成的验收项**（代码侧无法自验）：
   - 手工语音冒烟：kimi 三种发送模式各一句（§8 第 2 步的遗留项）
   - 面板渲染确认：「投递目标」选择器在 400×632 窗口内的排版、
     悬停气泡、auto 模式「现在投给谁」指示
   - DSH 输入框 Enter 语义（Phase 0 第 6 步遗留）：Enter 是否直接发送；
     不是则改 `dsh.submit_key`（如 `ctrl+enter`）
   - 剪贴板贴图进 DSH 输入框验证（dialog 模式带图说一句）

## 9. 测试计划（沿用 tests/ 的 sys.path 直插风格）

- `tests/test_dsh_cookie.py`：固定 secret + authority 构造 Cookie，
  验证名字（sha256）、值格式（`v1.<payload>.<hmac>`）、过期重签；
  用 hmac/hashlib 独立复算比对。
- `tests/test_dsh_rpc.py`：信封结构、`args` 按参数名编码、
  `result.ok=false` 业务错误解析、401 处理路径（mock httpx transport）。
- `tests/test_dsh_resolve.py`：标题剥后缀（含无后缀/自定义后缀）、
  session/list 匹配（唯一/重名/零命中/pinned 回退）。
- `tests/test_target_router.py`：auto 路由三分支 + 手动模式覆盖。
- 现有测试必须保持全绿（尤其 `test_send_routing.py`、
  `test_target_resolver.py`——kimi 行为零回归）。

## 10. 铁律对照（自查清单）

- [ ] 任何一层无超时重发（dsh 路径同样一次请求一次发送）
- [ ] requestId 每次提交新生成（不重用，去重只是服务端保险）
- [ ] 收到文字时同步锁定目标，后续不重新解析
- [ ] 识别/投递异常按通知 + 空结果回流，线程不死
- [ ] secret 不进日志/提交/panel；`.credentials.yaml` 只读
- [ ] 进程间契约不变（gate.state/send.mode/gate.cmd/panel.cmd/stop.cmd/
      日志锚点 `[stt] loading engine=` / `[stt] engine ready`）
- [ ] 新增 `target.mode` 契约同步写进根 AGENTS.md
