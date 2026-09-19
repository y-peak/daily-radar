"""静态站点渲染。

页面在流水线里一次性生成好，线上服务只发文件。这么做换来三件事：

    * 页面秒开，**后端进程挂了历史报告照样能看**
    * 每天的报告天然归档成一个静态文件，历史可回溯、可 diff
    * 线上服务极小 —— 只需发文件 + 提供"有没有更新"的口子

模板分两层：
    templates/base.html            页面外壳（导航 / 页头 / 页脚）
    modules/<name>/template.html   只写正文，套壳由核心负责

模块不提供模板时，用 templates/module_generic.html 兜底（通用键值表格）。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import charts, md
from .config import Config
from .module import Module
from .store import (build_index, fingerprint, module_dates, read_json,
                    read_text, save_index)


def make_env(cfg: Config) -> Environment:
    env = Environment(
        loader=FileSystemLoader([str(cfg.template_dir)]),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["md"] = md.to_html
    # 服务端内联 SVG 小图表（见 core/charts.py）。做成过滤器而不是全局函数，
    # 是为了在模板里写成 `{{ 序列 | spark }}`，读起来就是"把这个序列画成图"。
    env.filters["spark"] = charts.spark
    env.filters["bars"] = charts.bars
    env.filters["hbar"] = charts.hbar
    return env


def _module_context(cfg: Config, app: dict, module, dates: list[str], date: str,
                    data, report_text: str) -> dict:
    return {
        "app": app,
        "mod": module,
        "module": {
            "name": module.name,
            "title": module.title,
            "subtitle": module.subtitle,
            "schedule": module.schedule,
        },
        "date": date,
        "dates": dates,
        "is_latest": bool(dates) and date == dates[0],
        "data": data,
        "report": report_text,
        "report_html": md.to_html(report_text),
    }


def _render_body(env: Environment, cfg: Config, ctx: dict) -> str:
    """模块自带 template.html 就用它，否则用通用兜底模板。"""
    tpl_file = cfg.modules_dir / ctx["mod"].name / "template.html"
    if tpl_file.exists():
        return env.from_string(tpl_file.read_text(encoding="utf-8")).render(**ctx)
    return env.get_template("module_generic.html").render(**ctx)


def build_site(cfg: Config, app: dict, modules: list, log=print, index: dict | None = None) -> dict:
    """重建整个静态站点。返回统计信息。

    索引在这里**现算**而不是读盘上的旧文件：读旧文件会让首页的"累计天数"和
    "最新日期"永远慢一拍（今天的数据已经写好了，页面却还按昨天的索引渲染）。
    """
    env = make_env(cfg)
    public = cfg.public_dir
    data_dir = cfg.data_dir
    if index is None:
        index = build_index(data_dir, [m.meta() for m in modules])

    if public.exists():
        shutil.rmtree(public, ignore_errors=True)
    public.mkdir(parents=True, exist_ok=True)

    # 静态资源
    if cfg.static_dir.is_dir():
        shutil.copytree(cfg.static_dir, public / "static")

    # Service Worker 必须落在**站点根目录**，不能待在 /static/ 下面。
    #
    # 原因：SW 的默认作用域是"脚本所在目录及其子目录"。放在 /static/sw.js
    # 就只能管住 /static/**，管不到首页 —— 而可安装性判定要求 SW 的作用域
    # 覆盖 start_url。放根目录（/sw.js）作用域就是全站，一行注册即可生效。
    sw_src = cfg.static_dir / "sw.js"
    if sw_src.is_file():
        shutil.copy2(sw_src, public / "sw.js")

    nav = [m.meta() for m in modules]
    written = 0

    # 静态资产的缓存版本号：跟着数据指纹走，数据一变资源 URL 就变 → 手机不会拿到旧缓存
    asset_ver = fingerprint(index, data_dir)
    _now = datetime.now()
    built_at = _now.strftime("%Y-%m-%d %H:%M")
    # today 给总览页判断"模块是不是今天更新的"。用构建时间而不是浏览器时间：
    # 页面是静态快照，两者本来就可能差一整天，用构建时间去比才自洽。
    today = _now.strftime("%Y-%m-%d")
    shell = {"app": app, "nav": nav, "asset_ver": asset_ver, "built_at": built_at,
             "today": today, "fingerprint": asset_ver}
    base_tpl = env.get_template("base.html")

    for module in modules:
        dates = module_dates(data_dir, module.name)
        out_dir = public / module.name
        out_dir.mkdir(parents=True, exist_ok=True)
        if not dates:
            body = env.get_template("module_generic.html").render(
                **_module_context(cfg, app, module, [], "", None, "")
            )
            (out_dir / "index.html").write_text(
                base_tpl.render(**shell, active=module.name, body=body,
                                module={"name": module.name},
                                dates=[], date=None, title=module.title,
                                is_latest=False),
                encoding="utf-8")
            written += 1
            continue

        for date in dates:
            data = read_json(data_dir / module.name / f"{date}.json")
            report_text = read_text(data_dir / module.name / f"{date}.md")
            page_ctx = _module_context(cfg, app, module, dates, date, data, report_text)
            body = _render_body(env, cfg, page_ctx)
            # dates 必须传下去：base.html 靠它决定要不要画日期切换条
            html = base_tpl.render(
                **shell, active=module.name, body=body,
                module={"name": module.name},
                dates=dates, date=date, title=module.title,
                is_latest=(date == dates[0]))
            (out_dir / f"{date}.html").write_text(html, encoding="utf-8")
            written += 1
            if date == dates[0]:
                (out_dir / "index.html").write_text(html, encoding="utf-8")

    # ---- 多页模块的额外产出（见 core/module.py::extra_pages）----
    #
    # 核心只管"一个模块一个页面"（public/<name>/）。像阅读器这种"书房 → 单本书 →
    # 章节"的多层结构，由模块自己通过 extra_pages 钩子给出页面，核心负责落盘。
    # 为什么不让模块自己往 public/ 写：构建时会整体 rmtree(public)，自己写会被清掉。
    #
    # 单个模块出错只记日志、不改整站结果 —— 和 registry 的容错口径一致：
    # 一个坏模块不该把整站构建搞崩。
    extra = 0
    for module in modules:
        if type(module).extra_pages is Module.extra_pages:
            continue                       # 没实现（还是基类那个）就跳过
        mod_dates = module_dates(data_dir, module.name)

        def _wrap(body, _m=module, **kw):
            """把正文套进 base.html —— 模块无需知道外壳有哪些变量。"""
            base_kw = dict(active=_m.name, title=_m.title, dates=[], date=None,
                           is_latest=False, module={"name": _m.name})
            base_kw.update(kw)
            return base_tpl.render(**shell, body=body, **base_kw)

        page_ctx = {
            "env": env, "shell": shell, "index": index, "cfg": cfg,
            "data_dir": data_dir, "public_dir": public,
            "module": module.meta(), "dates": mod_dates,
            "wrap": _wrap, "log": log,
        }
        try:
            pages = module.extra_pages(page_ctx) or []
        except Exception as exc:  # noqa: BLE001
            log(f"  [warn] 模块 {module.name} 的额外页面生成失败："
                f"{type(exc).__name__}: {exc}")
            continue

        root = public.resolve()
        for rel, html in pages:
            dest = (public / rel).resolve()
            # 防越界：模块给的相对路径必须落在 public/ 内
            if root not in dest.parents:
                log(f"  [warn] {module.name} 试图写到 public 之外，已忽略：{rel}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(html, encoding="utf-8")
            extra += 1
            written += 1

    # 首页：正文是 index.html 模板，一样要套 base.html 外壳
    # （否则没有 <head>/CSS/导航，页面是裸的）
    home_body = env.get_template("index.html").render(**shell, index=index)
    (public / "index.html").write_text(
        base_tpl.render(**shell, active=None, body=home_body, module=None,
                        dates=[], date=None, title=None, is_latest=False),
        encoding="utf-8")
    written += 1

    # PWA 清单：手机上"添加到主屏幕"用。
    #
    # ⚠️ icons 不能是空数组。Chrome/Edge 判定"能不能安装"的硬条件里就包含
    #    「至少有一个 192x192 和一个 512x512 图标」—— 之前这里写的是 []，
    #    看着像 PWA，浏览器其实根本不给安装入口，装不成 app。
    #    maskable 那两份是给 Android 自适应图标用的（会被裁成圆形/方形）。
    (public / "manifest.json").write_text(json.dumps({
        "id": "/",
        "name": app["name"],
        "short_name": app["short_name"],
        "description": f"{app['name']} —— 每日热榜、资金流与离线书架",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "display_override": ["standalone", "minimal-ui"],
        "orientation": "portrait-primary",
        "lang": "zh-CN",
        "dir": "ltr",
        "background_color": "#f6f7f9",
        "theme_color": "#1f6feb",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-maskable-192.png", "sizes": "192x192",
             "type": "image/png", "purpose": "maskable"},
            {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "maskable"},
        ],
        # 长按图标直接跳模块，少点两下
        "shortcuts": [
            {"name": m["title"], "url": f"/{m['name']}/"} for m in nav
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    # 个人站不该被搜索引擎收录（公网 IP 被扫到会进索引）
    (public / "robots.txt").write_text(
        "User-agent: *\nDisallow: /\n", encoding="utf-8")

    # 索引跟站点一起落盘 —— 保证"页面里显示的"和"/api/status 报的"是同一份
    save_index(data_dir, index)

    log(f"  站点已生成：{written} 个页面 → {public}"
        + (f"（其中 {extra} 个来自模块的多页产出）" if extra else ""))
    return {"pages": written, "extra_pages": extra, "public_dir": str(public),
            "fingerprint": asset_ver}
