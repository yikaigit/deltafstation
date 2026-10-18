"""校验器规则测试。

重点锁住三条**曾经大规模误报**的规则 —— 它们都在真实 HS300 全量回填中
把 300 只标的全部标成 WARN，导致 degraded 判定失效（噪声淹没信号）：

* R1.6 只要 Volume 是 float 就告警 → 300/300 误报；
* R2.7 用 10% 硬比低价股涨停（2.29 元涨到 2.52 元 = 10.0437%）→ 140/300 误报；
* R2.8 用自然日间隔判漏拉，春节国庆长假必然超 5 天 → 298/300 误报。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.validator import (
    LEVEL_ERROR,
    LEVEL_WARN,
    clean_suspensions,
    normalize_columns,
    suspension_mask,
    validate_dataframe,
)


def _frame(rows, volume=None):
    """构造一个最小可用的行情表（不复权）。"""
    n = len(rows)
    dates = pd.to_datetime([r[0] for r in rows])
    closes = [float(r[1]) for r in rows]
    pre = [float(r[2]) if len(r) > 2 else (closes[0] if i == 0 else closes[i - 1])
           for i, r in enumerate(rows)]
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "preclose": pre,
            "volume": volume if volume is not None else [1_000_000] * n,
        }
    )


def _rules(report, level=None):
    return [i for i in report.issues if level is None or i.level == level]


class TestColumnNormalization:
    def test_renames_lake_columns(self):
        df = pd.DataFrame({"date": [], "open": [], "high": [], "low": [], "close": [], "volume": []})
        out = normalize_columns(df)
        assert set(["Date", "Open", "High", "Low", "Close", "Volume"]).issubset(out.columns)

    def test_keeps_canonical_untouched(self):
        df = pd.DataFrame({"Date": [], "Open": [], "High": [], "Low": [], "Close": [], "Volume": []})
        assert list(normalize_columns(df).columns) == list(df.columns)


class TestR16VolumePrecision:
    def test_float_volume_without_fraction_is_not_flagged(self):
        """float 类型本身不代表丢精度 —— 2^53 内整数完全精确。"""
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.2)]
        df = _frame(rows, volume=[22066700.0, 18203654.0])
        report = validate_dataframe(df, symbol="sh.600000")
        assert not [i for i in report.issues if i.rule == "R1.6"]

    def test_float_volume_with_fraction_is_flagged(self):
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.2)]
        df = _frame(rows, volume=[22066700.5, 18203654.0])
        report = validate_dataframe(df, symbol="sh.600000")
        hits = [i for i in report.issues if i.rule == "R1.6"]
        assert hits and hits[0].level == LEVEL_WARN
        assert hits[0].count == 1

    def test_integer_volume_never_flagged(self):
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.2)]
        df = _frame(rows, volume=np.array([22066700, 18203654], dtype="int64"))
        report = validate_dataframe(df, symbol="sh.600000")
        assert not [i for i in report.issues if i.rule == "R1.6"]


class TestR27ReturnLimit:
    def test_low_price_limit_up_is_not_flagged(self):
        """包钢股份 2.29 → 2.52 是涨停（10.0437%），不该报异常。"""
        rows = [("2025-07-23", 2.29), ("2025-07-24", 2.52)]
        report = validate_dataframe(_frame(rows), symbol="sh.600010")
        assert not [i for i in report.issues if i.rule == "R2.7"]

    def test_normal_price_limit_up_is_not_flagged(self):
        """15.47 → 13.92 是跌停（-10.0194%），同样不该报。"""
        rows = [("2025-04-03", 15.47), ("2025-04-07", 13.92)]
        report = validate_dataframe(_frame(rows), symbol="sh.600104")
        assert not [i for i in report.issues if i.rule == "R2.7"]

    def test_real_anomaly_still_flagged(self):
        """超出容差的真异常必须抓住。"""
        rows = [("2025-04-03", 10.0), ("2025-04-07", 13.0)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        hits = [i for i in report.issues if i.rule == "R2.7"]
        assert hits and hits[0].level == LEVEL_WARN

    def test_hard_limit_is_error(self):
        rows = [("2025-04-03", 10.0), ("2025-04-07", 20.0)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        errs = [i for i in report.issues if i.rule == "R2.7" and i.level == LEVEL_ERROR]
        assert errs

    def test_star_board_uses_20pct(self):
        """科创板 20% 阈值：+15% 不算异常。"""
        rows = [("2025-04-03", 100.0), ("2025-04-07", 115.0)]
        report = validate_dataframe(_frame(rows), symbol="sh.688111")
        assert not [i for i in report.issues if i.rule == "R2.7"]


class TestR28DateGap:
    def _calendar(self, start="2024-02-01", end="2024-02-29"):
        return pd.bdate_range(start, end)

    def test_spring_festival_gap_is_not_flagged_with_calendar(self):
        """有日历时，长假不是漏拉。"""
        # 模拟春节休市：2/8 之后直接跳到 2/19
        cal = pd.DatetimeIndex(
            list(pd.bdate_range("2024-01-02", "2024-02-08"))
            + list(pd.bdate_range("2024-02-19", "2024-02-29"))
        )
        rows = [("2024-02-07", 10.0), ("2024-02-08", 10.1), ("2024-02-19", 10.2)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000", trading_days=cal)
        assert not [i for i in report.issues if i.rule == "R2.8"]

    def test_missing_trading_days_is_flagged_with_calendar(self):
        """真漏拉（中间少了好几个交易日）必须报。"""
        cal = pd.bdate_range("2024-01-02", "2024-01-31")
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.1), ("2024-01-15", 10.2)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000", trading_days=cal)
        hits = [i for i in report.issues if i.rule == "R2.8"]
        assert hits and hits[0].level == LEVEL_WARN
        assert "遗漏交易日" in hits[0].message

    def test_without_calendar_long_holiday_is_not_flagged(self):
        """无日历时阈值放到 12 天，春节也不误报。"""
        rows = [("2024-02-08", 10.0), ("2024-02-19", 10.2)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        assert not [i for i in report.issues if i.rule == "R2.8"]

    def test_without_calendar_real_gap_is_flagged(self):
        rows = [("2024-01-02", 10.0), ("2024-02-15", 10.2)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        assert [i for i in report.issues if i.rule == "R2.8"]


class TestSuspension:
    def test_mask_detects_flat_zero_volume(self):
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "Open": [6.6, 6.6], "High": [6.7, 6.6], "Low": [6.5, 6.6],
                "Close": [6.65, 6.6], "Volume": [1000, 0],
            }
        )
        assert suspension_mask(df).tolist() == [False, True]

    def test_clean_drops_suspension_rows(self):
        df = pd.DataFrame(
            {
                "Date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "Open": [6.6, 6.6], "High": [6.7, 6.6], "Low": [6.5, 6.6],
                "Close": [6.65, 6.6], "Volume": [1000, 0],
            }
        )
        assert len(clean_suspensions(df)) == 1


class TestBasicRules:
    def test_missing_required_column(self):
        df = _frame([("2024-01-02", 10.0)]).drop(columns=["volume"])
        report = validate_dataframe(df, symbol="sh.600000")
        assert [i for i in report.issues if i.rule == "R1.1" and i.level == LEVEL_ERROR]

    def test_extra_columns_are_tolerated(self):
        """数据湖的 bars 带 preclose/amount/turn/tradestatus，不该判结构错误。"""
        df = _frame([("2024-01-02", 10.0)])
        df["tradestatus"] = 1
        df["is_st"] = False
        report = validate_dataframe(df, symbol="sh.600000")
        assert not [i for i in report.issues if i.rule == "R1.1"]

    def test_duplicate_dates(self):
        df = _frame([("2024-01-02", 10.0), ("2024-01-02", 10.1)])
        report = validate_dataframe(df, symbol="sh.600000")
        assert [i for i in report.issues if i.rule == "R1.4"]

    def test_high_below_close(self):
        df = _frame([("2024-01-02", 10.0)])
        df.loc[0, "high"] = 9.0
        report = validate_dataframe(df, symbol="sh.600000")
        assert [i for i in report.issues if i.rule == "R2.1"]

    def test_non_positive_price(self):
        df = _frame([("2024-01-02", 0.0)])
        df.loc[0, "open"] = 0.0
        report = validate_dataframe(df, symbol="sh.600000")
        assert [i for i in report.issues if i.rule == "R2.3"]

    def test_empty_frame(self):
        report = validate_dataframe(pd.DataFrame(), symbol="sh.600000")
        assert not report.ok
        assert report.issues[0].rule == "R0.0"

    def test_ok_when_clean(self):
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.1)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        assert report.ok
        assert report.rows == 2
        assert report.first_date == "2024-01-02"


class TestAdjustmentBasis:
    def test_uses_back_adjusted_price_when_events_given(self):
        """除权日不复权价暴跌 50%，用后复权价算就不该报异常。"""
        rows = [("2024-06-03", 20.0), ("2024-06-04", 10.0)]
        events = pd.DataFrame(
            {
                "code": ["sh.600000", "sh.600000"],
                "ex_date": pd.to_datetime(["1999-11-10", "2024-06-04"]),
                "fore_factor": [1.0, 2.0],
                "back_factor": [1.0, 2.0],
            }
        )
        df = _frame(rows)
        df.loc[1, "preclose"] = 20.0
        report = validate_dataframe(df, symbol="sh.600000", events=events)
        # 后复权：20*1=20 → 10*2=20，涨幅 0
        assert not [i for i in report.issues if i.rule == "R2.7"]

    def test_skips_note_without_events(self):
        rows = [("2024-01-02", 10.0), ("2024-01-03", 10.1)]
        report = validate_dataframe(_frame(rows), symbol="sh.600000")
        assert any("R2.7" in s for s in report.skipped)
