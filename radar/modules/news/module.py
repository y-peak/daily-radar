"""相关新闻 —— 和你的自选股、大盘有关的快讯，按主题分好组。

## 为什么做"相关"而不是"全部"

全网快讯一天几千条，全搬过来就又是一个没人看的瀑布流。这里做三件事把它变窄：

  1. **按主题分组**（规则见 `rules.py`），每组都有条数上限 ——
     一个没人能看完的页面等于没有
  2. **自选股优先**：命中名单里标的的新闻单独成组、放在最前面。
     "我的票今天有什么事"才是真正要看的（名单见 `data/watchlist.json`）
  3. **增量标记**：对比最近几天，标出今天**新出现**的（`fresh`）。
     快讯接口每次拉回来的都是"最近 N 条"，不标增量的话每次打开看到的是同一批

## 分工：跨日期的状态必须在 collect 里定下来

整站有一条硬规则：**analyze / render 只读今天这一份快照**。
所以下面这些全都发生在 collect，结果写进快照：

    · 跨源去重合并（同一条快讯在两个源上都有）
    · `fresh`（对比最近 N 天的 guid）
    · 自选股命中
    · 关键词分组

这样半年后重建今天这个页面，分组、标记、增量还是当时那一份 ——
"历史页面不可变"才成立。analyze 只做聚合、排序和（可选的）大模型导读。

## 容错

  * **两个源都挂** → 抛错，今天不写快照，页面继续显示昨天那份好的
  * **只有一个源挂** → 照常出页面，但 `errors` 里如实写，
    否则用户会以为"今天真的没有相关新闻"
  * **一条都合并不出来**（全是空标题之类）→ 抛错，理由同第一条
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from radar.core import prompts
from radar.core.llm import LLM
from radar.core.module import Context, Module, load_sibling

rules = load_sibling(__file__, "rules")
sources = load_sibling(__file__, "sources")

# 「自选股」是**算出来的**组，不来自规则文本 —— 名字固定，页面靠它决定置顶。
WATCH_GROUP = "自选股"

# 名称匹配的最小长度。**两个字的名字不做文本匹配** ——
# 「平安」「中国」「东方」这类词在财经新闻里到处都是，撞上就报"和你的自选股有关"
# 是假信号，比漏掉更糟（用户会开始不信这个页面）。两字名只认代码和源自带标的。
NAME_MATCH_MIN = 3

# 正文里出现的 6 位数字（A 股代码）——前后不能再挨着数字，
# 否则 12 位长串（订单号、身份证之类）会被切出一个假的 6 位码。
_RE_SIX = re.compile(r"(?<!\d)(\d{6})(?!\d)")

DIGEST_SYSTEM = prompts.system_for_untrusted(
    "你是财经简讯编辑，给个人投资者写一句话导读。"
    "不预测涨跌，不给投资建议，不推荐买卖。"
    "语言平实，避免『重磅』『利好』这类夸张词。"
)
DIGEST_USER = """\
下面是今天抓到的财经快讯（已按主题分组，每行"时间 | 分组 | 标题"）：
{lines}

请写一段 3-5 句的中文导读：
1. 先说市场层面今天最集中的方向是什么（哪些主题反复出现）
2. 再说和我的自选股有关的事（如果没有就说没有）
3. 最后一句提示接下来值得留意什么

不要逐条复述，不要用列表，不要写标题。直接输出 JSON：{{"digest": "……"}}
"""


# ------------------------------------------------------------------ 小工具
def _secid_of_cn(code: str) -> str:
    """6 位 A 股代码 → secid（`1.` 沪 / `0.` 深）。

    与 `watchlist/sources.guess_secid` 是**同一条规则**。这里没去 import 隔壁模块
    （模块目录不是包），改规则时两边都要改 —— 冒烟里有一条断言把两份实现在
    一批代码上逐一比对，漂了就红，所以这条注释不是"希望你记得"，是有东西兜着。
    """
    head = code[:1]
    return f"{1 if head in '569' else 0}.{code}"


def _secid_of_symbol(sym: dict) -> str:
    """源自带的关联标的 → secid。只处理个股，指数/主题在 sources 里已被丢掉。"""
    market = sym.get("market")
    code = str(sym.get("code") or "")
    if market == "cn" and len(code) == 6:
        return _secid_of_cn(code)
    if market == "hk" and code:
        return f"116.{code}"
    return ""


def _hhmm(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _ts_text(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _load_watchlist(data_dir) -> dict:
    """读自选股名单。

    ⚠️ 直接读 `data/watchlist.json`，**不 import 隔壁 watchlist 模块**。
    理由：模块目录不是包，跨目录 import 会绕过 registry 的动态加载；
    而名单本身是一份**扁平的数据文件**，形状写在 `watchlist/userlist.py`
    的模块注释里（`items[].secid/code/name`），读它比建立模块间依赖便宜得多。

    读不到 / 坏了 / 字段缺 → 一律当空名单。新闻页不该因为名单坏了打不开。
    """
    import json
    p = Path(data_dir) / "watchlist.json"
    out = {"updated_at": None, "items": []}
    try:
        if not p.is_file():
            return out
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    out["updated_at"] = data.get("updated_at")
    for raw in (data.get("items") or []):
        if not isinstance(raw, dict):
            continue
        secid = str(raw.get("secid") or "").strip()
        if not secid:
            continue
        out["items"].append({
            "secid": secid,
            "code": str(raw.get("code") or "").strip(),
            "name": str(raw.get("name") or "").strip(),
        })
    return out


def _watch_hits(item: dict, wl_items: list[dict]) -> list[dict]:
    """这条新闻和哪些自选股有关。三路匹配，优先级从硬到软。

    ① **源自带的关联标的** —— 新浪给了 `ext.stocks`，这是最硬的信号
       （它自己标的关联，不是我们拿文字瞎撞）
    ② **正文里的 6 位代码** —— 快讯里常直接写代码，精确无歧义
    ③ **名称匹配（只认 >= 3 个字的名字）** —— 详见 `NAME_MATCH_MIN` 的说明

    返回 `[{"secid","code","name"}]`，按名字稳定排序（页面每次渲染顺序一致）。
    """
    if not wl_items:
        return []
    by_secid = {it["secid"]: it for it in wl_items if it.get("secid")}
    hit: dict[str, dict] = {}

    for sym in (item.get("symbols") or []):
        if not isinstance(sym, dict):
            continue
        secid = _secid_of_symbol(sym)
        if secid in by_secid:
            hit[secid] = by_secid[secid]

    blob = f"{item.get('title') or ''} {item.get('text') or ''}"
    for code in set(_RE_SIX.findall(blob)):
        secid = _secid_of_cn(code)
        if secid in by_secid:
            hit[secid] = by_secid[secid]

    for it in wl_items:
        name = it.get("name") or ""
        if len(name) >= NAME_MATCH_MIN and name in blob:
            hit[it["secid"]] = it

    return sorted(hit.values(), key=lambda x: (x.get("name") or "", x["secid"]))


def _extra_symbols(item: dict) -> list[dict]:
    """源自带、但**不属于自选股**的关联标的。最多两个。

    为什么值得单独列：它常常正是"这条为什么值得看一眼"的答案
    （"公司股权激励的目标…？胜宏科技回应" → 标签写「胜宏科技」比写「公司」有用）。
    已经在 `hit` 里的不重复标 —— 页面上出现「自选 · 贵州茅台」再紧跟一个「贵州茅台」
    会让人以为哪里错了。
    """
    have = {h.get("secid") for h in (item.get("hit") or [])}
    out: list[dict] = []
    for s in (item.get("symbols") or []):
        if not isinstance(s, dict) or not s.get("name"):
            continue
        if _secid_of_symbol(s) in have:
            continue
        out.append({"code": str(s.get("code") or ""), "name": str(s.get("name") or "")})
        if len(out) >= 2:
            break
    return out


def _trim(item: dict) -> dict:
    """快照里每条只留这些字段 —— 页面和 LLM 都用不着更多的。"""
    return {
        "guid": item.get("guid") or "",
        "title": item.get("title") or "",
        "text": item.get("text") or "",
        "url": item.get("url") or "",
        "time": item.get("time") or "",
        "ts_text": item.get("ts_text") or "",
        "ts": item.get("ts") or 0,
        "group": item.get("group") or "",
        "fresh": bool(item.get("fresh")),
        "sources": item.get("sources") or [],
        "src_labels": item.get("src_labels") or [],
        "tags": item.get("tags") or [],
        # symbols 留着：它让快照自己说明"当时源标了哪些标的"，
        # 排查时不用回原始响应里翻；页面要显示的已在 extra_symbols 里截好
        "symbols": item.get("symbols") or [],
        "extra_symbols": _extra_symbols(item),
        "hit": item.get("hit") or [],
    }


def _src_set(item: dict) -> set:
    """这条来自哪些源 —— **统一成展示名**。

    ⚠️ 必须归一化：条目上同时有 `sources`（内部键 `wscn`/`sina`）和
    `src_labels`（展示名）。两边都往计数里塞的话，同一个源会被算两次
    （页面上就会出现"新浪财经 100 · 新浪财经 100"这种）。
    """
    labels = {"wscn": "华尔街见闻", "sina": "新浪财经"}
    out = set()
    for key in (item.get("sources") or []):
        if key:
            out.add(labels.get(str(key), str(key)))
    for lbl in (item.get("src_labels") or []):
        if lbl:
            out.add(str(lbl))
    return out


class News(Module):
    name = "news"
    title = "相关新闻"
    subtitle = "和自选股、大盘有关的快讯，按主题分组，标出今天新增"
    order = 22
    schedule = "每日 09/12/15/18/21 点"

    # ------------------------------------------------------------------ collect
    def collect(self, ctx: Context) -> Any:
        cfg = ctx.config.module_cfg(self.name) or {}

        res = sources.fetch_all(ctx.http, cfg, ctx.log_info)
        merged, dedup = rules.merge(res["items"])
        if not merged:
            raise RuntimeError("合并之后一条不剩（标题全空？）")
        ctx.log_info(f"      ✓ 原始 {dedup['in']} 条 → 去重合并 {dedup['out']} 条"
                     f"（合掉 {dedup['merged']}）")

        # ---- 跨日期状态：一律在这里定下来，写进快照（见模块注释）
        rule_text, rule_src = rules.load(ctx.data_dir, cfg.get("rules", ""))
        groups = rules.parse(rule_text)
        wl = _load_watchlist(ctx.data_dir)
        fresh_days = int(cfg.get("fresh_days", 3))
        seen = rules.recent_guids(ctx.data_dir, ctx.date, fresh_days)
        ctx.log_info(f"      · 规则来自 {rule_src}（{len(groups)} 组）；"
                     f"自选股 {len(wl['items'])} 只；"
                     f"最近 {fresh_days} 天已有 {len(seen)} 条可比对")

        for it in merged:
            hits = _watch_hits(it, wl["items"])
            blob = f"{it.get('title') or ''} {it.get('text') or ''}"
            it["hit"] = hits
            it["group"] = WATCH_GROUP if hits else rules.classify(blob, groups)
            it["fresh"] = it["guid"] not in seen
            it["time"] = _hhmm(it.get("ts"))
            it["ts_text"] = _ts_text(it.get("ts"))

        fresh_n = sum(1 for it in merged if it["fresh"])
        watch_n = sum(1 for it in merged if it.get("hit"))
        ctx.log_info(f"      ✓ 今日新增 {fresh_n} 条；命中自选股 {watch_n} 条")

        return {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "errors": res["errors"],
            "source_stats": res["stats"],
            "raw_count": res.get("raw_count", 0),
            "dedup": dedup,
            "rules_source": rule_src,
            "rules_text": rule_text,
            "group_meta": [g.to_meta() for g in groups],
            "watchlist": wl["items"],
            "watchlist_updated_at": wl["updated_at"],
            "seen_before": len(seen),
            "fresh_count": fresh_n,
            "watch_count": watch_n,
            "items": [_trim(it) for it in merged],
        }

    # ------------------------------------------------------------------ analyze
    def analyze(self, raw: Any, ctx: Context) -> Any:
        cfg = ctx.config.module_cfg(self.name) or {}
        items = [it for it in (raw.get("items") or []) if isinstance(it, dict)]
        limits = {m["name"]: m.get("limit", 20)
                  for m in (raw.get("group_meta") or []) if isinstance(m, dict)}
        watch_limit = int(cfg.get("watch_limit", 20))
        # 「未归类」默认只放很少几条：它装的是**没归进任何主题**的国际/社会新闻，
        # 一放就是几十条，整页的重点会被它冲掉。要看得更多就去改规则。
        other_limit = int(cfg.get("unclassified_limit", 8))

        # ---- 分组。顺序：自选股 → 规则里的顺序 → 未归类（永远垫底）
        buckets: dict[str, list[dict]] = {}
        for it in items:
            buckets.setdefault(it.get("group") or rules.FALLBACK_GROUP, []).append(it)

        order = [WATCH_GROUP]
        order += [m["name"] for m in (raw.get("group_meta") or [])
                  if isinstance(m, dict) and m.get("name")]
        order += [rules.FALLBACK_GROUP]
        seen_names: set[str] = set()
        groups: list[dict] = []
        for name in order:
            if name in seen_names or name not in buckets:
                continue
            seen_names.add(name)
            rows = buckets[name]
            if name == WATCH_GROUP:
                limit = watch_limit
            elif name == rules.FALLBACK_GROUP:
                limit = other_limit
            else:
                limit = limits.get(name, 20)
            groups.append(self._group(name, rows, limit, name == WATCH_GROUP))
        # 规则里没提、但数据里真出现了的组（用户改了规则又跑了一半之类）也别丢
        for name in sorted(buckets):
            if name in seen_names:
                continue
            groups.append(self._group(name, buckets[name], 20, name == WATCH_GROUP))

        # ---- 按标的聚一遍：页面顶部「我的票今天有什么事」
        watch_hits = self._by_stock(items, raw.get("watchlist") or [])

        shown = sum(len(g["items"]) for g in groups)
        by_hour = [0] * 24
        for it in items:
            try:
                by_hour[datetime.fromtimestamp(int(it.get("ts") or 0)).hour] += 1
            except (TypeError, ValueError, OSError, OverflowError):
                continue

        src_counts: dict[str, int] = {}
        for it in items:
            for label in _src_set(it):
                src_counts[label] = src_counts.get(label, 0) + 1
        labels = {"wscn": "华尔街见闻", "sina": "新浪财经"}
        src_display = []
        for key, n in sorted(src_counts.items(), key=lambda kv: -kv[1]):
            src_display.append({"name": labels.get(key, key), "count": n})

        tag_counts: dict[str, int] = {}
        for it in items:
            for t in (it.get("tags") or []):
                if t:
                    tag_counts[str(t)] = tag_counts.get(str(t), 0) + 1
        hot_tags = [{"name": k, "count": v}
                    for k, v in sorted(tag_counts.items(), key=lambda kv: -kv[1])[:12]]

        digest, digest_model = self._digest(ctx, cfg, groups, items)

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "fetched_at": raw.get("fetched_at"),
            "errors": raw.get("errors") or {},
            "source_stats": raw.get("source_stats") or {},
            "rules_source": raw.get("rules_source"),
            "watchlist": raw.get("watchlist") or [],
            "watchlist_updated_at": raw.get("watchlist_updated_at"),
            "empty": not items,
            "stats": {
                "total": len(items),
                "shown": shown,
                "fresh": sum(1 for it in items if it.get("fresh")),
                "watch": sum(1 for it in items if it.get("hit")),
                "symbols": sum(1 for it in items if it.get("symbols")),
                "raw": raw.get("raw_count", 0),
                "merged": (raw.get("dedup") or {}).get("merged", 0),
                "seen_before": raw.get("seen_before", 0),
                "sources": src_display,
            },
            "groups": groups,
            "watch_hits": watch_hits,
            "by_hour": by_hour,
            "hot_tags": hot_tags,
            "digest": digest,
            "digest_model": digest_model,
        }

    # ------------------------------------------------------------------ 分组
    @staticmethod
    def _group(name: str, rows: list[dict], limit: int, is_watch: bool) -> dict:
        """一组。**按时间倒序**，并且 `fresh` 的排在前面 ——
        同一组里"今天新出现的"比"昨天就有的"值得先看。"""
        ordered = sorted(
            rows,
            key=lambda it: (not it.get("fresh"), -(int(it.get("ts") or 0))),
        )
        keep = ordered[:max(0, int(limit))]
        return {
            "name": name,
            "is_watch": is_watch,
            "count": len(rows),
            "shown": len(keep),
            "limit": int(limit),
            "fresh": sum(1 for it in rows if it.get("fresh")),
            "items": keep,
        }

    # ------------------------------------------------------------------ 按标的
    @staticmethod
    def _by_stock(items: list[dict], watchlist: list[dict]) -> list[dict]:
        """把命中的新闻按标的聚起来。**每只最多给 5 条**，
        页面顶部一个小列表就够 —— 它是"要去看哪一组"的索引，不是第二个列表页。"""
        by_secid: dict[str, dict] = {}
        for it in items:
            for stock in (it.get("hit") or []):
                secid = stock.get("secid") or ""
                if not secid:
                    continue
                slot = by_secid.setdefault(secid, {
                    "secid": secid,
                    "code": stock.get("code") or "",
                    "name": stock.get("name") or "",
                    "count": 0,
                    "fresh": 0,
                    "items": [],
                })
                # 名单里的名字可能还没回填（用户只敲了代码），行情那边补上后这里就有了；
                # 空的话用最后一次拿到的非空名字兜住
                if not slot["name"] and stock.get("name"):
                    slot["name"] = stock["name"]
                slot["count"] += 1
                if it.get("fresh"):
                    slot["fresh"] += 1
                if len(slot["items"]) < 5:
                    slot["items"].append({
                        "guid": it.get("guid"),
                        "title": it.get("title"),
                        "time": it.get("time"),
                        "url": it.get("url"),
                        "fresh": bool(it.get("fresh")),
                    })
        # 名单里有、但今天一条新闻都没有的标的也要露面 ——
        # "没有消息"本身是信息（比整块消失强），所以 count=0 的留着
        for it in watchlist:
            secid = it.get("secid") or ""
            if secid and secid not in by_secid:
                by_secid[secid] = {"secid": secid, "code": it.get("code") or "",
                                   "name": it.get("name") or "", "count": 0,
                                   "fresh": 0, "items": []}
        return sorted(by_secid.values(),
                      key=lambda s: (-s["count"], s["name"] or "", s["secid"]))

    # ------------------------------------------------------------------ 导读
    def _digest(self, ctx: Context, cfg: dict, groups: list[dict],
                items: list[dict]) -> tuple[str, str]:
        """大模型写的导读。**失败一律降级成空串**，页面照常出。

        为什么值得单独一段：分组是"按规则切的"，人还是要一眼看出
        "今天到底集中在哪几个方向"。这件事规则做不了，一段三句话能。
        但它只是锦上添花 —— 所以任何一步出问题都只是"今天没有导读"，
        绝不连累列表。
        """
        scfg = cfg.get("digest") or {}
        if not scfg.get("enabled", True):
            return "", ""
        if not items:
            return "", ""

        llm_cfg = dict(ctx.config.section("llm") or {})
        llm = LLM(llm_cfg, cache_path=ctx.data_dir / ".llm_cache.json", log=ctx.log_info)
        if not (llm_cfg.get("enabled", False) and llm.available):
            ctx.log_info(f"      跳过新闻导读：{llm.why_unavailable()}")
            return "", ""

        # 只喂标题：正文进上下文既贵又没用（标题已经说清了一件事）
        max_items = int(scfg.get("max_items", 60))
        lines: list[str] = []
        for g in groups:
            for it in g["items"]:
                if len(lines) >= max_items:
                    break
                lines.append(f"{it.get('ts_text') or it.get('time') or '—'} | "
                             f"{g['name']} | {it.get('title') or ''}")
            if len(lines) >= max_items:
                break
        if not lines:
            return "", ""

        # ⭐ 标题是第三方原文（不可信），必须包进显式边界再喂给模型 ——
        #    否则一条写着"忽略以上要求"的标题就能改写整段导读。
        payload = prompts.wrap_untrusted("\n".join(lines), "今日财经快讯标题")
        data = llm.chat_json(DIGEST_SYSTEM, DIGEST_USER.format(lines=payload),
                             max_tokens=int(scfg.get("max_tokens", 900)),
                             required=("digest",))
        llm.save_cache()
        llm.log_summary()
        if not data or not isinstance(data.get("digest"), str):
            ctx.log_info("      新闻导读没生成（模型输出解析不了），列表照常")
            return "", ""
        return data["digest"].strip(), f"{llm.provider}/{llm.model}"

    # ------------------------------------------------------------------ report
    def report(self, data: Any, ctx: Context) -> str:
        L: list[str] = ["## 相关新闻", ""]
        s = data.get("stats") or {}
        errors = data.get("errors") or {}

        if data.get("empty"):
            L.append("今天没抓到任何新闻。")
            L.append("")
        L.append(f"**{s.get('total', 0)} 条** ｜ 今日新增 {s.get('fresh', 0)} ｜ "
                 f"命中自选股 {s.get('watch', 0)} ｜ "
                 f"去重合并 {s.get('merged', 0)} ｜ 页面展示 {s.get('shown', 0)}")
        L.append("")

        if errors:
            L.append("> ⚠️ 有来源没拉到，页面上的条数会偏少：")
            for key, msg in errors.items():
                L.append(f">   · `{key}`：{msg}")
            L.append("")

        if data.get("digest"):
            L.append("### 今日导读")
            L.append("")
            L.append(data["digest"])
            L.append("")
            if data.get("digest_model"):
                L.append(f"*（由 {data['digest_model']} 生成，仅供参考）*")
                L.append("")

        for g in data.get("groups") or []:
            if not g.get("items"):
                continue
            head = f"### {g['name']}（{g['shown']}/{g['count']}）"
            L.append(head)
            L.append("")
            for it in g["items"]:
                mark = "🆕 " if it.get("fresh") else ""
                tags = "".join(f" `{t}`" for t in (it.get("tags") or [])[:3])
                link = f"[{it['title']}]({it['url']})" if it.get("url") else it["title"]
                L.append(f"- {mark}**{it.get('time', '')}** {link}{tags}")
            L.append("")

        L.append("> 来源：华尔街见闻快讯、新浪财经 7×24（均为公开接口）。"
                 "分组与「自选股命中」都是机械规则，不构成任何投资建议。")
        return "\n".join(L)
