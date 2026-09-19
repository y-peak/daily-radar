"""生成"扫码下载安卓 App"的二维码卡片（纯本地，不走网络）。

用法：
    python tools/make_download_qr.py --token <口令> [--path /app] [--out <png>]

二维码默认指向 `/app?t=<口令>` —— 这是 `/dl/latest.apk` 的**短别名**，
链接越短二维码越疏、越好扫。手机扫码即开始下载，不用手输地址。

口令与 APK 内嵌的是同一个，等于"扫码 = 获得访问权"，
所以这张图**不要公开发布**。
"""
from __future__ import annotations

import argparse
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

HOST = "118.196.100.121"
BRAND = (31, 111, 235)      # #1f6feb
INK = (23, 26, 31)
MUTED = (110, 118, 129)
BG = (255, 255, 255)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """尽量拿一个中文字体，拿不到就退回 PIL 默认（英文仍可读）。"""
    names = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc") if bold else \
            ("msyh.ttc", "simhei.ttf", "simsun.ttc", "msyhbd.ttc")
    for root in (Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype")):
        for n in names:
            p = root / n
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except OSError:
                    pass
    return ImageFont.load_default()


def build(url: str, out: Path, title: str = "个人情报台",
          sub: str = "扫码下载安卓 App") -> Path:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=INK, back_color=BG).convert("RGB")

    # 卡片布局：上方标题区 + 中间二维码 + 下方提示
    pad, top = 48, 132
    w = qr_img.width + pad * 2
    h = top + qr_img.height + 108
    card = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(card)

    ft = _font(44, bold=True)
    fs = _font(28)
    fu = _font(22)

    for text, font, fill, y in (
        (title, ft, INK, 46),
        (sub, fs, BRAND, 100),
    ):
        tw = d.textlength(text, font=font)
        d.text(((w - tw) / 2, y), text, font=font, fill=fill)

    card.paste(qr_img, (pad, top))
    d.rectangle([pad - 1, top - 1, pad + qr_img.width, top + qr_img.height],
                outline=(228, 232, 238), width=2)

    hint = f"或浏览器打开  https://{HOST}/"
    hw = d.textlength(hint, font=fu)
    d.text(((w - hw) / 2, top + qr_img.height + 34), hint, font=fu, fill=MUTED)

    out.parent.mkdir(parents=True, exist_ok=True)
    card.save(out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="生成下载二维码")
    ap.add_argument("--token", required=True, help="访问口令")
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--path", default="/app",
                    help="下载路径（默认 /app 短链；也可用 /dl/latest.apk）")
    ap.add_argument("--out", default="radar-download-qr.png")
    a = ap.parse_args()
    path = a.path if a.path.startswith("/") else "/" + a.path
    url = f"https://{a.host}{path}?t={a.token}"
    p = build(url, Path(a.out))
    print(f"已生成：{p}  ({p.stat().st_size} B)")
    print(f"指向：{url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
