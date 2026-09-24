"""论题卡 —— 给每只自选股记一条**可证伪的判断**，而不是一句"我看好"。

## 方法来源与为什么要这个

借鉴 `anthropics/financial-services` 里 equity-research 的 `thesis-tracker`：
持仓最危险的状态不是"跌了"，而是**当初买它的理由已经悄悄不成立，人却还在拿着**。
天天看涨跌幅**看不出来**这件事 —— 涨跌是结果，理由是另一回事。

所以每只股票允许带一张论题卡：

    statement   一句话论题（必须能被证伪）
    pillars     3-6 条支撑支柱，每条有自己的状态（达标 / 走弱 / 待验证）
    risks       ≥1 条"什么情况下我认错" —— **可以没有吗？不行**，见下
    exit        退出条件（触发就走，不临场找理由）
    catalysts   催化日历：什么时候会知道答案

## 两条硬规则（都是从源仓库抄来的，也都写成了代码）

1. ⭐ **不可证伪的论题不算论题。** 原文："A thesis should be falsifiable —
   if nothing could disprove it, it's not a thesis"。所以 `validate()` 里
   `risks` 为空 = **不合格**，页面会直接把问题列出来，而不是装作没事。
2. ⭐ **定期复盘。** 原文："Review theses at least quarterly, even when nothing
   dramatic has happened"。超过 `REVIEW_DAYS` 没更新的，`scorecard` 会标 `review_due` ——
   一张半年没动过的论题卡，信息量等于没有。

## 与"只读今天这一份快照"的关系

论题卡是**用户数据**（和名单一样），不是算出来的。但它是会变的 ——
所以 `collect` 把当天那一份原样写进快照，页面渲染的是**那天的论题**。
半年后打开今天的页面，看到的是"当时我是怎么想的"，而不是现在的想法。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

FILE_NAME = "thesis.json"

MAX_PILLARS = 6
MAX_RISKS = 6
MAX_CATALYSTS = 8
STATEMENT_MAX = 120
PILLAR_MAX = 80
NOTE_MAX = 120
EXIT_MAX = 120
CATALYST_MAX = 60

REVIEW_DAYS = 90          # 超过它没更新 → 提示该复盘了
SOON_DAYS = 14            # 催化剂进入这个窗口 → 算"临近"

STATUSES = ("on_track", "behind", "pending")
STATUS_LABEL = {"on_track": ("达标", "good"),
                "behind": ("走弱", "bad"),
                "pending": ("待验证", "flat")}


# ------------------------------------------------------------------ 规整
def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\n", " ").replace("\r", " ").strip()[:limit]


def _pillar(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"), PILLAR_MAX)
    if not text:
        return None
    status = str(raw.get("status") or "").strip()
    return {
        "text": text,
        # 认不出的状态一律当"待验证"：不猜、也不丢
        "status": status if status in STATUSES else "pending",
        "note": _text(raw.get("note"), NOTE_MAX),
    }


def _catalyst(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = _text(raw.get("text"), CATALYST_MAX)
    when = _text(raw.get("date"), 10)
    if not (text or when):
        return None
    return {"date": when, "text": text}


def normalize(raw) -> dict | None:
    """把一条论题规整成可信形状；连论题本身都没有就返回 None。"""
    if not isinstance(raw, dict):
        return None
    statement = _text(raw.get("statement"), STATEMENT_MAX)
    pillars = [p for p in (_pillar(x) for x in (raw.get("pillars") or [])) if p]
    risks = [t for t in (_text(x, PILLAR_MAX) for x in (raw.get("risks") or [])) if t]
    catalysts = [c for c in (_catalyst(x) for x in (raw.get("catalysts") or [])) if c]
    exit_text = _text(raw.get("exit"), EXIT_MAX)
    if not (statement or pillars or risks):
        return None
    return {
        "statement": statement,
        "pillars": pillars[:MAX_PILLARS],
        "risks": risks[:MAX_RISKS],
        "catalysts": catalysts[:MAX_CATALYSTS],
        "exit": exit_text,
        "updated_at": _text(raw.get("updated_at"), 32),
    }


def validate(thesis: dict) -> list[str]:
    """返回**问题清单**（空 = 合格）。

    这里刻意不抛异常、也不自动修补：一张缺胳膊少腿的论题卡如果被"宽容"地
    渲染出来，用户会以为它是完整的。把问题明写在页面上才有人去补。
    """
    if not thesis:
        return ["论题卡是空的"]
    problems: list[str] = []
    if not thesis.get("statement"):
        problems.append("缺一句话论题（statement）")
    if not thesis.get("pillars"):
        problems.append("缺支撑支柱（pillars）—— 没有支柱就没有可对照的判据")
    if not thesis.get("risks"):
        problems.append(
            "缺「什么情况下我认错」（risks）—— "
            "不可证伪的论题不算论题，这一条不能空")
    if not thesis.get("exit"):
        problems.append("缺退出条件（exit）—— 到点了会临场找理由")
    return problems


# ------------------------------------------------------------------ 读写
def path(data_dir) -> Path:
    return Path(data_dir) / FILE_NAME


def load(data_dir) -> dict:
    """读全部论题卡。文件不存在 / 坏了 → 空，**不抛异常**（和名单同一口径）。"""
    p = path(data_dir)
    if not p.is_file():
        return {"updated_at": None, "items": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updated_at": None, "items": {}}
    raw = data.get("items") if isinstance(data, dict) else None
    items: dict[str, dict] = {}
    if isinstance(raw, dict):
        for secid, one in raw.items():
            got = normalize(one)
            if got:
                items[str(secid)] = got
    return {"updated_at": (data.get("updated_at") if isinstance(data, dict) else None),
            "items": items}


# ------------------------------------------------------------------ 记分板
def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def scorecard(thesis: dict, today: date | None = None) -> dict:
    """论题卡 → 可渲染的记分板。**纯函数**（今天由调用方给，便于测试）。

    刻意保留 `problems`：不合格的论题卡也要出示，并明写缺什么。
    直接藏起来的话，用户不会知道"我少写了一条"。
    """
    if not thesis:
        return {}
    today = today or date.today()
    problems = validate(thesis)

    counts = {k: 0 for k in STATUSES}
    pillars: list[dict] = []
    for p in thesis.get("pillars") or []:
        status = p.get("status") or "pending"
        counts[status] = counts.get(status, 0) + 1
        label, tone = STATUS_LABEL.get(status, STATUS_LABEL["pending"])
        pillars.append({"text": p.get("text", ""), "status": status,
                        "label": label, "tone": tone, "note": p.get("note", "")})

    # 判词：**走弱优先**。只要有一条支柱走弱，就不该说"整体还行" ——
    # 论题失效通常是从一条支柱塌掉开始的，平均数会把它抹平。
    if counts["behind"]:
        verdict = {"text": f"{counts['behind']} 条支柱走弱，论题需要重估", "tone": "bad"}
    elif pillars and counts["on_track"] == len(pillars):
        verdict = {"text": "支柱全部达标，论题成立", "tone": "good"}
    elif counts["on_track"]:
        verdict = {"text": f"{counts['on_track']}/{len(pillars)} 条达标，其余待验证",
                   "tone": "flat"}
    else:
        verdict = {"text": "支柱都还没结论，先记着", "tone": "flat"}

    # 催化剂：标出"临近"（含已过期 —— 过期了最该被看到）
    due: list[dict] = []
    catalysts: list[dict] = []
    for c in thesis.get("catalysts") or []:
        when = _parse_date(c.get("date", ""))
        entry = {"date": c.get("date", ""), "text": c.get("text", "")}
        if when:
            days = (when - today).days
            entry["days"] = days
            entry["overdue"] = days < 0
            entry["soon"] = 0 <= days <= SOON_DAYS
            if entry["soon"] or entry["overdue"]:
                due.append(entry)
        catalysts.append(entry)

    # 复盘提醒：算"多久没动过这张卡"
    updated = _parse_date((thesis.get("updated_at") or "")[:10])
    stale_days = (today - updated).days if updated else None
    review = {
        "updated_at": thesis.get("updated_at", ""),
        "stale_days": stale_days,
        "due": bool(stale_days is None or stale_days > REVIEW_DAYS),
        "text": ("没记更新日期" if stale_days is None
                 else f"{stale_days} 天前更新"),
    }

    return {
        "statement": thesis.get("statement", ""),
        "pillars": pillars,
        "counts": counts,
        "counts_text": " · ".join(f"{STATUS_LABEL[k][0]} {counts[k]}"
                                  for k in STATUSES if counts[k]),
        "verdict": verdict,
        "risks": list(thesis.get("risks") or []),
        "exit": thesis.get("exit", ""),
        "catalysts": catalysts,
        "due": due,
        "review": review,
        "problems": problems,
        "ok": not problems,
    }
