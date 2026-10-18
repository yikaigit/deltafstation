"""打分编排测试：版本配置、窗口回溯、快照落盘、增量与全量一致性。

测试全部跑在合成数据湖上（tmp_path），不碰真实 ``data/``。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from backend.core import scorer as scorer_module
from backend.core.calendar import Calendar
from backend.core.config import get_config
from backend.core.scorer import LOOKBACK_BUFFER, MAX_LOOKBACK, Scorer

app_cfg = get_config()

CODES = ("sh.600000", "sz.000001", "sz.300750")
N_DAYS = MAX_LOOKBACK + LOOKBACK_BUFFER + 40      # 460，覆盖全量重算的窗口需求


@pytest.fixture()
def trading_days():
    return pd.bdate_range("2023-01-03", periods=N_DAYS)


@pytest.fixture()
def lake_with_data(cfg, lake, trading_days):
    """合成数据湖：bars / adj_factor / universe / calendar / index_bars。"""
    rng = np.random.default_rng(20240101)
    rows = []
    for offset, code in enumerate(CODES):
        listed_from = 60 if offset == 2 else 0
        price = 10.0 + 8.0 * offset
        for index, day in enumerate(trading_days):
            if index < listed_from:
                continue
            price *= 1.0 + rng.normal(0.0003 + 0.0002 * offset, 0.017 + 0.004 * offset)
            open_ = price * (1.0 + rng.normal(0, 0.004))
            close = price
            high = max(open_, close) * 1.005
            low = min(open_, close) * 0.995
            volume = float(rng.uniform(8e5, 2e6))
            prev = rows[-1]["close"] if rows and rows[-1]["code"] == code else close
            rows.append({
                "date": day, "code": code,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": volume, "amount": volume * close,
                "turn": float(rng.uniform(0.002, 0.04)),
                "pct_chg": (close / prev - 1.0) * 100.0,
                "tradestatus": 1,
            })
    bars = pd.DataFrame(rows)
    lake.write("bars", bars)
    lake.write("adj_factor", pd.DataFrame({
        "code": ["sh.600000"],
        "ex_date": [trading_days[120]],
        "dividend": [0.2],
        "back_factor": [1.5],
    }))
    lake.write("universe", pd.DataFrame({
        "code": list(CODES),
        "name": ["甲银行", "乙平安", "丙科技"],
        "board": ["main", "main", "gem"],
        "industry_l1": ["银行Ⅱ", "银行Ⅱ", "计算机"],
        "in_hs300": [True, True, True],
        "is_st": [False, False, False],
    }))
    lake.write("calendar", pd.DataFrame({"date": trading_days, "is_trading_day": True}))
    lake.write("index_bars", pd.DataFrame({
        "date": trading_days,
        "code": ["sh.000300"] * len(trading_days),
        "close": 4000.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.009, len(trading_days))),
    }))
    return lake


@pytest.fixture()
def scorer(cfg, lake_with_data, store):
    return Scorer(cfg=cfg, lake=lake_with_data, store=store)


@pytest.fixture(autouse=True)
def isolate_meta_db(monkeypatch, db):
    """record_version 内部用 get_meta_db()（全局配置），测试里必须指向临时库。"""
    monkeypatch.setattr(scorer_module, "get_meta_db", lambda: db)


class TestVersionConfig:
    def test_load_v1(self):
        spec = Scorer.load_version("v1.0.0")
        assert spec["version"] == "v1.0.0"
        assert len(spec["factors"]) == 19
        assert spec["weights"] == {"trend": 0.35, "momentum": 0.28,
                                   "volume": 0.22, "risk": 0.15}
        assert spec["dimension_of"]["ma_bias_20"] == "trend"
        assert spec["dimension_of"]["dd_1y"] == "risk"
        assert spec["grades"][0]["label"] == "强烈看多"

    def test_unknown_version(self):
        with pytest.raises(FileNotFoundError, match="找不到打分版本配置"):
            Scorer.load_version("v9.9.9")

    def test_unknown_factor_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scorer_module, "SCORING_DIR", tmp_path)
        (tmp_path / "bad.yaml").write_text(
            yaml.safe_dump({"version": "bad", "dimensions": {
                "trend": {"weight": 1.0, "factors": ["no_such_factor"]}}}),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="未实现的因子"):
            Scorer.load_version("bad")

    def test_missing_dimensions_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scorer_module, "SCORING_DIR", tmp_path)
        (tmp_path / "empty.yaml").write_text(yaml.safe_dump({"version": "empty"}),
                                             encoding="utf-8")
        with pytest.raises(ValueError, match="没有配置任何维度"):
            Scorer.load_version("empty")

    def test_available_versions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scorer_module, "SCORING_DIR", tmp_path)
        assert Scorer.available_versions() == []
        for name in ("b.yaml", "a.yaml"):
            (tmp_path / name).write_text("version: x\ndimensions: {}\n", encoding="utf-8")
        assert Scorer.available_versions() == ["a", "b"]


class TestWindowStart:
    def test_calendar_based_backtrack(self, scorer, trading_days, monkeypatch):
        monkeypatch.setattr("backend.core.calendar.get_calendar",
                            lambda: Calendar(scorer.cfg, scorer.lake))
        day = pd.Timestamp(trading_days[-1])
        start = pd.Timestamp(scorer._window_start(day))
        expected = pd.Timestamp(trading_days[-1 - (MAX_LOOKBACK + LOOKBACK_BUFFER)])
        assert start == expected

    def test_calendar_failure_falls_back_to_natural_days(self, scorer, monkeypatch):
        def boom():
            raise RuntimeError("no calendar")

        monkeypatch.setattr("backend.core.calendar.get_calendar", boom)
        day = pd.Timestamp("2024-06-03")
        span = MAX_LOOKBACK + LOOKBACK_BUFFER
        start = pd.Timestamp(scorer._window_start(day))
        assert start == day - pd.Timedelta(days=int(span * 1.55))

    def test_window_covers_max_lookback(self, scorer):
        assert MAX_LOOKBACK + LOOKBACK_BUFFER >= 420
        assert MAX_LOOKBACK == 260


class TestScoreDay:
    def test_summary(self, scorer, trading_days):
        result = scorer.score_day(write=False)
        assert result["mode"] == "day"
        # 默认版本跟随 config/app.yaml 的 scoring.default_version
        assert result["version"] == app_cfg.get("scoring.default_version", "v1.1.0")
        assert result["codes"] == 3
        assert result["window_days"] >= MAX_LOOKBACK + LOOKBACK_BUFFER
        assert result["diagnostics"]["valid_dates"] > 0

    def test_explicit_date(self, scorer, trading_days):
        day = trading_days[-5]
        result = scorer.score_day(trade_date=day.strftime("%Y-%m-%d"), write=False)
        assert result["trade_date"] == day.strftime("%Y-%m-%d")

    def test_non_trading_date_raises(self, scorer, trading_days):
        """周末（数据集内部的非交易日）应报「不在行情日期内」。"""
        trading = set(trading_days)
        weekend = pd.Timestamp(trading_days[100])
        while weekend in trading:
            weekend += pd.Timedelta(days=1)
        assert weekend not in trading
        with pytest.raises(RuntimeError, match="不在行情日期内"):
            scorer.score_day(trade_date=weekend.strftime("%Y-%m-%d"), write=False)

    def test_write_creates_snapshot(self, scorer, store, trading_days):
        scorer.score_day(write=False)  # 先暖身，确认无副作用
        result = scorer.score_day(write=True)
        path = store.root / f"version={result['version']}" / \
            f"trade_date={result['trade_date']}" / "part.parquet"
        assert path.exists()
        assert result["rows"] == 3
        assert result["days_written"] == 1
        assert set(result["distribution"]) <= {"强烈看多", "看多", "中性", "看空", "强烈看空"}

    def test_progress_callback_monotonic(self, scorer, trading_days):
        ratios = []
        scorer.score_day(write=False,
                         progress=lambda r, m: ratios.append((r, m)))
        assert len(ratios) > 5
        values = [r for r, _ in ratios]
        assert values == sorted(values), "进度必须单调不减"
        assert values[-1] > 0.7, "计算子步骤必须推进到 0.7 以上（write=False 不写快照）"


class TestScoreRange:
    def test_summary_and_write(self, scorer, store, trading_days):
        result = scorer.score_range(write=True, days=3)
        assert result["mode"] == "range"
        assert result["codes"] == 3
        assert result["dates"] == N_DAYS
        assert result["end"] == trading_days[-1].strftime("%Y-%m-%d")
        assert result["days_written"] == 3, "snapshot_days 只落盘最近 3 天"
        assert len(store.dates(result["version"])) == 3

    def test_full_history_computed_even_if_not_written(self, scorer):
        result = scorer.score_range(write=False, days=0)
        assert result["dates"] == N_DAYS
        assert result["valid_dates"] > 0


class TestPersist:
    def _result_and_panel(self, scorer, trading_days):
        from backend.core.panels import Panels

        panel = Panels.from_lake(lake=scorer.lake, cfg=scorer.cfg,
                                 start=trading_days[0], end=trading_days[-1])
        spec = Scorer.load_version("v1.0.0")
        return scorer._compute(panel, spec), panel, spec

    def test_dimension_columns_padded(self, scorer, store, trading_days):
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        written = scorer._persist(result, panel, spec, days=2)
        table = store.read(version=spec["version"])
        for column in ("dim_trend", "dim_momentum", "dim_volume", "dim_risk",
                       "dim_valuation", "dim_quality"):
            assert column in table.columns
        assert table["dim_valuation"].isna().all(), "技术四维没有估值列"
        assert table["dim_trend"].notna().any()
        assert written["rows"] == 6

    def test_only_dates_filter(self, scorer, store, trading_days):
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        scorer._persist(result, panel, spec, only_dates=[trading_days[-1]])
        table = store.read(version=spec["version"])
        assert table["trade_date"].nunique() == 1
        assert table["trade_date"].iloc[0] == pd.Timestamp(trading_days[-1])

    def test_days_filter_keeps_tail(self, scorer, store, trading_days):
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        scorer._persist(result, panel, spec, days=5)
        table = store.read(version=spec["version"])
        assert table["trade_date"].nunique() == 5
        assert table["trade_date"].max() == pd.Timestamp(trading_days[-1])

    def test_rows_only_where_score_exists(self, scorer, store, trading_days):
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        scorer._persist(result, panel, spec, days=1)
        table = store.read(version=spec["version"])
        assert table["score_pct"].notna().all()

    def test_universe_merged(self, scorer, store, trading_days):
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        scorer._persist(result, panel, spec, days=1)
        table = store.read(version=spec["version"])
        assert set(table["name"].dropna()) == {"甲银行", "乙平安", "丙科技"}
        assert "industry" in table.columns

    def test_record_version_failure_does_not_break_persist(self, scorer, store,
                                                           trading_days, monkeypatch,
                                                           db):
        def boom(**kwargs):
            raise RuntimeError("meta.db 挂了")

        monkeypatch.setattr(db, "add_score_version", boom)
        result, panel, spec = self._result_and_panel(scorer, trading_days)
        written = scorer._persist(result, panel, spec, days=1)
        assert written["rows"] == 3


class TestConsistency:
    def test_incremental_matches_full_recompute(self, scorer, trading_days):
        """P2 验收标准「同日两次运行结果完全一致」的严格版：
        增量（420 交易日窗口）与全历史重算的最后一天必须逐位一致。"""
        from backend.core.panels import Panels

        spec = Scorer.load_version("v1.0.0")
        day = trading_days[-1]

        full_panel = Panels.from_lake(lake=scorer.lake, cfg=scorer.cfg)
        full = scorer._compute(full_panel, spec)

        window_start = scorer._window_start(pd.Timestamp(day))
        inc_panel = Panels.from_lake(lake=scorer.lake, cfg=scorer.cfg,
                                     start=window_start, end=day.strftime("%Y-%m-%d"))
        incremental = scorer._compute(inc_panel, spec)

        for key in ("score_pct", "score_abs", "data_quality", "grade"):
            a = full[key].iloc[-1].to_numpy()
            b = incremental[key].iloc[-1].to_numpy()
            if key == "grade":
                assert list(a) == list(b), key
            else:
                assert np.allclose(a, b, equal_nan=True, rtol=1e-6), key
        for dim in full["dims"]:
            a = full["dims"][dim].iloc[-1].to_numpy()
            b = incremental["dims"][dim].iloc[-1].to_numpy()
            assert np.allclose(a, b, equal_nan=True, rtol=1e-6), dim

    def test_two_runs_are_identical(self, scorer, trading_days):
        """同模式两次运行必须完全一致（验收标准原文）。"""
        a = scorer.score_day(write=False)
        b = scorer.score_day(write=False)
        assert a["diagnostics"] == b["diagnostics"]
        assert a["codes"] == b["codes"]


class TestHelpers:
    def test_emit_none_safe(self):
        scorer_module._emit(None, 0.5, "msg")

    def test_sub_maps_ratio(self):
        seen = []
        mapped = scorer_module._sub(lambda r, m: seen.append(r), 0.2, 0.6)
        mapped(0.0, "start")
        mapped(0.5, "mid")
        mapped(1.0, "end")
        assert seen == [0.2, 0.4, 0.6]

    def test_sub_none_passthrough(self):
        assert scorer_module._sub(None, 0.0, 1.0) is None

    def test_sub_negative_ratio_reports_indeterminate(self):
        seen = []
        scorer_module._sub(lambda r, m: seen.append(r), 0.0, 1.0)(-1.0, "unknown")
        assert seen == [-1.0]

    def test_last_distribution(self, trading_days):
        table = pd.DataFrame({
            "trade_date": pd.to_datetime([trading_days[-1]] * 3),
            "grade": ["看多", "看多", "中性"],
        })
        assert scorer_module._last_distribution(table) == {"看多": 2, "中性": 1}
        assert scorer_module._last_distribution(pd.DataFrame()) == {}

    def test_get_scorer_singleton(self, monkeypatch, cfg, lake_with_data, store):
        scorer_module._scorer = None
        first = scorer_module.get_scorer()
        assert first is scorer_module.get_scorer()
        scorer_module._scorer = None
