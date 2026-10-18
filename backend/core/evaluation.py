"""因子评价：IC / ICIR / 分层 / 相关性 / 换手。

规格 §5.7 的六个指标，全部实现在这里。

为什么必须先想清楚「方向」
--------------------------
``direction=-1`` 的因子（``hv_60``、``peg_safe`` 等）原始值越**小**越好。
如果直接用原始值算 IC，会得到一列显著的**负** IC —— 看起来像「因子失效」，
其实是方向没翻。所以本模块在算 IC 之前**统一按方向翻正**
（``value * direction``），这样「IC 为正 = 因子有效」对所有因子都成立。

RankIC 的三条纪律
-----------------
1. **用秩不用值**：Spearman 而非 Pearson。财务因子里一个 ST 股的 ROE 可能是
   -800%，Pearson 会被它整个带偏；秩相关只看排序，天然稳健。
2. **逐日算、再统计**：把 1212 天的 RankIC 当成 1212 个独立样本求均值/标准差。
   直接把所有日期堆在一起算一个相关系数是**错的** —— 不同日期的估值水平
   完全不可比（牛市 PE 30、熊市 PE 10），混在一起算出来的 IC 没有含义。
3. **前视收益必须对齐到「未来」**：``forward_return`` 用 ``close.shift(-1) /
   close - 1`` 从**次日**开始累计，不能用 ``pct_change`` 的当日收益
   （那是「用今天的因子预测今天的收益」，等于作弊）。

分层与多空
----------
按因子值把当日横截面切成 5 组（Q1 最小 → Q5 最大），看各组未来收益是否单调。
**必须先按方向翻正**，否则 direction=-1 的因子会出现「Q1 最高」的假象。
多空 = Q5 − Q1，这是因子最纯粹的表达（对冲掉市场 beta）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 计算「未来收益」的窗口（交易日），与规格 §5.7 一致
FORWARD_DAYS = 20

#: 分层层数
QUANTILES = 5

#: 每个交易日至少要有多少只有效标的才算一次有效样本
MIN_CROSS_SECTION = 20

#: 规格 §5.7 的门槛
THRESHOLDS = {
    "ic_mean": 0.03,
    "ic_tstat": 2.0,
    "icir": 0.30,
    "ic_win_rate": 0.55,
    "monotonicity": 0.60,
    "long_short": 0.08,
    "turnover": 0.60,
    "max_corr": 0.70,
}


@dataclass
class FactorEval:
    """单个因子的评价结果。"""

    name: str
    category: str = ""
    direction: int = 1
    ic_mean: Optional[float] = None
    ic_std: Optional[float] = None
    icir: Optional[float] = None
    ic_win_rate: Optional[float] = None
    ic_days: int = 0
    ic_tstat: Optional[float] = None
    monotonicity: Optional[float] = None
    long_short: Optional[float] = None
    quantile_returns: List[Optional[float]] = field(default_factory=list)
    turnover: Optional[float] = None
    coverage: float = 0.0
    passed: bool = False
    failed_on: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "direction": self.direction,
            "ic_mean": self.ic_mean,
            "ic_std": self.ic_std,
            "icir": self.icir,
            "ic_win_rate": self.ic_win_rate,
            "ic_days": self.ic_days,
            "ic_tstat": self.ic_tstat,
            "monotonicity": self.monotonicity,
            "long_short": self.long_short,
            "turnover": self.turnover,
            "coverage": self.coverage,
            "passed": self.passed,
            "failed_on": list(self.failed_on),
            **{f"q{i + 1}": v for i, v in enumerate(self.quantile_returns)},
        }


def forward_returns(close: pd.DataFrame, days: int = FORWARD_DAYS) -> pd.DataFrame:
    """未来 ``days`` 个交易日的累计收益（对齐到「今天」）。

    ``ret[t] = close[t+days] / close[t] - 1``。

    ⚠️ 用 ``shift(-days)`` 而不是 ``pct_change()`` 再滚动求和：
    后者会把 t 日自身的涨跌算进「未来收益」里，而 t 日的收盘价正是因子
    输入的最后一根 bar —— 等于让因子偷看了自己的答案。
    """
    future = close.shift(-days)
    return (future / close - 1.0).astype("float32")


def _daily_rank_ic(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    min_obs: int = MIN_CROSS_SECTION,
) -> pd.Series:
    """逐日 RankIC 序列（Spearman 秩相关）。

    实现方式：把每行（截面上）的因子值和未来收益都转成秩，再逐行算
    Pearson —— 这就是 Spearman。自己写而不是 ``scipy.stats.spearmanr``
    是因为后者一次只能算一对，1212 天要循环 1212 次，太慢。
    """
    common_dates = factor.index.intersection(forward.index)
    common_cols = factor.columns.intersection(forward.columns)
    if not len(common_dates) or not len(common_cols):
        return pd.Series(dtype="float64")

    f = factor.loc[common_dates, common_cols]
    r = forward.loc[common_dates, common_cols]

    # 只保留两边都有值的格子
    both = f.notna() & r.notna()
    f = f.where(both)
    r = r.where(both)

    f_rank = f.rank(axis=1, method="average")
    r_rank = r.rank(axis=1, method="average")

    f_centered = f_rank.sub(f_rank.mean(axis=1), axis=0)
    r_centered = r_rank.sub(r_rank.mean(axis=1), axis=0)

    numerator = (f_centered * r_centered).sum(axis=1, skipna=True)
    denominator = np.sqrt(
        (f_centered ** 2).sum(axis=1, skipna=True)
        * (r_centered ** 2).sum(axis=1, skipna=True)
    )
    valid_count = both.sum(axis=1)

    with np.errstate(invalid="ignore", divide="ignore"):
        ic = numerator / denominator
    ic = ic.where((denominator > 0) & (valid_count >= min_obs))
    return ic.dropna()


def _quantile_stats(
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    quantiles: int = QUANTILES,
    min_obs: int = MIN_CROSS_SECTION,
) -> tuple:
    """分层收益 + 单调性 + 多空。

    Returns:
        (每层日均收益列表, 单调性 Spearman, 多空年化)
    """
    common_dates = factor.index.intersection(forward.index)
    common_cols = factor.columns.intersection(forward.columns)
    if not len(common_dates) or not len(common_cols):
        return [], None, None

    f = factor.loc[common_dates, common_cols]
    r = forward.loc[common_dates, common_cols]
    both = f.notna() & r.notna()
    f = f.where(both)
    r = r.where(both)

    # 逐日分组：用 rank(pct=True) 再 floor 到 quantile 桶
    pct = f.rank(axis=1, pct=True)
    buckets = np.ceil(pct * quantiles).clip(1, quantiles)

    daily: Dict[int, List[float]] = {q: [] for q in range(1, quantiles + 1)}
    values_f = f.to_numpy(dtype="float64")
    values_r = r.to_numpy(dtype="float64")
    values_b = buckets.to_numpy(dtype="float64")
    valid_rows = both.sum(axis=1).to_numpy() >= min_obs

    for row in range(values_f.shape[0]):
        if not valid_rows[row]:
            continue
        for q in range(1, quantiles + 1):
            mask = (values_b[row] == q) & np.isfinite(values_r[row])
            if mask.any():
                daily[q].append(float(values_r[row][mask].mean()))

    if not any(daily.values()):
        return [], None, None

    means = [float(np.mean(v)) if v else None for v in (daily[q] for q in range(1, quantiles + 1))]

    # 单调性：层序 vs 日均收益的 Spearman
    usable = [(q, m) for q, m in enumerate(means, 1) if m is not None]
    monotonicity = None
    if len(usable) >= 3:
        order = pd.Series([q for q, _ in usable])
        returns = pd.Series([m for _, m in usable])
        monotonicity = float(order.corr(returns, method="spearman"))
        if not np.isfinite(monotonicity):
            monotonicity = None

    # 多空：Q5 - Q1 的日均收益 × 年化（252 / 持仓窗口）
    long_short = None
    if means[0] is not None and means[-1] is not None:
        spread = means[-1] - means[0]
        # 每 FORWARD_DAYS 天持有一次，一年有 252/FORWARD_DAYS 个完整周期
        long_short = float((1.0 + spread) ** (252.0 / FORWARD_DAYS) - 1.0)

    return means, monotonicity, long_short


def factor_turnover(factor: pd.DataFrame, top_ratio: float = 0.2) -> Optional[float]:
    """Top 组的成分换手率（每个调仓周期换掉多少比例）。

    做法：按 ``FORWARD_DAYS`` 为间隔抽样，看相邻两次「前 20%」的成分重叠度，
    换手 = 1 - 重叠率。用全日期逐日算会把「同一只票连续 20 天都排前 20%」
    误算成 19 次换手。
    """
    common = factor.dropna(how="all")
    if len(common) < FORWARD_DAYS * 2:
        return None

    selection = factor.iloc[::FORWARD_DAYS]
    previous: Optional[set] = None
    changes: List[float] = []
    for _, row in selection.iterrows():
        valid = row.dropna()
        if len(valid) < MIN_CROSS_SECTION:
            continue
        keep = max(1, int(len(valid) * top_ratio))
        current = set(valid.nlargest(keep).index)
        if previous is not None and previous:
            overlap = len(current & previous) / max(len(previous), 1)
            changes.append(1.0 - overlap)
        previous = current
    return float(np.mean(changes)) if changes else None


def evaluate_factor(
    name: str,
    factor: pd.DataFrame,
    forward: pd.DataFrame,
    close: pd.DataFrame,
    direction: int = 1,
    category: str = "",
) -> FactorEval:
    """评价单个因子（内部会按 ``direction`` 翻正）。"""
    result = FactorEval(name=name, category=category, direction=direction)
    if factor is None or factor.empty:
        return result

    # 方向翻正：direction=-1 的因子取负，这样「越大越好」对所有因子成立
    oriented = factor.astype("float64") * float(direction)

    # 覆盖率：有值格子 / **该因子应当有值的格子**。
    #
    # ⚠️ 分母不能简单用「已上市格子」：估值因子从 2021-10 才有数据，
    # 而行情从 2008 年开始，这样算出来覆盖率恒为 0.34，看起来像「因子有 2/3
    # 的数据是坏的」，其实只是评价区间跨越了数据源的历史边界。
    #
    # 正确口径：只在「该因子至少有一个有效值的日期」上算 —— 也就是
    # 「因子有数据之后，它在已上市股票里的覆盖率」。这样 0.34 会变成接近 1.0，
    # 真实的缺口（次新股、停牌）才会显出来。
    listed = close.notna()
    common = oriented.reindex(index=listed.index, columns=listed.columns)
    covered = common.notna() & listed
    active_dates = covered.any(axis=1)
    if active_dates.any():
        denominator = int(listed.loc[active_dates].to_numpy().sum())
        if denominator:
            result.coverage = float(covered.loc[active_dates].to_numpy().sum() / denominator)

    ic = _daily_rank_ic(oriented, forward)
    if len(ic):
        result.ic_days = int(len(ic))
        result.ic_mean = float(ic.mean())
        result.ic_std = float(ic.std(ddof=1)) if len(ic) > 1 else None
        if result.ic_std and result.ic_std > 1e-9:
            result.icir = float(result.ic_mean / result.ic_std)
        result.ic_win_rate = float((ic > 0).mean())
        # t 值：ic_std 已经是日度 IC 的标准差，所以 t = mean / (std/√n) × √n… 注意
        # 这里是 **日度 IC 序列**的 t 检验，衡量「均值是否显著不为 0」。
        # 单看 ic_mean ≥ 0.03 的门槛是不够的：500 天样本、日度 IC 波动 0.25 时，
        # 0.03 的均值对应的 t 只有 0.03/(0.25/√500) ≈ 2.7，勉强显著；
        # 而 200 天样本同样 0.03 的 t 只有 1.7，其实什么也证明不了。
        if result.ic_std and result.ic_std > 1e-9 and len(ic) > 2:
            result.ic_tstat = float(result.ic_mean / (result.ic_std / np.sqrt(len(ic))))

    means, monotonicity, long_short = _quantile_stats(oriented, forward)
    result.quantile_returns = means
    result.monotonicity = monotonicity
    result.long_short = long_short
    result.turnover = factor_turnover(oriented)

    # 门禁
    failed: List[str] = []
    if result.ic_mean is None or abs(result.ic_mean) < THRESHOLDS["ic_mean"]:
        failed.append("ic_mean")
    if result.ic_tstat is None or abs(result.ic_tstat) < THRESHOLDS["ic_tstat"]:
        failed.append("ic_tstat")
    if result.icir is None or abs(result.icir) < THRESHOLDS["icir"]:
        failed.append("icir")
    if result.ic_win_rate is None or result.ic_win_rate < THRESHOLDS["ic_win_rate"]:
        failed.append("ic_win_rate")
    if result.monotonicity is None or abs(result.monotonicity) < THRESHOLDS["monotonicity"]:
        failed.append("monotonicity")
    if result.long_short is None or result.long_short < THRESHOLDS["long_short"]:
        failed.append("long_short")
    if result.turnover is not None and result.turnover > THRESHOLDS["turnover"]:
        failed.append("turnover")
    result.failed_on = failed
    result.passed = len(failed) == 0
    return result


def correlation_matrix(
    factor_pcts: Dict[str, pd.DataFrame],
    forward: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """因子间相关性矩阵。

    优先用「因子的日度 RankIC 序列」做相关（规格 §5.7 的写法）：两个因子
    如果捕捉的是同一个信号，它们的 IC 会同涨同跌。直接对因子值做相关会被
    「同一天全市场都在涨」这种共同因子污染 —— 那时所有因子的横截面相关
    都接近 1，看不出任何信息。

    没有 forward 时退化为「逐日横截面 Spearman 的平均值」。
    """
    names = list(factor_pcts)
    if not names:
        return pd.DataFrame()

    if forward is not None:
        series = {}
        for name in names:
            ic = _daily_rank_ic(factor_pcts[name], forward)
            if len(ic):
                series[name] = ic
        if series:
            frame = pd.DataFrame(series)
            return frame.corr(method="spearman")

    # 退化路径：逐日横截面秩相关，再取均值
    index = names
    matrix = np.eye(len(names))
    ranks = {n: factor_pcts[n].rank(axis=1) for n in names}
    for i, a in enumerate(names):
        for j in range(i + 1, len(names)):
            b = names[j]
            corr = ranks[a].corrwith(ranks[b], axis=1)
            value = float(corr.mean()) if corr.notna().any() else np.nan
            matrix[i, j] = matrix[j, i] = value
    return pd.DataFrame(matrix, index=index, columns=index)


def evaluate_all(
    factor_frames: Dict[str, pd.DataFrame],
    close: pd.DataFrame,
    directions: Dict[str, int],
    categories: Optional[Dict[str, str]] = None,
    forward_days: int = FORWARD_DAYS,
    progress=None,
) -> Dict[str, object]:
    """批量评价。

    Returns:
        dict(factors=[FactorEval...], table=DataFrame, correlation=DataFrame)
    """
    categories = categories or {}
    forward = forward_returns(close, forward_days)
    results: List[FactorEval] = []
    total = len(factor_frames)
    for index, (name, frame) in enumerate(factor_frames.items(), 1):
        try:
            evaluated = evaluate_factor(
                name, frame, forward, close,
                direction=int(directions.get(name, 1)),
                category=categories.get(name, ""),
            )
        except Exception as exc:  # 单因子失败不该拖垮整份报告
            logger.warning("因子 %s 评价失败：%s", name, exc)
            evaluated = FactorEval(name=name)
        results.append(evaluated)
        if progress:
            progress(index, total, name)

    table = pd.DataFrame([r.to_dict() for r in results])
    correlation = correlation_matrix(factor_frames, forward)
    return {"factors": results, "table": table, "correlation": correlation,
            "forward_days": forward_days, "forward": forward}
