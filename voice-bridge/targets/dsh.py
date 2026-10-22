"""DSH 桌面版投递目标（Electron 壳，本机认证 Host）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

实现 dsh-integration-plan.md §3（已按 rc.2 实测修正）：
- 端口发现：iphlpapi TCP 表按进程镜像路径反查（桌面版优先于 `dsh web`），
  401 指纹兜底，缓存 30s，连接失败作废重探；`dsh.port` 可手动钉死。
- 认证：读 ~/.dsh/.credentials.yaml 的 browser-session secret（只读，
  绝不进日志/panel），自签 Cookie；HMAC 输入是 base64url 编码后的 body
  字符串（不是原始 JSON）；带 Cookie 的请求只发 127.0.0.1（authority 强校验）。
- 当前会话：前台窗口标题剥掉 `dsh.window_title_suffix` 后在 session/list
  缓存（后台低频轮询刷新）里精确匹配；重名/零命中回退
  `dsh.pinned_session_id` 或弹通知，不猜。
- 投递：POST /api/session/prompt（mode=queue），一次请求一次发送，绝不重发；
  requestId 每次新生成（服务端去重只是保险）；401 作废端口缓存后放弃并通知；
  result.ok=false 读出业务错误弹通知。

安全约定：secret 绝不打印、不写日志、不进 panel；.credentials.yaml 只读。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import wintypes
from pathlib import Path

import httpx

from targets.base import ResolvedTarget

logger = logging.getLogger("voice-bridge.dsh")

WINDOW_TITLE_SUFFIX = " — DeepSeek Harness"
AUTH_401_FINGERPRINT = "dsh web authentication required"
_DEFAULT_PROCESS_HINTS = ["deepseek harness", "dsh"]

_PORT_CACHE_TTL_SEC = 30
_SESSION_CACHE_TTL_SEC = 5
_COOKIE_TTL_MS = 24 * 3600 * 1000

TCP_TABLE_OWNER_PID_LISTENER = 5
_AF_INET = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_HAS_WIN = hasattr(ctypes, "windll")


class DshAuthError(RuntimeError):
    """401：Cookie 失效或端口错了。"""


class DshBusinessError(RuntimeError):
    """result.ok=false：服务端业务错误。"""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------- 端口发现

class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


def _list_loopback_listeners() -> list[tuple[int, int]]:
    """返回 [(port, pid)]，仅 127.0.0.1 上的 LISTEN。"""
    if not _HAS_WIN:
        return []
    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(
        None, ctypes.byref(size), False, _AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(
            buf, ctypes.byref(size), False,
            _AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    num = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    rows = ctypes.cast(
        ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
        ctypes.POINTER(_MIB_TCPROW_OWNER_PID * num),
    ).contents
    out = []
    for row in rows:
        # dwLocalAddr 是网络字节序；127.0.0.1 = 0x0100007F
        if row.dwLocalAddr != 0x0100007F:
            continue
        port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        out.append((port, row.dwOwningPid))
    return out


def _process_image_path(pid: int) -> str:
    if not _HAS_WIN:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
        size = wintypes.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _matches_dsh_process(image_path: str, hints: list[str]) -> bool:
    """进程镜像路径命中配置 hint，或可执行文件名含 dsh / deepseek。"""
    low = (image_path or "").lower()
    if not low:
        return False
    exe = low.rsplit("\\", 1)[-1]
    return (
        any(str(h).lower() in low for h in hints)
        or "dsh" in exe
        or "deepseek" in exe
    )


def _port_has_fingerprint(port: int) -> bool:
    """未认证 GET / 回 401 且正文带固定指纹即为 DSH Host。"""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.5, trust_env=False)
    except httpx.HTTPError:
        return False
    return resp.status_code == 401 and AUTH_401_FINGERPRINT in resp.text


def discover_port(hints: list[str]) -> int | None:
    """按进程镜像路径反查（桌面版优先于 `dsh web`），401 指纹兜底。
    并发探测 + 命中即返回：DSH 进程持有多个 loopback 监听（IPC 管道口不
    应 HTTP），按顺序等结果会被排在前面的哑巴端口白等满超时（实测 2.3s）；
    DSH 重启瞬间路径匹配落空时同理（曾串行拖到近十秒）。"""
    listeners = _list_loopback_listeners()
    candidates = [
        port for port, pid in listeners
        if _matches_dsh_process(_process_image_path(pid), hints)
    ]
    if not candidates:
        candidates = [port for port, _ in listeners]
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        return candidates[0] if _port_has_fingerprint(candidates[0]) else None
    pool = ThreadPoolExecutor(max_workers=16)
    try:
        futures = {pool.submit(_port_has_fingerprint, p): p for p in candidates}
        for fut in as_completed(futures):
            if fut.result():
                return futures[fut]
        return None
    finally:
        # 不等哑端口的在途探测（至多挂 1.5s 自行结束）
        pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- 凭据与 Cookie

def _dsh_home(configured: str | None) -> Path:
    if configured:
        return Path(configured)
    env = os.environ.get("DSH_HOME")
    if env:
        return Path(env)
    return Path.home() / ".dsh"


def read_browser_session_secret(home: str | None = None) -> bytes | None:
    """极简缩进感知解析，只取
    records."client-connection/browser-session".payload.secret（base64url 32 字节）。
    文件只读，secret 绝不进日志。"""
    path = _dsh_home(home) / ".credentials.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_records = False
    in_target = False
    for line in lines:
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_records = stripped == "records:"
            in_target = False
            continue
        if in_records and indent == 2:
            in_target = stripped.rstrip(":") == "client-connection/browser-session"
            continue
        if in_target and indent >= 4 and stripped.startswith("secret:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            try:
                return _b64url_decode(value)
            except Exception:
                return None
    return None


def sign_cookie(secret: bytes, authority: str, now_ms: int | None = None) -> tuple[str, str, int]:
    """返回 (cookie 名, cookie 值, expiresAt 毫秒)。
    HMAC 输入是 base64url 编码后的 body 字符串，不是原始 JSON。"""
    name = "dsh-auth-" + _b64url_encode(hashlib.sha256(authority.encode()).digest())
    now = int(time.time() * 1000) if now_ms is None else now_ms
    expires = now + _COOKIE_TTL_MS
    payload = json.dumps({
        "version": 1,
        "authority": authority,
        "issuedAt": now,
        "expiresAt": expires,
    }, separators=(",", ":"))
    body = _b64url_encode(payload.encode())
    sig = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    return name, f"v1.{body}.{_b64url_encode(sig)}", expires


class _CookieSigner:
    """按 authority 缓存签名，过期重签（签名是纯计算，微秒级）。"""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret
        self._cached: tuple[str, str, int] | None = None

    def cookie(self, authority: str, now_ms: int | None = None) -> tuple[str, str]:
        now = int(time.time() * 1000) if now_ms is None else now_ms
        expected = "dsh-auth-" + _b64url_encode(
            hashlib.sha256(authority.encode()).digest())
        cached = self._cached
        if cached is None or cached[0] != expected or now >= cached[2]:
            self._cached = sign_cookie(self._secret, authority, now)
            cached = self._cached
        return cached[0], cached[1]


# ---------------------------------------------------------------- 窗口标题

def _foreground_window() -> tuple[int, str]:
    """(前台 hwnd, 标题)；无前台窗口时 (0, "")。"""
    if not _HAS_WIN:
        return 0, ""
    user32 = ctypes.windll.user32
    hwnd = int(user32.GetForegroundWindow() or 0)
    if not hwnd:
        return 0, ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return hwnd, ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return hwnd, buf.value or ""


def _window_process_path(hwnd: int) -> str:
    if not _HAS_WIN or not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _process_image_path(pid.value)


def strip_title_suffix(title: str, suffix: str) -> str | None:
    """剥掉窗口标题后缀得到候选会话标题；没有后缀返回 None（不是会话窗口）。"""
    if suffix and title.endswith(suffix):
        return title[: -len(suffix)]
    return None


def session_item_title(item: dict) -> str:
    """SessionSummary 的标题在 projections.values.title（可能为空串）。"""
    proj = (item.get("projections") or {}).get("values") or {}
    return str(proj.get("title") or "")


def dsh_foreground(config: dict) -> bool:
    """前台窗口进程是不是 DSH 桌面版（router 的 auto 判定用）。"""
    hints = config.get("process_path_hints") or _DEFAULT_PROCESS_HINTS
    hwnd, _ = _foreground_window()
    if not hwnd:
        return False
    return _matches_dsh_process(_window_process_path(hwnd), hints)


# ---------------------------------------------------------------- 会话匹配

def match_session(
    title: str | None, sessions: list[dict], pinned: str | None
) -> tuple[str | None, str | None]:
    """标题精确匹配 session/list。返回 (session_id, 错误提示)。
    唯一命中用之；重名不猜（提示固定 pinned）；零命中/无标题回退 pinned。"""
    if title:
        hits = [it for it in sessions if session_item_title(it) == title]
        if len(hits) == 1:
            return hits[0].get("sessionId"), None
        if len(hits) > 1:
            return None, "DSH 里有多个同名会话，请在面板固定目标（dsh.pinned_session_id）"
    if pinned:
        return pinned, None
    return None, "先在 DSH 里点一下会话，或在面板固定目标"


# ---------------------------------------------------------------- DSH target

class DshTarget:
    name = "dsh"

    def __init__(self, config: dict, notify, poll_interval_sec: float = 0.7) -> None:
        self._config = config or {}
        self._notify = notify
        self._hints = self._config.get("process_path_hints") or _DEFAULT_PROCESS_HINTS
        self._suffix = self._config.get("window_title_suffix") or WINDOW_TITLE_SUFFIX
        self._pinned = self._config.get("pinned_session_id") or None
        self._poll_interval = max(0.3, float(poll_interval_sec))
        self._port_cache: tuple[int, float] | None = None  # (port, 过期 monotonic)
        self._signer: _CookieSigner | None = None
        self._sessions: list[dict] = []
        self._sessions_ts: float = 0.0
        self._last_title: str = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """后台低频轮询 session/list，刷新 标题→id 缓存（失败静默，下轮再来）。"""
        if not _HAS_WIN or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="dsh-session-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_sessions()
            except Exception as e:
                logger.debug("dsh 会话列表轮询异常: %s", e)
            self._stop.wait(self._poll_interval)

    # ---------- 端口与凭据 ----------

    def _ensure_port(self) -> int | None:
        pinned_port = self._config.get("port")
        if pinned_port:
            return int(pinned_port)
        now = time.monotonic()
        if self._port_cache and now < self._port_cache[1]:
            return self._port_cache[0]
        port = discover_port(self._hints)
        if port:
            self._port_cache = (port, now + _PORT_CACHE_TTL_SEC)
            logger.info("dsh 端口发现: 127.0.0.1:%s", port)
        return port

    def _invalidate_port(self) -> None:
        self._port_cache = None

    def _cookie(self, authority: str) -> tuple[str, str] | None:
        """带 Cookie 的请求只发 127.0.0.1（authority 强校验）。"""
        if not authority.startswith("127.0.0.1:"):
            logger.warning("拒绝向非 127.0.0.1 地址发带 Cookie 的请求: %s", authority)
            return None
        if self._signer is None:
            secret = read_browser_session_secret(self._config.get("home"))
            if not secret:
                return None
            self._signer = _CookieSigner(secret)
        return self._signer.cookie(authority)

    # ---------- RPC ----------

    async def _rpc(
        self, client: httpx.AsyncClient, authority: str, method: str, args: dict
    ) -> dict:
        cookie = self._cookie(authority)
        if cookie is None:
            raise DshAuthError("读不到 DSH 凭据")
        name, value = cookie
        envelope = {
            "type": "client-request",
            "rpcId": uuid.uuid4().hex,
            "method": method,
            "payload": {"args": args},
        }
        resp = await client.post(
            f"http://{authority}/api/{method}",
            json=envelope,  # httpx 自带 Content-Type: application/json（缺了 415）
            headers={"Cookie": f"{name}={value}"},
            timeout=30,
        )
        if resp.status_code == 401:
            self._invalidate_port()
            raise DshAuthError("DSH 认证失败（401）")
        resp.raise_for_status()
        result = (resp.json() or {}).get("result") or {}
        if not result.get("ok"):
            raise DshBusinessError(json.dumps(result, ensure_ascii=False)[:300])
        return result.get("value") or {}

    def _refresh_sessions(self) -> None:
        if time.monotonic() - self._sessions_ts < _SESSION_CACHE_TTL_SEC:
            return
        port = self._ensure_port()
        if not port:
            return
        authority = f"127.0.0.1:{port}"
        cookie = self._cookie(authority)
        if cookie is None:
            return
        name, value = cookie
        envelope = {
            "type": "client-request",
            "rpcId": uuid.uuid4().hex,
            "method": "session/list",
            "payload": {"args": {"_request": {}}},
        }
        with httpx.Client(trust_env=False, timeout=10) as client:
            resp = client.post(f"http://{authority}/api/session/list",
                               json=envelope,
                               headers={"Cookie": f"{name}={value}"})
        if resp.status_code == 401:
            self._invalidate_port()
            return
        resp.raise_for_status()
        result = (resp.json() or {}).get("result") or {}
        if not result.get("ok"):
            return
        items = (result.get("value") or {}).get("items") or []
        with self._lock:
            self._sessions = items
            self._sessions_ts = time.monotonic()

    def _sessions_snapshot(self) -> list[dict]:
        with self._lock:
            stale = time.monotonic() - self._sessions_ts >= _SESSION_CACHE_TTL_SEC
        if stale:
            try:
                self._refresh_sessions()
            except Exception as e:
                logger.debug("dsh 会话列表同步刷新失败: %s", e)
        with self._lock:
            return list(self._sessions)

    # ---------- TargetAdapter 协议 ----------

    async def is_alive(self, client: httpx.AsyncClient, address: str) -> bool:
        try:
            r = await client.get(f"http://{address}/", timeout=3)
            return r.status_code == 401 and AUTH_401_FINGERPRINT in r.text
        except Exception:
            return False

    def is_foreground(self) -> bool:
        return dsh_foreground(self._config)

    def resolve(self) -> ResolvedTarget:
        """同步、快：端口走 30s 缓存，标题读前台窗口（毫秒级），
        会话表走 ≤5s 缓存。收到文字时调用，锁定后不再重新解析。"""
        port = self._ensure_port()
        if not port:
            return ResolvedTarget(self.name, None, None)
        address = f"127.0.0.1:{port}"
        title = None
        if self.is_foreground():
            _, raw = _foreground_window()
            title = strip_title_suffix(raw, self._suffix)
        sid, err = match_session(title, self._sessions_snapshot(), self._pinned)
        if err:
            self._notify(err)
        self._last_title = title or ""
        return ResolvedTarget(self.name, address, sid, self._last_title)

    async def submit_fast(
        self, client: httpx.AsyncClient, target: ResolvedTarget, text: str
    ) -> str:
        """session/prompt：一次请求一次发送，绝不重发（requestId 服务端去重
        只是保险）。失败一律通知 + 返回 "failed"，线程不死。"""
        if not target.address or not target.session_id:
            return "failed"
        try:
            value = await self._rpc(client, target.address, "session/prompt", {
                "request": {
                    "requestId": uuid.uuid4().hex,
                    "sessionId": target.session_id,
                    "mode": "queue",
                    "content": [{"type": "text", "text": text}],
                },
            })
        except DshAuthError:
            # 401/凭据问题：端口缓存已作废，下一句会重探；本句放弃（不是重发）
            self._notify("DSH 认证失败，语音指令没有发出去（下一句会自动重试连接）")
            return "failed"
        except DshBusinessError as e:
            self._notify(f"DSH 没有收下这句话：{e}")
            return "failed"
        except Exception as e:
            self._notify(f"DSH 投递失败：{type(e).__name__}，语音指令没有发出去")
            return "failed"
        if value.get("accepted") is not True:
            self._notify("DSH 没有确认收到这句话，语音指令可能没发出去")
            return "failed"
        logger.info("已投递到 dsh 会话 %s", target.session_id)
        return "accepted"

    def paste(self, text: str, submit: bool) -> str:
        """粘贴进 DSH 输入框：复用 draft_input 泛化主流程（前台判定 =
        前台进程是 DSH，跳过浏览器 omnibox 检查，发送键走 dsh.submit_key）。"""
        from draft_input import paste_into_dsh

        return paste_into_dsh(
            text,
            submit,
            foreground_ok=self.is_foreground,
            submit_key=self._config.get("submit_key") or "enter",
        )

    def panel_state(self) -> dict:
        port = self._port_cache[0] if self._port_cache else self._config.get("port")
        return {
            "dsh_port": port,
            "dsh_session_title": self._last_title,
        }
