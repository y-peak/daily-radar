"""自选股的行情数据源，以及"用户输入 → secid"的解析。

两个接口，都在东财：

    push2delay  ulist.np/get      批量快照（一次请求拿全部自选）
    searchapi   suggest/get       名字 / 拼音 / 代码 → secid（输入"茅台"也能认）

⚠️ `push2.eastmoney.com` 会**直接断连**（不是限流，是拒绝服务，重试没用），
必须用 `push2delay`——行情延迟约 15 分钟。对"每天看一次的日报"完全够，
换来的是稳定。这条经验在 `market_flow/sources.py` 里也记着，别再踩第二次。

字段含义（ulist / clist 通用，**已实测确认**，别照抄别处的编号）：
    f2  最新价      f3  涨跌幅%    f4  涨跌额     f5  成交量(手)
    f6  成交额(元)  f7  振幅%      f8  换手率%    f9  市盈率(动)
    f10 量比        f12 代码       f13 市场       f14 名称
    f15 最高        f16 最低       f17 今开       f18 昨收
    f20 总市值      f21 流通市值   f23 市净率     f124 数据时间戳

`f13` 是市场编号：0 深 1 沪 116 港 105/106/107 美（北交所在 0 里，代码 4/8 开头）。
`"<f13>.<f12>"` 就是 secid —— 这也是 searchapi 直接给的 `QuoteID`，
所以"用户存下来的 secid"和"行情回来的 secid"能对得上，不用另建映射。
"""

from __future__ import annotations

import re
import time
import urllib.parse

EM_DELAY = "https://push2delay.eastmoney.com"
EM_SEARCH = "https://searchapi.eastmoney.com/api/suggest/get"

# 东财搜索接口要一个公开的 token（它自己的网页也用这个常量）。
# 不是凭证、也不是我们的密钥 —— 但**它随时可能变**，所以调用要能失败降级：
# 搜索挂了就退回"只认代码"，绝不让整条链因为补全不能用而挂掉。
SEARCH_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"

QUOTE_FIELDS = ("f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
                "f20,f21,f23,f124")

# 一次请求的 secid 个数。名称+代码加起来 URL 会变长，50 个还很安全；
# 超过就分批，别指望东财永远收长 URL。
BATCH = 50


class SourceError(RuntimeError):
    pass


def _q(url: str) -> str:
    """补时间戳避开中间层缓存（东财对重复 URL 会返回旧值）。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_={int(time.time())}"


# ------------------------------------------------------------------ 代码解析
# 纯字符串解析（不联网）：用户敲代码/带前缀时不必去问搜索接口。
# 只处理**确定性**的形状，认不出来的一律返回 None，交给搜索兜底 ——
# 宁可多问一次接口，也不要瞎猜一个市场编号。
_SH_PREFIX = ("sh", "SH")
_SZ_PREFIX = ("sz", "SZ")
_BJ_PREFIX = ("bj", "BJ", "BJSE")

_RE_PREFIXED = re.compile(r"^(sh|sz|bj)[.\-_]?(\d{6})$", re.I)
_RE_SIX = re.compile(r"^\d{6}$")
_RE_HK = re.compile(r"^(?:hk[.\-_]?)?(\d{5})(?:\.hk)?$", re.I)
_RE_US = re.compile(r"^(?:us[.\-_]?)?([A-Za-z][A-Za-z.\-]{0,9})$")
# secid 本体（`1.600519` / `116.00700`）—— 允许用户直接写，
# 页面回填给表单时也用它（当"代码"没法唯一还原市场时会退回这个形状）
_RE_SECID = re.compile(r"^\d{1,3}\.[A-Za-z0-9._\-]{1,16}$")


def guess_secid(token: str) -> str | None:
    """把"像代码"的输入转成 secid；认不出来返回 None。"""
    t = (token or "").strip()
    if not t:
        return None

    if _RE_SECID.match(t):
        return t

    m = _RE_PREFIXED.match(t)
    if m:
        prefix, digits = m.group(1).lower(), m.group(2)
        return f"{1 if prefix == 'sh' else 0}.{digits}"

    if _RE_SIX.match(t):
        head = t[0]
        # 6/5 = 沪市主板/基金，9 = 沪市B股；0/1/2/3 = 深市，4/8 = 北交所
        return f"{1 if head in '569' else 0}.{t}"

    m = _RE_HK.match(t)
    if m:
        return f"116.{m.group(1)}"

    m = _RE_US.match(t)
    if m:
        # 美股 105/106/107 是 NASDAQ/NYSE/AMEX，光看 ticker 分不出来，
        # 默认 105（互通性最好，拿不到的行情会被下面的"缺了谁"如实报出来）。
        tick = m.group(1).upper()
        return f"105.{tick}"
    return None


def looks_like_code(token: str) -> bool:
    """是不是"代码形状"。是的话优先按代码解释，不去搜索。"""
    t = (token or "").strip()
    if _RE_SECID.match(t) or _RE_PREFIXED.match(t) or _RE_SIX.match(t):
        return True
    # 5 位数字只在带 hk 或 .hk 时才算代码 —— 否则"00700"这种也可能是别的意思
    return bool(re.match(r"^(hk[.\-_]?\d{5}|\d{5}\.hk)$", t, re.I))


# ------------------------------------------------------------------ 搜索补全
def search(http, keyword: str, count: int = 8) -> list[dict]:
    """名字 / 拼音 / 代码 → 候选列表。

    返回 `[{"secid","code","name","type","pinyin"}]`。
    接口挂了**抛 SourceError**，由调用方决定怎么降级（通常就是"只认代码"）。
    """
    kw = (keyword or "").strip()
    if not kw:
        return []
    params = {"input": kw, "type": "14", "token": SEARCH_TOKEN, "count": count}
    url = f"{EM_SEARCH}?{urllib.parse.urlencode(params)}"
    data = http.get_json(url, referer="https://www.eastmoney.com/")
    table = data.get("QuotationCodeTable")
    if not isinstance(table, dict):
        raise SourceError("搜索接口返回结构不认识（可能又改了）")
    out: list[dict] = []
    for row in table.get("Data") or []:
        secid = (row.get("QuoteID") or "").strip()
        code = (row.get("Code") or "").strip()
        name = (row.get("Name") or "").strip()
        if not secid or not code or not name:
            continue
        out.append({
            "secid": secid,
            "code": code,
            "name": name,
            "pinyin": (row.get("PinYin") or "").strip(),
            "type": (row.get("SecurityTypeName") or row.get("Classify") or "").strip(),
        })
    return out


# ------------------------------------------------------------------ 批量行情
def quotes(http, secids: list[str]) -> list[dict]:
    """批量快照。**一条都没回来**才算失败（个别代码失效是常事，不该拖垮整页）。"""
    ids = [s for s in (secids or []) if s]
    if not ids:
        return []
    out: list[dict] = []
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        params = {"fltt": 2, "secids": ",".join(batch), "fields": QUOTE_FIELDS}
        url = _q(f"{EM_DELAY}/api/qt/ulist.np/get?{urllib.parse.urlencode(params)}")
        data = http.get_json(url, referer="https://quote.eastmoney.com/")
        block = data.get("data")
        if not isinstance(block, dict):
            # 单批失败不立刻抛：别的批次可能好着
            continue
        out += block.get("diff") or []
    if not out:
        raise SourceError("ulist 一条行情都没返回（代码全失效？还是接口变了？）")
    return out


def secid_of(row: dict) -> str:
    """行情行 → secid，用来和用户名单对齐（顺序不保证一致）。"""
    market = row.get("f13")
    code = row.get("f12")
    if market is None or code is None:
        return ""
    return f"{market}.{code}"
