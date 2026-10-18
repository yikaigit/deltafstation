"""P5 测试：财务 asof 对齐（防前视）、估值/质量因子、因子评价。

最重要的是 ``TestNoLookAhead``：它用「手工构造的两期财报」证明
「公告日之前拿不到新数据、公告日当天才能拿到」。
这是 P5 唯一一个「错了会让回测好得离谱而没人发现」的地方，所以测在最前面。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.fundamental import build_panel, _fallback_notice_date


def _trading_days(start: str, end: str) -> pd.DatetimeIndex:
    """工作日序列（测试里不需要真的交易日历）。"""
    return pd.bdate_range(start, end)


class TestNoLookAhead:
    """财务必须按公告日生效，不能按报告期。"""

    def _frame(self):
        # 一份半年报：报告期末 2026-06-30，但 8 月 24 日才公告
        return pd.DataFrame(
            {
                "code": ["sh.600000", "sh.600000"],
                "report_date": pd.to_datetime(["2026-03-31", "2026-06-30"]),
                "notice_date": pd.to_datetime(["2026-04-25", "2026-08-24"]),
                "roe_wa": [3.0, 8.0],
                "eps": [0.30, 0.80],
            }
        )

    def test_report_date_does_not_leak_before_notice(self):
        dates = _trading_days("2026-04-20", "2026-09-10")
        built = build_panel(self._frame(), dates, pd.Index(["sh.600000"]))
        roe = built["roe_wa"]["sh.600000"]

        # 4/25 之前：一期都还没公告 → 没有任何值
        assert roe.loc[:"2026-04-24"].dropna().empty
        # 4/25 起能看到一季报（3.0），但绝不是半年报的 8.0
        assert roe.loc["2026-04-27"] == pytest.approx(3.0)
        assert roe.loc["2026-08-21"] == pytest.approx(3.0)
        # 8/24 公告当天才切到 8.0
        assert roe.loc["2026-08-24"] == pytest.approx(8.0)
        assert roe.loc["2026-09-10"] == pytest.approx(8.0)

    def test_no_value_before_first_notice(self):
        """第一份报告公告之前必须全 NaN，而不是 0 或首个已知值。"""
        dates = _trading_days("2026-01-01", "2026-04-24")
        built = build_panel(self._frame(), dates, pd.Index(["sh.600000"]))
        assert built["roe_wa"]["sh.600000"].isna().all()

    def test_duplicate_notice_keeps_latest_report(self):
        """同一公告日披露多期（更正/重述）→ 取报告期更晚的那一期。"""
        frame = pd.DataFrame(
            {
                "code": ["sh.600000", "sh.600000"],
                "report_date": pd.to_datetime(["2026-03-31", "2026-06-30"]),
                "notice_date": pd.to_datetime(["2026-08-24", "2026-08-24"]),
                "roe_wa": [3.0, 8.0],
            }
        )
        dates = _trading_days("2026-08-24", "2026-09-01")
        built = build_panel(frame, dates, pd.Index(["sh.600000"]))
        assert built["roe_wa"]["sh.600000"].iloc[0] == pytest.approx(8.0)


class TestNoticeDateFallback:
    """公告日缺失/非法时的兜底必须保守（只会更晚，绝不会更早）。"""

    @pytest.mark.parametrize(
        "report,expected",
        [
            ("2026-03-31", "2026-04-30"),
            ("2026-06-30", "2026-08-31"),
            ("2026-09-30", "2026-10-31"),
            ("2026-12-31", "2027-04-30"),
        ],
    )
    def test_legal_deadline(self, report, expected):
        assert _fallback_notice_date(pd.Timestamp(report)) == pd.Timestamp(expected)

    def test_non_standard_period_uses_safe_lag(self):
        got = _fallback_notice_date(pd.Timestamp("2026-05-31"))
        assert got == pd.Timestamp("2026-09-28")  # +120 天

    def test_missing_notice_filled_and_counted(self):
        frame = pd.DataFrame(
            {
                "code": ["sh.600000"],
                "report_date": pd.to_datetime(["2026-06-30"]),
                "notice_date": pd.to_datetime([None]),
                "roe_wa": [8.0],
            }
        )
        dates = _trading_days("2026-08-25", "2026-09-05")
        built = build_panel(frame, dates, pd.Index(["sh.600000"]))
        assert built.diagnostics["notice_fallback"] == 1
        roe = built["roe_wa"]["sh.600000"]
        # 兜底到 8/31（半年报法定披露截止日），所以 8/25~8/28 仍拿不到
        assert roe.loc[:"2026-08-28"].isna().all()
        # 8/31 当天才生效
        assert roe.loc["2026-08-31"] == pytest.approx(8.0)


class TestTtm:
    """TTM 必须由「单季还原后的 4 季之和」得到，不能直接累加 4 个累计值。"""

    def _frame(self):
        # 用累计口径模拟：Q1 100、H1 250、Q3 420、FY 600
        return pd.DataFrame(
            {
                "code": ["sh.600000"] * 4,
                "report_date": pd.to_datetime(
                    ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"]
                ),
                "notice_date": pd.to_datetime(
                    ["2025-04-28", "2025-08-28", "2025-10-28", "2026-04-20"]
                ),
                "rev": [100.0, 250.0, 420.0, 600.0],
            }
        )

    def test_ttm_after_annual_report_equals_full_year(self):
        dates = _trading_days("2026-04-21", "2026-05-05")
        built = build_panel(self._frame(), dates, pd.Index(["sh.600000"]))
        # 年报公告后，TTM = 全年 600
        assert built["rev_ttm"]["sh.600000"].iloc[-1] == pytest.approx(600.0)

    def test_ttm_not_sum_of_cumulative(self):
        """反例断言：如果实现把 4 个累计值直接相加会得到 1370，这里必须不是。"""
        dates = _trading_days("2026-04-21", "2026-05-05")
        built = build_panel(self._frame(), dates, pd.Index(["sh.600000"]))
        assert built["rev_ttm"]["sh.600000"].iloc[-1] != pytest.approx(1370.0)


class TestValuationFactors:
    """估值因子的负值与缺失语义。"""

    def _panel(self, **frames):
        from backend.core.panels import Panels

        index = pd.bdate_range("2026-01-01", periods=3)
        base = pd.DataFrame(10.0, index=index, columns=["A", "B", "C"])
        data = {"close": base, "high": base, "low": base, "open": base,
                "volume": base, "raw_close": base, "turn": base,
                "pct_chg": base, "tradestatus": base.astype("int8"),
                "amount": base, "factor": base}
        data.update(frames)
        return Panels(data)

    def test_negative_pe_stays_negative(self):
        """亏损股（PE<0）取倒数后仍是负数 → 排最后，而不是变 NaN 或 0。"""
        from backend.core import factors as F

        pe = pd.DataFrame(
            {"A": [10.0, 10.0, 10.0], "B": [-20.0, -20.0, -20.0], "C": [np.nan] * 3},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        panel = self._panel(pe_ttm=pe)
        got = F.compute("ep", panel)
        assert got["A"].iloc[0] == pytest.approx(0.1)
        assert got["B"].iloc[0] == pytest.approx(-0.05)
        assert got["C"].isna().all()

    def test_zero_pe_is_missing_not_infinity(self):
        from backend.core import factors as F

        pe = pd.DataFrame(
            {"A": [0.0, 0.0, 0.0], "B": [10.0, 10.0, 10.0]},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        got = F.compute("ep", self._panel(pe_ttm=pe))
        assert got["A"].isna().all()
        assert np.isfinite(got["B"]).all()

    def test_peg_non_positive_is_missing(self):
        from backend.core import factors as F

        peg = pd.DataFrame(
            {"A": [0.8, 0.8, 0.8], "B": [-3.2, -3.2, -3.2], "C": [0.0, 0.0, 0.0]},
            index=pd.bdate_range("2026-01-01", periods=3),
        )
        got = F.compute("peg_safe", self._panel(peg=peg))
        assert got["A"].iloc[0] == pytest.approx(0.8)
        assert got["B"].isna().all()
        assert got["C"].isna().all()


class TestQualityFactors:
    def _panel(self, **frames):
        from backend.core.panels import Panels

        index = pd.bdate_range("2026-01-01", periods=3)
        base = pd.DataFrame(1.0, index=index, columns=["A", "B"])
        data = {"close": base, "high": base, "low": base, "open": base,
                "volume": base, "raw_close": base, "turn": base,
                "pct_chg": base, "tradestatus": base.astype("int8"),
                "amount": base, "factor": base}
        data.update(frames)
        return Panels(data)

    def test_ocf_to_profit_guards_non_positive_eps(self):
        from backend.core import factors as F

        index = pd.bdate_range("2026-01-01", periods=3)
        ocf = pd.DataFrame({"A": [1.0, 1.0, 1.0], "B": [1.0, 1.0, 1.0]}, index=index)
        eps = pd.DataFrame({"A": [0.5, 0.5, 0.5], "B": [-0.5, -0.5, -0.5]}, index=index)
        got = F.compute("ocf_to_profit", self._panel(ocf_ps=ocf, eps=eps))
        assert got["A"].iloc[0] == pytest.approx(2.0)
        # 亏损公司：负负得正会变成「盈利质量 2.0」，必须置缺失
        assert got["B"].isna().all()


class TestEvaluation:
    """评价体系的三个关键数学性质。"""

    def _data(self, direction: int = 1):
        """构造一个「因子与未来收益完全同序」的面板，IC 应接近 1。"""
        dates = pd.bdate_range("2026-01-01", periods=60)
        codes = [f"S{i:02d}" for i in range(30)]
        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.02, size=(len(dates), len(codes)))
        close = pd.DataFrame(100 * np.cumprod(1 + returns, axis=0), index=dates, columns=codes)
        forward = close.shift(-10) / close - 1
        # 因子 = 未来收益 + 噪声（方向由 direction 决定）
        factor = forward * direction + rng.normal(0, 0.002, size=forward.shape)
        return close, pd.DataFrame(factor, index=dates, columns=codes), forward

    def test_forward_return_uses_next_day(self):
        from backend.core.evaluation import forward_returns

        dates = pd.bdate_range("2026-01-01", periods=5)
        close = pd.DataFrame({"A": [10.0, 11.0, 12.0, 13.0, 14.0]}, index=dates)
        fwd = forward_returns(close, days=1)
        # 第 0 天的 1 日未来收益 = 11/10 - 1 = 10%
        assert fwd["A"].iloc[0] == pytest.approx(0.1)
        # 最后一天没有未来
        assert pd.isna(fwd["A"].iloc[-1])

    def test_ic_is_positive_when_factor_predicts(self):
        from backend.core.evaluation import evaluate_factor

        close, factor, forward = self._data(direction=1)
        got = evaluate_factor("demo", factor, forward, close, direction=1)
        assert got.ic_mean is not None and got.ic_mean > 0.3
        assert got.ic_tstat is not None and got.ic_tstat > 2.0

    def test_direction_is_flipped_before_ic(self):
        """direction=-1 的因子（原始值越大越差）翻正后 IC 也应为正。"""
        from backend.core.evaluation import evaluate_factor

        close, factor, forward = self._data(direction=-1)
        got = evaluate_factor("demo_neg", factor, forward, close, direction=-1)
        assert got.ic_mean is not None and got.ic_mean > 0.3

    def test_random_factor_has_near_zero_ic(self):
        """纯噪声因子不应通过门禁。"""
        from backend.core.evaluation import evaluate_factor

        close, _, forward = self._data(direction=1)
        rng = np.random.default_rng(7)
        noise = pd.DataFrame(
            rng.normal(size=forward.shape), index=forward.index, columns=forward.columns
        )
        got = evaluate_factor("noise", noise, forward, close, direction=1)
        assert abs(got.ic_mean) < 0.05
        assert got.passed is False

    def test_quantile_returns_are_ordered(self):
        from backend.core.evaluation import evaluate_factor

        close, factor, forward = self._data(direction=1)
        got = evaluate_factor("demo", factor, forward, close, direction=1)
        assert len(got.quantile_returns) == 5
        # Q5（因子最大）应显著优于 Q1
        assert got.quantile_returns[-1] > got.quantile_returns[0]
        assert got.monotonicity is not None and got.monotonicity > 0.8

    def test_correlation_matrix_is_symmetric(self):
        from backend.core.evaluation import correlation_matrix

        close, factor, forward = self._data(direction=1)
        frames = {"a": factor, "b": factor * 1.01, "c": -factor}
        corr = correlation_matrix(frames, forward)
        assert corr.shape == (3, 3)
        np.testing.assert_allclose(corr.to_numpy(), corr.to_numpy().T, atol=1e-9)
        # a 与 b 几乎完全同向
        assert corr.loc["a", "b"] > 0.9
        # a 与 c 反向
        assert corr.loc["a", "c"] < -0.8


class TestVersionConfig:
    def test_v2_has_six_dimensions(self):
        from backend.core.scorer import Scorer

        spec = Scorer.load_version("v2.0.0")
        assert set(spec["weights"]) == {
            "trend", "momentum", "volume", "risk", "valuation", "quality"
        }
        assert sum(spec["weights"].values()) == pytest.approx(1.0)
        assert len(spec["factors"]) == 30

    def test_v1_still_loadable(self):
        """旧版本必须还能加载（否则历史快照无法复现）。"""
        from backend.core.scorer import Scorer

        spec = Scorer.load_version("v1.1.0")
        assert len(spec["weights"]) == 4
        assert "valuation" not in spec["weights"]

    def test_factors_in_config_are_registered(self):
        from backend.core import factors as F
        from backend.core.scorer import Scorer

        for version in ("v1.0.0", "v1.1.0", "v2.0.0"):
            spec = Scorer.load_version(version)
            missing = [n for n in spec["factors"] if n not in F.FACTORS]
            assert not missing, f"{version} 引用未实现因子 {missing}"
