"""pywebview 窗口外观辅助（Windows）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors
"""

import ctypes
from ctypes import wintypes

_GWL_STYLE = -16
_GWL_EXSTYLE = -20
_GWLP_WNDPROC = -4
_WS_CAPTION = 0x00C00000
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_SYSMENU = 0x00080000
_WS_EX_DLGMODALFRAME = 0x00000001
_WS_EX_WINDOWEDGE = 0x00000100
_WS_EX_STATICEDGE = 0x00020000
_WM_NCCALCSIZE = 0x0083
_WM_NCHITTEST = 0x0084
_WM_NCPAINT = 0x0085
_WM_NCACTIVATE = 0x0086
_HTCLIENT = 1
# NOSIZE | NOMOVE | NOZORDER | FRAMECHANGED
_SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0020
_SPI_GETWORKAREA = 0x0030

_u32 = ctypes.windll.user32
# 64 位原型必须显式声明：默认 restype 是 c_int，窗口过程指针会被截断，
# CallWindowProcW 转到坏地址 = 整个窗口消息处理瘫痪（关窗/隐藏全部失灵）
_u32.GetWindowLongPtrW.restype = ctypes.c_void_p
_u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.SetWindowLongPtrW.restype = ctypes.c_void_p
_u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_u32.CallWindowProcW.restype = ctypes.c_ssize_t
_u32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM,
]

_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM
)
_procs = {}  # hwnd -> (callback, old_proc)，持引用防 GC


def _find_form(title: str):
    """按标题找 pywebview 的 WinForms 宿主窗口（类名校验，防误伤同名窗口）。"""
    hwnd = _u32.FindWindowW(None, title)
    if not hwnd:
        return None
    buf = ctypes.create_unicode_buffer(64)
    _u32.GetClassNameW(hwnd, buf, 64)
    return hwnd if buf.value.startswith("WindowsForms") else None


def centered_position(width: int, height: int, y_bias: int = -28):
    """主屏工作区内居中（略上偏，视觉重心更自然）。失败返回 (None, None)。"""
    try:
        rect = wintypes.RECT()
        if _u32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            x = rect.left + max(0, (rect.right - rect.left - width) // 2)
            y = rect.top + max(0, (rect.bottom - rect.top - height) // 2 + y_bias)
            return x, y
    except Exception:
        pass
    return None, None


def style_window(title: str) -> None:
    """把 pywebview 窗口改成无边框直角 + DWM 真投影。

    不做圆角：DWM 原生圆角是 Win11 特性；Win10 上唯一的 SetWindowRgn 裁剪
    边缘硬切、没有抗锯齿（坑坑洼洼），故统一保持干净直角。

    投影不要用 pywebview 的 shadow=True：它走 CS_DROPSHADOW，会描出矩形
    残边。这里用标准配方：加 WS_THICKFRAME 让 DWM 画投影，子类化
    WM_NCCALCSIZE 把边框宽度吃成零（不可见、也不可拖拽改尺寸）。
    窗口不可缩放，shown 时做一次即可。
    """
    try:
        hwnd = _find_form(title)
        if not hwnd:
            return

        # 1) 子类化：非客户区归零 + 命中全算客户区（否则厚边框会被拖到改尺寸）
        if hwnd not in _procs:
            old_proc = _u32.GetWindowLongPtrW(hwnd, _GWLP_WNDPROC)

            def _proc(h, msg, wp, lp):
                if msg == _WM_NCCALCSIZE and wp:
                    return 0  # 客户区 = 整个窗口，边框归零
                if msg == _WM_NCHITTEST:
                    return _HTCLIENT
                if msg == _WM_NCACTIVATE:
                    return 1  # 不触发边框重绘
                if msg == _WM_NCPAINT:
                    return 0  # 非客户区不画任何东西
                return _u32.CallWindowProcW(_procs[h][1], h, msg, wp, lp)

            cb = _WNDPROC(_proc)
            _procs[hwnd] = (cb, old_proc)
            _u32.SetWindowLongPtrW(
                hwnd, _GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p)
            )

        # 2) 加 WS_THICKFRAME（DWM 投影的前提）+ WS_MINIMIZEBOX/WS_SYSMENU
        #    （没有它们 Explorer 认为窗口不可最小化，点任务栏按钮不会收起），
        #    剥掉会画出边线的扩展样式
        style = _u32.GetWindowLongW(hwnd, _GWL_STYLE)
        style = (style | _WS_THICKFRAME | _WS_MINIMIZEBOX | _WS_SYSMENU) & ~_WS_CAPTION
        _u32.SetWindowLongW(hwnd, _GWL_STYLE, style)
        exstyle = _u32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        exstyle &= ~(_WS_EX_WINDOWEDGE | _WS_EX_DLGMODALFRAME | _WS_EX_STATICEDGE)
        _u32.SetWindowLongW(hwnd, _GWL_EXSTYLE, exstyle)
        _u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_FLAGS)
    except Exception:
        pass


def bring_to_front(title: str) -> None:
    """把窗口抬到最前并抢前台焦点。

    系统对后台进程抢前台有限制（SetForegroundWindow 常静默失败），可靠配方：
    先短暂置顶再取消（抬 Z 序、激活窗口），再用 AttachThreadInput 挂上当前
    前台线程的输入队列后抢焦点。
    """
    try:
        hwnd = _find_form(title)
        if not hwnd:
            return
        flags = 0x0001 | 0x0002  # NOSIZE | NOMOVE
        _u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, flags)   # HWND_TOPMOST
        _u32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, flags)   # HWND_NOTOPMOST
        k32 = ctypes.windll.kernel32
        fg = _u32.GetForegroundWindow()
        tid_fg = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_cur = k32.GetCurrentThreadId()
        attached = bool(tid_fg) and tid_fg != tid_cur
        if attached:
            _u32.AttachThreadInput(tid_cur, tid_fg, True)
        try:
            _u32.ShowWindow(hwnd, 5)  # SW_SHOW
            _u32.SetForegroundWindow(hwnd)
            _u32.SetActiveWindow(hwnd)
        finally:
            if attached:
                _u32.AttachThreadInput(tid_cur, tid_fg, False)
    except Exception:
        pass
