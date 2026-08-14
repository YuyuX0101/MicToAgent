"""voice-launcher：语音助手启动向导。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

窗口本体是 WebView2，UI 在 panel-ui 里（launcher.html 入口，与控制面板
同一套 React + Tailwind cir-tabs 设计语言），构建产物 panel-ui/dist 入库、
运行时 file:// 直载。本文件只做壳和进程编排：四阶段进度实时推给前端，
全部就绪后自动关闭，并在此时才拉起 mic-gate（托盘图标）和控制面板。
右上角有最小化和关闭按钮；启动中点关闭 = 终止本次启动（杀掉已拉起的进程）。

用法：
    启动（桌面快捷方式） pythonw.exe voice-launcher.pyw
                        （未运行→启动向导；已在运行→打开控制面板）
    停止（托盘右键「退出」，或面板设置关闭=退出） pythonw.exe voice-launcher.pyw --stop
                        （杀全部进程，不弹提示窗）

阶段检测依据（均读日志文件，logging 走 stderr 最可靠）：
    1. 启动桥接适配器      → GET 127.0.0.1:9100/health 返回 200
    2. 加载运行依赖库      → err.log 出现 "[stt] loading engine"
       （sherpa-onnx 导入 + 开始加载模型，单独一阶段给反馈）
    3. 加载语音识别模型    → err.log 出现 "[stt] engine ready"
       （模型加载完成后会做预热推理）
    4. 连接目标会话        → GET /ready 返回 true（此时预热 Hello 必然早已到达，
       只是最终确认链路通畅，通常立即通过）
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import webview

from envutil import bridge_dir, project_root
from winstyle import bring_to_front, centered_position, style_window

BASE = bridge_dir()                              # voice-bridge 目录
PROJ = project_root()                            # 项目根（.venv / logs 所在）
VENV_PY = PROJ / ".venv" / "Scripts" / "python.exe"
VENV_PYW = PROJ / ".venv" / "Scripts" / "pythonw.exe"
LOGS = PROJ / "logs"
LOGS.mkdir(exist_ok=True)
DIST = Path(__file__).resolve().parent / "panel-ui" / "dist"

ADAPTER_HEALTH = "http://127.0.0.1:9100/health"
ADAPTER_READY = "http://127.0.0.1:9100/ready"

WIN_TITLE = "MicToAgent 启动向导"
WIN_W, WIN_H = 380, 460
WIN_X, WIN_Y = centered_position(WIN_W, WIN_H)

STAGE_NAMES = [
    "启动桥接适配器",
    "加载运行依赖库",
    "加载语音识别模型",
    "连接目标会话",
]


# ---------- 单实例保护 ----------
def _already_running(name: str) -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# ---------- 进程与阶段推进（后台线程） ----------

class Launcher:
    def __init__(self, on_stage, on_done, on_error):
        self.on_stage = on_stage
        self.on_done = on_done
        self.on_error = on_error
        self.procs = []
        self.aborted = False

    def kill_all(self):
        self.aborted = True
        for p in self.procs:
            try:
                if p.poll() is None:
                    p.kill()
            except Exception:
                pass

    def _spawn(self, cmd, cwd, out_name, err_name, env_extra=None):
        env = dict(os.environ)
        # 子进程 stdout/stderr 重定向到文件时是块缓冲，进程被 kill 后缓冲丢失、
        # 日志只剩零星几行，卡死时无法定位。强制无缓冲，日志实时可见。
        env["PYTHONUNBUFFERED"] = "1"
        env.update(env_extra or {})
        out = open(LOGS / out_name, "w", encoding="utf-8", errors="replace")
        err = open(LOGS / err_name, "w", encoding="utf-8", errors="replace")
        p = subprocess.Popen(cmd, cwd=str(cwd), stdout=out, stderr=err,
                             env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        self.procs.append(p)
        return p

    @staticmethod
    def _http_json(url):
        try:
            # 只打本机适配器，不能走系统代理（Clash 等会把 127.0.0.1 拐走）
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _log_has(name, needle) -> bool:
        try:
            return needle in (LOGS / name).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return False

    def _alive(self) -> bool:
        return all(p.poll() is None for p in self.procs)

    def _wait(self, cond, timeout) -> bool:
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if self.aborted or not self._alive():
                return False
            if cond():
                return True
            time.sleep(0.5)
        return False

    def _fail(self, msg):
        dead = [p.poll() for p in self.procs if p.poll() is not None]
        if dead:
            msg += f"（进程已退出，退出码 {dead}）"
        self.on_error(msg + "，请查看 logs 目录日志")

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.on_error(f"启动向导自身出错：{type(e).__name__} {e}")

    def _run(self):
        # 启动期间强制关门（静音）：清掉上次可能残留的开门状态，
        # 门控状态文件由 mic-gate 在全部就绪后拉起时接管
        try:
            (BASE / "gate.state").write_text("0", encoding="ascii")
        except Exception:
            pass
        try:
            from runtime_state import clear_stop_cmd
            clear_stop_cmd()
        except Exception:
            pass
        self.on_stage(0, "active")
        self._spawn([str(VENV_PY), str(BASE / "adapter.py")], BASE,
                    "voice-bridge.out.log", "voice-bridge.err.log")
        if not self._wait(lambda: self._http_json(ADAPTER_HEALTH) is not None, 30):
            return None if self.aborted else self._fail("适配器 30 秒未响应")
        self.on_stage(0, "done")

        self.on_stage(1, "active")
        self._spawn([str(VENV_PY), str(BASE / "voice-core.py")], PROJ,
                    "voice-assistant.out.log", "voice-assistant.err.log",
                    env_extra={"HF_HUB_OFFLINE": "1"})
        # sherpa-onnx 导入 + 开始加载模型前日志没有输出，单独占一个阶段
        # 让用户知道在干活。"[stt] loading engine" = 导入完成、开始加载。
        deps_done = lambda: self._log_has("voice-assistant.err.log", "[stt] loading engine")
        if not self._wait(deps_done, 300):
            return None if self.aborted else self._fail("运行依赖库加载超时")
        self.on_stage(1, "done")

        self.on_stage(2, "active")
        # 模型加载完成、预热推理结束后打印 "[stt] engine ready"
        if not self._wait(lambda: self._log_has("voice-assistant.err.log", "[stt] engine ready"), 300):
            return None if self.aborted else self._fail("语音识别模型加载超时")
        self.on_stage(2, "done")

        self.on_stage(3, "active")
        ready = lambda: bool((self._http_json(ADAPTER_READY) or {}).get("ready"))
        if not self._wait(ready, 120):
            return None if self.aborted else self._fail("链路预热超时")
        self.on_stage(3, "done")

        # 就绪：此刻才拉起门控助手（托盘图标出现 = 真正可用）。
        # 默认同时打开控制面板；设置了静默启动则只留托盘。
        self._spawn([str(VENV_PYW), str(BASE / "mic-gate.pyw")], BASE,
                    "mic-gate.out.log", "mic-gate.err.log")
        from runtime_state import read_panel_prefs
        if not read_panel_prefs().get("silent_start"):
            self._spawn([str(VENV_PYW), str(BASE / "panel.pyw")], BASE,
                        "panel.out.log", "panel.err.log")
        self.on_done()


# ---------- 界面（pywebview 壳，UI 在 panel-ui/src/launcher.jsx） ----------

class _LauncherApi:
    """暴露给前端的桥（window.pywebview.api.*）。"""

    def __init__(self, win: "LauncherWindow"):
        self._win = win

    def get_state(self) -> dict:
        """进度快照：页面加载晚于首批事件时靠它对齐。"""
        return self._win.snapshot

    def win_minimize(self) -> None:
        self._win.window.minimize()

    def win_close(self) -> None:
        # destroy 会触发 closing 事件，关闭语义集中在 LauncherWindow._closing
        self._win.window.destroy()


class LauncherWindow:
    """启动进度卡：窗口即内容（无边框直角 + DWM 投影），Python 只推状态快照。

    前端布局：图标标题、状态行、节点串联进度条（轨道穿过四个阶段点，
    进行中节点呼吸脉冲，填充缓动滑向下一节点）、底部使用提示。"""

    def __init__(self, on_close):
        self.on_close = on_close
        self.finished = False   # 成功/失败后的自动关窗不再触发 kill_all
        self.snapshot = {
            "stages": ["pending"] * len(STAGE_NAMES),
            "status": "正在初始化…",
            "kind": "active",
        }
        self.api = _LauncherApi(self)
        self.window = webview.create_window(
            WIN_TITLE,
            url=str(DIST / "launcher.html"),
            width=WIN_W,
            height=WIN_H,
            x=WIN_X,
            y=WIN_Y,
            resizable=False,
            frameless=True,
            shadow=False,  # CS_DROPSHADOW 在区域裁剪后留矩形残边；投影由 style_window 走 DWM
            background_color="#fafbfc",
            easy_drag=False,  # 只允许 .pywebview-drag-region 拖动，控件不误触
            js_api=self.api,
        )
        self.window.events.closing += self._closing
        self.window.events.shown += lambda: (
            style_window(WIN_TITLE),
            bring_to_front(WIN_TITLE),  # 启动时跳到最前让用户看到进度
            # WebView2 初始化可能把窗口重新压下去，延时再抬一次保险
            threading.Timer(0.6, lambda: bring_to_front(WIN_TITLE)).start(),
        )

    def _closing(self) -> bool:
        # 启动中关窗 = 终止本次启动；已结束（成功/失败）时直接关即可
        if not self.finished:
            self.on_close()
        return True

    def _push(self) -> None:
        try:
            self.window.evaluate_js(
                "window.launcherSync && window.launcherSync("
                + json.dumps(self.snapshot, ensure_ascii=False) + ")"
            )
        except Exception:
            pass

    # 以下三个方法由 Launcher 工作线程回调，evaluate_js 线程安全
    def set_stage(self, idx, state):
        self.snapshot["stages"][idx] = state
        if state == "active":
            self.snapshot["status"] = STAGE_NAMES[idx] + "…"
            self.snapshot["kind"] = "active"
        self._push()

    def succeed(self):
        self.finished = True
        self.snapshot["status"] = "已就绪，可以开始使用了"
        self.snapshot["kind"] = "done"
        self._push()
        threading.Timer(1.4, self._safe_destroy).start()

    def fail(self, msg):
        self.finished = True
        # 进行中的节点钉成红色
        self.snapshot["stages"] = [
            "error" if s == "active" else s for s in self.snapshot["stages"]
        ]
        self.snapshot["status"] = msg
        self.snapshot["kind"] = "error"
        self._push()
        threading.Timer(12, self._safe_destroy).start()

    def _safe_destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass

    def run(self):
        webview.start(icon=str(PROJ / "voice-assistant.ico"), private_mode=True)


# ---------- 启动/停止入口 ----------
# 桌面快捷方式 = 只启动（无参数）：未运行→启动向导；已在运行→打开控制面板。
# 停止只有显式 --stop（托盘右键或面板关闭行为=退出）：杀全部进程，不弹窗。


def _find_assistant_pids():
    """按命令行特征找运行中的语音助手进程（适配器/语音管线/门控，排除自身）。"""
    ps = ("$procs = Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |"
          " Where-Object { $_.CommandLine -match"
          " 'voice-core|adapter\\.py|mic-gate|panel\\.pyw' };"
          "$procs.ProcessId -join ' '")
    own = {os.getpid(), os.getppid()}
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return [int(x) for x in r.stdout.split()
                if x.strip().isdigit() and int(x) not in own]
    except Exception:
        return []


def _kill_pids(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass


def _flush_stale_tray_icons():
    """Explorer 在进程被强杀后会留下幽灵图标，直到鼠标划过。对通知区发一次空移动。"""
    user32 = ctypes.windll.user32
    FindWindowEx = user32.FindWindowExW
    FindWindowEx.restype = wintypes.HWND
    FindWindowEx.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
    WM_MOUSEMOVE = 0x0200

    def _scan(hwnd):
        if not hwnd:
            return
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return
        y = max(0, (rect.bottom - rect.top) // 2)
        width = max(rect.right, 0)
        for x in range(0, width + 1, 8):
            user32.SendMessageW(hwnd, WM_MOUSEMOVE, 0, (y << 16) | (x & 0xFFFF))

    tray = user32.FindWindowW("Shell_TrayWnd", None)
    notify = FindWindowEx(tray, None, "TrayNotifyWnd", None) if tray else None
    pager = FindWindowEx(notify, None, "SysPager", None) if notify else None
    toolbar = FindWindowEx(pager or notify, None, "ToolbarWindow32", None) if notify else None
    _scan(toolbar)
    overflow = user32.FindWindowW("NotifyIconOverflowWindow", None)
    overflow_tb = FindWindowEx(overflow, None, "ToolbarWindow32", None) if overflow else None
    _scan(overflow_tb)


def _adapter_alive() -> bool:
    return Launcher._http_json(ADAPTER_HEALTH) is not None


def _spawn_resident(script: str, out_name: str, err_name: str, cwd=None):
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    out = open(LOGS / out_name, "a", encoding="utf-8", errors="replace")
    err = open(LOGS / err_name, "a", encoding="utf-8", errors="replace")
    return subprocess.Popen(
        [str(VENV_PYW), str(BASE / script)],
        cwd=str(cwd or BASE),
        stdout=out,
        stderr=err,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def main():
    if "--stop" in sys.argv:
        from runtime_state import clear_stop_cmd, request_full_stop
        request_full_stop()
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.0:
            pids = _find_assistant_pids()
            if not pids:
                break
            time.sleep(0.15)
        pids = _find_assistant_pids()
        if pids:
            _kill_pids(pids)
            time.sleep(0.2)
        _flush_stale_tray_icons()
        clear_stop_cmd()
        return

    # 只有适配器真的在服务，才视为已在运行。残留的 panel.pyw 不算，
    # 否则快捷方式会跳过启动器、也起不来托盘。
    if _adapter_alive():
        from runtime_state import mutex_exists, open_or_show_panel
        open_or_show_panel()
        if not mutex_exists("MicToAgent-mic-gate"):
            LOGS.mkdir(exist_ok=True)
            _spawn_resident("mic-gate.pyw", "mic-gate.out.log", "mic-gate.err.log")
        return

    leftovers = _find_assistant_pids()
    if leftovers:
        _kill_pids(leftovers)
        time.sleep(0.3)
        _flush_stale_tray_icons()

    if _already_running("MicToAgent-voice-launcher"):
        return  # 已有一个向导在跑，避免双实例互踩（端口/日志冲突）

    from runtime_state import read_panel_prefs
    if not read_panel_prefs().get("show_launcher", True):
        # 完全静默启动：不弹进度卡，前台干等到就绪；启动失败才弹卡展示
        # 错误，否则进程悄悄没了用户无从知晓
        states = ["pending"] * len(STAGE_NAMES)
        outcome = {}
        headless = Launcher(
            on_stage=lambda i, s: states.__setitem__(i, s),
            on_done=lambda: outcome.setdefault("ok", True),
            on_error=lambda m: outcome.setdefault("err", m),
        )
        headless.run()
        if "err" not in outcome:
            return
        win = LauncherWindow(on_close=headless.kill_all)
        win.snapshot["stages"] = states
        win.fail(outcome["err"])
        win.run()
        return

    launcher = Launcher(
        on_stage=None, on_done=None, on_error=None,  # 占位，下面窗口建好再接
    )
    win = LauncherWindow(on_close=launcher.kill_all)
    launcher.on_stage = win.set_stage
    launcher.on_done = win.succeed
    launcher.on_error = win.fail
    threading.Thread(target=launcher.run, daemon=True).start()
    win.run()


if __name__ == "__main__":
    main()
