"""自选股模块 —— 自己关注的标的，每天一行：涨跌、量额、估值。

**为什么不走"只存手机"那套。** 规划是纯个人的、服务端不需要知道；自选股不一样：
每日大盘简报、以后按标的筛新闻、榜单联动，全都在服务器侧生成。名单存手机的话，
每个消费方都得再问一遍手机。所以名单落在 `data/watchlist.json`（见 userlist.py）。

**容错口径（和 market_flow 一致）：**
  * 名单是空的 → **正常状态，不是错误**。页面照样出，带一个"加两只"的表单。
    空名单如果没有页面，用户就没有入口去添加 —— 这是个死循环。
  * 行情一条都没拉到 → **抛错，今天不写快照**。这样页面继续显示昨天那份好的，
    而不是被一份空数据顶掉（"拿不到数据不许覆盖好数据"）。

**结论不靠猜**：下面 `_signals()` 里每条判断都能对应到一个可复现的数字条件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from radar.core.module import Context, Module, load_sibling

sources = load_sibling(__file__, "sources")
userlist = load_sibling(__file__, "userlist")

# 判定阈值，集中放这里便于调参
BIG_MOVE = 3.0          # 单只涨跌超过它算"大波动"
BIG_AMOUNT = 20e8       # 成交额超过它算"放量"（元）

MARKET_LABEL = {0: "深", 1: "沪", 116: "港", 105: "美", 106: "美", 107: "美"}


# ------------------------------------------------------------------ 格式化
def _num(value):
    return value if isinstance(value, (int, float)) else None


def _price_text(value) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"{v:,.2f}" if v < 10000 else f"{v:,.0f}"


def _pct(value) -> float | None:
    v = _num(value)
    return round(v, 2) if v is not None else None


def _pct_text(value) -> str:
    v = _pct(value)
    if v is None:
        return "—"
    return f"{v:+.2f}%"


def _tone(value) -> str:
    """红涨绿跌（中国习惯）。零涨跌走 flat，别染成红色。"""
    v = _num(value)
    if v is None or v == 0:
        return "flat"
    return "up" if v > 0 else "down"


def _amount_text(value) -> str:
    v = _num(value)
    if v is None:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f} 万亿"
    if v >= 1e8:
        return f"{v / 1e8:.1f} 亿"
    if v >= 1e4:
        return f"{v / 1e4:.0f} 万"
    return f"{v:.0f}"


def _cap_text(value) -> str:
    v = _num(value)
    if v is None:
        return "—"
    if v >= 1e12:
        return f"{v / 1e12:.2f} 万亿"
    if v >= 1e8:
        return f"{v / 1e8:.0f} 亿"
    return f"{v:.0f}"


def _volume_text(value) -> str:
    """成交量单位是"手"，1 手 = 100 股。"""
    v = _num(value)
    if v is None:
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f} 亿手"
    if v >= 1e4:
        return f"{v / 1e4:.1f} 万手"
    return f"{v:.0f} 手"


def _ratio_text(value, digits: int = 2) -> str:
    v = _num(value)
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def market_label(market, code: str = "") -> str:
    if market == 0 and str(code)[:1] in ("4", "8"):
        return "北"          # 北交所在深市那个编号里，靠代码首位区分
    return MARKET_LABEL.get(market, "—")


# ------------------------------------------------------------------ 规整一条
def _item(row: dict, note: str = "") -> dict:
    pct = _pct(row.get("f3"))
    amount = _num(row.get("f6"))
    return {
        "code": str(row.get("f12") or ""),
        "name": str(row.get("f14") or ""),
        "market": market_label(row.get("f13"), row.get("f12")),
        "price": _num(row.get("f2")),
        "price_text": _price_text(row.get("f2")),
        "pct": pct,
        "pct_text": _pct_text(pct),
        "tone": _tone(pct),
        "change": _num(row.get("f4")),
        "open": _num(row.get("f17")),
        "high": _num(row.get("f15")),
        "low": _num(row.get("f16")),
        "prev_close": _num(row.get("f18")),
        "amplitude": _num(row.get("f7")),
        "amplitude_text": _pct_text(row.get("f7")).lstrip("+"),
        "amount": amount,
        "amount_text": _amount_text(amount),
        "big_amount": bool(amount and amount >= BIG_AMOUNT),
        "volume_text": _volume_text(row.get("f5")),
        "turnover_text": _ratio_text(row.get("f8")),
        "pe_text": _ratio_text(row.get("f9")),
        "pb_text": _ratio_text(row.get("f23")),
        "mktcap_text": _cap_text(row.get("f20")),
        "float_cap_text": _cap_text(row.get("f21")),
        "note": note,
        "ts": row.get("f124"),
    }


class Watchlist(Module):
    name = "watchlist"
    title = "我的自选股"
    subtitle = "自己盯的标的：涨跌、量额、估值，一屏看完"
    order = 25
    schedule = "每日 08:15"

    # ------------------------------------------------------------------ collect
    def collect(self, ctx: Context) -> Any:
        wl = userlist.load(ctx.data_dir)
        requested = [it["secid"] for it in wl["items"]]

        out: dict[str, Any] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "list_updated_at": wl["updated_at"],
            "requested": requested,
            # 名单原样带一份：页面上的"管理"表单要靠它回填，
            # 而且快照里留下名单 = 半年后打开今天这页，能看清"当时盯的是哪几只"
            "list": wl["items"],
            "notes": {it["secid"]: it["note"] for it in wl["items"] if it["note"]},
            "quotes": [],
            "errors": {},
        }

        if not requested:
            # 空名单是合法状态：页面要能出来（上面有"添加"入口），所以不抛错
            out["empty"] = True
            ctx.log_info("      · 名单还是空的（去 /watchlist/ 加两只）")
            return out

        try:
            out["quotes"] = sources.quotes(ctx.http, requested)
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 一条都没拉到 → 让今天没有快照，页面继续显示昨天那份好的。
            #    宁可"今天没更新"，也不要拿一份空数据把好数据顶掉。
            raise RuntimeError(
                f"自选股行情拉取失败（{len(requested)} 只）：{exc}"
            ) from exc
        ctx.log_info(f"      ✓ {len(out['quotes'])}/{len(requested)} 只行情")
        return out

    # ------------------------------------------------------------------ analyze
    def analyze(self, raw: Any, ctx: Context) -> Any:
        notes = raw.get("notes") or {}
        items = [_item(row, notes.get(sources.secid_of(row), ""))
                 for row in (raw.get("quotes") or [])]
        # 按涨跌幅排序：涨得最猛的在最上面。把 None（停牌）压到最后。
        items.sort(key=lambda x: (x["pct"] is None, -(x["pct"] or 0)))

        got = {sources.secid_of(row) for row in (raw.get("quotes") or [])}
        missing = [s for s in (raw.get("requested") or []) if s not in got]

        up = sum(1 for i in items if (i["pct"] or 0) > 0)
        down = sum(1 for i in items if (i["pct"] or 0) < 0)
        flat = len(items) - up - down
        known = [i["pct"] for i in items if i["pct"] is not None]
        avg = round(sum(known) / len(known), 2) if known else None
        # ⚠️ 最弱那只**不能**取排序后的最后一项：停牌股（pct 为 None）被排在最后，
        #    拿它当"最弱"会得出"今天最惨的是 XX"这种假结论。
        ranked = [i for i in items if i["pct"] is not None]

        # 用行情里的权威名字回填名单（用户可能只敲了代码）
        if not ctx.dry_run and items:
            userlist.fill_names(ctx.data_dir, {sources.secid_of(r): r.get("f14")
                                               for r in (raw.get("quotes") or [])
                                               if r.get("f14")})

        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "list_updated_at": raw.get("list_updated_at"),
            "empty": bool(raw.get("empty")) or not items,
            "count": len(items),
            "items": items,
            "missing": missing,
            "list_text": self._list_text(raw.get("list") or []),
            "list_display": " · ".join(
                f"{it['code']} {it['name']}".strip()
                for it in (raw.get("list") or [])[:12]),
            "summary": {
                "up": up, "down": down, "flat": flat,
                "avg_pct": avg,
                "avg_pct_text": _pct_text(avg),
                "tone": _tone(avg),
                "best": ranked[0]["name"] if ranked else "",
                "worst": ranked[-1]["name"] if ranked else "",
                "big_movers": [i["name"] for i in items
                               if abs(i["pct"] or 0) >= BIG_MOVE],
                "heavy": [i["name"] for i in items if i["big_amount"]],
            },
            "signals": self._signals(items, avg),
        }

    # ------------------------------------------------------------------ 结论
    @staticmethod
    def _list_text(items: list[dict]) -> str:
        """名单 → 表单里那一行行文本。

        优先写**代码**（人读得懂"600519"，读不懂"1.600519"），但只有当代码
        能**唯一还原回同一个 secid** 时才写代码；还原不回去的就写 secid 本身。

        为什么较这个真：表单是"回填 → 用户改一个字 → 再提交"的来回。
        如果回填的东西再解析一次会变成**另一只股票**，用户会在毫不知情的情况下
        把名单改了。这一条自检很便宜，但它挡住的是"静默换标的"。
        """
        lines: list[str] = []
        for it in items:
            code = it.get("code") or ""
            secid = it.get("secid") or ""
            if code and secid and sources.guess_secid(code) == secid:
                lines.append(code)
            else:
                lines.append(secid)
        return "\n".join(lines)

    @staticmethod
    def _signals(items: list[dict], avg: float | None) -> list[dict]:
        """规则化判断。每条都能对上一个可复现的数字条件，改口径就改这里。"""
        out: list[dict] = []
        if not items:
            return out

        if avg is not None:
            if avg >= BIG_MOVE:
                out.append({"level": "good", "text":
                            f"自选股整体走强，平均 {avg:+.2f}%（超过 {BIG_MOVE}% 算强）"})
            elif avg <= -BIG_MOVE:
                out.append({"level": "bad", "text":
                            f"自选股整体走弱，平均 {avg:+.2f}%"})
            else:
                out.append({"level": "flat", "text":
                            f"自选股整体窄幅波动，平均 {avg:+.2f}%"})

        up = sum(1 for i in items if (i["pct"] or 0) > 0)
        down = sum(1 for i in items if (i["pct"] or 0) < 0)
        if items:
            if up == len(items):
                out.append({"level": "good", "text": f"全部 {len(items)} 只上涨"})
            elif down == len(items):
                out.append({"level": "bad", "text": f"全部 {len(items)} 只下跌"})
            else:
                out.append({"level": "flat", "text":
                            f"{up} 涨 / {down} 跌" + (f" / {len(items)-up-down} 平"
                                                      if len(items) - up - down else "")})

        movers = [i for i in items if abs(i["pct"] or 0) >= BIG_MOVE]
        if movers:
            names = "、".join(f"{i['name']}({i['pct_text']})" for i in movers[:5])
            out.append({"level": "warn", "text":
                        f"波动超过 {BIG_MOVE}% 的有 {len(movers)} 只：{names}"})

        heavy = [i for i in items if i["big_amount"]]
        if heavy:
            names = "、".join(f"{i['name']}({i['amount_text']})" for i in heavy[:5])
            out.append({"level": "flat", "text": f"明显放量的：{names}"})
        return out

    # ------------------------------------------------------------------ report
    def report(self, data: Any, ctx: Context) -> str:
        if data.get("empty"):
            return ("## 我的自选股\n\n"
                    "还没添加任何标的。到 `/watchlist/` 页面底部把代码或名字填进去"
                    "（例如 `600519`、`茅台`、`hk00700`），保存后下一次采集就会出现。\n")

        L: list[str] = ["## 我的自选股", ""]
        s = data.get("summary") or {}
        L.append(f"**{data.get('count', 0)} 只** ｜ 涨 {s.get('up', 0)} · "
                 f"跌 {s.get('down', 0)} · 平 {s.get('flat', 0)} ｜ "
                 f"平均 {s.get('avg_pct_text', '—')}")
        L.append("")

        signals = data.get("signals") or []
        if signals:
            L.append("### 看点")
            L.append("")
            for sig in signals:
                L.append(f"- {sig['text']}")
            L.append("")

        L.append("### 明细")
        L.append("")
        L.append("| 名称 | 代码 | 市场 | 最新 | 涨跌幅 | 成交额 | 换手 | 市盈率 | 市净率 |")
        L.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
        for it in data.get("items") or []:
            L.append(f"| {it['name']} | {it['code']} | {it['market']} | "
                     f"{it['price_text']} | {it['pct_text']} | {it['amount_text']} | "
                     f"{it['turnover_text']} | {it['pe_text']} | {it['pb_text']} |")
        L.append("")

        missing = data.get("missing") or []
        if missing:
            L.append(f"> ⚠️ 有 {len(missing)} 只没拿到行情，可能已退市或代码写错了："
                     f"`{'`, `'.join(missing)}`")
            L.append("")

        L.append("> 数据来源：东方财富（`push2delay` 域，行情约 15 分钟延迟），"
                 "仅用于每日复盘，不适合盘中交易。")
        return "\n".join(L)
