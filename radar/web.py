"""线上服务：发静态文件 + 两个 API。

为什么是"静态站 + 极小服务"而不是普通的动态 Web 应用：

    * **页面不依赖服务进程** —— 服务挂了/重启中，历史报告照样能打开
    * 服务只负责三件事：发文件、回答"有没有更新"、接受"现在拉一次"
    * 模块再多也不用改这里，服务与模块完全解耦

手机端的"更新口子"由两个接口构成：

    GET  /api/status    当前数据指纹 + 最近一次运行结果 + 是否正在跑
    POST /api/refresh   立刻触发一轮采集（异步执行，立即返回）

前端轮询 /api/status，指纹一变就 reload —— 这就是"监测到更新并自行拉取"。
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from flask import (Flask, abort, g, jsonify, request, send_file,
                   send_from_directory)
from werkzeug.middleware.proxy_fix import ProxyFix

from .core import registry, store
from .core.config import Config, load
from .core.http import HttpClient
from .core.module import Context
from .core.runner import run_all, today

# --------------------------------------------------------------------- 运行态
# 单进程内的互斥：同一时刻只允许一轮采集，避免两个手机同时点刷新把源打挂
_state = {
    "busy": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
}
_lock = threading.Lock()


def _log_to(prefix: str):
    """采集日志打到 stdout，systemd 会收进 journalctl。"""
    def _log(msg: str) -> None:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {prefix}{msg}", flush=True)
    return _log


def _build_context(cfg: Config, date: str) -> Context:
    app = cfg.section("app")
    return Context(
        config=cfg,
        date=date,
        data_dir=cfg.data_dir,
        public_dir=cfg.public_dir,
        http=HttpClient(cfg.section("http"), log=lambda m: print("   ", m, flush=True)),
        log=_log_to("   "),
        dry_run=False,
    )


def _run_pipeline(cfg: Config, modules: list) -> None:
    """在后台线程里跑完整流程。异常一律吞下并记到 _state，别让线程死得无声无息。"""
    log = _log_to("")
    try:
        date = today()
        log(f"开始采集 {date}")
        ctx = _build_context(cfg, date)
        summary = run_all(cfg, cfg.section("app"), modules, ctx, log=log)
        with _lock:
            _state["error"] = None if summary["failed"] == 0 else "部分模块失败"
    except Exception as exc:  # noqa: BLE001
        log(f"采集异常：{type(exc).__name__}: {exc}")
        with _lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        with _lock:
            _state["busy"] = False
            _state["finished_at"] = datetime.now().isoformat(timespec="seconds")


def _safe_join(base: Path, rel: str) -> Path | None:
    """把 URL 路径安全地映射到 base 之下，挡掉 ../ 穿越。"""
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def _guess_version(filename: str) -> str:
    """从 `radar-1.2.apk` 里抠出 `1.2`，抠不到就给空串（宁可空也别瞎猜）。"""
    m = re.match(r"^.*?-(\d[\w.]*)\.apk$", filename)
    return m.group(1) if m else ""


def _apk_version_code(apk: Path) -> int:
    """读 sidecar JSON 里的 versionCode；缺失/损坏一律给 0。

    给 0 是刻意的：调用方用它排序或比较，0 只会让这个包**排在最后/不被提示**，
    绝不会误判成"更新"。**失败方向必须安全**。
    """
    side = apk.with_suffix(".json")
    if not side.is_file():
        return 0
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
        code = data.get("versionCode") if isinstance(data, dict) else None
        return int(code) if isinstance(code, (int, float)) or (
            isinstance(code, str) and code.isdigit()) else 0
    except (OSError, ValueError, TypeError):
        return 0


def _ver_tuple(text: str) -> tuple:
    """把 `1.2` / `v1.2.3` 拆成 `(1, 2)` / `(1, 2, 3)`，只用于**确定性排序兜底**。

    抠不出数字就给空元组（比较时最小）。别拿它当正式版本比较用 ——
    真正的版本比较走 `versionCode`。
    """
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))


def create_app(root: Path | None = None) -> Flask:
    cfg = load(root)
    modules = registry.ordered(registry.discover(cfg.modules_dir))
    # 口令优先取环境变量 RADAR_TOKEN：config.json 要推到公开仓库，
    # 真正的口令只能放在不入库的地方（服务器上是 .env，由 systemd 加载）
    token = (os.environ.get("RADAR_TOKEN")
             or cfg.section("server").get("token") or "").strip()

    app = Flask(__name__, static_folder=None)
    public = cfg.public_dir

    # 生产环境 TLS 由 nginx 终结，它转发过来的请求是明文 HTTP，
    # 真实协议放在 X-Forwarded-Proto 里。不认这个头的话 request.is_secure 永远是
    # False → Cookie 拿不到 Secure 标记。信任 1 跳代理即可 —— radar 只监听
    # 127.0.0.1，外网必须先经过 nginx，伪造不了这个头。
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)

    # ------------------------------------------------------------ 访问口令
    # 端口是对公网开的，所以提供一个可选口令。**关键是让它能在手机上用**：
    # 只用 `?t=` 的话，点导航跳到 /market_flow/ 口令就丢了。
    # 所以在首次带口令访问时种一个 Cookie，之后同一个浏览器一直有效。
    # config.server.token 留空（默认）＝ 不校验。
    @app.before_request
    def _guard():
        if not token:
            return None
        given = (request.args.get("t") or request.headers.get("X-Radar-Token")
                 or request.cookies.get("radar_token") or "")
        if given != token:
            return jsonify({
                "error": "未授权",
                "hint": "首次访问请带口令，如 http://<IP>:<端口>/?t=<口令>，"
                        "之后浏览器会记住。",
            }), 401
        g.persist_token = bool(request.args.get("t")
                               or request.headers.get("X-Radar-Token"))
        return None

    @app.after_request
    def _after(resp):
        if getattr(g, "persist_token", False):
            # ⚠️ 必须标 Secure（当请求本身是 HTTPS）。
            #    这条 Cookie 是"装到主屏后还能免口令"的唯一凭据 —— 因为 PWA 是从
            #    manifest 的 start_url 启动的，地址里**没有** ?t= 参数，全靠 Cookie。
            #    不标 Secure 的话，同一个 Cookie 会跟着明文 HTTP 请求一起发出去，
            #    公网 IP 上等于把口令送人。
            resp.set_cookie("radar_token", token, max_age=31536000,
                            httponly=True, samesite="Lax",
                            secure=request.is_secure)
        # 页面不做强缓存：手机上（尤其"添加到主屏幕"的独立窗口）很容易一直用旧页面，
        # 而数据每天都在变。CSS/JS 已经用 ?v=<指纹> 做了版本号，交给浏览器缓存即可。
        if resp.mimetype == "text/html":
            resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp

    # -------------------------------------------------------------- 页面
    @app.get("/")
    def home():
        if not (public / "index.html").exists():
            return (
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<p style='font:15px system-ui;padding:24px'>站点还没生成。"
                "在服务器上执行：<code>python3 -m radar run all</code></p>",
                200,
            )
        return send_from_directory(public, "index.html")

    # ------------------------------------------------------------ 安卓壳下载
    # `/dl/<name>` 发的是 app/android 构建出来的 APK。
    #
    # ⚠️ 三个刻意的选择：
    #   1. 放在 `public/` **之外**（`<root>/apk/`）。`build_site` 每次都先
    #      rmtree(public) 再整站生成，放里面等于每轮采集都把安装包删掉。
    #   2. 鉴权交给上面的 `_guard` 统一负责 —— APK 里嵌着访问口令，
    #      公开可下等于把口令挂到网上。手机上是带 Cookie 下的，所以照常能用。
    #   3. `latest.apk` 永远指向目录里最新的那个。这样**站点上的下载链接不用跟着
    #      版本号改**，发新版只要把新 APK 传上去就自动生效。
    #   4. `/app` 是它的**短别名** —— 手机上手敲链接时少打一大截
    #      （`/app?t=…` 比 `/dl/latest.apk?t=…` 好记也好输）。
    def _latest_apk() -> Path | None:
        """挑出"最新"的那个 APK。

        ⚠️ 排序**优先看 versionCode，其次才看 mtime**。
        只看 mtime 的话，"谁是线上最新版"就由**上传顺序**决定 ——
        一旦哪天误传了一个更旧的包（或补传一个历史版本做留档），
        它就会顶掉新包成为 `latest`，于是**所有 App 再也收不到更新提示**，
        而且不报任何错。这个坑很难从现象反推，所以从读的一侧也堵上。
        （写的一侧在 app/android/build.sh，那里会拒绝上传更旧的版本。）

        versionCode 取自 sidecar JSON；没有 sidecar 的包按 0 处理 ——
        它只会**排在后面**，不会误当最新，方向是安全的。
        """
        apk_dir = cfg.root / "apk"
        if not apk_dir.is_dir():
            return None
        cands = list(apk_dir.glob("*.apk"))
        if not cands:
            return None

        def key(p: Path):
            # ⚠️ 第三个键是**确定性 tie-break**，必须有。
            #    文件系统的时间戳粒度可能是秒级：同一个测试/同一次上传里
            #    先后写两个包，mtime 可能**完全相同**。这时如果只按
            #    (versionCode, mtime) 排序，谁被选中就取决于 glob 的返回顺序
            #    —— 那是个不受控的偶然量。（原来的实现就是这样：它侥幸能过，
            #    换成 max() 后 tie-break 方向反转，测试立刻翻出来。）
            #    用"版本名拆成数字元组"兜底，既确定又符合直觉。
            return (_apk_version_code(p), p.stat().st_mtime,
                    _ver_tuple(_guess_version(p.name)))

        return max(cands, key=key)

    def _send_apk(target: Path):
        return send_file(target, as_attachment=True, download_name=target.name,
                         mimetype="application/vnd.android.package-archive")

    @app.get("/dl/<path:name>")
    def download(name: str):
        apk_dir = cfg.root / "apk"
        target = _safe_join(apk_dir, name)
        if target is None or target.suffix.lower() != ".apk" or not target.is_file():
            target = _latest_apk() if name == "latest.apk" else None
        if target is None or not target.is_file():
            abort(404)
        return _send_apk(target)

    @app.get("/app")
    def app_shortcut():
        target = _latest_apk()
        if target is None:
            abort(404)
        return _send_apk(target)

    @app.get("/<path:rel>")
    def asset(rel: str):
        # 目录 → 找它的 index.html
        target = _safe_join(public, rel)
        if target and target.is_dir():
            index = target / "index.html"
            if index.exists():
                return send_from_directory(target, "index.html")
        if target and target.is_file():
            return send_from_directory(target.parent, target.name)
        # 兜底：站点还没 build 时，直接发源码里的静态资源
        fallback = _safe_join(cfg.static_dir, rel[len("static/"):]) if rel.startswith("static/") else None
        if fallback and fallback.is_file():
            return send_from_directory(fallback.parent, fallback.name)
        abort(404)

    # ---------------------------------------------------------------- API
    @app.get("/api/status")
    def api_status():
        """手机端轮询它。指纹变了就说明有新数据。"""
        index = store.load_index(cfg.data_dir)
        runs = store.load_runs(cfg.data_dir, limit=1)
        with _lock:
            busy = _state["busy"]
            error = _state["error"]
        latest = max([m.get("latest") or "" for m in index.get("modules", [])] or [""])
        return jsonify({
            "app": cfg.section("app").get("name"),
            "fingerprint": store.fingerprint(index, cfg.data_dir),
            "generated_at": index.get("generated_at"),
            "latest": latest or None,
            "busy": busy,
            "error": error,
            "modules": [
                {"name": m.get("name"), "title": m.get("title"),
                 "latest": m.get("latest"), "days": m.get("days", 0)}
                for m in index.get("modules", [])
            ],
            "last_run": runs[0] if runs else None,
        })

    @app.get("/api/app")
    def api_app():
        """回答"安卓壳的最新版本是哪个" —— App 启动时问一次，比自己新就提示升级。

        ⚠️ 这里回答的是**壳自身**的版本，不是内容版本。内容更新走 /api/status
        （站点轮询指纹自动 reload），两者互不相干，别混。

        版本元数据来自构建时落下的 sidecar JSON（`<apk同名>.json`）——
        因为 versionCode **不在文件名里**，光看文件名只能拿到 versionName。
        sidecar 缺失时退化为"只报 versionName、versionCode 为 0"：
        App 侧是 `code <= 本地` 才不提示，所以 0 只会导致**不提示**，
        不会误报"有新版"——失败方向是安全的。
        """
        target = _latest_apk()
        if target is None:
            abort(404)
        meta = {"versionCode": 0, "versionName": _guess_version(target.name),
                "size": target.stat().st_size, "sha256": None, "built": None,
                "notes": ""}
        side = target.with_suffix(".json")
        if side.is_file():
            try:
                data = json.loads(side.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    meta.update({k: v for k, v in data.items() if k in meta})
            except (OSError, ValueError):
                pass          # sidecar 坏了就用手上的，别让接口 500
        # ⚠️ 这里给的是**具体那个文件**的路径，不是 `/app` 别名。
        #
        # 为什么不用 `/app`：它每次请求都重新挑一次"最新的那个包"。App 的行为是
        # "先问版本 → 再下载"，两次请求之间隔着若干秒 —— 刚好在这中间传了新包，
        # App 就会拿着 v1.5 的版本号去下 v1.6 的包。通常无害，但会出现
        # "更新完还是提示有新版"这种自相矛盾的观感，而且极难复现。
        # 给确定路径就没这个问题：报的版本和给的包是**同一次读取**里取出来的。
        #
        # 兼容性：`/dl/<name>` 一直是公开路由，老版本 App 忽略这个字段、照旧用
        # AppConfig.APK_URL（`/app`），所以改这里不会让已装的 App 出问题。
        meta["url"] = f"/dl/{target.name}"
        return jsonify(meta)

    @app.post("/api/refresh")
    def api_refresh():
        """触发一轮采集。异步执行，立即返回，前端继续轮询 /api/status。"""
        if not modules:
            return jsonify({"error": "没有发现任何模块"}), 400
        with _lock:
            if _state["busy"]:
                return jsonify({"busy": True, "error": "已有一轮采集在进行"}), 409
            _state["busy"] = True
            _state["started_at"] = datetime.now().isoformat(timespec="seconds")
            _state["error"] = None

        t = threading.Thread(target=_run_pipeline, args=(cfg, modules),
                             name="radar-run", daemon=True)
        t.start()
        return jsonify({"started": True, "busy": True,
                        "started_at": _state["started_at"]}), 202

    @app.get("/api/modules")
    def api_modules():
        return jsonify({"modules": [m.meta() for m in modules]})

    return app


def main(root: Path | None = None, port: int | None = None, host: str | None = None) -> None:
    cfg = load(root)
    srv = cfg.section("server")
    flask_app = create_app(root)
    # 优先级：命令行参数 > 环境变量（服务器 .env）> config.json
    # RADAR_PORT 的存在理由和 RADAR_TOKEN 一样 —— config.json 是要公开的，
    # "服务器实际监听哪个端口"这类部署细节不该写死在里面。
    host = host or os.environ.get("RADAR_HOST") or srv.get("host", "0.0.0.0")
    port = int(port or os.environ.get("RADAR_PORT") or srv.get("port", 18090))
    print(f"{cfg.section('app').get('name')} 已启动 → http://{host}:{port}")
    print(f"  模块 {len(registry.discover(cfg.modules_dir))} 个 · "
          f"数据目录 {cfg.data_dir}")
    # threaded=True：刷新时后台跑采集，页面不能因此卡住
    flask_app.run(host=host, port=port, threaded=True)
