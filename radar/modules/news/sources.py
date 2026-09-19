"""新闻源：华尔街见闻快讯 + 新浪财经 7×24。

两个源都**实测过、都是结构化 JSON**，不需要爬 HTML：

  * 见闻 `api-one.wallstcn.com/apiv1/content/lives` —— 按频道取，一次一个频道。
    `a-stock-channel` / `global-channel` / `us-stock-channel` / `hk-stock-channel`
    / `forex-channel` 都实测可用（`goods-channel` 返回 0 条，不用）。
    一条快讯会同时属于多个频道（`channels` 字段），所以**跨频道必然重复**,
    去重靠 `id`（同一个 id 就是同一条）。
  * 新浪 `zhibo.sina.com.cn/api/zhibo/feed` —— 一条流，但自带两样很值钱的东西：
      - `tag[].name`：公司 / 央行 / 宏观 / 市场 / 国际 / 观点 …… 直接可以当分类用
      - `ext.stocks[]`：**关联标的**，`{"market":"cn","symbol":"sz300476","key":"胜宏科技"}`
        → 这是做「自选股相关」最硬的信号，比拿名字去正文里瞎撞可靠得多

**实测排除、记在这里省得下次再试**（都是本机/服务器上真跑过的结论）：

  * 财联社 `cls.cn` telegraph/v1 —— 要签名（`errno 10012`）；`telegraphList` 返回 HTML
  * newsnow 公共实例 —— 403（要自建才有）
  * 雪球 `stock.xueqiu.com` —— 要 cookie
  * 东财 `newsapi.eastmoney.com` 快讯 —— 返回的是 HTML，不是 JSON

**一个源挂了不能让整页没有新闻**：`fetch_all` 逐个源独立 try/except，
失败的记在 `errors` 里由页面如实显示，成功的照常出。
"""

from __future__ import annotations

import json
import re
from datetime import datetime

WSCN_LIVES = "https://api-one.wallstcn.com/apiv1/content/lives"
SINA_FEED = "https://zhibo.sina.com.cn/api/zhibo/feed"

# 见闻频道 → 展示用名字。顺序就是页面上"来源"的展示顺序。
WSCN_CHANNELS = [
    ("a-stock-channel", "A股"),
    ("global-channel", "全球"),
    ("us-stock-channel", "美股"),
    ("hk-stock-channel", "港股"),
    ("forex-channel", "外汇"),
]

# 频道 key → 中文标签。**用来给条目打 `tags`。**
#
# ⚠️ 只收录"人能看懂"的频道，其余（`xgb-channel` 这类）一律**丢掉**而不是原样保留 ——
#    页面上出现一串 `xgb-channel` 只会让人以为界面坏了，
#    而且它也没告诉读者任何事（标签的价值在于"我能据此筛"，不是"我知道它属于哪个内部频道"）。
CHANNEL_LABELS = dict(WSCN_CHANNELS)
CHANNEL_LABELS.update({
    "goldc-channel": "黄金",
    "oil-channel": "原油",
    "bond-channel": "债券",
    "a-share-channel": "A股",
})

TEXT_MAX = 400          # 单条正文截断长度：快照要留 400 天，别把全文都存进来
TITLE_MAX = 80

# 新浪的 symbol 形如 `sh600522` / `sz300925` / `hk02476` / `si931071`。
# ⚠️ `si` 开头的是**指数/主题**（如 931071 人工智能），不是个股 ——
#    拿它去跟自选股对代码永远对不上，留着还会在页面上冒充"关联标的"。
_RE_CN = re.compile(r"^(sh|sz)(\d{6})$")
_RE_HK = re.compile(r"^(\d{4,5})$")


def _is_index_code(prefix: str, code: str) -> bool:
    """`sh`/`sz` + 6 位码，判断它其实是**指数**而不是个股。

    为什么单靠 `si` 前缀不够：新浪对指数**两种写法都用过**。
    实测抓到的 `sz399975`（证券公司）、`sh000300`（沪深300）走的是 `sh/sz` 前缀，
    照样匹配 `_RE_CN` 一路进到页面的「关联标的」里 ——
    读者看到一条新闻挂着「证券公司」，会以为是某只票，其实是个板块指数。

    号段是不冲突的，所以可以按市场精确判断：
      * 沪市个股一律 `6` 开头（600/601/603/605/688），所以 `sh000xxx` 必然是上证指数系列
      * 深市个股是 000/001/002/003/300/301，所以 `sz399xxx` 必然是深证指数系列
    """
    if prefix == "sh":
        return code.startswith("000")
    if prefix == "sz":
        return code.startswith("399")
    return False

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")


def _clean(text, limit: int = TEXT_MAX) -> str:
    """去 HTML 标签、压空白。新浪/见闻偶尔混一点标签和小程序卡片文案进来。"""
    if not isinstance(text, str):
        return ""
    text = _TAG_RE.sub("", text)
    text = text.replace("\r", "\n")
    text = _WS_RE.sub(" ", text)
    # 三个以上连续换行压成两个（正文里的段落感留着，卡片尾巴的空白不要）
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _title_of(text: str, fallback: str = "") -> str:
    """从正文里抠一个标题。

    新浪的 `rich_text` 长这样：`【特朗普媒体禁令开始执行 CNN 等记者被拒绝进入白宫】MS NOW 和 CNN 表示……`
    —— 标题就在【】里。没有【】的就退到第一句。
    """
    body = _clean(text, TEXT_MAX).strip()
    if not body:
        return _clean(fallback, TITLE_MAX)
    if body.startswith("【"):
        end = body.find("】")
        if 1 < end <= TITLE_MAX:
            return body[1:end].strip()
    # 没有【】：取到第一个句号/问号/感叹号/换行为止
    for stop in ("。", "！", "？", "\n"):
        idx = body.find(stop)
        if 0 < idx <= TITLE_MAX:
            return body[:idx].strip()
    return body[:TITLE_MAX]


def _body_after_title(text: str) -> str:
    """去掉【标题】前缀后的正文。"""
    body = _clean(text, TEXT_MAX)
    if body.startswith("【"):
        end = body.find("】")
        if 1 < end:
            return body[end + 1:].strip()
    return body


# ------------------------------------------------------------------ 见闻
def fetch_wscn(http, channel: str, limit: int = 30) -> list[dict]:
    url = f"{WSCN_LIVES}?channel={channel}&client=pc&limit={int(limit)}"
    data = http.get_json(url)
    if data.get("code") != 20000:
        raise RuntimeError(f"见闻返回 code={data.get('code')} {data.get('message')!r}")
    items = ((data.get("data") or {}).get("items")) or []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("id")
        if sid is None:
            continue
        title = _clean(it.get("title"), TITLE_MAX)
        text = _clean(it.get("content_text") or it.get("content"), TEXT_MAX)
        if not title:
            title = _title_of(text, it.get("highlight_title") or "")
        if not title and not text:
            continue
        chans = [c for c in (it.get("channels") or []) if isinstance(c, str)]
        # 只留看得懂的频道名，且去重保序（同一条常同时属于 4-6 个频道）
        labels: list[str] = []
        for c in chans:
            label = CHANNEL_LABELS.get(c)
            if label and label not in labels:
                labels.append(label)
        out.append({
            "src": "wscn",
            "sid": f"wscn:{sid}",
            "title": title or text[:TITLE_MAX],
            "text": text,
            "url": str(it.get("uri") or ""),
            "ts": _int(it.get("display_time")),
            "tags": labels,
            "symbols": [],
            "src_label": "华尔街见闻",
        })
    return out


# ------------------------------------------------------------------ 新浪
def fetch_sina(http, size: int = 100) -> list[dict]:
    url = (f"{SINA_FEED}?page=1&page_size={int(size)}&zhibo_id=152"
           f"&tag_id=0&dire=f&dpc=1")
    data = http.get_json(url)
    res = data.get("result") or {}
    status = res.get("status") or {}
    if status.get("code") not in (0, "0"):
        raise RuntimeError(f"新浪返回 code={status.get('code')} {status.get('msg')!r}")
    feed = (res.get("data") or {}).get("feed") or {}
    items = feed.get("list") or []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = it.get("id")
        if sid is None:
            continue
        raw_text = it.get("rich_text") or it.get("text") or ""
        title = _title_of(raw_text, it.get("title") or "")
        if not title:
            continue
        tags = [t.get("name") for t in (it.get("tag") or [])
                if isinstance(t, dict) and t.get("name")]
        out.append({
            "src": "sina",
            "sid": f"sina:{sid}",
            "title": title,
            "text": _body_after_title(raw_text),
            "url": _sina_url(it),
            "ts": _int(it.get("create_time")),
            "tags": tags,
            "symbols": _sina_symbols(it),
            "src_label": "新浪财经",
        })
    return out


def _sina_url(it: dict) -> str:
    """优先用 `ext.docurl`（finance.sina.com.cn 的正式稿），
    退了才用 `docurl`（m 站）。两个都没有就给空串 —— 页面据此不渲染外链。"""
    ext = {}
    try:
        ext = json.loads(it.get("ext") or "{}")
    except (ValueError, TypeError):
        ext = {}
    if isinstance(ext, dict) and ext.get("docurl"):
        return str(ext["docurl"])
    return str(it.get("docurl") or "")


def _sina_symbols(it: dict) -> list[dict]:
    """把 `ext.stocks` 规整成 [{market, code, name}]，**只留真正的个股**。

    指数/主题一律丢掉（两种写法：`si931071` 与 `sz399975`/`sh000300`，
    见 `_is_index_code`）：拿它跟自选股对代码永远对不上，
    留在页面上还会让人以为"这条新闻跟我的票有关"。
    """
    try:
        ext = json.loads(it.get("ext") or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(ext, dict):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for s in (ext.get("stocks") or []):
        if not isinstance(s, dict):
            continue
        sym = str(s.get("symbol") or "").strip()
        key = str(s.get("key") or "").strip()
        market = str(s.get("market") or "").strip()
        code = ""
        mk = ""
        m = _RE_CN.match(sym)
        if m and market == "cn" and not _is_index_code(m.group(1), m.group(2)):
            mk, code = "cn", m.group(2)
        elif market == "hk":
            # ⚠️ 港股 symbol 实测是**裸数字**（`"02476"`），但这里两种写法都认。
            #    只认裸数字的话，新浪哪天改成 `hk02476`，港股关联标的会**静默全灭** ——
            #    页面上看不出错，只是"腾讯控股"再也不出现在任何新闻里。
            hk = _RE_HK.match(sym) or re.match(r"^hk(\d{4,5})$", sym, re.I)
            if hk:
                mk, code = "hk", hk.group(1)
        if not (mk and code):
            continue
        token = f"{mk}:{code}"
        if token in seen:
            continue
        seen.add(token)
        out.append({"market": mk, "code": code, "name": key})
    return out


def _int(value) -> int:
    """时间统一成 unix 秒。两种形态都见过：`display_time` 是 int，
    新浪 `create_time` 是 `"2026-09-19 23:06:08"`（**本地时间**，不是 UTC）。"""
    if isinstance(value, (int, float)):
        v = int(value)
        # 毫秒戳兜底：13 位的当毫秒
        return v // 1000 if v > 10 ** 11 else v
    if isinstance(value, str) and value.strip():
        text = value.strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return int(datetime.strptime(text, fmt).timestamp())
            except ValueError:
                continue
    return 0


# ------------------------------------------------------------------ 汇总
def fetch_all(http, cfg: dict | None, log=None) -> dict:
    """拉全部源。返回 `{"items": [...], "errors": {...}, "stats": {...}}`。

    **一个源失败不影响另一个** —— 新闻页最要紧的是"今天有没有东西看"，
    半个来源挂了也该出页面，但要在 `errors` 里如实说，
    否则用户会以为"今天真的没有相关新闻"。
    """
    cfg = cfg or {}
    log = log or (lambda *a, **k: None)
    limit = int(cfg.get("per_channel", 30))
    sina_size = int(cfg.get("sina_size", 100))
    items: list[dict] = []
    errors: dict[str, str] = {}
    stats: dict[str, int] = {}

    total_raw = 0
    for channel, label in WSCN_CHANNELS:
        try:
            rows = fetch_wscn(http, channel, limit)
        except Exception as exc:  # noqa: BLE001
            # 单个频道失败不算"源挂了"：见闻有 5 个频道，挂一个还有四个。
            # 但**每一个**都失败时下面会把它降级成整源错误。
            errors.setdefault("wscn", f"{channel}：{type(exc).__name__}: {exc}")
            log(f"      · 见闻 {label} 频道失败：{type(exc).__name__}")
            rows = []
        total_raw += len(rows)
        stats[f"wscn:{label}"] = len(rows)
        items += rows
        log(f"      · 见闻 {label} {len(rows)} 条")

    try:
        rows = fetch_sina(http, sina_size)
        errors.pop("sina", None)
        stats["sina:7x24"] = len(rows)
        items += rows
        total_raw += len(rows)      # ⚠️ 别漏了这句：raw_count 是"去重前一共多少条"，
                                    #    漏了它页面上会显示"原始 150 条 → 合并 175 条"这种
                                    #    自相矛盾的数字
        log(f"      · 新浪 7×24 {len(rows)} 条")
    except Exception as exc:  # noqa: BLE001
        errors["sina"] = f"{type(exc).__name__}: {exc}"
        log(f"      · 新浪 7×24 失败：{type(exc).__name__}")

    # 见闻一个频道都没成功 → 那才是"整源失败"，别让 errors 里只留一条频道级的说明
    if all(v == 0 for k, v in stats.items() if k.startswith("wscn:")):
        errors["wscn"] = errors.get("wscn") or "所有频道都没返回数据"

    if not items:
        raise RuntimeError(
            "两个源一条新闻都没拿到：" + ("; ".join(f"{k}={v}" for k, v in errors.items())
                                          or "原因不明"))
    return {"items": items, "errors": errors, "stats": stats, "raw_count": total_raw}
