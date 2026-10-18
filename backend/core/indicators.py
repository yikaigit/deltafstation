"""技术指标：talib 的面板封装。

为什么不能直接把面板喂给 talib
------------------------------
talib 的滚动类指标（SMA/RSI/ATR…）遇到 NaN 会把 NaN **向后传播到整段输出**：
窗口里只要有一个 NaN，之后所有值都是 NaN。而我们的面板里「未上市期间」
天然是 NaN，一只 2020 年上市的票会因此整列报废。

所以这里统一走 ``apply_per_column``：先把每列的非 NaN 段抽出来算，再按原
位置写回。代价是逐列调用（300 次而不是 1 次），但每列只是一维数组运算，
实测 300 列 × 4400 行全部指标约 2 秒，远低于规格的 30 秒预算。

滚动分位（``turn_rank`` / ``atr_pct_rank``）用 pandas 的 ``Rolling.rank``
（C 实现），不走 Python 回调 —— 后者在 132 万个值上要跑几十秒。
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 缺 talib 时给出明确提示
    import talib
except Exception:  # pragma: no cover
    talib = None


def require_talib() -> None:
    if talib is None:
        raise RuntimeError("需要 TA-Lib 才能计算技术因子，请执行 pip install TA-Lib")


def _runs(valid: np.ndarray) -> list:
    """把布尔掩码切成若干**连续为 True 的段**，返回 ``[(start, stop), ...]``。

    ⚠️ 不能用 ``values[valid]`` 直接抽：那样会把缺口前后的有效值拼成**一段**
    连续序列，指标会在缺口处「跨接」出一个本来不存在的窗口。
    实测：``[1..10, NaN×5, 101..110]`` 算 SMA(3)，第 11 个值会变成
    ``mean(9, 10, 101) = 40`` —— 停牌前后的价格被混在一起，
    复牌后头几天的 SMA/MACD/ADX 全是被污染的值。
    """
    padded = np.concatenate(([False], valid, [False])).astype(np.int8)
    diff = np.diff(padded)
    starts = np.flatnonzero(diff == 1)
    ends = np.flatnonzero(diff == -1)
    return list(zip(starts.tolist(), ends.tolist()))


def apply_per_column(
    frame: pd.DataFrame,
    fn: Callable[..., Optional[np.ndarray]],
    *args,
    **kwargs,
) -> pd.DataFrame:
    """对每列的非 NaN 段调用 ``fn``，结果按原索引写回。

    Args:
        frame: 面板（index=date, columns=code）
        fn: 接收一维 float64 数组，返回同长度数组（或 None 表示该列无结果）

    Returns:
        与输入同形状的 float32 面板，无结果的格子为 NaN
    """
    index = frame.index
    columns = frame.columns
    values = frame.to_numpy(dtype="float64", copy=False)
    n_rows, n_cols = values.shape
    out = np.full((n_rows, n_cols), np.nan, dtype="float64")

    for j in range(n_cols):
        column = values[:, j]
        valid = np.isfinite(column)
        if valid.sum() < 2:
            continue
        for start, stop in _runs(valid):
            segment = column[start:stop]
            try:
                result = fn(segment, *args, **kwargs)
            except Exception as exc:  # 单段算不出来不该拖垮整批
                logger.debug("指标计算失败（列 %s 段 %d:%d）：%s", columns[j], start, stop, exc)
                continue
            if result is None:
                continue
            result = np.asarray(result, dtype="float64")
            if result.shape[0] != segment.shape[0]:
                continue
            out[start:stop, j] = result

    return pd.DataFrame(out.astype("float32"), index=index, columns=columns)


# ---------- 均线 ----------

def sma(frame: pd.DataFrame, period: int) -> pd.DataFrame:
    require_talib()
    return apply_per_column(frame, talib.SMA, timeperiod=period)


def ema(frame: pd.DataFrame, period: int) -> pd.DataFrame:
    require_talib()
    return apply_per_column(frame, talib.EMA, timeperiod=period)


# ---------- 趋势 ----------

def macd_hist(frame: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """MACD 柱（DIF - DEA），绝对值，调用方自行除以价格归一。"""
    require_talib()

    def _hist(segment: np.ndarray) -> np.ndarray:
        _, _, hist = talib.MACD(segment, fastperiod=fast, slowperiod=slow, signalperiod=signal)
        return hist

    return apply_per_column(frame, _hist)


def linearreg_slope(frame: pd.DataFrame, period: int) -> pd.DataFrame:
    require_talib()
    return apply_per_column(frame, talib.LINEARREG_SLOPE, timeperiod=period)


def adx(frame: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame,
        period: int = 14) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """返回 (ADX, PDI, MDI)。

    三个指标必须用同一段有效数据 —— 分开算会因各列 NaN 位置不同而错位，
    所以这里一次调用把三者都取出来。
    """
    require_talib()
    index, columns = frame.index, frame.columns
    shape = frame.shape
    out = {k: np.full(shape, np.nan) for k in ("adx", "pdi", "mdi")}

    close = frame.to_numpy(dtype="float64", copy=False)
    hi = high.to_numpy(dtype="float64", copy=False)
    lo = low.to_numpy(dtype="float64", copy=False)

    for j in range(shape[1]):
        valid = np.isfinite(close[:, j]) & np.isfinite(hi[:, j]) & np.isfinite(lo[:, j])
        if valid.sum() < period + 1:
            continue
        # 按连续段算：把缺口前后的数据拼成一段会让 DI 跨缺口「借用」停牌前的价格
        for start, stop in _runs(valid):
            if stop - start < period + 1:
                continue
            h, l, c = hi[start:stop, j], lo[start:stop, j], close[start:stop, j]
            out["adx"][start:stop, j] = talib.ADX(h, l, c, timeperiod=period)
            out["pdi"][start:stop, j] = talib.PLUS_DI(h, l, c, timeperiod=period)
            out["mdi"][start:stop, j] = talib.MINUS_DI(h, l, c, timeperiod=period)

    return tuple(
        pd.DataFrame(out[k].astype("float32"), index=index, columns=columns)
        for k in ("adx", "pdi", "mdi")
    )


# ---------- 动量 ----------

def rsi(frame: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    require_talib()
    return apply_per_column(frame, talib.RSI, timeperiod=period)


# ---------- 量能 ----------

def mfi(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
        volume: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    require_talib()
    index, columns = close.index, close.columns
    shape = close.shape
    out = np.full(shape, np.nan)

    h = high.to_numpy(dtype="float64", copy=False)
    l = low.to_numpy(dtype="float64", copy=False)
    c = close.to_numpy(dtype="float64", copy=False)
    v = volume.to_numpy(dtype="float64", copy=False)

    for j in range(shape[1]):
        valid = (
            np.isfinite(c[:, j]) & np.isfinite(h[:, j])
            & np.isfinite(l[:, j]) & np.isfinite(v[:, j])
        )
        if valid.sum() < period + 1:
            continue
        for start, stop in _runs(valid):
            if stop - start < period + 1:
                continue
            out[start:stop, j] = talib.MFI(h[start:stop, j], l[start:stop, j],
                                           c[start:stop, j], v[start:stop, j],
                                           timeperiod=period)

    return pd.DataFrame(out.astype("float32"), index=index, columns=columns)


def obv(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    """能量潮（累计成交量方向）。"""
    require_talib()
    index, columns = close.index, close.columns
    shape = close.shape
    out = np.full(shape, np.nan)
    c = close.to_numpy(dtype="float64", copy=False)
    v = volume.to_numpy(dtype="float64", copy=False)

    for j in range(shape[1]):
        valid = np.isfinite(c[:, j]) & np.isfinite(v[:, j])
        if valid.sum() < 2:
            continue
        # OBV 是累计量，跨缺口拼接会让复牌后的累计值凭空跳变
        for start, stop in _runs(valid):
            if stop - start < 2:
                continue
            out[start:stop, j] = talib.OBV(c[start:stop, j], v[start:stop, j])

    return pd.DataFrame(out, index=index, columns=columns)


# ---------- 波动 ----------

def atr(high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame,
        period: int = 14) -> pd.DataFrame:
    require_talib()
    index, columns = close.index, close.columns
    shape = close.shape
    out = np.full(shape, np.nan)

    h = high.to_numpy(dtype="float64", copy=False)
    l = low.to_numpy(dtype="float64", copy=False)
    c = close.to_numpy(dtype="float64", copy=False)

    for j in range(shape[1]):
        valid = np.isfinite(c[:, j]) & np.isfinite(h[:, j]) & np.isfinite(l[:, j])
        if valid.sum() < period + 1:
            continue
        for start, stop in _runs(valid):
            if stop - start < period + 1:
                continue
            out[start:stop, j] = talib.ATR(h[start:stop, j], l[start:stop, j],
                                           c[start:stop, j], timeperiod=period)

    return pd.DataFrame(out.astype("float32"), index=index, columns=columns)


# ---------- 滚动统计（pandas 实现） ----------

def rolling_pct_rank(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    """当前值在过去 ``window`` 天中的百分位（0~1）。

    用 ``Rolling.rank(pct=True)``（C 实现）。等价于「今天在这 250 天里排第几」，
    用于把「换手率」「ATR 占比」这类量纲不同的指标转成可比的分位。
    """
    return frame.rolling(window, min_periods=max(20, window // 4)).rank(pct=True).astype("float32")


def rolling_std(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return frame.rolling(window, min_periods=max(20, window // 4)).std().astype("float32")


def rolling_max(frame: pd.DataFrame, window: int,
                min_periods: Optional[int] = None) -> pd.DataFrame:
    """滚动最大值。

    ``min_periods`` 默认取 ``window//4``（下限 20）而不是 1：
    如果允许 1，``dd_1y`` 在上市第一天就会返回 0（当前价即最高价），
    横截面 rank 后全变成 50 分位 —— 一个没有任何信息量的「假中性」。
    """
    mp = min_periods if min_periods is not None else max(20, window // 4)
    return frame.rolling(window, min_periods=mp).max().astype("float32")


def rolling_mean(frame: pd.DataFrame, window: int) -> pd.DataFrame:
    return frame.rolling(window, min_periods=max(5, window // 4)).mean().astype("float32")


def downside_vol(frame: pd.DataFrame, window: int, annualize: int = 250) -> pd.DataFrame:
    """下行波动率：只统计负收益的平方和，再年化。

    与 ``hv`` 的区别是不把上涨算作风险 —— 这是 Sortino 的口径，
    对「涨得凶但也跌得凶」和「只是涨得凶」能区分开。

    ⚠️ ``pct_change`` 必须显式 ``fill_method=None``：默认的 ``pad`` 会用前值
    填充 NaN，把「未上市期间」变成 0 收益混进波动率，而且 pandas 未来版本
    会改掉这个默认值 —— 不写死的话同一份数据在不同 pandas 上算出的结果不同，
    「同日两次运行结果完全一致」这条验收就没法保证。

    ⚠️ 正收益置零必须用 ``clip(upper=0.0)`` 而不是 ``where(ret < 0, 0.0)``：
    ``where`` 在 ``ret`` 为 NaN 时条件为 False，会取到替代值 0.0 —— 于是
    未上市/停牌列的下行方差恒为 0，被算成「零风险」。这个因子 ``direction=-1``，
    零风险经反向标准化后变成最高分，等于让一只根本没上市的股票以 99 分
    霸占 top-N（实测踩过）。``clip`` 与 ``mask`` 都保留 NaN，缺数据就是缺数据，
    不是零风险。
    """
    ret = frame.pct_change(fill_method=None)
    negative = ret.clip(upper=0.0)
    vol = negative.pow(2).rolling(window, min_periods=max(20, window // 4)).mean().pow(0.5)
    return (vol * np.sqrt(annualize)).astype("float32")
