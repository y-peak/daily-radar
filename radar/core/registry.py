"""模块自动发现。

扫描 `radar/modules/*/module.py`，动态加载，找出 Module 子类并实例化。

新增模块不需要动核心一行代码 —— 这就是"加一个模块 = 加一个目录"的落点。

约定了两条硬规则：
    * 目录名必须等于类属性 `name`（防止 URL 与目录对不上，排查时抓狂）
    * 一个 module.py 只允许定义一个 Module 子类
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from .module import Module


class RegistryError(RuntimeError):
    pass


def _load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(f"radar_dyn_{name}", path)
    if spec is None or spec.loader is None:
        raise RegistryError(f"无法加载 {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def discover(modules_dir: Path) -> dict[str, Module]:
    """返回 {name: 实例}。发现不了就返回空字典，不抛异常（避免一个坏模块拖垮全站）。"""
    found: dict[str, Module] = {}
    if not modules_dir.is_dir():
        return found

    for folder in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
        if folder.name.startswith(("_", ".")):
            continue
        entry = folder / "module.py"
        if not entry.exists():
            continue
        try:
            py = _load_file(folder.name, entry)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 模块 {folder.name} 加载失败：{type(exc).__name__}: {exc}")
            continue

        classes = [
            obj for obj in vars(py).values()
            if isinstance(obj, type) and issubclass(obj, Module) and obj is not Module
        ]
        if not classes:
            print(f"  [warn] 模块 {folder.name} 里没找到 Module 子类，跳过")
            continue
        if len(classes) > 1:
            print(f"  [warn] 模块 {folder.name} 里有 {len(classes)} 个 Module 子类，"
                  f"只会用第一个 {classes[0].__name__}")

        cls = classes[0]
        if not cls.name:
            cls.name = folder.name
        if cls.name != folder.name:
            print(f"  [warn] 模块目录 {folder.name} 与类属性 name={cls.name!r} 不一致，"
                  f"以类属性为准")

        try:
            instance = cls()
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] 模块 {folder.name} 实例化失败：{type(exc).__name__}: {exc}")
            continue

        found[instance.name] = instance

    return found


def ordered(modules: dict[str, Module]) -> list[Module]:
    return sorted(modules.values(), key=lambda m: (m.order, m.name))


def template_of(modules_dir: Path, module: Module) -> Path | None:
    path = modules_dir / module.name / "template.html"
    return path if path.exists() else None
