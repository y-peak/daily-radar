"""书架清单 —— 阅读器的"数据源声明"。

和 `market_flow/sources.py` 同一个角色：**把"从哪来、什么顺序、怎么用"集中在一处**，
业务代码只消费不发明。加一本书 = 往 `BOOKS` 里加一条 + 把正文放进
`radar/static/books/<slug>/`，模块代码一行不用改。

两条刻意的设计：
  * **章节顺序与 slug 显式写死**，不从文件名猜。书架是策展过的内容，
    顺序是产品决定；而且 URL（`/reader/<book>/<chapter>/`）一旦公开就不能变，
    靠"自然排序"推导早晚会因改文件名而断链。
  * **内容按 commit 钉死**。`commit` 记录的是正文来源的版本，
    阅读器的"历史页面不可变"就建立在这上面 —— 书稿不动，页面就不该动。
    要更新书 → 显式改 commit 并重新落地，是一次有意识的操作，而不是每天自动漂移。

许可：两本书都是 Apache-2.0（见各自 LICENSE），仓库里的正文与配图可再分发。
"""

from __future__ import annotations

from pathlib import Path

# 内容落盘位置：放在 radar/static/ 下，这样核心构建时会把它们
# 一起复制到 public/static/ —— 图片就能以 /static/books/... 直接引用，
# 不需要为"模块自己的静态资源"新增一套机制。
BOOKS_SUBDIR = "books"

BOOKS: list[dict] = [
    {
        "slug": "ai-infra",
        "title": "深入理解 AI Infra",
        "subtitle": "量化分析与系统设计",
        "author": "李博杰（bojieli）",
        "repo": "https://github.com/bojieli/ai-infra-book",
        "license": "Apache-2.0",
        "ref": "main",
        "commit": "b069b6971faf6c4701b57a8b025462b9f2694b8f",
        "source_root": "manuscripts",       # 正文在仓库里的目录
        "summary": "从硬件约束和模型架构出发，量化推导系统设计 —— 姊妹篇里「基础设施」的那一半。",
        "accent": "blue",
        "order": 10,
        "chapters": [
            ("00-前言.md", "preface", "前言"),
            ("01-初识 AI Infra.md", "ch01", "初识 AI Infra"),
            ("02-模型架构.md", "ch02", "模型架构"),
            ("03-推理与训练负载.md", "ch03", "推理与训练负载"),
            ("04-加速器架构.md", "ch04", "加速器架构"),
            ("05-算子与运行时.md", "ch05", "算子与运行时"),
            ("06-超节点.md", "ch06", "超节点"),
            ("07-数据中心网络.md", "ch07", "数据中心网络"),
            ("08-推理优化.md", "ch08", "推理优化"),
            ("09-分布式推理.md", "ch09", "分布式推理"),
            ("10-训练系统.md", "ch10", "训练系统"),
            ("11-资源调度与运行环境.md", "ch11", "资源调度与运行环境"),
            ("12-端边云协同.md", "ch12", "端边云协同"),
        ],
    },
    {
        "slug": "ai-agent",
        "title": "深入理解 AI Agent",
        "subtitle": "设计原理与工程实践",
        "author": "李博杰（bojieli）",
        "repo": "https://github.com/bojieli/ai-agent-book",
        "license": "Apache-2.0",
        "ref": "main",
        "commit": "3a0f2d0fb29a073b79e6c9d08c613d6c139e6272",
        "source_root": "book",
        "summary": "围绕 Agent = LLM + 上下文 + 工具，用 10 章从原理讲到工程实战。",
        "accent": "purple",
        "order": 20,
        "chapters": [
            ("introduction.md", "intro", "引言"),
            ("chapter1.md", "ch01", "AI Agent 入门"),
            ("chapter2.md", "ch02", "上下文工程"),
            ("chapter3.md", "ch03", "用户记忆和知识库"),
            ("chapter4.md", "ch04", "工具"),
            ("chapter5.md", "ch05", "Coding Agent 与通用 Agent"),
            ("chapter6.md", "ch06", "交互：观察与动作空间的扩展"),
            ("chapter7.md", "ch07", "Agent 的评估"),
            ("chapter8.md", "ch08", "模型后训练"),
            ("chapter9.md", "ch09", "Agent 的持续进化"),
            ("chapter10.md", "ch10", "多 Agent 协作"),
            ("afterword.md", "afterword", "后记"),
            ("reference-answers.md", "answers", "参考答案"),
        ],
    },
]


def books_root(static_dir: Path) -> Path:
    return static_dir / BOOKS_SUBDIR


def book_dir(static_dir: Path, book: dict) -> Path:
    return books_root(static_dir) / book["slug"]


def asset_prefix(book: dict) -> str:
    """图片 URL 前缀 —— 与核心把 static/ 复制成 public/static/ 的路径对应。"""
    return f"/static/{BOOKS_SUBDIR}/{book['slug']}/"


def validate(static_dir: Path) -> list[str]:
    """检查清单与磁盘是否对得上，返回问题列表（空 = 一切正常）。

    这是"**拿不到数据不许装作没事**"的落点：清单里声明了某一章、
    但文件不在，必须显式报出来，而不是让那一章悄悄消失。
    """
    problems: list[str] = []
    for book in BOOKS:
        folder = book_dir(static_dir, book)
        if not folder.is_dir():
            problems.append(f"{book['slug']}：目录不存在 {folder}")
            continue
        for filename, slug, _title in book["chapters"]:
            path = folder / filename
            if not path.is_file():
                problems.append(f"{book['slug']}：缺少章节文件 {filename}")
            elif path.stat().st_size < 200:
                problems.append(f"{book['slug']}：章节文件过小（疑似 LFS 指针）{filename}")
        # slug 必须唯一，否则页面会互相覆盖
        slugs = [c[1] for c in book["chapters"]]
        dupes = {s for s in slugs if slugs.count(s) > 1}
        if dupes:
            problems.append(f"{book['slug']}：章节 slug 重复 {sorted(dupes)}")
    return problems
