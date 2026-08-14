"""MicToAgent 控制面板（pywebview + React/Tailwind UI）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

窗口本体是 WebView2，UI 源码在 voice-bridge/panel-ui/（React + Tailwind，
cir-tabs 风格），构建产物 panel-ui/dist 提交入库、运行时直接 file:// 加载。
本文件只做壳：单实例、无边框直角窗口（DWM 投影；不要开 transparent，
Windows 上它只能透到 WinForms 灰底，会露出一层壳）、
pywebview-drag-region 拖动区、js_api 桥。

JS 不直接发 HTTP：取值/切换全部经 Python 中转，适配器不在时回落读状态
文件（与旧面板语义一致）。改 UI 后在 panel-ui 里 npm run build 重新出 dist。
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import webview

from envutil import bridge_dir, project_root
from winstyle import bring_to_front, centered_position, style_window
from runtime_state import (
    CLOSE_QUIT,
    PANEL_MUTEX,
    autostart_enabled,
    consume_panel_show,
    read_panel_prefs,
    read_send_mode,
    request_panel_show,
    set_autostart,
    stop_requested,
    write_gate_cmd,
    write_panel_prefs,
    write_send_mode,
)

BASE = bridge_dir()
PROJ = project_root()
DIST = Path(__file__).resolve().parent / "panel-ui" / "dist"
try:
    CONFIG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
except Exception:
    CONFIG = {}
LISTEN_PORT = int(CONFIG.get("listen_port", 9100))
PANEL_API = f"http://127.0.0.1:{LISTEN_PORT}/v1/panel"

# 窗口即内容（DWM 负责投影），不再留透明边距
WIN_W, WIN_H = 400, 632
WIN_X, WIN_Y = centered_position(WIN_W, WIN_H)


def _http_json(url: str, data: dict | None = None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    headers = {"Content-Type": "application/json"}
    if data is None:
        req = urllib.request.Request(url, headers=headers)
    else:
        req = urllib.request.Request(
            url, json.dumps(data).encode("utf-8"), headers=headers
        )
    with opener.open(req, timeout=1.5) as r:
        return json.loads(r.read().decode("utf-8"))


def gather_state() -> dict:
    """面板全量状态：适配器在线取其 /v1/panel，不在线回落读状态文件。"""
    st = {
        "send_mode": read_send_mode(),
        "ready": False,
        "gate": "0",
        "session_id": None,
        "target": "away",
        "last_notice": "",
    }
    try:
        st.update(_http_json(PANEL_API))
    except Exception:
        gate = "0"
        try:
            gate = (BASE / "gate.state").read_text(encoding="ascii").strip() or "0"
        except OSError:
            pass
        st["gate"] = gate
        st["send_mode"] = read_send_mode()
        st["ready"] = False
    st["prefs"] = read_panel_prefs()
    try:
        st["autostart"] = bool(autostart_enabled())
    except Exception:
        st["autostart"] = False
    return st


class PanelApi:
    """暴露给前端的桥（window.pywebview.api.*）。取值方法返回最新状态。"""

    def __init__(self):
        self._window = None

    # -- 状态 --
    def get_state(self) -> dict:
        return gather_state()

    # -- 主页动作 --
    def set_send_mode(self, mode: str) -> dict:
        write_send_mode(str(mode))
        try:
            _http_json(PANEL_API, {"send_mode": str(mode)})
        except Exception:
            pass
        return gather_state()

    def set_gate(self, mute: bool) -> dict:
        write_gate_cmd("0" if mute else "1")
        try:
            _http_json(PANEL_API, {"mute": bool(mute)})
        except Exception:
            pass
        return gather_state()

    # -- 设置页动作 --
    def set_close_action(self, action: str) -> dict:
        write_panel_prefs({"close_action": str(action)})
        return gather_state()

    def set_silent_start(self, on: bool) -> dict:
        write_panel_prefs({"silent_start": bool(on)})
        return gather_state()

    def set_show_launcher(self, on: bool) -> dict:
        write_panel_prefs({"show_launcher": bool(on)})
        return gather_state()

    def set_autostart(self, on: bool) -> dict:
        try:
            set_autostart(bool(on))
        except Exception as exc:
            return {"ok": False, "error": f"没能改开机自启：{exc}"}
        return {"ok": True}

    # -- 窗口控制 --
    def win_minimize(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def win_close(self) -> None:
        """X = 按偏好回托盘或退出全部进程。"""
        if read_panel_prefs()["close_action"] == CLOSE_QUIT:
            _quit_all()
            return
        if self._window is not None:
            self._window.hide()


def _quit_all() -> None:
    launcher = BASE / "voice-launcher.pyw"
    pythonw = PROJ / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen(
        [str(pythonw), str(launcher), "--stop"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


class Panel:
    def __init__(self):
        self._boot = time.time()
        self.api = PanelApi()
        self.window = webview.create_window(
            "MicToAgent",
            url=str(DIST / "index.html"),
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
        self.api._window = self.window
        self.window.events.closing += self._on_closing
        self.window.events.shown += lambda: (
            style_window("MicToAgent"),
            bring_to_front("MicToAgent"),  # 被启动器拉起时跳到最前
        )

    def _on_closing(self) -> bool:
        """系统途径关窗（Alt+F4 等）走与 X 相同的语义，不直接销毁。"""
        self.api.win_close()
        return False

    def _watch_cmds(self) -> None:
        """消费 panel.cmd / stop.cmd（托盘左键唤出、launcher --stop 退出）。"""
        while True:
            time.sleep(0.15)
            try:
                if stop_requested(since=self._boot):
                    self.window.destroy()
                    return
                if consume_panel_show():
                    self.window.restore()
                    self.window.show()
                    bring_to_front("MicToAgent")
            except Exception:
                return

    def run(self) -> None:
        webview.start(
            func=self._watch_cmds,
            icon=str(PROJ / "voice-assistant.ico"),
            private_mode=True,
        )


def main() -> None:
    global _MUTEX
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, PANEL_MUTEX)
    if ctypes.windll.kernel32.GetLastError() == 183:
        request_panel_show()
        return
    Panel().run()


_MUTEX = None


if __name__ == "__main__":
    main()
