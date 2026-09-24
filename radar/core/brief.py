"""每日简报 —— 系统通知到点时，手机来取的那一段话。

**为什么单独一层。** 通知里放不下一张表，它只能放一句话 + 几个关键数字。
而这句话要同时满足三件事：

  * 数字必须来自**已经冻结的快照**（实时拉的要明确标出来），不能现编；
  * 三个时段说的事情不一样 —— 早上看隔夜和昨日收盘，14:30 看盘中，
    收盘后是复盘 + 明日预期；
  * ⭐ **LLM 那一部分失败了，整条简报也不能消失**。通知是唯一触达用户的东西，
    宁可只给数字，也不要弹一条"简报生成失败"。

所以结构是：**数字部分是规则拼的（永远有），判断部分是 LLM 的（可以没有）**。

**成本纪律**（沿袭 core/llm.py 那套）：
  * 同一个 (日期, 时段) 只调一次，结果落 `data/brief/<date>-<slot>.json`；
  * 只缓存**成功**的结果 —— 失败了不写缓存，下次还有机会；
  * 尾盘那一次**不调 LLM**：数字每小时都在变，缓存必然对不上，实时生成又白花钱。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from . import store
from .config import Config
from . import prompts
from .llm import LLM
from .module import load_sibling

SLOTS = ("morning", "intraday", "close")

SLOT_TITLE = {"morning": "早间关注", "intraday": "尾盘", "close": "收盘复盘"}
SLOT_EMOJI_HEAD = {"morning": "🌅", "intraday": "⏰", "close": "🔔"}

DISCLAIMER = "以上是据公开数据做的概率性表述，不构成任何投资建议。"

_SYSTEM = prompts.system_for_numbers(
    "你是一个给个人投资者写每日复盘的中文助手。"
    "语气克制、简短，不喊口号，不用夸张词。"
)


# ------------------------------------------------------------------ 快照读取
def _latest(data_dir: Path, module: str):
    date = store.latest_date(data_dir, module)
    if not date:
        return None, None
    return date, store.read_json(data_dir / module / f"{date}.json")


def _siblings(cfg: Config):
    """拿到两个模块的 sources（模块目录不是包，只能按文件加载）。"""
    wl = load_sibling(str(cfg.modules_dir / "watchlist" / "sources.py"), "sources")
    return wl


# ------------------------------------------------------------------ 数字部分
def _idx(market: dict, code: str) -> dict:
    for i in (market.get("cn_indices") or []):
        if i.get("code") == code:
            return i
    return {}


def _as_of(market: dict) -> str:
    """⭐ 这份快照里的数字**实际是哪天的**。

    ⚠️ 这和"文件属于哪天"是两件事，别混。采集器 08:00 也会跑一次，
    那次写出的文件叫 `2026-09-13.json`（**周日的**采集），
    里面装的却是 **09-11（周五）16:11 的收盘数据** —— 因为盘还没开。
    只按文件名当期号，就会出现"这周三的早间简报说昨天跌了 1.18%"，
    而那个 1.18% 是上周五的。

    所以日期以**行情行自带的 `ts`** 为准（东财每个报价都带），
    退回 `fetched_at`（采集时间，比真实数据时间晚不了几小时）。
    """
    for i in (market.get("cn_indices") or []):
        ts = i.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            try:
                return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            except (OverflowError, OSError, ValueError):
                break
    return str(market.get("fetched_at") or "")[:10]


def _as_of_label(as_of: str, day: str) -> str:
    """数字不是今天的 → 给它一个能在句子里读出来的前缀。

    "" = 就是今天（不必标注）；否则 "昨日" / "09-11"。
    """
    if not as_of or as_of == day:
        return ""
    try:
        prev = (datetime.strptime(day, "%Y-%m-%d")
                - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        prev = ""
    return "昨日" if as_of == prev else as_of[5:]


def _tick(tag: str) -> str:
    """日期前缀 → 能直接拼在句子前面的样子。

    非空时**自带一个尾空格**，否则会拼出"09-11只有 10.9% 的股票在涨"这种
    数字和汉字挤在一起的句子。空串原样返回，让调用方自己决定说什么。
    """
    return f"{tag} " if tag else ""


def _board(i: dict) -> dict:
    """一条板块记录 → 一句话。老快照可能没有 flow_text，退回涨幅。"""
    name = i.get("name") or ""
    flow = i.get("flow_text")
    pct = i.get("pct_text") or ""
    return {
        "name": name, "flow_text": flow or "—", "pct_text": pct or "—",
        "tone": i.get("tone") or "flat",
        "text": f"{name}{flow or pct}".strip(),
    }


def _market_numbers(market: dict, live: dict | None, day: str) -> dict:
    """指数取哪一份：live 有就用 live（14:30 那次），否则用快照。

    `live` 只覆盖**指数**。涨跌家数、资金流那几块没有实时版（现拉要翻 56 页，
    一次通知等不起），它们仍然是快照的 —— 所以下面每一块都要挂上自己的
    `as_of`，免得"实时指数 + 隔夜家数"被拼成一句自相矛盾的话。
    """
    sh = (live or {}).get("000001") or _idx(market, "000001")
    as_of = _as_of(market)
    label = _as_of_label(as_of, day)
    out = {
        "index_name": sh.get("name") or "上证指数",
        "index_price": sh.get("price_text") or "—",
        "index_pct": sh.get("pct_text") or "—",
        "index_tone": sh.get("tone") or "flat",
        "live": bool(live),
        "as_of": as_of,
        "as_of_label": label,
    }
    breadth = market.get("breadth") or {}
    if breadth.get("total"):
        out.update({
            "up": breadth.get("up"), "down": breadth.get("down"),
            "flat": breadth.get("flat"), "total": breadth.get("total"),
            "up_ratio": breadth.get("up_ratio"),
        })
    # ⚠️⭐ industry.inflow 是**按主力净流入金额**排的，**不是**按涨幅排的。
    #    里面完全可能出现"资金在进、股价却在跌"的板块（本快照里 `通信` 就是
    #    +44.32 亿 / -0.04%）。所以这一块**绝不能**叫"领涨板块" ——
    #    那等于把一组数据当成另一组在用，用户会照着它去理解当天谁在涨。
    ind = market.get("industry") or {}
    out["flow_in"] = [_board(i) for i in (ind.get("inflow") or [])[:3]]
    out["flow_out"] = [_board(i) for i in (ind.get("outflow") or [])[:2]]
    gl = market.get("global_indices") or []
    out["global"] = [{"name": i.get("name"), "pct_text": i.get("pct_text"),
                      "tone": i.get("tone")} for i in gl[:6]]
    return out


def _watch_numbers(watch: dict, live_rows: list | None) -> dict:
    """自选股那一块。live_rows 是实时行情原始行（14:30 用）。"""
    if live_rows:
        rows = live_rows
        pcts = [r.get("f3") for r in rows if isinstance(r.get("f3"), (int, float))]
        up = sum(1 for p in pcts if p > 0)
        down = sum(1 for p in pcts if p < 0)
        avg = round(sum(pcts) / len(pcts), 2) if pcts else None
        return {
            "count": len(rows), "up": up, "down": down,
            "flat": len(rows) - up - down,
            "avg_pct": avg,
            "avg_pct_text": (f"{avg:+.2f}%" if avg is not None else "—"),
            "items": [{"name": r.get("f14"), "code": r.get("f12"),
                       "pct": r.get("f3"),
                       "pct_text": (f"{r['f3']:+.2f}%" if isinstance(r.get("f3"), (int, float)) else "—"),
                       "tone": ("up" if isinstance(r.get("f3"), (int, float)) and r["f3"] > 0
                                else "down" if isinstance(r.get("f3"), (int, float)) and r["f3"] < 0
                                else "flat")}
                      for r in rows[:12]],
            "live": True,
        }
    s = watch.get("summary") or {}
    return {
        "count": watch.get("count") or 0,
        "up": s.get("up", 0), "down": s.get("down", 0), "flat": s.get("flat", 0),
        "avg_pct": s.get("avg_pct"), "avg_pct_text": s.get("avg_pct_text"),
        "items": [{"name": i.get("name"), "code": i.get("code"),
                   "pct": i.get("pct"), "pct_text": i.get("pct_text"),
                   "tone": i.get("tone")} for i in (watch.get("items") or [])[:12]],
        "live": False,
    }


def _compose_line(slot: str, mk: dict, wl: dict) -> str:
    """一句话：能塞进通知标题下面那行的长度。

    ⭐ 铁律：**同一句话里不许出现两个时间基准**。
    尾盘那次指数是此刻的，家数/资金只能是上一份快照的 ——
    直接拼起来会读成"指数涨 0.94%，605 涨 4567 跌"。所以凡是快照来的数字，
    一律带上 `as_of_label`（"昨日" / "09-11"）。
    """
    parts: list[str] = []
    label = mk.get("as_of_label") or ""
    if slot == "morning":
        gl = [g for g in (mk.get("global") or [])
              if g.get("name") in ("纳斯达克", "标普500", "道琼斯", "恒生指数")]
        if gl:
            parts.append("隔夜 " + " ".join(f"{g['name']}{g['pct_text']}"
                                            for g in gl[:3]))
        parts.append(f"{_tick(label) or '昨日'}{mk.get('index_name')} "
                     f"{mk.get('index_price')} {mk.get('index_pct')}")
    else:
        prefix = "14:30 " if slot == "intraday" else ""
        parts.append(f"{prefix}{mk.get('index_name')} {mk.get('index_price')} "
                     f"{mk.get('index_pct')}")
        if mk.get("total"):
            parts.append(f"{_tick(label) or '今日 '}"
                         f"{mk.get('up')} 涨 / {mk.get('down')} 跌")

    if wl.get("count"):
        avg = wl.get("avg_pct")
        avg_text = (wl.get("avg_pct_text")
                    or (f"{avg:+.2f}%" if isinstance(avg, (int, float)) else "—"))
        parts.append(f"自选 {wl['count']} 只 {wl.get('up', 0)}涨"
                     f"{wl.get('down', 0)}跌（均 {avg_text}）")
    else:
        parts.append("自选股还没加")
    return " ｜ ".join(parts)


def _rule_focus(slot: str, mk: dict, wl: dict) -> list[str]:
    """没有 LLM 时的"看点" —— 纯规则，永远有。"""
    out: list[str] = []
    tag = _tick(mk.get("as_of_label") or "")   # 非今天就带上日期，别让读者误会
    if mk.get("up") is not None and mk.get("total"):
        ratio = mk.get("up_ratio")
        if ratio is not None:
            if ratio >= 60:
                out.append(f"{tag}{ratio}% 的股票在涨，赚钱效应偏强")
            elif ratio <= 25:
                out.append(f"{tag}只有 {ratio}% 的股票在涨，普跌")
            else:
                out.append(f"{tag}{ratio}% 的股票在涨，涨跌参半")
    if mk.get("flow_in"):
        out.append(f"{tag}主力净流入居前：" + "、".join(x["text"] for x in mk["flow_in"]))
    if mk.get("flow_out"):
        out.append(f"{tag}主力净流出居前：" + "、".join(x["text"] for x in mk["flow_out"]))
    if wl.get("count"):
        avg = wl.get("avg_pct")
        if isinstance(avg, (int, float)):
            if avg >= 3:
                out.append(f"自选股整体走强（平均 {avg:+.2f}%）")
            elif avg <= -3:
                out.append(f"自选股整体走弱（平均 {avg:+.2f}%）")
        movers = [i for i in (wl.get("items") or [])
                  if isinstance(i.get("pct"), (int, float)) and abs(i["pct"]) >= 3]
        if movers:
            out.append("波动较大：" + "、".join(
                f"{i['name']}{i['pct_text']}" for i in movers[:4]))
    return out[:4]


# ------------------------------------------------------------------ LLM 部分
def _brief_cache_path(data_dir: Path, date: str, slot: str) -> Path:
    return data_dir / "brief" / f"{date}-{slot}.json"


def _llm_view(cfg: Config, data_dir: Path, day: str, market_date: str, slot: str,
              payload: dict, log) -> dict:
    """要一段"人话"。拿不到就返回空 dict —— **绝不抛错**。

    ⚠️ 缓存键是 **(这份简报属于哪天, 时段)**，**不是**行情快照那天。
    反例：如果采集连挂三天，`market_date` 会一直停在周五；拿它当键的话，
    周一的早间简报会直接命中周五那条缓存 —— 而里面写的"隔夜外盘"
    跟周五毫无关系。简报的身份是"哪天早上该说的话"，不是"引用了哪份快照"。
    """
    cache = _brief_cache_path(data_dir, day, slot)
    if cache.is_file():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("summary"):
                data["cached"] = True
                return data
        except (OSError, ValueError):
            pass

    llm = LLM(cfg.section("llm"), cache_path=data_dir / ".llm_cache.json", log=log)
    if not llm.available:
        log(f"      大模型不可用（{llm.why_unavailable()}），简报只给数字")
        return {}

    want_expect = slot == "close"
    keys = '{"summary": "...", "focus": ["...", "..."]'
    keys += ', "expect": "..."}' if want_expect else "}"
    user = (
        f"今天：{day}　时段：{SLOT_TITLE.get(slot, slot)}"
        f"（下面的数字来自 {market_date or '无'} 那份快照）\n"
        f"数据（全部是已收盘或实时的公开数字，不要引入其它信息）：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=1)}\n\n"
        "请只输出一个 JSON 对象，不要任何解释文字：\n"
        f"{keys}\n"
        "要求：summary 一句话（40 字内，说清今天/现在什么状态）；"
        "focus 三条以内，每条 20 字内，每条都必须能在上面数字里找到依据；"
        + ("expect 一句话（40 字内）描述**下一个交易日**需要留意什么，"
           "用「关注」「留意」这类措辞，**不要**给出确定的涨跌预测，"
           "也不要写具体买卖点位；" if want_expect else "")
        + "没有任何依据的推断一律不要写。"
        + "注意「主力净流入」是按资金金额排的，不代表该板块在涨 —— "
          "提到它时必须同时看涨跌幅，不要写成「领涨」。"
    )
    got = llm.chat_json(_SYSTEM, user, max_tokens=700,
                        required=("summary", "focus"))
    if not got or not got.get("summary"):
        log("      大模型没给出可用结果，简报只给数字")
        return {}

    view = {
        "summary": str(got.get("summary") or "").strip()[:120],
        "focus": [str(x).strip()[:40] for x in (got.get("focus") or []) if str(x).strip()][:3],
        "expect": str(got.get("expect") or "").strip()[:120],
        "market_date": market_date,
        "day": day,
        "model": f"{llm.provider}/{llm.model}",
        "at": datetime.now().isoformat(timespec="seconds"),
        "cached": False,
    }
    if not view["summary"]:
        return {}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(view, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    except OSError:
        pass          # 缓存写不进去只是下次多花一次钱，不该影响这次输出
    return view


# ------------------------------------------------------------------ 对外入口
def build_brief(cfg: Config, slot: str = "close", http=None, log=None) -> dict:
    """组装一份简报。**任何一步失败都不会抛异常** —— 通知链路要它永远有输出。"""
    log = log or (lambda *a, **k: None)
    slot = slot if slot in SLOTS else "close"
    data_dir: Path = cfg.data_dir
    day = datetime.now().strftime("%Y-%m-%d")

    m_date, market = _latest(data_dir, "market_flow")
    w_date, watch = _latest(data_dir, "watchlist")
    market = market if isinstance(market, dict) else {}
    watch = watch if isinstance(watch, dict) else {}

    live_idx = None
    live_rows = None
    live_error = None
    if slot == "intraday" and http is not None:
        # 尾盘那一次必须看**此刻**的数：早上的快照对 14:30 毫无意义
        try:
            src = _siblings(cfg)
            ids = _requested(data_dir)
            if ids:
                live_rows = src.quotes(http, ids)
        except Exception as exc:  # noqa: BLE001
            live_error = f"{type(exc).__name__}: {exc}"
            log(f"      实时自选股拉取失败：{live_error}")
        try:
            mf = load_sibling(str(cfg.modules_dir / "market_flow" / "sources.py"),
                              "sources")
            idx_rows = mf.indices(http, ["1.000001", "0.399001", "0.399006"])
            live_idx = {}
            for r in idx_rows:
                live_idx[str(r.get("f12"))] = {
                    "name": r.get("f14"),
                    "price_text": (f"{r['f2']:,.2f}"
                                   if isinstance(r.get("f2"), (int, float)) else "—"),
                    "pct_text": (f"{r['f3']:+.2f}%"
                                 if isinstance(r.get("f3"), (int, float)) else "—"),
                    "tone": ("up" if isinstance(r.get("f3"), (int, float)) and r["f3"] > 0
                             else "down" if isinstance(r.get("f3"), (int, float)) and r["f3"] < 0
                             else "flat"),
                }
        except Exception as exc:  # noqa: BLE001
            live_error = live_error or f"{type(exc).__name__}: {exc}"
            log(f"      实时指数拉取失败：{exc}")

    mk = _market_numbers(market, live_idx, day)
    wl = _watch_numbers(watch, live_rows)
    line = _compose_line(slot, mk, wl)

    # 快照是旧的时候必须**说出来**。否则"收盘复盘"下面挂着三天前的指数，
    # 用户会以为是今天的 —— 这种错看着一点都不像错，最危险。
    #
    # 两种"旧"要分开：
    #   stale  —— 文件就不是今天的（采集压根没跑）
    #   behind —— 文件是今天的，但里面的数字是更早的（08:00 那次采的是前一日收盘）
    # ⚠️ behind **周末不报**：周六日没有"今天的收盘"，报出来就是误报。
    #    节假日仍会误报一次，所以文案只说"还没有"，不咬定"采集没跑完"——
    #    我们分不清"没跑完"和"今天本来就不开市"，那就别替用户下这个判断。
    as_of = mk.get("as_of") or ""
    stale = bool(m_date) and m_date != day
    ahead_of_close = m_date == day and slot == "close"
    behind = (bool(as_of) and as_of != day and ahead_of_close
              and datetime.now().weekday() < 5)

    payload = {
        "指数": {k: mk.get(k) for k in
                 ("index_name", "index_price", "index_pct", "up", "down", "total", "up_ratio")},
        "主力净流入": [x["text"] for x in (mk.get("flow_in") or [])],
        "主力净流出": [x["text"] for x in (mk.get("flow_out") or [])],
        "隔夜外盘": mk.get("global"),
        "自选股": {"数量": wl.get("count"), "涨": wl.get("up"), "跌": wl.get("down"),
                   "平均涨跌": wl.get("avg_pct"),
                   "明细": [{"名称": i["name"], "涨跌幅": i["pct_text"]}
                            for i in (wl.get("items") or [])[:8]]},
        "规则看点": _rule_focus(slot, mk, wl),
    }
    # 尾盘那次不调大模型：数字每小时都在变，缓存必然对不上，实时生成又白花钱
    view = {} if slot == "intraday" else _llm_view(
        cfg, data_dir, day, m_date or "", slot, payload, log)

    focus = view.get("focus") or _rule_focus(slot, mk, wl)
    big_lines = [line]
    if stale:
        big_lines.append(f"⚠️ 今天还没采到数据，下面用的是 {m_date} 那份快照")
    elif behind:
        big_lines.append(f"⚠️ 指数还是 {as_of} 收盘的 —— 今天还没有新的收盘数据")
    if view.get("summary"):
        big_lines.append(view["summary"])
    for f in focus[:3]:
        big_lines.append("· " + f)
    if view.get("expect"):
        big_lines.append("明日：" + view["expect"])
    big_lines.append(DISCLAIMER)

    title = f"{(m_date or '')[-5:]} {SLOT_TITLE.get(slot, slot)}"
    return {
        "date": m_date,
        "day": day,
        "as_of": as_of,
        "stale": stale,
        "behind": behind,
        "slot": slot,
        "title": title,
        "head": SLOT_EMOJI_HEAD.get(slot, "🔔"),
        "line": line,
        "big": "\n".join(big_lines)[:900],
        "focus": focus[:3],
        "expect": view.get("expect") or "",
        "market": mk,
        "watch": wl,
        "sources": {"market": m_date, "watch": w_date,
                    "live": bool(live_idx or live_rows),
                    "live_error": live_error},
        "llm": {"used": bool(view), "model": view.get("model"),
                "cached": bool(view.get("cached"))},
        "disclaimer": DISCLAIMER,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _requested(data_dir: Path) -> list[str]:
    """实时拉自选股时需要 secid 列表 —— 从名单文件读，别再解析一次快照。"""
    try:
        data = json.loads((data_dir / "watchlist.json").read_text(encoding="utf-8"))
        return [i["secid"] for i in (data.get("items") or []) if i.get("secid")]
    except (OSError, ValueError, KeyError, TypeError):
        return []
