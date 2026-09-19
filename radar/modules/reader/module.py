"""读书模块 —— 把开源书稿变成可离线阅读的本地书架。

## 它和另外两个模块的根本区别

`github_trending` / `market_flow` 是**采集型**模块：每天去外面拿新数据，
按日期分片存快照，"今天的东西变了"是常态。
读书模块是**资产型**模块：书稿是**钉死的内容**，不随日期变。

这个差别决定了三件事：

  1. **collect 不联网**。它只读 `radar/static/books/` 下已经落地的正文，
     算字数/摘要/校验和。跑一万次结果一致，构建可以完全离线。
     更新书稿是一次**显式的、有意识的**操作（见 tools/ 里的落地脚本），
     不是每天自动漂移 —— 否则"历史页面不可变"就无从谈起。
  2. **"历史页面不可变"靠 pin 内容，而不是靠按日期分片**。
     `books.py` 里每个书都钉了 `commit`，章节页不带日期。
     书稿不动 → 渲染结果就是确定的。
  3. **页面是三层**：书房（/reader/）→ 单本书（/reader/<book>/）→ 章节
     （/reader/<book>/<chapter>/）。核心只给模块生成第一层，
     后两层通过 `extra_pages()` 钩子产出（见 core/module.py 的说明）。

## 渲染

正文走模块自带的 `render.py`（书籍专用 Markdown），不是 core/md.py。
公式转 **MathML**：服务端渲染、零前端 JS、断网可看，和展示层的既有原则一致。
渲染失败会**可见降级**（原样显示 TeX 并加 `is-raw` 标记），绝不留白 ——
"公式没渲染出来但页面看着正常"是最难发现的一类问题。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from radar.core.module import Context, Module, load_sibling

books_meta = load_sibling(__file__, "books")
mdbook = load_sibling(__file__, "render")

# 中文技术书阅读速度：约 350 字/分钟（含公式与图表停顿）
CHARS_PER_MINUTE = 350
# 每章最多在目录里展开多少个小节，避免目录本身长到不可用
TOC_MAX = 200

_IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _minutes(chars: int) -> int:
    return max(1, round(chars / CHARS_PER_MINUTE))


def _dur(minutes: int) -> str:
    """时长文案。放在 Python 里算而不是模板里 —— 模板只该负责摆放，不该做算术。"""
    if minutes < 60:
        return f"{minutes} 分钟"
    h, m = divmod(minutes, 60)
    return f"{h} 小时" + (f" {m} 分" if m else "")


def _wan(chars: int) -> str:
    return f"{chars / 10000:.1f} 万字" if chars >= 10000 else f"{chars} 字"


def _toc_cap(headings: list[dict]) -> list[dict]:
    """目录只保留 h2/h3，且总量设上限。"""
    picked = [h for h in headings if h["level"] in (2, 3)][:TOC_MAX]
    return picked


class Reader(Module):
    name = "reader"
    title = "读书"
    subtitle = "两本开源书，正文与插图都在本地，断网也能读"
    order = 30
    schedule = "内容随书稿更新（按 commit 钉死）"

    # ---------------------------------------------------------------- collect
    def collect(self, ctx: Context) -> Any:
        """把书架清单和磁盘上的正文对一遍，产出**内容清单**（不联网）。

        清单里记的是"这台机器上此刻的书稿长什么样"：字数、图数、
        内容校验和。校验和既是完整性证明，也是"这一章有没有被换过"的判据。
        """
        static_dir = ctx.config.static_dir
        problems = books_meta.validate(static_dir)
        for p in problems:
            # 缺章不能装作没事：明确报出来，让人去补，而不是让那一章悄悄消失
            ctx.log_info(f"      [warn] {p}")

        inventory = []
        for book in sorted(books_meta.BOOKS, key=lambda b: b.get("order", 100)):
            folder = books_meta.book_dir(static_dir, book)
            chapters = []
            for filename, slug, title in book["chapters"]:
                path = folder / filename
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                plain = mdbook.plain_text(text)
                _, headings = mdbook.render(text)      # 只为拿大纲，正文在 extra_pages 渲
                chapters.append({
                    "slug": slug,
                    "title": title,
                    "file": filename,
                    "chars": len(plain),
                    "chars_text": _wan(len(plain)),
                    "minutes": _minutes(len(plain)),
                    "duration_text": _dur(_minutes(len(plain))),
                    "hash": _sha(text),
                    "figures": len(_IMG_RE.findall(text)),
                    "toc": _toc_cap(headings),
                })
            inventory.append({
                "slug": book["slug"],
                "title": book["title"],
                "subtitle": book.get("subtitle", ""),
                "author": book.get("author", ""),
                "repo": book.get("repo", ""),
                "license": book.get("license", ""),
                "commit": book.get("commit", ""),
                "summary": book.get("summary", ""),
                "accent": book.get("accent", "blue"),
                "order": book.get("order", 100),
                "chapters": chapters,
            })
        if not inventory or all(not b["chapters"] for b in inventory):
            raise RuntimeError("书架上没有任何可读章节：检查 radar/static/books/ 是否已落地")
        return {"books": inventory, "problems": problems}

    # ---------------------------------------------------------------- analyze
    def analyze(self, raw: Any, ctx: Context) -> Any:
        """汇总成图书馆视图：每本书的字数/时长/图数，以及全局合计。"""
        books = []
        for b in raw.get("books") or []:
            chapters = b.get("chapters") or []
            chars = sum(c.get("chars", 0) for c in chapters)
            figures = sum(c.get("figures", 0) for c in chapters)
            books.append({
                **b,
                "count": len(chapters),
                "chars": chars,
                "chars_text": _wan(chars),
                "minutes": sum(c.get("minutes", 0) for c in chapters),
                "duration_text": _dur(sum(c.get("minutes", 0) for c in chapters)),
                "figures": figures,
                # 内容指纹：任何一章被换过都会变。用来判断"书架是否被动过"。
                "fingerprint": _sha("".join(c.get("hash", "") for c in chapters)),
            })
        books.sort(key=lambda b: b.get("order", 100))
        problem_count = len(raw.get("problems") or [])
        total_chars = sum(b["chars"] for b in books)
        total_minutes = sum(b["minutes"] for b in books)
        return {
            "books": books,
            "problems": raw.get("problems") or [],
            "totals": {
                "books": len(books),
                "chapters": sum(b["count"] for b in books),
                "chars": total_chars,
                "chars_text": _wan(total_chars),
                "minutes": total_minutes,
                "duration_text": _dur(total_minutes),
                "figures": sum(b["figures"] for b in books),
                "problems": problem_count,
            },
        }

    # ----------------------------------------------------------------- report
    def report(self, data: Any, ctx: Context) -> str:
        totals = data.get("totals") or {}
        lines = [
            "# 读书",
            "",
            f"书架 **{totals.get('books', 0)} 本**，共 **{totals.get('chapters', 0)} 章**，"
            f"约 **{totals.get('chars', 0) / 10000:.1f} 万字**"
            f"（按 {CHARS_PER_MINUTE} 字/分钟估算，通读约 {totals.get('minutes', 0) / 60:.0f} 小时）。",
            "",
        ]
        for b in data.get("books") or []:
            lines.append(
                f"## {b['title']}")
            lines.append("")
            lines.append(f"{b.get('subtitle', '')} · {b.get('author', '')} · "
                         f"{b.get('license', '')}")
            lines.append("")
            lines.append(f"- {b['count']} 章 · 约 {b['chars'] / 10000:.1f} 万字 · "
                         f"{b['minutes']} 分钟 · {b['figures']} 张插图")
            lines.append(f"- 来源：{b.get('repo', '')} @ `{(b.get('commit') or '')[:8]}`")
            lines.append("")
            for c in b.get("chapters") or []:
                lines.append(f"  - {c['title']}（{c['chars'] / 10000:.1f} 万字 / {c['minutes']} 分钟）")
            lines.append("")
        if totals.get("problems"):
            lines += [f"> ⚠️ 有 {totals['problems']} 处内容问题（缺章或文件异常），详见日志。", ""]
        lines.append("> 书稿按 commit 钉死，正文与插图均离线保存在本站，"
                     "不依赖上游仓库可达。")
        return "\n".join(lines)

    # ------------------------------------------------------------ extra_pages
    def extra_pages(self, page_ctx: dict) -> list[tuple[str, str]]:
        """产出单本书页与章节页（第三层、第二层）。

        一次渲染同时拿到正文 HTML 和标题大纲 —— 不为了目录再渲一遍。
        """
        env = page_ctx["env"]
        wrap = page_ctx["wrap"]
        log = page_ctx.get("log") or (lambda *_a: None)
        cfg = page_ctx["cfg"]
        static_dir = cfg.static_dir

        data = self._latest_data(page_ctx)
        if not data:
            log("      [warn] reader 还没有数据快照，跳过书的页面")
            return []

        tpl_dir = cfg.modules_dir / self.name
        book_tpl = env.from_string((tpl_dir / "book.html").read_text(encoding="utf-8"))
        chap_tpl = env.from_string((tpl_dir / "chapter.html").read_text(encoding="utf-8"))

        lib_books = data.get("books") or []
        pages: list[tuple[str, str]] = []

        # 全站导航用的"书 → 章节"骨架，章节页要用它做上/下一章
        shelf = [{"slug": b["slug"], "title": b["title"],
                  "chapters": [{"slug": c["slug"], "title": c["title"]}
                               for c in b.get("chapters") or []]}
                 for b in lib_books]

        for book in lib_books:
            slug = book["slug"]
            source_dir = books_meta.book_dir(static_dir, book)
            prefix = books_meta.asset_prefix(book)
            chapters_meta = book.get("chapters") or []

            rendered: list[dict] = []
            for idx, meta in enumerate(chapters_meta):
                path = source_dir / meta["file"]
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                html, headings = mdbook.render(text, img_prefix=prefix)
                prev_c = chapters_meta[idx - 1] if idx > 0 else None
                next_c = chapters_meta[idx + 1] if idx + 1 < len(chapters_meta) else None
                ctx_ch = {
                    "book": {**book, "chapters": chapters_meta},
                    "shelf": shelf,
                    "chapter": meta,
                    "index": idx,
                    "total": len(chapters_meta),
                    "html": html,
                    "headings": _toc_cap(headings),
                    "prev": {"slug": prev_c["slug"], "title": prev_c["title"]} if prev_c else None,
                    "next": {"slug": next_c["slug"], "title": next_c["title"]} if next_c else None,
                }
                body = chap_tpl.render(**ctx_ch)
                pages.append((
                    f"reader/{slug}/{meta['slug']}.html",
                    wrap(body, active=self.name, title=f"{meta['title']} · {book['title']}",
                         dates=[], date=None, is_latest=False),
                ))
                rendered.append({**meta, "headings": _toc_cap(headings)})

            book_body = book_tpl.render(
                book={**book, "chapters": rendered}, shelf=shelf)
            pages.append((
                f"reader/{slug}/index.html",
                wrap(book_body, active=self.name, title=book["title"],
                     dates=[], date=None, is_latest=False),
            ))
        log(f"      多页产出：{len(pages)} 个页面（{len(lib_books)} 本书）")
        return pages

    # ------------------------------------------------------------------ 内部
    def _latest_data(self, page_ctx: dict) -> dict | None:
        """取本模块最新的一份快照（就是 collect/analyze 刚落盘的那份）。"""
        from radar.core.store import read_json
        data_dir = page_ctx["data_dir"]
        dates = page_ctx.get("dates") or []
        for date in dates:
            payload = read_json(data_dir / self.name / f"{date}.json")
            if payload:
                return payload
        return None
