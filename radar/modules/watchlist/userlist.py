"""自选股名单：落盘、读取、把用户输入解析成 secid。

**名单存在服务器侧**（`data/watchlist.json`）—— 这跟"我的规划"（只存手机）是
两个刻意的不同选择：

    规划    只有你自己看，服务端不需要知道 → 存手机，服务端零痕迹
    自选股  每天的大盘简报、新闻筛选、榜单联动都要用它，这些**全在服务器侧生成**
            → 存服务器；存手机的话每个消费方都得自己再问一遍手机

`data/` 在 `.gitignore` 里，所以名单不会跟着仓库公开出去 —— 但它**不是秘密**，
别往里塞任何敏感东西。

文件形态（故意扁平，人手改也不容易改坏）：

    {
      "updated_at": "2026-09-19T22:10:00",
      "items": [{"secid": "1.600519", "code": "600519", "name": "贵州茅台",
                 "note": "", "added_at": "2026-09-19T22:10:00"}]
    }
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

sources = None          # 由 module.py / web.py 注入（见 load_sources()）

MAX_ITEMS = 60          # 上限：再多页面就成噪音了，而且一次请求也变长
NAME_MAX = 24
NOTE_MAX = 40

FILE_NAME = "watchlist.json"

# ⚠️ secid 会被**拼进 URL 的 query**，所以必须严格白名单校验。
#    用户输入是不可信来源（这层将来也可能被别的调用方用），
#    不校验就等于把 URL 注入的口子留在那儿。
_RE_SECID = re.compile(r"^\d{1,3}\.[A-Za-z0-9._\-]{1,16}$")


def load_sources():
    """延迟加载同目录的 sources.py。

    用**自己的 `__file__`** 去算同级路径（不是绕 module.py）——
    `load_sibling` 的缓存键是"模块目录名 + 文件名"，从哪个文件进来都拿到同一个实例，
    但绕 module.py 会让人以为存在循环依赖。延迟加载是因为 sources 只在
    `resolve()` 里用得上，没必要让每次 `load()` 都拖它进来。
    """
    global sources
    if sources is None:
        from radar.core.module import load_sibling
        sources = load_sibling(__file__, "sources")
    return sources


def path(data_dir) -> Path:
    return Path(data_dir) / FILE_NAME


def _clean_text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\n", " ").replace("\r", " ").strip()[:limit]


def _norm_item(raw) -> dict | None:
    """把一条记录规整成可信的形状；不可信就丢（返回 None）。"""
    if not isinstance(raw, dict):
        return None
    secid = str(raw.get("secid") or "").strip()
    if not _RE_SECID.match(secid):
        return None
    code = _clean_text(raw.get("code"), 16) or secid.split(".", 1)[1]
    return {
        "secid": secid,
        "code": code,
        "name": _clean_text(raw.get("name"), NAME_MAX),
        "note": _clean_text(raw.get("note"), NOTE_MAX),
        "added_at": _clean_text(raw.get("added_at"), 32),
    }


def load(data_dir) -> dict:
    """读名单。文件不存在 / 坏了 / 被手改乱 → 一律当空名单，**不抛异常**。

    理由和整站的容错口径一致：名单是"输入"，它坏了不该让页面打不开；
    页面空着 + 明确提示"去加两只" 比 500 有用得多。
    """
    p = path(data_dir)
    if not p.is_file():
        return {"updated_at": None, "items": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"updated_at": None, "items": []}
    raw_items = data.get("items") if isinstance(data, dict) else None
    items: list[dict] = []
    seen: set[str] = set()
    for raw in (raw_items or []):
        item = _norm_item(raw)
        if item and item["secid"] not in seen:
            seen.add(item["secid"])
            items.append(item)
        if len(items) >= MAX_ITEMS:
            break
    return {"updated_at": (data.get("updated_at") if isinstance(data, dict) else None),
            "items": items}


def save(data_dir, items) -> dict:
    """整份覆盖写入。返回真正落盘的内容（**去重 + 截断之后的**）。

    返回值很重要：调用方要拿它回显给用户，让"我加进去的"和"真存下的"一致 ——
    静默丢弃多余的条目是这类功能最让人不信任的地方。
    """
    clean: list[dict] = []
    seen: set[str] = set()
    now = datetime.now().isoformat(timespec="seconds")
    for raw in (items or []):
        item = _norm_item(raw)
        if not item or item["secid"] in seen:
            continue
        seen.add(item["secid"])
        item["added_at"] = item["added_at"] or now
        clean.append(item)
        if len(clean) >= MAX_ITEMS:
            break
    payload = {"updated_at": now, "items": clean}
    p = path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再 rename：中途死掉也不会留下半截 JSON
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return payload


# ------------------------------------------------------------------ 输入解析
_SPLIT = re.compile(r"[,，;；、\s]+")


def resolve(http, text: str) -> dict:
    """把用户敲的一串东西解析成 items。

    支持混着写，例如：`600519, 茅台, hk00700, sh000001`

    返回：
        {
          "items":      [规整后的条目...],
          "unresolved": ["认不出来的原词"...],
          "ambiguous":  [{"token": "...", "candidates": [{name, code, secid}, ...]}],
          "error":      "搜索接口不可用时的说明" | None,
        }

    ⭐ **不猜**：
      * 认不出代码形状、搜索又没结果 → 进 `unresolved`，**不硬塞**
      * 搜索出多个候选、且没有一个名字**完全等于**用户敲的词 → 进 `ambiguous`，
        **不由我们替用户挑**（挑错了比让他再敲一次烦得多）
      * 搜索接口本身挂了 → `error` 里如实说，已解析出来的照常返回
    """
    src = load_sources()
    tokens = [t for t in _SPLIT.split((text or "").strip()) if t]
    items: list[dict] = []
    unresolved: list[str] = []
    ambiguous: list[dict] = []
    search_error: str | None = None

    for token in tokens:
        secid = src.guess_secid(token) if src.looks_like_code(token) else None
        if secid:
            items.append({"secid": secid, "code": secid.split(".", 1)[1], "name": ""})
            continue

        try:
            hits = src.search(http, token)
        except Exception as exc:  # noqa: BLE001
            # 搜索不可用时，纯代码仍然照常工作 —— 降级要说出来，不能装作没发生
            search_error = f"{type(exc).__name__}: {exc}"
            hits = []

        if not hits:
            unresolved.append(token)
            continue

        # 名字**完全相等**是最强信号；其次，**代码完全相等**同样强 ——
        # `00700` 这类 5 位数字会被东财同时搜出 00700(腾讯控股) / 000700(模塑科技)
        # / 300700 / 600700，光看"有多个候选"就判歧义的话，会把用户压根没改过的
        # 那一行丢掉（我们自己的表单回填的就是 `00700`，于是"点保存 = 少一只"）。
        # 拿代码相等作为判据不是"猜"：它命中的那条候选，代码就是用户敲的那串。
        key = token.strip()
        exact = [h for h in hits if (h.get("name") or "").strip() == key]
        if not exact:
            exact = [h for h in hits if str(h.get("code") or "").strip() == key]
        if exact:
            pick = exact[0]
        elif len(hits) == 1:
            pick = hits[0]
        else:
            ambiguous.append({
                "token": token,
                "candidates": [
                    {"name": h["name"], "code": h["code"], "secid": h["secid"],
                     "type": h["type"]}
                    for h in hits[:4]
                ],
            })
            continue
        items.append({"secid": pick["secid"], "code": pick["code"],
                      "name": pick["name"]})

    # 去重（同一只股票写两遍很常见）
    out: list[dict] = []
    seen: set[str] = set()
    for item in items:
        if item["secid"] in seen:
            continue
        seen.add(item["secid"])
        out.append(item)

    return {"items": out, "unresolved": unresolved, "ambiguous": ambiguous,
            "error": search_error}


def fill_names(data_dir, quotes_by_secid: dict[str, str]) -> dict:
    """把行情里拿到的**权威名称**写回名单。

    为什么值得单独做：用户存下"600519"时名字可能是空的（他没敲名字），
    而页面、简报、通知都要显示名字。行情回来时顺手补上，之后即使某天
    行情挂了，名单里也已经有名字可显示。
    """
    cur = load(data_dir)
    changed = False
    for item in cur["items"]:
        name = quotes_by_secid.get(item["secid"])
        if name and name != item["name"]:
            item["name"] = _clean_text(name, NAME_MAX)
            changed = True
    if not changed:
        return cur
    # 只有真变了才写盘：每天的采集都会走这里，别做无谓的写
    payload = save(data_dir, cur["items"])
    return payload
