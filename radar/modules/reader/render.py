r"""书籍正文的 Markdown 渲染器。

**为什么不复用 `core/md.py`**：那个是给"每日报告"用的极简版 —— 不支持图片、
不支持嵌套列表、不处理公式，而且它的转义口径已经被现有报告页面依赖。
书籍正文完全是另一档需求（见下），硬塞进同一个函数迟早把报告页弄坏。
所以这里独立一份，核心那个保持不动。

相对 `core/md.py` 多出来的能力（都是书稿实测需要）：
    * 标题锚点      —— 生成稳定 anchor，供目录跳转与"复制本节链接"
    * 图片          —— `![alt](path)` → `<figure><img loading=lazy><figcaption>`
    * 公式          —— `$..$` / `$$..$$` → **MathML**（服务端渲染，零 JS、断网可看）
    * 脚注          —— `[^1]` 引用 + `[^1]:` 定义，聚合成页尾列表
    * 嵌套列表      —— 按缩进归并，书里大量两级列表
    * 代码块语言    —— ```python 之类带上 class，便于高亮配色
    * 表格对齐      —— 解析 `:---:` 对齐标记

公式为什么要转 MathML 而不是引 KaTeX：
本项目的展示层原则是"服务端渲染、零前端 JS、断网可看"（见 skills/static-site-presentation）。
KaTeX 需要引入 ~1MB 的 JS + 字体并在客户端跑；MathML 是浏览器原生能力
（Chrome 109+ / Safari / Firefox 均支持），转完就是静态 HTML，原则不破。
实测 latex2mathml 对本书用到的 \frac \sum \begin{aligned} \begin{bmatrix}
\mathbb \mathrm \text \underbrace \left( 等均能正确转换。
"""

from __future__ import annotations

import html as _html
import re

# ---------------------------------------------------------------- 公式转换

_math_warned = False


def _to_mathml(tex: str, block: bool) -> str:
    """LaTeX → MathML。失败时**可见降级**（原样显示 TeX 并标注），绝不留白。

    绝不静默吞掉：公式渲染不出来而页面看起来"正常"，是最难发现的一类问题。
    """
    global _math_warned
    tex = tex.strip()
    if not tex:
        return ""
    try:
        import latex2mathml.converter as conv
    except ImportError:
        if not _math_warned:
            _math_warned = True
            print("  [warn] 未安装 latex2mathml → 公式将退回纯文本显示。"
                  "安装：pip install latex2mathml")
        return _fallback(tex, block)
    try:
        out = conv.convert(tex, display="block" if block else "inline")
    except Exception:  # noqa: BLE001  —— 单条公式失败不该让整页崩掉
        return _fallback(tex, block)
    # latex2mathml 会自带 xml 声明之外的属性，直接内联即可
    return out


def _fallback(tex: str, block: bool) -> str:
    tag = "div" if block else "span"
    cls = "math-block is-raw" if block else "math-inline is-raw"
    return f'<{tag} class="{cls}" title="公式未能转换，显示原始 TeX">{_html.escape(tex)}</{tag}>'


# ------------------------------------------------------------------- 锚点
_ANCHOR_BAD = re.compile(r"[^\w\u4e00-\u9fff-]+", re.UNICODE)


def slugify(text: str, used: dict[str, int] | None = None, fallback: str = "sec") -> str:
    """把标题转成稳定、可读的 anchor。保留中文（现代浏览器支持 CJK 片段），
    去掉标点空格，重复时加 -2/-3 后缀。"""
    s = _ANCHOR_BAD.sub("-", text.strip().lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:60].strip("-")
    if not s:
        s = fallback
    if used is not None:
        used[s] = used.get(s, 0) + 1
        if used[s] > 1:
            s = f"{s}-{used[s]}"
    return s


# --------------------------------------------------------------- 行内规则
_CODE = re.compile(r"`([^`]+)`")
# 图片后面可以跟一个 pandoc 属性块：`![](x.svg){height=55%}`
# 不认它的话 `{height=55%}` 会被当正文原样显示出来 —— 书稿里就这么一处，
# 但它是"可见的标记泄漏"，属于必须修掉的那类问题。
_IMG_ATTR = r"(?:\{([^}]*)\})?"
_IMG = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)" + _IMG_ATTR)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|mailto:[^)\s]+|#?[^)\s]*)\)")
_FN_REF = re.compile(r"\[\^([^\]]+)\]")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_STRIKE = re.compile(r"~~(.+?)~~")
_EM = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")

# 行内公式：$...$，但排除 $$ 与已转义的 \$
_INLINE_MATH = re.compile(r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$)")
_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.S)

# 占位符用私用区字符，正文里不可能出现
_PH = "\ue000%d\ue001"


_FIG_ONLY = re.compile(r"^\s*(?:!\[[^\]]*\]\([^)\s]+\)(?:\{[^}]*\})?\s*)+$")
_FIG_ONE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)(?:\{([^}]*)\})?")
# 图注段落：整段是 `*图 2-1　……*` 这种写法
_CAP_PARA = re.compile(r"^\s*\*{1,2}\s*(?:图|表|Figure|Fig\.?|Table)\s*[\d一二三四五六七八九十]")


def _img_style(spec: str) -> str:
    """把 pandoc 属性块译成行内样式。只认 width / height，其余属性直接丢掉。

    `height=55%` 在 pandoc 里是"文本高度的 55%"，网页里没有一一对应物，
    最接近的是视口高度（vh）—— 用 max-height 而不是 height，这样窄屏上图
    不会被压扁，只是上限收住。
    """
    if not spec:
        return ""
    attrs = dict(re.findall(r"([a-zA-Z%-]+)\s*=\s*([^\s,]+)", spec))
    css: list[str] = []
    if "width" in attrs:
        css.append(f"width:{attrs['width']}")
    elif "height" in attrs:
        h = attrs["height"]
        css.append(f"max-height:{h[:-1]}vh" if h.endswith("%") else f"max-height:{h}")
    return f' style="{";".join(css)}"' if css else ""


# 什么时候可以把 alt 当图注用？
#
# 前提是"书稿自己没有图注段落"（见 render() 的前瞻判断）。此时 alt 是唯一
# 可用的说明文字，用它当图注总比什么都没有强 —— 但**太长的整句描述不适合**：
# ai-infra 的 alt 常常是一整句话（"循环神经网络的依赖。相同颜色的圆点表示……"），
# 挂在每张图下面会让版面全是文字。所以设一个上限，超了就宁可不放图注。
_ALT_CAPTION_MAX = 120


def _alt_as_caption(alt: str) -> bool:
    return bool(alt) and len(alt) <= _ALT_CAPTION_MAX


def _figure_html(alt: str, src: str, img_prefix: str, with_caption: bool,
                 spec: str = "") -> str:
    url = src if src.startswith(("http://", "https://", "data:")) else img_prefix + src.lstrip("./")
    img = (f'<img src="{_html.escape(url, quote=True)}" '
           f'alt="{_html.escape(alt, quote=True)}"{_img_style(spec)} '
           f'loading="lazy" decoding="async">')
    cap = ("<figcaption>%s</figcaption>" % _html.escape(alt)) if with_caption else ""
    return f'<figure class="fig">{img}{cap}</figure>'


def _figures_block(content: str, img_prefix: str, with_caption: bool = True) -> list[str] | None:
    """整段只有图片时，按**块级**输出。

    为什么要单独处理：`<figure>` 是块级元素，被塞进 `<p>` 里是非法结构
    （浏览器会自作主张把 <p> 截断，后面的内容会被挪出段落）。书里大量
    "图独占一段"的写法，正好走这条路。

    `with_caption=False` 用于"书稿自己带了图注段落"的情况（见 render() 里的
    前瞻判断）：ai-infra 写成 `![只缩短交换](…)` + 下一段 `*图 7-52：……*`，
    此时 alt 只是图片标签，真正的说明在下一段 —— 再拿它当图注就是重复。
    """
    if not _FIG_ONLY.match(content):
        return None
    out = []
    for m in _FIG_ONE.finditer(content):
        alt, src = m.group(1).strip(), m.group(2).strip()
        spec = (m.group(3) or "").strip()
        out.append(_figure_html(alt, src, img_prefix,
                                with_caption and _alt_as_caption(alt), spec))
    return out


class _Hold:
    """暂存已处理片段（公式 / 行内代码），避免被后续正则二次加工。"""

    def __init__(self) -> None:
        self.items: list[str] = []

    def put(self, html: str) -> str:
        self.items.append(html)
        return _PH % (len(self.items) - 1)

    def restore(self, text: str) -> str:
        def sub(m):
            return self.items[int(m.group(1))]
        return re.sub("\ue000(\\d+)\ue001", sub, text)


def inline(text: str, img_prefix: str = "", hold: _Hold | None = None) -> str:
    """行内元素。图片与公式先摘出来占位，最后再放回。"""
    hold = hold or _Hold()

    # 1) 图片（要在链接规则之前，否则 ![..](..) 会被当成链接）
    #    这里只处理"夹在文字中间"的图片 —— 必须用行内 <img>，不能套 <figure>
    #    （<figure> 是块级元素，放进 <p> 是非法结构）。独占一段的图片不走这里，
    #    由 render() 交给 _figures_block() 输出成块级 <figure>。
    def _img(m):
        alt, src = m.group(1).strip(), m.group(2).strip()
        spec = (m.group(3) or "").strip()
        if src.startswith(("http://", "https://", "data:")):
            url = src
        else:
            url = img_prefix + src.lstrip("./")
        return hold.put(
            f'<img class="fig-img" src="{_html.escape(url, quote=True)}" '
            f'alt="{_html.escape(alt, quote=True)}"{_img_style(spec)} '
            f'loading="lazy" decoding="async">'
        )

    text = _IMG.sub(_img, text)

    # 2) 行内代码 —— 必须在公式之前：`$x` 这种"代码里的美元号"不该被当成公式
    text = _CODE.sub(lambda m: hold.put(f"<code>{_html.escape(m.group(1), quote=False)}</code>"), text)

    # 3) 公式：先块级 $$ 再行内 $（块级已在 render() 里优先处理，
    #    这里兜住"夹在段落中间"的少数情况；用 span+CSS 以免 <div> 落在 <p> 里）
    text = _DISPLAY_MATH.sub(
        lambda m: hold.put(f'<span class="math-block">{_to_mathml(m.group(1), True)}</span>'), text)
    text = _INLINE_MATH.sub(lambda m: hold.put(_to_mathml(m.group(1), False)), text)

    # 4) 转义（此时公式/代码已成占位符，不会被误转义）
    text = _html.escape(text, quote=False)

    # 5) 链接 / 脚注 / 强调
    text = _LINK.sub(
        lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>',
        text)
    text = _FN_REF.sub(lambda m: f'<sup class="fn-ref" id="fnref-{_fnid(m.group(1))}">'
                                 f'<a href="#fn-{_fnid(m.group(1))}">{m.group(1)}</a></sup>', text)
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _STRIKE.sub(r"<del>\1</del>", text)
    text = _EM.sub(r"<em>\1</em>", text)

    return hold.restore(text)


def _fnid(label: str) -> str:
    return slugify(label, fallback="fn")


# ------------------------------------------------------------------ 块级
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_FENCE = re.compile(r"^\s*(```+|~~~+)\s*([\w+#.-]*)\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FN_DEF = re.compile(r"^\[\^([^\]]+)\]:\s*(.*)$")
_HTML_BLOCK = re.compile(r"^\s*<(div|figure|table|details|p|img|br|hr|center)\b", re.I)


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def render(text: str, *, img_prefix: str = "", heading_offset: int = 0) -> tuple[str, list[dict]]:
    """Markdown → (HTML, 标题清单)。

    img_prefix  图片 URL 前缀（如 /static/books/ai-infra/）
    heading_offset  标题级别整体下移几级（章节页里 h1 已被页面标题占用时可设为 0）
    """
    if not text:
        return "", []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    out: list[str] = []
    headings: list[dict] = []
    footnotes: list[tuple[str, str]] = []
    used: dict[str, int] = {}
    hold = _Hold()
    n = len(lines)
    i = 0

    def flush_footnotes() -> None:
        if not footnotes:
            return
        items = "".join(
            f'<li id="fn-{_fnid(lab)}">{body} '
            f'<a class="fn-back" href="#fnref-{_fnid(lab)}" aria-label="回到正文">↩</a></li>'
            for lab, body in footnotes)
        out.append(f'<section class="footnotes"><hr><h2>注释</h2><ol>{items}</ol></section>')

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        # 脚注定义
        m = _FN_DEF.match(line)
        if m:
            footnotes.append((m.group(1), inline(m.group(2), img_prefix, hold)))
            i += 1
            continue

        # 独立成行的公式块 $$...$$（书里大量出现，必须块级处理才能居中排版）
        stripped = line.lstrip()
        if stripped.startswith("$$"):
            body = stripped[2:]
            if "$$" in body:                    # 单行 $$ ... $$
                tex = body.split("$$", 1)[0]
                i += 1
            else:                               # 跨多行
                buf = [body]
                i += 1
                while i < n and "$$" not in lines[i]:
                    buf.append(lines[i])
                    i += 1
                if i < n:
                    buf.append(lines[i].split("$$", 1)[0])
                    i += 1
                tex = "\n".join(buf)
            out.append(f'<div class="math-block">{_to_mathml(tex, True)}</div>')
            continue

        # 代码围栏
        m = _FENCE.match(line)
        if m:
            fence, lang = m.group(1), m.group(2)
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith(fence[:3]):
                buf.append(lines[i])
                i += 1
            i += 1
            cls = f' class="language-{_html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{_html.escape(chr(10).join(buf), quote=False)}</code></pre>")
            continue

        # 水平线
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        # 标题
        m = _HEADING.match(line)
        if m:
            lvl = min(6, max(1, len(m.group(1)) + heading_offset))
            raw = m.group(2)
            # 标题里也可能有公式/代码
            title_html = inline(raw, img_prefix, hold)
            plain = re.sub(r"<[^>]+>", "", title_html)
            anchor = slugify(plain, used)
            out.append(f'<h{lvl} id="{anchor}"><a class="h-anchor" '
                       f'href="#{anchor}" aria-hidden="true">#</a>{title_html}</h{lvl}>')
            if 1 < lvl <= 4:
                headings.append({"level": lvl, "text": plain, "anchor": anchor})
            i += 1
            continue

        # 表格
        if "|" in line and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            header = _cells(line)
            i += 2
            body: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                body.append(_cells(lines[i]))
                i += 1
            th = "".join(f"<th>{inline(c, img_prefix, hold)}</th>" for c in header)
            tr = "".join(
                "<tr>" + "".join(f"<td>{inline(c, img_prefix, hold)}</td>" for c in row) + "</tr>"
                for row in body)
            out.append(f'<div class="tbl"><table><thead><tr>{th}</tr></thead>'
                       f"<tbody>{tr}</tbody></table></div>")
            continue

        # 引用块
        if _QUOTE.match(line):
            buf = []
            while i < n and _QUOTE.match(lines[i]):
                buf.append(_QUOTE.match(lines[i]).group(1))
                i += 1
            inner = render("\n".join(buf), img_prefix=img_prefix)[0]
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # 列表（支持按缩进嵌套）
        if _UL.match(line) or _OL.match(line):
            html_out, i = _list_block(lines, i, img_prefix, hold)
            out.append(html_out)
            continue

        # 裸 HTML 块（书里少量 <div class="hero"> 之类）
        if _HTML_BLOCK.match(line):
            out.append(line)
            i += 1
            continue

        # 段落
        buf = []
        while i < n and lines[i].strip() and not (
            _HEADING.match(lines[i]) or _QUOTE.match(lines[i]) or _HR.match(lines[i])
            or _FENCE.match(lines[i]) or _UL.match(lines[i]) or _OL.match(lines[i])
            or _FN_DEF.match(lines[i]) or _HTML_BLOCK.match(lines[i])
            or ("|" in lines[i] and i + 1 < n and _TABLE_SEP.match(lines[i + 1]))
        ):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            joined = " ".join(buf)
            # 前瞻：紧跟其后的第一个非空块是不是书稿自带的图注段落？
            # 是的话就当"本书有正规图注"，alt 不再重复充当图注。
            nxt = i
            while nxt < n and not lines[nxt].strip():
                nxt += 1
            has_own_caption = nxt < n and bool(_CAP_PARA.match(lines[nxt].strip()))

            figs = _figures_block(joined, img_prefix, with_caption=not has_own_caption)
            if figs is not None:
                # 整段都是图 → 输出块级 <figure>，绝不包进 <p>
                out.extend(figs)
            elif _CAP_PARA.match(joined):
                # `*图 2-1　…*` 这种"斜体独占一段"的写法就是图注，单独给个类，
                # 免得被当成普通强调文本（前端据此去斜体、居中、压暗）
                out.append(f'<p class="fig-cap">{inline(joined, img_prefix, hold)}</p>')
            else:
                out.append(f"<p>{inline(joined, img_prefix, hold)}</p>")

    flush_footnotes()
    return "\n".join(out), headings


def _list_block(lines: list[str], i: int, img_prefix: str, hold: _Hold) -> tuple[str, int]:
    """把连续的列表行按缩进归并成嵌套 <ul>/<ol>。

    书稿里两级列表极多（`- 要点` + 缩进 `  - 子项`）。规则很简单：
    以第一行的缩进为基准，**更深的行挂到上一个条目下面**，更浅的行说明本列表结束。
    嵌套层不自己再写一套递归 —— 直接把子块交给 render() 走同一套块级逻辑，
    这样"列表里套引用块/段落"也能自然工作。
    """
    n = len(lines)

    def indent_of(s: str) -> int:
        return len(s) - len(s.lstrip(" "))

    def is_item(s: str) -> bool:
        return bool(_UL.match(s) or _OL.match(s))

    first_ol = bool(_OL.match(lines[i]))
    base = indent_of(lines[i])
    items: list[tuple[str, list[str]]] = []      # (本行内容, 更深一层的原始行)

    while i < n and lines[i].strip() and is_item(lines[i]):
        cur = indent_of(lines[i])
        if cur < base:
            break                                # 回到外层，交给调用者
        if cur > base:
            if not items:
                break                            # 没有可挂靠的父项，交给上层处理
            items[-1][1].append(lines[i])
            i += 1
            continue
        matched = _UL.match(lines[i]) or _OL.match(lines[i])
        items.append((matched.group(2), []))
        i += 1

    tag = "ol" if first_ol else "ul"
    parts = []
    for content, deeper in items:
        sub = render("\n".join(deeper), img_prefix=img_prefix)[0] if deeper else ""
        parts.append(f"<li>{inline(content, img_prefix, hold)}{sub}</li>")
    return f"<{tag}>" + "".join(parts) + f"</{tag}>", i


def plain_text(text: str) -> str:
    """去掉 markdown 记号，用于字数/摘要统计。"""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\$\$.*?\$\$", " ", t, flags=re.S)
    t = re.sub(r"\$[^$\n]+\$", " ", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[\^[^\]]+\]", " ", t)
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", t)
    t = re.sub(r"[*_`>~|#-]", "", t)
    return re.sub(r"\s+", " ", t).strip()
