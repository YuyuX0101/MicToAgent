"""把语音文字粘贴进 Kimi Code Web 输入框。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

不激活浏览器、不代点输入框。键只打给当前前台：调用方必须保证用户正看着
Kimi Web 且光标已在输入框里。否则返回错误码，由适配器弹 Windows 通知，
不重试（语音提交非幂等）。

剪贴板里若已有图片，先 Ctrl+V 把图贴进输入框，再写入识别文字并粘贴，
最后尽量恢复发送前的剪贴板。submit=True 时再回车发出；False 则停在输入框。
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes

from target_resolver import auto, foreground_kimi_state

logger = logging.getLogger("voice-bridge.draft")

_HAS_WIN = hasattr(ctypes, "windll")

_CF_BITMAP = 2
_CF_DIB = 8
_CF_DIBV5 = 17
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_VK_CONTROL = 0x11
_VK_V = 0x56
_VK_RETURN = 0x0D
_KEYEVENTF_KEYUP = 0x0002
_GA_ROOT = 2
_IMAGE_BITMAP = 0
_LR_CREATEDIBSECTION = 0x2000
_TOOLBAR_PX = 90

# 测试可把间隔改成 0
_IMAGE_SETTLE_SEC = 0.25
_KEY_GAP_SEC = 0.05

_OMNIBOX_HINTS = (
    "address",
    "search bar",
    "omnibox",
    "地址",
    "搜索栏",
    "网站搜索",
    "address and search",
)

OK = "ok"
NOT_FOREGROUND = "not_foreground"
NOT_COMPOSER = "not_composer"
CLIPBOARD = "clipboard"
UNAVAILABLE = "unavailable"

PASTE_NOTICES = {
    NOT_FOREGROUND: "请先切到 Kimi Web 再说话",
    NOT_COMPOSER: "请先点一下输入框再说话",
    CLIPBOARD: "没能写入剪贴板，语音没有进输入框",
    UNAVAILABLE: "当前环境无法粘贴到输入框",
}


def is_composer_focus(
    *,
    control_type: str,
    name: str,
    top_inset: int,
    control_height: int,
    window_height: int,
) -> bool:
    """根据 UIA 焦点控件判断是不是网页输入框（不是地址栏、不是整页文档）。"""
    name_l = (name or "").lower()
    if any(hint in name_l for hint in _OMNIBOX_HINTS):
        return False
    if top_inset < _TOOLBAR_PX:
        return False
    ct = (control_type or "").lower()
    if "edit" in ct or "combobox" in ct:
        return True
    if "document" in ct:
        if window_height and control_height > window_height * 0.75:
            return False
        return True
    if "custom" in ct:
        if 40 <= control_height <= 420:
            return True
    return False


def _send_keys(*vks: int) -> None:
    user32 = ctypes.windll.user32
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


def _png_format() -> int:
    return ctypes.windll.user32.RegisterClipboardFormatW("PNG")


def _open_clipboard() -> bool:
    user32 = ctypes.windll.user32
    for _ in range(10):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def _dup_hglobal(handle: int) -> int | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    size = kernel32.GlobalSize(handle)
    if not size:
        return None
    newh = kernel32.GlobalAlloc(_GMEM_MOVEABLE, size)
    if not newh:
        return None
    src = kernel32.GlobalLock(handle)
    dst = kernel32.GlobalLock(newh)
    if not src or not dst:
        if dst:
            kernel32.GlobalUnlock(newh)
        kernel32.GlobalFree(newh)
        return None
    ctypes.memmove(dst, src, size)
    kernel32.GlobalUnlock(handle)
    kernel32.GlobalUnlock(newh)
    return int(newh)


def _snapshot_clipboard() -> list[tuple[int, int]]:
    """复制当前剪贴板各格式的一份句柄（调用方负责在 restore 时移交或释放）。"""
    if not _HAS_WIN:
        return []
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not _open_clipboard():
        return []
    snap: list[tuple[int, int]] = []
    try:
        fmt = 0
        saw_dib = False
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            copied = None
            if fmt == _CF_BITMAP:
                if saw_dib:
                    continue
                copied = user32.CopyImage(
                    handle, _IMAGE_BITMAP, 0, 0, _LR_CREATEDIBSECTION
                )
            else:
                copied = _dup_hglobal(handle)
                if fmt in (_CF_DIB, _CF_DIBV5) and copied:
                    saw_dib = True
            if copied:
                snap.append((fmt, int(copied)))
        return snap
    except Exception:
        logger.debug("剪贴板快照失败", exc_info=True)
        for _, h in snap:
            try:
                kernel32.GlobalFree(h)
            except Exception:
                pass
        return []
    finally:
        user32.CloseClipboard()


def _snapshot_has_image(snap: list[tuple[int, int]]) -> bool:
    png = _png_format() if _HAS_WIN else 0
    return any(fmt in (_CF_BITMAP, _CF_DIB, _CF_DIBV5, png) for fmt, _ in snap)


def _restore_clipboard(snap: list[tuple[int, int]]) -> None:
    if not _HAS_WIN:
        return
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not _open_clipboard():
        for _, h in snap:
            try:
                kernel32.GlobalFree(h)
            except Exception:
                pass
        return
    try:
        user32.EmptyClipboard()
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        for fmt, handle in snap:
            if user32.SetClipboardData(fmt, handle):
                continue
            try:
                kernel32.GlobalFree(handle)
            except Exception:
                pass
    finally:
        user32.CloseClipboard()


def _set_clipboard_text(text: str) -> bool:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HANDLE
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        return False
    try:
        user32.EmptyClipboard()
        hglob = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not hglob:
            return False
        ptr = kernel32.GlobalLock(hglob)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(hglob)
        return bool(user32.SetClipboardData(_CF_UNICODETEXT, hglob))
    finally:
        user32.CloseClipboard()


def _rect_wh(rect) -> tuple[int, int, int]:
    """返回 (top, height, window-like height helper)."""
    top = int(getattr(rect, "top", 0) or 0)
    bottom = int(getattr(rect, "bottom", 0) or 0)
    if hasattr(rect, "height") and callable(rect.height):
        height = int(rect.height())
    else:
        height = max(0, bottom - top)
    return top, height, height


def _control_root_hwnd(ctrl) -> int:
    user32 = ctypes.windll.user32
    hwnd = 0
    cur = ctrl
    for _ in range(24):
        if cur is None:
            break
        try:
            hwnd = int(cur.NativeWindowHandle or 0)
        except Exception:
            hwnd = 0
        if hwnd:
            break
        try:
            cur = cur.GetParentControl()
        except Exception:
            break
    if not hwnd:
        return 0
    root = user32.GetAncestor(hwnd, _GA_ROOT)
    return int(root or hwnd)


def _composer_focused(fg_hwnd: int) -> bool:
    if auto is None or not fg_hwnd:
        return False
    try:
        focused = auto.GetFocusedControl()
    except Exception:
        return False
    if not focused:
        return False
    root = _control_root_hwnd(focused)
    if root and root != fg_hwnd:
        return False
    try:
        win = auto.ControlFromHandle(fg_hwnd)
        wr = win.BoundingRectangle
        fr = focused.BoundingRectangle
        w_top, w_h, _ = _rect_wh(wr)
        f_top, f_h, _ = _rect_wh(fr)
        top_inset = f_top - w_top
        return is_composer_focus(
            control_type=getattr(focused, "ControlTypeName", "") or "",
            name=getattr(focused, "Name", "") or "",
            top_inset=top_inset,
            control_height=f_h,
            window_height=w_h,
        )
    except Exception:
        logger.debug("判断输入框焦点失败", exc_info=True)
        return False


def _foreground_hwnd() -> int:
    if not _HAS_WIN:
        return 0
    return int(ctypes.windll.user32.GetForegroundWindow() or 0)


def paste_into_composer(text: str, port: int, submit: bool = True) -> str:
    """把文字（及已复制的图）粘贴进当前焦点输入框。

    返回 ok / not_foreground / not_composer / clipboard / unavailable。
    不 SetForegroundWindow，不寻找输入框。
    """
    if not _HAS_WIN or not text:
        return UNAVAILABLE

    inited = False
    try:
        import comtypes

        comtypes.CoInitialize()
        inited = True
    except Exception:
        pass
    try:
        state, _ = foreground_kimi_state(port)
        if state == "away":
            logger.info("前台不是 Kimi Web，取消粘贴")
            return NOT_FOREGROUND
        fg = _foreground_hwnd()
        if not _composer_focused(fg):
            logger.info("焦点不在输入框，取消粘贴")
            return NOT_COMPOSER

        snap = _snapshot_clipboard()
        has_image = _snapshot_has_image(snap)
        try:
            if has_image:
                _send_keys(_VK_CONTROL, _VK_V)
                time.sleep(_IMAGE_SETTLE_SEC)
            if not _set_clipboard_text(text):
                logger.warning("写剪贴板失败")
                return CLIPBOARD
            time.sleep(_KEY_GAP_SEC)
            _send_keys(_VK_CONTROL, _VK_V)
            time.sleep(_KEY_GAP_SEC)
            if submit:
                _send_keys(_VK_RETURN)
            logger.info(
                "已粘贴进输入框（%d 字, 图=%s, 回车=%s）",
                len(text),
                has_image,
                submit,
            )
            return OK
        finally:
            _restore_clipboard(snap)
    finally:
        if inited:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
