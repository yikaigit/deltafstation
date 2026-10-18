"""财务数据的「时点展开」：把定期报告变成日频面板，且不偷看未来。

要解决的问题
------------
财报本身是**低频离散事件**：每季度一条，带两个日期
* ``report_date``：报告期末（如 2026-06-30）
* ``notice_date``：公告日（如 2026-08-24）

打分发生在某个交易日 ``t``。那一刻市场**能看到的**只是「``notice_date ≤ t``
的最新一期报告」。如果直接用 ``report_date ≤ t`` 去取，会在 7 月 1 日就用上
8 月 24 日才公布的半年报 —— 这就是典型的前视偏差（look-ahead bias），
回测会好得离谱，实盘完全兑现不了。

所以本模块只暴露一个语义：给定交易日面板的日期轴，返回
「每一天 = 截至该日已公告的最新一期」的面板。翻译成 pandas 就是
「按公告日 reindex + ffill」，无需逐日循环。

三个必须小心的细节
------------------
1. **同一公告日多期报告**：季报/半年报修正、年报重述都可能导致一次公告里
   出现多期数据，甚至同一期被公告两次（先预告后修正）。因此排序键是
   ``(notice_date, report_date)``，且同键保留**最后写入**的那条（upsert 语义）。
2. **公告日缺失**：数据源偶尔不给 ``notice_date``。这里用
   ``report_date + 默认滞后`` 兜底（法定披露截止：一季报 4/30、半年报 8/31、
   三季报 10/31、年报次年 4/30），并记进诊断信息 —— 绝不能拿
   ``report_date`` 直接当时点，那等于前视。
3. **回填长度上限**：某只票两年没发新报告，不应该让两年前的 ROE 一直
   ffill 到今天还当「最新」。超过 ``max_stale_days`` 的格子置为 NaN，
   让因子层把它当缺失（而不是一个陈旧值）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 法定披露截止日（月, 日）→ 用于 ``notice_date`` 缺失时的保守兜底
LEGAL_DEADLINE = {
    (3, 31): (4, 30),    # 一季报
    (6, 30): (8, 31),    # 半年报
    (9, 30): (10, 31),   # 三季报
    (12, 31): (4, 30),   # 年报（次年）
}

#: 兜底滞后天数（找不到具体规则时用 120 天：覆盖年报 4 个月的披露窗口）
DEFAULT_LAG_DAYS = 120

#: 财报最多可以「沿用」这么多自然日。超过就当缺失 ——
#: 1200 天 ≈ 3.3 年，正常公司每年至少 4 期报告，超过说明中间断档了。
DEFAULT_MAX_STALE_DAYS = 1200


def _fallback_notice_date(report_date: pd.Timestamp) -> pd.Timestamp:
    """报告期缺失公告日时的保守兜底：按法定披露截止日。

    ⚠️ 只有年报会跨年（12-31 → 次年 4-30），其余三期都在同一年内披露。
    早先一段实现给所有期都 ``year + 1``，半年报被推到了次年 8 月 ——
    比真实披露晚整整一年，质量因子的数据会「凭空消失」一年（被测试抓到）。
    """
    month_day = (report_date.month, report_date.day)
    target = LEGAL_DEADLINE.get(month_day)
    if target is None:
        # 非标准报告期（改期、并购并表等），退化成 +120 天
        return report_date + pd.Timedelta(days=DEFAULT_LAG_DAYS)

    month, day = target
    year = report_date.year + 1 if month_day == (12, 31) else report_date.year
    return pd.Timestamp(year=year, month=month, day=day)


@dataclass
class FundamentalAsOf:
    """财务面板容器：已公告数据 + 诊断信息。"""

    frames: Dict[str, pd.DataFrame] = field(default_factory=dict)
    diagnostics: Dict[str, object] = field(default_factory=dict)

    def __getitem__(self, name: str) -> pd.DataFrame:
        return self.frames[name]

    def __contains__(self, name: str) -> bool:
        return name in self.frames

    def keys(self):
        return self.frames.keys()


def _ttm_sum(group: pd.DataFrame, column: str) -> pd.Series:
    """滚动 4 期求和（TTM）。

    ⚠️ 不能无脑 ``rolling(4).sum()``：报告是**累计口径**（一季报是 1-3 月累计，
    年报是全年累计），直接用累计值做 TTM 会算出「4 个不同区间的累计值相加」
    这种没有意义的东西。这里的做法是按「累计值差分还原单季」再滚动 4 季：

    * 一季报（3-31）：单季 = 累计
    * 半年报（6-30）：单季 = 累计 - 一季报累计
    * 三季报（9-30）：单季 = 累计 - 半年报累计
    * 年报（12-31）：单季 = 累计 - 三季报累计

    缺失上一期时（新上市、休刊）单季为空，TTM 也随之空 —— 宁可缺，不要错。
    """
    if column not in group.columns:
        return pd.Series(dtype="float64")

    series = pd.to_numeric(group[column], errors="coerce")
    # 按报告期排序后，用上一条累计值还原单季
    cumulative = series.to_numpy(dtype="float64")
    dates = pd.DatetimeIndex(group["report_date"])
    quarter = dates.month  # 3 / 6 / 9 / 12

    single = np.full(len(cumulative), np.nan)
    for i in range(len(cumulative)):
        if not np.isfinite(cumulative[i]):
            continue
        if quarter[i] == 3:
            single[i] = cumulative[i]
        else:
            prev_quarter = {6: 3, 9: 6, 12: 9}.get(int(quarter[i]))
            previous = None
            for j in range(i - 1, -1, -1):
                if not np.isfinite(cumulative[j]):
                    continue
                if int(quarter[j]) == int(quarter[i]):
                    # 同季度上一年 → 不是「上一期」，停止回溯（中间缺期）
                    break
                if int(quarter[j]) == prev_quarter:
                    previous = cumulative[j]
                    break
            if previous is not None:
                single[i] = cumulative[i] - previous

    single_series = pd.Series(single, index=pd.DatetimeIndex(dates))
    # 单季序列转 TTM：最近 4 个单季之和
    return single_series.rolling(4, min_periods=4).sum()


def build_panel(
    frame: pd.DataFrame,
    dates: pd.DatetimeIndex,
    codes: pd.Index,
    fields: Optional[List[str]] = None,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
) -> FundamentalAsOf:
    """把财务长表展开成日频面板（index=date, columns=code）。

    Args:
        frame: 财务长表（含 code / report_date / notice_date / 各指标）
        dates: 目标日期轴（来自行情面板，保证与价格完全对齐）
        codes: 目标标的（来自行情面板）
        fields: 需要展开的字段（None = 自动推断）
        max_stale_days: 财报最多沿用多少自然日

    Returns:
        ``FundamentalAsOf``：``.frames[name]`` 是日频面板；
        ``.diagnostics`` 记录公告日兜底条数、断档造成的置空比例等。
    """
    dates = pd.DatetimeIndex(dates)
    codes = pd.Index([str(c) for c in codes])
    empty = {name: pd.DataFrame(np.nan, index=dates, columns=codes) for name in (fields or [])}
    if frame is None or frame.empty:
        logger.warning("财务数据为空，估值/质量维度将全部缺失")
        return FundamentalAsOf(frames=empty, diagnostics={"available": False})

    data = frame.copy()
    data["code"] = data["code"].astype(str)
    data = data[data["code"].isin(set(codes))].copy()
    if data.empty:
        return FundamentalAsOf(frames=empty, diagnostics={"available": False})

    # ---- 1. 公告日缺失的兜底 ----
    report = pd.to_datetime(data["report_date"], errors="coerce")
    notice = pd.to_datetime(data["notice_date"], errors="coerce")
    missing_notice = notice.isna()
    if missing_notice.any():
        notice = notice.copy()
        notice[missing_notice] = report[missing_notice].map(
            lambda ts: _fallback_notice_date(ts) if pd.notna(ts) else pd.NaT
        )
    # 公告日早于报告期是数据错误（不可能是「财报发出来才知道期末」），
    # 这类记录按兜底处理而不是直接相信
    invalid = notice.notna() & report.notna() & (notice < report)
    if invalid.any():
        notice.loc[invalid] = report.loc[invalid].map(_fallback_notice_date)

    data["report_date"] = report
    data["notice_date"] = notice
    data = data[data["report_date"].notna() & data["notice_date"].notna()]

    # ---- 2. 排序：同一公告日多期 → 以报告期较大者为准（更新的一期更完整）----
    data = data.sort_values(["code", "notice_date", "report_date"])
    data = data.drop_duplicates(subset=["code", "notice_date", "report_date"], keep="last")

    if fields is None:
        fields = [c for c in data.columns
                  if c not in ("code", "report_date", "notice_date", "notice_estimated")]

    # ---- 3. 派生 TTM 指标（按标的逐只算，因为要还原单季）----
    derived: Dict[str, pd.DataFrame] = {}
    for column in ("rev", "net_profit"):
        if column in data.columns:
            parts = []
            for code, group in data.groupby("code", sort=False):
                group = group.sort_values(["notice_date", "report_date"])
                ttm = _ttm_sum(group, column)
                parts.append(pd.DataFrame({
                    "code": code,
                    "notice_date": group["notice_date"].to_numpy(),
                    "report_date": group["report_date"].to_numpy(),
                    f"{column}_ttm": ttm.to_numpy(dtype="float64"),
                }))
            if parts:
                derived[f"{column}_ttm"] = pd.concat(parts, ignore_index=True)

    for ttm_frame in derived.values():
        data = data.merge(
            ttm_frame, on=["code", "notice_date", "report_date"], how="left"
        )

    all_fields = list(fields)
    for name in derived:
        if name not in all_fields:
            all_fields.append(name)
    # ---- 4. 展开：以 notice_date 为时点，reindex 到行情日期轴后 ffill ----
    #
    # 这里的 ``wide`` 索引是「每次公告的日期」，值是该次公告披露的那一期数据。
    # reindex 到行情日期轴 + ffill 就得到「当天能看到的最新已公告值」。
    union_index = dates.union(pd.DatetimeIndex(data["notice_date"].unique())).sort_values()
    frames: Dict[str, pd.DataFrame] = {}

    for name in all_fields:
        if name not in data.columns:
            continue
        wide = (
            data.pivot_table(index="notice_date", columns="code", values=name, aggfunc="last")
            .reindex(union_index)
            .ffill()
            .reindex(dates)
            .reindex(columns=codes)
        )
        frames[name] = wide

    # ---- 5. 数据年龄：公告太久远的不该继续当真 ----
    #
    # 每只票各自维护「公告日序列 → 数据」，对每个交易日求「距最近一次公告多少天」。
    # 超过 max_stale_days 的格子置 NaN（缺失），而不是让三年前的 ROE 一直挂着。
    stale_cutoff = pd.Timedelta(days=int(max_stale_days))
    stale_counts: Dict[str, int] = {}

    for code in codes:
        if code not in data["code"].values:
            continue
        notices = np.sort(data.loc[data["code"] == code, "notice_date"].unique())
        if len(notices) == 0:
            continue
        notices = pd.DatetimeIndex(notices)
        # 每个交易日对应的「最近公告日」
        positions = notices.searchsorted(dates.to_numpy(), side="right") - 1
        has = positions >= 0
        asof = np.full(len(dates), np.datetime64("NaT"), dtype="datetime64[ns]")
        asof[has] = notices.to_numpy()[positions[has]]
        # 日期差按「天」取整数：直接相减得到 timedelta64[ns]，
        # 除以 np.timedelta64(1, "D") 在 numpy 2.x 会触发 generic-unit 弃用警告
        age_days = (dates.to_numpy() - asof).astype("timedelta64[D]").astype("int64")
        age_days = np.where(has, age_days, 10**6)
        stale = ~has | (age_days > stale_cutoff.days)
        if stale.any():
            stale_counts[str(code)] = int(stale.sum())
            for wide in frames.values():
                if code not in wide.columns:
                    continue
                values = wide[code].to_numpy(dtype="float64")
                values[stale] = np.nan
                wide[code] = values

    diagnostics = {
        "available": True,
        "codes": int(data["code"].nunique()),
        "rows": int(len(data)),
        "notice_fallback": int(missing_notice.sum()),
        "notice_invalid_fixed": int(invalid.sum()),
        "stale_codes": len(stale_counts),
        "stale_cells": int(sum(stale_counts.values())),
        "max_stale_days": int(max_stale_days),
        "fields": sorted(frames),
        "last_report": (
            str(pd.Timestamp(data["report_date"].max()).date()) if len(data) else None
        ),
    }
    return FundamentalAsOf(frames=frames, diagnostics=diagnostics)
