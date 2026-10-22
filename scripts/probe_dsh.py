# -*- coding: utf-8 -*-
"""DSH 桌面版接入 Phase 0 探测脚本（一次性诊断工具，不入主链路）。

验证 dsh-integration-plan.md §2 的全部假设：
  1. DSH 桌面版进程在跑
  2. 端口发现（TCP 表按进程反查 + 401 指纹兜底）
  3. 自签浏览器会话 Cookie，POST /api/session/list
  4. 窗口标题反查 sessionId
  5. 新建测试会话并投递一条消息
  6. （手动步骤，脚本不管）

安全约定：secret 绝不打印、绝不写日志；.credentials.yaml 只读；
带 Cookie 的请求只发往 127.0.0.1。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from ctypes import wintypes

import httpx

DSH_PROCESS_HINTS = ["deepseek harness", "dsh"]
WINDOW_TITLE_SUFFIX = " — DeepSeek Harness"
AUTH_401_FINGERPRINT = "dsh web authentication required"

PASS, FAIL = "[PASS]", "[FAIL]"
failures: list[str] = []


def report(ok: bool, label: str, detail: str = "") -> None:
    print(f"{PASS if ok else FAIL} {label}" + (f" | {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------- 端口发现

class MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


TCP_TABLE_OWNER_PID_LISTENER = 5
AF_INET = 2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def process_image_path(pid: int) -> str:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
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


def list_loopback_listeners() -> list[tuple[int, int]]:
    """返回 [(port, pid)]，仅 127.0.0.1 上的 LISTEN。"""
    iphlpapi = ctypes.windll.iphlpapi
    size = wintypes.DWORD(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False,
                                 AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0)
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False,
                                    AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
        return []
    num = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
    rows = ctypes.cast(
        ctypes.byref(buf, ctypes.sizeof(wintypes.DWORD)),
        ctypes.POINTER(MIB_TCPROW_OWNER_PID * num),
    ).contents
    out = []
    for row in rows:
        # dwLocalAddr 是网络字节序；127.0.0.1 = 0x0100007F
        if row.dwLocalAddr != 0x0100007F:
            continue
        port = ((row.dwLocalPort & 0xFF) << 8) | ((row.dwLocalPort >> 8) & 0xFF)
        out.append((port, row.dwOwningPid))
    return out


def discover_port() -> int | None:
    listeners = list_loopback_listeners()
    print(f"  loopback 监听端口数: {len(listeners)}")
    candidates: list[int] = []
    for port, pid in listeners:
        path = process_image_path(pid)
        low = path.lower()
        if any(h in low for h in DSH_PROCESS_HINTS):
            print(f"  命中进程: pid={pid} port={port} path={path}")
            candidates.append(port)
    if not candidates:
        print("  按进程路径未命中，改用 401 指纹扫描全部 loopback 监听端口")
        candidates = [port for port, _ in listeners]
    for port in candidates:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/", timeout=3, trust_env=False)
        except httpx.HTTPError:
            continue
        if resp.status_code == 401 and AUTH_401_FINGERPRINT in resp.text:
            return port
        print(f"  端口 {port} 指纹不符: status={resp.status_code}")
    return None


# ---------------------------------------------------------------- 凭据读取

def read_browser_session_secret() -> bytes | None:
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    path = os.path.join(home, ".credentials.yaml")
    if not os.path.isfile(path):
        print(f"  凭据文件不存在: {path}")
        return None
    in_records = False
    in_target = False
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
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
                    return b64url_decode(value)
                except Exception:
                    print("  secret 不是合法 base64url")
                    return None
    print("  未找到 client-connection/browser-session 记录")
    return None


# ---------------------------------------------------------------- Cookie 签名

def sign_cookie(secret: bytes, authority: str) -> tuple[str, str]:
    name = "dsh-auth-" + b64url_encode(hashlib.sha256(authority.encode()).digest())
    now_ms = int(time.time() * 1000)
    payload = json.dumps({
        "version": 1,
        "authority": authority,
        "issuedAt": now_ms,
        "expiresAt": now_ms + 24 * 3600 * 1000,
    }, separators=(",", ":"))
    # HMAC 的输入是 base64url 编码后的 body 字符串，不是原始 JSON
    body = b64url_encode(payload.encode())
    sig = hmac.new(secret, body.encode(), hashlib.sha256).digest()
    value = f"v1.{body}.{b64url_encode(sig)}"
    return name, value


# ---------------------------------------------------------------- RPC

def rpc(client: httpx.Client, port: int, method: str, args: dict) -> dict:
    envelope = {
        "type": "client-request",
        "rpcId": uuid.uuid4().hex,
        "method": method,
        "payload": {"args": args},
    }
    resp = client.post(f"http://127.0.0.1:{port}/api/{method}", json=envelope)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    result = body.get("result") or {}
    if not result.get("ok"):
        raise RuntimeError(f"业务错误: {json.dumps(result, ensure_ascii=False)[:300]}")
    return result.get("value") or {}


# ---------------------------------------------------------------- 窗口标题

def dsh_window_titles(dsh_pids: set[int]) -> list[str]:
    user32 = ctypes.windll.user32
    titles: list[str] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum_proc
    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in dsh_pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value:
            titles.append(buf.value)
        return True

    user32.EnumWindows(_cb, 0)
    return titles


def main() -> int:
    print("== Step 1: DSH 桌面版进程 ==")
    listeners = list_loopback_listeners()
    dsh_pids = {pid for _, pid in listeners
                if any(h in process_image_path(pid).lower() for h in DSH_PROCESS_HINTS)}
    # 进程可能不持有监听端口（渲染进程），再用全量进程列表兜底
    if not dsh_pids:
        print("  （监听表里没有 DSH 进程，可能未启动 host）")
    report(bool(dsh_pids), "DSH 进程在跑", f"pids={sorted(dsh_pids)}")

    print("== Step 2: 端口发现 ==")
    port = discover_port()
    report(port is not None, "端口发现", f"127.0.0.1:{port}" if port else "未找到")
    if port is None:
        return 1

    print("== Step 3: 自签 Cookie + session/list ==")
    secret = read_browser_session_secret()
    report(secret is not None and len(secret) == 32,
           "读取 browser-session secret", "32 bytes (内容不打印)" if secret else "")
    if not secret:
        return 1
    authority = f"127.0.0.1:{port}"
    cookie_name, cookie_value = sign_cookie(secret, authority)
    client = httpx.Client(
        cookies={cookie_name: cookie_value}, trust_env=False, timeout=10)
    try:
        sessions = rpc(client, port, "session/list", {"_request": {}})
    except Exception as exc:
        report(False, "session/list", str(exc))
        return 1
    items = sessions.get("items") or []
    report(True, "session/list", f"共 {len(items)} 个会话")

    def item_title(item: dict) -> str:
        proj = (item.get("projections") or {}).get("values") or {}
        return str(proj.get("title") or "")

    for item in items[:10]:
        print(f"  - sessionId={item.get('sessionId')} title={item_title(item)!r} "
              f"running={item.get('running')} keys={sorted(item.keys())}")
    # §6 开放问题 1：SessionSummary 有 running 布尔字段，v2 盯梢可用轮询
    if items:
        print(f"  SessionSummary running 字段样例: {items[0].get('running')!r}")

    print("== Step 4: 窗口标题反查 ==")
    titles = dsh_window_titles(dsh_pids)
    report(bool(titles), "找到 DSH 窗口", f"{len(titles)} 个")
    by_title = {item_title(it): it.get("sessionId") for it in items}
    for title in titles:
        print(f"  窗口标题: {title!r}")
        if title.endswith(WINDOW_TITLE_SUFFIX):
            candidate = title[: -len(WINDOW_TITLE_SUFFIX)]
            hit = by_title.get(candidate)
            print(f"    剥后缀 -> {candidate!r} -> sessionId={hit}")

    print("== Step 5: 建测试会话 + 投消息 ==")
    try:
        created = rpc(client, port, "session/create", {"request": {}})
        session_id = created.get("sessionId")
        report(bool(session_id), "session/create", f"sessionId={session_id}")
        value = rpc(client, port, "session/prompt", {"request": {
            "requestId": uuid.uuid4().hex,
            "sessionId": session_id,
            "mode": "queue",
            "content": [{"type": "text",
                         "text": "这是 MicToAgent 探测脚本发的测试消息，请只回复 ok 两个字。"}],
        }})
        report(value.get("accepted") is True, "session/prompt",
               json.dumps(value, ensure_ascii=False))
        print("  >>> 请到 DSH 窗口肉眼确认消息到达且排队语义正常 <<<")
    except Exception as exc:
        report(False, "session/create|prompt", str(exc))

    print("== 汇总 ==")
    if failures:
        print("失败项: " + "; ".join(failures))
        return 1
    print("全部通过。Step 6（Enter 语义 / 贴图）需手动验证后回写计划文档。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
