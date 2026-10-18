"""绩效指标测试（规格 §6.4 / §6.7）。

口径全部手算核对：Sharpe/Sortino 用简单收益、Sortino 只对负收益求标准差、
月度胜率按自然月分组、IS/OOS 必须乘法自洽。这些公式错了不会报错，
只会给出一份「看起来很正常」的报告，所以每个都单独钉死。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.backtest.metrics import (
    TRADING_DAYS,
    check_self_consistency,
    compute_metrics,
    downsample_equity,
    drawdown_series,
    max_drawdown,
    nav_series,
    split_period,
)

DAYS = pd.bdate_range("2024-01-02", periods=20)


def make_returns(values, index=None) -> pd.Series:
    values = np.asarray(values, dtype="float64")
    if index is None:
        index = pd.bdate_range("2024-01-02", periods=len(values))
    return pd.Series(values, index=index)


# ==================== 净值序列 ====================


class TestNavSeries:
    def test_empty(self):
        assert nav_series(pd.Series(dtype="float64")).empty
        assert nav_series(None).empty

    def test_compounds_from_one(self):
        nav = nav_series(make_returns([0.1, 0.1]))
        assert nav.iloc[0] == pytest.approx(1.1)
        assert nav.iloc[1] == pytest.approx(1.21)          # 复利不是 1.2
        assert nav.iloc[0] == 1.0 + 0.1                    # 首日收益不计入起点

    def test_nan_treated_as_flat(self):
        nav = nav_series(make_returns([0.1, np.nan, 0.1]))
        assert nav.iloc[1] == pytest.approx(1.1)
        assert nav.iloc[2] == pytest.approx(1.21)


# ==================== 回撤 ====================


class TestDrawdown:
    def test_max_drawdown_peak_to_trough(self):
        nav = pd.Series([1.0, 1.2, 0.9, 1.1, 0.8])
        # 峰 1.2 → 谷 0.8 = -33.33%
        assert max_drawdown(nav) == pytest.approx(0.8 / 1.2 - 1.0)

    def test_max_drawdown_monotonic_up_is_zero(self):
        assert max_drawdown(pd.Series([1.0, 1.1, 1.2, 1.3])) == pytest.approx(0.0)

    def test_max_drawdown_empty(self):
        assert max_drawdown(pd.Series(dtype="float64")) == 0.0

    def test_drawdown_series_tracks_running_peak(self):
        nav = pd.Series([1.0, 1.2, 0.9, 1.3])
        dd = drawdown_series(nav)
        assert dd.iloc[0] == pytest.approx(0.0)
        assert dd.iloc[1] == pytest.approx(0.0)            # 新高
        assert dd.iloc[2] == pytest.approx(0.9 / 1.2 - 1.0)
        assert dd.iloc[3] == pytest.approx(0.0)            # 再创新高

    def test_drawdown_never_positive(self):
        rng = np.random.default_rng(3)
        nav = nav_series(pd.Series(rng.normal(0, 0.02, 300)))
        assert (drawdown_series(nav) <= 1e-12).all()


# ==================== 全套指标 ====================


class TestComputeMetrics:
    def test_empty_returns(self):
        out = compute_metrics(pd.Series(dtype="float64"), label="full")
        assert out == {"label": "full", "days": 0}

    def test_total_return_matches_manual_compounding(self):
        r = make_returns([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.03, 0.02, 0.01, -0.01])
        out = compute_metrics(r)
        expected = (float((1.0 + r).prod() - 1.0)) * 100
        assert out["total_return"] == pytest.approx(expected, abs=0.01)
        assert out["days"] == 10
        assert out["label"] == ""

    def test_annualisation_uses_250_days(self):
        r = make_returns([0.001] * 250)
        out = compute_metrics(r)
        cum = 1.001 ** 250 - 1.0
        assert out["total_return"] == pytest.approx(cum * 100, abs=0.01)
        # 恰好一年 → 年化 = 累计
        assert out["annual_return"] == pytest.approx(cum * 100, abs=0.01)

    def test_sharpe_uses_simple_returns(self):
        r = make_returns([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.03, 0.02, 0.01, -0.01])
        out = compute_metrics(r)
        expected = float(r.mean() / r.std(ddof=1) * np.sqrt(TRADING_DAYS))
        assert out["sharpe"] == pytest.approx(expected, abs=0.001)

    def test_sortino_only_downside(self):
        r = make_returns([0.01, -0.02, 0.03, -0.01, 0.02, 0.01, -0.03, 0.02, 0.01, -0.01])
        out = compute_metrics(r)
        downside = r[r < 0]
        expected = float(r.mean() / downside.std(ddof=1) * np.sqrt(TRADING_DAYS))
        assert out["sortino"] == pytest.approx(expected, abs=0.001)
        # 负收益比全样本少 → 下行标准差更小 → Sortino 的绝对值更大
        assert abs(out["sortino"]) > abs(out["sharpe"])

    def test_sortino_none_when_never_lost(self):
        """没有负收益 → None 而不是 inf：「没亏过」≠「零风险」。"""
        out = compute_metrics(make_returns([0.01] * 10))
        assert out["sortino"] is None
        # 常数值序列的 std 不是精确 0 而是 1e-18 的浮点粉尘，
        # 不用 VOL_EPS 挡开会算出 8.6e16 的 Sharpe
        assert out["sharpe"] is None
        assert out["annual_vol"] == 0.0

    def test_zero_vol_flat_series_has_no_sharpe(self):
        out = compute_metrics(make_returns([0.0] * 30))
        assert out["sharpe"] is None
        assert out["sortino"] is None
        assert out["calmar"] is None

    def test_calmar_none_without_drawdown(self):
        out = compute_metrics(make_returns([0.01] * 10))
        assert out["max_drawdown"] == pytest.approx(0.0, abs=0.01)
        assert out["calmar"] is None

    def test_calmar_annual_over_mdd(self):
        r = make_returns([0.05, -0.04, 0.05, -0.04] * 40)
        out = compute_metrics(r)
        nav = nav_series(r)
        mdd = max_drawdown(nav)
        years = len(r) / TRADING_DAYS
        annual = (1.0 + float((1.0 + r).prod() - 1.0)) ** (1.0 / years) - 1.0
        assert out["calmar"] == pytest.approx(annual / abs(mdd), abs=0.01)

    def test_month_win_rate_by_calendar_month(self):
        index = pd.bdate_range("2024-01-29", periods=10)   # 跨 1/2 月
        r = pd.Series([0.01] * 4 + [-0.05] * 6, index=index)
        out = compute_metrics(r)
        # 1 月（含 1/29~1/31）涨 → 胜；2 月全跌 → 负
        assert out["month_win_rate"] == pytest.approx(50.0, abs=0.1)

    def test_bench_excess_and_ir(self):
        r = make_returns([0.02, 0.01, -0.01, 0.03, 0.02])
        b = make_returns([0.01, 0.01, 0.00, 0.01, 0.01])
        out = compute_metrics(r, bench_returns=b)
        strat_cum = float((1.0 + r).prod() - 1.0)
        bench_cum = float((1.0 + b).prod() - 1.0)
        assert out["bench_return"] == pytest.approx(bench_cum * 100, abs=0.01)
        assert out["excess_return"] == pytest.approx((strat_cum - bench_cum) * 100, abs=0.01)
        excess = r - b
        ir = float(excess.mean() / excess.std(ddof=1) * np.sqrt(TRADING_DAYS))
        assert out["information_ratio"] == pytest.approx(ir, abs=0.001)

    def test_no_bench_no_bench_keys(self):
        out = compute_metrics(make_returns([0.01, 0.02]))
        for key in ("bench_return", "excess_return", "information_ratio"):
            assert key not in out

    def test_extras_passthrough(self):
        out = compute_metrics(make_returns([0.01, 0.02]), extras={
            "turnover": 4.0, "trades": 12, "blocked_trades": 3,
            "avg_positions": 7.5, "ignored": "x",
        })
        assert out["turnover"] == 4.0
        assert out["trades"] == 12
        assert out["blocked_trades"] == 3
        assert out["avg_positions"] == 7.5
        assert "ignored" not in out

    def test_non_finite_becomes_none(self):
        """inf/NaN 不能写进 JSON（前端会拿到 undefined）。"""
        out = compute_metrics(make_returns([0.0, 0.0]), label="oos")
        assert out["sharpe"] is None
        assert out["annual_vol"] == 0.0


# ==================== IS / OOS 切分 ====================


class TestSplitPeriod:
    def test_ratio_cut(self):
        period = split_period(DAYS, is_ratio=0.7)
        cut = int(20 * 0.7)
        assert period["is_start"] == DAYS[0].strftime("%Y-%m-%d")
        assert period["is_end"] == DAYS[cut - 1].strftime("%Y-%m-%d")
        assert period["oos_start"] == DAYS[cut].strftime("%Y-%m-%d")
        assert period["oos_end"] == DAYS[-1].strftime("%Y-%m-%d")

    def test_seams_are_adjacent_and_cover_all(self):
        period = split_period(DAYS, is_ratio=0.6)
        assert pd.Timestamp(period["is_end"]) < pd.Timestamp(period["oos_start"])
        assert (pd.Timestamp(period["oos_start"]) - pd.Timestamp(period["is_end"])).days <= 3
        assert pd.Timestamp(period["is_start"]) == DAYS[0]
        assert pd.Timestamp(period["oos_end"]) == DAYS[-1]

    def test_ratio_clamped(self):
        assert split_period(DAYS, is_ratio=0.0)["is_start"] == DAYS[0].strftime("%Y-%m-%d")
        low = split_period(DAYS, is_ratio=0.0)
        high = split_period(DAYS, is_ratio=1.0)
        assert pd.Timestamp(low["is_end"]) < pd.Timestamp(high["is_end"])

    def test_too_short(self):
        period = split_period(pd.bdate_range("2024-01-02", periods=1))
        assert period == {"is_start": None, "is_end": None,
                          "oos_start": None, "oos_end": None}


class TestSelfConsistency:
    def _triple(self, values, ratio=0.7):
        r = make_returns(values)
        cut = int(len(r) * ratio)
        return (compute_metrics(r.iloc[:cut], label="is"),
                compute_metrics(r.iloc[cut:], label="oos"),
                compute_metrics(r, label="full"))

    def test_real_split_is_self_consistent(self):
        rng = np.random.default_rng(11)
        values = rng.normal(0.0005, 0.015, 120)
        is_m, oos_m, full_m = self._triple(values)
        assert check_self_consistency(is_m, oos_m, full_m) is None

    def test_big_returns_still_consistent(self):
        """收益翻几倍时，两位小数的舍入就能差出 0.07pp——容差必须随量级放大。"""
        rng = np.random.default_rng(5)
        values = rng.normal(0.02, 0.06, 250)
        is_m, oos_m, full_m = self._triple(values)
        assert check_self_consistency(is_m, oos_m, full_m) is None
        # 但给一个死板的固定小容差就会误报 —— 这正是自动容差存在的理由
        assert check_self_consistency(is_m, oos_m, full_m, tolerance_pp=0.01)
        assert check_self_consistency(is_m, oos_m, full_m, tolerance_pp=0.01) is not None

    def test_mismatch_is_reported(self):
        rng = np.random.default_rng(7)
        values = rng.normal(0.001, 0.02, 120)
        is_m, oos_m, full_m = self._triple(values)
        tampered = dict(full_m, total_return=full_m["total_return"] + 10.0)
        problem = check_self_consistency(is_m, oos_m, tampered)
        assert problem is not None and "自洽校验失败" in problem

    def test_missing_metrics_reported(self):
        assert check_self_consistency({}, {}, {}) is not None


# ==================== 前端曲线降采样 ====================


class TestDownsampleEquity:
    @staticmethod
    def _equity(n):
        index = pd.bdate_range("2020-01-02", periods=n)
        return pd.DataFrame({
            "nav": np.linspace(1.0, 2.0, n),
            "bench": np.linspace(1.0, 1.5, n),
            "drawdown": np.linspace(0.0, -0.2, n),
        }, index=index)

    def test_empty(self):
        assert downsample_equity(pd.DataFrame()) == []
        assert downsample_equity(None) == []

    def test_small_frame_passes_through(self):
        records = downsample_equity(self._equity(10))
        assert len(records) == 10
        assert records[0]["date"] == "2020-01-02"
        assert records[0]["nav"] == pytest.approx(1.0, abs=1e-6)
        assert set(records[0]) == {"date", "nav", "bench", "drawdown"}

    def test_long_frame_is_capped_and_keeps_endpoints(self):
        records = downsample_equity(self._equity(3000), max_points=500)
        assert 0 < len(records) <= 500
        assert records[0]["date"] == self._equity(3000).index[0].strftime("%Y-%m-%d")
        last = self._equity(3000).index[-1].strftime("%Y-%m-%d")
        assert records[-1]["date"] == last
        assert records[0]["nav"] == pytest.approx(1.0, abs=1e-6)
        assert records[-1]["nav"] == pytest.approx(2.0, abs=1e-6)

    def test_nan_bench_becomes_none(self):
        frame = self._equity(5)
        frame.loc[frame.index[2], "bench"] = np.nan
        records = downsample_equity(frame)
        assert records[2]["bench"] is None
        assert records[0]["bench"] is not None

    def test_dates_are_strings(self):
        for record in downsample_equity(self._equity(6)):
            assert isinstance(record["date"], str)
