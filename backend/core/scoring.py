"""标准化、维度合成与分档。

流程（规格 §5.4）
----------------
```
① 去极值    median ± k×MAD 截断
② 行业中性  行业内 rank → pct；行业内样本 < min_samples 时退回全市场分位
③ 方向统一  direction=-1 时取 1-pct
④ 维度分   维度内因子 pct 等权平均 × 100
⑤ 综合分   维度分加权求和 → 再在横截面上做一次 rank → score_pct(0~100)
```

为什么最后还要再 rank 一次
--------------------------
``score_abs``（维度分加权和）是「绝对刻度」，会随市场整体涨跌漂移：
牛市里所有票的动量分都高，score_abs 中位数会整体上移，跨日期不可比。
再做一次横截面 rank 得到 ``score_pct``，含义就变成「今天排在第几百分位」，
既消除了时间维度上的漂移，也让分档（前 10% 强烈看多）有稳定语义。

两个分数都保留：``score_abs`` 用于展示「有多强」，``score_pct`` 用于分档与选股。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: MAD 的正态一致性系数（1.4826 = 1/Φ⁻¹(0.75)），使 MAD 与标准差可比
MAD_SCALE = 1.4826

#: MAD 小于该值视为「该日横截面几乎同值」，跳过截断
MAD_EPS = 1e-12

DEFAULT_GRADES = [
    {"min_pct": 90, "label": "强烈看多", "key": "strong_buy"},
    {"min_pct": 70, "label": "看多", "key": "buy"},
    {"min_pct": 30, "label": "中性", "key": "neutral"},
    {"min_pct": 10, "label": "看空", "key": "sell"},
    {"min_pct": 0, "label": "强烈看空", "key": "strong_sell"},
]


@dataclass
class ScoringDiagnostics:
    """标准化过程的诊断信息（写进任务日志，便于判断结果是否可信）。"""

    factors: List[str] = field(default_factory=list)
    industry_neutral_factors: int = 0
    industry_fallback_factors: int = 0
    industry_covered_codes: int = 0
    industry_fallback_codes: int = 0
    industry_count: int = 0
    valid_dates: int = 0
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "factors": self.factors,
            "industry_neutral_factors": self.industry_neutral_factors,
            "industry_fallback_factors": self.industry_fallback_factors,
            "industry_covered_codes": self.industry_covered_codes,
            "industry_fallback_codes": self.industry_fallback_codes,
            "industry_count": self.industry_count,
            "valid_dates": self.valid_dates,
            "notes": self.notes,
        }


# ================= ① 去极值 =================

def winsorize_mad(frame: pd.DataFrame, k: float = 5.0) -> pd.DataFrame:
    """按行（每个交易日）做 median ± k×MAD 截断。

    用 MAD 而不是 3σ：财务/估值类因子的分布尾部极厚，标准差本身就被极值
    污染，用它做阈值等于「用异常值判断什么是异常」。

    实现上用 numpy 且只对**有值的行**计算：pandas 的 ``median(axis=1)``
    遇到全 NaN 行会走 ``np.nanmedian`` 并抛 ``RuntimeWarning: Mean of empty
    slice``，早期交易日（全市场都还没上市满 lookback）会天天刷屏。
    """
    if frame.empty:
        return frame

    values = frame.to_numpy(dtype="float64")
    valid = np.isfinite(values)
    has_value = valid.any(axis=1)
    if not has_value.any():
        return frame

    rows = np.flatnonzero(has_value)
    subset = values[rows]
    median = np.nanmedian(subset, axis=1, keepdims=True)
    deviation = np.abs(subset - median)
    mad = np.nanmedian(deviation, axis=1, keepdims=True) * MAD_SCALE

    usable = mad > MAD_EPS
    lower = np.where(usable, median - k * mad, -np.inf)
    upper = np.where(usable, median + k * mad, np.inf)
    clipped = np.clip(subset, lower, upper)

    out = values.copy()
    out[rows] = clipped
    return pd.DataFrame(out, index=frame.index, columns=frame.columns).astype(frame.dtypes.iloc[0])


# ================= ② 行业中性 =================

def industry_pct_rank(
    frame: pd.DataFrame,
    industry: Optional[pd.Series],
    min_samples: int = 5,
) -> tuple[pd.DataFrame, int]:
    """行业内百分位；行业样本不足时退回全市场分位。

    Returns:
        (分位面板, 实际做行业内的标的数)

    实现要点：转置成 ``code × date`` 后按行业 groupby，一次算完所有交易日 ——
    如果按天循环，4400 天 × 76 个行业会有几十万次小 rank，慢到不可用。
    """
    if frame.empty:
        return frame, 0
    if industry is None or len(industry) == 0:
        return frame.rank(axis=1, pct=True), 0

    transposed = frame.T
    labels = industry.reindex(transposed.index)
    counts = labels.value_counts()
    enough = labels.map(counts).fillna(0) >= min_samples

    if not enough.any():
        return frame.rank(axis=1, pct=True), 0

    filled = labels.fillna("__UNKNOWN__")
    grouped = transposed.groupby(filled).rank(pct=True)
    market = transposed.rank(pct=True)
    merged = grouped.where(enough, market)
    return merged.T, int(enough.sum())


# ================= ③ 方向统一 =================

def standardize(
    frame: pd.DataFrame,
    direction: int = 1,
    industry: Optional[pd.Series] = None,
    mad_k: float = 5.0,
    min_samples: int = 5,
) -> tuple[pd.DataFrame, int]:
    """完整标准化：去极值 → 行业中性 → 方向统一。"""
    clipped = winsorize_mad(frame, mad_k)
    pct, covered = industry_pct_rank(clipped, industry, min_samples)
    if direction < 0:
        pct = 1.0 - pct
    return pct, covered


# ================= ④ 维度分 =================

def dimension_score(factor_pcts: Dict[str, pd.DataFrame]) -> Optional[pd.DataFrame]:
    """维度内因子 pct 等权平均 × 100（忽略 NaN，全 NaN 处保持 NaN）。

    手写「有效值求和 / 有效值个数」而不是 ``np.nanmean``：
    后者遇到整片全 NaN 的切片会抛 ``RuntimeWarning: Mean of empty slice``，
    在每日任务里刷屏且无法用 errstate 抑制。
    """
    frames = [f for f in factor_pcts.values() if f is not None and not f.empty]
    if not frames:
        return None
    stack = np.stack([f.to_numpy(dtype="float64") for f in frames], axis=0)
    valid = np.isfinite(stack)
    count = valid.sum(axis=0)
    total = np.where(valid, stack, 0.0).sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        averaged = np.where(count > 0, total / np.maximum(count, 1), np.nan) * 100.0
    return pd.DataFrame(averaged, index=frames[0].index, columns=frames[0].columns)


# ================= ⑤ 综合分 =================

def composite_score(
    dim_scores: Dict[str, pd.DataFrame],
    weights: Dict[str, float],
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """维度分加权 → score_abs；再横截面 rank → score_pct。

    Returns:
        (score_abs 0~100, score_pct 0~100, quality 0~1)

    ``quality`` 是「该标的当日实际参与合成的维度权重 / 总权重」。
    早期数据不足时某些维度整片 NaN，此时权重会自动再归一（保证分数不空缺），
    但分数的可信度下降 —— 用 quality 把这个事实显式记下来，
    而不是让「只有一个维度有值」的分数看起来和「四维齐全」的一样可靠。
    """
    usable = {
        key: frame
        for key, frame in dim_scores.items()
        if frame is not None and key in weights and weights.get(key, 0) > 0
    }
    if not usable:
        raise ValueError("没有任何可用的维度分，无法合成")

    total_weight = float(sum(weights[key] for key in usable))
    reference = next(iter(usable.values()))
    accumulator = np.zeros(reference.shape, dtype="float64")
    weight_used = np.zeros(reference.shape, dtype="float64")

    for key, frame in usable.items():
        values = frame.to_numpy(dtype="float64")
        valid = np.isfinite(values)
        accumulator[valid] += values[valid] * weights[key]
        weight_used[valid] += weights[key]

    with np.errstate(invalid="ignore", divide="ignore"):
        absolute = np.where(weight_used > 0, accumulator / weight_used, np.nan)
        quality = np.where(weight_used > 0, weight_used / total_weight, np.nan)

    score_abs = pd.DataFrame(absolute, index=reference.index, columns=reference.columns)
    score_pct = score_abs.rank(axis=1, pct=True) * 100.0
    data_quality = pd.DataFrame(quality, index=reference.index, columns=reference.columns)
    return score_abs, score_pct, data_quality


# ================= 分档 =================

def assign_grade(score_pct: pd.DataFrame, grades: Optional[Sequence[dict]] = None) -> pd.DataFrame:
    """按 ``score_pct`` 固定比例分档（从高到低匹配第一个满足的阈值）。"""
    grades = list(grades or DEFAULT_GRADES)
    ordered = sorted(grades, key=lambda g: -float(g.get("min_pct", 0)))
    values = score_pct.to_numpy(dtype="float64")
    out = np.full(values.shape, None, dtype=object)

    assigned = np.zeros(values.shape, dtype=bool)
    for rule in ordered:
        threshold = float(rule.get("min_pct", 0))
        hit = np.isfinite(values) & (values >= threshold) & ~assigned
        out[hit] = rule.get("label")
        assigned |= hit

    return pd.DataFrame(out, index=score_pct.index, columns=score_pct.columns)


def grade_key(label: str, grades: Optional[Sequence[dict]] = None) -> str:
    for rule in grades or DEFAULT_GRADES:
        if rule.get("label") == label:
            return rule.get("key", "")
    return ""


def distribution(grade_frame: pd.DataFrame, day=None) -> Dict[str, int]:
    """某日（默认最后一日）的评级分布。"""
    if grade_frame.empty:
        return {}
    row = grade_frame.iloc[-1] if day is None else grade_frame.loc[pd.Timestamp(day)]
    counts = row.value_counts()
    return {str(k): int(v) for k, v in counts.items() if k is not None}


# ================= 一站式 =================

def score_panels(
    factor_frames: Dict[str, pd.DataFrame],
    factor_meta: Dict[str, dict],
    dimension_of: Dict[str, str],
    weights: Dict[str, float],
    industry: Optional[pd.Series] = None,
    mad_k: float = 5.0,
    min_samples: int = 5,
    grades: Optional[Sequence[dict]] = None,
    on_progress=None,
) -> dict:
    """把「原始因子面板」加工成最终打分面板。

    Args:
        factor_frames: 因子名 → 原始值面板
        factor_meta: 因子名 → {direction, ...}
        dimension_of: 因子名 → 维度名
        weights: 维度名 → 权重

    Returns:
        dict(score_abs, score_pct, grade, dims, diagnostics)
    """
    diagnostics = ScoringDiagnostics(factors=list(factor_frames))
    if industry is not None and len(industry):
        counts = industry.reindex(list(next(iter(factor_frames.values())).columns)).value_counts()
        diagnostics.industry_count = int(len(counts))
        diagnostics.industry_covered_codes = int(counts[counts >= min_samples].sum())
        diagnostics.industry_fallback_codes = int(counts[counts < min_samples].sum())
        diagnostics.notes.append(
            f"行业中性：{diagnostics.industry_count} 个行业，"
            f"{diagnostics.industry_covered_codes} 只走行业内分位，"
            f"{diagnostics.industry_fallback_codes} 只因行业样本 <{min_samples} 退回全市场分位"
        )

    # 逐因子标准化，按维度归集
    by_dimension: Dict[str, Dict[str, pd.DataFrame]] = {}
    total = len(factor_frames)
    for index, (name, frame) in enumerate(factor_frames.items(), 1):
        meta = factor_meta.get(name, {})
        direction = int(meta.get("direction", 1))
        pct, covered = standardize(
            frame, direction=direction, industry=industry,
            mad_k=mad_k, min_samples=min_samples,
        )
        if covered:
            diagnostics.industry_neutral_factors += 1
        else:
            diagnostics.industry_fallback_factors += 1
        by_dimension.setdefault(dimension_of.get(name, "other"), {})[name] = pct
        if on_progress:
            on_progress(index, total, name)

    # 维度分
    dim_scores: Dict[str, pd.DataFrame] = {}
    for dimension, frames in by_dimension.items():
        score = dimension_score(frames)
        if score is not None:
            dim_scores[dimension] = score

    score_abs, score_pct, data_quality = composite_score(dim_scores, weights)
    grade = assign_grade(score_pct, grades)

    valid_rows = score_pct.notna().any(axis=1)
    diagnostics.valid_dates = int(valid_rows.sum())
    quality_values = data_quality.to_numpy(dtype="float64")
    finite_quality = np.isfinite(quality_values)
    full_ratio = (
        float((quality_values[finite_quality] >= 0.999).mean()) if finite_quality.any() else 0.0
    )
    # 文案里的维度数必须动态取：v1.x 是四维、v2.x 是六维，硬编码「四维」
    # 会在六维版本上给出自相矛盾的诊断（实测踩过：v2.0.0 里写着「四维齐全 58%」）
    dim_count = len(dim_scores)
    diagnostics.notes.append(
        f"有效交易日 {diagnostics.valid_dates} 天；"
        f"{dim_count} 维齐全的「股票×日」格子占 {full_ratio:.1%}，"
        f"其余因 lookback 不足或基本面缺失而缺部分维度"
        f"（权重已自动再归一，data_quality 字段记录实际覆盖率）"
    )

    return {
        "score_abs": score_abs,
        "score_pct": score_pct,
        "grade": grade,
        "data_quality": data_quality,
        "dims": dim_scores,
        "diagnostics": diagnostics,
    }
