"""GitHub 日榜模块 —— 每天哪些仓库最热，以及它们各自在做什么。

"每个仓库是做什么的"分三层回答，逐层加深：

    第 1 层（永远有）  仓库自己的 description —— 作者写的一句话，通常最准
    第 2 层（永远有）  topics / 语言 / 活跃度 / 体积 —— 判断它是什么形态的东西
                       （几 MB 的工具 和 几百 MB 的模型仓库，用法完全不同）
                       再叠关键词规则推出"类别"，纯离线、可复现
    第 3 层（配了 key 才有）**读 README 正文 + 根目录结构，让大模型写一段中文介绍**

第 3 层是 2026-09-13 加的，解决的是这个具体问题：英文 description 常常只有
半句话（"A fast ORM for Python"），机翻出来还是半句话，读者看完仍然不知道
"这玩意解决什么问题、我该不该点进去"。给它更多原料（README/目录/语言构成）
才可能写出人话 —— 见 core/llm.py 和 sources.fetch_repo_ctx。

第 1、2 层**不依赖任何外部服务**，跑一万次结果一致；第 3 层是增强，
没配 key、余额不足、限流、超时 —— 任何一环挂掉都只是少一段介绍，
榜单和其余信息照常出。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from radar.core.llm import LLM
from radar.core.module import Context, Module, load_sibling
from radar.core.translate import Translator

sources = load_sibling(__file__, "sources")

# 关键词 → 类别，按顺序匹配，先命中先算
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("AI / 大模型", ("llm", "gpt", "ai-", "artificial-intelligence", "machine-learning",
                    "deep-learning", "transformer", "diffusion", "agent", "rag", "prompt",
                    "neural", "tensorflow", "pytorch", "inference", "openai", "embedding",
                    "chatbot", "vision", "nlp", "speech", "stable-diffusion", "vllm")),
    ("安全 / 逆向", ("security", "pentest", "exploit", "vulnerability", "malware",
                   "reverse-engineering", "cryptography", "encryption", "hacking", "cve")),
    ("数据库 / 存储", ("database", "postgres", "mysql", "sqlite", "redis", "mongodb",
                      "vector-database", "storage", "sql", "kafka", "queue", "cache",
                      "elasticsearch", "object-storage")),
    ("数据 / 分析", ("data-", "analytics", "etl", "pipeline", "spark", "warehouse",
                    "visualization", "dashboard", "chart", "bi", "scraping", "crawler")),
    ("前端 / UI", ("react", "vue", "svelte", "angular", "nextjs", "nuxt", "tailwind",
                  "css", "frontend", "ui", "component", "web-components", "design-system",
                  "html", "animation", "canvas", "three.js", "webgl")),
    ("后端 / 服务", ("server", "backend", "api", "microservice", "fastapi", "django",
                    "flask", "spring", "gin", "grpc", "graphql", "http-server",
                    "framework", "web-framework", "rest")),
    ("开发工具", ("cli", "tool", "devops", "terminal", "shell", "vim", "neovim", "editor",
                 "ide", "debug", "test", "lint", "formatter", "build", "package-manager",
                 "git", "docker", "kubernetes", "ci-cd", "productivity")),
    ("系统 / 底层", ("kernel", "operating-system", "compiler", "interpreter", "runtime",
                    "embedded", "driver", "firmware", "performance", "low-level",
                    "distributed", "networking", "protocol", "wasm")),
    ("移动 / 桌面", ("android", "ios", "flutter", "react-native", "desktop", "electron",
                    "tauri", "swiftui", "mobile", "qt", "gtk", "cross-platform")),
    ("学习 / 资源", ("awesome", "tutorial", "course", "book", "roadmap", "guide",
                    "examples", "cheatsheet", "interview", "learning", "notes", "blog")),
]

# 体积 → 形态判断（决定"这东西怎么用"）
def _form(size_kb: int | None) -> str:
    if not size_kb:
        return ""
    mb = size_kb / 1024
    if mb < 5:
        return "轻量库/工具"
    if mb < 60:
        return "中型项目"
    if mb < 500:
        return "大型项目"
    return "庞大仓库（多半含模型/数据）"


def _category(item: dict) -> str:
    haystack = " ".join([
        (item.get("description") or "").lower(),
        " ".join(item.get("topics") or []).lower(),
        (item.get("language") or "").lower(),
        item.get("name", "").lower(),
    ])
    for label, keys in CATEGORY_RULES:
        if any(k in haystack for k in keys):
            return label
    return "其他"


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def _fmt_int(value) -> str:
    if value is None:
        return "—"
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    return f"{value:,}"


# --------------------------------------------------- API 失败时的"沿用上一轮"
# 为什么需要它：`api.github.com` **未认证只有 60 次/小时**。某轮撞上限流之后，
# 如果什么都不继承，这一轮产出的页面会比上一轮**更差**
# （没 topics、没根目录、没许可证，README 摘要也没了），而数据文件是**覆盖写**的
# —— 读者看到的是退化版，而且看不出发生过什么。实测踩过：
#     一轮 enriched=10、上下文 10/10、25344 tokens
#     下一轮额度耗尽 → enriched=0、上下文 0/10、9373 tokens，把好数据盖掉了
#
# 处理方式：API 拿不到时，用**今天更早那一轮**已落盘的事实字段兜底。
# 最坏也只是数字略旧，不会退化成"什么都没写"。
_FALLBACK_KEYS = ("topics", "license", "homepage", "size_kb", "created_at",
                  "pushed_at", "stars", "forks", "open_issues", "archived")


def _load_prev_items(ctx: Context, name: str) -> dict[str, dict]:
    """读今天更早一轮的原始落盘，按 full_name 建索引。没有就返回空字典。"""
    try:
        data = json.loads(ctx.raw_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    items = data.get("items") if isinstance(data, dict) else None
    return {it["full_name"]: it for it in (items or [])
            if isinstance(it, dict) and it.get("full_name")}


def _inherit(it: dict, old: dict | None) -> list[str]:
    """把上一轮有、这一轮缺的字段搬过来。**只填空白，绝不覆盖新拿到的值。**

    ⭐ 最要紧的是 `pushed_at`：仓库上下文的磁盘缓存正是按它做 key 的。
    拿不到 pushed_at → 缓存 key 全失配 → 连 README 都要重新抓，
    而"没有额度"恰恰就是这一轮的问题所在。所以修好 pushed_at
    等于顺带修好了"上下文缓存集体失效"。
    """
    if not old:
        return []
    moved: list[str] = []
    for key in _FALLBACK_KEYS:
        if it.get(key) in (None, "", [], {}) and old.get(key) not in (None, "", [], {}):
            it[key] = old[key]
            moved.append(key)
    # 描述也兜一下：API 返回的通常比 Trending 页面上那句完整
    if not it.get("description") and old.get("description"):
        it["description"] = old["description"]
        moved.append("description")
    return moved


# --------------------------------------------------------- 大模型总结（第 3 层）
# 提示词的两条纪律，都是为了压住大模型的通病：
#   1. "不要重复仓库名" —— 否则 tagline 十有八九写成"XX 是一个……"
#   2. "资料里没提到就留空" —— 不这么说，它会给所有仓库编一段 pip install
_SUMMARY_SYSTEM = (
    "你是资深工程师，负责给中文开发者写技术简报，帮他们快速判断一个开源项目"
    "值不值得看。只依据提供的事实，不要脑补、不要编造用法或性能数据。只输出 JSON。"
)

_SUMMARY_USER = """下面是一个 GitHub 仓库的资料，请写一段中文介绍。

资料：
{card}

输出 JSON，三个字段：
{{"tagline": "一句话说清它是什么（20 字以内，不要以仓库名开头）",
 "detail": "2~3 句话：它解决什么问题、关键做法或亮点是什么",
 "usage": "一句话：典型使用场景或上手方式。资料里没写到就填空字符串，不要编"}}"""


def _build_card(it: dict) -> str:
    """把仓库资料拼成给模型看的"资料卡"。只喂事实，不喂结论。"""
    lines = [
        f"仓库：{it.get('full_name')}",
        f"官方简介：{it.get('description') or '（无）'}",
        f"主要语言：{it.get('language') or '未知'}",
        f"主题标签：{', '.join((it.get('topics') or [])[:10]) or '（无）'}",
        f"今日新增 star：{_fmt_int(it.get('stars_growth'))}，总计：{_fmt_int(it.get('stars'))}",
        f"仓库体积：{_fmt_int(it.get('size_kb'))} KB",
        f"许可证：{it.get('license') or '未声明'}",
        f"最近一次提交：{it.get('pushed_at') or '未知'}",
        f"创建时间：{it.get('created_at') or '未知'}",
    ]
    tree = it.get("_tree")
    if tree:
        lines.append(f"根目录结构：{tree}")
    readme = it.get("_readme")
    if readme:
        lines.append("\nREADME 内容（可能被截断）：\n" + readme)
    return "\n".join(lines)


def _summarize_one(llm: LLM, it: dict, max_tokens: int) -> dict:
    """给一个仓库生成中文介绍。失败/资料太少返回空字典。"""
    # 连简介和 README 都没有的话，模型只能瞎猜 —— 不如不写
    if not (it.get("description") or it.get("_readme") or it.get("topics")):
        return {}
    data = llm.chat_json(_SUMMARY_SYSTEM, _SUMMARY_USER.format(card=_build_card(it)),
                         max_tokens=max_tokens,
                         required=("tagline", "detail"))
    if not data:
        return {}
    out = {
        "ai_tagline": str(data.get("tagline") or "").strip(),
        "ai_detail": str(data.get("detail") or "").strip(),
        "ai_usage": str(data.get("usage") or "").strip(),
        "ai_model": llm.model,
    }
    return out if out["ai_tagline"] or out["ai_detail"] else {}


class GithubTrending(Module):
    name = "github_trending"
    title = "GitHub 日榜"
    subtitle = "每天最热的 10 个仓库，以及它们各自在做什么"
    order = 10
    schedule = "每日 08:00"

    # ------------------------------------------------------------------ collect
    def collect(self, ctx: Context) -> Any:
        cfg = ctx.config.module_cfg(self.name)
        since = cfg.get("since", "daily")
        top_n = int(cfg.get("top_n", 10))
        fcfg = cfg.get("fetch") or {}
        ua = fcfg.get("user_agent") or ctx.config.section("http").get("user_agent", "Mozilla/5.0")
        token = (cfg.get("github_token") or "").strip()

        result = sources.fetch_trending(fcfg, since, log=ctx.log_info)
        items = sources.parse_trending(result["html"])
        if not items:
            raise RuntimeError("Trending 页面解析出 0 条，页面结构可能变了")

        picked = items[:top_n]

        # 今天更早那一轮的结果：只在 API 拿不到时用来兜底（见 _inherit 的说明）
        prev = _load_prev_items(ctx, self.name)

        enriched = 0
        from_prev = 0
        if cfg.get("api_enrich", True):
            ctx.log_info(f"      补全 {len(picked)} 个仓库的详情…")
            for it in picked:
                meta = sources.fetch_repo_meta(it["full_name"], ua, token=token)
                if meta.get("_error"):
                    it["meta_error"] = meta["_error"]
                    if _inherit(it, prev.get(it["full_name"])):
                        from_prev += 1
                        it["meta_from_prev"] = True
                    continue
                enriched += 1
                it.update({k: v for k, v in meta.items() if k != "_error"})
                # description 以 API 为准（页面上的可能被截断）
                if meta.get("description"):
                    it["description"] = meta["description"]
            if from_prev:
                ctx.log_info(
                    f"      ⚠️ {from_prev}/{len(picked)} 个仓库沿用今天上一轮的数据"
                    f"（API 不可用，原因见 meta_error；页面不会因此退化）")
        else:
            ctx.log_info("      已跳过 API 补全（api_enrich=false）")

        # ------------------------------------------------- 大模型（翻译 + 总结）
        # 两个用途共用一个实例、一份缓存，key 也只读一次：
        #   翻译 description（让哪一层都读得懂中文）
        #   写总结（读 README 后产出 tagline/detail/usage）
        llm_cfg = dict(ctx.config.section("llm") or {})
        tcfg = cfg.get("translate") or {}
        scfg = cfg.get("summarize") or {}
        # 翻译/总结任一方要用，就把大模型建起来（llm.enabled 关掉则整体不用）
        llm = LLM(llm_cfg, cache_path=ctx.data_dir / ".llm_cache.json", log=ctx.log_info)
        if llm_cfg.get("enabled", False) and not llm.available:
            ctx.log_info(f"      大模型未启用：{llm.why_unavailable()}，"
                         f"走免 key 翻译")

        # ---------------------------------------------------------- 翻译成中文
        # 放在 collect 而不是 analyze：analyze 是纯函数（同样的输入必得同样的输出），
        # 翻译要发网络请求，属于采集阶段的活。
        if tcfg.get("enabled", True):
            ctx.log_info("      翻译简介为中文…")
            tr = Translator(tcfg, http=ctx.http,
                            cache_path=ctx.data_dir / ".translate_cache.json",
                            log=ctx.log_info, llm=llm)
            zh = tr.translate_batch([it.get("description") or "" for it in picked])
            for it, z in zip(picked, zh):
                it["description_zh"] = z
        else:
            ctx.log_info("      已跳过翻译（translate.enabled=false）")

        # --------------------------------------------- 读代码 + 写中文介绍
        summarize_on = scfg.get("enabled", True) and llm.available
        if summarize_on:
            ctx.log_info(f"      读取 README / 根目录，请大模型写介绍"
                         f"（{llm.provider}/{llm.model}）…")
            cache = sources.RepoContextCache(
                ctx.data_dir / self.name / ".repo_ctx.json", log=ctx.log_info)
            got_ctx = 0
            for it in picked:
                c = sources.fetch_repo_ctx(it["full_name"], ua, token=token,
                                           cache=cache, pushed_at=it.get("pushed_at"))
                if c.get("readme") or c.get("entries"):
                    got_ctx += 1
                it["_readme"] = c.get("readme") or ""
                it["_tree"] = sources.format_tree(c.get("entries") or [])
                if c.get("errors"):
                    it["ctx_errors"] = c["errors"]
            cache.save()
            ctx.log_info(f"      拿到 {got_ctx}/{len(picked)} 个仓库的代码上下文")

            n_ok = 0
            summarize_limit = int(scfg.get("max_items", len(picked)))
            # 默认给到 2000：带思考的模型（deepseek-flash）reasoning 与正文
            # 共用这个预算，按"够写 500 字正文"来配会全是思考、正文为空。
            per_tokens = int(scfg.get("max_tokens", 2000))
            for it in picked[:max(0, summarize_limit)]:
                info = _summarize_one(llm, it, per_tokens)
                if info:
                    it.update(info)
                    n_ok += 1
            ctx.log_info(f"      生成 {n_ok}/{len(picked)} 段中文介绍")
            llm.save_cache()
            llm.log_summary()
        elif scfg.get("enabled", True):
            ctx.log_info(f"      已跳过中文介绍：{llm.why_unavailable()}")

        # ⚠️ README 正文只留在内存里给提示词用。落盘时换成前 800 字摘要：
        #    raw/<date>.json 是每天一份的历史快照，整篇 README（几 KB 到几十 KB）
        #    塞进去会让 data/ 迅速膨胀，而它除了排查时看一眼，没有别的用途。
        #    根目录结构倒是照原样留着 —— 它只有一行，而且值得显示给读者看。
        for it in picked:
            readme = it.pop("_readme", "") or ""
            if readme:
                it["readme_excerpt"] = readme[:800]
            it["tree"] = it.pop("_tree", "") or ""

        return {
            "source": {
                "url": result["url"], "since": since,
                "ip": result["ip"], "elapsed": result["elapsed"],
                "attempt": result["attempt"],
                "page_items": len(items), "picked": len(picked),
                "enriched": enriched, "from_prev": from_prev,
            },
            "items": picked,
        }

    # ------------------------------------------------------------------ analyze
    def analyze(self, raw: Any, ctx: Context) -> Any:
        cfg = ctx.config.module_cfg(self.name)
        items = []
        for it in raw.get("items", []):
            growth = it.get("stars_growth")
            total = it.get("stars_total") or it.get("stars")
            pushed_days = _days_since(it.get("pushed_at"))
            created_days = _days_since(it.get("created_at"))

            # "为什么上榜"：今日新增占总 star 的比例越高，说明是刚被引爆的
            ratio = None
            if growth and total:
                ratio = round(growth / total * 100, 1)

            items.append({
                "rank": it.get("rank"),
                "full_name": it.get("full_name"),
                "url": it.get("url"),
                "owner": it.get("owner"),
                "slug": it.get("name"),
                # 中文翻译优先；翻不出来（接口挂了/没额度）就退回英文原文 ——
                # 翻译失败绝不能让报告开天窗。
                "what": (it.get("description_zh") or it.get("description")
                         or "（作者未填写描述）"),
                # 英文原文保留：页面挂在 title 属性上，鼠标悬停能看，
                # 但默认不显示，免得一屏英文干扰阅读。
                "what_en": it.get("description") or "",
                # 第 3 层：大模型读完 README 写的中文介绍（没配 key 时全是空串，
                # 模板里用 if 判空，页面自然退化回两层结构）
                "ai_tagline": it.get("ai_tagline") or "",
                "ai_detail": it.get("ai_detail") or "",
                "ai_usage": it.get("ai_usage") or "",
                # 根目录结构：让读者对"代码长什么样"有个直观印象
                "tree": it.get("tree") or "",
                "language": it.get("language") or "",
                "topics": (it.get("topics") or [])[:8],
                "category": _category(it),
                "form": _form(it.get("size_kb")),
                "stars_growth": growth,
                "stars_total": total,
                "stars_growth_text": _fmt_int(growth),
                "stars_total_text": _fmt_int(total),
                "growth_ratio": ratio,
                "forks": it.get("forks"),
                "license": it.get("license") or "",
                "homepage": it.get("homepage") or "",
                "pushed_days": pushed_days,
                "created_days": created_days,
                "archived": bool(it.get("archived")),
                "meta_error": it.get("meta_error", ""),
            })

        # ---- 群体观察：语言 / 类别分布，谁最猛，谁是新人
        langs: dict[str, int] = {}
        cats: dict[str, int] = {}
        for it in items:
            if it["language"]:
                langs[it["language"]] = langs.get(it["language"], 0) + 1
            cats[it["category"]] = cats.get(it["category"], 0) + 1

        by_growth = sorted([i for i in items if i["stars_growth"]],
                           key=lambda x: x["stars_growth"], reverse=True)
        newcomers = [i for i in items
                     if i["created_days"] is not None and i["created_days"] <= 180]
        stale = [i for i in items
                 if i["pushed_days"] is not None and i["pushed_days"] > 60]

        return {
            "source": raw.get("source", {}),
            "count": len(items),
            # 注意：这里不能叫 "items" —— Jinja 里 `data.items` 会先命中 dict 的
            # .items() 方法而不是这个键，模板会拿到一个函数然后报错。
            "repos": items,
            "stats": {
                "languages": sorted(langs.items(), key=lambda kv: -kv[1]),
                "categories": sorted(cats.items(), key=lambda kv: -kv[1]),
                "top_gainer": by_growth[0]["full_name"] if by_growth else None,
                "top_growth": by_growth[0]["stars_growth"] if by_growth else None,
                "newcomers": [i["full_name"] for i in newcomers],
                "stale": [i["full_name"] for i in stale],
                "with_topics": sum(1 for i in items if i["topics"]),
                "with_ai": sum(1 for i in items if i["ai_tagline"] or i["ai_detail"]),
                "with_tree": sum(1 for i in items if i["tree"]),
                "avg_growth": (
                    round(sum(i["stars_growth"] for i in by_growth) / len(by_growth))
                    if by_growth else None),
            },
        }

    # ------------------------------------------------------------------- report
    def report(self, data: Any, ctx: Context) -> str:
        items = data.get("repos", [])
        stats = data.get("stats", {})
        src = data.get("source", {})
        lines: list[str] = []

        lines.append(f"# GitHub 日榜 · {ctx.date}")
        lines.append("")
        lines.append(f"> 榜单来自 GitHub 官方 Trending（{src.get('since', 'daily')}），"
                     f"抓到 {src.get('page_items', 0)} 条、取前 {data.get('count', 0)} 条。"
                     f"用时 {src.get('elapsed', '?')}s。")
        lines.append("")

        lines.append("## 今日十强")
        lines.append("")
        for it in items:
            flag = " ⚠️已归档" if it["archived"] else ""
            lines.append(f"**{it['rank']}. [{it['full_name']}]({it['url']})**{flag}  ")
            # ⚠️ 新加的字段一律用 .get()：数据目录里存着**今天之前生成的历史快照**，
            #    它们没有 ai_* / tree 这些后加的键。直接 it["ai_tagline"] 会让
            #    "对旧数据重出报告" 直接 KeyError 崩掉 —— 老数据不该让新代码报错，
            #    这是本项目从第一版就定下的规矩（模板那边同理，Jinja 的
            #    Undefined 天然是假值，所以模板不用改）。
            ai_tagline = it.get("ai_tagline") or ""
            ai_detail = it.get("ai_detail") or ""
            ai_usage = it.get("ai_usage") or ""
            # 有中文介绍就先给结论，再给作者原话 —— 读者的阅读顺序应该是
            # "这是干嘛的" → "细节" → "数字"，而不是先读一行英文再自己猜
            if ai_tagline:
                lines.append(f"**{ai_tagline}**  ")
            if ai_detail:
                lines.append(f"{ai_detail}  ")
            lines.append(f"{it['what']}  ")
            if ai_usage:
                lines.append(f"　📌 {ai_usage}  ")
            bits = [f"今日 +{it['stars_growth_text']} ★",
                    f"总计 {it['stars_total_text']} ★"]
            if it["language"]:
                bits.append(it["language"])
            if it["category"]:
                bits.append(it["category"])
            if it["form"]:
                bits.append(it["form"])
            lines.append("　" + " ｜ ".join(bits) + "  ")
            if it["topics"]:
                lines.append("　`" + "` `".join(it["topics"]) + "`  ")
            if it.get("tree"):
                lines.append(f"　📁 `{it['tree']}`  ")
            if it["pushed_days"] is not None and it["pushed_days"] > 60:
                lines.append(f"　⚠️ 已 {it['pushed_days']} 天没有新提交")
            lines.append("")

        lines.append("## 观察")
        lines.append("")
        if stats.get("languages"):
            top = "、".join(f"{lang} {n} 个" for lang, n in stats["languages"][:5])
            lines.append(f"- **语言分布**：{top}")
        if stats.get("categories"):
            top = "、".join(f"{c} {n} 个" for c, n in stats["categories"][:5])
            lines.append(f"- **类别分布**：{top}")
        if stats.get("top_gainer"):
            lines.append(f"- **涨得最猛**：`{stats['top_gainer']}`"
                         f"，今日 +{stats['top_growth']} ★")
        if stats.get("avg_growth"):
            lines.append(f"- **平均涨幅**：今日约 +{stats['avg_growth']} ★")
        if stats.get("newcomers"):
            lines.append(f"- **新面孔（创建半年内）**："
                         + "、".join(f"`{n}`" for n in stats["newcomers"]))
        if stats.get("stale"):
            lines.append(f"- **热度与更新脱节**（超 60 天无提交）："
                         + "、".join(f"`{n}`" for n in stats["stale"]))
        lines.append("")

        if stats.get("top_gainer"):
            lines.append(f"> 一句话：今天榜单的主角是 `{stats['top_gainer']}`；"
                         f"整体重心落在「{(stats.get('categories') or [('—', 0)])[0][0]}」这一类。")
            lines.append("")
        return "\n".join(lines)
