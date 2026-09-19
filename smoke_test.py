#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地冒烟测试：不联网、不依赖 flask，只验证语法与纯函数正确性。

服务器上再跑真实采集。这里先把"低级错误"挡在部署之前。
"""
import importlib.util
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# 输出同时写进 _smoke.txt（UTF-8）。Windows 控制台编码经常把中文搞成乱码，
# 直接看文件最可靠。
_LOG = open(ROOT / "_smoke.txt", "w", encoding="utf-8")


class _Tee:
    def write(self, s):
        try:
            sys.__stdout__.write(s)
        except Exception:  # noqa: BLE001
            pass
        _LOG.write(s)

    def flush(self):
        try:
            sys.__stdout__.flush()
        except Exception:  # noqa: BLE001
            pass
        _LOG.flush()


sys.stdout = _Tee()

FAILED = []


# ------------------------------------------------------------------ 断言助手
def eq(a, b):
    if a != b:
        raise AssertionError(f"{a!r} != {b!r}")


def has(needle, haystack):
    if needle not in str(haystack):
        raise AssertionError(f"{needle!r} 不在结果里：{str(haystack)[:200]!r}")


def check(name, fn):
    try:
        fn()
        print(f"  [OK] {name}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [!!] {name}: {type(exc).__name__}: {exc}")
        FAILED.append(name)


# ============================================================ 1. 语法编译
print("== 1. 全量语法编译 ==")
py_files = sorted(p for p in ROOT.rglob("*.py")
                  if "__pycache__" not in p.parts and ".venv" not in p.parts)
compile_errs = []
for p in py_files:
    try:
        py_compile.compile(str(p), doraise=True, cfile=str(p) + ".pyc")
    except py_compile.PyCompileError as exc:
        compile_errs.append(f"{p.relative_to(ROOT)}: {exc}")
    finally:
        Path(str(p) + ".pyc").unlink(missing_ok=True)
print(f"  编译 {len(py_files)} 个文件，失败 {len(compile_errs)}")
for e in compile_errs:
    print(f"    [!!] {e}")
if compile_errs:
    FAILED.append("syntax")
    print("\n语法都没过，后面的测试没意义，先退出。")
    sys.exit(1)

# ============================================================ 2. 核心层
print("\n== 2. 核心层 ==")
from radar.core import md, registry, store               # noqa: E402
from radar.core.config import load                       # noqa: E402

cfg = load(ROOT)
check("config 加载根目录", lambda: eq(cfg.root, ROOT))
check("data_dir 解析", lambda: eq(cfg.data_dir.name, "data"))
check("module_cfg 合并", lambda: eq(cfg.module_cfg("github_trending").get("top_n"), 10))

check("md 标题", lambda: has("<h1>A</h1>", md.to_html("# A")))
check("md 表格", lambda: has("<table>", md.to_html("| a | b |\n|---|---|\n| 1 | 2 |")))
check("md 列表", lambda: has("<li>x</li>", md.to_html("- x\n- y")))
check("md 加粗", lambda: has("<strong>b</strong>", md.to_html("**b**")))
check("md 转义", lambda: has("&lt;script&gt;", md.to_html("<script>")))
check("md 引用", lambda: has("<blockquote>", md.to_html("> hi")))
check("md 代码围栏", lambda: has("<pre><code>", md.to_html("```\nx=1\n```")))
check("md 链接", lambda: has('<a href="https://a.b/c"', md.to_html("[n](https://a.b/c)")))
check("md 粗体链接", lambda: has("<strong><a href=", md.to_html("**[n](https://a.b)**")))
check("md 表格里的链接", lambda: has("<a href=", md.to_html("| h |\n|---|\n| [n](https://a.b) |")))

_IDX = {"modules": [{"name": "a", "latest": "2026-09-13", "days": 3},
                    {"name": "b", "latest": "2026-09-12", "days": 1}]}
check("fingerprint 稳定", lambda: eq(store.fingerprint(_IDX), store.fingerprint(dict(_IDX))))
_IDX2 = {"modules": [{"name": "a", "latest": "2026-09-14", "days": 4},
                     {"name": "b", "latest": "2026-09-12", "days": 1}]}
check("fingerprint 随数据变", lambda: eq(
    store.fingerprint(_IDX) != store.fingerprint(_IDX2), True))
check("fingerprint 前缀含日期", lambda: has("2026-09-13", store.fingerprint(_IDX)))

# 带 data_dir 的指纹：要能察觉"同一天内的二次采集"
import tempfile                                             # noqa: E402
_fpdir = Path(tempfile.mkdtemp(prefix="radar_fp_"))
_fpidx = {"modules": [{"name": "a", "latest": "2026-09-13", "days": 1}]}
store.write_json(_fpdir / "a" / "2026-09-13.json", {"v": 1})
_f1 = store.fingerprint(_fpidx, _fpdir)
check("同数据重复算指纹一致", lambda: eq(_f1, store.fingerprint(_fpidx, _fpdir)))
store.write_json(_fpdir / "a" / "2026-09-13.json", {"v": 2, "more": "内容变了"})
_f2 = store.fingerprint(_fpidx, _fpdir)
check("同一天数据变→指纹变", lambda: eq(_f1 != _f2, True))
check("日期段保持不变", lambda: has("2026-09-13", _f2))
_fpidx["modules"][0]["latest"] = "2026-09-14"
_f3 = store.fingerprint(_fpidx, _fpdir)      # 文件不存在也不能抛异常
check("文件缺失不抛异常", lambda: eq(isinstance(_f3, str), True))
check("文件缺失时指纹变化", lambda: eq(_f3 != _f2, True))
import shutil as _shutil                                     # noqa: E402
_shutil.rmtree(_fpdir, ignore_errors=True)

# ==================================================== 2b. 服务端 SVG 小图表
# 这几张图是"烤进 HTML"的：不依赖 JS，因此**必须在服务端就算对**。
# 画错了页面上不会有任何报错，只会安静地显示一张错的图 —— 所以逐个边界都验一遍。
print("\n== 2b. 服务端 SVG 小图表 ==")
from radar.core import charts                                # noqa: E402

_sp = charts.spark([1, 3, 2, 5, 4])
check("spark 出 SVG", lambda: has('<svg class="spark"', _sp))
check("spark 有点坐标", lambda: has("<polyline", _sp))
# 折线上的坐标点数必须等于数据点数 —— 少画一个点肉眼看不出来，但图就是错的
import re as _re2                                            # noqa: E402
_sp_pts = _re2.search(r'<polyline points="([^"]*)"', _sp).group(1)
check("spark 点数等于数据点数", lambda: eq(len(_sp_pts.split(" ")), 5))
check("spark 不拉伸变形", lambda: has('preserveAspectRatio="none"', _sp))
# 空/单点没有趋势可画：必须返回空串而不是崩，也不能画一根假线
check("spark 空序列", lambda: eq(str(charts.spark([])), ""))
check("spark 单点", lambda: eq(str(charts.spark([5])), ""))
check("spark 全是脏值", lambda: eq(str(charts.spark([None, "x", float("nan")])), ""))
# 脏值必须被跳过，而不是让整张图消失（数据里有 None 是常态）
_dirty = charts.spark([None, "7", float("nan"), 3, 4, "x"])
check("spark 跳过脏值后仍有图", lambda: has("<polyline", _dirty))
# 全平时画在中间（贴着底边会看起来像 0）
check("spark 全平居中", lambda: has("21.0", charts.spark([2, 2, 2], height=42)))
# 颜色要过转义：万一有人把外部数据当颜色喂进来，不能突破属性
check("spark 颜色转义", lambda: has("&#34;", charts.spark([1, 2], color='a"b')))

_b_mixed = charts.bars([-22.1, 18.9, 114.8])
check("bars 出 SVG", lambda: has('<svg class="sbars"', _b_mixed))
check("bars 有正负就画零基线", lambda: has("<line", _b_mixed))
check("bars 正柱用 up 色", lambda: has("var(--up)", _b_mixed))
check("bars 负柱用 down 色", lambda: has("var(--down)", _b_mixed))
# 全正时不该出现零基线（那是条多余的横线）
check("bars 全正无基线", lambda: eq("<line" in charts.bars([1, 2, 3]), False))
check("bars 空序列", lambda: eq(str(charts.bars([])), ""))
# 全 0 时基线要居中，不能把一排柱子顶到画面顶端
check("bars 全零基线居中", lambda: has('y1="19.0"', charts.bars([0, 0, 0], height=38)))

check("hbar 百分比", lambda: has("width:75.0%", charts.hbar(3, 4)))
check("hbar 超过最大值封顶", lambda: has("width:100.0%", charts.hbar(9, 4)))
check("hbar 负值归零", lambda: has("width:0.0%", charts.hbar(-1, 4)))
check("hbar maxv=0 不画", lambda: eq(str(charts.hbar(3, 0)), ""))
check("hbar 非法值不画", lambda: eq(str(charts.hbar(None, 4)), ""))

# ==================================================== 3. github_trending
print("\n== 3. github_trending ==")


def load_file(key, path):
    spec = importlib.util.spec_from_file_location(key, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


gt = load_file("t_gt_sources", ROOT / "radar/modules/github_trending/sources.py")

SAMPLE_ROW = """
<article class="Box-row">
  <h2 class="h3 lh-condensed"><a href="/vllm-project/vllm">vllm-project / vllm</a></h2>
  <p class="col-9 color-fg-muted my-1 pr-4">A high-throughput and memory-efficient inference
  and serving engine for LLMs</p>
  <span itemprop="programmingLanguage">Python</span>
  <a href="/vllm-project/vllm/stargazers"><span class="Counter">52,341</span></a>
  <span class="d-inline-block float-sm-right">1,234 stars today</span>
</article>
"""
parsed = gt.parse_trending(SAMPLE_ROW)
check("解析出 1 条", lambda: eq(len(parsed), 1))
row = parsed[0]
check("owner/name", lambda: eq((row["owner"], row["name"]), ("vllm-project", "vllm")))
check("description", lambda: has("throughput", row["description"]))
check("language", lambda: eq(row["language"], "Python"))
check("stars_growth", lambda: eq(row["stars_growth"], 1234))
check("stars_total", lambda: eq(row["stars_total"], 52341))
check("url", lambda: eq(row["url"], "https://github.com/vllm-project/vllm"))
check("空 HTML 不炸", lambda: eq(gt.parse_trending(""), []))

# ==================================================== 4. market_flow
print("\n== 4. market_flow ==")
mf_src = load_file("t_mf_sources", ROOT / "radar/modules/market_flow/sources.py")
check("_q 加时间戳", lambda: has("_=", mf_src._q("https://x/y?a=1")))
check("FS 常量", lambda: eq(mf_src.FS_INDUSTRY, "m:90+t:2"))
check("FS 概念板块", lambda: eq(mf_src.FS_CONCEPT, "m:90+t:3"))
check("候选指数数", lambda: eq(len(mf_src.INDEX_GLOBAL), 8))
check("候选 IP 非空", lambda: eq(len(mf_src.__dict__.get("FS_A_SHARE", "")) > 0, True))

mf = load_file("t_mf_module", ROOT / "radar/modules/market_flow/module.py")
check("_yi 元→亿", lambda: eq(mf._yi(4741400064.0), 47.41))
check("_yi 非数值", lambda: eq(mf._yi(None), None))
check("_yi_text 正数", lambda: eq(mf._yi_text(4.7e9), "+47.00 亿"))
check("_tone 红涨绿跌", lambda: eq((mf._tone(1.2), mf._tone(-1.2), mf._tone(0)),
                                    ("up", "down", "flat")))
check("_pct_text", lambda: eq(mf._pct_text(-1.18), "-1.18%"))
check("_amount_text 万亿", lambda: has("万亿", mf._amount_text(1.97e12)))
check("_amount_text 亿", lambda: has("亿", mf._amount_text(9.58e11)))

DATA = {
    "fetched_at": "2026-09-13T08:10:00",
    "ok_sources": ["国内指数", "行业板块资金流"],
    "errors": {},
    "cn_indices": [
        {"code": "000001", "name": "上证指数", "pct": -1.18, "tone": "down",
         "price_text": "3,888.11", "pct_text": "-1.18%", "amount": 958186336970.0,
         "amount_text": "9582 亿", "amplitude": 1.53},
        {"code": "399001", "name": "深证成指", "pct": -1.08, "tone": "down",
         "price_text": "13,471.26", "pct_text": "-1.08%", "amount": 1013712150759.0,
         "amount_text": "1.01 万亿", "amplitude": 1.9},
    ],
    "global_indices": [{"code": "DJIA", "name": "道琼斯", "pct": 0.98, "tone": "up",
                        "price_text": "52,573.29", "pct_text": "+0.98%"}],
    "industry": {"inflow": [{"name": "通信设备", "flow": 47.41, "flow_text": "+47.41 亿",
                             "super": 40.9, "super_text": "+40.91 亿",
                             "small": -54.3, "small_text": "-54.34 亿",
                             "flow_pct": 2.67, "pct_text": "+0.52%", "tone": "up"}],
                 "outflow": [{"name": "银行", "flow_text": "-30.00 亿"}]},
    "concept": {"inflow": [], "outflow": []},
    "stock": {"inflow": [{"name": "中际旭创", "flow": 31.2, "flow_text": "+31.20 亿",
                          "super_text": "+25.00 亿", "flow_pct": 10.41,
                          "pct_text": "+4.03%", "tone": "up",
                          "small_text": "-10.00 亿"}],
              "outflow": []},
    "breadth": {"total": 5558, "up": 1000, "down": 4000, "flat": 558,
                "up_ratio": 18.0, "ratio": 0.25, "tone": "down"},
    "margin": {"date": "2026-09-10", "rzye": 26171.0, "rzjme": -22.14, "delta": -13.6,
               "delta_tone": "down", "rqye": 100.0, "turnover_pct": 2.626,
               # 至少 2 天：走势图是靠这段多日序列画的，只给 1 天等于没测到图
               "trend": [
                   {"date": "2026-09-10", "rzye": 26171.0, "rzjme": -22.14, "pct": -0.53},
                   {"date": "2026-09-09", "rzye": 26194.0, "rzjme": 18.92, "pct": 0.30},
                   {"date": "2026-09-08", "rzye": 26175.1, "rzjme": -5.40, "pct": -0.11}]},
}

summary = mf.MarketFlow._summarize(DATA)
check("_summarize 上涨数", lambda: eq(summary["cn_up"], 0))
check("_summarize 下跌数", lambda: eq(summary["cn_down"], 2))
check("_summarize 成交额合计", lambda: has("万亿", summary["amount_total_text"]))
check("_summarize 龙头板块", lambda: eq(summary["top_industry"], "通信设备"))

sigs = mf.MarketFlow._signals(DATA)
titles = " | ".join(s["title"] for s in sigs)
check("_signals 非空", lambda: eq(len(sigs) > 0, True))
check("_signals 结构性行情", lambda: has("结构性", titles))
check("_signals 主力吸筹", lambda: has("主力接散户抛盘", titles))
check("_signals 普跌", lambda: has("普跌", titles))
check("_signals 两融", lambda: has("去杠杆", titles))
check("_signals level 合法", lambda: eq(
    set(s["level"] for s in sigs) <= {"ok", "warn", "info"}, True))

# 广度阈值两侧都要覆盖到
for ratio, want in ((85.0, "赚钱效应好"), (10.0, "普跌"), (45.0, "涨跌分化")):
    d2 = dict(DATA)
    d2["breadth"] = {"total": 5000, "up": int(ratio * 50), "down": 5000 - int(ratio * 50),
                     "flat": 0, "up_ratio": ratio, "ratio": None, "tone": "up"}
    got = " ".join(x["title"] for x in mf.MarketFlow._signals(d2))
    check(f"广度 {ratio}% → {want}", (lambda g: (lambda: has(want, g)))(got))

# down=0 时不能出现 inf（会序列化成非法 JSON "Infinity"，前端 JSON.parse 直接炸）
import json as _json
_RAW_MIN = {"cn_indices": [], "global_indices": [],
            "industry": {"inflow": [], "outflow": []},
            "concept": {"inflow": [], "outflow": []},
            "stock": {"inflow": [], "outflow": []},
            "breadth": {"total": 100, "up": 100, "down": 0, "flat": 0, "pages": 1},
            "margin": []}
_norm = mf.MarketFlow().analyze(
    _RAW_MIN, type("C", (), {"config": cfg, "log_info": lambda *a: None})())
check("down=0 时 ratio 为 None", lambda: eq(_norm["breadth"]["ratio"], None))
check("广度数据可 JSON 序列化", lambda: _json.dumps(_norm["breadth"]))
check("全 A 上涨时出赚钱效应", lambda: has(
    "赚钱效应", " ".join(x["title"] for x in _norm["signals"])))


class _Ctx:
    date = "2026-09-13"


# report() 读的是 analyze() 的产出，把这两项补进 DATA，才和线上形状一致
DATA["summary"] = summary
DATA["signals"] = sigs
rep = mf.MarketFlow().report(DATA, _Ctx())
for needle in ("# 股市资金流向", "结论摘要", "## 一、指数", "## 二、板块资金流",
               "## 三、个股主力资金", "## 四、两融", "北向资金", "不构成任何投资建议",
               "口径校验", "融资余额占流通市值"):
    check(f"report 含 {needle}", (lambda n: (lambda: has(n, rep)))(needle))
rep_dupe = rep.count("较前一交易日")
check("两融变化只出现一次", lambda: eq(rep_dupe, 1))

check("report 空数据不炸", lambda: mf.MarketFlow().report(
    {"summary": {}, "signals": [], "cn_indices": [], "global_indices": [],
     "industry": {"inflow": [], "outflow": []}, "concept": {"inflow": [], "outflow": []},
     "stock": {"inflow": [], "outflow": []}, "breadth": {}, "margin": {}}, _Ctx()))

# ============================================================ 5. registry
print("\n== 5. registry 发现 ==")
found = registry.discover(cfg.modules_dir)
# ⚠️ 别再写死模块数量。加一个模块就红一片的测试是**噪音**，只会训练人忽略失败。
# 这里改为断言"内置模块都在"，并把底栏数量从注册表推出来 —— 加模块时该改的是
# 这一行的清单（失败信息会明确说少了谁），而不是三个散落的魔数。
BUILTIN_MODULES = ["github_trending", "market_flow", "reader"]
check("发现全部内置模块", lambda: eq(sorted(found), sorted(BUILTIN_MODULES)))
check("name 与目录一致", lambda: eq(all(k == v.name for k, v in found.items()), True))
check("order 排序", lambda: eq(
    [m.name for m in registry.ordered(found)],
    [n for n, _ in sorted(found.items(), key=lambda kv: kv[1].order)]))
check("模板存在", lambda: eq(
    all((cfg.modules_dir / n / "template.html").exists() for n in found), True))

# ==================================================== 6. 模板真实渲染
# 这一段是重点：Jinja 的 `data.items` 会命中 dict 的 .items() 方法而不是同名的键，
# 光看代码看不出来，必须真渲染一遍才会暴露。用合成数据把整站生成一次。
print("\n== 6. 模板渲染（整站生成）==")
try:
    import jinja2                                            # noqa: F401
    HAVE_JINJA = True
except ImportError:
    HAVE_JINJA = False

if not HAVE_JINJA:
    print("  [skip] 没装 jinja2，跳过（装了才有意义）")
else:
    import shutil
    import tempfile

    from radar.core import render as render_mod

    TMP = Path(tempfile.mkdtemp(prefix="radar_smoke_"))
    cfg2 = load(ROOT)
    cfg2["storage"] = dict(cfg2["storage"])
    cfg2["storage"]["data_dir"] = str(TMP / "data")
    cfg2["storage"]["public_dir"] = str(TMP / "public")

    DATE = "2026-09-13"
    GH_DATA = {
        "source": {"url": "https://github.com/trending?since=daily", "ip": "140.82.112.4",
                   "elapsed": 2.7, "attempt": 1, "page_items": 25, "picked": 2, "enriched": 2},
        "count": 2,
        "repos": [
            {"rank": 1, "full_name": "vllm-project/vllm", "url": "https://github.com/vllm-project/vllm",
             "what": "A high-throughput LLM inference engine", "language": "Python",
             "topics": ["llm", "inference"], "category": "AI / 大模型", "form": "大型项目",
             "stars_growth": 1234, "stars_total": 52341, "stars_growth_text": "1,234",
             "stars_total_text": "5.2万", "growth_ratio": 2.4, "pushed_days": 0,
             "created_days": 900, "archived": False, "license": "Apache-2.0", "meta_error": ""},
            {"rank": 2, "full_name": "old/tool", "url": "https://github.com/old/tool",
             "what": "（作者未填写描述）", "language": "Go", "topics": [], "category": "开发工具",
             "form": "轻量库/工具", "stars_growth": 500, "stars_total": 900,
             "stars_growth_text": "500", "stars_total_text": "900", "growth_ratio": 55.6,
             "pushed_days": 200, "created_days": 2000, "archived": True,
             "license": "", "meta_error": "HTTP 403"},
        ],
        "stats": {"languages": [("Python", 1), ("Go", 1)], "categories": [("AI / 大模型", 1)],
                  "top_gainer": "vllm-project/vllm", "top_growth": 1234,
                  "newcomers": ["vllm-project/vllm"], "stale": ["old/tool"],
                  "with_topics": 1, "avg_growth": 867},
    }
    MF_DATA = dict(DATA)
    MF_DATA["ok_sources"] = ["国内指数", "行业板块资金流", "涨跌家数"]
    MF_DATA["errors"] = {"concept": "SourceError: clist 返回空"}
    # analyze() 真实产出的形状
    MF_DATA["summary"] = mf.MarketFlow._summarize(MF_DATA)
    MF_DATA["signals"] = mf.MarketFlow._signals(MF_DATA)

    d = cfg2.data_dir
    store.write_json(d / "github_trending" / f"{DATE}.json", GH_DATA)
    store.write_text(d / "github_trending" / f"{DATE}.md",
                     found["github_trending"].report(GH_DATA, _Ctx()))
    store.write_json(d / "market_flow" / f"{DATE}.json", MF_DATA)
    store.write_text(d / "market_flow" / f"{DATE}.md", mf.MarketFlow().report(MF_DATA, _Ctx()))
    # 再放一份"字段缺失"的老快照，验证模板的防御性取值（历史页面必须还能打开）
    store.write_json(d / "market_flow" / "2026-09-01.json",
                     {"cn_indices": [], "ok_sources": []})
    store.write_text(d / "market_flow" / "2026-09-01.md", "")
    store.write_json(d / "github_trending" / "2026-09-01.json", {})
    store.save_index(d, store.build_index(d, [m.meta() for m in registry.ordered(found)]))

    try:
        stats = render_mod.build_site(cfg2, cfg2.section("app"),
                                      registry.ordered(found), log=lambda m: None)
        check("build_site 返回页数", lambda: eq(stats["pages"] >= 3, True))

        pub = cfg2.public_dir
        for rel in ("index.html", "manifest.json", "static/style.css", "static/app.js",
                    "github_trending/index.html", f"github_trending/{DATE}.html",
                    "market_flow/index.html", f"market_flow/{DATE}.html"):
            check(f"产出 {rel}", (lambda r: (lambda: eq((pub / r).exists(), True)))(rel))

        home = (pub / "index.html").read_text(encoding="utf-8")
        check("首页含模块名", lambda: has("GitHub 日榜", home))
        check("首页套了外壳（含 head）", lambda: has("<!DOCTYPE html>", home))
        check("首页含样式表", lambda: has("style.css?v=", home))
        check("首页含脚本", lambda: has("app.js?v=", home))
        check("首页含指纹 meta", lambda: has('name="radar-fingerprint"', home))
        check("首页含 body 指纹", lambda: has("data-fingerprint=", home))
        check("首页含顶栏导航", lambda: has('class="tabs"', home))
        check("首页含底栏导航", lambda: has('class="bottom-nav"', home))
        # ⭐ 2026-09-20 重设计：底栏用 SVG 图标（不是 emoji 字符）
        check("底栏图标用 SVG", lambda: 'class="bn-item' in home
              and home.count('<svg viewBox="0 0 24 24"') >= 3)
        # 手机上把网页变成真 App 的入口（页脚链接，指向 latest.apk）——
        # 链接丢了的话用户在站上就再也找不到安装包了
        check("页脚有装 App 的入口", lambda: has('href="/app"', home))

        # --------------------------------------------- 设计：问候条 + 卡片
        check("首页含问候条", lambda: has('class="greeting"', home))
        check("问候条有 h1", lambda: has('id="greeting-text"', home))
        check("问候条显示日期", lambda: has('id="greeting-date"', home))
        check("问候条有数据新鲜度占位", lambda: has('id="freshness-2"', home))
        # 首页卡用新的 .mod-grid / .mod-card 体系（淘汰旧的 .card / .card-glyph）
        check("首页有 mod-grid 网格", lambda: has('class="mod-grid"', home))
        check("首页有 mod-card", lambda: has('class="mod-card', home))
        check("首页卡有 glyph 图标", lambda: has('class="glyph"', home))
        # hero 数字 走 data-countup（不是写死）；模板的 count-up JS 会从 0 滚到目标
        check("首页 hero 有 data-countup", lambda: has('data-countup=', home))
        # 趋势迷你线 / 区域填充
        check("首页有迷你 SVG 折线", lambda: has('<svg class="sparkline-thumb"', home))
        check("首页 trend area 是闭合多边形", lambda: has('fill-opacity="0.12"', home))
        # 顶部 brand.dot（必须用 inline SVG 不用 emoji）
        check("首页 brand 用品牌色 dot", lambda: has('class="brand-dot"', home))

        gh_html = (pub / "github_trending" / "index.html").read_text(encoding="utf-8")
        for needle in ("vllm-project/vllm", "A high-throughput", "AI / 大模型",
                       "已归档", "已 200 天无提交", "1,234", "140.82.112.4",
                       "完整报告", 'class="tabs"', "datebar", "最新"):
            check(f"榜单页含 {needle}", (lambda n: (lambda: has(n, gh_html)))(needle))

        mf_html = (pub / "market_flow" / "index.html").read_text(encoding="utf-8")
        # 一些名字换了：中国指数 → 今天怎么样（hero 行）、市场广度 → 内嵌于 hero 段
        for needle in ("结论摘要", "今天怎么样", "通信设备", "中际旭创", "两融",
                       "上涨 ", "涨跌", "concept 缺失",
                       "全球指数", "道琼斯"):
            check(f"行情页含 {needle}", (lambda n: (lambda: has(n, mf_html)))(needle))

        check("日期切换条可点", lambda: has("2026-09-01.html", mf_html))
        check("模块页也有装 App 的入口", lambda: has('href="/app"', gh_html))

        # ------------------------------------------------- 展示层（本次新增）
        # 这些图/条都是服务端烘进 HTML 的，测试里只验"有没有真的落到产物里" ——
        # 少了任何一块页面都不会报错，只会安静地少一眼信息。
        for needle in ('<svg class="spark"', '<svg class="sbars"', 'class="breadth-bar"',
                       'class="seg up"', 'class="seg down"',
                       'class="col-opt"', 'class="charts"'):
            check(f"行情页含 {needle}", (lambda n: (lambda: has(n, mf_html)))(needle))
        check("三张走势图都画了", lambda: eq(mf_html.count('class="chart"'), 3))
        check("三张图各有轴标注", lambda: eq(mf_html.count('class="chart-axis"'), 3))
        # ⭐ 2026-09-20 重设计：上证/深证 改用 stat-cell + count-up
        check("行情页有 hero-row", lambda: has('class="hero-row"', mf_html))
        check("上证用 stat-cell 容器", lambda: has('class="stat-cell', mf_html))
        check("上证有数据计数动画", lambda: mf_html.count('data-countup=') >= 4)
        # 广度条的三段宽度按家数实算（1000/558/4000 对 5558），加起来必须约等于 100%
        check("广度条宽度按家数", lambda: has('style="width:17.99%"', mf_html)
              and has('style="width:71.97%"', mf_html))
        # 图表不能是空壳：折线得有坐标、柱状得有色块
        check("折线有坐标点", lambda: has("<polyline points=", mf_html))
        check("柱状有色块", lambda: has("var(--up)", mf_html) or has("var(--down)", mf_html))

        for needle in ('class="dist-list"', 'class="hbar accent"', 'class="dist-name"'):
            check(f"榜单页含 {needle}", (lambda n: (lambda: has(n, gh_html)))(needle))
        check("分布占比条存在", lambda: gh_html.count('class="hbar accent"') >= 5)

        check("首页有模块字形标识", lambda: has('class="glyph"', home))
        check("首页卡带时间戳副信息", lambda: has("数据 ", home) and has("累计 ", home))

        css_txt = (pub / "static" / "style.css").read_text(encoding="utf-8")
        check("窄屏隐藏二级列", lambda: has("table.tbl .col-opt { display: none; }", css_txt))
        check("图表高度自适应", lambda: has(".chart svg { display: block; width: 100%; height: auto; }", css_txt))
        check("状态栏色随深浅色", lambda: eq(home.count('name="theme-color"'), 2))
        check("声明支持双配色", lambda: has('name="color-scheme" content="light dark"', home))
        # 深色模式不能再用写死的前三配色（会跟深色底打架）
        check("榜单配色已变量化", lambda: eq("#d1242f; border-color: #d1242f" in css_txt, False))
        # ⭐ 2026-09-20 重设计：dark mode 是 OLED 友好的真黑
        check("暗色背景近似 OLED", lambda: has("#0a0c0f", css_txt))
        # 入场动画尊重 reduced-motion
        check("动效尊重减弱偏好", lambda: has("prefers-reduced-motion: reduce", css_txt))
        # 设计令牌化（出现 oklch 或新 HSL 语义不算退化）
        check("新设计令牌存在（--brand-glow）", lambda: has("--brand-glow", css_txt))
        check("新设计令牌存在（--r-lg）", lambda: has("--r-lg", css_txt))

        # 字段缺失的老快照：图表必须整体不出现，而不是画出半张残图
        old = (pub / "market_flow" / "2026-09-01.html").read_text(encoding="utf-8")
        check("老快照不炸", lambda: has("<!DOCTYPE html>", old))
        check("老快照无摘要区", lambda: eq("结论摘要" in old, False))
        check("老快照不出图表", lambda: eq('class="charts"' in old, False))
        check("老快照不出广度条", lambda: eq('class="breadth-bar"' in old, False))
        old_gh = (pub / "github_trending" / "2026-09-01.html").read_text(encoding="utf-8")
        check("空数据快照不炸", lambda: has("今日十强", old_gh))
        # 空 repos → 参照值 0 → 一条增长条都不该画（不是画出宽度 0 的空壳）
        check("空数据快照不出增长条", lambda: eq('class="hbar' in old_gh, False))

        # ⭐ 更新口子的核心不变量：页面里烘的指纹，必须等于 /api/status 现在会返回的指纹。
        # 两者一致 ⇔ "指纹变了" 精确等价于 "数据比页面新"。
        import re as _re
        m = _re.search(r'data-fingerprint="([^"]*)"', gh_html)
        page_fp = m.group(1) if m else ""
        api_fp = store.fingerprint(store.load_index(cfg2.data_dir), cfg2.data_dir)
        check("页面里有指纹", lambda: eq(bool(page_fp), True))
        check("页面指纹 == 构建时索引指纹", lambda: eq(page_fp, api_fp))
        check("首页与模块页指纹一致", lambda: eq(
            _re.search(r'data-fingerprint="([^"]*)"', home).group(1), page_fp))
        check("指纹含最新日期", lambda: has(DATE, page_fp))

        check("静态资源版本号", lambda: has("style.css?v=", gh_html))

        # ----------------------------------------- 交互层（本次新增）
        # 模板产物里要能验出新交互组件的真存在；CSS/JS 里要能验出关键实现段。
        # 漏写一段 → 页面不会报错，只会"少一个手指能按下去的地方"，故全验。
        for needle in ('id="pull-indicator"', 'id="share-btn"', 'class="bottom-nav"',
                       'class="bn-item active"', 'id="install-banner"'):
            check(f"base 含 {needle}", (lambda n: (lambda: has(n, home)))(needle))
        # 底栏 = 总览 + 各启用模块。数量从注册表 + config 推出来，不写魔数。
        _nav_mods = [n for n in found if cfg.module_cfg(n).get("enabled", True)]
        check("底栏 = 总览 + 各模块", lambda: eq(
            home.count('class="bn-item'), 1 + len(_nav_mods)))
        check("底栏含每个启用模块", lambda: eq(
            all(f"/{n}/" in home for n in _nav_mods), True))
        # 安装横幅只在首页出（active=None 时），模块页不该出现
        check("install-banner 不在模块页", lambda: eq(
            'id="install-banner"' in mf_html or 'id="install-banner"' in gh_html, False))
        check("旧快照也不会冒横幅", lambda: eq(
            'id="install-banner"' in old, False))

        js_txt = (pub / "static" / "app.js").read_text(encoding="utf-8")
        for needle in ("setupPullToRefresh", "setupLongPressCopy", "setupCollapsible",
                       "setupShare", "setupInstallBanner", "requestRefresh",
                       "navigator.vibrate", "navigator.share",
                       "navigator.clipboard.writeText", "radar:refreshEnd"):
            check(f"app.js 含 {needle}", (lambda n: (lambda: has(n, js_txt)))(needle))
        # ⭐ 2026-09-20 重设计后新加的：
        # 长按菜单（pin/mute/share/copy 浮动菜单）
        check("app.js 含长按菜单", lambda: has("setupLongPressMenu", js_txt))
        check("长按菜单能复制链接", lambda: has('data-act="copy"', js_txt))
        check("长按菜单能固定", lambda: has('data-act="pin"', js_txt))
        check("长按菜单能静音一天", lambda: has('data-act="mute"', js_txt))
        # 时间问候 + count-up
        check("app.js 含时间问候", lambda: has("timeOfDayGreeting", js_txt))
        check("app.js 含数字 count-up", lambda: has("setupCountUp", js_txt)
              and has("data-countup", js_txt))
        check("count-up 守 reduced-motion", lambda: has("prefers-reduced-motion", js_txt))
        # 边滑切模块
        check("app.js 含 swipe 切模块", lambda: has("setupSwipeBetweenModules", js_txt))
        check("app.js swipe 用 bottom-nav 推算", lambda: has("bn-item", js_txt))
        # FAB 接管刷新（重复点击不会进 busy 重复请求）
        check("app.js FAB 绑刷新", lambda: has('"fab"', js_txt))
        check("app.js 撑住 pull 后回弹", lambda: has("pullEl.style.transform", js_txt))
        # ⭐ 2026-09-20：App 内的「版本 + 检查更新」入口。
        # 壳里的自动检查是隐形的（6 小时静默一次），用户看不到版本也没法主动问 ——
        # 这个入口补的就是那部分感知。
        check("app.js 含版本入口", lambda: has("setupAppVersion", js_txt))
        check("app.js 调原生桥", lambda: has("RadarNative", js_txt))
        check("app.js 调 checkForUpdate", lambda: has("checkForUpdate", js_txt))
        check("浏览器里不显示版本入口", lambda: has("if (!native) return;", js_txt))
        check("有新版本时点亮入口", lambda: has("has-new", js_txt))
        # 页脚里那个按钮的骨架（JS 才 unhide，静态产物里是 hidden 的）
        check("页脚含版本入口节点", lambda: has('id="app-ver"', home))
        check("页脚版本入口默认隐藏", lambda: has('id="app-ver" class="app-ver" hidden', home))
        check("页脚含版本号占位", lambda: has('id="app-ver-name"', home))
        check("CSS 含版本入口样式", lambda: has(".app-ver", css_txt))
        check("CSS 有高亮态", lambda: has(".app-ver.has-new", css_txt))
        # 长按复制的几个关键判断：选区、超时、移动容差、抑制 click
        check("长按 520ms", lambda: has("520", js_txt))
        check("长按 MOVE_TOL=10", lambda: has("MOVE_TOL = 10", js_txt))
        check("长按后抑制 click", lambda: has("suppressNextClick", js_txt))

        for needle in (".bottom-nav", "@media (max-width: 560px)", "@media (min-width: 561px)",
                       ".pull-indicator", ".pull-ready", ".panel.is-collapsed",
                       ".install-banner", ".bn-item.active::before",
                       "@keyframes panel-in", "prefers-reduced-motion",
                       "overscroll-behavior", "-webkit-tap-highlight-color"):
            check(f"CSS 含 {needle}", (lambda n: (lambda: has(n, css_txt)))(needle))
        # 桌面端必须把底部导航藏掉 —— 否则顶部 tabs 和底栏同时出现很挤
        check("桌面端藏底栏", lambda: has(".bottom-nav { display: none !important; }", css_txt))
        check("手机端藏顶 tabs", lambda: has(".tabs { display: none; }", css_txt))

        # ---------------------------------------------------------- PWA 可安装性
        # 这几条不是"锦上添花"，是"装了能不能像个 app"的硬条件。浏览器不会报错，
        # 只会默默不给安装入口 —— 所以必须在这里显式验。
        check("manifest 在 public 根目录", lambda: eq((pub / "manifest.json").exists(), True))

        import json as _json

        def _png_size(p):
            """直接读 PNG 的 IHDR，拿到真实像素尺寸。"""
            b = p.read_bytes()
            if b[:8] != b"\x89PNG\r\n\x1a\n":
                return None
            return (int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big"))

        mf = _json.loads((pub / "manifest.json").read_text(encoding="utf-8"))
        check("manifest display=standalone", lambda: eq(mf.get("display"), "standalone"))
        check("manifest 有 start_url", lambda: eq(bool(mf.get("start_url")), True))
        check("manifest 有 scope", lambda: eq(bool(mf.get("scope")), True))
        check("manifest 有 name/short_name",
              lambda: eq(bool(mf.get("name")) and bool(mf.get("short_name")), True))
        check("manifest 有 theme_color", lambda: eq(bool(mf.get("theme_color")), True))

        # ⚠️ 关键：Chrome 判定可安装，硬性要求"至少一个 192x192 + 一个 512x512 图标"。
        #    之前 icons 是空数组 → 页面看着一切正常，就是装不成 app。
        sizes = {(i.get("sizes"), i.get("purpose", "any")) for i in mf.get("icons", [])}
        check("manifest 图标非空", lambda: eq(len(mf.get("icons", [])) >= 4, True))
        check("有 any 192x192", lambda: eq(("192x192", "any") in sizes, True))
        check("有 any 512x512", lambda: eq(("512x512", "any") in sizes, True))
        check("有 maskable 图标", lambda: eq(any(p == "maskable" for _, p in sizes), True))

        # 图标文件必须真的存在、且真实尺寸与声明一致 ——
        # 用 192 的图冒充 512，浏览器同样会拒绝，而且不报错。
        for src, want in (
            ("icon-192.png", (192, 192)),
            ("icon-512.png", (512, 512)),
            ("icon-maskable-192.png", (192, 192)),
            ("icon-maskable-512.png", (512, 512)),
            ("apple-touch-icon.png", (180, 180)),
        ):
            check(f"图标 {src} 真实尺寸 {want[0]}px",
                  (lambda s, w: (lambda: eq(_png_size(pub / "static" / "icons" / s), w)))(src, want))

        # iOS 不认 manifest.icons，只认 apple-touch-icon 这个 link
        check("外壳含 apple-touch-icon", lambda: has('rel="apple-touch-icon"', home))
        check("外壳含 manifest link", lambda: has('rel="manifest"', home))
        check("外壳含 apple-mobile-web-app-capable",
              lambda: has("apple-mobile-web-app-capable", home))

        # Service Worker 必须在**根目录**：放 /static/ 下作用域就只有 /static/**，
        # 管不到首页，等于白装。
        check("sw.js 在 public 根目录", lambda: eq((pub / "sw.js").exists(), True))
        sw = (pub / "sw.js").read_text(encoding="utf-8")
        check("sw.js 含 install 事件", lambda: has("'install'", sw))
        check("sw.js 含 fetch 事件", lambda: has("'fetch'", sw))
        check("sw.js 不缓存 /api/", lambda: has("'/api/'", sw))
        check("app.js 注册了 /sw.js", lambda: has('"/sw.js"', (pub / "static" / "app.js")
                                                  .read_text(encoding="utf-8")))

        check("robots.txt 禁止收录", lambda: has("Disallow: /",
                                              (pub / "robots.txt").read_text(encoding="utf-8")))
        check("manifest 有 shortcuts", lambda: eq(len(mf.get("shortcuts", [])) >= 2, True))
    except Exception as exc:  # noqa: BLE001
        import traceback
        print("  [!!] build_site 抛异常：")
        print(traceback.format_exc())
        FAILED.append("build_site")

    shutil.rmtree(TMP, ignore_errors=True)

# ==================================================== 7. Web API
print("\n== 7. Web API ==")
if not HAVE_JINJA:
    print("  [skip] 缺依赖")
else:
    try:
        from radar.web import create_app
        flask_app = create_app(ROOT)
        client = flask_app.test_client()
        r = client.get("/api/status")
        check("GET /api/status 200", lambda: eq(r.status_code, 200))
        js = r.get_json()
        check("status 含 fingerprint", lambda: has("fingerprint", js))
        check("status 含 modules", lambda: eq(isinstance(js.get("modules"), list), True))
        check("status busy 是 bool", lambda: eq(isinstance(js.get("busy"), bool), True))

        r2 = client.get("/api/modules")
        check("GET /api/modules 200", lambda: eq(r2.status_code, 200))

        r3 = client.get("/nope/not-exist.html")
        check("404 兜底", lambda: eq(r3.status_code, 404))

        # PWA 资源必须真能取到，而且 /sw.js 得在**根路径** ——
        # 浏览器按"SW 脚本所在路径"决定作用域，只有根路径才能覆盖全站。
        for rel, ct in (("/manifest.json", "json"), ("/sw.js", "javascript")):
            rr = client.get(rel)
            check(f"GET {rel} 200", (lambda r: (lambda: eq(r.status_code, 200)))(rr))
            check(f"{rel} 内容类型 {ct}",
                  (lambda r, c: (lambda: has(c, r.headers.get("Content-Type", ""))))(rr, ct))
        ico = client.get("/static/icons/icon-512.png")
        check("GET 图标 200", lambda: eq(ico.status_code, 200))
        check("图标是 png", lambda: has("image/png", ico.headers.get("Content-Type", "")))

        # 访问口令：重点是"手机上能用" —— 带一次 ?t= 之后要靠 Cookie 自动续上，
        # 否则点导航跳到 /market_flow/ 就会 401
        import radar.web as web_mod
        _orig_load = web_mod.load
        _cfg3 = _orig_load(ROOT)
        _cfg3["server"] = dict(_cfg3["server"])
        _cfg3["server"]["token"] = "s3cret"
        web_mod.load = lambda root=None: _cfg3
        try:
            app3 = web_mod.create_app(ROOT)
            c3 = app3.test_client()
            check("无口令 → 401", lambda: eq(c3.get("/api/status").status_code, 401))
            r_t = c3.get("/?t=s3cret")
            check("带口令 → 200", lambda: eq(r_t.status_code, 200))
            check("种了 Cookie", lambda: has("radar_token", r_t.headers.get("Set-Cookie", "")))

            # ⭐ 装到主屏后 PWA 从 start_url 启动，地址里没有 ?t=，全靠这条 Cookie
            #    免口令。所以它必须带 Secure —— 否则会跟着明文 HTTP 请求一起发出去。
            #    TLS 由 nginx 终结，radar 看到的是明文，只能靠 X-Forwarded-Proto 判断。
            check("HTTP 下 Cookie 不带 Secure", lambda: eq(
                "Secure" in r_t.headers.get("Set-Cookie", ""), False))
            r_s = c3.get("/?t=s3cret", headers={"X-Forwarded-Proto": "https"})
            check("经反代 https 时 Cookie 带 Secure", lambda: has(
                "Secure", r_s.headers.get("Set-Cookie", "")))
            check("反代 https 下页面 200", lambda: eq(r_s.status_code, 200))
            check("Cookie 之后免口令", lambda: eq(c3.get("/api/status").status_code, 200))
            check("Cookie 之后页面可访问", lambda: eq(c3.get("/market_flow/").status_code in (200, 404), True))
            check("错误口令 → 401", lambda: eq(
                app3.test_client().get("/?t=wrong").status_code, 401))

            c4 = app3.test_client()
            check("请求头口令可用", lambda: eq(
                c4.get("/api/status", headers={"X-Radar-Token": "s3cret"}).status_code, 200))
        finally:
            web_mod.load = _orig_load

        app5 = web_mod.create_app(ROOT)          # token 为空 → 免校验
        check("无口令配置时直通", lambda: eq(app5.test_client().get("/api/status").status_code, 200))
        html_r = app5.test_client().get("/")
        check("HTML 不做强缓存", lambda: has("no-cache", html_r.headers.get("Cache-Control", "")))

        # ---------------- /dl/ 安卓壳下载（2026-09-18 新增）----------------
        # 这条路由要守住三件事：
        #   ① 只能下 .apk（别把仓库里的任何文件都暴露出去）
        #   ② 挡掉路径穿越
        #   ③ latest.apk 永远指向最新那个 —— 站点上的链接就不用跟着版本号改
        import os as _os
        import shutil as _shutil
        import tempfile as _tf2
        _dlroot = Path(_tf2.mkdtemp(prefix="radar_dl_"))
        (_dlroot / "apk").mkdir(parents=True)
        (_dlroot / "apk" / "radar-1.0.apk").write_bytes(b"PK\x03\x04old")
        (_dlroot / "apk" / "radar-1.1.apk").write_bytes(b"PK\x03\x04new")
        (_dlroot / "apk" / "notes.txt").write_bytes(b"nope")
        (_dlroot / "config.json").write_text('{"server": {"token": ""}}', encoding="utf-8")
        (_dlroot / "secret.txt").write_text("top", encoding="utf-8")
        # ⚠️ 显式把两个包的 mtime 拉开。连续 write_bytes 的 mtime 可能落在
        #    **同一个时间戳刻度**上（这里就是），那时"按 mtime 取最新"完全
        #    取决于 glob 的返回顺序 —— 测试会**靠运气通过**。
        #    这类"看起来在测、其实在赌"的断言最危险，所以这里把时间钉死。
        _t0 = 1_700_000_000
        _os.utime(_dlroot / "apk" / "radar-1.0.apk", (_t0, _t0))
        _os.utime(_dlroot / "apk" / "radar-1.1.apk", (_t0 + 60, _t0 + 60))
        # ⚠️ 服务器上跑测试时环境里有 RADAR_TOKEN，不清掉这段会全部 401
        #    （和"测试里 api_key_env 必须指向不存在的变量"是同一类坑）
        _saved_tok = _os.environ.pop("RADAR_TOKEN", None)
        try:
            cdl = web_mod.create_app(_dlroot).test_client()
            r_dl = cdl.get("/dl/latest.apk")
            check("/dl/latest.apk → 200", lambda: eq(r_dl.status_code, 200))
            check("latest 给的是最新那个",
                  lambda: has("radar-1.1.apk", r_dl.headers.get("Content-Disposition", "")))
            check("APK 的 MIME 是安卓安装包", lambda: eq(
                r_dl.headers.get("Content-Type", "").split(";")[0],
                "application/vnd.android.package-archive"))
            check("是下载而不是直接打开", lambda: has(
                "attachment", r_dl.headers.get("Content-Disposition", "")))
            check("按名字也能下", lambda: eq(
                cdl.get("/dl/radar-1.0.apk").status_code, 200))
            check("非 .apk 不让下", lambda: eq(
                cdl.get("/dl/notes.txt").status_code, 404))
            check("路径穿越被挡", lambda: eq(
                cdl.get("/dl/../secret.txt").status_code in (400, 404), True))
            check("不存在的包 → 404", lambda: eq(
                cdl.get("/dl/nope.apk").status_code, 404))

            # /app 是 /dl/latest.apk 的短别名 —— 手机上手敲链接用，
            # 行为必须和 /dl/latest.apk 完全一致，否则"短链"就成了另一个入口。
            r_app = cdl.get("/app")
            check("/app → 200", lambda: eq(r_app.status_code, 200))
            check("/app 给的是最新那个", lambda: has(
                "radar-1.1.apk", r_app.headers.get("Content-Disposition", "")))
            check("/app 也是下载而不是直接打开", lambda: has(
                "attachment", r_app.headers.get("Content-Disposition", "")))
            check("/app 的 MIME 是安卓安装包", lambda: eq(
                r_app.headers.get("Content-Type", "").split(";")[0],
                "application/vnd.android.package-archive"))

            # apk 目录里一个包都没有时，短链要 404 而不是 500
            # （比如换机器/刚部署还没传包的时候）
            _empty = Path(_tf2.mkdtemp(prefix="radar_dl_empty_"))
            (_empty / "config.json").write_text('{"server": {"token": ""}}',
                                                encoding="utf-8")
            try:
                check("apk 目录不存在时 /app → 404", lambda: eq(
                    web_mod.create_app(_empty).test_client().get("/app").status_code,
                    404))
                check("apk 目录不存在时 /api/app → 404", lambda: eq(
                    web_mod.create_app(_empty).test_client()
                    .get("/api/app").status_code, 404))
            finally:
                _shutil.rmtree(_empty, ignore_errors=True)

            # ---------------- /api/app：回答"壳自身的最新版本" ----------------
            # 和 /api/status 是两回事：status 管**内容**更新（站点轮询指纹自己
            # reload），app 管**壳**更新（App 拿 versionCode 比自己）。
            # 版本元数据来自构建时落下的 sidecar JSON —— versionCode
            # **不在文件名里**，光看文件名只能拿到 versionName。
            (_dlroot / "apk" / "radar-1.1.json").write_text(
                '{"versionCode": 3, "versionName": "1.1", "size": 11,'
                ' "sha256": "deadbeef", "built": "2026-09-18T00:00:00Z"}',
                encoding="utf-8")
            r_ver = cdl.get("/api/app")
            check("/api/app → 200", lambda: eq(r_ver.status_code, 200))
            _v = r_ver.get_json()
            check("报出 sidecar 里的 versionCode", lambda: eq(_v.get("versionCode"), 3))
            check("报出 versionName", lambda: eq(_v.get("versionName"), "1.1"))
            check("报出下载地址", lambda: eq(_v.get("url"), "/app"))
            check("带出 sha256 供手机核对", lambda: eq(_v.get("sha256"), "deadbeef"))

            # ⭐ "谁是最新版"**不能由上传顺序决定**。
            #    把旧包 1.0 的 mtime 改成比 1.1 还新，它也不能顶掉 1.1 ——
            #    否则哪天误传一个历史版本做留档，它就成了 `latest`，
            #    所有已装的 App **再也收不到更新提示**，而且不报任何错。
            #    所以排序优先看 versionCode，其次才是 mtime。
            (_dlroot / "apk" / "radar-1.0.json").write_text(
                '{"versionCode": 1, "versionName": "1.0"}', encoding="utf-8")
            _apk10 = _dlroot / "apk" / "radar-1.0.apk"
            _future = _apk10.stat().st_mtime + 3600
            _os.utime(_apk10, (_future, _future))
            check("旧包的 mtime 更新也不能当最新", lambda: has(
                "radar-1.1.apk",
                cdl.get("/app").headers.get("Content-Disposition", "")))
            check("/api/app 报的仍是 1.1 的 versionCode", lambda: eq(
                cdl.get("/api/app").get_json().get("versionCode"), 3))

            # ⭐ mtime **完全相同**且都没有 sidecar 时，结果也必须确定 ——
            #    否则"谁是线上最新版"就由 glob 的返回顺序（一个不受控的偶然量）
            #    决定。这条断言就是上面那个 bug 的真身。
            _tie = Path(_tf2.mkdtemp(prefix="radar_tie_"))
            (_tie / "apk").mkdir(parents=True)
            for _n in ("radar-1.0.apk", "radar-1.1.apk"):
                (_tie / "apk" / _n).write_bytes(b"PK\x03\x04x")
                _os.utime(_tie / "apk" / _n, (_t0, _t0))   # 同一时刻，故意打平
            (_tie / "config.json").write_text('{"server": {"token": ""}}',
                                              encoding="utf-8")
            try:
                _picked = set()
                for _ in range(5):
                    _picked.add(web_mod.create_app(_tie).test_client()
                                .get("/app").headers.get("Content-Disposition", ""))
                check("mtime 打平时结果确定（不靠 glob 顺序）",
                      lambda: eq(len(_picked), 1))
                check("mtime 打平时选版本名更高的那个",
                      lambda: eq(_picked.pop().count("radar-1.1.apk"), 1))
            finally:
                _shutil.rmtree(_tie, ignore_errors=True)

            # 没有 sidecar 的包按 versionCode=0 算 —— 只会排在后面，
            # 绝不能因为"它是刚传的"就当成最新（失败方向必须安全）
            _noside2 = Path(_tf2.mkdtemp(prefix="radar_nc_"))
            (_noside2 / "apk").mkdir(parents=True)
            (_noside2 / "apk" / "radar-1.1.apk").write_bytes(b"PK\x03\x04old")
            (_noside2 / "apk" / "radar-1.1.json").write_text(
                '{"versionCode": 3}', encoding="utf-8")
            _later = _noside2 / "apk" / "radar-9.9.apk"      # 名字更唬人、mtime 更新
            _later.write_bytes(b"PK\x03\x04new")
            (_noside2 / "config.json").write_text('{"server": {"token": ""}}',
                                                  encoding="utf-8")
            try:
                check("没 sidecar 的包不会因为刚传就当最新", lambda: has(
                    "radar-1.1.apk",
                    web_mod.create_app(_noside2).test_client()
                    .get("/app").headers.get("Content-Disposition", "")))
            finally:
                _shutil.rmtree(_noside2, ignore_errors=True)

            # 没有 sidecar 时必须**保守**：versionCode 退化为 0（App 侧比的是
            # "服务器 code > 本地 code 才提示"，所以 0 只会导致不提示）。
            # 失败方向要安全 —— 宁可不提示，也不能误报"有新版本"。
            _noside = Path(_tf2.mkdtemp(prefix="radar_noside_"))
            (_noside / "apk").mkdir(parents=True)
            (_noside / "apk" / "radar-1.2.apk").write_bytes(b"PK\x03\x04x")
            (_noside / "config.json").write_text('{"server": {"token": ""}}',
                                                 encoding="utf-8")
            try:
                _v2 = web_mod.create_app(_noside).test_client().get("/api/app").get_json()
                check("无 sidecar 时 versionCode 退化为 0（不会误报有新版）",
                      lambda: eq(_v2.get("versionCode"), 0))
                check("无 sidecar 时 versionName 从文件名抠",
                      lambda: eq(_v2.get("versionName"), "1.2"))
            finally:
                _shutil.rmtree(_noside, ignore_errors=True)

            # 口令是硬门槛：APK 里嵌着访问口令，绝不能公开可下
            (_dlroot / "config.json").write_text(
                '{"server": {"token": "s3cret"}}', encoding="utf-8")

            # ⚠️ 每个"无口令"断言都要用**全新 client**：
            #    带 ?t= 请求过一次之后，_guard 会给响应种下 radar_token Cookie，
            #    同一个 client 的后续请求就"免口令"了（这正是装到手机上之后
            #    不用再输口令的机制）。若共用 client，"无口令"断言会假通过。
            check("无口令下 APK → 401", lambda: eq(
                web_mod.create_app(_dlroot).test_client()
                .get("/dl/latest.apk").status_code, 401))
            check("无口令下 /app → 401", lambda: eq(
                web_mod.create_app(_dlroot).test_client()
                .get("/app").status_code, 401))
            check("无口令下 /api/app → 401", lambda: eq(
                web_mod.create_app(_dlroot).test_client()
                .get("/api/app").status_code, 401))

            cdl2 = web_mod.create_app(_dlroot).test_client()
            check("带口令下 APK → 200", lambda: eq(
                cdl2.get("/dl/latest.apk?t=s3cret").status_code, 200))
            check("带口令下 /app → 200", lambda: eq(
                cdl2.get("/app?t=s3cret").status_code, 200))
            # 种过 Cookie 之后免口令也能继续下 —— 这就是"装到手机后不用重输口令"
            # 的机制本身，所以专门断言一次，别以后被当成漏洞顺手"修"掉。
            check("种过 Cookie 后免口令也能下", lambda: eq(
                cdl2.get("/app").status_code, 200))
        finally:
            if _saved_tok is not None:
                _os.environ["RADAR_TOKEN"] = _saved_tok
            _shutil.rmtree(_dlroot, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001
        import traceback
        print("  [!!] Web API 抛异常：")
        print(traceback.format_exc())
        FAILED.append("web")

# ==================================================== 8. 翻译（translate）
# 这一段全部**不联网**：翻译接口的通断不该由单元测试来验（会 flaky），
# 这里只固定住纯函数与降级行为 —— 而"降级不抛异常"恰恰是最要紧的那条，
# 因为流水线是无人值守跑的，翻译接口挂了不能让整轮采集崩掉。
print("\n== 8. 翻译（translate）==")
try:
    from radar.core.translate import (Translator, _fix_acronyms, _fix_mixed,
                                      _cjk_ratio, _repair_brands)

    # 缩写大写：这里曾经因为用 \b 做边界而失效 —— 中文也算 \w，
    # 导致 `面向llm的` 里匹配不到 `llm`。改用环视后正常。
    check("缩写：中文紧邻也能大写",
          lambda: eq(_fix_acronyms("面向llm的高吞吐量推理引擎"), "面向LLM的高吞吐量推理引擎"))
    check("缩写：gpu → GPU", lambda: eq(_fix_acronyms("一个gpu加速的终端"), "一个GPU加速的终端"))
    check("缩写：多个一起修", lambda: eq(
        _fix_acronyms("用 api 和 cli 处理 json"), "用 API 和 CLI 处理 JSON"))
    # 反向：不能误伤含缩写的普通单词
    check("缩写：不误伤 Airtable", lambda: eq(
        _fix_acronyms("Airtable 和 clips"), "Airtable 和 clips"))
    check("缩写：不误伤 clipper", lambda: eq(_fix_acronyms("clipper"), "clipper"))
    check("缩写：ios/macos 大小写", lambda: eq(
        _fix_acronyms("ios 和 macos"), "iOS 和 macOS"))
    check("缩写：多词短语 android tv", lambda: eq(
        _fix_acronyms("browse on android tv"), "browse on Android TV"))
    check("缩写：bittorrent 大小写", lambda: eq(
        _fix_acronyms("for bittorrent users"), "for BitTorrent users"))

    # 品牌名定向修复：机翻把普通词形态的产品名按字面翻（Intercom → 对讲机）。
    # 任何"标记保护"写法都救不了（实测 <b>、方括号、大写全失效），只能事后修。
    check("品牌修复：Intercom 回来了",
          lambda: eq(_repair_brands("开源替代对讲机", "Open alternative to Intercom"),
                     "开源替代Intercom"))
    check("品牌修复：Notion 回来了",
          lambda: eq(_repair_brands("与概念一起工作", "works with Notion"),
                     "与Notion一起工作"))
    # ⭐ 防误伤：原文里根本没有 Intercom 时，不许动"对讲机"
    check("品牌修复：原文无该词则不修",
          lambda: eq(_repair_brands("开源替代对讲机", "Open alternative to Zendesk"),
                     "开源替代对讲机"))
    check("品牌修复：原文已有品牌名则不动",
          lambda: eq(_repair_brands("与 Notion 一起工作", "works with Notion"),
                     "与 Notion 一起工作"))
    check("品牌修复：空输入", lambda: eq(_repair_brands("", "Notion"), ""))

    # 排版清理：这里曾经因为 re.sub 不重叠扫描而留下"免费的 工具"这种空格
    check("排版：标点前后不留空格", lambda: eq(
        _fix_mixed("开源 ， 免费 的 工具"), "开源，免费的工具"))
    check("排版：汉字之间去空格", lambda: eq(_fix_mixed("免费的 工具"), "免费的工具"))
    check("排版：汉字后开括号紧贴", lambda: eq(
        _fix_mixed("免费的 工具 （ 测试 ）"), "免费的工具（测试）"))
    check("排版：百分号前不留空格", lambda: eq(
        _fix_mixed("50 % 的 提升"), "50% 的提升"))
    check("排版：保留中英之间的空格", lambda: eq(
        _fix_mixed("面向 LLM 的引擎"), "面向 LLM 的引擎"))
    check("排版：空串", lambda: eq(_fix_mixed(""), ""))

    # 中文占比判断：本来就是中文的不该再送去翻译（省额度、避免被翻坏）
    check("cjk 占比：纯中文", lambda: eq(_cjk_ratio("全中文描述") > 0.3, True))
    check("cjk 占比：纯英文", lambda: eq(_cjk_ratio("all english here") > 0.3, False))

    TMPT = Path(tempfile.mkdtemp(prefix="radar_tr_")) if HAVE_JINJA else None

    # 关闭开关 → 直接返回空
    t_off = Translator({"enabled": False}, http=None)
    check("禁用时不翻译", lambda: eq(t_off.to_zh("hello world"), ""))
    check("空串不翻译", lambda: eq(Translator({"enabled": True}, http=None).to_zh(""), ""))

    # 已有中文 → 原样返回，不发请求
    t = Translator({"enabled": True}, http=None)
    check("已是中文则原样返回", lambda: eq(t.to_zh("一个中文描述"), "一个中文描述"))

    # ⭐ 关键：所有 provider 都取不到时**不能抛异常**，只返回空串，由调用方退回英文。
    #    http=None 会让 provider 内部报错，正好模拟"接口全挂"。
    t_fail = Translator({"enabled": True, "providers": ["youdao", "mymemory"]}, http=None)
    check("接口全挂不抛异常", lambda: eq(t_fail.to_zh("some english text"), ""))
    check("失败计数被记录", lambda: eq(t_fail._stats["fail"], 1))

    # 缓存命中：命中时不该碰 http（此处 http=None，一旦真发请求就会炸）
    if TMPT:
        cache_file = TMPT / ".translate_cache.json"
        TEXT = "A high-throughput inference engine"
        seed = Translator({"enabled": True}, http=None, cache_path=cache_file)
        seed._cache[seed._key(TEXT)] = "高吞吐量推理引擎"
        seed._dirty = True
        seed.save_cache()
        check("缓存文件已落盘", lambda: eq(cache_file.exists(), True))

        t_hit = Translator({"enabled": True}, http=None, cache_path=cache_file)
        check("缓存命中不联网", lambda: eq(t_hit.to_zh(TEXT), "高吞吐量推理引擎"))
        check("缓存命中计数", lambda: eq(t_hit._stats["hit"], 1))
        check("缓存命中不发请求", lambda: eq(t_hit._stats["new"], 0))
        # 缓存损坏也要能自救（真实场景：写了一半断电）
        cache_file.write_text("{ 这不是合法 json", encoding="utf-8")
        check("缓存损坏不抛异常", lambda: eq(
            Translator({"enabled": True}, http=None, cache_path=cache_file)._cache, {}))
        shutil.rmtree(TMPT, ignore_errors=True)

    # 渠道签名：换渠道要能失效，**换模型也要**。
    # 只写 "llm" 的话，从 deepseek-flash 换成 deepseek-v4-pro 时翻译缓存照样命中，
    # 等于"换了模型但译文还是旧模型翻的" —— 和"换渠道不失效"是同一类 bug。
    class _FakeLLMObj:
        model = "deepseek-flash"
        available = True

    _t_sig = Translator({"enabled": True}, http=None, llm=_FakeLLMObj())
    check("大模型翻译的签名带模型名", lambda: has("deepseek-flash", _t_sig.sig))
    check("签名能看出是大模型渠道", lambda: has("llm:", _t_sig.sig))
    check("免 key 渠道签名不带 llm",
          lambda: eq(Translator({"enabled": True}, http=None).sig, "youdao+mymemory"))
    # 同一个模型 → 同一个签名；换模型 → 必须变（否则缓存不失效）
    _sig_a = Translator({"enabled": True}, http=None, llm=_FakeLLMObj()).sig

    class _OtherLLM:
        model = "deepseek-v4-pro"
        available = True

    _sig_b = Translator({"enabled": True}, http=None, llm=_OtherLLM()).sig
    check("换模型签名必须变化", lambda: eq(_sig_a != _sig_b, True))
except NameError as exc:
    print(f"  [skip] 缺前置变量：{exc}")
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 翻译模块抛异常：")
    print(traceback.format_exc())
    FAILED.append("translate")

# ==================================================== 9. 流水线接线
# 这类 bug 用普通单测抓不到（要真跑采集），但**接线错了代价很大**：
# `radar run <一个模块>` 曾经只把该模块交给 build_site，而 build_site 会先清空
# public/ 再整站生成 → 其它模块的页面被连带删光，页面上凭空少一个 Tab。
# 所以这里直接对"接线"本身做断言。
#
# ⚠️ 用**读源码 + ast** 而不是 import 进来检查：导入 runner 会连带 import
#    render → jinja2，于是"没装 jinja2 的机器上这一段必然失败"，而它检查的
#    本来就是源码结构、根本不需要运行时依赖。改成静态分析后哪台机器都能跑。
print("\n== 9. 流水线接线（渲染必须覆盖全部模块）==")
try:
    import ast
    from pathlib import Path

    _ROOT = Path(__file__).resolve().parent
    src_run = (_ROOT / "radar" / "core" / "runner.py").read_text(encoding="utf-8")
    src_cmd = (_ROOT / "radar" / "__main__.py").read_text(encoding="utf-8")

    def _func_args(src: str, fname: str):
        """返回 (参数名列表, {参数名: 默认值源码})。默认值只看字面量 None。"""
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.FunctionDef) and node.name == fname:
                names = [a.arg for a in node.args.args]
                defaults: dict = {}
                # defaults 与 args 的**尾部**对齐
                tail = names[len(names) - len(node.args.defaults):]
                for name, d in zip(tail, node.args.defaults):
                    defaults[name] = (d.value if isinstance(d, ast.Constant) else "<expr>")
                return names, defaults
        raise AssertionError(f"找不到函数 {fname}")

    _names, _defaults = _func_args(src_run, "run_all")
    check("run_all 支持 all_modules 参数", lambda: eq("all_modules" in _names, True))
    check("all_modules 默认为 None（不传时行为不变）",
          lambda: eq(_defaults.get("all_modules"), None))

    check("索引按全部模块构建", lambda: has("site_modules", src_run))
    check("站点按全部模块重建", lambda: has(
        "build_site(cfg, app, site_modules", src_run))
    check("清理也按全部模块", lambda: has("for module in site_modules", src_run))

    check("cmd_run 传入了全部模块", lambda: has("all_modules=everything", src_cmd))
    check("cmd_run 先取全部模块再筛选", lambda: has("_select(everything", src_cmd))
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 流水线接线检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("runner-wiring")

# ==================================================== 10. 大模型客户端
# 这一段全部用假的 _post，**不联网、不花钱**。要验证的是"出错时会不会炸"，
# 而不是"模型答得好不好" —— 后者只能靠真跑。
print("\n== 10. 大模型客户端（llm）==")
try:
    import json as _json                                      # noqa: F401
    import tempfile as _tf
    import urllib.error

    from radar.core.llm import LLM, PROVIDERS, _strip_fence

    for name in ("deepseek", "openai", "local"):
        check(f"内置供应商 {name}", lambda n=name: eq(n in PROVIDERS, True))

    # ⚠️ 测试里必须把 api_key_env 指向一个不存在的变量名。
    #    否则在配了 RADAR_LLM_KEY 的机器（比如服务器）上，下面这些"没 key"
    #    的断言会真的读到一个真 key，然后发真请求 —— 测试就得花钱了。
    _NOKEY = {"enabled": True, "provider": "deepseek", "api_key_env": "RADAR_LLM_KEY_NOT_SET"}
    _KEYED = {**_NOKEY, "api_key": "sk-test"}

    l_off = LLM({"enabled": False, "api_key": "sk-x", "api_key_env": "RADAR_LLM_KEY_NOT_SET"})
    check("未启用时 available 为假", lambda: eq(l_off.available, False))
    check("未启用时 chat 返回空串", lambda: eq(l_off.chat("s", "u"), ""))
    check("未启用的原因可读", lambda: has("enabled", l_off.why_unavailable()))

    l_nokey = LLM(_NOKEY)
    check("没 key 时 available 为假", lambda: eq(l_nokey.available, False))
    check("没 key 时 chat 直接返回空串（不发请求）",
          lambda: eq(l_nokey.chat("s", "u"), ""))
    check("没 key 的提示指向环境变量名",
          lambda: has("RADAR_LLM_KEY_NOT_SET", l_nokey.why_unavailable()))

    l = LLM(_KEYED)
    check("有 key 即 available", lambda: eq(l.available, True))
    check("deepseek 默认端点", lambda: eq(l.base_url, "https://api.deepseek.com"))
    # ⚠️ 默认模型是 deepseek-flash。设成 deepseek-chat 也能通（服务端别名路由到
    #    flash），但那是"碰巧能用"，写清楚才不会被某天的路由变动搞懵。
    check("deepseek 默认模型是 flash", lambda: eq(l.model, "deepseek-flash"))

    calls = {"n": 0}

    def _ok(payload):
        calls["n"] += 1
        return {"choices": [{"message": {"content": '{"tagline":"x"}'}}],
                "usage": {"total_tokens": 42}}

    l._post = _ok
    check("chat 返回正文", lambda: eq(l.chat("s", "u1"), '{"tagline":"x"}'))
    check("chat_json 解析成 dict", lambda: eq(l.chat_json("s", "u2"), {"tagline": "x"}))
    _n_before = calls["n"]
    check("同一提示第二次不请求", lambda: eq((l.chat("s", "u1"), calls["n"]), ('{"tagline":"x"}', _n_before)))
    check("缓存命中被计数", lambda: eq(l._stats["hit"] >= 1, True))
    check("返回内容被计数", lambda: eq(l._stats["new"] >= 2, True))
    check("token 用量被累计", lambda: eq(l._stats["tokens"] > 0, True))
    check("统计行含模型名", lambda: has("deepseek", l.summary_line()))

    # ---- 思考 token 会被单独统计（它是成本大头，也是"正文为空"的唯一线索）
    l._post = lambda p: {"choices": [{"message": {"content": "答案"},
                                      "finish_reason": "stop"}],
                         "usage": {"total_tokens": 100,
                                   "completion_tokens_details": {"reasoning_tokens": 70}}}
    _r_before = l._stats["reasoning"]
    check("带思考的返回能取到正文", lambda: eq(l.chat("s", "u-reason"), "答案"))
    check("思考 token 被单独累计", lambda: eq(l._stats["reasoning"] - _r_before, 70))
    check("统计行报出思考 token", lambda: has("思考 70", l.summary_line()))

    # ⭐ 核心容错：思考把 max_tokens 吃光 → finish_reason=length + 正文为空。
    #    这不是报错，是"安静地什么都没写出来"，必须自动加预算重试一次。
    #    （实测 deepseek-flash：max_tokens=200 → 正文空、思考 200）
    starve = {"n": 0, "budgets": []}

    def _starved(payload):
        starve["n"] += 1
        starve["budgets"].append(payload["max_tokens"])
        if starve["n"] == 1:
            return {"choices": [{"message": {"content": ""},
                                 "finish_reason": "length"}],
                    "usage": {"total_tokens": 300,
                              "completion_tokens_details": {"reasoning_tokens": 300}}}
        return {"choices": [{"message": {"content": '{"tagline":"救回来了"}'},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 500,
                          "completion_tokens_details": {"reasoning_tokens": 100}}}

    l._post = _starved
    check("思考吃光预算后自动加预算重试",
          lambda: eq(l.chat("s", "u-starve", max_tokens=200), '{"tagline":"救回来了"}'))
    check("重试确实发生了", lambda: eq(starve["n"], 2))
    check("重试时把预算放大了", lambda: eq(starve["budgets"][1] > starve["budgets"][0], True))
    check("加预算次数被计数", lambda: eq(l._stats["bumped"] >= 1, True))
    check("统计行报出加预算次数", lambda: has("加预算", l.summary_line()))

    # 重试也失败时不能把空串写进缓存（否则以后永远吃这条空缓存）
    _empty_cache = LLM(_KEYED, cache_path=None)
    _empty_cache._post = lambda p: {"choices": [{"message": {"content": ""},
                                                 "finish_reason": "length"}],
                                    "usage": {"completion_tokens_details": {"reasoning_tokens": 99}}}
    check("一直空正文则返回空串", lambda: eq(_empty_cache.chat("s", "u-empty"), ""))
    check("空正文不进缓存", lambda: eq(len(_empty_cache._cache), 0))

    # 预算放大有上限，不能一路翻倍把额度烧穿
    _cap = LLM({**_KEYED, "max_retry_tokens": 500})
    _cap_calls = {"n": 0}

    def _cap_post(payload):
        _cap_calls["n"] += 1
        _cap_calls["mt"] = payload["max_tokens"]
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 400}}}

    _cap._post = _cap_post
    check("加预算不超过 max_retry_tokens",
          lambda: eq((_cap.chat("s", "u-cap", max_tokens=200), _cap_calls["mt"]), ("", 500)))

    def _boom(payload):
        raise TimeoutError("读超时")

    l._post = _boom
    check("调用失败返回空串而不是抛异常", lambda: eq(l.chat("s", "u-fail"), ""))
    check("失败被计数", lambda: eq(l._stats["fail"] >= 1, True))

    # 有些网关不认 response_format，报 400 → 应该自动摘掉参数重试一次
    seq = {"n": 0}

    def _flaky(payload):
        seq["n"] += 1
        if "response_format" in payload:
            raise urllib.error.HTTPError("http://x", 400, "bad request", {}, None)
        return {"choices": [{"message": {"content": "OK"}}], "usage": {}}

    l._post = _flaky
    check("模型不支持 JSON 模式时退回普通输出",
          lambda: eq(l.chat("s", "u-400", json_mode=True), "OK"))
    check("确实只重试了一次", lambda: eq(seq["n"], 2))

    def _wrapped(payload):
        return {"choices": [{"message": {"content": '好的：{"tagline":"y"} 完毕'}}], "usage": {}}

    l._post = _wrapped
    check("JSON 前后夹着废话也能解析",
          lambda: eq(l.chat_json("s", "u-wrap"), {"tagline": "y"}))

    l._post = lambda p: {"choices": [{"message": {"content": "完全不是 JSON"}}], "usage": {}}
    check("不是 JSON 返回 None", lambda: eq(l.chat_json("s", "u-junk"), None))

    l._post = lambda p: {"choices": [{"message": {"content": '{"a":1}'}}], "usage": {}}
    check("缺必需字段返回 None",
          lambda: eq(l.chat_json("s", "u-miss", required=("tagline",)), None))
    check("字段齐全才返回 dict",
          lambda: eq(l.chat_json("s", "u-miss2", required=("a",)), {"a": 1}))

    # ⭐⭐ JSON 被截断 → 加预算重来一次。
    #    为什么 chat() 那道兜底拦不住：那道只在「正文**为空**」时才触发，
    #    而截断时正文是**非空但不合法**的（写到一半被 length 切断）。
    #    实测就有一个仓库（根目录最长，666 字符）因为这样整段介绍没生成。
    jseq = {"budgets": [], "users": []}

    def _trunc(payload):
        jseq["budgets"].append(payload["max_tokens"])
        jseq["users"].append(payload["messages"][1]["content"])
        if len(jseq["budgets"]) == 1:          # 第一次：JSON 写一半就被截断
            return {"choices": [{"message": {"content": '{"tagline":"半截'},
                                 "finish_reason": "length"}],
                    "usage": {"total_tokens": 900,
                              "completion_tokens_details": {"reasoning_tokens": 300}}}
        return {"choices": [{"message": {"content": '{"tagline":"完整","detail":"好了"}'},
                             "finish_reason": "stop"}],
                "usage": {"total_tokens": 600,
                          "completion_tokens_details": {"reasoning_tokens": 100}}}

    l._post = _trunc
    check("JSON 被截断时加预算重来",
          lambda: eq(l.chat_json("s", "u-trunc", max_tokens=500, required=("tagline",)),
                     {"tagline": "完整", "detail": "好了"}))
    check("确实重来了第二次", lambda: eq(len(jseq["budgets"]), 2))
    check("重来时把预算放大了", lambda: eq(jseq["budgets"][1], 2000))
    check("重试时给模型加了「被截断」的提示",
          lambda: has("截断了", jseq["users"][1]))
    check("JSON 截断被计数", lambda: eq(l._stats["json_bumped"] >= 1, True))
    check("统计行报出 JSON 截断重来", lambda: has("JSON 截断", l.summary_line()))

    # ⭐ 成功结果必须写回「原始提示词」的缓存位。
    #    否则每轮都是"先失败一次 → 再带提示重试一次"，每天两轮白多花两次请求。
    #    这条很值钱，别在重构时弄丢。
    _jround1 = len(jseq["budgets"])
    check("重试成功后结果被写回原提示词的缓存位",
          lambda: eq(l.chat_json("s", "u-trunc", max_tokens=500, required=("tagline",)),
                     {"tagline": "完整", "detail": "好了"}))
    check("第二轮回合零请求（直接吃缓存）",
          lambda: eq(len(jseq["budgets"]), _jround1))

    # JSON 那条路的预算上限同样要生效，不能一路翻倍烧额度
    _jcap_budgets = []

    def _jcap_post(payload):
        _jcap_budgets.append(payload["max_tokens"])
        return {"choices": [{"message": {"content": "{还是截断"},
                             "finish_reason": "length"}], "usage": {}}

    _jcap = LLM({**_KEYED, "max_tokens": 200, "max_retry_tokens": 400},
                cache_path=None)
    _jcap._post = _jcap_post
    check("JSON 重试也不超过 max_retry_tokens",
          lambda: eq((_jcap.chat_json("s", "u-jcap", required=("tagline",)),
                      _jcap_budgets), (None, [200, 400])))

    # 空正文说明 chat() 那边已经加预算试过且确实失败 → 不该在这里再花一次钱
    _jempty = {"n": 0}

    def _jempty_post(payload):
        _jempty["n"] += 1
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 500}}}

    _jempty_llm = LLM({**_KEYED, "max_retry_tokens": 200}, cache_path=None)
    _jempty_llm._post = _jempty_post
    check("chat_json 遇到空正文直接放弃，不重复重试",
          lambda: eq((_jempty_llm.chat_json("s", "u-jempty"), _jempty["n"]), (None, 1)))

    # ⭐⭐ 模型把答案套了一层"信封"。实测 deepseek-flash 在 10 个仓库里出现 1 个：
    #     {"type":"json_object","content":{真正的字段}}
    #     因为 response_format 被它理解成了"要输出的内容格式"。
    #     字段全在、只是层级错了 —— 重试可能照样再套一层，所以必须在解析这层剥掉。
    l._post = lambda p: {"choices": [{"message": {"content":
        '{"type":"json_object","content":{"tagline":"套了壳","detail":"剥掉"}}'}}],
        "usage": {}}
    check("带 type/content 信封的回复能剥出来",
          lambda: eq(l.chat_json("s", "u-env1", required=("tagline", "detail")),
                     {"tagline": "套了壳", "detail": "剥掉"}))

    l._post = lambda p: {"choices": [{"message": {"content":
        '{"data":{"tagline":"a","detail":"b"}}'}}], "usage": {}}
    check("data 信封也能剥",
          lambda: eq(l.chat_json("s", "u-env2", required=("tagline", "detail")),
                     {"tagline": "a", "detail": "b"}))

    l._post = lambda p: {"choices": [{"message": {"content":
        '{"任意一个键":{"tagline":"a","detail":"b"}}'}}], "usage": {}}
    check("只有一个内层对象时也能剥（信封键名不固定）",
          lambda: eq(l.chat_json("s", "u-env3", required=("tagline",)),
                     {"tagline": "a", "detail": "b"}))

    # 不该乱剥：顶层本来就齐全 → 保持原样（否则会把正常数据吃掉）
    l._post = lambda p: {"choices": [{"message": {"content":
        '{"tagline":"a","detail":"b","extra":{"x":1}}'}}], "usage": {}}
    check("顶层字段齐全时不剥信封（不误伤正常回复）",
          lambda: eq(l.chat_json("s", "u-env4", required=("tagline",))["extra"],
                     {"x": 1}))

    # 内层也凑不齐 required → 仍判失败（别把不相干的 dict 当成答案）
    l._post = lambda p: {"choices": [{"message": {"content":
        '{"meta":{"foo":1,"bar":2}}'}}], "usage": {}}
    check("内层凑不齐 required 时仍返回 None",
          lambda: eq(l.chat_json("s", "u-env5", required=("tagline", "detail")), None))

    l._post = lambda p: {"usage": {}}
    check("返回里没有 choices 时不炸", lambda: eq(l.chat("s", "u-nochoice"), ""))
    l._post = lambda p: {"choices": [{}], "usage": {}}
    check("choices 里没有 message 时不炸", lambda: eq(l.chat("s", "u-nomsg"), ""))

    check("围栏能剥掉", lambda: eq(_strip_fence('```json\n{"a":1}\n```'), '{"a":1}'))
    check("围栏能剥掉（无语言标记）", lambda: eq(_strip_fence('```\n{"a":1}\n```'), '{"a":1}'))

    # 磁盘缓存：重启后不该重花一遍钱
    _llmdir = Path(_tf.mkdtemp(prefix="radar_llm_"))
    _cpath = _llmdir / "llm.json"
    l1 = LLM(_KEYED, cache_path=_cpath)
    l1._post = _ok
    l1.chat("sys", "persist-me")
    l1.save_cache()
    check("缓存落盘", lambda: eq(_cpath.exists(), True))
    l2calls = {"n": 0}

    def _should_not_run(payload):
        l2calls["n"] += 1
        raise AssertionError("命中了缓存却还是发了请求")

    l2 = LLM(_KEYED, cache_path=_cpath)
    l2._post = _should_not_run
    check("换进程后命中磁盘缓存",
          lambda: eq(l2.chat("sys", "persist-me"), '{"tagline":"x"}'))
    check("命中磁盘缓存时零请求", lambda: eq(l2calls["n"], 0))
    _shutil.rmtree(_llmdir, ignore_errors=True)
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 大模型客户端检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("llm")

# ==================================================== 11. GitHub 代码上下文
print("\n== 11. GitHub 代码上下文（README / 根目录）==")
try:
    import tempfile as _tf2

    check("根目录：目录带斜杠、文件不带",
          lambda: eq(gt.format_tree([{"name": "app", "type": "dir"},
                                     {"name": "a.py", "type": "file"}]), "app/ a.py"))
    check("根目录：空输入返回空串", lambda: eq(gt.format_tree([]), ""))
    check("根目录：缺 name 的条目跳过",
          lambda: eq(gt.format_tree([{"type": "file"}, {"name": "ok.py"}]),
                     "ok.py"))

    # ---- 403 的报错必须自带解法（否则看到 403 只会以为仓库不存在）
    _orig_request = gt._request
    gt._request = lambda *a, **k: (403, "")
    try:
        _meta403 = gt.fetch_repo_meta("a/b", "ua")
        check("403 时提示里带限额数字",
              lambda: has("60", _meta403.get("_error", "")))
        check("403 时提示里带 github_token 这个解法",
              lambda: has("github_token", _meta403.get("_error", "")))
    finally:
        gt._request = _orig_request

    # ---- README 截断 / 404 降级（都靠替换 _api，不联网）
    gt._api = lambda *a, **k: "x" * 100
    check("README 按 limit 截断",
          lambda: eq(len(gt.fetch_readme("a/b", "ua", limit=10)), 10))

    def _raise404(*a, **k):
        raise gt.SourceError("HTTP 404")

    gt._api = _raise404
    check("仓库没有 README → 空串而不是报错",
          lambda: eq(gt.fetch_readme("a/b", "ua"), ""))

    def _raise500(*a, **k):
        raise gt.SourceError("HTTP 500")

    gt._api = _raise500

    def _propagates():
        try:
            gt.fetch_readme("a/b", "ua")
            return "没抛"
        except gt.SourceError:
            return "抛了"

    check("其它错误照常抛出（交给上层降级）", lambda: eq(_propagates(), "抛了"))
    check("fetch_repo_ctx 失败也不抛异常",
          lambda: eq(isinstance(gt.fetch_repo_ctx("a/b", "ua"), dict), True))
    check("fetch_repo_ctx 记录了错误原因",
          lambda: eq(len(gt.fetch_repo_ctx("a/b", "ua").get("errors", [])) >= 1, True))

    # ---- 缓存：同一份代码只补一次额度
    gt._api = lambda path, *a, **k: (
        "READMECONTENT" if "readme" in path
        else '[{"name":"src","type":"dir"},{"name":"main.py","type":"file"}]')
    _ctxdir = Path(_tf2.mkdtemp(prefix="radar_ctx_"))
    _cache = gt.RepoContextCache(_ctxdir / "ctx.json")
    _c1 = gt.fetch_repo_ctx("o/r", "ua", cache=_cache, pushed_at="2026-09-13T00:00:00Z")
    check("拿到 README 正文", lambda: has("READMECONTENT", _c1.get("readme", "")))
    check("拿到并格式化根目录",
          lambda: eq(gt.format_tree(_c1.get("entries")), "src/ main.py"))
    _cache.save()
    _cache2 = gt.RepoContextCache(_ctxdir / "ctx.json")
    _c2 = gt.fetch_repo_ctx("o/r", "ua", cache=_cache2, pushed_at="2026-09-13T00:00:00Z")
    check("第二次读缓存（不再消耗 API 额度）",
          lambda: eq(_c2.get("_cached"), True))
    _c3 = gt.fetch_repo_ctx("o/r", "ua", cache=_cache2, pushed_at="2026-09-14T00:00:00Z")
    check("pushed_at 变了就重取（代码更新则总结跟着更新）",
          lambda: eq(bool(_c3.get("_cached")), False))

    _small = gt.RepoContextCache(_ctxdir / "small.json", max_entries=2)
    for _i in range(5):
        _small.put(f"o/r{_i}", "t", {"readme": "x", "entries": []})
    check("缓存条数被修剪（不会无限膨胀）", lambda: eq(len(_small._data), 2))
    _shutil.rmtree(_ctxdir, ignore_errors=True)
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 代码上下文检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("repo-context")

# ==================================================== 12. 大模型总结接线
print("\n== 12. 大模型总结（模块接线）==")
try:
    _mod_src = (ROOT / "radar/modules/github_trending/module.py").read_text(encoding="utf-8")
    _tpl_src = (ROOT / "radar/modules/github_trending/template.html").read_text(encoding="utf-8")

    check("collect 会建大模型实例", lambda: has("LLM(llm_cfg", _mod_src))
    check("翻译复用同一个大模型实例", lambda: has("llm=llm", _mod_src))
    check("collect 会取 README/根目录", lambda: has("fetch_repo_ctx", _mod_src))
    check("README 不落盘（只留摘要）", lambda: has("readme_excerpt", _mod_src))
    check("落盘前把 _readme 摘掉", lambda: has('it.pop("_readme"', _mod_src))
    check("模板渲染 AI 结论", lambda: has("ai_tagline", _tpl_src))
    check("模板渲染根目录", lambda: has("ai-tree", _tpl_src))

    gm = load_file("t_gt_module", ROOT / "radar/modules/github_trending/module.py")

    class _FakeLLM:
        model = "fake-model"

        def chat_json(self, system, user, max_tokens=None, required=()):
            return {"tagline": "一个测试项目", "detail": "它解决某个问题。", "usage": ""}

    class _BadLLM(_FakeLLM):
        def chat_json(self, system, user, max_tokens=None, required=()):
            return None

    _rich = {"full_name": "a/b", "description": "test", "_readme": "R" * 50,
             "_tree": "src/ main.py", "topics": ["x"], "stars": 100}
    check("资料齐全时产出中文简介",
          lambda: eq(gm._summarize_one(_FakeLLM(), _rich, 100).get("ai_tagline"),
                     "一个测试项目"))
    check("记录产出它的模型名",
          lambda: eq(gm._summarize_one(_FakeLLM(), _rich, 100).get("ai_model"),
                     "fake-model"))
    check("模型不返回 JSON 时退化为空", lambda: eq(gm._summarize_one(_BadLLM(), _rich, 100), {}))
    check("资料太少时不瞎编（连简介都没有）",
          lambda: eq(gm._summarize_one(_FakeLLM(), {"full_name": "a/b"}, 100), {}))
    check("usage 缺省时是空串而非 None",
          lambda: eq(gm._summarize_one(_FakeLLM(), _rich, 100).get("ai_usage"), ""))

    _card = gm._build_card({"full_name": "a/b", "description": "d",
                            "_readme": "R" * 10, "_tree": "src/ main.py",
                            "topics": ["x"], "stars": 100})
    check("资料卡带上 README", lambda: has("RRRRRRRRRR", _card))
    check("资料卡带上根目录", lambda: has("src/ main.py", _card))
    check("资料卡带上仓库名", lambda: has("a/b", _card))

    from radar.core.config import load as _load_cfg

    class _Ctx:
        config = _load_cfg(ROOT)
        date = "2026-09-13"

        def log_info(self, msg):
            pass

    _mod = gm.GithubTrending()
    _data = _mod.analyze({"items": [{
        "rank": 1, "full_name": "a/b", "description": "d",
        "ai_tagline": "T", "ai_detail": "D", "ai_usage": "U", "tree": "src/",
        "stars_growth": 10, "stars": 100,
    }]}, _Ctx())
    check("analyze 透传 AI 结论", lambda: eq(_data["repos"][0]["ai_tagline"], "T"))
    check("analyze 透传用法规劝", lambda: eq(_data["repos"][0]["ai_usage"], "U"))
    check("analyze 透传根目录", lambda: eq(_data["repos"][0]["tree"], "src/"))
    check("analyze 统计里有 with_ai", lambda: eq(_data["stats"]["with_ai"], 1))
    _rep = _mod.report(_data, _Ctx())
    check("报告里出现 AI 结论", lambda: has("T", _rep))
    check("报告里出现根目录", lambda: has("src/", _rep))
    # 没配 key 时 ai_* 全是空串，报告里**不能**冒出 "None" 这种字面量。
    # 注意 fixture 要给全 url —— 少了它 markdown 链接会写成 [a/b](None)，
    # 那是测试数据的问题，不是产品的问题。
    _plain = _mod.report(_mod.analyze(
        {"items": [{"rank": 1, "full_name": "a/b", "url": "https://github.com/a/b",
                    "description": "d"}]}, _Ctx()), _Ctx())
    check("没有 AI 数据时不冒出 None", lambda: eq("None" in _plain, False))

    # ⭐ 回归：数据目录里躺着**今天之前生成的历史快照**，它们没有 ai_* / tree 这些
    #    后加的键。report() 里只要有一处硬取 it["ai_tagline"]，"对旧数据重出报告"
    #    就会 KeyError —— 这正是加这一段时在服务器上真踩到的坑（第 6 段用
    #    GH_DATA 这个手写 fixture 直接触发）。老数据不该让新代码报错。
    import copy as _copy
    _legacy = _copy.deepcopy(_data)
    for _r in _legacy["repos"]:
        for _k in ("ai_tagline", "ai_detail", "ai_usage", "ai_model", "tree"):
            _r.pop(_k, None)

    def _report_legacy():
        _mod.report(_legacy, _Ctx())
        return "ok"

    check("旧数据（无 ai_* 字段）也能出报告", lambda: eq(_report_legacy(), "ok"))

    # ⭐ 回归：API 撞限额时，**不能用更差的数据覆盖上一份好数据**。
    #    实测踩过：一轮 enriched=10 / 上下文 10/10，下一轮额度耗尽 →
    #    enriched=0 / 上下文 0/10，把好数据盖掉了，页面上 topics、根目录、
    #    README 摘要全没了，而且看不出发生过什么。
    _old = {"topics": ["a", "b"], "license": "MIT", "pushed_at": "2026-09-13T00:00:00Z",
            "size_kb": 1234, "stars": 999, "forks": 12, "description": "旧描述",
            "readme_excerpt": "x"}
    _new = {"full_name": "a/b", "description": "来自榜单页的简介", "topics": []}

    _moved = gm._inherit(_new, _old)
    check("沿用：把缺失的 topics 补上", lambda: eq(_new["topics"], ["a", "b"]))
    check("沿用：把 pushed_at 补上（缓存 key 靠它）",
          lambda: eq(_new["pushed_at"], "2026-09-13T00:00:00Z"))
    check("沿用：补了许可证", lambda: eq(_new["license"], "MIT"))
    check("沿用：返回值列出补了哪些字段", lambda: eq(len(_moved) >= 5, True))
    check("沿用：**不覆盖**已有值（榜单页的简介优先）",
          lambda: eq(_new["description"], "来自榜单页的简介"))
    check("沿用：没有旧数据时什么也不做",
          lambda: eq(gm._inherit({"full_name": "a/b"}, None), []))
    _kept = {"description": "d", "topics": ["new"], "license": "Apache-2.0"}
    gm._inherit(_kept, _old)
    check("沿用：已有 topics 不被旧的覆盖", lambda: eq(_kept["topics"], ["new"]))
    check("沿用：已有 license 不被旧的覆盖", lambda: eq(_kept["license"], "Apache-2.0"))

    # 结构层面：collect 里必须真的接上了这套兜底，并有可见的提示
    check("collect 读取了上一轮数据", lambda: has("_load_prev_items", _mod_src))
    check("collect 调用了兜底", lambda: has("_inherit(it, prev.get", _mod_src))
    check("沿用了多少仓库会被记下来", lambda: has("from_prev", _mod_src))
    _tpl2 = (ROOT / "radar/modules/github_trending/template.html").read_text(encoding="utf-8")
    check("页面会提示：这次沿用了上一轮", lambda: has("from_prev", _tpl2))
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 大模型总结接线检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("summarize")

# ==================================================== 13. 安卓壳的更新链路
# 壳的"检查更新"属于**用户感知不到就等于没做**的那类功能。这里做静态结构检查
# （源码里该有的都在不在），真机行为交给 deploy/healthcheck.sh 兜。
#
# 背景：1.1 是"死版本"—— 它根本没有 checkUpdate()，所以装它的人永远收不到任何
# 升级提示。而 1.2 起虽然有了自动检查，却是**隐形**的（每 6 小时静默一次，
# 用户既看不到自己装的是哪版，也没法主动问）。所以 1.4 补了那个显式入口。
print("\n== 13. 安卓壳更新链路 ==")
try:
    _app_dir = ROOT / "app/android"
    _act = (_app_dir / "java/com/ypeak/radar/MainActivity.java").read_text(encoding="utf-8")
    _cfg_in = (_app_dir / "java/com/ypeak/radar/AppConfig.java.in").read_text(encoding="utf-8")
    _strs = (_app_dir / "res/values/strings.xml").read_text(encoding="utf-8")
    _build = (_app_dir / "build.sh").read_text(encoding="utf-8")
    _mani = (_app_dir / "AndroidManifest.xml").read_text(encoding="utf-8")

    # --- 1. 自动检查（1.2 起） ---
    check("壳里有 checkUpdate", lambda: has("private void checkUpdate", _act))
    check("壳里有 6 小时闸", lambda: has("UPDATE_CHECK_INTERVAL_MS", _act))
    check("壳里尊重「跳过此版本」", lambda: has('"skipped"', _act))
    check("壳里问的是 /api/app", lambda: has("API_APP", _cfg_in))

    # --- 2. ⭐ 主动检查（1.4 新增）：用户点一下就能查，且必须有反馈 ---
    check("有 JS 桥", lambda: has("addJavascriptInterface", _act))
    check("桥叫 RadarNative", lambda: has('"RadarNative"', _act))
    check("桥暴露 versionName", lambda: has("String versionName()", _act))
    check("桥暴露 versionCode", lambda: has("int versionCode()", _act))
    check("桥暴露 checkForUpdate", lambda: has("void checkForUpdate()", _act))
    check("桥方法都标了注解", lambda: eq(_act.count("@JavascriptInterface") >= 3, True))
    check("手动模式跳过 6 小时闸", lambda: has("if (!manual)", _act))
    check("手动模式失败有反馈", lambda: has("update_check_failed", _act))
    check("已是最新也有反馈", lambda: has("update_is_latest", _act))
    # 反馈文案必须真的在资源里：漏了会在运行时显示成空字符串，而且不报错
    check("strings 有最新提示", lambda: has('name="update_is_latest"', _strs))
    check("strings 有失败提示", lambda: has('name="update_check_failed"', _strs))
    # 桥方法在 JavaBridge 线程被调，碰 UI 必须回主线程
    check("桥回主线程", lambda: has("runOnUiThread", _act))

    # --- 3. 资源清理：销毁时摘桥，否则已死 Activity 被 JS 侧继续引用 ---
    check("销毁时移除桥", lambda: has('removeJavascriptInterface("RadarNative")', _act))

    # --- 4. 权限取向：下载交给系统浏览器 → 不该申请应用内安装权限 ---
    check("不申请应用内安装权限", lambda: eq("REQUEST_INSTALL_PACKAGES" in _mani, False))

    # --- 5. 版本前进（写侧）—— 否则线上"最新版"会被更旧的包顶掉 ---
    check("build.sh 版本自动递增", lambda: has("LAST_CODE + 1", _build))
    check("build.sh 拒绝倒退上传", lambda: has("--allow-stale", _build))
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 安卓壳检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("android-shell")

# ==================================================== 14. 读书模块（阅读器）
# 阅读器有大量"错了也不报错"的地方，这一段专门堵它们：
#   缺图 → 页面照样 200，只是图破掉
#   块级元素被塞进 <p> → 浏览器自己"修好"，看着正常
#   公式渲染失败 → 留白最难发现
#   书里自带的图注和 alt 重复 → 只是同一句话显示两遍
print("\n== 14. 读书模块（书架 / 阅读页）==")
try:
    import posixpath                                       # noqa: E402
    import re                                              # noqa: E402
    import importlib                                       # noqa: E402

    _rbooks = importlib.import_module("radar.modules.reader.books")
    _rrender = importlib.import_module("radar.modules.reader.render")

    # --- 清单与磁盘的一致性 -------------------------------------------------
    check("books 清单校验通过", lambda: eq(_rbooks.validate(cfg.static_dir), []))

    _chapters = []                                         # (book_slug, chap_slug, path)
    for _b in _rbooks.BOOKS:
        _d = _rbooks.book_dir(cfg.static_dir, _b)
        for _fn, _slug, _t in _b["chapters"]:
            _chapters.append((_b["slug"], _slug, _d / _fn))
    check("章节文件都在且不像 LFS 指针", lambda: eq(
        [str(p.name) for _, _, p in _chapters
         if not p.is_file() or p.stat().st_size < 200], []))
    # slug 只要求**书内**唯一：页面路径是 /reader/<slug>/<chap>.html，
    # 不同书之间重名（两本都有 "ch01"）是正常的。
    check("每本书内章节 slug 不重复（否则页面互相覆盖）", lambda: eq(
        [b["slug"] for b in _rbooks.BOOKS
         if len({c[1] for c in b["chapters"]}) != len(b["chapters"])], []))
    check("书名 slug 不重复（否则整本书互相覆盖）", lambda: eq(
        len({b["slug"] for b in _rbooks.BOOKS}), len(_rbooks.BOOKS)))
    check("每本书都钉了 commit", lambda: eq(
        all((b.get("commit") or "") for b in _rbooks.BOOKS), True))

    # --- 插图引用完整性：本条最有价值 ---------------------------------------
    # 书丢了图，页面**照样 200**，只是图破掉 —— 只有把引用和磁盘对一遍才发现。
    _miss = []
    for _b in _rbooks.BOOKS:
        _d = _rbooks.book_dir(cfg.static_dir, _b)
        for _fn, _slug, _t in _b["chapters"]:
            if not (_d / _fn).is_file():
                continue
            for _m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)",
                                  (_d / _fn).read_text(encoding="utf-8")):
                _rel = posixpath.normpath(_m.group(1).lstrip("./"))
                if not _d.joinpath(*_rel.split("/")).is_file():
                    _miss.append(f"{_b['slug']}/{_rel}")
    check("插图引用全都能在本地找到", lambda: eq(_miss[:5], []))

    # --- 渲染器回归 ---------------------------------------------------------
    # 独占一段的图必须输出**块级** <figure>：塞进 <p> 是非法结构，
    # 浏览器会自己"修好"，肉眼完全看不出问题 —— 所以只能靠断言守。
    _h, _ = _rrender.render("![短图注](images/a.svg)\n")
    check("独占一段的图输出 figure", lambda: has('<figure class="fig">', _h))
    check("figure 不被包进 <p>", lambda: eq("<p><figure" in _h, False))
    check("短 alt 当图注", lambda: has("<figcaption>", _h))

    _h, _ = _rrender.render("![alt](a.svg){height=55%}\n")
    check("pandoc 属性块不泄漏成正文", lambda: eq("height=55%" in _h, False))
    check("height 译成 max-height", lambda: has("max-height:55vh", _h))

    _h, _ = _rrender.render("如 ![图](a.svg) 所示。\n")
    check("段落中间的图用行内 img", lambda: has('<img class="fig-img"', _h))
    check("段落中间的图不套 figure", lambda: eq("<figure" in _h, False))

    _h, _ = _rrender.render("*图 2-1\u3000说明文字*\n")
    check("书稿图注段落单独成类", lambda: has('<p class="fig-cap">', _h))

    # 书稿自带图注时，不能再拿 alt 重复当图注（否则同一句话显示两遍）
    _h, _ = _rrender.render("![短标签](a.svg)\n\n*图 2-1\u3000真正的说明*\n")
    check("有书稿图注时 alt 不重复当图注", lambda: eq("<figcaption>" in _h, False))
    check("书稿图注仍然渲染出来", lambda: has('<p class="fig-cap">', _h))

    # --- 公式：能转就转，不能转必须**可见降级**，绝不留白 --------------------
    _h, _ = _rrender.render("$$\\frac{a}{b}$$\n")
    check("块级公式转成 MathML", lambda: has("<math", _h))
    check("残留 $$ 被清掉", lambda: eq(_h.count("$$"), 0))
    check("公式失败时可见降级（带 is-raw）", lambda: has("is-raw", _rrender._fallback("\\bad{", True)))
    check("公式失败时保留原文不留白", lambda: has("\\bad{", _rrender._fallback("\\bad{", True)))

    # --- 产物层（跑过 build 才有；没跑就跳过，不当失败） ---------------------
    _pub = ROOT / "public" / "reader"
    if _pub.is_dir():
        _want = ([_pub / "index.html"]
                 + [_pub / b["slug"] / "index.html" for b in _rbooks.BOOKS]
                 + [_pub / b / (s + ".html") for b, s, _ in _chapters])
        check("每本书 / 每章的页面都生成了", lambda: eq(
            [str(p.relative_to(ROOT)) for p in _want if not p.is_file()], []))

        _pages = sorted(_pub.rglob("*.html"))
        _html = "".join(p.read_text(encoding="utf-8") for p in _pages)
        check("产物里没有非法 <p><figure>", lambda: eq(
            len(re.findall(r"<p>\s*<figure", _html)), 0))
        check("产物里没有残留 $$", lambda: eq(_html.count("$$"), 0))
        check("产物里有 MathML 公式", lambda: has("<math", _html))
        check("产物里有块级图与图注", lambda: eq(
            _html.count('<figure class="fig">') > 0
            and _html.count('<figcaption>') > 0, True))
        _pmiss = [m.group(1) for m in
                  re.finditer(r'<img [^>]*src="(/static/[^"]+)"', _html)
                  if not (ROOT / "public" / m.group(1).lstrip("/")).is_file()]
        check("产物里的插图文件都存在", lambda: eq(_pmiss[:5], []))
    else:
        print("  [skip] 还没有 public/reader，跳过产物检查（先跑 python -m radar build）")

    # --- 前端：阅读进度的关键实现（少一段只是"少个能按的地方"，不报错） --------
    _appjs = (ROOT / "radar" / "static" / "app.js").read_text(encoding="utf-8")
    for _needle in ("radar.reading.v1", "setupReaderChapter", "setupReaderShelf",
                    "setupReaderBook", "read-bar-fill"):
        check(f"app.js 含 {_needle}", (lambda n: (lambda: has(n, _appjs)))(_needle))
    _css = (ROOT / "radar" / "static" / "style.css").read_text(encoding="utf-8")
    for _needle in (".book-content", "figure.fig", ".math-block", ".toc-sub[open]",
                    "--scroll-hint", "p.fig-cap"):
        check(f"style.css 含 {_needle}", (lambda n: (lambda: has(n, _css)))(_needle))
except Exception as exc:  # noqa: BLE001
    import traceback
    print("  [!!] 读书模块检查抛异常：")
    print(traceback.format_exc())
    FAILED.append("reader")

print("\n" + "=" * 52)
if FAILED:
    print(f"失败 {len(FAILED)} 项：{FAILED}")
    sys.exit(1)
print("全部通过")