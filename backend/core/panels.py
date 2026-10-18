"""面板容器：把长表行情转成「index=交易日, columns=code」的宽表。

为什么需要这一层
----------------
因子计算的天然形态是面板运算（``close / close.rolling(20).mean()``），
直接对长表 groupby 会有两个问题：一是每算一个因子都要重新分组，二是
跨股票的横截面比较（rank / 分位）在长表上非常别扭。这里一次性把长表
透视为宽表，后面所有因子只做纯矩阵运算。

复权口径
--------
``open/high/low/close`` 一律是**后复权**（不复权价 × 当日 back_factor），
这是回测唯一安全的口径：
* 前复权含未来函数（基准日之后的除权会改写历史价格），只能画图；
* 不复权在除权日会凭空跳空，动量类因子会被除权噪声污染。

``raw_close`` 保留不复权价，用于「市值 / 价格档位 / 涨跌停判定」这类
必须看真实报价的场景。``volume/amount/turn/pct_chg/tradestatus`` 保持原样。

估值与财务
----------
``pe_ttm / pb_mrq / ps_ttm / peg / total_mv`` 来自 ``valuation`` 表（东财日频横截面）。
``roe_wa / rev_yoy / ... / rev_ttm / net_profit_ttm`` 来自 ``fundamental``，由
``fundamental.build_panel`` 按**公告日**展开 —— 关键点是「T 日只能看到 T 日之前
已公告的财报」，绝不能按 ``report_date`` 取（那会在 7 月 1 日就用上 8 月才披露的
半年报，是典型的前视偏差）。

缺失值语义
----------
未上市 / 退市之后是 ``NaN``（不是 0）—— 因子计算必须能区分「没有数据」
和「数据是 0」。停牌日**有**价格（停牌前收盘价）且 ``tradestatus=0``，
由使用方决定是否剔除。
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Optional, Sequence

import numpy as np
import pandas as pd

from .adjustment import expand_factors
from .config import Config, get_config
from .lake import Lake, get_lake

logger = logging.getLogger(__name__)

#: 参与复权换算的价格字段
PRICE_FIELDS = ("open", "high", "low", "close")
#: 需要透视的全部字段（顺序即 unstack 顺序）
PANEL_FIELDS = (
    "open", "high", "low", "close",
    "volume", "amount", "turn", "pct_chg", "tradestatus",
)

#: 从数据湖读取的列（裁剪掉 is_st 等因子用不到的列，减少内存）
_READ_COLUMNS = ["date", "code", *PANEL_FIELDS]

DEFAULT_BENCHMARK = "sh.000300"


class Panels:
    """宽表面板容器。

    属性访问即取字段：``panel.close`` / ``panel.volume`` / ``panel.industry``。
    """

    def __init__(
        self,
        frames: Dict[str, pd.DataFrame],
        industry: Optional[pd.Series] = None,
        bench_ret: Optional[pd.Series] = None,
        meta: Optional[Dict[str, object]] = None,
    ):
        self._frames = frames
        self.industry = industry if industry is not None else pd.Series(dtype=object)
        self.bench_ret = bench_ret if bench_ret is not None else pd.Series(dtype="float64")
        # 诊断信息（财务/asof 覆盖面等），写进任务日志用
        self.meta: Dict[str, object] = meta or {}

    @property
    def date(self) -> pd.DataFrame:
        """日期面板：每列都是该列的交易日索引。

        估值因子需要「date × code」形状的 DataFrame 参与运算
        （如 ``1 / pe``），而面板的 index 本身是日期，直接广播不方便，
        所以提供一个与其它字段同形状的日期面板。
        """
        first = next(iter(self._frames.values()))
        values = np.repeat(
            first.index.to_numpy().reshape(-1, 1), len(first.columns), axis=1
        )
        return pd.DataFrame(values, index=first.index, columns=first.columns)

    def __getattr__(self, name: str):
        frames = self.__dict__.get("_frames") or {}
        if name in frames:
            return frames[name]
        raise AttributeError(f"Panels 没有字段 {name}；可用：{sorted(frames)}")

    def __contains__(self, name: str) -> bool:
        return name in (self.__dict__.get("_frames") or {})

    # ---------- 基础信息 ----------

    @property
    def dates(self) -> pd.DatetimeIndex:
        first = next(iter(self._frames.values()))
        return pd.DatetimeIndex(first.index)

    @property
    def codes(self) -> pd.Index:
        first = next(iter(self._frames.values()))
        return pd.Index(first.columns)

    @property
    def shape(self) -> tuple:
        first = next(iter(self._frames.values()))
        return first.shape

    @property
    def fields(self) -> list:
        return sorted(self._frames)

    def subset(self, codes: Optional[Sequence[str]] = None,
               start=None, end=None) -> "Panels":
        """按标的/日期切片，返回新的 Panels（视图语义，不复制底层数据）。"""
        frames = {}
        for name, frame in self._frames.items():
            sliced = frame
            if codes is not None:
                keep = [c for c in sliced.columns if c in set(codes)]
                sliced = sliced[keep]
            if start is not None:
                sliced = sliced.loc[sliced.index >= pd.Timestamp(start)]
            if end is not None:
                sliced = sliced.loc[sliced.index <= pd.Timestamp(end)]
            frames[name] = sliced
            industry = self.industry
            if codes is not None:
                industry = industry[industry.index.isin(set(codes))]
            bench = self.bench_ret
            # ⚠️ 空 Series 的 index 是 object dtype，直接和 Timestamp 比较会抛
            # TypeError（"Invalid comparison between dtype=object"）。
            # 没有基准指数（未回填 index_bars）+ 带日期切片时就会踩到，
            # 所以先转成 DatetimeIndex 再比，且只在有数据时切。
            if start is not None or end is not None:
                if len(bench):
                    bench = pd.Series(
                        bench.to_numpy(), index=pd.DatetimeIndex(bench.index)
                    )
                    if start is not None:
                        bench = bench.loc[bench.index >= pd.Timestamp(start)]
                    if end is not None:
                        bench = bench.loc[bench.index <= pd.Timestamp(end)]
            return Panels(frames, industry=industry, bench_ret=bench, meta=self.meta)

    def describe(self) -> dict:
        """概况（写进任务日志，便于排查）。"""
        return {
            "dates": int(len(self.dates)),
            "codes": int(len(self.codes)),
            "fields": self.fields,
            "first_date": self.dates[0].strftime("%Y-%m-%d") if len(self.dates) else None,
            "last_date": self.dates[-1].strftime("%Y-%m-%d") if len(self.dates) else None,
            "industry_covered": int(self.industry.notna().sum()),
            "benchmark": bool(len(self.bench_ret)),
        }

    # ---------- 构造 ----------

    @classmethod
    def from_lake(
        cls,
        lake: Optional[Lake] = None,
        cfg: Optional[Config] = None,
        codes: Optional[Sequence[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        benchmark: Optional[str] = None,
        dtype: str = "float32",
        with_estimates: bool = True,
        with_fundamental: bool = True,
    ) -> "Panels":
        """从数据湖装配面板。

        Args:
            with_estimates: 是否装配估值字段（pe_ttm/pb_mrq/...）。
                技术四维版本可以关掉，省 350ms 和一块内存。
            with_fundamental: 是否装配财务字段（按公告日展开，防前视）。
        """
        lake = lake or get_lake()
        cfg = cfg or get_config()

        if not lake.exists("bars"):
            raise RuntimeError("本地没有行情数据，请先执行 scripts/backfill.py")

        bars = lake.read("bars", columns=_READ_COLUMNS, start=start, end=end)
        if bars.empty:
            raise RuntimeError("行情为空，检查 start/end 区间")
        if codes is not None:
            wanted = set(str(c) for c in codes)
            bars = bars[bars["code"].astype(str).isin(wanted)]
            if bars.empty:
                raise RuntimeError("指定的标的在本地没有行情")

        bars = bars.sort_values(["date", "code"]).reset_index(drop=True)

        # 后复权：不复权价 × 当日累计因子（点-in-time，除权日当天即用新因子）
        events = lake.read("adj_factor") if lake.exists("adj_factor") else None
        factor = expand_factors(bars, events)
        for field in PRICE_FIELDS:
            bars[f"adj_{field}"] = bars[field].to_numpy(dtype="float64") * factor
        bars["raw_close"] = bars["close"]

        frames = _pivot_all(bars, dtype=dtype)
        industry = _load_industry(lake)
        bench_ret = _load_benchmark(lake, benchmark or cfg.get("backtest.benchmark", DEFAULT_BENCHMARK))

        meta: Dict[str, object] = {}
        dates = pd.DatetimeIndex(frames["close"].index)
        codes_index = pd.Index(frames["close"].columns)

        # 估值面板：东财日频横截面，与行情同轴。缺失的交易日用前值填充
        # （估值口径按日更新，但停牌日没有横截面行；不填会让停牌股的估值因子变 NaN）
        if with_estimates:
            valuation_frames = _load_valuation(lake, dates, codes_index, dtype=dtype)
            frames.update(valuation_frames)
            meta["valuation_fields"] = sorted(valuation_frames)

        # 财务面板：按公告日展开（防前视）
        if with_fundamental:
            from .fundamental import build_panel as build_fundamental

            frame = lake.read("fundamental") if lake.exists("fundamental") else None
            built = build_fundamental(frame, dates, codes_index)
            for name, wide in built.frames.items():
                frames[name] = wide.astype(dtype, errors="ignore") if dtype else wide
            meta["fundamental"] = built.diagnostics

        return cls(frames, industry=industry, bench_ret=bench_ret, meta=meta)


def _pivot_all(bars: pd.DataFrame, dtype: str = "float32") -> Dict[str, pd.DataFrame]:
    """长表 → 宽表（一次 set_index，逐列 unstack）。

    ``unstack`` 比 ``pivot_table`` 快得多，但要求 (date, code) 唯一 ——
    数据湖的 upsert 保证了这一点；万一有重复，这里用 ``last`` 兜底而不是抛错，
    因为「多一条重复行」不该让整个打分崩掉。
    """
    indexed = bars.set_index(["date", "code"])
    frames: Dict[str, pd.DataFrame] = {}

    for field in PRICE_FIELDS:
        frames[field] = _unstack(indexed[f"adj_{field}"], dtype)

    for field in PANEL_FIELDS:
        if field in PRICE_FIELDS:
            continue
        if field == "tradestatus":
            wide = _unstack(indexed[field], "float32")
            frames[field] = wide.fillna(0).astype("int8")
        else:
            frames[field] = _unstack(indexed[field], dtype)

    frames["raw_close"] = _unstack(indexed["raw_close"], dtype)
    frames["factor"] = _unstack(indexed["adj_close"] / indexed["raw_close"].replace(0, np.nan), dtype)
    return frames


def _unstack(series: pd.Series, dtype: str = "float32") -> pd.DataFrame:
    wide = series.unstack("code")
    wide.index = pd.DatetimeIndex(wide.index)
    wide = wide.sort_index()
    if dtype:
        wide = wide.astype(dtype, errors="ignore")
    return wide


def _load_valuation(
    lake: Lake,
    dates: pd.DatetimeIndex,
    codes: pd.Index,
    dtype: str = "float32",
) -> Dict[str, pd.DataFrame]:
    """估值宽表面板（index=date, columns=code）。

    ``valuation`` 是「每天一个全市场横截面」，但停牌股当天没有行。
    这里 reindex 到行情日期轴后做 ``ffill``：停牌期间沿用停牌前最后一个估值，
    这是市场惯例（PE 不会因为停牌而消失）。回填上限 20 个交易日 ——
    再长基本就是退市/长期停牌，沿用下去会捏造出一个不存在的估值。
    """
    if not lake.exists("valuation"):
        logger.warning("缺少 valuation 数据集，估值维度将全部缺失")
        return {}

    frame = lake.read(
        "valuation",
        columns=["date", "code", "pe_ttm", "pb_mrq", "ps_ttm", "peg", "total_mv"],
    )
    if frame is None or frame.empty:
        return {}

    frame = frame.copy()
    frame["code"] = frame["code"].astype(str)
    frame = frame[frame["code"].isin(set(str(c) for c in codes))]
    if frame.empty:
        return {}

    out: Dict[str, pd.DataFrame] = {}
    for field in ("pe_ttm", "pb_mrq", "ps_ttm", "peg", "total_mv"):
        if field not in frame.columns:
            continue
        wide = (
            frame.pivot_table(index="date", columns="code", values=field, aggfunc="last")
            .reindex(columns=codes)
        )
        wide.index = pd.DatetimeIndex(wide.index)
        wide = wide.sort_index()
        # 先对齐到行情日期轴再回填，避免「非交易日」占位
        wide = wide.reindex(dates.union(wide.index)).ffill(limit=20).reindex(dates)
        out[field] = wide.astype(dtype, errors="ignore") if dtype else wide
    return out


def _load_industry(lake: Lake) -> pd.Series:
    """行业映射（index=code）。缺失时返回空 Series，标准化会退化为全市场分位。"""
    if not lake.exists("universe"):
        return pd.Series(dtype=object)
    try:
        uni = lake.read("universe", columns=["code", "industry_l1"])
    except Exception as exc:  # pragma: no cover
        logger.warning("读取行业失败，退化为全市场分位：%s", exc)
        return pd.Series(dtype=object)
    if uni.empty or "industry_l1" not in uni.columns:
        return pd.Series(dtype=object)
    return pd.Series(
        uni["industry_l1"].astype(object).to_numpy(),
        index=uni["code"].astype(str).to_numpy(),
        name="industry",
    )


def _load_benchmark(lake: Lake, benchmark: str) -> pd.Series:
    """基准指数日收益率（index=date）。

    ``rs_60`` 需要它。指数缺失时返回空 Series，因子会退化为「绝对收益」，
    此时会打日志提示 —— 不静默改变语义。
    """
    if not lake.exists("index_bars"):
        logger.warning("缺少基准指数行情（index_bars），rs_60 将退化为绝对收益")
        return pd.Series(dtype="float64")
    frame = lake.read("index_bars", columns=["date", "code", "close"])
    if frame.empty:
        return pd.Series(dtype="float64")
    frame = frame[frame["code"].astype(str) == benchmark]
    if frame.empty:
        logger.warning("基准 %s 不在 index_bars 中，rs_60 将退化为绝对收益", benchmark)
        return pd.Series(dtype="float64")
    frame = frame.sort_values("date")
    close = pd.Series(
        pd.to_numeric(frame["close"], errors="coerce").to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(frame["date"]),
    )
    return close.pct_change()


def get_panels(codes: Optional[Sequence[str]] = None, start: Optional[str] = None,
               end: Optional[str] = None, **kwargs) -> Panels:
    """便捷入口。"""
    return Panels.from_lake(codes=codes, start=start, end=end, **kwargs)
