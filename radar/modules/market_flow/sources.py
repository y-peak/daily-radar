"""行情与资金流数据源。

**最重要的一条经验：`push2.eastmoney.com` 会直接断连**（RemoteDisconnected），
不是限流、是拒绝服务，重试也救不回来。必须用 **`push2delay.eastmoney.com`** ——
同一个 API、同一套字段、同一份数据，只是行情有约 15 分钟延迟。
对"每天看一次的日报"来说，延迟完全无所谓，换来的是稳定。

各数据源分工：

    push2delay  clist/get      板块 / 个股的资金流排行
    push2delay  ulist.np/get   指数快照（国内 + 全球）
    datacenter-web             两融（融资融券）历史
    qt.gtimg.cn / hq.sinajs.cn 备用指数源（必须按 GBK 解，且新浪要 Referer）

⚠️ **北向资金已停发**：2024-08-19 起，沪深港通实时资金流不再逐日公布，
`RPT_MUTUAL_DEAL_HISTORY` 的金额字段返回 null。所以"外资流向"这一维度
用 **板块主力资金 + 两融** 来替代 —— 这是口径上的取舍，报告里会注明。

字段含义（东财 clist/ulist 通用）：
    f2 最新价   f3 涨跌幅%  f4 涨跌额   f5 成交量(手)  f6 成交额(元)  f7 振幅%
    f12 代码    f13 市场    f14 名称
    f15 最高    f16 最低    f17 今开      f18 昨收     f124 数据时间戳
资金流专用：
    f62 主力净流入(元)  f184 主力净占比%
    f66 超大单净流入    f69 超大单净占比
    f72 大单净流入      f75 大单净占比
    f78 中单净流入      f81 中单净占比
    f84 小单净流入      f87 小单净占比
两融（RPTA_RZRQ_LSHJ）：
    DIM_DATE 日期  RZYE 融资余额  RZJME 融资净买入  RZMRE 融资买入额
    RQYE 融券余额  RQJME 融券净卖出  LTSZ 流通市值  ZDF 涨跌幅
"""

from __future__ import annotations

import json
import time
import urllib.parse

# 被拒的域，保留在注释里以免后人踩坑：push2.eastmoney.com
EM_DELAY = "https://push2delay.eastmoney.com"
EM_DATA = "https://datacenter-web.eastmoney.com"

# 板块 / A股 的筛选串
FS_INDUSTRY = "m:90+t:2"          # 行业板块
FS_CONCEPT = "m:90+t:3"           # 概念板块
FS_A_SHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"   # 沪深京 A 股

INDEX_CN = ["1.000001", "0.399001", "0.399006", "1.000300", "1.000688", "1.000905"]
INDEX_GLOBAL = [
    "100.DJIA", "100.NDX", "100.SPX", "100.HSI",
    "100.N225", "100.FTSE", "100.GDAXI", "100.KS11",
]

FLOW_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87"


class SourceError(RuntimeError):
    pass


def _q(url: str) -> str:
    """给 URL 补一个时间戳，避开中间层缓存（东财对重复 URL 会返回旧值）。"""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_={int(time.time())}"


# ------------------------------------------------------------------ 排行列表
def clist(http, fs: str, fid: str, order: int, size: int,
          fields: str = FLOW_FIELDS) -> list[dict]:
    """拉一个排行列表。order=1 降序（净流入最多），0 升序（净流出最多）。"""
    params = {
        "pn": 1, "pz": size, "po": order, "np": 1,
        "fltt": 2, "invt": 2, "fid": fid, "fs": fs, "fields": fields,
    }
    url = _q(f"{EM_DELAY}/api/qt/clist/get?{urllib.parse.urlencode(params)}")
    data = http.get_json(url, referer="https://quote.eastmoney.com/")
    block = data.get("data")
    if not block:
        raise SourceError(f"clist 返回空：fs={fs} fid={fid} po={order}")
    return block.get("diff") or []


def board_flow(http, size: int = 10, kind: str = "industry") -> dict:
    """板块资金流。返回 {inflow:[...], outflow:[...]}。"""
    fs = FS_INDUSTRY if kind == "industry" else FS_CONCEPT
    return {
        "inflow": clist(http, fs, "f62", 1, size),
        "outflow": clist(http, fs, "f62", 0, size),
    }


def stock_flow(http, size: int = 15) -> dict:
    """个股主力资金流。返回 {inflow:[...], outflow:[...]}。"""
    return {
        "inflow": clist(http, FS_A_SHARE, "f62", 1, size),
        "outflow": clist(http, FS_A_SHARE, "f62", 0, size),
    }


# -------------------------------------------------------------------- 指数
def indices(http, secids: list[str]) -> list[dict]:
    params = {
        "fltt": 2,
        "secids": ",".join(secids),
        "fields": "f2,f3,f4,f5,f6,f7,f12,f13,f14,f15,f16,f17,f18,f124",
    }
    url = _q(f"{EM_DELAY}/api/qt/ulist.np/get?{urllib.parse.urlencode(params)}")
    data = http.get_json(url, referer="https://quote.eastmoney.com/")
    block = data.get("data")
    if not block:
        raise SourceError("ulist 返回空（指数快照）")
    return block.get("diff") or []


# ---------------------------------------------------------------- 市场广度
def breadth(http, max_pages: int = 60, page_size: int = 100) -> dict:
    """涨跌家数。

    **注意：东财 clist 的 `pz` 被硬性截断到 100**（实测 pz=100/500/1000/5000/6000
    一律只返回 100 条），所以全市场约 5500 只要翻 56 页。

    为什么不偷懒只取一页：按 f3 排序时第 1 页全是涨停股，数出来的"上涨 100 家"
    是完全错误的信号（实测踩过）。这里老实用 min_interval 限速翻完，
    每天跑一次、多花 45 秒，换一个准确的市场广度，值。

    也试过更省的路子，都不行：
      * `stock/get` 的 f104/f105/f106（涨/跌/平家数）恒为 0/0/100，字段已废弃；
      * 二分查找 f3 过零点只需 ~6 次请求，但停牌股（f3="-"）排序位置不定，
        会有几条误差 —— 广度是要拿来做判断的，宁可慢也要准。
    """
    up = down = flat = 0
    total = 0
    pages = 0
    for pn in range(1, max_pages + 1):
        params = {
            "pn": pn, "pz": page_size, "po": 1, "np": 1, "fltt": 2, "invt": 2,
            "fid": "f3", "fs": FS_A_SHARE, "fields": "f3",
        }
        url = _q(f"{EM_DELAY}/api/qt/clist/get?{urllib.parse.urlencode(params)}")
        data = http.get_json(url, referer="https://quote.eastmoney.com/")
        block = data.get("data") or {}
        total = block.get("total") or total
        diff = block.get("diff") or []
        pages = pn
        if not diff:
            break
        for it in diff:
            v = it.get("f3")
            if isinstance(v, (int, float)):
                if v > 0:
                    up += 1
                elif v < 0:
                    down += 1
                else:
                    flat += 1
            else:
                flat += 1          # 停牌 / 无数据
        if len(diff) < page_size:
            break
    return {"total": total, "up": up, "down": down, "flat": flat, "pages": pages}


# -------------------------------------------------------------------- 两融
def margin_history(http, size: int = 10) -> list[dict]:
    params = {
        "reportName": "RPTA_RZRQ_LSHJ",
        "columns": "ALL",
        "sortColumns": "dim_date",
        "sortTypes": -1,
        "pageSize": size,
        "pageNumber": 1,
        "source": "WEB",
        "client": "WEB",
    }
    url = _q(f"{EM_DATA}/api/data/v1/get?{urllib.parse.urlencode(params)}")
    data = http.get_json(url, referer="https://data.eastmoney.com/")
    result = data.get("result") or {}
    return result.get("data") or []


# ------------------------------------------------------- 备用：腾讯 / 新浪
def indices_tencent(http, codes: list[str]) -> dict:
    """备用指数源。腾讯返回 GBK，字段用 ~ 分隔。

    v_sh000001="1~上证指数~000001~当前~昨收~今开~成交量~…~时间~涨跌~涨跌幅~…"
    """
    url = "https://qt.gtimg.cn/q=" + ",".join(codes)
    text = http.get(url, referer="https://gu.qq.com/")
    out: dict[str, dict] = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if "=" not in chunk or '"' not in chunk:
            continue
        key, _, payload = chunk.partition("=")
        parts = payload.strip().strip('"').split("~")
        if len(parts) < 33:
            continue
        try:
            out[key.strip().removeprefix("v_")] = {
                "name": parts[1],
                "code": parts[2],
                "price": float(parts[3]) if parts[3] else None,
                "prev_close": float(parts[4]) if parts[4] else None,
                "change": float(parts[31]) if parts[31] else None,
                "pct": float(parts[32]) if parts[32] else None,
                "time": parts[30],
            }
        except (ValueError, IndexError):
            continue
    return out


def indices_sina(http, codes: list[str]) -> dict:
    """第二备用。新浪返回 GBK，必须带 Referer，否则 403。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    text = http.get(url, referer="https://finance.sina.com.cn/")
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if "=" not in line or '"' not in line:
            continue
        key, _, payload = line.partition("=")
        parts = payload.strip().strip('";').split(",")
        if len(parts) < 6:
            continue
        try:
            out[key.strip().removeprefix("var hq_str_")] = {
                "name": parts[0],
                "open": float(parts[1]) if parts[1] else None,
                "prev_close": float(parts[2]) if parts[2] else None,
                "price": float(parts[3]) if parts[3] else None,
                "date": parts[30] if len(parts) > 30 else "",
                "time": parts[31] if len(parts) > 31 else "",
            }
        except (ValueError, IndexError):
            continue
    return out
