"""v1 技术因子库（趋势 / 动量 / 量能 / 风险，共 19 个）。

约定
----
* 因子函数只读 ``Panels``，**不允许 IO**，不做任何横截面处理；
* 返回 float32 面板（index=date, columns=code），缺失为 NaN；
* ``direction=1`` 越大越好，``-1`` 越小越好 —— 标准化阶段统一翻成「越大越好」；
* ``lookback`` 是「至少需要多少天历史才有意义」，用于决定因子有效起点。

为什么把方向放在元数据里而不是让函数返回「已经取反的值」
------------------------------------------------------
方向是**口径**而不是**计算**。同一个因子换个研究视角可能就想反过来用
（比如低波动 vs 高弹性）。放在 registry 里，改配置即可换方向，不用改代码，
也便于 P5 的评价体系按方向统一算 IC。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from . import indicators as ind
from .panels import Panels

logger = logging.getLogger(__name__)

TREND, MOMENTUM, VOLUME, RISK = "trend", "momentum", "volume", "risk"
CATEGORY_LABELS = {
    TREND: "趋势",
    MOMENTUM: "动量",
    VOLUME: "量能",
    RISK: "风险",
}


@dataclass(frozen=True)
class Factor:
    """因子元数据 + 计算函数。"""

    name: str
    category: str
    direction: int
    lookback: int
    description: str
    compute: Callable[[Panels], pd.DataFrame]

    def __call__(self, panel: Panels) -> pd.DataFrame:
        return self.compute(panel)


#: 全局注册表（顺序即配置里出现的顺序，便于日志对照）
FACTORS: Dict[str, Factor] = {}


def register(name: str, category: str, direction: int, lookback: int,
             description: str) -> Callable:
    def wrapper(fn: Callable[[Panels], pd.DataFrame]) -> Callable:
        FACTORS[name] = Factor(name, category, direction, lookback, description, fn)
        return fn

    return wrapper


def _pct_change(frame: pd.DataFrame, periods: int) -> pd.DataFrame:
    """n 日收益率。

    不用 ``pct_change`` 是因为它默认会 ``pad`` 填充 NaN，
    未上市期间会被算成 0 收益混进分位统计。
    """
    return (frame / frame.shift(periods) - 1.0).astype("float32")


# ================= 趋势（6） =================

@register("ma_bias_20", TREND, 1, 20, "收盘价相对 20 日均线的乖离率")
def ma_bias_20(panel: Panels) -> pd.DataFrame:
    return (panel.close / ind.sma(panel.close, 20) - 1.0).astype("float32")


@register("ma_bias_60", TREND, 1, 60, "收盘价相对 60 日均线的乖离率")
def ma_bias_60(panel: Panels) -> pd.DataFrame:
    return (panel.close / ind.sma(panel.close, 60) - 1.0).astype("float32")


@register("ma_bias_120", TREND, 1, 120, "收盘价相对 120 日均线的乖离率")
def ma_bias_120(panel: Panels) -> pd.DataFrame:
    return (panel.close / ind.sma(panel.close, 120) - 1.0).astype("float32")


@register("macd_hist_norm", TREND, 1, 34, "MACD 柱除以价格归一（消除股价量纲）")
def macd_hist_norm(panel: Panels) -> pd.DataFrame:
    hist = ind.macd_hist(panel.close)
    return (hist / panel.close.replace(0, np.nan)).astype("float32")


@register("adx_dir", TREND, 1, 28, "ADX 强度 × 方向（PDI>MDI 为多头）")
def adx_dir(panel: Panels) -> pd.DataFrame:
    """``sign(PDI-MDI) × min(ADX/40, 1)``。

    ADX 本身只衡量趋势强度、不含方向，所以必须乘上 ``PDI-MDI`` 的符号，
    否则「单边下跌」会被判成强趋势而给高分。
    """
    adx, pdi, mdi = ind.adx(panel.close, panel.high, panel.low)
    strength = (adx / 40.0).clip(upper=1.0)
    direction = np.sign((pdi - mdi).to_numpy(dtype="float64"))
    out = strength.to_numpy(dtype="float64") * direction
    return pd.DataFrame(out.astype("float32"), index=adx.index, columns=adx.columns)


@register("slope_60", TREND, 1, 60, "对数价格 60 日线性回归斜率（年化）")
def slope_60(panel: Panels) -> pd.DataFrame:
    """``LINEARREG_SLOPE(ln(close), 60) × 250``。

    取对数再回归，斜率就是「日均对数收益率」，× 250 年化后可跨股票比较；
    直接对价格回归会得到「元/天」，高价股的斜率天然更大，完全不可比。
    """
    log_close = panel.close.where(panel.close > 0).apply(np.log)
    return (ind.linearreg_slope(log_close, 60) * 250.0).astype("float32")


# ================= 动量（5） =================

@register("roc_20", MOMENTUM, 1, 20, "20 日收益率")
def roc_20(panel: Panels) -> pd.DataFrame:
    return _pct_change(panel.close, 20)


@register("roc_60", MOMENTUM, 1, 60, "60 日收益率")
def roc_60(panel: Panels) -> pd.DataFrame:
    return _pct_change(panel.close, 60)


@register("roc_120", MOMENTUM, 1, 120, "120 日收益率")
def roc_120(panel: Panels) -> pd.DataFrame:
    return _pct_change(panel.close, 120)


@register("rs_60", MOMENTUM, 1, 60, "60 日相对基准的超额收益")
def rs_60(panel: Panels) -> pd.DataFrame:
    """``(1+R_i) / (1+R_bench) - 1``，60 日窗口。

    ⚠️ 不用规格初稿写的 ``Σ(r_i - r_bench)``：算术累加日收益差会**系统性高估
    高波动股**。因为 ``Σ log(1+r) ≈ Σr - Σr²/2``，波动越大低估越多，做差之后
    波动大的票反而显得超额更多 —— 相当于给因子偷偷加了一层「高波动暴露」。
    实测同一时点：算术累加中位 +7.88%，累计收益比中位 +6.55%，差 1.3 个点。

    基准缺失时退化为绝对收益（并打日志，不静默改语义）。
    """
    close = panel.close
    stock_growth = close / close.shift(60)
    bench = panel.bench_ret
    if bench is not None and len(bench):
        aligned = bench.reindex(close.index).astype("float64")
        # ⚠️ 首日收益（pct_change 的首值）与 reindex 出来的缺失日必须填 0：
        # pandas 的 cumprod 会把首个 NaN 原样留在 growth[0]，
        # 于是 bench_growth[60] = growth[60]/growth[0] = NaN ——
        # 整整一个交易日的 rs_60 会静默消失。填 0 的含义是「该日指数无变化」，
        # 这是缺数据时唯一不引入幽灵收益的口径。
        returns = aligned.fillna(0.0)
        growth = (1.0 + returns).cumprod()
        bench_growth = (growth / growth.shift(60)).to_numpy(dtype="float64")[:, None]
        with np.errstate(divide="ignore", invalid="ignore"):
            out = stock_growth.to_numpy(dtype="float64") / bench_growth - 1.0
        return pd.DataFrame(out.astype("float32"), index=close.index, columns=close.columns)
    logger.warning("rs_60：缺少基准收益，退化为绝对收益")
    return (stock_growth - 1.0).astype("float32")


#: RSI 健康度映射：45~62 是「趋势健康且未过热」的最优区间
_RSI_XP = [0.0, 25.0, 40.0, 45.0, 62.0, 72.0, 80.0, 100.0]
_RSI_FP = [0.0, 0.10, 0.60, 1.00, 1.00, 0.55, 0.15, 0.00]


@register("rsi_health", MOMENTUM, 1, 20, "RSI14 健康度（45~62 最优，>78 过热扣分）")
def rsi_health(panel: Panels) -> pd.DataFrame:
    """把 RSI 映射成 0~1 的「健康度」。

    RSI 不是单调因子：太低=超卖（可能是下跌趋势），太高=过热（回撤风险），
    中间偏强才理想。因此用分段线性映射而不是直接当因子用。
    断点：<25 极弱、40~45 偏强、45~62 最优、>72 转弱、>80 过热。
    """
    values = ind.rsi(panel.close, 14)
    array = values.to_numpy(dtype="float64")
    mapped = np.interp(array, _RSI_XP, _RSI_FP)
    mapped[~np.isfinite(array)] = np.nan
    return pd.DataFrame(mapped.astype("float32"), index=values.index, columns=values.columns)


# ================= 量能（4） =================

@register("vol_ratio", VOLUME, 1, 60, "5 日均量 / 60 日均量（放量程度）")
def vol_ratio(panel: Panels) -> pd.DataFrame:
    short = ind.sma(panel.volume, 5)
    long = ind.sma(panel.volume, 60)
    return (short / long.replace(0, np.nan)).astype("float32")


@register("obv_z", VOLUME, 1, 60, "OBV 相对其 60 日均值的 z 分数")
def obv_z(panel: Panels) -> pd.DataFrame:
    """``(OBV - MA60(OBV)) / STD60(OBV)``。

    OBV 是累计量、量纲随股票和时间变化，直接比较无意义；
    减去自身 60 日均值再除以标准差，才能横截面可比。
    """
    values = ind.obv(panel.close, panel.volume)
    mean = values.rolling(60, min_periods=30).mean()
    std = values.rolling(60, min_periods=30).std()
    return ((values - mean) / std.replace(0, np.nan)).astype("float32")


@register("mfi_14", VOLUME, 1, 15, "资金流量指标（带量的 RSI）")
def mfi_14(panel: Panels) -> pd.DataFrame:
    return ind.mfi(panel.high, panel.low, panel.close, panel.volume, 14)


@register("turn_rank", VOLUME, 1, 250, "换手率的 250 日滚动分位")
def turn_rank(panel: Panels) -> pd.DataFrame:
    """换手率的时序分位，用于衡量「当前关注度相对自身历史的高低」。

    横截面直接比换手率会被「大盘股天然低换手」带偏，取时序分位后
    每只票都变成「相对自己」的活跃度，可比性更好。
    """
    return ind.rolling_pct_rank(panel.turn, 250)


# ================= 风险（4） =================

@register("atr_pct_rank", RISK, -1, 260, "ATR/收盘价的 250 日滚动分位（越高越波动）")
def atr_pct_rank(panel: Panels) -> pd.DataFrame:
    atr = ind.atr(panel.high, panel.low, panel.close, 14)
    ratio = atr / panel.close.replace(0, np.nan)
    return ind.rolling_pct_rank(ratio, 250)


@register("hv_60", RISK, -1, 60, "60 日年化波动率")
def hv_60(panel: Panels) -> pd.DataFrame:
    ret = panel.close.pct_change(fill_method=None)
    return ind.rolling_std(ret, 60) * np.sqrt(250.0)


@register("downside_vol", RISK, -1, 60, "60 日下行波动率（只计负收益）")
def downside_vol(panel: Panels) -> pd.DataFrame:
    return ind.downside_vol(panel.close, 60)


@register("dd_1y", RISK, 1, 250, "距 250 日最高点的回撤（越接近 0 越好）")
def dd_1y(panel: Panels) -> pd.DataFrame:
    peak = ind.rolling_max(panel.close, 250)
    return (panel.close / peak.replace(0, np.nan) - 1.0).astype("float32")


# ================= 批量 =================

def compute(name: str, panel: Panels) -> pd.DataFrame:
    if name not in FACTORS:
        raise KeyError(f"未注册的因子：{name}")
    return FACTORS[name](panel)


def compute_many(names: Sequence[str], panel: Panels,
                 on_progress: Optional[Callable[[int, int, str], None]] = None) -> Dict[str, pd.DataFrame]:
    """批量计算，单因子失败不中断（记录后跳过）。"""
    out: Dict[str, pd.DataFrame] = {}
    total = len(names)
    for index, name in enumerate(names, 1):
        try:
            frame = compute(name, panel)
            out[name] = frame
        except Exception as exc:
            logger.warning("因子 %s 计算失败：%s", name, exc)
        if on_progress:
            on_progress(index, total, name)
    return out


def names_by_category(category: str) -> List[str]:
    return [name for name, f in FACTORS.items() if f.category == category]


def categories() -> List[str]:
    seen = []
    for f in FACTORS.values():
        if f.category not in seen:
            seen.append(f.category)
    return seen
