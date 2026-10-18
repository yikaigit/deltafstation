"""打分快照测试：目录布局、按日写入、历史不覆盖、版本枚举。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.snapshots import SCORE_COLUMNS


def make_snapshot(trade_date: str, codes=("sh.600000", "sz.000001")) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": pd.to_datetime([trade_date] * len(codes)),
            "code": list(codes),
            "score_pct": [88.0, 42.0][: len(codes)],
            "score_abs": [76.5, 51.0][: len(codes)],
            "grade": ["看多", "中性"][: len(codes)],
            "dim_trend": [80.0, 40.0][: len(codes)],
            "dim_momentum": [70.0, 45.0][: len(codes)],
            "dim_volume": [60.0, 50.0][: len(codes)],
            "dim_risk": [55.0, 60.0][: len(codes)],
        }
    )


class TestEmptyState:
    def test_no_versions(self, store):
        assert store.versions() == []

    def test_no_dates(self, store):
        assert store.dates("v1.0.0") == []

    def test_latest_date_none(self, store):
        assert store.latest_date() is None

    def test_read_returns_schema_columns(self, store):
        df = store.read()
        assert df.empty
        assert list(df.columns) == SCORE_COLUMNS


class TestWrite:
    def test_path_layout(self, store):
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        expected = store.root / "version=v1.0.0" / "trade_date=2026-10-16" / "part.parquet"
        assert expected.exists()

    def test_versions_listed(self, store):
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        assert store.versions() == ["v1.0.0"]

    def test_dates_listed_and_sorted(self, store):
        for day in ("2026-10-16", "2026-10-14", "2026-10-15"):
            store.write(make_snapshot(day), version="v1.0.0")
        assert store.dates("v1.0.0") == ["2026-10-14", "2026-10-15", "2026-10-16"]

    def test_latest_date(self, store):
        for day in ("2026-10-14", "2026-10-16"):
            store.write(make_snapshot(day), version="v1.0.0")
        assert store.latest_date("v1.0.0") == "2026-10-16"

    def test_version_column_stamped(self, store):
        store.write(make_snapshot("2026-10-16"), version="v2.0.0")
        out = store.read(version="v2.0.0", trade_date="2026-10-16")
        assert (out["version"] == "v2.0.0").all()

    def test_empty_snapshot_rejected(self, store):
        with pytest.raises(ValueError, match="拒绝写入"):
            store.write(pd.DataFrame(), version="v1.0.0")

    def test_missing_date_column_rejected(self, store):
        df = make_snapshot("2026-10-16").drop(columns=["trade_date"])
        with pytest.raises(ValueError, match="缺少 trade_date"):
            store.write(df, version="v1.0.0")


class TestHistoryPreserved:
    def test_each_day_is_separate_file(self, store):
        store.write(make_snapshot("2026-10-15"), version="v1.0.0")
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        assert len(store.dates("v1.0.0")) == 2

    def test_rewrite_same_day_replaces_not_appends(self, store):
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        patched = make_snapshot("2026-10-16")
        patched.loc[:, "score_pct"] = 99.0
        store.write(patched, version="v1.0.0")
        out = store.read(version="v1.0.0", trade_date="2026-10-16")
        assert len(out) == 2
        assert (out["score_pct"] == 99.0).all()

    def test_versions_are_isolated(self, store):
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        store.write(make_snapshot("2026-10-16"), version="v2.0.0")
        assert store.versions() == ["v1.0.0", "v2.0.0"]
        v1 = store.read(version="v1.0.0")
        assert (v1["version"] == "v1.0.0").all()

    def test_read_all_dates_when_unspecified(self, store):
        store.write(make_snapshot("2026-10-15"), version="v1.0.0")
        store.write(make_snapshot("2026-10-16"), version="v1.0.0")
        assert len(store.read(version="v1.0.0")) == 4


class TestExplicitDate:
    def test_date_injected_when_column_missing(self, store):
        df = make_snapshot("2026-10-16").drop(columns=["trade_date"])
        store.write(df, version="v1.0.0", trade_date="2026-10-16")
        out = store.read(version="v1.0.0", trade_date="2026-10-16")
        assert len(out) == 2
        assert out["trade_date"].iloc[0] == pd.Timestamp("2026-10-16")
