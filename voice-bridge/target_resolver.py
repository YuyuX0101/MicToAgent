"""跟随 kimi web 当前选中会话的目标解析器（Windows UI Automation）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

用户在浏览器里点中哪个会话，地址栏 URL 就带上哪个会话 id（/sessions/<id>）。
本模块用 UIA 读浏览器（Edge/Chrome）地址栏：后台线程低频轮询并缓存，
发送语音文字前再同步刷新一次（约 25ms）——文字到达适配器必然晚于用户
切换会话好几秒，所以"刚切换完就说话"也进正确的会话。

UIA 不可用（非 Windows、未装 uiautomation、没找到 kimi web 标签页）时返回 None，
由调用方语音提示用户先去网页里选中一个会话。地址栏在 kimi web 但没有会话 id
时（新建会话草稿页），on_kimi_web() 返回 True，由调用方按发送模式走粘贴或降级。
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
from ctypes import wintypes
from urllib.parse import urlparse

logger = logging.getLogger("voice-bridge.target")

try:  # UIA 只在 Windows 上有意义；uiautomation 未装时静默降级
    import uiautomation as auto

    _HAS_UIA = hasattr(ctypes, "windll")
except Exception:  # pragma: no cover - 取决于运行环境
    auto = None
    _HAS_UIA = False

_BROWSER_PROCESSES = {"msedge.exe", "chrome.exe"}
_BROWSER_WNDCLASS = "Chrome_WidgetWin_1"

# 本机私有地址别名（如用局域网 IP 访问同一 kimi web 实例）。
# 由 set_address_aliases 在适配器启动时注册，匹配逻辑对三个调用方
# （跟随轮询 / 发送前刷新 / 粘贴前置检查）统一生效。
_EXTRA_AUTHORITIES: list[str] = []


def set_address_aliases(bases) -> None:
    """注册额外地址：host:port 或完整 URL（如 http://192.168.x.x:58627）。"""
    _EXTRA_AUTHORITIES.clear()
    for base in bases or []:
        netloc = urlparse(base if "://" in str(base) else f"http://{base}").netloc
        if netloc and netloc not in _EXTRA_AUTHORITIES:
            _EXTRA_AUTHORITIES.append(netloc)


def _authority_alt(port: int) -> str:
    """地址栏匹配的 authority 交替式：localhost/127.0.0.1:port + 私有别名。"""
    auths = [f"localhost:{port}", f"127.0.0.1:{port}"]
    auths += [a for a in _EXTRA_AUTHORITIES if a not in auths]
    return "(?:" + "|".join(re.escape(a) for a in auths) + ")"


def extract_session_id(omnibox_value: str, port: int) -> str | None:
    """从地址栏文本里提取 kimi web 会话 id；不是 kimi web 会话页则返回 None。

    地址栏通常不显示协议头，如 localhost:58627/sessions/session_xxx。
    """
    if not omnibox_value:
        return None
    m = re.search(rf"{_authority_alt(port)}/sessions/([\w-]+)", omnibox_value)
    return m.group(1) if m else None


def classify_omnibox(omnibox_value: str, port: int) -> tuple[str, str | None]:
    """地址栏三态分类：
    - ("session", sid)：kimi web 会话页；
    - ("home", None)：在 kimi web 但没有会话 id——新建会话草稿页（会话是
      懒创建的，第一条消息发出前服务端不存在，地址栏只有裸域名）；
    - ("away", None)：不在 kimi web（或读不到地址栏）。
    """
    sid = extract_session_id(omnibox_value, port)
    if sid:
        return "session", sid
    if omnibox_value and re.search(rf"{_authority_alt(port)}(?:/|$)", omnibox_value):
        return "home", None
    return "away", None


def _enum_browser_hwnds() -> list[int]:
    """用 Win32 EnumWindows 找浏览器顶层窗口（纯 C 调用，毫秒级）。
    按 z-order 返回（最前台的在前），只保留 Edge/Chrome 进程。"""
    if not hasattr(ctypes, "windll"):
        return []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnds: list[int] = []
    buf = ctypes.create_unicode_buffer(64)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)
    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        user32.GetClassNameW(hwnd, buf, 64)
        if buf.value != _BROWSER_WNDCLASS:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        # 只留真正的浏览器进程（排除 QQ、Docker 这类内嵌 Chromium 的窗口）
        hproc = kernel32.OpenProcess(0x1000, False, pid.value)  # LIMITED_INFORMATION
        if hproc:
            try:
                name_buf = ctypes.create_unicode_buffer(260)
                size = wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(hproc, 0, name_buf, ctypes.byref(size)):
                    exe = name_buf.value.rsplit("\\", 1)[-1].lower()
                    if exe in _BROWSER_PROCESSES:
                        hwnds.append(hwnd)
            finally:
                kernel32.CloseHandle(hproc)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


def foreground_kimi_state(port: int) -> tuple[str, str | None]:
    """前台窗口是不是 kimi web（不激活、不抢焦点）。

    返回 classify_omnibox 的三态。前台不是 Chrome/Edge 时为 ("away", None)。
    """
    if not _HAS_UIA or not hasattr(ctypes, "windll"):
        return "away", None
    fg = ctypes.windll.user32.GetForegroundWindow()
    if not fg or fg not in _enum_browser_hwnds():
        return "away", None
    return classify_omnibox(_read_omnibox(fg), port)


def _read_omnibox(hwnd: int) -> str:
    """读某个浏览器窗口的地址栏文本；读不到返回空串。"""
    try:
        win = auto.ControlFromHandle(hwnd)
        edit = win.EditControl(searchDepth=20)
        if edit.Exists(0.3):
            return edit.GetValuePattern().Value or ""
    except Exception as e:
        logger.debug("读地址栏失败 hwnd=%s: %s", hwnd, e)
    return ""


class FollowUiResolver:
    """后台轮询浏览器地址栏，缓存"用户正在看的 kimi 会话 id"。"""

    def __init__(self, kimi_base: str, interval_sec: float = 0.7):
        port = urlparse(kimi_base).port
        self._port = port or (443 if kimi_base.startswith("https") else 80)
        self._interval = max(0.3, float(interval_sec))
        self._current: str | None = None
        self._on_kimi_web: bool = False
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def available(self) -> bool:
        return _HAS_UIA

    @property
    def port(self) -> int:
        return self._port

    def start(self) -> None:
        if not _HAS_UIA:
            logger.warning("UIA 不可用，无法跟随选中会话（将一直提示先选中会话）")
            return
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="follow-ui-resolver", daemon=True
            )
            self._thread.start()
            logger.info("跟随模式已启动：轮询浏览器地址栏（每 %.1fs）", self._interval)

    def stop(self) -> None:
        self._stop.set()

    def current(self) -> str | None:
        """返回缓存的会话 id（可能为 None）。"""
        with self._lock:
            return self._current

    def on_kimi_web(self) -> bool:
        """浏览器里是否有 kimi web 页面（不一定选中了会话）。
        True 且 current() 为 None = 正停在新建会话草稿页。"""
        with self._lock:
            return self._on_kimi_web

    def refresh(self) -> None:
        """同步刷新一次（实测约 25ms）。语音文字到达适配器时调用：
        文字到达必然晚于用户切换会话好几秒，当场刷新保证
        「刚切换完就说话」也进正确的会话。"""
        if _HAS_UIA:
            self._poll_once()

    def _loop(self) -> None:
        import comtypes

        comtypes.CoInitialize()
        try:
            while not self._stop.is_set():
                try:
                    self._poll_once()
                except Exception as e:
                    logger.debug("跟随轮询异常: %s", e)
                self._stop.wait(self._interval)
        finally:
            comtypes.CoUninitialize()

    def _poll_once(self) -> None:
        # 后台轮询和发送前的同步刷新可能撞车，串行化避免并发走 UIA
        with self._poll_lock:
            sid: str | None = None
            on_web = False
            for hwnd in _enum_browser_hwnds():
                state, found = classify_omnibox(_read_omnibox(hwnd), self._port)
                if state == "session":
                    sid = found
                    on_web = True
                    break
                if state == "home":
                    on_web = True
        with self._lock:
            self._on_kimi_web = on_web
            if sid != self._current:
                logger.info("跟随目标切换: %s -> %s", self._current, sid)
                self._current = sid
