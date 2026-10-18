"""复权：不复权价 + 除权事件表 → 后复权 / 前复权价。

设计原则
--------
1. 落盘只存**不复权价 + 除权事件表**（事件表通常几十行/只），不预计算每日复权价。
   300 只 × 4400 行 = 132 万行，落盘复权价没有收益，只会让「换因子口径」变困难。
2. 后复权价 = 不复权价 × ``back_factor(截至当日)``
   → 只依赖**当日及以前**的事件，点-in-time 天然安全，回测主序列用它。
3. 前复权价 = 后复权价 / 基准日 back_factor
   → 分母用到未来事件，**天然带未来函数**，仅用于图表展示。
4. 除权日**当天**即启用新因子（不是次日）。已用浦发 2026-07-16 逐位验证：
   不复权 9.31→8.85 看似 -4.9%，后复权 118.833→118.298 实际 -0.45%，
   反推因子 12.7637(07-15) / 13.3670(07-16) 与事件表一致。
5. 指数无复权因子（``query_adjust_factor`` 返回 0 行）→ 因子恒 1.0，后复权价 == 不复权价，
   基准对比天然正确。
6. 涨跌停与停牌判定**必须用不复权价 + preclose**，不能用复权价（见回测引擎）。

性能
----
面板展开用「按 code 分组 + searchsorted」实现，300 只 × 4400 行约 10ms，
不使用 ``merge_asof``（多键排序约束易踩坑）也不使用逐行循环。
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .codes import is_index, normalize_bs_code  # noqa: F401  (对外复用)

logger = logging.getLogger(__name__)

#: 复权事件表列名（数据湖口径）
EVENT_COLUMNS = ["code", "ex_date", "fore_factor", "back_factor"]

#: 需要复权的价格列
PRICE_COLUMNS = ["open", "high", "low", "close"]

MODE_NONE = "none"
MODE_BACK = "back"
MODE_FORWARD = "forward"

__all__ = [
    "EVENT_COLUMNS",
    "MODE_BACK",
    "MODE_FORWARD",
    "MODE_NONE",
    "apply_adjust",
    "factor_at",
    "factor_from_events",
    "expand_factors",
    "is_index",
    "looks_truncated",
    "normalize_bs_code",
]


# ---------- 单序列 ----------


def factor_from_events(events: Optional[pd.DataFrame], dates) -> np.ndarray:
    """把单只标的的除权事件表展开为逐日 back_factor。

    规则：``factor(d)`` = 最后一条 ``ex_date <= d`` 的 back_factor；
    d 早于首个除权日则为 1.0；**晚于窗口末日的事件不参与**（点-in-time）。

    Args:
        events: 含 ``ex_date`` / ``back_factor`` 的事件表（单只）
        dates: 目标日期序列
    """
    target = pd.DatetimeIndex(pd.to_datetime(list(dates)))
    n = len(target)
    if n == 0:
        return np.ones(0, dtype="float64")
    if events is None or len(events) == 0 or "ex_date" not in events.columns:
        return np.ones(n, dtype="float64")

    ev = events[["ex_date", "back_factor"]].copy()
    ev["ex_date"] = pd.to_datetime(ev["ex_date"], errors="coerce")
    ev["back_factor"] = pd.to_numeric(ev["back_factor"], errors="coerce")
    ev = ev.dropna().sort_values("ex_date")
    if ev.empty:
        return np.ones(n, dtype="float64")

    ev_d = ev["ex_date"].to_numpy()
    ev_f = ev["back_factor"].to_numpy(dtype="float64")

    # 点-in-time：剔除晚于窗口末日的事件
    horizon = target.to_numpy().max()
    keep = ev_d <= horizon
    ev_d, ev_f = ev_d[keep], ev_f[keep]
    if len(ev_d) == 0:
        return np.ones(n, dtype="float64")

    pos = np.searchsorted(ev_d, target.to_numpy(), side="right") - 1
    return np.where(pos >= 0, ev_f[np.clip(pos, 0, len(ev_f) - 1)], 1.0).astype("float64")


def factor_at(events: Optional[pd.DataFrame], day) -> float:
    """某一天的 back_factor（无事件为 1.0）。"""
    values = factor_from_events(events, [day])
    return float(values[0]) if len(values) else 1.0


def looks_truncated(events: Optional[pd.DataFrame], first_date) -> bool:
    """事件表疑似被截断（会把后复权价算成不复权价）。

    判据：首个事件日晚于数据首日，且首个事件的 back_factor 明显大于 1
    —— 说明该股在首个事件之前还有未被拉到的除权历史。
    """
    if events is None or len(events) == 0 or "ex_date" not in events.columns or first_date is None:
        return False
    try:
        ev_first = pd.to_datetime(events["ex_date"]).min()
        data_first = pd.Timestamp(first_date)
        if ev_first <= data_first:
            return False
        head = float(pd.to_numeric(events["back_factor"]).iloc[0])
    except Exception:
        return False
    return head > 1.5


# ---------- 面板 ----------


def expand_factors(bars: pd.DataFrame, adj: Optional[pd.DataFrame]) -> np.ndarray:
    """面板级因子展开：为 bars 的每一行求 back_factor。

    Args:
        bars: 至少含 ``date`` / ``code``
        adj: 含 ``code`` / ``ex_date`` / ``back_factor``；为空则全部为 1.0

    Returns:
        与 bars 同长度的 float 数组（顺序与 bars 行序一致）
    """
    n = len(bars)
    out = np.ones(n, dtype="float64")
    if n == 0 or adj is None or len(adj) == 0:
        return out
    if "code" not in adj.columns or "ex_date" not in adj.columns:
        return out

    groups: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    clean = adj[["code", "ex_date", "back_factor"]].copy()
    clean["ex_date"] = pd.to_datetime(clean["ex_date"], errors="coerce")
    clean["back_factor"] = pd.to_numeric(clean["back_factor"], errors="coerce")
    clean = clean.dropna()
    for code, group in clean.groupby("code", sort=False):
        ordered = group.sort_values("ex_date")
        groups[str(code)] = (
            ordered["ex_date"].to_numpy(),
            ordered["back_factor"].to_numpy(dtype="float64"),
        )

    dates = pd.to_datetime(bars["date"]).to_numpy()
    codes = bars["code"].astype(str).to_numpy()
    for code, idx in bars.groupby("code", sort=False).indices.items():
        pair = groups.get(str(code))
        if pair is None:
            continue
        ev_d, ev_f = pair
        target = dates[idx]
        pos = np.searchsorted(ev_d, target, side="right") - 1
        out[idx] = np.where(pos >= 0, ev_f[np.clip(pos, 0, len(ev_f) - 1)], 1.0)
    return out


def apply_adjust(
    bars: pd.DataFrame,
    adj: Optional[pd.DataFrame],
    mode: str = MODE_BACK,
    base_date=None,
    prefix: str = "adj_",
) -> pd.DataFrame:
    """给行情表加上复权价列，返回新 DataFrame（不修改入参）。

    Args:
        bars: 含 date / code / open / high / low / close
        adj: 复权事件表
        mode: ``back`` 后复权（回测用） / ``forward`` 前复权（仅展示） / ``none``
        base_date: 前复权基准日；None 表示用序列最后一天
        prefix: 新增列前缀，默认 ``adj_``

    Returns:
        原表 + ``<prefix>open/high/low/close``（mode=none 时原样返回副本）
    """
    out = bars.copy()
    if mode == MODE_NONE or out.empty:
        return out
    if mode not in (MODE_BACK, MODE_FORWARD):
        raise ValueError(f"不支持的复权口径：{mode}")

    factor = expand_factors(out, adj)
    if mode == MODE_FORWARD:
        if base_date is None:
            # 以每只标的最后一天的因子为基准（分母含未来事件，仅展示用）
            base = (
                pd.DataFrame(
                    {
                        "code": out["code"].astype(str).to_numpy(),
                        "date": pd.to_datetime(out["date"]).to_numpy(),
                        "factor": factor,
                    }
                )
                .sort_values("date")
                .groupby("code")["factor"]
                .last()
            )
        else:
            base = _factor_on_date(out, adj, base_date)
        base_per_row = out["code"].astype(str).map(base).to_numpy(dtype="float64")
        scale = np.divide(factor, base_per_row, out=np.ones_like(factor), where=base_per_row != 0)
    else:
        scale = factor

    for column in PRICE_COLUMNS:
        if column in out.columns:
            out[prefix + column] = pd.to_numeric(out[column], errors="coerce").to_numpy() * scale
    out[prefix + "factor"] = factor
    return out


def _factor_on_date(bars: pd.DataFrame, adj: Optional[pd.DataFrame], day) -> pd.Series:
    """各标的在指定基准日的 back_factor。"""
    codes = bars["code"].drop_duplicates().astype(str)
    if adj is None or len(adj) == 0:
        return pd.Series(1.0, index=codes)
    values = {}
    for code in codes:
        events = adj[adj["code"].astype(str) == code]
        values[code] = factor_at(events, day)
    return pd.Series(values)


# ---------- 便捷入口 ----------


def load_events(lake, codes: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """从数据湖读复权事件表（可按标的裁剪）。"""
    if not lake.exists("adj_factor"):
        return pd.DataFrame(columns=EVENT_COLUMNS)
    frame = lake.read("adj_factor")
    if codes is not None and not frame.empty:
        frame = frame[frame["code"].astype(str).isin({str(c) for c in codes})]
    return frame.reset_index(drop=True)


def check_factor_integrity(events: pd.DataFrame, min_factor: float = 1.0) -> dict:
    """复权因子完整性体检。

    Returns:
        {"symbols": n, "empty": n, "non_monotonic": n, "below_one": n, "samples": [...]}
    """
    result = {
        "symbols": 0,
        "empty": 0,
        "non_monotonic": 0,
        "below_one": 0,
        "samples": [],
    }
    if events is None or events.empty:
        return result
    for code, group in events.groupby("code"):
        result["symbols"] += 1
        ordered = group.sort_values("ex_date")
        factors = pd.to_numeric(ordered["back_factor"], errors="coerce").dropna()
        if factors.empty:
            result["empty"] += 1
            continue
        if (factors.diff() < -1e-9).any():
            result["non_monotonic"] += 1
            if len(result["samples"]) < 5:
                result["samples"].append({"code": code, "issue": "非单调递减"})
        if (factors < min_factor - 1e-9).any():
            result["below_one"] += 1
    return result
