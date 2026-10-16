"""mic-gate：语音门控热键 + 托盘助手。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

门控位置在**音频入口**（voice-core.py 的音频回调）：本助手只负责把门控
状态原子写入 voice-bridge/gate.state，voice-core 读到 "0" 就直接丢弃
音频帧、"1"/"2" 才放行真实音频进 VAD。

gate.state 三个值：
- "0" 关门（静音），默认状态；
- "1" 常听开门（点按右 Ctrl 切换）；
- "2" 按住说话开门（长按右 Ctrl 期间）——此期间 VAD 不切段提交，停顿再久也
  全部收录，松开后把按住期间说的所有话作为一整轮**立即**处理提交
  （voice-core.py 的分段器实现）；常听中长按说完
  松开 = 连常听一起关掉并立即提交（说完即收工）。

同一个键同时出现在 gate_ptt_keys 和 gate_toggle_keys 里就是双模键：
点按（< TAP_MAX_SEC）= 切换常听开/关；长按 = 按住说话，松开断开。
默认配置里右 Ctrl 就是这个双模键。只出现在其中一个列表里的键保持单一功能
（如鼠标侧键纯按住说话）。所有切换**立即生效、没有宽限期**。

系统托盘图标 = 门控状态（绿=收音中，红=静音，预渲染双态图，
见 scripts/build_icon.py），只变图、不弹通知；开门且 VAD 检出真实语音时
播放说话动画（ping-pong 循环 tray_talk_* 帧；信号 = voice-core
维护的 speak.state mtime 新鲜度，说话一停自动回静态绿/红）。
右键「退出」调 voice-launcher --stop 杀掉语音助手全部进程（含本助手）。
左键打开控制面板。

默认关门：语音助手启动后，不按键时任何声音都进不了 VAD。

实现说明：按键检测用 Win32 GetAsyncKeyState 轮询（50ms），
不用全局钩子——钩子的事件名在不同键盘/驱动下不稳定
（右 Ctrl 可能被报成 'ctrl' 导致松开事件丢失），轮询原始按键状态最可靠。

运行（由 voice-launcher.pyw 在全部就绪后拉起）：
    .venv\\Scripts\\pythonw.exe voice-bridge\\mic-gate.pyw
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time

import pystray
from PIL import Image

from envutil import bridge_dir, project_root
from runtime_state import consume_gate_cmd, open_or_show_panel, stop_requested

BASE_DIR = bridge_dir()
CONFIG = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))

GATE_STATE = BASE_DIR / "gate.state"
POLL_SEC = 0.05
TAP_MAX_SEC = 0.4   # 双模键点按/长按分界：短于此算点按（切换常听）

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


# 按住说话键 / 常听开关键，都可配置多个（任一触发即生效）；
# 同键同列两表 = 双模键（点按切换、长按说话）
PTT_VKS = _vk_list(
    CONFIG.get("gate_ptt_keys", CONFIG.get("gate_ptt_key", "right ctrl")), "right ctrl")
TOGGLE_VKS = _vk_list(
    CONFIG.get("gate_toggle_keys", CONFIG.get("gate_toggle_key", "right ctrl")), "right ctrl")
DUAL_VKS = [vk for vk in PTT_VKS if vk in TOGGLE_VKS]
PURE_PTT_VKS = [vk for vk in PTT_VKS if vk not in TOGGLE_VKS]
PURE_TOGGLE_VKS = [vk for vk in TOGGLE_VKS if vk not in PTT_VKS]

_GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState


def _key_down(vk: int) -> bool:
    return bool(_GetAsyncKeyState(vk) & 0x8000)


_lock = threading.Lock()
_ptt_held = False      # 正在按住说话
_toggle_on = False     # 常听开关
_icon: pystray.Icon | None = None


def _gate_value() -> str:
    """门控状态："2"=按住说话 / "1"=常听开 / "0"=关。"""
    if _ptt_held:
        return "2"
    return "1" if _toggle_on else "0"


def _dual_release_kind(down_since: float, now: float) -> str:
    """双模键松开时判定本次是点按（切换常听）还是长按（按住说话结束）。"""
    return "tap" if now - down_since < TAP_MAX_SEC else "hold"


ICONS_DIR = BASE_DIR / "icons"

SPEAK_STATE = BASE_DIR / "speak.state"
SPEAK_FRESH_SEC = 0.35   # speak.state mtime 在此窗口内 = VAD 正检出真实语音
ANIM_EVERY_TICKS = 3     # 每 3 个轮询周期（150ms）换一帧

_base_imgs: dict[bool, Image.Image] = {}
_talk_imgs: list[Image.Image] = []
_TALK_SEQ = (0, 1, 2, 1)  # 动画帧顺序：ping-pong 来回摆


def _make_image(open_: bool) -> Image.Image:
    """托盘图标：预渲染双态（build_icon.py 生成）——绿=收音中，红=静音。"""
    if open_ not in _base_imgs:
        name = "tray_on.png" if open_ else "tray_off.png"
        _base_imgs[open_] = Image.open(ICONS_DIR / name).convert("RGBA")
    return _base_imgs[open_]


def _talk_frame(tick: int) -> Image.Image:
    """说话动画帧：预渲染三帧（build_icon.py 生成），ping-pong 循环。"""
    if not _talk_imgs:
        for i in range(3):
            _talk_imgs.append(Image.open(ICONS_DIR / f"tray_talk_{i}.png").convert("RGBA"))
    return _talk_imgs[_TALK_SEQ[tick % len(_TALK_SEQ)]]


def _speaking() -> bool:
    """speak.state 的 mtime 足够新 = VAD 当前正检出真实语音。"""
    try:
        return (time.time() - os.path.getmtime(SPEAK_STATE)) < SPEAK_FRESH_SEC
    except OSError:
        return False


def _write_gate(value: str) -> None:
    """原子写门控状态文件（voice-core 按 mtime 缓存读取）。

    Windows 上杀软/搜索索引可能在 replace 瞬间锁住目标文件（PermissionError），
    快速连写（点按双模键一次松手会连写 "2"→"1"→"0"）时尤其容易撞锁。
    失败重试几次；仍失败不吞——打印到 err.log，由轮询循环的看门狗兜底自愈，
    否则会出现「托盘红色静音、gate.state 却是开门、语音照收」的状态分裂。
    """
    for _ in range(3):
        try:
            tmp = GATE_STATE.with_suffix(".tmp")
            tmp.write_text(value, encoding="ascii")
            os.replace(tmp, GATE_STATE)
            return
        except OSError:
            time.sleep(0.05)
    print(f"[mic-gate] gate.state 写入失败（值 {value!r}），等待看门狗重试",
          file=sys.stderr, flush=True)


def _gate_on_disk() -> str:
    try:
        return GATE_STATE.read_text(encoding="ascii").strip()
    except OSError:
        return ""


_TITLES = {
    "0": "语音门控：关（静音中）",
    "1": "语音门控：开（常听中）",
    "2": "语音门控：开（按住说话中）",
}


def _sync() -> None:
    """写门控状态文件（音频入口立即生效）并换托盘图标。只变色，不弹通知。"""
    value = _gate_value()
    _write_gate(value)
    if _icon is not None:
        _icon.icon = _make_image(value != "0")
        _icon.title = _TITLES[value]


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


def _set_listen(on: bool) -> None:
    """控制面板请求开/关常听（不直接写 gate.state）。"""
    global _toggle_on
    with _lock:
        if _toggle_on == on:
            return
        _toggle_on = on
        _sync()


def _release_dual(kind: str) -> None:
    """双模键松开：先算清最终状态再一次性 _sync（一次写文件、一次换图标）。

    拆成 _ptt_up()+_toggle() 会在一次松开里连写两次 gate.state——点按开门
    的中间态是 "0"，托盘图标绿→红→绿闪一帧（图标更新是否被 Windows 合并
    看机器时序，MateBook 上肉眼可见）；中间态 "0" 若被 voice-core 读到
    还会误触发关门 flush。"""
    global _ptt_held, _toggle_on
    with _lock:
        _ptt_held = False
        if kind == "tap":
            _toggle_on = not _toggle_on
        elif _toggle_on:
            _toggle_on = False  # 常听中长按说完：松开连常听一起关掉
        _sync()


def _poll_loop() -> None:
    """轮询按键状态，边沿触发。双模键：按下即开收音（长按不丢开头），
    松开时按持有时长区分点按（切换常听）和长按（结束说话）。
    同循环驱动托盘说话动画：开门且 VAD 检出真实语音时循环换帧。"""
    all_vks = PURE_PTT_VKS + PURE_TOGGLE_VKS + DUAL_VKS
    prev = {vk: _key_down(vk) for vk in all_vks}
    dual_down_since: dict[int, float] = {}
    anim_tick = 0
    anim_active = False
    boot = time.time()
    while True:
        time.sleep(POLL_SEC)
        if stop_requested(since=boot):
            _quit_tray()
            return
        cmd = consume_gate_cmd()
        if cmd == "0":
            _set_listen(False)
        elif cmd == "1":
            _set_listen(True)

        # 看门狗：每秒核对一次「文件 vs 内部状态」。_write_gate 撞锁重试
        # 仍失败时这里兜底重写，保证托盘图标、gate.state、实际收音三者一致
        if anim_tick % 20 == 0 and _gate_on_disk() != _gate_value():
            print(f"[mic-gate] gate.state 与内部状态不一致，重写为 {_gate_value()!r}",
                  file=sys.stderr, flush=True)
            _write_gate(_gate_value())
        now = time.monotonic()
        for vk in PURE_PTT_VKS:
            down = _key_down(vk)
            if down and not prev[vk]:
                _ptt_down()
            elif not down and prev[vk]:
                _ptt_up()
            prev[vk] = down
        for vk in PURE_TOGGLE_VKS:
            down = _key_down(vk)
            if down and not prev[vk]:
                _toggle()
            prev[vk] = down
        for vk in DUAL_VKS:
            down = _key_down(vk)
            if down and not prev[vk]:
                dual_down_since[vk] = now
                _ptt_down()   # 先按按住说话开门，点按也不会丢失开头
            elif not down and prev[vk]:
                since = dual_down_since.pop(vk, now)
                _release_dual(_dual_release_kind(since, now))
                # 点按=切换常听开/关；常听中长按说完：松开连常听一起关掉
                # （关门立即切段提交，由 voice-core 分段器保证）
            prev[vk] = down

        # 说话动画：只在开门且 VAD 检出真实语音时播；停止说话回静态图标
        anim_tick += 1
        if _icon is not None and anim_tick % ANIM_EVERY_TICKS == 0:
            if _gate_value() != "0" and _speaking():
                _icon.icon = _talk_frame(anim_tick // ANIM_EVERY_TICKS)
                anim_active = True
            elif anim_active:
                anim_active = False
                _icon.icon = _make_image(_gate_value() != "0")


def _quit_tray() -> None:
    """先注销托盘图标再退出，避免 Explorer 留下幽灵图标。"""
    icon = _icon
    if icon is None:
        return
    try:
        icon.visible = False
    except Exception:
        pass
    try:
        icon.stop()
    except Exception:
        pass


def _on_full_stop(icon, item) -> None:
    """调 voice-launcher 的 --stop 分支，杀掉语音助手全部进程（含本助手）。

    直接用 pythonw 启动，不经过 powershell，避免退出时闪现终端窗口。
    """
    launcher = BASE_DIR / "voice-launcher.pyw"
    pythonw = project_root() / ".venv" / "Scripts" / "pythonw.exe"
    subprocess.Popen([str(pythonw), str(launcher), "--stop"],
                     creationflags=subprocess.CREATE_NO_WINDOW)


def _open_panel(icon=None, item=None) -> None:
    open_or_show_panel()


def main() -> None:
    global _icon, _MUTEX
    # 单实例保护：两个实例同时轮询会导致一次按键触发两次切换（等于没切）
    _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, False, "MicToAgent-mic-gate")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()

    # default=True：左键单击/双击打开控制面板（不要绑在「退出」上）。
    menu = pystray.Menu(
        pystray.MenuItem("打开控制面板", _open_panel, default=True),
        pystray.MenuItem("退出", _on_full_stop),
    )
    _icon = pystray.Icon("mic-gate", _make_image(_gate_value() != "0"), _TITLES["0"], menu)
    _sync()
    _icon.run()


_MUTEX = None


if __name__ == "__main__":
    main()
