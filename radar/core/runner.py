"""流水线编排：把四个阶段串起来，并记录运行历史。

一条重要原则：**任一模块失败都不影响其他模块**。
每天定时跑的场景里，一个数据源挂了不该让整站停摆 ——
所以每个模块单独 try/except，状态分别记录。

运行结果写入 `data/runs.json`，`/api/status` 会读它，
前端据此显示"上次跑成了没 / 数据是哪天的"。
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime
from typing import Callable

from . import store
from .config import Config
from .module import Context, Module
from .render import build_site


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _describe(payload) -> str:
    """给日志一句人话的体量描述。"""
    if isinstance(payload, list):
        return f"{len(payload)} 条"
    if isinstance(payload, dict):
        return f"{len(payload)} 个字段"
    if payload is None:
        return "无数据"
    return type(payload).__name__


def run_module(module: Module, ctx: Context, log: Callable[[str], None]) -> dict:
    """跑完一个模块的四个阶段。返回该模块的运行记录。"""
    name = module.name
    started = time.time()
    record = {
        "name": name,
        "title": module.title,
        "status": "ok",
        "error": "",
        "date": ctx.date,
        "phases": {},
    }

    try:
        log(f"  [{module.title}] ① collect 拉取原始数据…")
        t = time.time()
        raw = module.collect(ctx)
        record["phases"]["collect"] = round(time.time() - t, 1)
        log(f"       ✓ {_describe(raw)}（{record['phases']['collect']}s）")
        if not ctx.dry_run:
            store.write_json(ctx.raw_path(name), raw)

        log(f"  [{module.title}] ② analyze 加工…")
        t = time.time()
        data = module.analyze(raw, ctx)
        record["phases"]["analyze"] = round(time.time() - t, 1)
        if not ctx.dry_run:
            store.write_json(ctx.data_path(name), data)

        log(f"  [{module.title}] ③ report 生成结论…")
        t = time.time()
        report_text = module.report(data, ctx) or ""
        record["phases"]["report"] = round(time.time() - t, 1)
        if not ctx.dry_run and report_text:
            store.write_text(ctx.data_path(name, "md"), report_text)
            log(f"       ✓ 结论 {len(report_text)} 字")

        record["data_date"] = ctx.date

    except Exception as exc:  # noqa: BLE001
        record["status"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"
        log(f"       ✗ 失败：{record['error']}")
        if ctx.log_traceback:
            log(traceback.format_exc())

    record["elapsed"] = round(time.time() - started, 1)
    record["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return record


def run_all(cfg: Config, app: dict, modules: list[Module], ctx: Context,
            log: Callable[[str], None] = print, skip_site: bool = False,
            all_modules: list[Module] | None = None) -> dict:
    """按顺序跑所有模块，然后重建站点与索引。

    `modules` 是**这一轮要采集的**模块；`all_modules` 是**站点里应该存在的**
    全部模块（默认同 `modules`）。

    为什么要分开：`python -m radar run github_trending` 只想重跑一个模块，
    但站点重建是"清空 public 再整站生成"的。如果只拿 `modules` 去渲染，
    其它模块的页面会被连带删光 —— 页面上就凭空少了一个 Tab。
    """
    started = time.time()
    log(f"{'=' * 58}")
    log(f" {app['name']} · {ctx.date}{'（dry-run）' if ctx.dry_run else ''}")
    log(f"{'=' * 58}")

    records = []
    for module in modules:
        module_cfg = cfg.module_cfg(module.name)
        if module_cfg.get("enabled", True) is False:
            log(f"  [{module.title}] 已禁用，跳过")
            records.append({"name": module.name, "title": module.title,
                            "status": "disabled", "date": ctx.date,
                            "elapsed": 0, "phases": {}})
            continue
        records.append(run_module(module, ctx, log))

    ok = sum(1 for r in records if r["status"] == "ok")
    failed = sum(1 for r in records if r["status"] == "failed")

    site_modules = all_modules if all_modules is not None else modules
    index = store.build_index(cfg.data_dir, [m.meta() for m in site_modules])
    site = {"pages": 0}
    if not ctx.dry_run and not skip_site:
        log("  ④ render 重建站点…")
        # 索引由 build_site 现算并落盘，避免"页面用新数据、索引还是旧的"
        site = build_site(cfg, app, site_modules, log=log, index=index)

    if not ctx.dry_run:
        pruned: list[str] = []
        keep = cfg.section("storage").get("keep_days", 400)
        for module in site_modules:
            pruned += store.prune_old(cfg.data_dir, module.name, keep)
        if pruned:
            log(f"  清理过期数据 {len(pruned)} 个文件")

    summary = {
        "date": ctx.date,
        "started_at": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
        "elapsed": round(time.time() - started, 1),
        "ok": ok,
        "failed": failed,
        "dry_run": ctx.dry_run,
        "modules": records,
        "site": site,
    }
    if not ctx.dry_run:
        store.record_run(cfg.data_dir, summary)

    log(f"{'-' * 58}")
    log(f" 完成：成功 {ok} / 失败 {failed}，用时 {summary['elapsed']}s")
    return summary
