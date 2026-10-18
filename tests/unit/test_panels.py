"""面板容器测试：长表透视、后复权口径、缺失语义、切片。

重点回归
--------
* 后复权 = 不复权价 × 当日 back_factor（点-in-time），且 raw_close 必须原样保留；
* 未上市期是 NaN 而不是 0 —— 因子计算要能区分「没有数据」和「数据是 0」；
* 停牌行有价格且 tradestatus=0，透视后不能被丢掉。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.panels import DEFAULT_BENCHMARK, Panels, _pivot_all, _unstack

CODES = ("sh.600000", "sz.000001", "sz.300750")


def make_bars(n_days: int = 40, codes=CODES, start="2024-01-02", seed=3) -> pd.DataFrame:
    """合成日线长表。

    * 第三个标的从第 20 个交易日才上市（测试 NaN 前缀语义）；
    * 停牌行保留价格 + tradestatus=0。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n_days)
    rows = []
    for offset, code in enumerate(codes):
        listed_from = 20 if offset == 2 else 0
        price = 10.0 + 7.0 * offset
        for index, day in enumerate(dates):
            if index < listed_from:
                continue
            price *= 1.0 + rng.normal(0.0005, 0.02)
            open_ = price * (1.0 + rng.normal(0, 0.005))
            high = max(open_, price) * 1.004
            low = min(open_, price) * 0.996
            volume = float(1_000_000 + 1000 * index + 500 * offset)
            prev_close = rows[-1]["close"] if rows and rows[-1]["code"] == code else price
            rows.append(
                {
                    "date": day,
                    "code": code,
                    "open": open_,
                    "high": high,
                    "low": low,
                    "close": price,
                    "volume": volume,
                    "amount": volume * price,
                    "turn": 0.01 + 0.001 * offset,
                    "pct_chg": (price / prev_close - 1.0) * 100.0,
                    "tradestatus": 0 if index % 17 == 3 else 1,
                }
            )
    return pd.DataFrame(rows)


def write_lake(lake, bars: pd.DataFrame, adj_events=None, universe=None,
               index_rows=None) -> None:
    lake.write("bars", bars)
    if adj_events is not None:
        lake.write("adj_factor", adj_events)
    if universe is not None:
        lake.write("universe", universe)
    if index_rows is not None:
        lake.write("index_bars", index_rows)


def make_universe(codes=CODES) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": list(codes),
            "name": ["甲", "乙", "丙"],
            "board": ["main", "main", "gem"],
            "industry_l1": ["银行Ⅱ", "银行Ⅱ", "计算机"],
            "in_hs300": [True, True, True],
            "is_st": [False, False, False],
        }
    )


def make_adj_events(ex_date="2024-01-25", back_factor=2.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "code": ["sh.600000"],
            "ex_date": pd.to_datetime([ex_date]),
            "dividend": [0.5],
            "back_factor": [back_factor],
        }
    )


class TestPivot:
    def test_shape_and_index(self, lake):
        bars = make_bars()
        write_lake(lake, bars)
        panel = Panels.from_lake(lake=lake)
        assert panel.shape == (40, 3)
        assert list(panel.codes) == list(CODES)
        assert panel.dates.is_monotonic_increasing

    def test_values_match_long_table(self, lake):
        bars = make_bars()
        write_lake(lake, bars)
        panel = Panels.from_lake(lake=lake)
        row = bars[bars["code"] == "sz.000001"].set_index("date")["close"]
        got = panel.close["sz.000001"].dropna()
        assert np.allclose(got.to_numpy(), row.to_numpy())

    def test_fields_present(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        for field in ("open", "high", "low", "close", "volume", "amount",
                      "turn", "pct_chg", "tradestatus", "raw_close", "factor"):
            assert field in panel, field
        assert "close" in panel and "nonexistent" not in panel

    def test_attribute_error_message(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        with pytest.raises(AttributeError, match="没有字段"):
            _ = panel.no_such_field

    def test_unlisted_prefix_is_nan_not_zero(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        late = panel.close["sz.300750"]
        assert late.iloc[:20].isna().all()
        assert late.iloc[20:].notna().all()

    def test_suspended_row_keeps_price(self, lake):
        bars = make_bars()
        write_lake(lake, bars)
        panel = Panels.from_lake(lake=lake)
        # 停牌日仍有价格
        assert panel.close.notna().sum().min() >= 20  # 后上市的标的前 20 日为 NaN
        assert set(np.unique(panel.tradestatus.to_numpy())) <= {0, 1}
        assert (panel.tradestatus.iloc[:, 0] == 0).any()

    def test_tradestatus_dtype(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        assert panel.tradestatus.dtypes.unique().tolist() == ["int8"]


class TestBackwardAdjustment:
    """后复权口径是整个回测的地基，单独一组。"""

    def test_price_scaled_by_factor_after_ex_date(self, lake):
        bars = make_bars()
        write_lake(lake, bars, adj_events=make_adj_events())
        panel = Panels.from_lake(lake=lake)
        raw = panel.raw_close["sh.600000"]
        adj = panel.close["sh.600000"]
        ex_day = pd.Timestamp("2024-01-25")
        before = raw.index < ex_day
        assert np.allclose(adj[before].to_numpy(), raw[before].to_numpy())
        assert np.allclose(adj[~before].to_numpy(), 2.0 * raw[~before].to_numpy())

    def test_raw_close_untouched(self, lake):
        bars = make_bars()
        write_lake(lake, bars, adj_events=make_adj_events())
        panel = Panels.from_lake(lake=lake)
        expected = bars[bars["code"] == "sh.600000"].set_index("date")["close"]
        assert np.allclose(panel.raw_close["sh.600000"].to_numpy(), expected.to_numpy())

    def test_factor_frame_mirrors_ratio(self, lake):
        write_lake(lake, make_bars(), adj_events=make_adj_events())
        panel = Panels.from_lake(lake=lake)
        ratio = (panel.close / panel.raw_close).to_numpy()
        assert np.allclose(panel.factor.to_numpy(), ratio, equal_nan=True)

    def test_no_adj_factor_means_raw_equals_adjusted(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        assert np.allclose(panel.close.to_numpy(), panel.raw_close.to_numpy(), equal_nan=True)

    def test_other_codes_unaffected(self, lake):
        write_lake(lake, make_bars(), adj_events=make_adj_events())
        panel = Panels.from_lake(lake=lake)
        assert np.allclose(panel.close["sz.000001"].to_numpy(),
                           panel.raw_close["sz.000001"].to_numpy(), equal_nan=True)


class TestCodesFilter:
    def test_from_lake_filters_codes(self, lake):
        write_lake(lake, make_bars(), universe=make_universe())
        panel = Panels.from_lake(lake=lake, codes=["sh.600000", "sz.300750"])
        assert list(panel.codes) == ["sh.600000", "sz.300750"]

    def test_unknown_codes_raise(self, lake):
        write_lake(lake, make_bars())
        with pytest.raises(RuntimeError, match="没有行情"):
            Panels.from_lake(lake=lake, codes=["sh.999999"])

    def test_date_range(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake, start="2024-01-15", end="2024-02-05")
        assert panel.dates[0] == pd.Timestamp("2024-01-15")
        assert panel.dates[-1] <= pd.Timestamp("2024-02-05")

    def test_empty_lake_raises(self, lake):
        with pytest.raises(RuntimeError, match="backfill"):
            Panels.from_lake(lake=lake)

    def test_empty_range_raises(self, lake):
        write_lake(lake, make_bars())
        with pytest.raises(RuntimeError, match="行情为空"):
            Panels.from_lake(lake=lake, start="2030-01-01")


class TestIndustryAndBenchmark:
    def test_industry_loaded(self, lake):
        write_lake(lake, make_bars(), universe=make_universe())
        panel = Panels.from_lake(lake=lake)
        assert panel.industry.to_dict() == {
            "sh.600000": "银行Ⅱ", "sz.000001": "银行Ⅱ", "sz.300750": "计算机"}

    def test_industry_missing_degrades(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        assert len(panel.industry) == 0

    def test_benchmark_returns(self, lake):
        bars = make_bars(n_days=10)
        index_rows = pd.DataFrame(
            {
                "date": pd.bdate_range("2024-01-02", periods=10),
                "code": [DEFAULT_BENCHMARK] * 10,
                "close": np.linspace(4000.0, 4100.0, 10),
            }
        )
        write_lake(lake, bars, index_rows=index_rows)
        panel = Panels.from_lake(lake=lake)
        assert len(panel.bench_ret) == 10
        assert panel.bench_ret.iloc[0] != panel.bench_ret.iloc[0]  # 首日无收益 → NaN
        assert panel.bench_ret.dropna().gt(0).all()

    def test_benchmark_missing_degrades(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake)
        assert len(panel.bench_ret) == 0

    def test_other_index_code_ignored(self, lake):
        index_rows = pd.DataFrame(
            {
                "date": pd.bdate_range("2024-01-02", periods=5),
                "code": ["sh.000001"] * 5,
                "close": [3000.0] * 5,
            }
        )
        write_lake(lake, make_bars(n_days=5), index_rows=index_rows)
        panel = Panels.from_lake(lake=lake)
        assert len(panel.bench_ret) == 0


class TestSubsetAndDescribe:
    def test_subset_codes(self, lake):
        write_lake(lake, make_bars(), universe=make_universe())
        panel = Panels.from_lake(lake=lake).subset(codes=["sh.600000"])
        assert list(panel.codes) == ["sh.600000"]
        assert list(panel.industry.index) == ["sh.600000"]

    def test_subset_dates(self, lake):
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake).subset(start="2024-01-10", end="2024-01-20")
        assert panel.dates[0] >= pd.Timestamp("2024-01-10")
        assert panel.dates[-1] <= pd.Timestamp("2024-01-20")

    def test_subset_dates_without_benchmark(self, lake):
        """回归：bench_ret 为空（未回填 index_bars）时按日期切片曾抛
        ``Invalid comparison between dtype=object`` —— 空 Series 的 index 是
        object dtype，不能直接和 Timestamp 比较。"""
        write_lake(lake, make_bars())
        panel = Panels.from_lake(lake=lake).subset(start="2024-01-10")
        assert panel.dates[0] >= pd.Timestamp("2024-01-10")
        assert len(panel.bench_ret) == 0

    def test_subset_dates_with_benchmark_slices_bench(self, lake):
        bars = make_bars(n_days=10)
        index_rows = pd.DataFrame(
            {
                "date": pd.bdate_range("2024-01-02", periods=10),
                "code": [DEFAULT_BENCHMARK] * 10,
                "close": np.linspace(4000.0, 4100.0, 10),
            }
        )
        write_lake(lake, bars, index_rows=index_rows)
        panel = Panels.from_lake(lake=lake).subset(start="2024-01-10", end="2024-01-12")
        assert panel.bench_ret.index.min() >= pd.Timestamp("2024-01-10")
        assert panel.bench_ret.index.max() <= pd.Timestamp("2024-01-12")

    def test_describe(self, lake):
        write_lake(lake, make_bars(), universe=make_universe())
        info = Panels.from_lake(lake=lake).describe()
        assert info["dates"] == 40 and info["codes"] == 3
        assert info["industry_covered"] == 3
        assert info["benchmark"] is False
        assert info["first_date"] < info["last_date"]


class TestHelpers:
    def test_pivot_all_shapes_align(self):
        bars = make_bars()
        for field in ("open", "high", "low", "close"):
            bars[f"adj_{field}"] = bars[field]
        bars["raw_close"] = bars["close"]
        frames = _pivot_all(bars)
        assert set(frames["close"].shape) == set(frames["volume"].shape)
        assert list(frames["close"].columns) == list(frames["volume"].columns)

    def test_unstack_sorted(self):
        series = pd.Series(
            [1.0, 2.0, 3.0, 4.0],
            index=pd.MultiIndex.from_tuples(
                [("2024-01-03", "a"), ("2024-01-02", "a"),
                 ("2024-01-03", "b"), ("2024-01-02", "b")],
                names=["date", "code"],
            ),
        )
        wide = _unstack(series)
        assert list(wide.index) == [pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-03")]
        assert np.allclose(wide.to_numpy(), [[2.0, 4.0], [1.0, 3.0]])
