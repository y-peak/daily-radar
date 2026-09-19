"""配置与路径解析。

所有相对路径都以项目根（含 config.json 的那层）为基准解析，
这样不管从哪个目录调用 CLI 都能正常工作。
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS: dict = {
    "app": {"name": "个人情报台", "short_name": "情报台", "version": "0.1.0"},
    "server": {"host": "0.0.0.0", "port": 18090},
    "storage": {"data_dir": "data", "public_dir": "public", "keep_days": 400},
    "http": {
        "user_agent": "Mozilla/5.0",
        "timeout": 15,
        "min_interval": 0.8,
        "retries": 3,
        "backoff": 1.6,
    },
    # 大模型默认**关**：没有 key 的机器上不该有任何"看起来配了其实不能用"的配置。
    # 想用就把 enabled 打开、把 key 放进 RADAR_LLM_KEY 环境变量（详见 core/llm.py）。
    # max_tokens 给 2000 是因为 deepseek 的模型带思考，reasoning 与正文共用预算。
    "llm": {
        "enabled": False,
        "provider": "deepseek",
        "model": "deepseek-flash",
        "api_key_env": "RADAR_LLM_KEY",
        "api_key": "",
        "timeout": 90,
        "max_tokens": 2000,
        "max_retry_tokens": 8000,
        "temperature": 0.3,
    },
    "modules": {},
}


def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config(dict):
    """配置对象。除字典访问外，附带项目根与几个常用目录。"""

    def __init__(self, data: dict, root: Path):
        super().__init__(data)
        self.root = root

    def section(self, name: str) -> dict:
        value = self.get(name) or {}
        return value if isinstance(value, dict) else {}

    def module_cfg(self, name: str) -> dict:
        value = self.section("modules").get(name) or {}
        return value if isinstance(value, dict) else {}

    @property
    def data_dir(self) -> Path:
        return self.root / self.section("storage").get("data_dir", "data")

    @property
    def public_dir(self) -> Path:
        return self.root / self.section("storage").get("public_dir", "public")

    @property
    def static_dir(self) -> Path:
        return self.root / "radar" / "static"

    @property
    def template_dir(self) -> Path:
        return self.root / "radar" / "templates"

    @property
    def modules_dir(self) -> Path:
        return self.root / "radar" / "modules"


def find_root(start: Path | None = None) -> Path:
    """从 start 所在位置向上找第一个含 config.json 的目录。"""
    cur = Path(start) if start else Path(__file__)
    cur = cur.resolve()
    if cur.is_file():
        cur = cur.parent
    for candidate in [cur, *cur.parents]:
        if (candidate / "config.json").exists():
            return candidate
    return Path.cwd()


def load(root: Path | None = None) -> Config:
    root = Path(root).resolve() if root else find_root()
    path = root / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return Config(deep_merge(DEFAULTS, raw), root)
