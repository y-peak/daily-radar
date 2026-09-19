"""极简 Markdown → HTML。

只覆盖报告里真正用到的语法，不引第三方库（少一个依赖，也少一个供应链面）。

支持：`#`~`####` 标题、段落、无序/有序列表、引用块、`**粗体**`、`*斜体*`、
`` `代码` ``、`[文字](链接)`、``` 围栏代码块、水平线、简单表格。

不支持嵌套列表、引用式链接、图片 —— 报告用不上。
"""

from __future__ import annotations

import html as _html
import re

_HEADING = re.compile(r"^(#{1,4})\s+(.*)$")
_UL = re.compile(r"^\s*[-*+]\s+(.*)$")
_OL = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
_SEP_CELL = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_FENCE = re.compile(r"^\s*```")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|/[^)\s]*)\)")


def _inline(text: str) -> str:
    text = _html.escape(text, quote=False)
    # 链接要在加粗之前处理，这样 **[名字](url)** 能整体变成粗体链接
    text = _LINK.sub(
        r'<a href="\2" target="_blank" rel="noopener">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"(?<!\*)\*(?!\s)([^*]+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", text)
    return text


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [cell.strip() for cell in line.split("|")]


def to_html(md: str) -> str:
    if not md:
        return ""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue

        # 围栏代码块：``` 到下一个 ```
        if _FENCE.match(line):
            i += 1
            buf = []
            while i < n and not _FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1                      # 跳过收尾的 ```
            body = _html.escape("\n".join(buf), quote=False)
            out.append(f"<pre><code>{body}</code></pre>")
            continue

        m = _HEADING.match(line)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2).strip())}</h{level}>")
            i += 1
            continue

        # 表格：当前行含 | 且下一行是分隔行
        if "|" in line and i + 1 < n and _SEP_CELL.match(lines[i + 1]):
            header = _split_row(line)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            thead = "".join(f"<th>{_inline(c)}</th>" for c in header)
            tbody = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in rows
            )
            out.append(f"<table><thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table>")
            continue

        if _UL.match(line):
            items = []
            while i < n and _UL.match(lines[i]):
                items.append(f"<li>{_inline(_UL.match(lines[i]).group(1))}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        if _OL.match(line):
            items = []
            while i < n and _OL.match(lines[i]):
                items.append(f"<li>{_inline(_OL.match(lines[i]).group(1))}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")
            continue

        if _QUOTE.match(line):
            buf = []
            while i < n and _QUOTE.match(lines[i]):
                buf.append(_inline(_QUOTE.match(lines[i]).group(1)))
                i += 1
            out.append("<blockquote>" + "<br>".join(buf) + "</blockquote>")
            continue

        buf = []
        while i < n and lines[i].strip() and not (
            _HEADING.match(lines[i]) or _UL.match(lines[i]) or _OL.match(lines[i])
            or _QUOTE.match(lines[i]) or _HR.match(lines[i])
        ):
            buf.append(lines[i].strip())
            i += 1
        if buf:
            out.append("<p>" + _inline(" ".join(buf)) + "</p>")

    return "\n".join(out)
