"""pytest 公共夹具：把配置指向临时目录，避免测试污染真实数据湖。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.config import load_config  # noqa: E402
from backend.core.lake import Lake  # noqa: E402
from backend.core.meta_db import MetaDB  # noqa: E402
from backend.core.snapshots import ScoreStore  # noqa: E402


@pytest.fixture()
def cfg(tmp_path):
    """基于临时根目录的配置（沿用仓库 config/app.yaml 的字段）。"""
    config = load_config(root=tmp_path)
    config.paths.ensure()
    return config


@pytest.fixture()
def lake(cfg):
    return Lake(cfg)


@pytest.fixture()
def db(cfg):
    return MetaDB(cfg).init()


@pytest.fixture()
def store(cfg):
    return ScoreStore(cfg)


@pytest.fixture()
def bars_df():
    """两标的、跨两个年度、共 6 行的最小行情样本。"""
    import pandas as pd

    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                [
                    "2023-12-28", "2023-12-29",
                    "2024-01-02", "2024-01-03",
                    "2023-12-28", "2024-01-02",
                ]
            ),
            "code": ["sh.600000", "sh.600000", "sh.600000", "sh.600000", "sz.000001", "sz.000001"],
            "open": [10.0, 10.2, 10.4, 10.1, 12.0, 12.3],
            "high": [10.5, 10.6, 10.7, 10.4, 12.4, 12.6],
            "low": [9.9, 10.1, 10.2, 9.9, 11.8, 12.1],
            "close": [10.2, 10.4, 10.1, 10.3, 12.3, 12.5],
            "volume": [1000, 1200, 900, 1100, 2000, 2100],
        }
    )
