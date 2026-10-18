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
VALUATION, QUALITY = "valuation", "quality"
CATEGORY_LABELS = {
    TREND: "趋势",
    MOMENTUM: "动量",
    VOLUME: "量能",
    RISK: "风险",
    VALUATION: "估值",
    QUALITY: "质量",
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


# ================= 估值（5） =================
#
# 口径要点
# --------
# * ``ep = 1/PE``、``bp = 1/PB``、``sp = 1/PS`` 是「收益率」形态，
#   方向统一为 1（越大越便宜）。直接拿 PE 比大小会让「PE 越低越好」和
#   「PE 为负（亏损）」冲突 —— 负数 PE 取倒数后仍是负数，天然排在最后，
#   这正是我们要的：亏损股不该因为「PE 很低」而显得便宜。
# * ⚠️ 绝不能用 ``np.where(pe > 0, 1/pe, np.nan)`` 之外的写法把负 PE 抹成 NaN
#   或 0：抹成 NaN 会让亏损股在这一维「不参与评分」，等于把最该扣分的股票
#   放进了中性区；抹成 0 会让它排在正 PE 股票的中间。保持负值才是对的。
# * 估值只对**已上市且有估值数据**的票有意义。未上市期间是 NaN，由标准化层
#   当作缺失处理（不会变成 0 分）。


def _reciprocal(frame: pd.DataFrame) -> pd.DataFrame:
    """倒数，同时保留负值语义、把 0 和缺失都当缺失。

    0 取倒数会得到 inf —— 那会污染 MAD 去极值（inf 参与中位数计算会把
    整个截断区间拉飞）。所以只在 ``|x| > eps`` 时取倒数。
    """
    values = frame.to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(np.abs(values) > 1e-9, 1.0 / values, np.nan)
    return pd.DataFrame(out.astype("float32"), index=frame.index, columns=frame.columns)


@register("ep", VALUATION, 1, 1, "盈利收益率 = 1 / PE(TTM)")
def ep(panel: Panels) -> pd.DataFrame:
    return _reciprocal(panel.pe_ttm)


@register("bp", VALUATION, 1, 1, "账面市值比 = 1 / PB(MRQ)")
def bp(panel: Panels) -> pd.DataFrame:
    return _reciprocal(panel.pb_mrq)


@register("sp", VALUATION, 1, 1, "销售收益率 = 1 / PS(TTM)")
def sp(panel: Panels) -> pd.DataFrame:
    return _reciprocal(panel.ps_ttm)


@register("div_yield", VALUATION, 1, 1, "股息率（%），仅当最新一期报告披露时才可用")
def div_yield(panel: Panels) -> pd.DataFrame:
    """股息率。

    这个字段有两个坑，都实测过：

    1. **取值只在部分报告期披露**：年报、半年报通常有，一季报/三季报
       全市场 98%~99% 缺失。数据源不是「没分红」，而是「这期不给这个字段」。
       若把缺失当 0，所有票在 Q1/Q3 会集体被扣到最低分 —— 制造出一层与
       股息率无关的「报告期」暴露。
    2. **ffill 会让旧值挂很久**：茅台 2025-12-31 的 2.31% 会一直填到
       2026 年 Q1/Q2 公告之后，看起来覆盖率 97.6%，实际上已经过期半年。

    所以这里不直接用面板里的 ffill 值，而是要求「数据年龄 ≤ 180 天」：
    超过就置 NaN（缺失），交给 ``data_quality`` 记录覆盖率下降，
    而不是让一个十个月前算出来的股息率参与排名。
    """
    return _stale_mask(panel.div_yield, max_stale_days=180)


@register("peg_safe", VALUATION, -1, 1, "PEG（方向 -1，越小越便宜）；非正值视为缺失")
def peg_safe(panel: Panels) -> pd.DataFrame:
    """PEG 越小越便宜（direction=-1）。

    为什么叫 ``_safe``：``peg`` 有 30% 负值（净利下滑或亏损）。PEG 的经济含义
    只在「增速为正、PE 为正」时成立，负值不是「很便宜」而是「没有意义」。
    所以这里把 ``peg <= 0`` 置为 NaN 而不是留着一个 -3.2 让它拿高分
    （direction=-1 → 越小越好的标准化会把 -3.2 排到最前面）。
    """
    values = panel.peg.to_numpy(dtype="float64")
    out = np.where(np.isfinite(values) & (values > 0), values, np.nan)
    return pd.DataFrame(out.astype("float32"), index=panel.peg.index, columns=panel.peg.columns)


def _stale_mask(frame: pd.DataFrame, max_stale_days: int = 180) -> pd.DataFrame:
    """把「距今超过 ``max_stale_days`` 的老值」置为 NaN。

    判定的基准是**该字段自己最后一次「刷新」的那一天**，而不是参考字段。
    为什么不能用参考字段（踩过）：数据源的 ``div_yield`` 只在年报/半年报
    披露，但 ``roe_wa`` 每期都有；拿 ROE 的变化点当刷新信号，等于认为
    「发了一季报就该有新的股息率」，阈值永远不会触发，覆盖率虚高到 97%，
    而实际用的可能是 10 个月前的数字。

    所以这里用「值发生变化」来近似刷新：
    * 第 0 行或上一行是 NaN → 刷新
    * 值与上一行不同 → 刷新（浮点用精确比较，因为数据源给的就是同一个数）

    刷新之后连续超过 ``max_rows`` 个交易日仍未变化 → 视为过期，置 NaN。
    """
    values = frame.to_numpy(dtype="float64")
    out = values.copy()
    n_dates = values.shape[0]
    max_rows = int(round(max_stale_days * 250 / 365))

    for col in range(values.shape[1]):
        column = values[:, col]
        last_refresh = None
        for row in range(n_dates):
            current = column[row]
            if not np.isfinite(current):
                continue
            previous = column[row - 1] if row > 0 else np.nan
            if last_refresh is None or not np.isfinite(previous) or current != previous:
                last_refresh = row
            if row - last_refresh > max_rows:
                out[row, col] = np.nan

    return pd.DataFrame(out.astype("float32"), index=frame.index, columns=frame.columns)


# ================= 质量（6） =================
#
# 时点纪律：所有这些字段都来自 ``fundamental.build_panel``，
# 已经是「按公告日展开」的日频面板 —— 因子函数里不需要、也不允许再按
# ``report_date`` 去筛选。这是 P5 防前视的核心保证。


@register("roe_ttm", QUALITY, 1, 1, "加权 ROE（最新已公告报告期）")
def roe_ttm(panel: Panels) -> pd.DataFrame:
    """加权 ROE。

    注意这是**报告期口径**（一季报是单季，年报是全年），不是 TTM：
    数据源只给了累计加权 ROE，用它算 TTM 需要拆单季再拼，误差大于收益。
    所以做横截面比较时必须清楚：Q1 公告期全市场都是「单季 ROE」，
    年报期全市场都是「全年 ROE」——同一时点内口径一致，横截面可比。
    跨日期不可比，但我们的打分是横截面 rank，不受影响。
    """
    return panel.roe_wa.astype("float32")


@register("roe_slope", QUALITY, 1, 250, "近 4 期 ROE 的环比改善（最近一期 - 4 期前）")
def roe_slope(panel: Panels) -> pd.DataFrame:
    """ROE 的趋势，用「最近一期 - 一年前同期」而不是线性回归斜率。

    年度同比消除了季节性（Q1 天然低、年报天然高），比回归斜率稳健得多：
    后者在只有 4 个点时几乎完全由最后一个点的噪声决定。
    """
    # 每个交易日的「已公告期」是阶梯函数，往前推 250 个交易日 ≈ 一年前的同一报告期
    current = panel.roe_wa.astype("float64")
    out = current - current.shift(250)
    return out.astype("float32")


@register("rev_yoy", QUALITY, 1, 1, "营业收入同比增速（%）")
def rev_yoy(panel: Panels) -> pd.DataFrame:
    return panel.rev_yoy.astype("float32")


@register("profit_yoy", QUALITY, 1, 1, "归母净利润同比增速（%）")
def profit_yoy(panel: Panels) -> pd.DataFrame:
    return panel.profit_yoy.astype("float32")


@register("gross_margin", QUALITY, 1, 1, "毛利率（%）")
def gross_margin(panel: Panels) -> pd.DataFrame:
    """毛利率。

    缺失率 16%（银行/券商等金融企业不披露毛利率，这是行业属性不是数据缺陷）。
    所以它**必须**配合行业中性使用：不中性时整个银行板块在这一维全部缺失，
    中性后每个行业内各自比较，金融股由同行业的其他指标决定排名。
    """
    return panel.gross_margin.astype("float32")


@register("ocf_to_profit", QUALITY, 1, 1, "经营现金流 / 净利润（盈利质量）")
def ocf_to_profit(panel: Panels) -> pd.DataFrame:
    """经营现金流对净利润的覆盖倍数。

    ``ocf_ps / eps`` 而不是直接 ``ocf / net_profit``：前者是每股口径，
    天然消除了股本差异；两者在经济含义上等价（都除掉了股本）。
    分母 ``eps ≤ 0`` 时置 NaN —— 亏损公司的「现金流/利润」是负负得正
    的噪声，没有可比性。
    """
    ocf = panel.ocf_ps.to_numpy(dtype="float64")
    epsv = panel.eps.to_numpy(dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(
            np.isfinite(epsv) & (epsv > 1e-6) & np.isfinite(ocf),
            ocf / epsv,
            np.nan,
        )
    return pd.DataFrame(out.astype("float32"), index=panel.ocf_ps.index, columns=panel.ocf_ps.columns)


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
