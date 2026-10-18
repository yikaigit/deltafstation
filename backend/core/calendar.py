"""交易日历。

用途：
* 判断某天是否交易日（每日任务第 1 步，非交易日直接退出）；
* 生成区间内的交易日序列（打分与回测的横截面日期来源）；
* 求「上一交易日 / 下一交易日」（增量更新与 T+1 成交都要用）。

数据来自 baostock ``query_trade_dates``，落 ``lake/calendar.parquet``。
本地无日历时退化为「周一至周五」，但会打日志提示 —— 长假会误判，必须刷新。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import List, Optional, Sequence, Union

import pandas as pd

from .config import Config, get_config
from .lake import Lake, get_lake
from . import providers

logger = logging.getLogger(__name__)

#: 默认日历起点（与 universe.start_date 一致）
DEFAULT_START = "2008-01-01"

DateLike = Union[str, date, pd.Timestamp]


class Calendar:
    """交易日历访问对象。"""

    def __init__(self, cfg: Optional[Config] = None, lake: Optional[Lake] = None):
        self.cfg = cfg or get_config()
        self.lake = lake or get_lake()
        self._cache: Optional[pd.DataFrame] = None

    # ---------- 刷新 ----------

    def refresh(self, start: str = DEFAULT_START, end: Optional[str] = None) -> int:
        """从 baostock 拉取并落盘（幂等 upsert），返回总行数。"""
        end = end or (pd.Timestamp.today() + pd.Timedelta(days=30)).strftime("%Y-%m-%d")
        frame = providers.get_baostock().trade_dates(start, end)
        if frame.empty:
            logger.warning("交易日历拉取为空（%s ~ %s）", start, end)
            return 0
        total = self.lake.write("calendar", frame)
        self._cache = None
        logger.info("交易日历已更新：%s ~ %s，共 %d 行", start, end, len(frame))
        return total

    def ensure(self, start: str = DEFAULT_START, end: Optional[str] = None) -> int:
        """本地缺失或覆盖不足时才联网刷新。"""
        if not self.lake.exists("calendar"):
            return self.refresh(start, end)
        frame = self.load()
        if frame.empty:
            return self.refresh(start, end)
        last = pd.Timestamp(frame["date"].max())
        target = pd.Timestamp(end or pd.Timestamp.today())
        if last < target:
            return self.refresh(str(frame["date"].min().date()), str((target + pd.Timedelta(days=30)).date()))
        return len(frame)

    # ---------- 读取 ----------

    def load(self) -> pd.DataFrame:
        if self._cache is None:
            frame = self.lake.read("calendar")
            if not frame.empty:
                frame = frame.sort_values("date").reset_index(drop=True)
            self._cache = frame
        return self._cache

    def _fallback_days(self, start: DateLike, end: DateLike) -> pd.DatetimeIndex:
        logger.warning("本地无交易日历，退化为工作日口径（长假会误判），请执行 calendar.refresh()")
        return pd.bdate_range(pd.Timestamp(start), pd.Timestamp(end))

    # ---------- 查询 ----------

    def trading_days(self, start: DateLike, end: DateLike) -> pd.DatetimeIndex:
        """区间内的交易日（含首尾）。"""
        frame = self.load()
        if frame.empty:
            return self._fallback_days(start, end)
        mask = (frame["date"] >= pd.Timestamp(start)) & (frame["date"] <= pd.Timestamp(end))
        days = frame.loc[mask & frame["is_trading_day"], "date"]
        return pd.DatetimeIndex(days.sort_values())

    def all_trading_days(self) -> pd.DatetimeIndex:
        frame = self.load()
        if frame.empty:
            return pd.DatetimeIndex([])
        return pd.DatetimeIndex(frame.loc[frame["is_trading_day"], "date"].sort_values())

    def is_trading_day(self, day: DateLike) -> bool:
        ts = pd.Timestamp(day).normalize()
        frame = self.load()
        if frame.empty:
            return ts.weekday() < 5
        hit = frame[frame["date"] == ts]
        if hit.empty:
            # 日历未覆盖的日期：按工作日近似
            return ts.weekday() < 5
        return bool(hit["is_trading_day"].iloc[0])

    def latest_trading_day(self, on_or_before: Optional[DateLike] = None) -> Optional[pd.Timestamp]:
        """不晚于给定日期的最近交易日。"""
        target = pd.Timestamp(on_or_before or pd.Timestamp.today()).normalize()
        days = self.all_trading_days()
        if len(days) == 0:
            cursor = target
            while cursor.weekday() >= 5:
                cursor -= pd.Timedelta(days=1)
            return cursor
        pos = days.searchsorted(target, side="right") - 1
        return days[pos] if pos >= 0 else None

    def next_trading_day(self, after: DateLike) -> Optional[pd.Timestamp]:
        """严格晚于给定日期的第一个交易日。"""
        target = pd.Timestamp(after).normalize()
        days = self.all_trading_days()
        if len(days) == 0:
            cursor = target + pd.Timedelta(days=1)
            while cursor.weekday() >= 5:
                cursor += pd.Timedelta(days=1)
            return cursor
        pos = days.searchsorted(target, side="right")
        return days[pos] if pos < len(days) else None

    def prev_trading_day(self, before: DateLike) -> Optional[pd.Timestamp]:
        """严格早于给定日期的最后一个交易日。"""
        target = pd.Timestamp(before).normalize()
        days = self.all_trading_days()
        if len(days) == 0:
            cursor = target - pd.Timedelta(days=1)
            while cursor.weekday() >= 5:
                cursor -= pd.Timedelta(days=1)
            return cursor
        pos = days.searchsorted(target, side="left") - 1
        return days[pos] if pos >= 0 else None

    def count_between(self, start: DateLike, end: DateLike) -> int:
        """区间内交易日数量（用于「N 个交易日后调仓」）。"""
        return len(self.trading_days(start, end))

    def shift(self, day: DateLike, n: int) -> Optional[pd.Timestamp]:
        """在交易日序列上平移 n 个交易日（n 可为负）。"""
        days = self.all_trading_days()
        if len(days) == 0:
            return None
        target = pd.Timestamp(day).normalize()
        pos = days.searchsorted(target, side="left")
        new_pos = pos + n
        if 0 <= new_pos < len(days):
            return days[new_pos]
        return None

    # ---------- 报告 ----------

    def coverage(self) -> dict:
        frame = self.load()
        if frame.empty:
            return {"exists": False, "rows": 0}
        trading = frame[frame["is_trading_day"]]
        return {
            "exists": True,
            "rows": int(len(frame)),
            "trading_days": int(len(trading)),
            "first_date": frame["date"].min().strftime("%Y-%m-%d"),
            "last_date": frame["date"].max().strftime("%Y-%m-%d"),
        }


def get_calendar() -> Calendar:
    return Calendar(get_config(), get_lake())
