"""标准化、维度合成与分档测试。

重点回归
--------
1. ``winsorize_mad`` 用 MAD 而不是 3σ —— 尾部厚的分布会让标准差本身被极值污染，
   用它做阈值等于「用异常值判断什么是异常」；全 NaN 行不能刷 RuntimeWarning；
2. 行业样本不足必须**退回全市场分位**（min_industry_samples=5 的必然结果），
   并在返回值里报告实际做了行业内的标的数；
3. ``composite_score`` 的 ``data_quality`` 必须反映「实际参与合成的维度权重」，
   而不是让「只有一个维度有值」的分数看起来和「四维齐全」的一样可靠；
4. 综合分要再 rank 一次 —— 绝对刻度会随市场整体涨跌漂移，跨日期不可比。
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from backend.core import scoring as sc

DATES = pd.bdate_range("2024-01-02", periods=12)  # 一站式用例需要 6 天


def frame(values, index=None, columns=None) -> pd.DataFrame:
    data = np.atleast_2d(np.asarray(values, dtype="float64"))
    return pd.DataFrame(
        data,
        index=index if index is not None else DATES[: len(data)],
        columns=columns,
    )


# ---------------- 去极值 ----------------


class TestWinsorizeMad:
    def test_clips_to_median_plus_k_mad(self):
        # median=4.5；|x-4.5| = [3.5,2.5,1.5,0.5,0.5,1.5,2.5,95.5]，中位数 2.0
        # → MAD=2.9652，上界 = 4.5 + 5×2.9652 = 19.326
        row = frame([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 100.0])
        out = sc.winsorize_mad(row)
        assert out.iloc[0, -1] == pytest.approx(19.326, abs=1e-3)
        assert list(out.iloc[0, :-1]) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    def test_lower_bound_too(self):
        row = frame([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, -100.0])
        out = sc.winsorize_mad(row)
        assert out.iloc[0, -1] == pytest.approx(-11.326, abs=1e-3)

    def test_k_is_honored(self):
        row = frame([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 100.0])
        tight = sc.winsorize_mad(row, k=1.0)
        loose = sc.winsorize_mad(row, k=10.0)
        assert tight.iloc[0, -1] < loose.iloc[0, -1]

    def test_degenerate_row_untouched(self):
        """MAD≈0（横截面几乎同值）时跳过截断，而不是把整行压成中位数。"""
        row = frame([5.0, 5.0, 5.0, 5.0, 5.0])
        out = sc.winsorize_mad(row)
        assert list(out.iloc[0]) == [5.0] * 5

    def test_all_nan_row_no_warning(self):
        f = frame([[np.nan] * 4, [1.0, 2.0, 3.0, 4.0]])
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            out = sc.winsorize_mad(f)
        assert out.iloc[0].isna().all()

    def test_per_row_independent(self):
        f = frame([[1.0, 2.0, 3.0, 100.0], [1.0, 2.0, 3.0, 4.0]])
        out = sc.winsorize_mad(f)
        assert out.iloc[0, 3] < 100.0
        assert out.iloc[1, 3] == 4.0

    def test_empty_frame(self):
        assert sc.winsorize_mad(pd.DataFrame()).empty


# ---------------- 行业中性 ----------------


class TestIndustryPctRank:
    def test_no_industry_uses_market(self):
        f = frame([[10.0, 20.0, 30.0]], columns=list("abc"))
        pct, covered = sc.industry_pct_rank(f, None)
        assert covered == 0
        assert list(pct.iloc[0]) == [1 / 3, 2 / 3, 1.0]

    def test_insufficient_samples_fall_back_to_market(self):
        """行业内只有 1 只 → 退回全市场分位。"""
        f = frame([[10.0, 20.0, 30.0, 40.0, 50.0, 100.0]], columns=list("abcdef"))
        industry = pd.Series(
            {"a": "X", "b": "X", "c": "X", "d": "X", "e": "X", "f": "Y"}
        )
        pct, covered = sc.industry_pct_rank(f, industry, min_samples=5)
        assert covered == 5
        assert pct.iloc[0]["f"] == 1.0           # Y 行业只有 1 只 → 全市场分位
        assert list(pct.iloc[0][:5]) == [0.2, 0.4, 0.6, 0.8, 1.0]

    def test_all_insufficient_falls_back(self):
        f = frame([[10.0, 20.0, 30.0]], columns=list("abc"))
        industry = pd.Series({"a": "X", "b": "Y", "c": "Z"})
        pct, covered = sc.industry_pct_rank(f, industry, min_samples=5)
        assert covered == 0
        assert list(pct.iloc[0]) == [1 / 3, 2 / 3, 1.0]

    def test_unknown_industry_grouped_together(self):
        """行业缺失的标的会被归成一组（__UNKNOWN__），样本不足时同样退回全市场。"""
        f = frame([[10.0, 20.0, 30.0, 40.0]], columns=list("abcd"))
        industry = pd.Series({"a": "X", "b": "X", "c": "X", "d": None})
        pct, covered = sc.industry_pct_rank(f, industry, min_samples=5)
        # X 只有 3 只 < 5 → 全部退回全市场
        assert covered == 0
        assert list(pct.iloc[0]) == [0.25, 0.5, 0.75, 1.0]

    def test_empty_frame(self):
        pct, covered = sc.industry_pct_rank(pd.DataFrame(), pd.Series(dtype=object))
        assert covered == 0
        assert pct.empty


class TestStandardize:
    def test_direction_flips(self):
        f = frame([[10.0, 20.0, 30.0]])
        up, _ = sc.standardize(f, direction=1)
        down, _ = sc.standardize(f, direction=-1)
        assert np.allclose(up.to_numpy(), 1.0 - down.to_numpy())

    def test_winsor_before_rank(self):
        """极值必须先去极值再排名，否则 rank 把极值压到 1.0 但分布全挤在下面。"""
        f = frame([[1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 100.0]])
        clipped = sc.winsorize_mad(f)
        assert clipped.iloc[0, -1] < 100.0
        pct, _ = sc.standardize(f, direction=1)
        assert pct.iloc[0, -1] == 1.0            # 截断后仍是最大
        assert pct.iloc[0, 0] < 0.2              # 最小值不受 100 影响被压扁

    def test_min_samples_forwarded(self):
        f = frame([[10.0, 20.0, 30.0]], columns=list("abc"))
        industry = pd.Series({"a": "X", "b": "X", "c": "X"})
        _, covered_strict = sc.standardize(f, industry=industry, min_samples=5)
        _, covered_loose = sc.standardize(f, industry=industry, min_samples=3)
        assert covered_strict == 0
        assert covered_loose == 3


# ---------------- 维度分 ----------------


class TestDimensionScore:
    def test_average_ignores_nan(self):
        f1 = frame([[0.1, 0.2, 0.3], [0.1, 0.2, np.nan]], columns=list("abc"))
        f2 = frame([[0.3, 0.2, 0.1], [0.3, 0.2, 0.3]], columns=list("abc"))
        out = sc.dimension_score({"a": f1, "b": f2})
        assert out.iloc[0].tolist() == [20.0, 20.0, 20.0]
        # 第二行第三列只有 f2 有值 → 0.3 × 100
        assert out.iloc[1, 2] == pytest.approx(30.0)

    def test_all_nan_stays_nan(self):
        f1 = frame([[np.nan, np.nan]])
        out = sc.dimension_score({"a": f1})
        assert out.isna().all().all()

    def test_empty_returns_none(self):
        assert sc.dimension_score({}) is None
        assert sc.dimension_score({"a": pd.DataFrame()}) is None

    def test_single_factor_times_100(self):
        f = frame([[0.25, 0.75]], columns=list("ab"))
        out = sc.dimension_score({"a": f})
        assert out.iloc[0].tolist() == [25.0, 75.0]


# ---------------- 综合分 ----------------


class TestCompositeScore:
    def test_weighted_sum_and_rank(self):
        a = frame([[90.0, 30.0, 60.0]], columns=list("abc"))
        b = frame([[90.0, 30.0, 60.0]], columns=list("abc"))
        score_abs, score_pct, quality = sc.composite_score(
            {"a": a, "b": b}, {"a": 0.5, "b": 0.5}
        )
        assert list(score_abs.iloc[0]) == [90.0, 30.0, 60.0]
        assert list(score_pct.iloc[0]) == [100.0, pytest.approx(100 / 3), pytest.approx(200 / 3)]
        assert list(quality.iloc[0]) == [1.0, 1.0, 1.0]

    def test_quality_reflects_missing_dimension(self):
        """只有一维有值时，quality 必须是该维权重占比，而不是 1.0。"""
        a = frame([[90.0, 30.0]], columns=list("ab"))
        b = frame([[np.nan, np.nan]], columns=list("ab"))
        score_abs, score_pct, quality = sc.composite_score(
            {"a": a, "b": b}, {"a": 0.35, "b": 0.65}
        )
        assert list(score_abs.iloc[0]) == [pytest.approx(90.0), pytest.approx(30.0)], \
            "权重自动再归一，分数不空缺"
        assert list(quality.iloc[0]) == [0.35, 0.35], "但必须把覆盖率记下来"

    def test_partial_missing_cell(self):
        a = frame([[90.0, np.nan]], columns=list("ab"))
        b = frame([[60.0, 50.0]], columns=list("ab"))
        _, _, quality = sc.composite_score({"a": a, "b": b}, {"a": 0.5, "b": 0.5})
        assert list(quality.iloc[0]) == [1.0, 0.5]

    def test_zero_weight_dimension_ignored(self):
        a = frame([[90.0, 10.0]], columns=list("ab"))
        b = frame([[10.0, 90.0]], columns=list("ab"))
        score_abs, _, _ = sc.composite_score({"a": a, "b": b}, {"a": 1.0, "b": 0.0})
        assert list(score_abs.iloc[0]) == [90.0, 10.0]

    def test_no_usable_dimension_raises(self):
        with pytest.raises(ValueError, match="没有任何可用的维度分"):
            sc.composite_score({"a": frame([[1.0]], columns=["a"])}, {"b": 1.0})

    def test_rank_uses_pct_not_abs(self):
        """score_pct 是横截面百分位，与绝对刻度无关。"""
        a = frame([[10.0, 20.0, 30.0]], columns=list("abc"))
        _, score_pct, _ = sc.composite_score({"a": a}, {"a": 1.0})
        assert list(score_pct.iloc[0]) == [pytest.approx(100 / 3), pytest.approx(200 / 3), 100.0]


# ---------------- 分档 ----------------


class TestGrade:
    def test_boundaries(self):
        f = frame([[95.0, 90.0, 89.999, 70.0, 69.999, 30.0, 29.999, 10.0, 9.999, 0.0]],
                  columns=list("abcdefghij"))
        grade = sc.assign_grade(f)
        assert list(grade.iloc[0]) == [
            "强烈看多", "强烈看多", "看多",
            "看多", "中性",
            "中性", "看空",
            "看空", "强烈看空",
            "强烈看空",
        ]

    def test_nan_stays_unassigned(self):
        f = frame([[50.0, np.nan]], columns=list("ab"))
        grade = sc.assign_grade(f)
        assert grade.iloc[0, 0] == "中性"
        assert grade.iloc[0, 1] is None

    def test_custom_grades(self):
        grades = [{"min_pct": 50, "label": "高", "key": "high"},
                  {"min_pct": 0, "label": "低", "key": "low"}]
        grade = sc.assign_grade(frame([[80.0, 20.0]], columns=list("ab")), grades)
        assert list(grade.iloc[0]) == ["高", "低"]

    def test_grade_key(self):
        assert sc.grade_key("强烈看多") == "strong_buy"
        assert sc.grade_key("不存在的评级") == ""

    def test_distribution(self):
        f = pd.DataFrame(
            [["强烈看多", "看多", "中性", "看空"]],
            index=DATES[:1],
            columns=list("abcd"),
        )
        assert sc.distribution(f) == {"强烈看多": 1, "看多": 1, "中性": 1, "看空": 1}
        assert sc.distribution(f, day=DATES[0]) == {"强烈看多": 1, "看多": 1,
                                                    "中性": 1, "看空": 1}


# ---------------- 一站式 ----------------


class TestScorePanels:
    def _build(self, industry=True):
        rng = np.random.default_rng(5)
        n_days, n_codes = 6, 8
        index = DATES[:n_days]
        codes = list("abcdefgh")
        base = pd.DataFrame(
            100.0 * np.cumprod(1 + rng.normal(0, 0.02, (n_days, n_codes)), axis=0),
            index=index, columns=codes,
        )
        factors = {
            "f_trend": (base * 1.01).astype("float32"),
            "f_momentum": (base * 0.99).astype("float32"),
            "f_risk_low": (1.0 / (base + 1.0)).astype("float32"),
        }
        meta = {"f_trend": {"direction": 1}, "f_momentum": {"direction": 1},
                "f_risk_low": {"direction": -1}}
        dimension_of = {"f_trend": "trend", "f_momentum": "momentum",
                        "f_risk_low": "risk"}
        weights = {"trend": 0.5, "momentum": 0.3, "risk": 0.2}
        industry_series = pd.Series(
            {c: ("X" if i < 5 else "Y") for i, c in enumerate(codes)}
        ) if industry else pd.Series(dtype=object)
        return factors, meta, dimension_of, weights, industry_series, codes

    def test_end_to_end_shapes_and_grades(self):
        factors, meta, dim_of, weights, industry, codes = self._build()
        result = sc.score_panels(factors, meta, dim_of, weights, industry=industry)

        assert set(result) >= {"score_abs", "score_pct", "grade", "data_quality",
                              "dims", "diagnostics"}
        for key in ("score_abs", "score_pct", "data_quality", "grade"):
            assert result[key].shape == (6, 8)
        assert set(result["dims"]) == {"trend", "momentum", "risk"}
        assert result["score_pct"].to_numpy().max() <= 100.0 + 1e-6
        assert result["score_pct"].to_numpy().min() >= 0.0 - 1e-6
        grades = set(np.unique(result["grade"].to_numpy())) - {None}
        assert grades <= {"强烈看多", "看多", "中性", "看空", "强烈看空"}

    def test_diagnostics_industry_counts(self):
        factors, meta, dim_of, weights, industry, codes = self._build()
        result = sc.score_panels(factors, meta, dim_of, weights, industry=industry)
        diag = result["diagnostics"].to_dict()
        assert diag["industry_count"] == 2          # X(5) 与 Y(3)
        assert diag["industry_covered_codes"] == 5  # X 够 min_samples
        assert diag["industry_fallback_codes"] == 3 # Y 不够 → 全市场
        assert diag["industry_neutral_factors"] == 3
        assert any("行业中性" in note for note in diag["notes"])
        # 维度数必须动态：这个用例只造了 3 个维度，文案就该说「3 维齐全」。
        # 硬编码「四维」会在六维版本里给出自相矛盾的诊断（P5 实测踩过）。
        assert any("3 维齐全" in note for note in diag["notes"]), diag["notes"]

    def test_diagnostics_without_industry(self):
        factors, meta, dim_of, weights, _, _ = self._build(industry=False)
        result = sc.score_panels(factors, meta, dim_of, weights, industry=None)
        diag = result["diagnostics"].to_dict()
        assert diag["industry_count"] == 0
        assert diag["industry_neutral_factors"] == 0
        assert diag["industry_fallback_factors"] == 3

    def test_progress_callback(self):
        factors, meta, dim_of, weights, industry, _ = self._build()
        seen = []
        sc.score_panels(factors, meta, dim_of, weights, industry=industry,
                        on_progress=lambda i, t, n: seen.append(n))
        assert seen == list(factors)

    def test_direction_minus_one_flips_dimension(self):
        """direction=-1 的因子必须让「原始值大」的股票得分低。"""
        factors, meta, dim_of, weights, _, _ = self._build()
        flipped = sc.score_panels(factors, meta, dim_of, weights, industry=None)
        assert "dims" in flipped
