"""命令行入口：`python3 -m radar <子命令>`。

设计上刻意让每个阶段都能单独跑（run / build / serve / status），
因为排查问题时最想要的是"只重跑报告、不重新抓数据"这类操作，
而不是每次都从头来一遍。

    python3 -m radar run all          # 全量采集 + 重建站点
    python3 -m radar run market_flow  # 只跑一个模块
    python3 -m radar build            # 只重建站点（不抓数据）
    python3 -m radar serve            # 起 Web 服务
    python3 -m radar status           # 看当前数据状况
    python3 -m radar list             # 列出发现的模块
    python3 -m radar new <name>       # 生成一个新模块骨架
    python3 -m radar clean            # 按 keep_days 清理过期数据
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from . import __version__
from .core import registry, store
from .core.config import Config, load
from .core.http import HttpClient
from .core.module import Context
from .core.render import build_site
from .core.runner import run_all, today

# --------------------------------------------------------------- 新模块骨架
MODULE_TEMPLATE = '''"""{title} 模块。

只实现了 collect()，其余三个阶段用基类默认（analyze 原样透传、report 不产出）。
跑通之后再按需补 analyze / report —— "先能跑，再丰富"。
"""

from __future__ import annotations

import json
from typing import Any

from radar.core.module import Context, Module


class {cls}(Module):
    name = "{name}"
    title = "{title}"
    subtitle = "{subtitle}"
    order = {order}
    schedule = "每日 08:00"

    def collect(self, ctx: Context) -> Any:
        # 用 ctx.http 拿数据，它已经带了限速、重试和编码探测
        text = ctx.http.get("https://example.com/data.json")
        return json.loads(text)

    def analyze(self, raw: Any, ctx: Context) -> Any:
        items = raw if isinstance(raw, list) else []
        return {{"count": len(items), "items": items[:20]}}

    def report(self, data: Any, ctx: Context) -> str:
        lines = [f"# {self.title} · {{ctx.date}}", ""]
        lines.append(f"- 共 {{data.get('count', 0)}} 条")
        return "\\n".join(lines)
'''

TEMPLATE_HTML = '''{% if not dates %}
<div class="empty"><p>{{ module.title }} 还没有数据。</p></div>
{% else %}

<section class="panel">
  <div class="panel-head"><h2>概览</h2>
    <span class="badge ok">{{ data.count }} 条</span></div>
  <ul class="rank">
    {% for it in data.items %}
    <li>
      <span class="rank-no">{{ loop.index }}</span>
      <div class="rank-body">
        <div class="rank-title"><strong>{{ it }}</strong></div>
      </div>
    </li>
    {% endfor %}
  </ul>
</section>

{% if report_html %}
<section class="panel">
  <h2>结论</h2>
  <div class="prose">{{ report_html | safe }}</div>
</section>
{% endif %}

{% endif %}
'''


def _log(msg: str) -> None:
    print(msg, flush=True)


def _make_ctx(cfg: Config, date: str, dry_run: bool = False,
              traceback_: bool = False) -> Context:
    return Context(
        config=cfg,
        date=date,
        data_dir=cfg.data_dir,
        public_dir=cfg.public_dir,
        http=HttpClient(cfg.section("http"), log=_log),
        log=_log,
        dry_run=dry_run,
        log_traceback=traceback_,
    )


def _select(modules: list, wanted: str) -> list:
    if not wanted or wanted == "all":
        return modules
    names = [n.strip() for n in wanted.split(",") if n.strip()]
    picked = [m for m in modules if m.name in names]
    missing = [n for n in names if n not in {m.name for m in picked}]
    if missing:
        _log(f"  [warn] 没找到模块：{', '.join(missing)}")
    return picked


# ------------------------------------------------------------------ 子命令
def cmd_run(args) -> int:
    cfg = load(args.root)
    # 全部模块：站点重建要用它，否则单跑一个模块会把别的模块页面删掉
    everything = registry.ordered(registry.discover(cfg.modules_dir))
    modules = _select(everything, args.module)
    if not modules:
        _log("没有可运行的模块。先 `python3 -m radar list` 看看。")
        return 1
    date = args.date or today()
    ctx = _make_ctx(cfg, date, args.dry_run, args.traceback)
    summary = run_all(cfg, cfg.section("app"), modules, ctx, log=_log,
                      skip_site=args.no_site, all_modules=everything)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 2


def cmd_build(args) -> int:
    cfg = load(args.root)
    modules = registry.ordered(registry.discover(cfg.modules_dir))
    build_site(cfg, cfg.section("app"), modules, log=_log)
    return 0


def cmd_serve(args) -> int:
    from .web import main as web_main
    web_main(args.root, port=args.port, host=args.host)
    return 0


def cmd_status(args) -> int:
    cfg = load(args.root)
    modules = registry.ordered(registry.discover(cfg.modules_dir))
    index = store.load_index(cfg.data_dir)
    by_name = {m["name"]: m for m in index.get("modules", [])}

    print(f"{cfg.section('app').get('name')} v{__version__}  ·  根目录 {cfg.root}")
    print(f"指纹  {store.fingerprint(index, cfg.data_dir)}")
    print("-" * 62)
    print(f"{'模块':<22}{'状态':<8}{'最新数据':<13}{'累计':<6}")
    for m in modules:
        cfg_on = cfg.module_cfg(m.name).get("enabled", True)
        info = by_name.get(m.name, {})
        flag = "开" if cfg_on else "关"
        latest = info.get("latest") or "—"
        days = info.get("days", 0)
        print(f"{m.name:<22}{flag:<8}{latest:<13}{days:<6}")
    runs = store.load_runs(cfg.data_dir, limit=3)
    if runs:
        print("-" * 62)
        print("最近运行：")
        for r in runs:
            print(f"  {r.get('started_at')}  成功 {r.get('ok')} / 失败 {r.get('failed')}"
                  f"  {r.get('elapsed')}s"
                  + ("  (dry-run)" if r.get("dry_run") else ""))
    else:
        print("还没有运行记录。")
    return 0


def cmd_list(args) -> int:
    cfg = load(args.root)
    found = registry.discover(cfg.modules_dir)
    if not found:
        _log(f"在 {cfg.modules_dir} 下没有发现模块。")
        return 1
    print(f"发现 {len(found)} 个模块（{cfg.modules_dir}）：")
    for m in registry.ordered(found):
        tpl = "有" if (cfg.modules_dir / m.name / "template.html").exists() else "通用"
        print(f"  {m.name:<22} {m.title:<14} order={m.order:<4} 模板={tpl}")
    return 0


def cmd_new(args) -> int:
    cfg = load(args.root)
    name = args.name
    folder = cfg.modules_dir / name
    if folder.exists():
        _log(f"目录已存在：{folder}")
        return 1
    folder.mkdir(parents=True)
    cls = "".join(part.capitalize() for part in name.split("_"))
    (folder / "module.py").write_text(
        MODULE_TEMPLATE.format(name=name, cls=cls, title=args.title or name,
                               subtitle=args.subtitle or "", order=args.order),
        encoding="utf-8")
    (folder / "template.html").write_text(TEMPLATE_HTML, encoding="utf-8")

    _log(f"已生成模块骨架：{folder}")
    _log("  改 module.py 里的 collect()，然后 `python3 -m radar run " + name + "`")
    return 0


def cmd_clean(args) -> int:
    cfg = load(args.root)
    modules = registry.ordered(registry.discover(cfg.modules_dir))
    keep = args.keep or cfg.section("storage").get("keep_days", 400)
    total = []
    for m in modules:
        total += store.prune_old(cfg.data_dir, m.name, keep)
    if total:
        _log(f"清理了 {len(total)} 个文件（保留最近 {keep} 天）：")
        for p in total:
            _log(f"  - {p}")
    else:
        _log(f"没有需要清理的文件（保留最近 {keep} 天）。")
    return 0


# --------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="radar", description="个人情报台")
    p.add_argument("--root", type=Path, default=None, help="项目根目录（含 config.json）")
    p.add_argument("--version", action="version", version=f"radar {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="采集数据并重建站点")
    r.add_argument("module", nargs="?", default="all", help="模块名，逗号分隔；默认 all")
    r.add_argument("--date", default=None, help="覆盖运行日期，形如 2026-09-13")
    r.add_argument("--dry-run", action="store_true", help="只跑不落盘")
    r.add_argument("--no-site", action="store_true", help="不重建站点")
    r.add_argument("--json", action="store_true", help="额外打印 JSON 摘要")
    r.add_argument("--traceback", action="store_true", help="失败时打印完整堆栈")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("build", help="只重建站点")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("serve", help="启动 Web 服务")
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--host", default=None, help="默认 0.0.0.0；本地预览用 127.0.0.1")
    s.set_defaults(func=cmd_serve)

    st = sub.add_parser("status", help="查看数据状况")
    st.set_defaults(func=cmd_status)

    ls = sub.add_parser("list", help="列出模块")
    ls.set_defaults(func=cmd_list)

    n = sub.add_parser("new", help="生成新模块骨架")
    n.add_argument("name", help="模块名（同时是目录名，建议用下划线）")
    n.add_argument("--title", default=None)
    n.add_argument("--subtitle", default=None)
    n.add_argument("--order", type=int, default=100)
    n.set_defaults(func=cmd_new)

    c = sub.add_parser("clean", help="清理过期数据")
    c.add_argument("--keep", type=int, default=None, help="保留天数")
    c.set_defaults(func=cmd_clean)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _log("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
