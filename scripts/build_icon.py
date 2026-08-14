#!/usr/bin/env python
"""MicToAgent 图标构建脚本。

SPDX-License-Identifier: Apache-2.0
Copyright 2026 MicToAgent contributors

高级简约的应用图标，纯 PIL 程序化绘制（矢量几何 + 4x 超采样），不用 AI 位图：
托盘 16px 小尺寸下几何图形依然锐利，且没有水印/合规问题。

设计：超椭圆（squircle）深墨底色 + 声波竖条阵列。
状态变体（托盘两态）：绿色声波=收音中，红色声波=静音——红绿强对比，
托盘 16px 下一眼可辨；应用图标/启动卡片维持蓝→紫品牌渐变。

输出（改设计后重跑本脚本即可全部更新）：
- voice-assistant.ico            应用图标（16~256 多尺寸，桌面快捷方式引用）
- icon-src.png                   1024 母图（设计源文件）
- voice-bridge/icons/tray_on.png   托盘-收音中（64px）
- voice-bridge/icons/tray_off.png  托盘-静音（64px）
- voice-bridge/icons/tray_talk_N.png 托盘-说话动画帧（N=0/1/2，绿色声波高度变化，
  mic-gate 在 VAD 检出真实语音时 ping-pong 循环切换）
- voice-bridge/panel-ui/src/assets/card_icon.png 面板/启动卡品牌图标（192px）

运行：.venv/Scripts/python.exe scripts/build_icon.py
"""

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

PROJ = Path(__file__).resolve().parent.parent

# 配色
BG_TOP = (28, 34, 52)       # 深墨蓝（顶）
BG_BOTTOM = (14, 17, 26)    # 深墨蓝（底）
WAVE_LEFT = (77, 124, 254)  # 声波渐变起：蓝（应用图标/卡片品牌色）
WAVE_RIGHT = (139, 92, 246)  # 声波渐变止：紫
# 托盘-收音中：绿色声波
WAVE_LEFT_ON = (74, 222, 128)
WAVE_RIGHT_ON = (22, 163, 74)
# 托盘-静音：红色声波
WAVE_LEFT_OFF = (248, 113, 113)
WAVE_RIGHT_OFF = (220, 38, 38)

SS = 4  # 超采样倍数


def _squircle_points(n_pts: int, radius: float, center: float, exponent: float = 4.0):
    """超椭圆（n=4 即苹果风 squircle）轮廓点列。"""
    pts = []
    for i in range(n_pts):
        t = 2 * math.pi * i / n_pts
        c, s = math.cos(t), math.sin(t)
        x = radius * math.copysign(abs(c) ** (2 / exponent), c)
        y = radius * math.copysign(abs(s) ** (2 / exponent), s)
        pts.append((center + x, center + y))
    return pts


def _vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    col = np.linspace(top, bottom, size, dtype=np.uint8)  # (size, 3)
    arr = np.repeat(col[:, None, :], size, axis=1)
    return Image.fromarray(arr, "RGB")


def _horizontal_gradient(size: int, left: tuple, right: tuple) -> Image.Image:
    row = np.linspace(left, right, size, dtype=np.uint8)  # (size, 3)
    arr = np.repeat(row[None, :, :], size, axis=0)
    return Image.fromarray(arr, "RGB")


def render(size: int, n_bars: int = 5, status: str = "brand",
           heights: list[float] | None = None) -> Image.Image:
    """渲染一版图标。size=输出边长；n_bars=声波条数（小尺寸用 3 条更清晰）；
    status：brand=品牌蓝紫（应用图标/卡片）、on=绿（托盘收音中）、off=红（托盘静音）；
    heights 可显式指定各声波条的相对高度（说话动画帧用）。"""
    N = size * SS
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

    # 底：超椭圆 + 纵向微渐变
    bg = _vertical_gradient(N, BG_TOP, BG_BOTTOM)
    mask = Image.new("L", (N, N), 0)
    d = ImageDraw.Draw(mask)
    pad = N * 0.012  # 微内缩，抗锯齿边缘更干净
    d.polygon(_squircle_points(720, (N - 2 * pad) / 2, N / 2), fill=255)
    img.paste(bg, (0, 0), mask)

    # 声波条：画在 mask 上，横向渐变透过 mask 合成
    mark_w, mark_h = N * 0.60, N * 0.42
    gap_ratio = 0.85 if n_bars >= 5 else 0.75
    bar_w = mark_w / (n_bars + (n_bars - 1) * gap_ratio)
    gap = bar_w * gap_ratio
    if heights is None:
        heights = ([0.38, 0.68, 1.0, 0.68, 0.38] if n_bars >= 5
                   else [0.58, 1.0, 0.58])
    x0 = (N - mark_w) / 2
    cy = N / 2
    wave_mask = Image.new("L", (N, N), 0)
    wd = ImageDraw.Draw(wave_mask)
    for i, hf in enumerate(heights):
        h = mark_h * hf
        x1 = x0 + i * (bar_w + gap)
        wd.rounded_rectangle([x1, cy - h / 2, x1 + bar_w, cy + h / 2],
                             radius=bar_w / 2, fill=255)
    wave_left, wave_right = {
        "brand": (WAVE_LEFT, WAVE_RIGHT),
        "on": (WAVE_LEFT_ON, WAVE_RIGHT_ON),
        "off": (WAVE_LEFT_OFF, WAVE_RIGHT_OFF),
    }[status]
    wave = _horizontal_gradient(N, wave_left, wave_right)
    img.paste(wave, (0, 0), wave_mask)

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    icons_dir = PROJ / "voice-bridge" / "icons"
    icons_dir.mkdir(exist_ok=True)

    # 母图（设计源文件，进仓库）
    render(1024).save(PROJ / "icon-src.png")

    # 应用图标：16/24/32 用 3 条简化版，其余 5 条。
    # 注意：ICO 保存以首帧尺寸为上限挑选 sizes，首帧必须是最大的 256
    frames = []
    for s in (256, 128, 64, 48, 32, 24, 16):
        frames.append(render(s, n_bars=3 if s <= 32 else 5))
    frames[0].save(PROJ / "voice-assistant.ico", format="ICO",
                   append_images=frames[1:])

    # 托盘两态（绿=收音中，红=静音）+ 面板/启动卡品牌图标（直接出到前端资源目录）
    render(64, status="on").save(icons_dir / "tray_on.png")
    render(64, status="off").save(icons_dir / "tray_off.png")
    card_icon = PROJ / "voice-bridge" / "panel-ui" / "src" / "assets" / "card_icon.png"
    render(192).save(card_icon)

    # 托盘说话动画帧（绿色声波，高度变化；mic-gate 说话时 ping-pong 切换）
    talk_heights = [
        [0.38, 0.68, 1.0, 0.68, 0.38],
        [0.75, 1.0, 0.45, 1.0, 0.75],
        [1.0, 0.50, 0.80, 0.50, 1.0],
    ]
    for i, hs in enumerate(talk_heights):
        render(64, status="on", heights=hs).save(icons_dir / f"tray_talk_{i}.png")

    written = [PROJ / "icon-src.png", PROJ / "voice-assistant.ico",
               icons_dir / "tray_on.png", icons_dir / "tray_off.png",
               card_icon]
    written += [icons_dir / f"tray_talk_{i}.png" for i in range(len(talk_heights))]
    for p in written:
        print("written:", p)


if __name__ == "__main__":
    main()
