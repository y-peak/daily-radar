#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成安卓壳需要的图标（`app/android/res/mipmap-*`）。

复用 `make_icons.py` 里那套雷达图形，**不另做一套视觉** ——
桌面图标和浏览器里看到的是同一个东西，用户才不会觉得是两个应用。

两套素材：

1. **传统图标** `ic_launcher.png`
   密度 48/72/96/144/192（mdpi→xxxhdpi）。Android 8 以下、以及部分国产桌面
   只认这个。

2. **自适应图标前景** `ic_launcher_foreground.png`
   Android 8+ 的 `mipmap-anydpi-v26/ic_launcher.xml` 引用它。
   ⚠️ 两个容易做错的点：
     · 前景必须**透明底**（底由 background 层给），否则会把背景色盖住
     · 图形只能占中间 **61%** —— 系统会按各家桌面的形状去裁
       （小米是超椭圆、原生是圆），画满整张图会被裁掉边角。
       这里取 58%（padding_ratio=0.21）留一点余量。

    pip install pillow
    python tools/make_android_icons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from make_icons import draw_radar          # noqa: E402

OUT = ROOT / "app" / "android" / "res"

# 传统图标：48dp 基准 × 各密度倍率
LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}

# 自适应前景：画布是 108dp（不是 48dp）× 各密度倍率
FOREGROUND = {"xhdpi": 216, "xxhdpi": 324, "xxxhdpi": 432}


def main() -> None:
    for dens, px in LEGACY.items():
        d = OUT / f"mipmap-{dens}"
        d.mkdir(parents=True, exist_ok=True)
        draw_radar(px, 0.0).save(d / "ic_launcher.png")
        print(f"  mipmap-{dens:<8} ic_launcher.png            {px}x{px}")

    for dens, px in FOREGROUND.items():
        d = OUT / f"mipmap-{dens}"
        d.mkdir(parents=True, exist_ok=True)
        # 0.21 → 图形占中间 58%，留在自适应图标的安全区（61%）之内
        draw_radar(px, 0.21, transparent_bg=True).save(d / "ic_launcher_foreground.png")
        print(f"  mipmap-{dens:<8} ic_launcher_foreground.png {px}x{px}")

    print(f"\n写入 {OUT}")


if __name__ == "__main__":
    main()
