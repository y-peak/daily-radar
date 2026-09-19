"""新闻的分组规则、去重、增量判定。

## 规则语法

借的是 TrendRadar 那套关键词语法的**简化版**，只留真正用得上的四条：

    # 这是注释
    [大盘]                    起一个新组
    +A股 / 沪指 / 上证         必须命中其中之一（同一组里写多行 `+` 会合并成一个 OR）
    !打新 / 广告               命中任一即**从本组剔除**（优先级高于 `+`）
    @12                       本组最多留 12 条（按时间倒序取）
    /停牌|退市/                整行以 / 开头结尾 → 当**正则**用

配套的 `@` 是必需的而不是装饰：不设上限的话「公司」组一天能收两百条，
页面上谁也看不完 —— **一个没人看的页面等于没有**。

## 归属：先到先得

一条新闻**只属于一个组**，按规则**从上到下**第一个命中的组就是它的家。
这样行为完全可预测：想优先看的组写在上面。

## 自选股不走关键词

「自选股」组的命中**不靠规则文本**，靠 `data/watchlist.json` 里的名单
（代码精确匹配 + 源自带标的 + 名称匹配），而且**优先级最高** ——
命中了就直接进那一组，根本不再参与关键词分组。
理由：用户盯着哪几只，页面就得先告诉他"这几只有什么事"，
拿关键词去撞名字（"茅台" vs "贵州茅台"）必然漏。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

RULES_FILE = "news_rules.txt"

# 默认规则。**写在代码里是刻意的**：这是"开箱即用"的一部分，
# 用户不配也能得到一个像样的页面；想改就放 `data/news_rules.txt` 覆盖它
# （data/ 不入库，所以个人偏好不会跟着仓库跑）。
DEFAULT_RULES = """\
# 每组：[组名] 起头，+ 必须有，! 排除，@ 最多保留几条
# 自选股不用写在这里 —— 它按 data/watchlist.json 的名单匹配，且优先级最高。
#
# ⚠️ 组的先后就是归属的先后（先到先得），所以"更专的"要写在前面。
#    「全球」放在「公司」前面是刻意的：讲美联储/油价的稿子经常顺带提一句公司，
#    先撞「公司」的话，全球大事会被塞进公司组里，两个组一起失真。

[大盘]
+A股 / 沪指 / 上证 / 深证 / 创业板 / 科创板 / 北证 / 两市 / 大盘 / 指数 / 收盘 / 开盘 / 午评
+涨停 / 跌停 / 成交额 / 北向 / 主力 / 龙虎榜 / 板块
@12

[政策]
+央行 / 证监会 / 交易所 / 发改委 / 财政部 / 国常会 / 国务院 / 政治局 / 工信部 / 商务部
+降准 / 降息 / 逆回购 / MLF / LPR / 专项债 / 关税 / 反垄断 / 新规 / 征求意见
@12

[全球]
+美联储 / 美股 / 纳斯达克 / 道琼斯 / 标普 / 欧央行 / 日央行 / 英国央行 / 欧洲央行
+原油 / 黄金 / 汇率 / 美元 / 通胀 / 非农 / CPI / 国债收益率 / 加息 / 降息 / 欧佩克
@12

[公司]
+业绩 / 预增 / 预亏 / 回购 / 增持 / 减持 / 定增 / 重组 / 并购 / 中标 / 签约
+停产 / 退市 / 立案 / 问询 / 处罚 / 换帅 / 分红 / 解禁 / 涨停板
@15

[其它]
+公司 / 市场 / 行业 / 机构 / 数据
@10
"""

# 兜底组：连「其它」都没命中时放这里。名字固定，页面拿它当"没归类的"。
FALLBACK_GROUP = "未归类"
MAX_GROUP_ITEMS = 60          # 单组硬上限（@ 只能调小，不能超过它）


# ------------------------------------------------------------------ 解析
class Group:
    """一个分组规则。"""

    __slots__ = ("name", "must", "deny", "limit", "regexes")

    def __init__(self, name: str):
        self.name = name
        self.must: list[str] = []
        self.deny: list[str] = []
        self.limit = 20
        self.regexes: list[re.Pattern] = []

    def match(self, text: str) -> bool:
        """`+` 命中任一 且 `!` 一条都没命中 且 所有正则都命中。

        ⚠️ `+` 与 `/正则/` 之间是 **AND**、`+` 内部是 **OR** —— 这个组合是刻意的：
        正则用来表达"必须同时满足的额外约束"，比如 `/公告/` 配 `+回购 / 减持`。
        """
        low = text.lower()
        if self.must and not any(w.lower() in low for w in self.must):
            return False
        if self.deny and any(w.lower() in low for w in self.deny):
            return False
        for rx in self.regexes:
            if not rx.search(text):
                return False
        return True

    def to_meta(self) -> dict:
        return {"name": self.name, "limit": self.limit,
                "must": len(self.must), "deny": len(self.deny),
                "regexes": len(self.regexes)}


def _split_words(rest: str) -> list[str]:
    return [w.strip() for w in re.split(r"[/|,，、]+", rest) if w.strip()]


def parse(text: str) -> list[Group]:
    """把规则文本解析成 Group 列表。

    **坏行一律跳过，不抛异常** —— 这份文本将来可能由用户手改，
    一行写错不该让整个新闻页消失。跳过多少行由调用方比较数量后写进日志。
    """
    groups: list[Group] = []
    cur: Group | None = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]") and len(line) > 2:
            cur = Group(line[1:-1].strip() or FALLBACK_GROUP)
            groups.append(cur)
            continue
        if cur is None:
            # 组名之前的裸词：自己起一个组兜住，别把用户写的规则丢掉
            cur = Group(FALLBACK_GROUP)
            groups.append(cur)
        if line.startswith("+"):
            cur.must += _split_words(line[1:])
        elif line.startswith("!"):
            cur.deny += _split_words(line[1:])
        elif line.startswith("@"):
            try:
                cur.limit = max(1, min(MAX_GROUP_ITEMS, int(line[1:].strip())))
            except ValueError:
                pass
        elif len(line) > 2 and line.startswith("/") and line.endswith("/"):
            try:
                cur.regexes.append(re.compile(line[1:-1]))
            except re.error:
                pass
        else:
            # 裸词当成 `+词`（少写一个 + 是很容易犯的错，静默丢掉会让人以为规则没生效）
            cur.must += _split_words(line)
    return [g for g in groups if g.must or g.deny or g.regexes]


def load(data_dir, fallback: str = "") -> tuple[str, str]:
    """返回 `(规则文本, 来源说明)`。

    优先 `data/news_rules.txt`（用户可改、不入库），没有就用 `fallback`，
    再没有就用内置默认。
    """
    p = Path(data_dir) / RULES_FILE
    try:
        if p.is_file():
            text = p.read_text(encoding="utf-8")
            if text.strip():
                return text, f"{RULES_FILE}（你自己改的）"
    except OSError:
        pass
    if (fallback or "").strip():
        return fallback, "config.json"
    return DEFAULT_RULES, "内置默认"


def classify(text: str, groups: list[Group]) -> str:
    """按顺序找第一个命中的组。都不中就进 `未归类`。"""
    for g in groups:
        if g.match(text):
            return g.name
    return FALLBACK_GROUP


# ------------------------------------------------------------------ 去重
# 去重键里要抹掉的字符。⚠️ 省略号 `…` 也在里面：
# 它同样是不带信息的装饰符，而「……」「！！…」这种"纯标点标题"在快讯流里真会出现 ——
# 漏掉它，这些条目会各自拿到一个**非空**的 guid，于是既没被丢掉、
# 又长得一模一样地挤在页面上（本该被当成"无标题"整条丢弃）。
_PUNCT = re.compile(r"[\s\u3000·、，,。.!！?？:：;；\"'“”‘’()（）\[\]【】<>《》—…\-_/\\|~`*+#]+")
_FULLWIDTH = str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ"
                           "ａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                           "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                           "abcdefghijklmnopqrstuvwxyz")


def norm_title(title: str) -> str:
    """标题规范化 —— 去重键的原料。

    要做的事：全角转半角、去掉所有标点与空白、转小写。
    **不做**的事：同义替换、去掉修饰词（"突发"、"重磅"）。那类归并靠人写规则，
    机器做过头的后果是"两条其实不同的新闻被合成一条"，比重复更糟。
    """
    if not isinstance(title, str):
        return ""
    low = title.translate(_FULLWIDTH).lower()
    return _PUNCT.sub("", low)


def guid(title: str) -> str:
    """一条新闻的稳定身份。用规范化标题，**不含日期** ——
    含了日期的话，"同一条新闻在两个时间点被两个源重复报道"就永远判不出重复。

    ⚠️ 规范化之后是空的（标题全空、或只有标点）→ **返回空串**。
    调用方一律用它判"这条能不能进桶"（`if not key: continue`）。
    如果这里写成"照常算 sha1"，空标题会拿到同一个常量
    `sha1('')`，后果是**所有没标题的条目被合并成一条**——
    页面上表现为"少了几条"，而且合并后的那条标题是空的，谁也看不出发生了什么。
    """
    norm = norm_title(title)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def merge(items: list[dict]) -> tuple[list[dict], dict]:
    """跨源合并同一条新闻。

    判据：① `sid` 完全相同（同一个源自己重复，比如见闻一条快讯属于多个频道）
          ② **规范化标题相同**（两个源报了同一件事，标题措辞一致）

    ⚠️ 只认①不够（那是同源去重），只认②不够（同一条快讯标题一样但 id 不同时，
    ②其实也能兜住 —— 两个都留着是因为①更便宜、②更宽）。

    合并时保留**最早的时间戳**（一条新闻的"发生时间"是它最早出现的时刻），
    并把来源标签并起来 —— 页面上显示「见闻 + 新浪」比显示其中一家有信息量。
    """
    by_key: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        key = guid(it.get("title") or "")
        if not key:
            continue
        cur = by_key.get(key)
        if cur is None:
            copy = dict(it)
            copy["guid"] = key
            copy["sources"] = [it.get("src")] if it.get("src") else []
            copy["src_labels"] = [it.get("src_label")] if it.get("src_label") else []
            copy["sids"] = [it.get("sid")] if it.get("sid") else []
            by_key[key] = copy
            continue
        # 合并进已有的那条
        ts = it.get("ts") or 0
        if ts and (not cur.get("ts") or ts < cur["ts"]):
            # 更早的那条说了算：标题、正文、链接都换成它的
            keep_sources, keep_labels, keep_sids = (cur["sources"], cur["src_labels"],
                                                    cur["sids"])
            cur.update({k: v for k, v in it.items()
                        if k in ("title", "text", "url", "ts", "src", "src_label", "sid")})
            cur["sources"], cur["src_labels"], cur["sids"] = (keep_sources, keep_labels,
                                                             keep_sids)
        if it.get("src") and it["src"] not in cur["sources"]:
            cur["sources"].append(it["src"])
        if it.get("src_label") and it["src_label"] not in cur["src_labels"]:
            cur["src_labels"].append(it["src_label"])
        if it.get("sid") and it["sid"] not in cur["sids"]:
            cur["sids"].append(it["sid"])
        # tags / symbols 并起来（两个源各自带的信息都别丢）
        for field in ("tags", "symbols"):
            have = cur.get(field) or []
            for v in (it.get(field) or []):
                if v not in have:
                    have.append(v)
            cur[field] = have

    out = list(by_key.values())
    stats = {
        "in": len(items),
        "out": len(out),
        "merged": max(0, len(items) - len(out)),
    }
    return out, stats


def recent_guids(data_dir, today: str, days: int = 3) -> set[str]:
    """读最近 N 天快照里的 guid 集合，用来判"今天新出现的"。

    ⚠️ **这一步必须在 collect 里做**，不能在 analyze / render 里做。
    整站有一条硬规则：analyze 与 render **只读今天这一份快照** ——
    不然半年后重建旧页面，分组和时间会跟着"当时的最近三天"变，
    "历史页面不可变"就破了。所以跨日期的状态必须在采集时**定下来、写进快照**。

    读坏了、文件缺了、字段没有 —— 一律当"没见过"，**绝不抛异常**：
    增量标记顶多不准，整页消失才是真事故。
    """
    seen: set[str] = set()
    try:
        base = datetime.strptime(today, "%Y-%m-%d")
    except ValueError:
        return seen
    for back in range(1, max(1, days) + 1):
        day = (base - timedelta(days=back)).strftime("%Y-%m-%d")
        p = Path(data_dir) / "news" / f"{day}.json"
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for row in _iter_guids(data):
            seen.add(row)
    return seen


def _iter_guids(data) -> list[str]:
    """从历史快照里捞 guid。**两种形态都要认**：
    ① analyze 结果里的 `groups[].items[].guid`
    ② collect 原始快照里的 `items[].guid`（历史文件正好是 collect 写的那种时）
    """
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    for row in (data.get("items") or []):
        if isinstance(row, dict) and row.get("guid"):
            out.append(str(row["guid"]))
    for group in (data.get("groups") or []):
        if not isinstance(group, dict):
            continue
        for row in (group.get("items") or []):
            if isinstance(row, dict) and row.get("guid"):
                out.append(str(row["guid"]))
    return out
