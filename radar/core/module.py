"""模块插件协议 —— 整个应用的扩展点。

**加一个模块 = 加一个目录**：

    radar/modules/<name>/
        module.py        # 一个 Module 子类
        template.html    # 页面模板（可选，没有就用通用表格）

四个阶段，每个都能单独跑，便于排查：

    collect   拉原始数据        → data/<mod>/raw/<date>.json
    analyze   加工成结构化结果  → data/<mod>/<date>.json
    report    写出人读的结论    → data/<mod>/<date>.md
    render    渲染页面          → public/<mod>/<date>.html

**只有 collect 必须实现**，其余不写就用基类默认（原样透传 / 通用表格）。
这样新模块可以"先跑起来，再逐步丰富"。

`Context` 把配置、路径、HTTP 客户端、日志、运行日期一次性递给模块，
模块不用自己拼路径、不用自己建 HTTP 客户端，也就不会各行其是。
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_sibling(module_file: str, name: str):
    """加载与 module.py 同目录的辅助文件（如 sources.py）。

    模块目录不是 Python 包（registry 是按文件动态加载的），所以 `from . import x`
    用不了。这里给一个统一的替代品，模块里这样写：

        sources = load_sibling(__file__, "sources")
    """
    path = Path(module_file).with_name(f"{name}.py")
    if not path.exists():
        raise FileNotFoundError(f"模块同级文件不存在：{path}")
    key = f"radar_sibling_{path.parent.name}_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Context:
    """一次运行传给模块的全部环境。"""

    config: Any            # Config
    date: str              # 运行日期 YYYY-MM-DD
    data_dir: Path         # 项目 data/
    public_dir: Path       # 项目 public/
    http: Any              # HttpClient
    log: Any = None        # log(msg) 回调
    dry_run: bool = False
    log_traceback: bool = False   # 出错时是否打印完整堆栈（调试用）

    def log_info(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def module_dir(self, name: str) -> Path:
        path = self.data_dir / name
        (path / "raw").mkdir(parents=True, exist_ok=True)
        return path

    def raw_path(self, name: str, ext: str = "json") -> Path:
        return self.module_dir(name) / "raw" / f"{self.date}.{ext}"

    def data_path(self, name: str, ext: str = "json") -> Path:
        return self.module_dir(name) / f"{self.date}.{ext}"

    def repo_root(self) -> Path:
        return self.config.root


class Module:
    """所有模块的基类。子类至少要实现 collect()。"""

    name: str = ""            # 唯一标识，同时用作目录名与 URL 段
    title: str = ""           # 导航里显示的名字
    subtitle: str = ""        # 一句话说明
    order: int = 100          # 导航排序，小的在前
    schedule: str = ""        # 展示用，例如 "每日 08:00"

    # ---------------------------------------------------------------- 四个阶段
    def collect(self, ctx: Context) -> Any:
        """拉原始数据。必须实现。返回值会被 JSON 序列化落到 raw/。"""
        raise NotImplementedError(f"{type(self).__name__} 必须实现 collect()")

    def analyze(self, raw: Any, ctx: Context) -> Any:
        """把原始数据加工成结构化结果。默认原样透传。"""
        return raw

    def report(self, data: Any, ctx: Context) -> str:
        """生成人读的分析结论（Markdown）。默认不产出。"""
        return ""

    def extra_pages(self, page_ctx: dict) -> list[tuple[str, str]]:
        """可选：产出主页面之外的**额外页面**。默认没有。

        核心只负责"一个模块一个页面"（`public/<name>/`）。但有的模块天然是多页的
        —— 阅读器要"书房 → 单本书 → 章节"三层；监控模块可能要"总览 + 每个集群一页"。
        这个钩子就是那类模块的标准出口，**不用改核心、也不用自己往 public 里写文件**
        （构建时 `public/` 会被整体重建，自己写会被清掉）。

        返回 `[(public 相对路径, 完整 HTML 字符串), ...]`，例如：

            [("reader/ai-infra/index.html", html), ...]

        `page_ctx` 提供（都由核心组装好，模块不必知道 base.html 的细节）：

            env        Jinja Environment（可 from_string 渲染模块自己的模板）
            shell      base.html 的外壳变量（app/nav/asset_ver/built_at/today/fingerprint）
            wrap(body, **kw) -> str
                       把一段正文套进 base.html。kw 与 base.html 的变量同名，
                       常用 active / title / dates / date / is_latest
            index      站点索引（含各模块 hero/trend）
            cfg / data_dir / public_dir / module / dates / log

        两条约定：
          * 路径必须落在 public/ 内，且**不要**覆盖 `<name>/index.html`（核心写它）
          * 页面内容随构建冻结 —— 要保证"历史页面不可变"，靠的是**内容本身被钉住**
            （如阅读器把书稿按 commit 固定），而不是按日期分片
        """
        return []

    # ------------------------------------------------------------------ 元信息
    @classmethod
    def meta(cls) -> dict:
        return {
            "name": cls.name,
            "title": cls.title,
            "subtitle": cls.subtitle,
            "order": cls.order,
            "schedule": cls.schedule,
        }
