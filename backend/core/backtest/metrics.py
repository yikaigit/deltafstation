"""绩效指标（规格 §6.4）。

口径统一
--------
* 年化用 ``sqrt(250)``（A 股年均约 250 个交易日）；
* Sharpe / Sortino 用**简单收益**算均值与标准差（不是对数收益）——
  对数收益会把「亏损 50%」和「赚回 100%」算成对称，Sharpe 会被高估；
* Sortino 只对负收益求标准差（下行风险），收益序列里没有负值时返回 None
  而不是 inf——「没亏过」不等于「零风险」；
* 月度胜率按自然月分组，月初不完整的那一月也计入；
* 超额 = 策略收益 - 基准收益（每一天做差再年化），信息比率 = 超额均值/超额标准差×√250；
* 换手率 = 单期换手均值 × (250/调仓间隔)，即「年均换手倍数」。

IS / OOS / 全区间**必须**用同一个函数计算，否则切分点的口径不一致会让
「自洽校验」变成自欺欺人。
"""
from __future__ import annotations

from typing import Dict, Optional

import numpy as np
import pandas as pd

TRADING_DAYS = 250

#: 判定「波动为 0」的绝对下限。
#: 不能直接用 ``vol > 0``：常数值序列的 std 并非精确 0 而是约 1e-18 的
#: 浮点粉尘，``mean / 1e-18`` 会算出 8.6e16 的 Sharpe ——
#: 一个几乎不波动的策略反而能霸占排行榜。日收益标准差低于 1e-12
#: （相当于「一分钱都不动」）就当 0 处理。
VOL_EPS = 1e-12

#: 指标键（写进 JSON，前端直接按 key 渲染）
METRIC_KEYS = (
    "days", "total_return", "annual_return", "annual_vol", "sharpe", "sortino",
    "max_drawdown", "calmar", "month_win_rate", "bench_return", "excess_return",
    "information_ratio", "turnover", "trades", "blocked_trades", "avg_positions",
)


def _round(value, digits=4):
    if value is None:
        return None
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return round(float(value), digits)


def nav_series(returns: pd.Series) -> pd.Series:
    """收益序列 → 净值序列（起点 1.0，首日收益不计入）。"""
    if returns is None or len(returns) == 0:
        return pd.Series(dtype="float64")
    return (1.0 + returns.fillna(0.0)).cumprod()


def max_drawdown(nav: pd.Series) -> float:
    """最大回撤（负数）。空序列返回 0.0。"""
    if nav is None or len(nav) == 0:
        return 0.0
    return float((nav / nav.cummax() - 1.0).min())


def drawdown_series(nav: pd.Series) -> pd.Series:
    if nav is None or len(nav) == 0:
        return pd.Series(dtype="float64")
    return nav / nav.cummax() - 1.0


def compute_metrics(
    returns: pd.Series,
    bench_returns: Optional[pd.Series] = None,
    label: str = "",
    extras: Optional[dict] = None,
) -> Dict:
    """由日收益序列计算全套绩效指标。

    Args:
        returns: 策略日收益（后复权口径，含费用）
        bench_returns: 基准日收益（可选）
        label: is / oos / full
        extras: 直接透传的计数（换手率、交易笔数、阻挡笔数、平均持仓数）
    """
    r = returns.dropna() if returns is not None else pd.Series(dtype="float64")
    if len(r) == 0:
        return {"label": label, "days": 0}

    cum = float((1.0 + r).prod() - 1.0)
    years = len(r) / TRADING_DAYS
    annual = float((1.0 + cum) ** (1.0 / years) - 1.0) if years > 0 and (1.0 + cum) > 0 else np.nan
    vol = float(r.std(ddof=1)) if len(r) > 1 else np.nan
    annual_vol = vol * np.sqrt(TRADING_DAYS) if np.isfinite(vol) else np.nan
    sharpe = float(r.mean() / vol * np.sqrt(TRADING_DAYS)) if vol > VOL_EPS else np.nan

    downside = r[r < 0]
    dstd = float(downside.std(ddof=1)) if len(downside) > 1 else np.nan
    if dstd is not None and np.isfinite(dstd) and dstd > VOL_EPS:
        sortino = float(r.mean() / dstd * np.sqrt(TRADING_DAYS))
    else:
        sortino = None

    nav = nav_series(r)
    mdd = max_drawdown(nav)
    calmar = annual / abs(mdd) if mdd < -VOL_EPS and np.isfinite(annual) else np.nan

    month_win = np.nan
    if isinstance(r.index, pd.DatetimeIndex):
        monthly = (1.0 + r).groupby(r.index.to_period("M")).prod() - 1.0
        if len(monthly):
            month_win = float((monthly > 0).sum() / len(monthly))

    out: Dict = {
        "label": label,
        "days": int(len(r)),
        "total_return": _round(cum * 100, 2),
        "annual_return": _round(annual * 100, 2),
        "annual_vol": _round(annual_vol * 100, 2) if np.isfinite(annual_vol) else None,
        "sharpe": _round(sharpe, 3),
        "sortino": _round(sortino, 3),
        "max_drawdown": _round(mdd * 100, 2),
        "calmar": _round(calmar, 3),
        "month_win_rate": _round(month_win * 100, 1) if np.isfinite(month_win) else None,
    }
    if bench_returns is not None and len(bench_returns):
        b = bench_returns.reindex(r.index).fillna(0.0)
        bench_cum = float((1.0 + b).prod() - 1.0)
        excess = r - b
        ex_std = float(excess.std(ddof=1)) if len(excess) > 1 else np.nan
        ir = float(excess.mean() / ex_std * np.sqrt(TRADING_DAYS)) if ex_std > VOL_EPS else np.nan
        out["bench_return"] = _round(bench_cum * 100, 2)
        out["excess_return"] = _round((cum - bench_cum) * 100, 2)
        out["information_ratio"] = _round(ir, 3)
    if extras:
        for key in ("turnover", "trades", "blocked_trades", "avg_positions"):
            if key in extras:
                out[key] = _round(extras[key], 2)
    return out


def split_period(dates: pd.DatetimeIndex, is_ratio: float = 0.7) -> Dict[str, str]:
    """按交易日索引切 IS/OOS（沿用参考项目口径，70/30）。

    切分点随数据更新前移，不写死日期。最后一个 IS 日与第一个 OOS 日相邻，
    因此同一条净值曲线切出来的 IS、OOS 满足乘法自洽。
    """
    if dates is None or len(dates) < 2:
        return {"is_start": None, "is_end": None, "oos_start": None, "oos_end": None}
    ratio = min(max(float(is_ratio), 0.1), 0.9)
    cut = int(len(dates) * ratio)
    cut = max(1, min(cut, len(dates) - 1))
    return {
        "is_start": dates[0].strftime("%Y-%m-%d"),
        "is_end": dates[cut - 1].strftime("%Y-%m-%d"),
        "oos_start": dates[cut].strftime("%Y-%m-%d"),
        "oos_end": dates[-1].strftime("%Y-%m-%d"),
    }


def check_self_consistency(
    is_metrics: Dict, oos_metrics: Dict, full_metrics: Dict, tolerance_pp: Optional[float] = None
) -> Optional[str]:
    """自洽校验：(1+IS)×(1+OOS)-1 == 全区间累计。

    同一条曲线切分天然满足，报错意味着有未来函数或口径漂移。

    ``tolerance_pp`` 缺省时按量级自动给：三项 ``total_return`` 都四舍五入到
    「百分数的两位小数」，单段绝对误差最多 0.005pp，而乘积
    ``(1+a)(1+b)-1`` 会把误差放大成 ``0.005×(2+|a|+|b|)`` —— 收益翻几倍时
    纯粹是舍入就能差出 0.07pp。这里按这个上界给 5 倍余量：

    .. math::

        \\text{tol} = \\max(0.05,\\ 0.03 \\times (2 + |a| + |b|))

    真正的口径漂移/未来函数会造成**几个百分点**的偏差，比这个容差仍大两个
    数量级，所以自动容差不会放过真问题。
    """
    labels = (("is", is_metrics), ("oos", oos_metrics), ("full", full_metrics))
    missing = [name for name, m in labels
               if not isinstance(m, dict) or m.get("total_return") is None]
    if missing:
        return f"指标缺失，无法自洽校验（缺 {'/'.join(missing)}）"

    is_cum = float(is_metrics["total_return"]) / 100.0
    oos_cum = float(oos_metrics["total_return"]) / 100.0
    full_cum = float(full_metrics["total_return"]) / 100.0

    combined = (1.0 + is_cum) * (1.0 + oos_cum) - 1.0
    diff_pp = abs(combined - full_cum) * 100.0
    if tolerance_pp is None:
        scale = 2.0 + abs(is_cum) + abs(oos_cum)
        tolerance_pp = max(0.05, 0.03 * scale)
    if diff_pp > tolerance_pp:
        return (f"IS/OOS 自洽校验失败：组合 {combined:.6f} vs 全区间 "
                f"{full_cum:.6f}（差 {diff_pp:.4f}pp > 容差 {tolerance_pp:.4f}pp）")
    return None


def downsample_equity(
    equity: pd.DataFrame, max_points: int = 1000
) -> list:
    """净值曲线按周降采样（规格 §6.7：约 1000 点上限）。"""
    if equity is None or equity.empty:
        return []
    frame = equity.copy()
    frame.index = pd.DatetimeIndex(frame.index)
    if len(frame) > max_points:
        # 按周取每周最后一个交易日，保证首末日不丢
        grouped = frame.groupby(pd.Grouper(freq="W-FRI")).tail(1)
        if len(grouped) > max_points:
            step = int(np.ceil(len(grouped) / max_points))
            grouped = grouped.iloc[::step]
        frame = grouped
    if frame.index[0] != equity.index[0]:
        frame = pd.concat([equity.iloc[[0]], frame])
    records = []
    for date, row in frame.iterrows():
        records.append(
            {
                "date": date.strftime("%Y-%m-%d"),
                "nav": _round(row.get("nav"), 6),
                "bench": _round(row.get("bench"), 6) if pd.notna(row.get("bench")) else None,
                "drawdown": _round(row.get("drawdown"), 6),
            }
        )
    return records
