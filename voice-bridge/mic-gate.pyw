"""mic-gate：语音门控热键 + 托盘助手（speech-to-kimi-code fork 原创）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors

门控位置在**音频入口**（speech-to-kimi-entry.py 对 LocalAudioStreamer 的
运行时补丁）：本助手只负责把门控状态原子写入 voice-bridge/gate.state，
s2s 音频回调读到 "0" 就给 VAD 喂静音帧、"1" 才放行真实音频。

- 按住「右 Ctrl」（或鼠标侧键，见 VK_MAP）：临时开门说话，松开**立即断音**——
  已经在管道里的那句会继续识别并送达，新声音进不来，没有宽限期。
- 「F9」：切换常听模式开/关，同样立即生效。
- 系统托盘图标 = 门控状态（绿=收音中，红=静音），只变色、不弹通知；
  右键「退出」调 voice-launcher --stop 杀掉语音助手全部进程（含本助手）。

默认关门：语音助手启动后，不按键时任何声音都进不了 VAD，
包括 TTS 自己外放的回复（从根上消除回显自问自答）。

实现说明：按键检测用 Win32 GetAsyncKeyState 轮询（50ms），
不用全局钩子——钩子的事件名在不同键盘/驱动下不稳定
（右 Ctrl 可能被报成 'ctrl' 导致松开事件丢失），轮询原始按键状态最可靠。

运行（由 voice-launcher.pyw 在全部就绪后拉起）：
    .venv\\Scripts\\pythonw.exe mic-gate.pyw
"""

import ctypes
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pystray
from PIL import Image, ImageOps

BASE_DIR = Path(__file__).resolve().parent
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

GATE_STATE = BASE_DIR / "gate.state"
POLL_SEC = 0.05

# 键名 → Win32 虚拟键码（可在此扩展）
VK_MAP = {
    "right ctrl": 0xA3,   # VK_RCONTROL（严格区分左右，不会和左 Ctrl 混淆）
    "left ctrl": 0xA2,    # VK_LCONTROL
    "f9": 0x78,           # VK_F9
    "mouse x1": 0x05,     # VK_XBUTTON1（鼠标侧键1=后退，无需映射软件直接可用）
    "mouse x2": 0x06,     # VK_XBUTTON2（鼠标侧键2=前进）
}


def _vk_list(value, default: str) -> list[int]:
    """把配置里的键名（单个或列表）解析成虚拟键码列表。"""
    if isinstance(value, str):
        value = [value]
    vks = [VK_MAP[str(v).lower()] for v in value if str(v).lower() in VK_MAP]
    return vks or [VK_MAP[default]]


# 按住说话键 / 常听开关键，都可配置多个（任一触发即生效）
PTT_VKS = _vk_list(
    CONFIG.get("gate_ptt_keys", CONFIG.get("gate_ptt_key", "right ctrl")), "right ctrl")
TOGGLE_VKS = _vk_list(
    CONFIG.get("gate_toggle_keys", CONFIG.get("gate_toggle_key", "f9")), "f9")

_GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState


def _key_down(vk: int) -> bool:
    return bool(_GetAsyncKeyState(vk) & 0x8000)


_lock = threading.Lock()
_ptt_held = False      # 正在按住说话
_toggle_on = False     # 常听开关
_icon: pystray.Icon | None = None


def _gate_state() -> bool:
    """门控状态：按住说话或常听开即为开。"""
    return _toggle_on or _ptt_held


_base_img: Image.Image | None = None


def _load_base() -> Image.Image:
    """加载桌面快捷方式同款图标（voice-assistant.ico）作为托盘底图。"""
    global _base_img
    if _base_img is None:
        ico = BASE_DIR.parent / "voice-assistant.ico"
        _base_img = Image.open(ico).convert("RGBA").resize((64, 64), Image.LANCZOS)
    return _base_img


def _make_image(open_: bool) -> Image.Image:
    """以快捷方式图标为底，按门控状态着色：开=绿调，关=红调。"""
    base = _load_base()
    gray = base.convert("L")
    color = (46, 204, 113) if open_ else (231, 76, 60)
    tinted = ImageOps.colorize(gray, black=(10, 10, 10), mid=color, white=(255, 255, 255))
    tinted = tinted.convert("RGBA")
    tinted.putalpha(base.getchannel("A"))
    return tinted


def _write_gate(open_: bool) -> None:
    """原子写门控状态文件（s2s 音频入口补丁按 mtime 缓存读取）。"""
    try:
        tmp = GATE_STATE.with_suffix(".tmp")
        tmp.write_text("1" if open_ else "0", encoding="ascii")
        os.replace(tmp, GATE_STATE)
    except Exception:
        pass  # 写失败时音频侧保持上次状态，下次切换再同步


def _sync() -> None:
    """写门控状态文件（音频入口立即生效）并换托盘图标。只变色，不弹通知。"""
    open_ = _gate_state()
    _write_gate(open_)
    if _icon is not None:
        _icon.icon = _make_image(open_)
        _icon.title = "语音门控：" + ("开（可以说话）" if open_ else "关（静音中）")


def _ptt_down() -> None:
    global _ptt_held
    with _lock:
        if _ptt_held:
            return
        _ptt_held = True
        _sync()


def _ptt_up() -> None:
    global _ptt_held
    with _lock:
        if not _ptt_held:
            return
        _ptt_held = False
        _sync()   # 立即断音；在途那句已在管道里，会自然走完


def _toggle() -> None:
    global _toggle_on
    with _lock:
        _toggle_on = not _toggle_on
        _sync()


def _poll_loop() -> None:
    """轮询按键状态，边沿触发（按下/松开/点按各触发一次）。任一绑定键生效。"""
    prev_ptt = any(_key_down(vk) for vk in PTT_VKS)
    prev_toggle = any(_key_down(vk) for vk in TOGGLE_VKS)
    while True:
        time.sleep(POLL_SEC)
        ptt = any(_key_down(vk) for vk in PTT_VKS)
        toggle = any(_key_down(vk) for vk in TOGGLE_VKS)
        if ptt and not prev_ptt:
            _ptt_down()
        elif not ptt and prev_ptt:
            _ptt_up()
        if toggle and not prev_toggle:
            _toggle()
        prev_ptt, prev_toggle = ptt, toggle


def _on_full_stop(icon, item) -> None:
    """调 voice-launcher 的 --stop 分支，杀掉语音助手全部进程（含本助手）。

    直接用 pythonw 启动，不经过 powershell，避免退出时闪现终端窗口。
    """
    launcher = BASE_DIR / "voice-launcher.pyw"
    pythonw = BASE_DIR.parent / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(launcher), "--stop"],
                     creationflags=subprocess.CREATE_NO_WINDOW)


def main() -> None:
    global _icon
    # 单实例保护：两个实例同时轮询会导致一次按键触发两次切换（等于没切）
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "speech-to-kimi-code-mic-gate")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    menu = pystray.Menu(
        pystray.MenuItem("退出", _on_full_stop, default=True),
    )
    _icon = pystray.Icon("mic-gate", _make_image(_gate_state()), "语音门控：关（静音中）", menu)
    _sync()
    _icon.run()


if __name__ == "__main__":
    main()
