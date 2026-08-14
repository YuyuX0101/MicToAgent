"""运行时状态文件：发送模式、门控指令、面板唤起。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

这些文件都是本机运行产物（已 gitignore），和 gate.state 一样原子写：
- send.mode   发送模式（adapter 每句读取，下一句生效）
- gate.cmd    面板请求静音/常听（mic-gate 轮询消费，禁止直接改 gate.state）
- panel.cmd   托盘/快捷方式请求显示控制面板（panel.pyw 消费）
- stop.cmd    请各进程自行退出（mic-gate 先注销托盘图标；launcher 再杀残留）
- panel.prefs.json  面板关闭行为（tray / quit）与静默启动；开机自启走「启动」文件夹快捷方式
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
from pathlib import Path

from envutil import bridge_dir, project_root

SEND_FAST = "fast"
SEND_DIALOG = "dialog"
SEND_COMPOSE = "compose"
SEND_MODES = (SEND_FAST, SEND_DIALOG, SEND_COMPOSE)
SEND_DEFAULT = SEND_FAST

SEND_LABELS = {
    SEND_FAST: "快速发送",
    SEND_DIALOG: "对话框发送",
    SEND_COMPOSE: "输入对话框",
}
SEND_NOTES = {
    SEND_FAST: "文字直达当前会话，不能配图。",
    SEND_DIALOG: "粘贴到输入框并发送，可配图。",
    SEND_COMPOSE: "只粘贴到输入框，由你检查后再发。",
}

PANEL_MUTEX = "MicToAgent-panel"

CLOSE_TRAY = "tray"
CLOSE_QUIT = "quit"
CLOSE_ACTIONS = (CLOSE_TRAY, CLOSE_QUIT)
CLOSE_DEFAULT = CLOSE_TRAY


def send_mode_path() -> Path:
    return bridge_dir() / "send.mode"


def gate_cmd_path() -> Path:
    return bridge_dir() / "gate.cmd"


def panel_cmd_path() -> Path:
    return bridge_dir() / "panel.cmd"


def stop_cmd_path() -> Path:
    return bridge_dir() / "stop.cmd"


def _atomic_write(path: Path, text: str, encoding: str = "ascii") -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def read_send_mode() -> str:
    try:
        value = send_mode_path().read_text(encoding="ascii").strip()
    except OSError:
        return SEND_DEFAULT
    return value if value in SEND_MODES else SEND_DEFAULT


def write_send_mode(mode: str) -> str:
    if mode not in SEND_MODES:
        raise ValueError(f"未知发送模式: {mode}")
    _atomic_write(send_mode_path(), mode)
    return mode


def write_gate_cmd(value: str) -> None:
    """请 mic-gate 把常听设为开（"1"）或关（"0"）。不要直接写 gate.state。"""
    if value not in ("0", "1"):
        raise ValueError(f"gate.cmd 只能是 0 或 1，收到 {value!r}")
    _atomic_write(gate_cmd_path(), value)


def consume_gate_cmd() -> str | None:
    path = gate_cmd_path()
    try:
        value = path.read_text(encoding="ascii").strip()
        path.unlink()
    except OSError:
        return None
    return value if value in ("0", "1") else None


def request_panel_show() -> None:
    _atomic_write(panel_cmd_path(), "show")


def consume_panel_show() -> bool:
    path = panel_cmd_path()
    try:
        text = path.read_text(encoding="ascii").strip()
        path.unlink()
    except OSError:
        return False
    return text == "show"


def request_full_stop() -> None:
    """通知门控先注销托盘、面板自行关闭；文件保留到 launcher 清掉。"""
    _atomic_write(stop_cmd_path(), "stop")


def stop_requested(since: float | None = None) -> bool:
    """是否收到退出指令。since=启动时刻时，忽略启动前留下的旧文件。"""
    path = stop_cmd_path()
    try:
        if since is not None and path.stat().st_mtime < since:
            return False
        return path.read_text(encoding="ascii").strip() == "stop"
    except OSError:
        return False


def clear_stop_cmd() -> None:
    try:
        stop_cmd_path().unlink()
    except OSError:
        pass


def mutex_exists(name: str) -> bool:
    if not hasattr(ctypes, "windll"):
        return False
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenMutexW(0x00100000, False, name)  # SYNCHRONIZE
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def open_or_show_panel() -> None:
    """已有面板则发显示指令；没有则拉起 panel.pyw。"""
    try:
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    except Exception:
        pass
    request_panel_show()
    if mutex_exists(PANEL_MUTEX):
        return
    pythonw = project_root() / ".venv" / "Scripts" / "pythonw.exe"
    panel = bridge_dir() / "panel.pyw"
    creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [str(pythonw), str(panel)],
        cwd=str(bridge_dir()),
        creationflags=creation,
    )


def panel_prefs_path() -> Path:
    return bridge_dir() / "panel.prefs.json"


def read_panel_prefs() -> dict:
    """关闭行为、静默启动。开机自启以启动文件夹快捷方式为准，不写在这里。"""
    try:
        data = json.loads(panel_prefs_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    action = data.get("close_action", CLOSE_DEFAULT)
    if action not in CLOSE_ACTIONS:
        action = CLOSE_DEFAULT
    return {
        "close_action": action,
        "silent_start": bool(data.get("silent_start", False)),
        "show_launcher": bool(data.get("show_launcher", True)),
    }


def write_panel_prefs(prefs: dict) -> dict:
    current = read_panel_prefs()
    if "close_action" in prefs:
        action = prefs["close_action"]
        if action not in CLOSE_ACTIONS:
            raise ValueError(f"未知关闭行为: {action}")
    else:
        action = current["close_action"]
    silent = bool(prefs["silent_start"]) if "silent_start" in prefs else current["silent_start"]
    show_launcher = (
        bool(prefs["show_launcher"]) if "show_launcher" in prefs else current["show_launcher"]
    )
    out = {"close_action": action, "silent_start": silent, "show_launcher": show_launcher}
    _atomic_write(
        panel_prefs_path(),
        json.dumps(out, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


def startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "MicToAgent.lnk"
    )


def autostart_enabled() -> bool:
    return startup_shortcut_path().is_file()


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def set_autostart(enabled: bool) -> None:
    """在当前用户「启动」文件夹写入或删除与桌面快捷方式相同的 .lnk。"""
    path = startup_shortcut_path()
    if not enabled:
        try:
            path.unlink()
        except OSError:
            pass
        return
    pythonw = project_root() / ".venv" / "Scripts" / "pythonw.exe"
    launcher = bridge_dir() / "voice-launcher.pyw"
    icon = project_root() / "voice-assistant.ico"
    if not pythonw.is_file():
        raise FileNotFoundError(f"找不到 {pythonw}")
    if not launcher.is_file():
        raise FileNotFoundError(f"找不到 {launcher}")
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$sh = New-Object -ComObject WScript.Shell\n"
        f"$lnk = $sh.CreateShortcut({_ps_literal(str(path))})\n"
        f"$lnk.TargetPath = {_ps_literal(str(pythonw))}\n"
        f"$lnk.Arguments = {_ps_literal(chr(34) + str(launcher) + chr(34))}\n"
        f"$lnk.WorkingDirectory = {_ps_literal(str(bridge_dir()))}\n"
        "$lnk.Description = 'MicToAgent'\n"
        "$lnk.WindowStyle = 1\n"
    )
    if icon.is_file():
        script += f"$lnk.IconLocation = {_ps_literal(str(icon) + ',0')}\n"
    script += "$lnk.Save()\n"
    fd, tmp_name = tempfile.mkstemp(suffix=".ps1")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(script, encoding="utf-8-sig")
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(tmp),
            ],
            check=True,
            creationflags=creation,
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def count_hotwords(path: Path | None = None) -> int:
    target = path or (bridge_dir() / "hotwords.txt")
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    n = 0
    for raw in lines:
        line = raw.strip()
        if line and not line.startswith("#"):
            n += 1
    return n
