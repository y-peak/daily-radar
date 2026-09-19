"""落盘与索引。

存储约定：

    data/<mod>/raw/<date>.json   原始响应 —— 便于回溯"当时接口到底返回了什么"
    data/<mod>/<date>.json       加工后的结构化结果
    data/<mod>/<date>.md         人读的结论
    data/index.json              站点索引：模块清单 + 各模块可用日期
    data/runs.json               运行历史（最近若干次）

为什么不上数据库：产物天然是"按天分片的小 JSON"，
文件系统就是最合适的存储 —— 还能直接 diff / grep / 手工改，排查成本最低。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

MAX_RUNS = 200


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)          # 原子替换，避免读到写了一半的文件


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------- 日期索引
def module_dates(data_dir: Path, module: str) -> list[str]:
    """某模块已有的日期列表，倒序（最新在前）。"""
    folder = data_dir / module
    if not folder.is_dir():
        return []
    dates = [
        p.stem
        for p in folder.glob("*.json")
        if p.name != "index.json" and len(p.stem) == 10 and p.stem[4] == "-"
    ]
    return sorted(dates, reverse=True)


def latest_date(data_dir: Path, module: str) -> str | None:
    dates = module_dates(data_dir, module)
    return dates[0] if dates else None


# ------------------------------------------------------------------------ 索引
def _trend_polylines(values, w=200, h=32):
    """把一组数渲成 polyline points 字符串 + 区域填充 points。
    没用 Jinja 过滤器（spark）—— build_index 不渲染 HTML，每次跑模板都重算代价大。
    直接生成 SVG 属性字符串嵌进索引 JSON。"""
    if not values or len(values) < 2:
        return None
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    n = len(vals)
    pts = []
    pad = 2  # SVG 上下留 2px 边距
    for i, v in enumerate(vals):
        x = (w - 1) * i / (n - 1)
        # 越高越好反转（屏幕坐标 y 向下），所以算 (hi - v) → 顶部为最大
        y = pad + (h - 2 * pad) * (hi - v) / rng
        pts.append(f"{x:.1f},{y:.1f}")
    line = " ".join(pts)
    area = line + f" {w-1:.1f},{h:.1f} 0,{h:.1f}"
    return {"line": line, "area": area}


def _hero_for_module(name: str, data: dict, dates: list[str]) -> dict:
    """模块首页卡片的"今天最关键一个数"。
    实现约定：
      - 返回 {} 表示不展示 hero（模板降级渲染）
      - 所有字段都用原生 float / str，方便模板里直接走 count-up 或格式化
    """
    if name == "market_flow":
        idx = (data.get("cn_indices") or [])
        if idx and idx[0].get("price") is not None:
            price = float(idx[0]["price"])
            pct = idx[0].get("pct")
            return {"value": price, "suffix": "", "decimals": 2,
                    "delta": pct, "tone": idx[0].get("tone")}
        breadth = data.get("breadth") or {}
        if breadth.get("total"):
            return {"value": breadth.get("up_ratio"), "suffix": "%",
                    "delta": breadth.get("ratio"), "tone": breadth.get("tone")}
    elif name == "github_trending":
        stats = data.get("stats") or {}
        n = data.get("count") or len(data.get("repos") or [])
        avg = stats.get("avg_growth")
        if avg:
            return {"value": avg, "suffix": "", "decimals": 0,
                    "delta": (stats.get("top_growth") or 0) - avg,
                    "tone": "up" if avg > 0 else "flat"}
        if n:
            return {"value": n, "suffix": " 个", "decimals": 0, "tone": "accent"}
    elif name == "reader":
        # 书架的核心数字是"总章数" —— 它直接告诉人"还有多少可读"。
        # 已读进度存在浏览器 localStorage 里，构建时拿不到，也不该在这里造。
        totals = data.get("totals") or {}
        chapters = totals.get("chapters")
        if chapters:
            return {"value": chapters, "suffix": " 章", "decimals": 0,
                    "delta": totals.get("books"), "tone": "accent"}
    elif name == "watchlist":
        # 自选股的首页数字：**我的票平均涨跌** ——
        # 它比"上证指数"更贴己（首页第一张卡说的就应该是"我关心的东西怎么样"）。
        # 名单为空时不展示 hero，让模板降级成"去加两只"的引导。
        s = data.get("summary") or {}
        avg = s.get("avg_pct")
        if isinstance(avg, (int, float)) and data.get("count"):
            return {"value": avg, "suffix": "%", "decimals": 2,
                    "delta": None, "tone": s.get("tone") or "flat"}
    return {}


def _trend_for_module(name: str, data: dict, data_dir: Path, dates: list[str]) -> dict:
    """首页卡迷你折线。
    数据源只用历史快照自己（margin.trend 之类），不跨日期文件读 ===>
    旧页面重渲也是当时看到的那条曲线，"历史页面不可变"不破。
    但首页是今日卡片，应该"表现今天走势" —— 用最新一天里
    自带的多日序列。所以 market_flow 用 margin.trend，github 用日增长聚合。"""
    label = ""
    vals = []
    if name == "market_flow":
        margin = data.get("margin") or {}
        trend = list(reversed(margin.get("trend") or []))
        for row in trend:
            rzye = row.get("rzye")
            if isinstance(rzye, (int, float)):
                vals.append(float(rzye))
        label = "融资余额"
    elif name == "github_trending":
        for row in (data.get("repos") or [])[:10]:
            g = row.get("stars_growth")
            if isinstance(g, (int, float)):
                vals.append(float(g))
        label = "今日涨幅"
    poly = _trend_polylines(vals)
    if not poly:
        return {}
    return {"points": poly["line"], "area": poly["area"], "label": label or "7 日趋势"}


def build_index(data_dir: Path, modules: list[dict]) -> dict:
    """生成站点索引。前端和 /api/status 都读它。

    除了模块清单，每个模块还会注入 `hero` / `trend` 字段供首页卡渲染。
    实现细节：latest.json 在采集后就写好，但读它做 hero 计算是廉价 IO
    （几 KB JSON）—— 总控调用 build_index 是页面构建路径，
    比让 Jinja 在每个访问页都跑计算更划算。"""
    entries = []
    for meta in modules:
        name = meta["name"]
        dates = module_dates(data_dir, name)
        latest = dates[0] if dates else None
        entry = {
            **meta,
            "dates": dates[:60],
            "latest": latest,
            "days": len(dates),
        }
        if latest:
            latest_data = read_json(data_dir / name / f"{latest}.json", default={})
            entry["hero"] = _hero_for_module(name, latest_data, dates)
            entry["trend"] = _trend_for_module(name, latest_data, data_dir, dates)
        else:
            entry["hero"] = {}
            entry["trend"] = {}
        entries.append(entry)
    entries.sort(key=lambda e: e.get("order", 100))
    return {"generated_at": datetime.now().isoformat(timespec="seconds"), "modules": entries}


def save_index(data_dir: Path, index: dict) -> None:
    write_json(data_dir / "index.json", index)


def load_index(data_dir: Path) -> dict:
    return read_json(data_dir / "index.json", default={"modules": []}) or {"modules": []}


def fingerprint(index: dict, data_dir: Path | None = None) -> str:
    """站点版本指纹 —— 手机端"监测到更新"就靠它。

    两段拼成：

        <最新日期>-<内容摘要 sha1 前 12 位>

    内容摘要覆盖两部分：
      * 各模块的 (名称, 最新日期, 天数) —— 跨天时必然变化
      * **每个模块最新数据文件的 (字节数, mtime)** —— 同一天内重跑也变

    第二条是关键：定时任务每天 08:00 和 16:30 各跑一次，同一天的指纹如果只看日期，
    收盘后的第二次采集就不会被手机察觉，页面会一直显示早上的旧数据。
    带上文件 mtime 之后，"数据变了"和"指纹变了"才是等价的。
    而纯粹的重复构建（数据没动）mtime 不变，指纹不变，不会误报"有更新"。
    """
    parts = [f"{m.get('name')}:{m.get('latest') or '-'}:{m.get('days', 0)}"
             for m in index.get("modules", [])]
    if data_dir is not None:
        for m in index.get("modules", []):
            latest = m.get("latest")
            if not latest:
                continue
            path = Path(data_dir) / str(m.get("name")) / f"{latest}.json"
            try:
                st = path.stat()
                # 用 ns 精度：同一天内两次采集可能落在同一秒，秒级 mtime 会漏判
                parts.append(f"{m.get('name')}@{latest}:{st.st_size}:{st.st_mtime_ns}")
            except OSError:
                parts.append(f"{m.get('name')}@{latest}:missing")

    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]
    latest = max([m.get("latest") or "" for m in index.get("modules", [])] or [""])
    return f"{latest or 'empty'}-{digest}"


# -------------------------------------------------------------------- 运行历史
def record_run(data_dir: Path, entry: dict) -> dict:
    path = data_dir / "runs.json"
    history = read_json(path, default=[]) or []
    history.insert(0, entry)
    write_json(path, history[:MAX_RUNS])
    return entry


def load_runs(data_dir: Path, limit: int = 30) -> list[dict]:
    return (read_json(data_dir / "runs.json", default=[]) or [])[:limit]


# ------------------------------------------------------------------------ 清理
def prune_old(data_dir: Path, module: str, keep_days: int) -> list[str]:
    """删掉过老的按天产物，报告（.md）不在清理范围内。"""
    if keep_days <= 0:
        return []
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed: list[str] = []
    folder = data_dir / module
    if not folder.is_dir():
        return removed
    for sub in (folder, folder / "raw"):
        for path in sub.glob("*"):
            if path.is_file() and len(path.stem) == 10 and path.stem < cutoff:
                try:
                    path.unlink()
                    removed.append(str(path.relative_to(data_dir)))
                except OSError:
                    pass
    return removed
