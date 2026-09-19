"""诊断：某个仓库为什么没写出 AI 中文介绍。

用法（项目根目录）：

    .venv/bin/python tools/diag_summary.py                 # 列出最新一轮全部仓库的 AI 状态
    .venv/bin/python tools/diag_summary.py DeskcommCRM     # 只看匹配的仓库，并打印模型原始回复
    .venv/bin/python tools/diag_summary.py DeskcommCRM --live   # 真发一次请求验证能否救回来（花钱）

**默认完全复用采集时的同一套缓存，零网络、零成本**：

  - 代码上下文读 `data/<模块>/.repo_ctx.json`
  - 模型回复读 `data/.llm_cache.json`

看到的就是"当时实际拿到的回复" —— 这是排查"这一句为什么没生成"最关键的一手证据，
比重新发一次请求更有价值（重发可能就正常了，反而看不出问题）。

能分辨三类原因：

  1. 正文非空但 JSON 不合法      → 被 `finish_reason=length` 截断（`chat_json` 会自动加预算重来）
  2. 正文为空 + length + 思考吃满 → `max_tokens` 太小，思考把预算吃光了
  3. JSON 合法但缺 required 字段  → 模型没按格式输出

第 1 类正是实测踩到的：根目录结构最长（666 字符）的那个仓库整段介绍没生成，
页面上只是少一个 `.ai-box`，日志里只有一行「生成 9/10」—— 不查不知道是谁。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.core import config as conf                     # noqa: E402
from radar.core.llm import LLM, _strip_fence              # noqa: E402
from radar.modules.github_trending import module as M     # noqa: E402
from radar.modules.github_trending import sources as S    # noqa: E402

MODULE = "github_trending"
REQUIRED = ("tagline", "detail")


def latest_items(data_dir: Path) -> tuple[str, list[dict]]:
    """取最新一轮的原始落盘（raw/ 里的信息最全）。"""
    files = sorted((data_dir / MODULE / "raw").glob("*.json"))
    if not files:
        raise SystemExit(f"没找到快照：{data_dir / MODULE / 'raw'}/*.json")
    path = files[-1]
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise SystemExit(f"{path} 里没有 items 列表")
    return path.name, items


def attach_ctx(items: list[dict], data_dir: Path) -> int:
    """从上下文缓存里把 README / 根目录补回内存，模拟采集时的 _readme / _tree。"""
    cache = S.RepoContextCache(data_dir / MODULE / ".repo_ctx.json")
    got = 0
    for it in items:
        c = cache.get(it["full_name"], it.get("pushed_at"))
        c = c or {}
        it["_readme"] = c.get("readme") or ""
        it["_tree"] = S.format_tree(c.get("entries") or [])
        if it["_readme"] or it["_tree"]:
            got += 1
    return got


def explain(raw: str) -> str:
    """把一段模型回复归类成"为什么不能用"。"""
    if not raw:
        return "正文为空 → 预算被『思考』吃光了（finish_reason=length），调大 max_tokens"
    try:
        parsed = json.loads(_strip_fence(raw))
    except json.JSONDecodeError as exc:
        return (f"JSON 不合法（{exc.msg} @ 位置 {exc.pos}）→ 大概率被截断，"
                f"结尾片段：{raw[-50:]!r}")
    if isinstance(parsed, dict) and any(
            isinstance(v, dict) and any(k in v for k in REQUIRED)
            for v in parsed.values()):
        return "字段被包在了外层信封里（如 type/content）→ _unwrap_envelope 已兼容"
    return "JSON 合法但缺 required 字段 → 模型没按格式输出"


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    live = "--live" in sys.argv
    keyword = args[0] if args else ""

    cfg = conf.load(ROOT)
    data_dir = cfg.data_dir
    snapshot, items = latest_items(data_dir)
    n_ctx = attach_ctx(items, data_dir)

    print(f"快照 {snapshot} · {len(items)} 个仓库 · 上下文命中 {n_ctx}/{len(items)}")
    print()

    matched = [it for it in items
               if not keyword or keyword.lower() in it["full_name"].lower()]
    if not matched:
        print(f"没有匹配 {keyword!r} 的仓库")
        return 1

    llm_cfg = dict(cfg.section("llm") or {})
    scfg = cfg.module_cfg(MODULE).get("summarize") or {}
    per_tokens = int(scfg.get("max_tokens", 2000))
    llm = LLM(llm_cfg, cache_path=data_dir / ".llm_cache.json")
    print(f"模型 {llm.provider}/{llm.model} · 预算 {per_tokens} · "
          f"可用 {llm.available} {llm.why_unavailable() or ''}".rstrip())
    print()

    for it in matched:
        has_ai = bool(it.get("ai_tagline") or it.get("ai_detail"))
        card = M._build_card(it)
        print(f"[{it['rank']:>2}] {it['full_name']}")
        print(f"     AI 介绍：{'✅ 有' if has_ai else '❌ 没有'}"
              f" · 资料卡 {len(card)} 字符 · 根目录 {len(it.get('_tree') or '')} 字符"
              f" · README {len(it.get('_readme') or '')} 字符")
        if has_ai and not keyword:
            continue

        system = M._SUMMARY_SYSTEM
        user = M._SUMMARY_USER.format(card=card)
        if live:
            # 走 chat() 而不是 chat_json()：要看的就是"原始回复长什么样"
            raw = llm.chat(system, user, json_mode=True, max_tokens=per_tokens,
                           use_cache=False)
            print(f"    [--live 重发一次，预算 {per_tokens}]")
        else:
            raw = llm.chat(system, user, json_mode=True, max_tokens=per_tokens)
            print(f"    [读缓存]")
        print(f"    原始回复 {len(raw)} 字符：{raw[:400]!r}")
        parsed = LLM._parse_json(raw, REQUIRED)
        if parsed:
            wrapped = '"type"' in raw[:60]
            print(f"    ✅ 可解析{'（回复里带了外层信封，已自动剥掉）' if wrapped else ''}："
                  f"{json.dumps(parsed, ensure_ascii=False)[:200]}")
            print()
            continue
        print(f"    ⚠️ {explain(raw)}")
        if live and raw:
            bigger = min(per_tokens * 4, llm.max_retry_tokens)
            print(f"    试试预算 {bigger} + 重试提示 …")
            raw2 = llm.chat(system, user + LLM._json_hint(REQUIRED),
                            json_mode=True, max_tokens=bigger, use_cache=False)
            ok = LLM._parse_json(raw2, REQUIRED)
            print(f"    结果 {len(raw2)} 字符 · 解析{'成功 ✅' if ok else '仍失败 ❌'}："
                  f"{json.dumps(ok, ensure_ascii=False)[:200] if ok else raw2[-60:]!r}")
        print()

    if not live:
        print("（以上为缓存内容，未联网、未花钱；用 --live 才会真发请求）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
