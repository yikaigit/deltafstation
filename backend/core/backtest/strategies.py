"""策略层：把打分快照变成目标权重（规格 §6.1 类型 A）。

策略 ``score_portfolio``
------------------------
每个调仓日 T 日收盘后：

1. 从打分快照取当日的 ``score_pct`` 横截面；
2. 剔除 ST（``exclude_st``）与百分位低于 ``min_score_pct`` 的标的；
3. 取前 ``top_n`` 名；
4. 按 ``weight_method`` 给权（equal 等权 / score 按分数占比）；
5. ``max_weight`` 封顶，超出部分按比例分给未封顶的标的；
6. 乘以大盘择时敞口 ``regime``（见下）。

**目标权重在信号日一次算定，执行阶段不再改动** —— 被涨跌停/停牌挡下来的
那一份权重就留在现金里（规格 §6.2「资金留存」），不会临时补配给别的股票，
否则回测会因为「临时改配方」而变得不可复现。

大盘择时（regime）
----------------
沿用参考项目的三档规则（先验规则，非参数寻优结果），基准默认沪深300：

| 条件 | 敞口 |
|---|---|
| 收盘 > 年线 且 季线 > 年线（牛市） | 100% |
| 二者只满足其一（震荡） | 50% |
| 收盘 < 年线 且 季线 < 年线（熊市） | 0% |

均线数据不足（窗口前 200 天）按 50% 处理 —— 「看不清楚时半仓」比
「看不清楚时满仓」保守，也比空仓更容易解释。

自定义策略（类型 B）
------------------
v1 只实现类型 A。类型 B 的约定（见规格 §6.1）是：

.. code-block:: python

    def generate_target_weights(panels, date) -> dict[str, float]:
        '''date 日收盘后的目标权重，和为 1（或 0 表示空仓）'''

引擎只负责执行与结算，v2 再开放注册入口。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

WEIGHT_METHODS = ("equal", "score")


@dataclass
class StrategyParams:
    """``score_portfolio`` 的全部参数（默认值与规格 §6.1 一致）。"""

    top_n: int = 20
    rebal_days: int = 20
    weight_method: str = "equal"
    regime: bool = True
    regime_ma_fast: int = 50
    regime_ma_slow: int = 200
    min_score_pct: float = 0.0
    max_weight: float = 0.10
    exclude_st: bool = True
    initial_cash: float = 1_000_000.0
    score_version: Optional[str] = None
    benchmark: str = "sh.000300"
    is_ratio: float = 0.7
    start: Optional[str] = None
    end: Optional[str] = None
    costs: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.weight_method not in WEIGHT_METHODS:
            raise ValueError(f"weight_method 只支持 {WEIGHT_METHODS}，收到 {self.weight_method}")
        if self.top_n < 1:
            raise ValueError("top_n 必须 ≥ 1")
        if self.rebal_days < 1:
            raise ValueError("rebal_days 必须 ≥ 1")
        if not 0 < self.max_weight <= 1:
            raise ValueError("max_weight 必须在 (0, 1] 区间")

    def warnings(self) -> List[str]:
        """参数组合的软性问题。

        ``max_weight×top_n < 1`` 意味着「每只票都顶到上限也用不满资金」——
        这是**合法的保守配置**（常态保留现金），不是错误，所以不 raise，
        只由引擎写进 ``result.warnings`` 让报告自证。
        """
        out: List[str] = []
        capacity = self.max_weight * self.top_n
        if capacity < 1.0 - 1e-9:
            out.append(
                f"max_weight×top_n = {capacity:.2f} < 1，常态将保留 "
                f"{(1.0 - capacity) * 100:.0f}% 现金（上限过严或持股过散）"
            )
        return out

    def to_dict(self) -> dict:
        return {
            "top_n": self.top_n,
            "rebal_days": self.rebal_days,
            "weight_method": self.weight_method,
            "regime": self.regime,
            "regime_ma_fast": self.regime_ma_fast,
            "regime_ma_slow": self.regime_ma_slow,
            "min_score_pct": self.min_score_pct,
            "max_weight": self.max_weight,
            "exclude_st": self.exclude_st,
            "initial_cash": self.initial_cash,
            "score_version": self.score_version,
            "benchmark": self.benchmark,
            "is_ratio": self.is_ratio,
            "start": self.start,
            "end": self.end,
        }

    @classmethod
    def from_payload(cls, payload: Optional[dict], cfg=None) -> "StrategyParams":
        """从 API/CLI 载荷构造，未知键忽略，缺省值取配置 ``backtest`` 段。"""
        payload = dict(payload or {})

        def pick(key, cast, fallback):
            if key in payload and payload[key] not in (None, ""):
                return cast(payload[key])
            if cfg is not None:
                return cast(cfg.get(f"backtest.{key}", fallback))
            return fallback

        costs = {}
        if cfg is not None:
            costs = {
                "commission": float(cfg.get("costs.commission", 0.00025)),
                "min_commission": float(cfg.get("costs.min_commission", 5.0)),
                "stamp_duty": float(cfg.get("costs.stamp_duty", 0.0005)),
                "slippage": float(cfg.get("costs.slippage", 0.0005)),
                "lot_size": int(cfg.get("costs.lot_size", 100)),
            }
        return cls(
            top_n=pick("top_n", int, 20),
            rebal_days=pick("rebal_days", int, 20),
            weight_method=str(payload.get("weight_method") or "equal"),
            regime=bool(payload.get("regime", True)),
            regime_ma_fast=int(payload.get("regime_ma_fast") or 50),
            regime_ma_slow=int(payload.get("regime_ma_slow") or 200),
            min_score_pct=float(payload.get("min_score_pct") or 0.0),
            max_weight=pick("max_weight", float, 0.10),
            exclude_st=bool(payload.get("exclude_st", True)),
            initial_cash=pick("initial_cash", float, 1_000_000.0),
            score_version=payload.get("version") or payload.get("score_version") or None,
            benchmark=str(payload.get("benchmark") or "sh.000300"),
            is_ratio=float(payload.get("is_ratio") or 0.7),
            start=payload.get("start"),
            end=payload.get("end"),
            costs=costs,
        )


# ---------------- 大盘择时 ----------------


def regime_exposure(
    bench_close: Optional[pd.Series],
    ma_fast: int = 50,
    ma_slow: int = 200,
) -> pd.Series:
    """三档敞口序列（1.0 / 0.5 / 0.0），index 与 bench_close 对齐。"""
    if bench_close is None or len(bench_close) == 0:
        return pd.Series(dtype="float64")
    close = bench_close.astype("float64")
    ma_f = close.rolling(ma_fast, min_periods=ma_fast).mean()
    ma_s = close.rolling(ma_slow, min_periods=ma_slow).mean()

    above = close > ma_s
    ma_up = ma_f > ma_s
    exposure = pd.Series(0.5, index=close.index, dtype="float64")
    exposure[above & ma_up] = 1.0
    exposure[(~above) & (~ma_up)] = 0.0
    # 均线未成型 → 0.5（看不清楚时半仓）
    warm = ma_s.isna()
    exposure[warm] = 0.5
    return exposure


# ---------------- 权重计算 ----------------


def cap_and_redistribute(weights: Dict[str, float], max_weight: float) -> Dict[str, float]:
    """单票上限封顶，超出部分按比例分给未封顶的标的。

    先把权重归一化到 1，再迭代封顶：每一轮把超标的压到 ``max_weight``，
     freed 出来的额度按比例加给**尚未触顶**的标的；已经触顶的进 ``fixed``
    不再参与分配，因此最多 ``n`` 轮收敛。

    特殊情形：所有票都触顶且没有承接空间（``max_weight×n < 1``，例如
    top_n=5 + max_weight=0.10 的等权组合）→ 返回的权重和小于 1，
    差额按引擎的「资金留存」规则留在现金里，不强行摊到其他票上。
    """
    if not weights:
        return {}
    if max_weight is None or max_weight >= 1.0:
        return dict(weights)
    total = sum(weights.values())
    if total <= 0:
        return {}
    items = {k: v / total for k, v in weights.items()}

    fixed: set = set()
    for _ in range(len(items) + 1):
        over = [k for k, v in items.items() if v > max_weight + 1e-12]
        if not over:
            break
        excess = sum(items[k] - max_weight for k in over)
        items = {k: (max_weight if k in over else v) for k, v in items.items()}
        fixed.update(over)
        room_total = sum(v for k, v in items.items() if k not in fixed)
        if room_total <= 0 or excess <= 0:
            break
        scale = (room_total + excess) / room_total
        items = {k: (v if k in fixed else v * scale) for k, v in items.items()}
    return items


def pick_codes(
    row: pd.Series,
    top_n: int,
    min_score_pct: float,
    st_codes: set,
) -> List[str]:
    """按 score_pct 取前 top_n（剔除 ST 与低分位）。"""
    values = row.dropna()
    if st_codes:
        values = values[~values.index.isin(st_codes)]
    if min_score_pct and min_score_pct > 0:
        values = values[values >= min_score_pct]
    if values.empty:
        return []
    return list(values.sort_values(ascending=False).head(top_n).index)


def weights_for_picks(picks: Sequence[str], scores: pd.Series, method: str) -> Dict[str, float]:
    """给入选股票分配权重（等权 / 按分数占比）。"""
    picks = list(picks)
    if not picks:
        return {}
    if method == "score":
        raw = np.array([max(float(scores.get(code, 0.0)), 0.0) for code in picks])
        total = raw.sum()
        if total <= 0:                       # 全 0 分退化为等权
            return {code: 1.0 / len(picks) for code in picks}
        return {code: float(r / total) for code, r in zip(picks, raw)}
    return {code: 1.0 / len(picks) for code in picks}


# ---------------- 目标权重总装 ----------------


def signal_dates(dates: pd.DatetimeIndex, rebal_days: int) -> pd.DatetimeIndex:
    """调仓信号日：第 0、rebal_days、2×rebal_days… 个交易日。

    调仓间隔 ≥ 5 个交易日天然满足「当日买入不卖」的 T+1 约束；间隔为 1~4 时
    引擎仍按 T+1 开盘成交，不会当日回转。
    """
    if len(dates) == 0:
        return dates
    step = max(1, int(rebal_days))
    return dates[::step]


def build_target_weights(
    score_wide: pd.DataFrame,
    dates: pd.DatetimeIndex,
    params: StrategyParams,
    st_codes: Optional[set] = None,
    exposure: Optional[pd.Series] = None,
) -> Dict[pd.Timestamp, Dict[str, float]]:
    """为每个调仓信号日计算目标权重。

    Args:
        score_wide: date × code 的 score_pct 面板
        dates: 回测区间内的交易日（面板日期）
        params: 策略参数
        st_codes: 需排除的 ST 标的集合
        exposure: 大盘敞口序列（index=date），None 表示不满仓择时（恒 1.0）

    Returns:
        {信号日: {code: weight}}，权重和为 1（熊市/无入选 → 空 dict = 空仓）
    """
    st_codes = st_codes or set()
    signals = signal_dates(dates, params.rebal_days)
    targets: Dict[pd.Timestamp, Dict[str, float]] = {}

    for day in signals:
        if day not in score_wide.index:
            continue
        row = score_wide.loc[day]
        if row.dropna().empty:
            # 当日**没有快照**（区别于「有数据但全部被过滤」）→ 不调仓，
            # 维持现有持仓。缺一天数据就清仓会把「数据缺口」误判成「看空」，
            # 而那种回测结果既不可复现也无法解释。
            continue
        picks = pick_codes(row, params.top_n,
                            params.min_score_pct, st_codes)
        if not picks:
            targets[day] = {}           # 有数据但全部被过滤 → 明确空仓
            continue

        weights = weights_for_picks(picks, score_wide.loc[day], params.weight_method)
        weights = cap_and_redistribute(weights, params.max_weight)

        expo = 1.0
        if params.regime and exposure is not None and len(exposure):
            value = exposure.reindex([day]).iloc[0] if day in exposure.index else None
            if value is None or pd.isna(value):
                value = 0.5                      # 基准在该日无数据 → 半仓
            expo = float(value)
        if expo <= 0:
            targets[day] = {}
            continue
        if expo < 1.0:
            weights = {k: v * expo for k, v in weights.items()}
        targets[day] = weights

    if not targets:
        logger.warning("区间内没有任何调仓信号日命中打分快照，回测将全程空仓")
    return targets


def resolve_st_codes(universe: Optional[pd.DataFrame]) -> set:
    """从 universe 里取 ST 集合（缺列/缺表 → 空集合）。"""
    if universe is None or universe.empty:
        return set()
    if "is_st" not in universe.columns or "code" not in universe.columns:
        return set()
    try:
        flagged = universe[universe["is_st"].astype(bool)]
    except Exception:  # pragma: no cover - 脏数据兜底
        return set()
    return set(flagged["code"].astype(str))
