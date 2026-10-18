"""回测包：策略 → 目标权重 → 执行 → 指标（规格 §6）。"""
from __future__ import annotations

from .costs import CostModel, is_tradable, limit_pct_for, limit_reason
from .engine import BacktestEngine, BacktestResult, run_backtest
from .metrics import (
    check_self_consistency,
    compute_metrics,
    downsample_equity,
    max_drawdown,
    nav_series,
    split_period,
)
from .strategies import (
    StrategyParams,
    build_target_weights,
    cap_and_redistribute,
    regime_exposure,
    signal_dates,
    weights_for_picks,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "CostModel",
    "StrategyParams",
    "build_target_weights",
    "cap_and_redistribute",
    "check_self_consistency",
    "compute_metrics",
    "downsample_equity",
    "is_tradable",
    "limit_pct_for",
    "limit_reason",
    "max_drawdown",
    "nav_series",
    "regime_exposure",
    "run_backtest",
    "signal_dates",
    "split_period",
    "weights_for_picks",
]
