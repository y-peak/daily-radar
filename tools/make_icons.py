#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PWA 需要的图标。

**为什么图标是必须的**：Chrome/Edge 判断"能不能安装"的硬性条件里就有
「manifest 里至少有 192x192 和 512x512 两个图标」。之前 manifest 里写的是
`"icons": []`，看着像 PWA，实际浏览器根本不给安装入口 —— 这是"app 装不上"的根因。

图标设计：蓝色圆角方块 + 同心弧（雷达波）+ 中心点，和顶栏那个小圆点是同一套视觉。

    pip install pillow
    python tools/make_icons.py

产物写进 radar/static/icons/，跟着仓库走（图标是资产，不该每次构建现算）。
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "radar" / "static" / "icons"

ACCENT = (31, 111, 235, 255)      # #1f6feb
ACCENT_DARK = (17, 78, 176, 255)
WHITE = (255, 255, 255, 255)


def draw_radar(size: int, padding_ratio: float = 0.0,
               transparent_bg: bool = False) -> Image.Image:
    """画一个 size×size 的图标。padding_ratio 给 maskable 用（安全区内缩）。

    `transparent_bg=True` 时**不画底**，只留图形 —— 安卓自适应图标的前景层
    要求透明底 + 图形缩在中间（系统会按各家桌面形状去裁），
    所以 `tools/make_android_icons.py` 会用到这个开关。
    """
    # 4 倍超采样再缩，边缘不会毛糙
    ss = 4
    n = size * ss
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 背景：圆角方块（maskable 时铺满，普通图标留一点圆角）
    radius = int(n * 0.22)
    if padding_ratio > 0:
        # maskable：整个画布填满，视觉元素缩到中间 60%
        if not transparent_bg:
            d.rectangle([0, 0, n, n], fill=ACCENT)
        inner = n * (1 - padding_ratio * 2)
        scale = inner / n
    else:
        if not transparent_bg:
            d.rounded_rectangle([0, 0, n - 1, n - 1], radius=radius, fill=ACCENT)
        scale = 1.0

    cx = cy = n / 2
    # 同心弧：从右下往左上扫，像雷达
    max_r = n * 0.46 * scale
    for i, ratio in enumerate((1.0, 0.68)):
        r = max_r * ratio
        box = [cx - r, cy - r, cx + r, cy + r]
        d.arc(box, start=200, end=340, fill=WHITE, width=max(2, int(n * 0.045)))
        d.arc(box, start=20, end=160, fill=ACCENT_DARK, width=max(2, int(n * 0.045)))

    # 中心点
    r = n * 0.085 * scale
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=WHITE)

    # 一道斜向"扫描线"，让图形不那么呆板
    ang = math.radians(-45)
    tip = (cx + math.cos(ang) * max_r * 0.92, cy + math.sin(ang) * max_r * 0.92)
    d.line([cx, cy, tip[0], tip[1]], fill=WHITE, width=max(2, int(n * 0.03)))

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    targets = {
        "icon-192.png": (192, 0.0),
        "icon-512.png": (512, 0.0),
        "icon-maskable-192.png": (192, 0.14),
        "icon-maskable-512.png": (512, 0.14),
        "apple-touch-icon.png": (180, 0.0),
        "favicon-32.png": (32, 0.0),
        "favicon-16.png": (16, 0.0),
    }
    for name, (size, pad) in targets.items():
        img = draw_radar(size, pad)
        img.save(OUT / name)
        print(f"  {name:<26} {size}x{size}")

    # .ico 给老浏览器兜底（Pillow 能直接写 ico）
    draw_radar(48).save(OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"  {'favicon.ico':<26} 16/32/48")
    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
