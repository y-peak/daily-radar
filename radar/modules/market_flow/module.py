"""股市资金流向模块 —— 中国 + 全球主要指数、板块与个股资金流向、两融，以及一份规则化的分析报告。

**设计要点一：容错优先。**
资金流是"多源拼图"，任何一个源挂了都不该让整份报告消失。
所以每个源单独 try/except，成功几块就拼几块，缺口记在 `errors` 里并在页面明示 ——
宁可报告缺一角，也不要因为一个接口抽风而白屏。

**设计要点二：结论必须由数据推出来，不靠猜。**
下面 report() 里的每条判断都能对应到一个可复现的数字条件
（例如"指数跌但仍有板块净流入超 10 亿 → 判定结构性行情"）。
这些规则写在一处（`_signals`），改口径就改这里，不用翻报告模板。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from radar.core.module import Context, Module, load_sibling

sources = load_sibling(__file__, "sources")

# 判定用的阈值，集中放这里便于调参
BIG_FLOW_YI = 10.0          # "大额净流入"门槛（亿元）
BREADTH_GOOD = 60.0         # 上涨家数占比超过它算"赚钱效应好"
BREADTH_BAD = 25.0          # 低于它算"普跌"
STRONG_PCT = 1.0            # 指数"明显"涨跌的幅度


# ------------------------------------------------------------------ 格式化
def _yi(value) -> float | None:
    """元 → 亿元。"""
    if not isinstance(value, (int, float)):
        return None
    return round(value / 1e8, 2)


def _yi_text(value) -> str:
    v = _yi(value)
    if v is None:
        return "—"
    return f"{v:+.2f} 亿" if v else "0.00 亿"


def _pct_text(value) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{value:+.2f}%"


def _tone(value) -> str:
    """红涨绿跌（中国习惯）。"""
    if not isinstance(value, (int, float)) or value == 0:
        return "flat"
    return "up" if value > 0 else "down"


def _amount_text(value) -> str:
    """成交额 → 万亿 / 亿。"""
    if not isinstance(value, (int, float)):
        return "—"
    if value >= 1e12:
        return f"{value / 1e12:.2f} 万亿"
    if value >= 1e8:
        return f"{value / 1e8:.0f} 亿"
    return f"{value:.0f}"


def _item(raw: dict, kind: str) -> dict:
    """把东财的一个条目规整成扁平结构。kind: index / board / stock。"""
    pct = raw.get("f3")
    flow = _yi(raw.get("f62"))
    return {
        "code": raw.get("f12") or "",
        "name": raw.get("f14") or "",
        "price": raw.get("f2"),
        "pct": pct if isinstance(pct, (int, float)) else None,
        "pct_text": _pct_text(pct),
        "tone": _tone(pct),
        "flow": flow,
        "flow_text": _yi_text(raw.get("f62")),
        "flow_tone": _tone(flow),
        "flow_pct": raw.get("f184") if isinstance(raw.get("f184"), (int, float)) else None,
        # 资金分层：超大单 / 大单 是"机构"，中单 / 小单 是"散户"
        "super": _yi(raw.get("f66")),
        "super_text": _yi_text(raw.get("f66")),
        "big": _yi(raw.get("f72")),
        "big_text": _yi_text(raw.get("f72")),
        "mid": _yi(raw.get("f78")),
        "mid_text": _yi_text(raw.get("f78")),
        "small": _yi(raw.get("f84")),
        "small_text": _yi_text(raw.get("f84")),
        "amount": raw.get("f6"),
        "amount_text": _amount_text(raw.get("f6")),
        "amplitude": raw.get("f7"),
        "kind": kind,
    }


def _idx_item(raw: dict, region: str) -> dict:
    pct = raw.get("f3")
    return {
        "code": raw.get("f12") or "",
        "name": raw.get("f14") or "",
        "region": region,
        "price": raw.get("f2"),
        "price_text": f"{raw['f2']:,.2f}" if isinstance(raw.get("f2"), (int, float)) else "—",
        "pct": pct if isinstance(pct, (int, float)) else None,
        "pct_text": _pct_text(pct),
        "tone": _tone(pct),
        "change": raw.get("f4"),
        "amount": raw.get("f6"),
        "amount_text": _amount_text(raw.get("f6")),
        "amplitude": raw.get("f7"),
        "high": raw.get("f15"),
        "low": raw.get("f16"),
        "ts": raw.get("f124"),
    }


class MarketFlow(Module):
    name = "market_flow"
    title = "股市资金流"
    subtitle = "中国与全球主要指数、板块/个股资金流向、两融，附分析报告"
    order = 20
    schedule = "每日 08:10"

    # ------------------------------------------------------------------ collect
    def collect(self, ctx: Context) -> Any:
        cfg = ctx.config.module_cfg(self.name)
        top_industries = int(cfg.get("top_n", 10))
        top_stocks = int(cfg.get("stock_top_n", 15))
        want_global = cfg.get("global", True)
        want_breadth = cfg.get("breadth", True)
        want_margin = cfg.get("margin", True)

        out: dict[str, Any] = {
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "errors": {},
            "ok_sources": [],
        }

        def grab(key: str, label: str, fn):
            try:
                out[key] = fn()
                out["ok_sources"].append(label)
                ctx.log_info(f"      ✓ {label}")
            except Exception as exc:  # noqa: BLE001
                out["errors"][key] = f"{type(exc).__name__}: {exc}"
                ctx.log_info(f"      ✗ {label}：{type(exc).__name__}: {exc}")

        grab("cn_indices", "国内指数",
             lambda: sources.indices(ctx.http, sources.INDEX_CN))
        if want_global:
            grab("global_indices", "全球指数",
                 lambda: sources.indices(ctx.http, sources.INDEX_GLOBAL))
        grab("industry", "行业板块资金流",
             lambda: sources.board_flow(ctx.http, top_industries, "industry"))
        grab("concept", "概念板块资金流",
             lambda: sources.board_flow(ctx.http, top_industries, "concept"))
        grab("stock", "个股资金流",
             lambda: sources.stock_flow(ctx.http, top_stocks))
        if want_breadth:
            grab("breadth", "涨跌家数",
                 lambda: sources.breadth(
                     ctx.http, max_pages=int(cfg.get("breadth_max_pages", 60))))
        if want_margin:
            grab("margin", "两融数据", lambda: sources.margin_history(ctx.http, 10))

        # 指数兜底：东财的指数接口全挂时，退到腾讯
        if not out.get("cn_indices"):
            try:
                out["fallback_tencent"] = sources.indices_tencent(
                    ctx.http, ["sh000001", "sz399001", "sz399006", "sh000300"])
                out["ok_sources"].append("国内指数(腾讯备用)")
                ctx.log_info("      ✓ 国内指数(腾讯备用)")
            except Exception as exc:  # noqa: BLE001
                out["errors"]["fallback_tencent"] = f"{type(exc).__name__}: {exc}"

        if not out["ok_sources"]:
            raise RuntimeError("所有数据源都失败了：" + repr(out["errors"])[:300])
        return out

    # ------------------------------------------------------------------ analyze
    def analyze(self, raw: Any, ctx: Context) -> Any:
        cn = [_idx_item(i, "cn") for i in (raw.get("cn_indices") or [])]
        gl = [_idx_item(i, "global") for i in (raw.get("global_indices") or [])]

        def flow_block(key: str, kind: str) -> dict:
            block = raw.get(key) or {}
            return {
                "inflow": [_item(i, kind) for i in (block.get("inflow") or [])],
                "outflow": [_item(i, kind) for i in (block.get("outflow") or [])],
            }

        industry = flow_block("industry", "board")
        concept = flow_block("concept", "board")
        stock = flow_block("stock", "stock")

        breadth = raw.get("breadth") or {}
        if breadth:
            total = breadth.get("total") or 0
            up = breadth.get("up") or 0
            down = breadth.get("down") or 0
            breadth["up_ratio"] = round(up / total * 100, 1) if total else None
            # down 为 0 时不写 ratio —— float('inf') 会序列化成非法的 JSON "Infinity"
            breadth["ratio"] = round(up / down, 2) if down else None
            breadth["tone"] = "up" if up > down else ("down" if down > up else "flat")

        margin = self._analyze_margin(raw.get("margin") or [])

        data = {
            "fetched_at": raw.get("fetched_at"),
            "ok_sources": raw.get("ok_sources", []),
            "errors": raw.get("errors", {}),
            "cn_indices": cn,
            "global_indices": gl,
            "industry": industry,
            "concept": concept,
            "stock": stock,
            "breadth": breadth,
            "margin": margin,
        }
        data["summary"] = self._summarize(data)
        data["signals"] = self._signals(data)
        return data

    @staticmethod
    def _analyze_margin(rows: list[dict]) -> dict:
        """两融：rows 是按日期倒序的原始记录。"""
        if not rows:
            return {}
        latest = rows[0]
        prev = rows[1] if len(rows) > 1 else None

        def f(row, key):
            v = row.get(key)
            return round(v / 1e8, 2) if isinstance(v, (int, float)) else None

        rzye = f(latest, "RZYE")
        rzjme = f(latest, "RZJME")
        delta = None
        if rzye is not None and prev is not None:
            p = f(prev, "RZYE")
            if p is not None:
                delta = round(rzye - p, 2)

        trend = []
        for row in rows[:5]:
            trend.append({
                "date": (row.get("DIM_DATE") or "")[:10],
                "rzye": f(row, "RZYE"),
                "rzjme": f(row, "RZJME"),
                "pct": row.get("ZDF"),
            })
        return {
            "date": (latest.get("DIM_DATE") or "")[:10],
            "rzye": rzye,
            "rzjme": rzjme,
            "delta": delta,
            "delta_tone": _tone(delta),
            "rqye": f(latest, "RQYE"),
            "turnover_pct": latest.get("RZYEZB"),
            "trend": trend,
        }

    @staticmethod
    def _summarize(data: dict) -> dict:
        cn = data["cn_indices"]
        up = sum(1 for i in cn if (i["pct"] or 0) > 0)
        down = sum(1 for i in cn if (i["pct"] or 0) < 0)
        amounts = [i["amount"] for i in cn
                   if i["code"] in ("000001", "399001") and i["amount"]]
        ind_in = data["industry"]["inflow"]
        return {
            "cn_up": up,
            "cn_down": down,
            "cn_total": len(cn),
            "amount_total": sum(amounts) if amounts else None,
            "amount_total_text": _amount_text(sum(amounts)) if amounts else "—",
            "top_industry": ind_in[0]["name"] if ind_in else None,
            "top_industry_flow": ind_in[0]["flow"] if ind_in else None,
            "top_industry_flow_text": ind_in[0]["flow_text"] if ind_in else "—",
            "top_stock": (data["stock"]["inflow"][0]["name"]
                          if data["stock"]["inflow"] else None),
            "top_stock_flow_text": (data["stock"]["inflow"][0]["flow_text"]
                                    if data["stock"]["inflow"] else "—"),
        }

    @staticmethod
    def _signals(data: dict) -> list[dict]:
        """规则化结论。每条都有明确的触发条件，可复现、可调参。"""
        out: list[dict] = []
        cn = data["cn_indices"]
        breadth = data.get("breadth") or {}
        industry = data["industry"]
        margin = data.get("margin") or {}

        main = next((i for i in cn if i["code"] == "000001"), None)
        main_pct = (main or {}).get("pct")

        # 1) 大盘方向
        if main_pct is not None:
            if main_pct <= -STRONG_PCT:
                out.append({"level": "warn", "title": "大盘明显走弱",
                            "text": f"上证指数 {main_pct:+.2f}%，跌破日内关键位，"
                                    f"注意系统性回撤风险。"})
            elif main_pct >= STRONG_PCT:
                out.append({"level": "ok", "title": "大盘走强",
                            "text": f"上证指数 {main_pct:+.2f}%，量价配合需看成交额是否同步放大。"})
            else:
                out.append({"level": "info", "title": "大盘窄幅震荡",
                            "text": f"上证指数 {main_pct:+.2f}%，方向未明，资金更可能做结构性选择。"})

        # 2) 结构性行情判定：指数没涨但仍有板块大额净流入
        big_in = [i for i in industry["inflow"] if (i["flow"] or 0) >= BIG_FLOW_YI]
        if big_in and main_pct is not None and main_pct < 0:
            names = "、".join(f"{i['name']}({i['flow_text']})" for i in big_in[:3])
            out.append({"level": "info", "title": "结构性行情：钱在少数方向抱团",
                        "text": f"指数下跌但仍有 {len(big_in)} 个行业净流入超 "
                                f"{BIG_FLOW_YI:.0f} 亿 —— {names}。"
                                f"说明不是没钱，是钱只肯去少数地方。"})
        elif big_in:
            names = "、".join(f"{i['name']}({i['flow_text']})" for i in big_in[:3])
            out.append({"level": "ok", "title": "资金有明确主线",
                        "text": f"{len(big_in)} 个行业净流入超 {BIG_FLOW_YI:.0f} 亿：{names}。"})

        # 3) 资金分层：主力进、散户出（或反之）
        top = (industry["inflow"] or [None])[0]
        if top and top.get("super") is not None and top.get("small") is not None:
            if top["super"] > 0 > (top["small"] or 0):
                out.append({"level": "ok", "title": f"{top['name']}：主力接散户抛盘",
                            "text": f"超大单 {top['super_text']}、小单 {top['small_text']}，"
                                    f"典型的主力吸筹结构。"})
            elif (top["super"] or 0) < 0 < (top["small"] or 0):
                out.append({"level": "warn", "title": f"{top['name']}：主力在派发",
                            "text": f"超大单 {top['super_text']}、小单 {top['small_text']}，"
                                    f"主力出货、散户接盘的结构，追高风险大。"})

        # 4) 市场广度（用上涨占比判定 —— 比 up/down 比值稳健，因为 down 可能为 0）
        up_ratio = breadth.get("up_ratio")
        if up_ratio is not None:
            detail = f"涨 {breadth.get('up', 0)} / 跌 {breadth.get('down', 0)}，上涨占比 {up_ratio}%"
            if up_ratio >= BREADTH_GOOD:
                out.append({"level": "ok", "title": "赚钱效应好", "text": detail + "。"})
            elif up_ratio <= BREADTH_BAD:
                out.append({"level": "warn", "title": "普跌，赚钱效应差", "text": detail + "。"})
            else:
                out.append({"level": "info", "title": "涨跌分化",
                            "text": detail + "，多空分歧。"})

        # 5) 两融（杠杆资金态度）
        if margin.get("delta") is not None:
            tone = "加杠杆" if margin["delta"] > 0 else "去杠杆"
            out.append({"level": "ok" if margin["delta"] > 0 else "info",
                        "title": f"两融资金在{tone}",
                        "text": f"{margin['date']} 融资余额 {margin['rzye']:.0f} 亿，"
                                f"较前一日 {margin['delta']:+.2f} 亿。"})

        # 6) 遗漏提醒
        if not data.get("global_indices"):
            out.append({"level": "info", "title": "全球指数缺失",
                        "text": "本次未取到海外指数，报告仅有 A 股部分。"})
        return out

    # ------------------------------------------------------------------- report
    def report(self, data: Any, ctx: Context) -> str:
        L: list[str] = []
        s = data.get("summary", {})
        L.append(f"# 股市资金流向 · {ctx.date}")
        L.append("")
        L.append("> 数据来源：东方财富（`push2delay` 域，行情约 15 分钟延迟）、"
                 "沪深两融汇总。抓取时间 " + str(data.get("fetched_at", "—")) + "。")
        L.append("")

        # ---------------------------------------------------------- 结论摘要
        L.append("## 结论摘要")
        L.append("")
        for sig in data.get("signals", []):
            mark = {"ok": "✅", "warn": "⚠️", "info": "ℹ️"}.get(sig["level"], "•")
            L.append(f"- {mark} **{sig['title']}** —— {sig['text']}")
        L.append("")

        # ------------------------------------------------------------ 指数
        L.append("## 一、指数")
        L.append("")
        L.append("| 指数 | 最新 | 涨跌幅 | 成交额 | 振幅 |")
        L.append("|---|---:|---:|---:|---:|")
        for i in data["cn_indices"]:
            L.append(f"| {i['name']} | {i['price_text']} | {i['pct_text']} "
                     f"| {i['amount_text']} | {i['amplitude'] if i['amplitude'] is not None else '—'}% |")
        L.append("")
        if s.get("amount_total_text") and s["amount_total_text"] != "—":
            L.append(f"两市合计成交额约 **{s['amount_total_text']}**"
                     f"（沪市 + 深市，含全部上市股票）。")
            L.append("")
        if data["global_indices"]:
            L.append("**全球主要指数**（均为最近收盘）")
            L.append("")
            L.append("| 指数 | 最新 | 涨跌幅 |")
            L.append("|---|---:|---:|")
            for i in data["global_indices"]:
                L.append(f"| {i['name']} | {i['price_text']} | {i['pct_text']} |")
            L.append("")

        # -------------------------------------------------------- 板块资金
        L.append("## 二、板块资金流")
        L.append("")
        for label, block in (("行业板块", data["industry"]), ("概念板块", data["concept"])):
            L.append(f"### {label}")
            L.append("")
            L.append("| 排名 | 名称 | 涨跌幅 | 主力净流入 | 净占比 | 超大单 | 小单 |")
            L.append("|---:|---|---:|---:|---:|---:|---:|")
            for n, it in enumerate(block["inflow"], 1):
                L.append(f"| {n} | {it['name']} | {it['pct_text']} | {it['flow_text']} "
                         f"| {it['flow_pct'] if it['flow_pct'] is not None else '—'}% "
                         f"| {it['super_text']} | {it['small_text']} |")
            L.append("")
            if block["outflow"]:
                names = "、".join(f"{i['name']}({i['flow_text']})" for i in block["outflow"][:5])
                L.append(f"**净流出前列**：{names}")
                L.append("")

        # -------------------------------------------------------- 个股资金
        L.append("## 三、个股主力资金")
        L.append("")
        L.append("| 排名 | 名称 | 涨跌幅 | 主力净流入 | 净占比 | 超大单 |")
        L.append("|---:|---|---:|---:|---:|---:|")
        for n, it in enumerate(data["stock"]["inflow"], 1):
            L.append(f"| {n} | {it['name']} | {it['pct_text']} | {it['flow_text']} "
                     f"| {it['flow_pct'] if it['flow_pct'] is not None else '—'}% "
                     f"| {it['super_text']} |")
        L.append("")
        if data["stock"]["outflow"]:
            names = "、".join(f"{i['name']}({i['flow_text']})" for i in data["stock"]["outflow"][:5])
            L.append(f"**净流出前列**：{names}")
            L.append("")

        # ------------------------------------------------------------ 两融
        m = data.get("margin") or {}
        if m:
            L.append("## 四、两融（杠杆资金）")
            L.append("")
            if m.get("rzye") is not None:
                line = f"- **{m.get('date', '—')}** 融资余额 **{m['rzye']:.0f} 亿**"
                if m.get("delta") is not None:
                    line += (f"，较前一交易日 {m['delta']:+.2f} 亿"
                             f"（{'资金在加杠杆' if m['delta'] > 0 else '资金在去杠杆'}）")
                L.append(line + "。")
                # 融资余额的日变化理应等于官方"融资净买入"；对不上就说明口径有变
                if (m.get("rzjme") is not None and m.get("delta") is not None
                        and abs(m["rzjme"] - m["delta"]) > 0.5):
                    L.append(f"- ⚠️ 口径校验：官方融资净买入为 {m['rzjme']:+.2f} 亿，"
                             f"与余额变化 {m['delta']:+.2f} 亿 不一致，请留意数据口径调整。")
            if m.get("rqye") is not None:
                L.append(f"- 融券余额 {m['rqye']:.2f} 亿")
            if m.get("turnover_pct") is not None:
                L.append(f"- 融资余额占流通市值 {m['turnover_pct']:.2f}%")
            L.append("")
            L.append("| 日期 | 融资余额(亿) | 融资净买入(亿) | 指数涨跌% |")
            L.append("|---|---:|---:|---:|")
            for row in m.get("trend", []):
                L.append(f"| {row['date']} | {row.get('rzye', '—')} "
                         f"| {row.get('rzjme', '—')} | {row.get('pct', '—')} |")
            L.append("")

        # ------------------------------------------------------------ 缺口
        ok = data.get("ok_sources") or []
        err = data.get("errors") or {}
        L.append("## 数据完整性")
        L.append("")
        L.append(f"- 成功 {len(ok)} 项：{'、'.join(ok)}")
        if err:
            for key, msg in err.items():
                L.append(f"- ⚠️ 缺失 `{key}`：{msg}")
        else:
            L.append("- 无缺失")
        L.append("")

        # ------------------------------------------------------------ 口径说明
        L.append("---")
        L.append("")
        L.append("### 口径与免责说明")
        L.append("")
        L.append("- **北向资金（外资）已停发**：2024-08-19 起沪深港通不再逐日公布实时资金流，"
                 "因此本报告用「板块主力净流入 + 两融余额」替代外资流向这一维度。")
        L.append("- **主力资金口径**：东财按单笔成交金额分档 —— 超大单(>100万)、大单(20~100万)"
                 "视为主力；中单、小单视为散户。`主力净流入 = 超大单 + 大单`。")
        L.append("- **行情延迟**：`push2delay` 域约 15 分钟延迟，适合每日复盘，不适合盘中交易。")
        L.append("- 本报告由规则自动生成，**不构成任何投资建议**。")
        L.append("")
        return "\n".join(L)
