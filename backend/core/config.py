"""应用配置加载。

配置唯一来源是 ``config/app.yaml``；本模块只做三件事：
1. 读取 YAML 并与内置默认值深合并（缺字段不至于启动失败）；
2. 把相对路径解析成基于仓库根目录的绝对路径；
3. 提供点号取值 ``get("backtest.is_ratio")``。

环境变量覆盖：``DELTAF_HOST`` / ``DELTAF_PORT`` / ``DELTAF_DEBUG``。
"""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

#: 仓库根目录（backend/core/config.py -> backend/core -> backend -> 根）
ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "app.yaml"

DEFAULTS: Dict[str, Any] = {
    "app": {"name": "DeltaFStation", "version": "2.0.0", "host": "0.0.0.0", "port": 5000, "debug": True},
    "paths": {
        "data": "data",
        "lake": "data/lake",
        "snapshots": "data/snapshots",
        "results": "data/results",
        "quality": "data/quality",
        "meta_db": "data/meta.db",
    },
    "universe": {"index": "hs300", "start_date": "2008-01-01", "backtest_years": 5, "exclude_st": True},
    "schedule": {"update_time": "18:00", "retry_until": "22:00", "retry_interval_min": 30, "enabled": True},
    "costs": {
        "commission": 0.00025,
        "min_commission": 5.0,
        "stamp_duty": 0.0005,
        "slippage": 0.0005,
        "lot_size": 100,
    },
    "scoring": {
        "default_version": "v1.0.0",
        "neutralize": "industry",
        "winsor_mad": 5.0,
        "min_industry_samples": 5,
    },
    "backtest": {
        "initial_cash": 1_000_000,
        "is_ratio": 0.7,
        "default_top_n": 20,
        "default_rebal_days": 20,
        "default_max_weight": 0.10,
        "benchmark": "sh.000300",
    },
}


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并：override 覆盖 base，返回新字典（不修改入参）。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


@dataclass(frozen=True)
class Paths:
    """解析后的绝对路径集合。"""

    root: Path
    data: Path
    lake: Path
    snapshots: Path
    results: Path
    quality: Path
    meta_db: Path

    def ensure(self) -> "Paths":
        """创建所有数据目录（幂等）。"""
        for path in (self.data, self.lake, self.snapshots, self.results, self.quality):
            path.mkdir(parents=True, exist_ok=True)
        self.meta_db.parent.mkdir(parents=True, exist_ok=True)
        return self


class Config:
    """配置对象：字典访问 + 路径解析。"""

    def __init__(self, raw: Dict[str, Any], root: Path = ROOT):
        self.raw = raw
        self.root = Path(root)
        p = raw.get("paths", {})
        self.paths = Paths(
            root=self.root,
            data=self.root / p["data"],
            lake=self.root / p["lake"],
            snapshots=self.root / p["snapshots"],
            results=self.root / p["results"],
            quality=self.root / p["quality"],
            meta_db=self.root / p["meta_db"],
        )

    def get(self, dotted: str, default: Any = None) -> Any:
        """按 ``"a.b.c"`` 取值，缺失返回 default。"""
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def host(self) -> str:
        return os.environ.get("DELTAF_HOST") or self.get("app.host", "0.0.0.0")

    @property
    def port(self) -> int:
        return int(os.environ.get("DELTAF_PORT") or self.get("app.port", 5000))

    @property
    def debug(self) -> bool:
        env = os.environ.get("DELTAF_DEBUG")
        if env is not None:
            return env.strip().lower() in ("1", "true", "yes", "on")
        return bool(self.get("app.debug", True))

    @property
    def version(self) -> str:
        return str(self.get("app.version", "2.0.0"))


def load_config(path: Path | str | None = None, root: Path = ROOT) -> Config:
    """读取 YAML 并与默认值合并；文件不存在时仅用默认值。"""
    target = Path(path) if path else CONFIG_FILE
    raw: Dict[str, Any] = {}
    if target.exists():
        with open(target, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    return Config(_deep_merge(DEFAULTS, raw), root=root)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """进程内单例配置。"""
    return load_config()
