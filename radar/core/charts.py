"""服务端生成的内联 SVG 小图表。

为什么不用图表库（Chart.js / ECharts / vue-data-ui 之类）：

    * 这个站是**构建时生成、之后只发文件**的静态快照。引一个运行时图表库，
      等于给每个历史页面绑一份 JS，还要手机打开页面时执行它才能看到图 ——
      而这里的 SVG 是"烤"进 HTML 的，页面一打开就是最终形态，
      没有加载态、没有布局抖动、断网也能看，后端挂了也照样显示。
    * 数据点极少（5 个以内），库的体积和坐标轴/交互那套复杂度都不划算。

三个过滤器各自只回答一个视觉问题，不做多余的事：

    spark —— 趋势朝哪个方向走（点与点的高低对比）
    bars  —— 正负值各自的量级（零基线两边的柱子）
    hbar  —— 单个值占最大值的比例（分布 / 占比条）

**安全约定**：颜色/类名也会拼进 HTML，虽然调用方都是自家模板，
仍然统一走 markupsafe.escape —— 免得哪天有人把外部数据喂进来。
"""

from __future__ import annotations

import math

from markupsafe import Markup, escape


def _numbers(values) -> list[float]:
    """只留下能画图的数：跳过 None / 布尔 / 非数 / NaN / inf。

    历史快照里字段可能是字符串（"12.3"）或干脆缺失，这里静默跳过而不是抛错 ——
    一个坏点不该让整页白屏。
    """
    out: list[float] = []
    for v in values or []:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f)
    return out


def _n(v: float) -> str:
    """坐标压到 1 位小数，省一半字节 —— 这两张图每天都要重发一遍。"""
    return f"{v:.1f}"


def spark(values, width=128, height=34, color="var(--accent)", fill=True,
          stroke=1.8, pad=3.0) -> Markup:
    """折线迷你图。点少于 2 个没趋势可看，直接返回空（模板那边不用判空）。"""
    vals = _numbers(values)
    n = len(vals)
    if n < 2:
        return Markup("")

    w, h = int(width), int(height)
    lo, hi = min(vals), max(vals)
    flat = (hi - lo) == 0.0          # 全平时画一条居中的水平线，别贴着底边（看着像 0）
    span = (hi - lo) or 1.0
    iw, ih = w - 2 * pad, h - 2 * pad

    pts = []
    for i, v in enumerate(vals):
        x = pad + iw * (i / (n - 1))
        # 值大 → y 小（屏幕坐标向下）
        y = (h / 2.0) if flat else pad + ih * (1.0 - (v - lo) / span)
        pts.append(f"{_n(x)},{_n(y)}")
    poly = " ".join(pts)
    c = escape(color)

    body = ""
    if fill:
        # 面积用极淡的同色：只做"这一块是实体区域"的暗示，不抢折线的注意力
        body += (f'<polygon points="{_n(pad)},{_n(h - pad)} {poly} '
                 f'{_n(w - pad)},{_n(h - pad)}" fill="{c}" opacity=".13"/>')
    # non-scaling-stroke：SVG 被 CSS 拉宽时线宽不跟着变形
    body += (f'<polyline points="{poly}" fill="none" stroke="{c}" stroke-width="{stroke}" '
             f'stroke-linecap="round" stroke-linejoin="round" '
             f'vector-effect="non-scaling-stroke"/>')
    return Markup(f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                  f'preserveAspectRatio="none" aria-hidden="true">{body}</svg>')


def bars(values, width=150, height=38, pos="var(--up)", neg="var(--down)",
         gap=2.0, radius=1.5) -> Markup:
    """零基线柱状图。正柱用 pos 色、负柱用 neg 色。

    中国习惯红涨绿跌，所以默认 pos=红（--up）、neg=绿（--down）：
    "净流入"为正画红柱、"净流出"为负画绿柱，和页面上数字的颜色一致。
    """
    vals = _numbers(values)
    n = len(vals)
    if not n:
        return Markup("")

    w, h = int(width), int(height)
    hi = max(max(vals), 0.0)         # 基线强制含 0，否则全正的序列会失去"多少"的量感
    lo = min(min(vals), 0.0)
    if hi == lo:
        # 全是 0：给个对称的假跨度，让零基线落在中间，而不是把一排柱子顶到画面顶端
        hi, lo = 1.0, -1.0
    span = hi - lo

    def y_of(v: float) -> float:
        return h * (hi - v) / span

    y0 = y_of(0.0)
    bw = (w - gap * (n - 1)) / n
    if bw <= 1.0:                    # 点太多挤不下就取消间隙，别再缩柱子
        bw, gap = w / n, 0.0

    parts = []
    for i, v in enumerate(vals):
        x = i * (bw + gap)
        yv = y_of(v)
        top, hh = min(y0, yv), abs(yv - y0)
        c = pos if v >= 0 else neg
        # 最小 1px：值为 0 或极小也留一道痕，避免"这一格是空的还是没数据"的歧义
        parts.append(f'<rect x="{_n(x)}" y="{_n(top)}" width="{_n(bw)}" '
                     f'height="{_n(max(hh, 1.0))}" rx="{radius}" fill="{escape(c)}"/>')

    zero = ""
    if lo < 0 < hi:                  # 有正有负才画基线，否则是条多余的横线
        zero = (f'<line x1="0" y1="{_n(y0)}" x2="{w}" y2="{_n(y0)}" '
                f'stroke="currentColor" stroke-opacity=".28" stroke-width="1" '
                f'vector-effect="non-scaling-stroke"/>')
    return Markup(f'<svg class="sbars" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
                  f'preserveAspectRatio="none" aria-hidden="true">{zero}{"".join(parts)}</svg>')


def hbar(value, maxv, tone="accent") -> Markup:
    """单个值相对最大值的水平占比条。maxv<=0 或值非法时给空串，不画半截。"""
    try:
        v = float(value)
        m = float(maxv)
    except (TypeError, ValueError):
        return Markup("")
    if not (math.isfinite(v) and math.isfinite(m)) or m <= 0:
        return Markup("")
    pct = max(0.0, min(100.0, v / m * 100.0))
    return Markup(f'<span class="hbar {escape(tone)}"><i style="width:{pct:.1f}%"></i></span>')
