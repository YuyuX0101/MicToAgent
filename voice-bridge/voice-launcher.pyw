"""voice-launcher：语音助手启动向导（speech-to-kimi-code fork 原创）。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 speech-to-kimi-code contributors

浅色无边框圆角卡片，实时展示启动进度（四个阶段），全部就绪后自动关闭，
并在此时才拉起 mic-gate（托盘图标同步出现），保证"看见托盘=真正可用"。
右上角有最小化和关闭按钮；启动中点关闭 = 终止本次启动（杀掉已拉起的进程）。

用法：
    启动（桌面快捷方式） pythonw.exe voice-launcher.pyw
                        （未运行→启动向导；已在运行→提示卡，不做停止）
    停止（托盘右键「退出」） pythonw.exe voice-launcher.pyw --stop
                        （杀全部进程并弹停止卡）
    停止提示卡    pythonw.exe voice-launcher.pyw --stopped（仅显示提示卡，不杀进程）

阶段检测依据（均读日志文件，logging 走 stderr 最可靠）：
    1. 启动桥接适配器      → GET 127.0.0.1:9100/health 返回 200
    2. 加载运行依赖库      → err.log 出现 funasr 开始加载模型的日志
       （torch/funasr 导入期间无任何输出，冷启动 1-3 分钟，单独一阶段给反馈）
    3. 加载语音识别模型    → err.log 出现 "Loading Qwen3-TTS model"（Paraformer 之后）
    4. 加载语音合成模型    → err.log 出现 "Qwen3TTSHandler warmed up"
       （TTS 模型+CUDA graph+预热全部完成；不要靠 stdout 的 "Talker CUDA graph
       captured"，它由多进程子进程 print，重导向下可能丢失；
       也不要靠适配器 /ready，s2s 的预热 Hello 在 TTS 加载完成前就发出）
    5. 连接语音总管        → GET /ready 返回 true（此时预热 Hello 必然早已到达，
       只是最终确认链路通畅，通常立即通过）
"""

import ctypes
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageDraw, ImageTk

BASE = Path(__file__).resolve().parent          # voice-bridge 目录
PROJ = BASE.parent                               # speech-to-kimi-code 项目根
VENV_PY = PROJ / ".venv" / "Scripts" / "python.exe"
VENV_PYW = PROJ / ".venv" / "Scripts" / "pythonw.exe"
LOGS = PROJ / "logs"
LOGS.mkdir(exist_ok=True)

ADAPTER_HEALTH = "http://127.0.0.1:9100/health"
ADAPTER_READY = "http://127.0.0.1:9100/ready"

S2S_ARGS = [
    "--mode", "local",
    "--language", "zh",
    "--stt", "paraformer",
    "--llm_backend", "chat-completions",
    "--model_name", "voice-bridge",
    "--responses_api_base_url", "http://127.0.0.1:9100/v1",
    "--responses_api_api_key", "not-needed",
    "--responses_api_stream",
    "--tts", "qwen3",
    "--qwen3_tts_backend", "torch",
    "--qwen3_tts_device", "cuda",
    "--qwen3_tts_speaker", "Vivian",
    "--qwen3_tts_language", "Chinese",
    "--qwen3_tts_instruct", '"以平稳自然的语气说话，语速适中，像日常聊天一样，不要夸张、不要抑扬顿挫"',
    "--stream_batch_sentences", "1",
    # 收音节奏（默认太灵敏：64ms 静音就切段、1 秒就提交，说半句就被处理）。
    # 停顿 500ms 才切段；切段后 1.5 秒内继续说还算同一轮，超过才提交处理。
    "--min_silence_ms", "500",
    "--speculative_reopen_ms", "1500",
    "--log_level", "INFO",
]

STAGE_NAMES = [
    "启动桥接适配器",
    "加载运行依赖库",
    "加载语音识别模型",
    "加载语音合成模型",
    "连接语音总管",
]

# ---------- 样式（浅色） ----------
MAGIC = "#010203"          # 透明打孔色
CARD = "#ffffff"           # 卡片白
BORDER = "#e3e3e8"         # 卡片描边
FG = "#1c1c1e"             # 主文字
DIM = "#9a9aa0"            # 次要文字
FAINT = "#c9c9cf"          # 待办阶段
ACCENT = "#007aff"         # 进行中（蓝）
GREEN = "#34c759"          # 完成（绿）
RED = "#ff3b30"            # 失败（红）
TRACK = "#ececf1"          # 进度条轨道
FONT = "Microsoft YaHei UI"


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
            with urllib.request.urlopen(url, timeout=2) as r:
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
        self.on_stage(0, "active")
        self._spawn([str(VENV_PY), str(BASE / "adapter.py")], BASE,
                    "voice-bridge.out.log", "voice-bridge.err.log")
        if not self._wait(lambda: self._http_json(ADAPTER_HEALTH) is not None, 30):
            return None if self.aborted else self._fail("适配器 30 秒未响应")
        self.on_stage(0, "done")

        self.on_stage(1, "active")
        self._spawn([str(VENV_PY), str(PROJ / "speech-to-kimi-entry.py")] + S2S_ARGS,
                    PROJ, "voice-assistant.out.log", "voice-assistant.err.log",
                    env_extra={"HF_HUB_OFFLINE": "1"})
        # 依赖库导入（torch/funasr 等）期间日志没有任何输出，冷启动可达 1-3 分钟，
        # 单独占一个阶段让用户知道在干活。funasr 开始加载模型 = 导入完成。
        deps_done = lambda: (self._log_has("voice-assistant.err.log", "download models from model hub")
                             or self._log_has("voice-assistant.err.log", "Loading pretrained params")
                             or self._log_has("voice-assistant.err.log", "Loading Qwen3-TTS model"))
        if not self._wait(deps_done, 300):
            return None if self.aborted else self._fail("运行依赖库加载超时")
        self.on_stage(1, "done")

        self.on_stage(2, "active")
        if not self._wait(lambda: self._log_has("voice-assistant.err.log", "Loading Qwen3-TTS model"), 300):
            return None if self.aborted else self._fail("语音识别模型加载超时")
        self.on_stage(2, "done")

        self.on_stage(3, "active")
        tts_ready = lambda: (self._log_has("voice-assistant.err.log", "Qwen3TTSHandler warmed up")
                             or self._log_has("voice-assistant.err.log", "Warmup generation failed"))
        if not self._wait(tts_ready, 300):
            return None if self.aborted else self._fail("语音合成模型加载超时")
        self.on_stage(3, "done")

        self.on_stage(4, "active")
        ready = lambda: bool((self._http_json(ADAPTER_READY) or {}).get("ready"))
        if not self._wait(ready, 120):
            return None if self.aborted else self._fail("语音总管预热超时")
        self.on_stage(4, "done")

        # 就绪：此刻才拉起门控助手（托盘图标出现 = 真正可用）
        self._spawn([str(VENV_PYW), str(BASE / "mic-gate.pyw")], BASE,
                    "mic-gate.out.log", "mic-gate.err.log")
        self.on_done()


# ---------- 界面 ----------

def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Card:
    """无边框圆角浅色卡片基类：居中、可拖动、带最小化/关闭按钮。

    不用 overrideredirect（那种窗口没有任务栏按钮，最小化后会"消失"），
    而是用普通窗口 + ctypes 剥掉标题栏/边框：保留任务栏按钮和原生最小化，
    点最小化后正常收到任务栏，点任务栏图标即可恢复。
    """

    W, H = 380, 480

    def __init__(self, title_buttons=True):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("语音助手")
        self.root.configure(bg=MAGIC)
        self.root.update_idletasks()          # 确保外层 frame 句柄已创建
        self._strip_frame()
        self.root.attributes("-transparentcolor", MAGIC, "-topmost", True)
        try:
            self.root.iconbitmap(str(PROJ / "voice-assistant.ico"))
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self.close)  # 任务栏右键关闭也要走 close()
        self._center()
        self._drag = None

        c = tk.Canvas(self.root, width=self.W, height=self.H, bg=MAGIC,
                      highlightthickness=0, bd=0)
        c.pack()
        _round_rect(c, 2, 2, self.W - 2, self.H - 2, 28, fill=CARD, outline=BORDER, width=1)
        self.canvas = c

        if title_buttons:
            self._add_title_buttons()

        for w in (self.root, c):
            w.bind("<Button-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        self.root.deiconify()
        # 剥框后外层尺寸可能有几个像素的残留偏移，重新校正一次
        self.root.update_idletasks()
        self._center()

    def _strip_frame(self):
        """剥掉系统标题栏/边框（保留任务栏按钮，替代 overrideredirect）。"""
        u32 = ctypes.windll.user32
        try:
            hwnd = int(self.root.wm_frame(), 16)   # 最外层 frame 的 HWND
        except Exception:
            hwnd = u32.GetParent(self.root.winfo_id())
        GWL_STYLE, GWL_EXSTYLE = -16, -20
        WS_CAPTION, WS_THICKFRAME, WS_MAXIMIZEBOX = 0x00C00000, 0x00040000, 0x00010000
        WS_EX_WINDOWEDGE = 0x00000100
        style = u32.GetWindowLongW(hwnd, GWL_STYLE)
        style &= ~(WS_CAPTION | WS_THICKFRAME | WS_MAXIMIZEBOX)
        u32.SetWindowLongW(hwnd, GWL_STYLE, style)
        exstyle = u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        exstyle &= ~WS_EX_WINDOWEDGE
        u32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle)
        # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED：触发框架重算，立即使新样式生效
        u32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)

    def _center(self):
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"{self.W}x{self.H}+{(sw - self.W) // 2}+{(sh - self.H) // 2 - 40}")

    def _add_title_buttons(self):
        c = self.canvas
        y = 22
        x_min, x_cls = self.W - 66, self.W - 34
        # 先画与卡片同色的隐形热区（细线太小点不中），再画符号
        c.create_rectangle(x_min - 13, y - 11, x_min + 13, y + 11,
                           fill=CARD, outline="", tags=("btnmin",))
        c.create_rectangle(x_cls - 13, y - 11, x_cls + 13, y + 11,
                           fill=CARD, outline="", tags=("btncls",))
        # 最小化：短横线（浏览器风格，长约 10px，不是 em 长横）
        c.create_line(x_min - 5, y, x_min + 5, y, fill=DIM, width=2,
                      capstyle=tk.ROUND, tags=("btnmin",))
        # 关闭：两条交叉线（约 10px）
        d = 5
        c.create_line(x_cls - d, y - d, x_cls + d, y + d, fill=DIM, width=2,
                      capstyle=tk.ROUND, tags=("btncls",))
        c.create_line(x_cls - d, y + d, x_cls + d, y - d, fill=DIM, width=2,
                      capstyle=tk.ROUND, tags=("btncls",))
        c.tag_bind("btnmin", "<Button-1>", lambda e: self.minimize())
        c.tag_bind("btncls", "<Button-1>", lambda e: self.close())
        for tag in ("btnmin", "btncls"):
            c.tag_bind(tag, "<Enter>", lambda e, t=tag: c.itemconfigure(t, fill=FG))
            c.tag_bind(tag, "<Leave>", lambda e, t=tag: c.itemconfigure(t, fill=DIM))

    def minimize(self):
        """原生最小化：收到任务栏，点任务栏图标恢复。"""
        self.root.iconify()

    def close(self):
        self.root.destroy()

    def _drag_start(self, e):
        self._drag = (e.x, e.y)

    def _drag_move(self, e):
        if self._drag:
            self.root.geometry(f"+{self.root.winfo_x() + e.x - self._drag[0]}"
                               f"+{self.root.winfo_y() + e.y - self._drag[1]}")


class LauncherWindow(Card):
    W, H = 380, 500   # 5 个阶段，比通用卡片加高 20，避免步骤条溢出压到页脚

    def __init__(self, on_close):
        super().__init__(title_buttons=True)
        self.on_close = on_close
        c = self.canvas

        img = Image.open(PROJ / "voice-assistant.ico").convert("RGBA").resize((64, 64), Image.LANCZOS)
        self.icon_img = ImageTk.PhotoImage(img)
        c.create_image(self.W // 2, 84, image=self.icon_img)

        # 进度环（绕图标一圈）：浅灰轨道 + 蓝色进度弧从 12 点顺时针填充。
        # tkinter Canvas 画弧线不抗锯齿（有毛边），改用 PIL 4 倍超采样渲染
        # 成位图再贴上来，环和 spinner 都是顺滑矢量画质
        self._ring_cx, self._ring_cy, self._ring_r = self.W // 2, 84, 43
        self._frac_target = 0.0
        self._frac_shown = 0.0
        self._ring_photo = self._render_ring(0)
        self._ring_item = c.create_image(self._ring_cx, self._ring_cy,
                                         image=self._ring_photo)

        c.create_text(self.W // 2, 146, text="语音助手", fill=FG,
                      font=(FONT, 16, "bold"))
        c.create_text(self.W // 2, 170, text="speech-to-kimi-code", fill=DIM,
                      font=(FONT, 9))

        self.status = c.create_text(self.W // 2, 198, text="", fill=ACCENT,
                                    font=(FONT, 9))

        # 阶段步骤条（竖向 stepper）：左对齐，状态图标 + 连接线 + 阶段名。
        # 图标全部 PIL 超采样渲染：pending=灰空心环，active=旋转弧，
        # done=绿底白勾，error=红底白叉
        self._icon_done = self._stage_icon("done")
        self._icon_pending = self._stage_icon("pending")
        self._icon_error = self._stage_icon("error")
        # 按最长阶段名实测宽度，把整个步骤条块（图标+文字）水平居中
        stage_font = tkfont.Font(family=FONT, size=10)
        text_w = max(stage_font.measure(n) for n in STAGE_NAMES)
        self._icon_x = (self.W - text_w - 22) // 2 + 10   # 22=图标半径10+图文间距12
        self._stage_y0 = 236
        self._stage_gap = 37
        for i in range(len(STAGE_NAMES) - 1):   # 连接线（图标盖住两端）
            y1 = self._stage_y0 + i * self._stage_gap + 11
            y2 = self._stage_y0 + (i + 1) * self._stage_gap - 11
            c.create_line(self._icon_x, y1, self._icon_x, y2, fill=TRACK, width=2)
        self.stage_icons = []
        self.stage_items = []
        for i, name in enumerate(STAGE_NAMES):
            y = self._stage_y0 + i * self._stage_gap
            self.stage_icons.append(
                c.create_image(self._icon_x, y, image=self._icon_pending))
            self.stage_items.append(
                c.create_text(self._icon_x + 22, y, text=name, anchor="w",
                              fill=FAINT, font=(FONT, 10)))

        # 使用提示
        c.create_line(60, 416, self.W - 60, 416, fill=TRACK)
        c.create_text(self.W // 2, 436, text="按住 右 Ctrl 说话 · F9 常听开关",
                      fill=DIM, font=(FONT, 9))
        c.create_text(self.W // 2, 456, text="托盘图标右键 → 退出",
                      fill=DIM, font=(FONT, 9))

        self._active = None
        self._spinner_row = None
        self._spin_angle = 0
        self._animating = False

    def close(self):
        self.on_close()
        super().close()

    def set_stage(self, idx, state):
        if state == "active":
            self._active = idx
            self._spinner_row = idx   # 该行图标位换成旋转弧（动画循环里逐帧更新）
            self.canvas.itemconfigure(self.stage_items[idx], fill=FG)
            if not self._animating:
                self._animating = True
                self._animate()
        elif state == "done":
            self.canvas.itemconfigure(self.stage_items[idx], fill=DIM)
            self.canvas.itemconfigure(self.stage_icons[idx], image=self._icon_done)
            if self._spinner_row == idx:
                self._spinner_row = None
            self._set_progress((idx + 1) / len(STAGE_NAMES))

    # ---------- 抗锯齿渲染（PIL 4x 超采样 + LANCZOS 缩小） ----------

    @staticmethod
    def _aa_image(box, stroke_w, draw_fn, ss=4):
        """box=逻辑边长，stroke_w=逻辑线宽；draw_fn(draw, p, w, s) 在高分辨率画布上作画。
        p=内边距(高分)，w=线宽(高分)，s=超采样倍数。"""
        img = Image.new("RGBA", (box * ss, box * ss))
        draw_fn(ImageDraw.Draw(img), 0, stroke_w * ss, ss)
        return ImageTk.PhotoImage(img.resize((box, box), Image.LANCZOS))

    def _render_ring(self, deg):
        r = self._ring_r
        box = 2 * (r + 5)

        def paint(d, p, w, s):
            cx = cy = box * s // 2
            rr = r * s
            bbox = [cx - rr, cy - rr, cx + rr, cy + rr]
            d.ellipse(bbox, outline=(236, 236, 241, 255), width=w)      # 轨道
            if deg >= 1:
                d.arc(bbox, start=-90, end=-90 + deg,
                      fill=(0, 122, 255, 255), width=w)                  # 进度弧
                for a in (-90, -90 + deg):                               # 两端圆角
                    rad = math.radians(a)
                    x, y = cx + rr * math.cos(rad), cy + rr * math.sin(rad)
                    d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2],
                              fill=(0, 122, 255, 255))

        return self._aa_image(box, 3, paint)

    @staticmethod
    def _stage_icon(kind):
        """步骤条状态图标：pending=灰空心环，done=绿底白勾，error=红底白叉。"""
        box = 20

        def paint(d, p, w, s):
            c = box * s // 2
            r = 8 * s
            if kind == "pending":
                d.ellipse([c - r, c - r, c + r, c + r],
                          outline=(199, 199, 204, 255), width=2 * s)
                return
            bg = (52, 199, 89, 255) if kind == "done" else (255, 59, 48, 255)
            d.ellipse([c - r, c - r, c + r, c + r], fill=bg)
            lw = int(2.2 * s)
            white = (255, 255, 255, 255)

            def stroke(x1, y1, x2, y2):
                d.line([x1, y1, x2, y2], fill=white, width=lw)
                for x, y in ((x1, y1), (x2, y2)):   # 圆头
                    d.ellipse([x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2],
                              fill=white)

            if kind == "done":   # 对勾（两段折线）
                stroke(c - 3.8 * s, c + 0.2 * s, c - 1.0 * s, c + 3.0 * s)
                stroke(c - 1.0 * s, c + 3.0 * s, c + 4.2 * s, c - 3.0 * s)
            else:                # 叉
                stroke(c - 3.2 * s, c - 3.2 * s, c + 3.2 * s, c + 3.2 * s)
                stroke(c - 3.2 * s, c + 3.2 * s, c + 3.2 * s, c - 3.2 * s)

        return LauncherWindow._aa_image(box, 2, paint)

    @staticmethod
    def _render_spinner(angle):
        """加载指示：浅灰细环轨道 + 蓝色短弧彗尾（90°，圆头）匀速旋转。
        轨道和短弧用同一组同心圆环（环形 + 环形扇区）绘制，严格贴合无错位；
        8 倍超采样，小尺寸下也丝滑无锯齿。"""
        box = 20

        def paint(d, p, w, s):
            c = box * s // 2
            r_out, r_in = 9 * s, 7 * s          # 环宽 2px（逻辑）
            r_mid = (r_out + r_in) / 2
            outer = [c - r_out, c - r_out, c + r_out, c + r_out]
            inner = [c - r_in, c - r_in, c + r_in, c + r_in]
            clear = (0, 0, 0, 0)                # RGBA 直写 = 擦除
            blue = (0, 122, 255, 255)
            # 轨道细环
            d.ellipse(outer, fill=(229, 229, 234, 255))
            d.ellipse(inner, fill=clear)
            # 蓝色短弧：同一圆环上的 90° 扇区，两端补圆头
            d.pieslice(outer, start=angle, end=angle + 90, fill=blue)
            d.pieslice(inner, start=angle, end=angle + 90, fill=clear)
            for a in (angle, angle + 90):
                rad = math.radians(a)
                x, y = c + r_mid * math.cos(rad), c + r_mid * math.sin(rad)
                d.ellipse([x - s, y - s, x + s, y + s], fill=blue)

        return LauncherWindow._aa_image(box, 2, paint, ss=8)

    # ---------- 动画 ----------

    def _animate(self):
        """spinner 匀速旋转（线性，持续运动）+ 进度环缓动逼近目标值。
        静态元素不动；全部静止后动画循环自动停止。"""
        if self._spinner_row is not None:
            self._spin_angle = (self._spin_angle + 18) % 360
            self._spinner_photo = self._render_spinner(self._spin_angle)
            self.canvas.itemconfigure(self.stage_icons[self._spinner_row],
                                      image=self._spinner_photo)
        # 指数逼近 = ease-out 手感：先快后慢，约 1 秒顺滑到位
        diff = self._frac_target - self._frac_shown
        if abs(diff) > 0.002:
            self._frac_shown += diff * 0.16
            self._draw_ring()
        if self._active is None and abs(self._frac_target - self._frac_shown) <= 0.002:
            self._animating = False
            return
        self.root.after(50, self._animate)

    def _set_progress(self, frac):
        self._frac_target = max(0.0, min(1.0, frac))
        if self._frac_shown == 0.0 and self._frac_target > 0:
            self._frac_shown = 0.004   # 起点可见，避免"凭空出现"
        self._draw_ring()

    def _draw_ring(self):
        deg = 359.999 * min(1.0, self._frac_shown)
        self._ring_photo = self._render_ring(deg)
        self.canvas.itemconfigure(self._ring_item, image=self._ring_photo)

    def succeed(self):
        self.canvas.itemconfigure(self.status, text="已就绪，可以开始使用了", fill=GREEN)
        self.root.after(1400, self.root.destroy)

    def fail(self, msg):
        if self._spinner_row is not None:
            self.canvas.itemconfigure(self.stage_icons[self._spinner_row],
                                      image=self._icon_error)
            self._spinner_row = None
        self._active = None
        self.canvas.itemconfigure(self.status, text=msg, fill=RED)
        self.root.after(12000, self.root.destroy)

    def run(self):
        self.root.mainloop()


class StoppedWindow(Card):
    """极简提示卡（自动 1.6 秒关闭）。用于停止确认 / 已在运行提示。"""
    W, H = 300, 180

    def __init__(self, title="语音助手已停止", subtitle="再次双击桌面快捷方式即可启动"):
        super().__init__(title_buttons=False)
        c = self.canvas

        img = Image.open(PROJ / "voice-assistant.ico").convert("RGBA").resize((46, 46), Image.LANCZOS)
        gray = img.convert("L")
        gray = Image.merge("RGBA", (gray, gray, gray, img.getchannel("A")))
        self.icon_img = ImageTk.PhotoImage(gray)
        c.create_image(self.W // 2, 66, image=self.icon_img)
        c.create_text(self.W // 2, 118, text=title, fill=FG,
                      font=(FONT, 12, "bold"))
        c.create_text(self.W // 2, 142, text=subtitle,
                      fill=DIM, font=(FONT, 9))
        self.root.after(1600, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ---------- 启动/停止入口 ----------
# 桌面快捷方式 = 只启动（无参数）：未运行→启动向导；已在运行→提示卡，不做停止。
# 停止只有显式 --stop（托盘右键「退出」调用）：杀全部进程并弹停止卡。

LOCK_FILE = Path(os.environ.get("TEMP", ".")) / "voice-assistant-toggle.lock"


def _debounced() -> bool:
    """10 秒防抖，防止快速重复双击起多个实例。"""
    try:
        if LOCK_FILE.exists() and time.time() - LOCK_FILE.stat().st_mtime < 10:
            return True
        LOCK_FILE.touch()
    except Exception:
        pass
    return False


def _find_assistant_pids():
    """按命令行特征找运行中的语音助手进程（适配器/语音管线/门控，排除自身）。"""
    ps = ("$procs = Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" |"
          " Where-Object { $_.CommandLine -match"
          " 'speech-to-kimi-entry|speech_to_speech|adapter\\.py|mic-gate' };"
          "$procs += Get-CimInstance Win32_Process -Filter \"Name='speech-to-speech.exe'\";"
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


def main():
    if "--stopped" in sys.argv:
        StoppedWindow().run()
        return

    if "--stop" in sys.argv:
        # ===== 停止（托盘右键「退出」调用）：杀全部相关进程，弹停止卡 =====
        pids = _find_assistant_pids()
        if pids:
            _kill_pids(pids)
            time.sleep(0.5)
        StoppedWindow().run()
        return

    if _debounced():
        return

    if _find_assistant_pids():
        # 已在运行：快捷方式只负责启动，关闭走托盘右键「退出」
        StoppedWindow("语音助手正在运行", "右键托盘图标「退出」可关闭").run()
        return

    if _already_running("speech-to-kimi-code-voice-launcher"):
        return  # 已有一个向导在跑，避免双实例互踩（端口/日志冲突）

    q = []
    launcher = Launcher(
        on_stage=lambda i, s: q.append(("stage", i, s)),
        on_done=lambda: q.append(("done",)),
        on_error=lambda m: q.append(("error", m)),
    )
    win = LauncherWindow(on_close=launcher.kill_all)
    threading.Thread(target=launcher.run, daemon=True).start()

    def pump():
        try:
            while q:
                ev = q.pop(0)
                if ev[0] == "stage":
                    win.set_stage(ev[1], ev[2])
                elif ev[0] == "done":
                    win.succeed()
                elif ev[0] == "error":
                    win.fail(ev[1])
            win.root.after(120, pump)
        except tk.TclError:
            pass  # 窗口已关闭

    win.root.after(120, pump)
    win.run()


if __name__ == "__main__":
    main()
