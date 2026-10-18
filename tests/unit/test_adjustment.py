"""复权：点-in-time 语义、面板展开、前/后复权口径。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.adjustment import (
    MODE_BACK,
    MODE_FORWARD,
    MODE_NONE,
    apply_adjust,
    check_factor_integrity,
    expand_factors,
    factor_at,
    factor_from_events,
    looks_truncated,
)


@pytest.fixture()
def events():
    """3 次除权：2020-06-01 因子 1.5，2021-06-01 因子 2.0，2022-06-01 因子 3.0。"""
    return pd.DataFrame(
        {
            "code": ["sh.600000"] * 3,
            "ex_date": pd.to_datetime(["2020-06-01", "2021-06-01", "2022-06-01"]),
            "fore_factor": [0.7, 0.55, 0.4],
            "back_factor": [1.5, 2.0, 3.0],
        }
    )


@pytest.fixture()
def bars():
    return pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-05-29", "2020-06-01", "2020-06-02", "2021-06-01", "2022-06-01"]
            ),
            "code": ["sh.600000"] * 5,
            "open": [10.0, 10.0, 10.0, 10.0, 10.0],
            "high": [10.5, 10.5, 10.5, 10.5, 10.5],
            "low": [9.5, 9.5, 9.5, 9.5, 9.5],
            "close": [10.0, 10.0, 10.0, 10.0, 10.0],
            "volume": [1000] * 5,
        }
    )


class TestFactorFromEvents:
    def test_no_events_returns_ones(self):
        dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
        assert (factor_from_events(None, dates) == 1.0).all()
        assert (factor_from_events(pd.DataFrame(), dates) == 1.0).all()

    def test_before_first_event_is_one(self, events):
        values = factor_from_events(events, pd.to_datetime(["2019-01-02"]))
        assert values[0] == pytest.approx(1.0)

    def test_day_before_ex_date_uses_old_factor(self, events):
        values = factor_from_events(events, pd.to_datetime(["2020-05-29"]))
        assert values[0] == pytest.approx(1.0)

    def test_ex_date_itself_uses_new_factor(self, events):
        """除权日**当天**即启用新因子（不是次日）—— 已用浦发实盘数据验证。"""
        values = factor_from_events(events, pd.to_datetime(["2020-06-01"]))
        assert values[0] == pytest.approx(1.5)

    def test_between_events(self, events):
        values = factor_from_events(events, pd.to_datetime(["2020-12-31"]))
        assert values[0] == pytest.approx(1.5)

    def test_after_last_event(self, events):
        values = factor_from_events(events, pd.to_datetime(["2023-01-01"]))
        assert values[0] == pytest.approx(3.0)

    def test_point_in_time_excludes_future_events(self, events):
        """窗口末日之前的事件才参与：只看到 2020 年的数据时，不能用 2021/2022 的因子。"""
        values = factor_from_events(events, pd.to_datetime(["2020-06-02"]))
        assert values[0] == pytest.approx(1.5)

    def test_empty_dates(self, events):
        assert len(factor_from_events(events, [])) == 0

    def test_unsorted_input(self, events):
        shuffled = events.iloc[[2, 0, 1]]
        values = factor_from_events(shuffled, pd.to_datetime(["2021-06-01"]))
        assert values[0] == pytest.approx(2.0)

    def test_factor_at(self, events):
        assert factor_at(events, "2021-06-01") == pytest.approx(2.0)
        assert factor_at(None, "2021-06-01") == pytest.approx(1.0)


class TestExpandFactors:
    def test_matches_single_series(self, events, bars):
        panel = pd.concat([bars, bars.assign(code="sz.000001")], ignore_index=True)
        both = pd.concat([events, events.assign(code="sz.000001")], ignore_index=True)
        expanded = expand_factors(panel, both)
        expected = np.concatenate(
            [factor_from_events(events, bars["date"]), factor_from_events(events, bars["date"])]
        )
        assert np.allclose(expanded, expected)

    def test_missing_code_defaults_to_one(self, events, bars):
        other = bars.assign(code="sz.000001")
        values = expand_factors(other, events)
        assert (values == 1.0).all()

    def test_no_events(self, bars):
        assert (expand_factors(bars, None) == 1.0).all()
        assert (expand_factors(bars, pd.DataFrame()) == 1.0).all()

    def test_row_order_preserved(self, events, bars):
        shuffled = bars.sample(frac=1.0, random_state=7).reset_index(drop=True)
        values = expand_factors(shuffled, events)
        direct = factor_from_events(events, shuffled["date"])
        assert np.allclose(values, direct)

    def test_empty_bars(self, events):
        empty = pd.DataFrame({"date": [], "code": []})
        assert len(expand_factors(empty, events)) == 0


class TestApplyAdjust:
    def test_back_mode_multiplies(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_BACK)
        assert out["adj_close"].iloc[0] == pytest.approx(10.0)   # 1.0
        assert out["adj_close"].iloc[1] == pytest.approx(15.0)   # 1.5
        assert out["adj_close"].iloc[3] == pytest.approx(20.0)   # 2.0
        assert out["adj_close"].iloc[4] == pytest.approx(30.0)   # 3.0

    def test_back_mode_covers_all_price_columns(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_BACK)
        for column in ("open", "high", "low", "close"):
            assert "adj_" + column in out.columns

    def test_none_mode_is_passthrough(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_NONE)
        assert "adj_close" not in out.columns
        assert out["close"].tolist() == bars["close"].tolist()

    def test_forward_last_day_equals_raw(self, events, bars):
        """前复权以最后一天为基准 → 末日复权价应等于不复权价。"""
        out = apply_adjust(bars, events, mode=MODE_FORWARD)
        assert out["adj_close"].iloc[-1] == pytest.approx(out["close"].iloc[-1])

    def test_forward_with_base_date(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_FORWARD, base_date="2021-06-01")
        # 基准日因子 2.0 → 该日复权价 == 不复权价
        row = out[out["date"] == pd.Timestamp("2021-06-01")]
        assert row["adj_close"].iloc[0] == pytest.approx(10.0)

    def test_does_not_mutate_input(self, events, bars):
        before = bars.copy()
        apply_adjust(bars, events, mode=MODE_BACK)
        pd.testing.assert_frame_equal(bars, before)

    def test_invalid_mode_raises(self, events, bars):
        with pytest.raises(ValueError, match="不支持的复权口径"):
            apply_adjust(bars, events, mode="weird")

    def test_returns_factor_column(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_BACK)
        assert "adj_factor" in out.columns
        assert out["adj_factor"].iloc[4] == pytest.approx(3.0)

    def test_custom_prefix(self, events, bars):
        out = apply_adjust(bars, events, mode=MODE_BACK, prefix="hfq_")
        assert "hfq_close" in out.columns

    def test_multi_code_forward(self, events, bars):
        other = bars.assign(code="sz.000001", close=[20.0] * 5)
        panel = pd.concat([bars, other], ignore_index=True)
        out = apply_adjust(panel, events, mode=MODE_FORWARD)
        for code in ("sh.600000", "sz.000001"):
            last = out[out["code"] == code].iloc[-1]
            assert last["adj_close"] == pytest.approx(last["close"])


class TestIntegrity:
    def test_clean_events(self, events):
        result = check_factor_integrity(events)
        assert result["symbols"] == 1
        assert result["non_monotonic"] == 0
        assert result["below_one"] == 0

    def test_detects_non_monotonic(self):
        frame = pd.DataFrame(
            {
                "code": ["sh.600000"] * 3,
                "ex_date": pd.to_datetime(["2020-01-01", "2021-01-01", "2022-01-01"]),
                "back_factor": [2.0, 1.5, 3.0],
            }
        )
        result = check_factor_integrity(frame)
        assert result["non_monotonic"] == 1

    def test_detects_below_one(self):
        frame = pd.DataFrame(
            {
                "code": ["sh.600000"],
                "ex_date": pd.to_datetime(["2020-01-01"]),
                "back_factor": [0.9],
            }
        )
        assert check_factor_integrity(frame)["below_one"] == 1

    def test_empty(self):
        assert check_factor_integrity(pd.DataFrame())["symbols"] == 0


class TestLooksTruncated:
    def test_truncated(self):
        """首个事件晚于数据首日且因子 > 1.5 → 疑似被截断。"""
        events = pd.DataFrame(
            {
                "ex_date": pd.to_datetime(["2022-01-01"]),
                "back_factor": [8.0],
            }
        )
        assert looks_truncated(events, "2020-01-01") is True

    def test_not_truncated_when_event_precedes_data(self):
        events = pd.DataFrame({"ex_date": pd.to_datetime(["1999-01-01"]), "back_factor": [1.0]})
        assert looks_truncated(events, "2020-01-01") is False

    def test_small_head_factor_is_fine(self):
        events = pd.DataFrame({"ex_date": pd.to_datetime(["2022-01-01"]), "back_factor": [1.2]})
        assert looks_truncated(events, "2020-01-01") is False

    def test_none(self):
        assert looks_truncated(None, "2020-01-01") is False
