"""交易日历：刷新、查询、前后交易日、平移。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.calendar import Calendar


@pytest.fixture()
def cal(cfg, lake):
    """2024-01-01 ~ 2024-01-12 的日历，含元旦假期（01-01 非交易日）。"""
    days = pd.bdate_range("2024-01-01", "2024-01-12")
    frame = pd.DataFrame(
        {
            "date": days,
            "is_trading_day": [d != pd.Timestamp("2024-01-01") for d in days],
        }
    )
    lake.write("calendar", frame)
    return Calendar(cfg, lake)


class TestEmptyState:
    def test_no_calendar_falls_back_to_weekdays(self, cfg, lake):
        calendar = Calendar(cfg, lake)
        assert calendar.load().empty
        assert calendar.is_trading_day("2024-01-02") is True   # 周二
        assert calendar.is_trading_day("2024-01-06") is False  # 周六

    def test_trading_days_fallback(self, cfg, lake):
        calendar = Calendar(cfg, lake)
        days = calendar.trading_days("2024-01-01", "2024-01-07")
        assert len(days) == 5  # 周一至周五


class TestQueries:
    def test_is_trading_day(self, cal):
        assert cal.is_trading_day("2024-01-01") is False
        assert cal.is_trading_day("2024-01-02") is True

    def test_trading_days_range(self, cal):
        days = cal.trading_days("2024-01-01", "2024-01-05")
        assert [d.strftime("%m-%d") for d in days] == ["01-02", "01-03", "01-04", "01-05"]

    def test_trading_days_inclusive_bounds(self, cal):
        days = cal.trading_days("2024-01-02", "2024-01-03")
        assert len(days) == 2

    def test_all_trading_days(self, cal):
        assert len(cal.all_trading_days()) == 9

    def test_latest_trading_day(self, cal):
        assert cal.latest_trading_day("2024-01-03").strftime("%Y-%m-%d") == "2024-01-03"

    def test_latest_trading_day_before_coverage_is_none(self, cal):
        """日历未覆盖该日期之前 → 返回 None（不猜测）。"""
        assert cal.latest_trading_day("2024-01-01") is None

    def test_next_trading_day(self, cal):
        assert cal.next_trading_day("2023-12-29").strftime("%Y-%m-%d") == "2024-01-02"
        assert cal.next_trading_day("2024-01-02").strftime("%Y-%m-%d") == "2024-01-03"

    def test_prev_trading_day(self, cal):
        assert cal.prev_trading_day("2024-01-03").strftime("%Y-%m-%d") == "2024-01-02"

    def test_prev_trading_day_at_data_start_is_none(self, cal):
        """2024-01-01 是元旦（非交易日），其前无数据 → None。"""
        assert cal.prev_trading_day("2024-01-02") is None

    def test_count_between(self, cal):
        assert cal.count_between("2024-01-01", "2024-01-12") == 9

    def test_shift_forward(self, cal):
        assert cal.shift("2024-01-02", 2).strftime("%Y-%m-%d") == "2024-01-04"

    def test_shift_backward(self, cal):
        assert cal.shift("2024-01-04", -2).strftime("%Y-%m-%d") == "2024-01-02"

    def test_shift_out_of_range(self, cal):
        assert cal.shift("2024-01-02", 100) is None


class TestCoverage:
    def test_coverage_shape(self, cal):
        info = cal.coverage()
        assert info["exists"] is True
        assert info["rows"] == 10
        assert info["trading_days"] == 9
        assert info["first_date"] == "2024-01-01"
        assert info["last_date"] == "2024-01-12"

    def test_coverage_empty(self, cfg, lake):
        assert Calendar(cfg, lake).coverage() == {"exists": False, "rows": 0}


class TestCache:
    def test_cache_invalidated_on_write(self, cal, lake):
        assert len(cal.all_trading_days()) == 9
        lake.write(
            "calendar",
            pd.DataFrame({"date": pd.to_datetime(["2024-02-01"]), "is_trading_day": [True]}),
        )
        # 缓存仍返回旧值（同一实例内不再重读），需显式重建
        assert len(Calendar(cal.cfg, lake).all_trading_days()) == 10
