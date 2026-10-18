"""数据湖测试：分区读写、幂等 upsert、按年裁剪、覆盖统计、DuckDB 查询。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.lake import DATASETS, LakeError


class TestRegistration:
    def test_unknown_dataset_raises(self, lake):
        with pytest.raises(LakeError, match="未注册的数据集"):
            lake.dataset_path("no_such_dataset")

    def test_all_registered_have_keys(self):
        for name, spec in DATASETS.items():
            assert spec["keys"], f"{name} 缺少主键定义"


class TestEmptyState:
    def test_not_exists_before_write(self, lake):
        assert lake.exists("bars") is False

    def test_read_returns_empty_frame(self, lake):
        df = lake.read("bars")
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_latest_date_none(self, lake):
        assert lake.latest_date("bars") is None

    def test_coverage_empty(self, lake):
        cov = lake.coverage("bars")
        assert list(cov.columns) == ["code", "first_date", "last_date", "rows"]
        assert cov.empty


class TestPartitionedWrite:
    def test_write_creates_year_partitions(self, lake, bars_df):
        lake.write("bars", bars_df)
        folder = lake.dataset_path("bars")
        years = sorted(p.parent.name for p in folder.glob("year=*/part.parquet"))
        assert years == ["year=2023", "year=2024"]

    def test_roundtrip_preserves_rows(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars")
        assert len(out) == len(bars_df)
        assert set(out["code"]) == {"sh.600000", "sz.000001"}

    def test_years_filter(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", years=[2024])
        assert len(out) == 3
        assert out["date"].dt.year.unique().tolist() == [2024]

    def test_date_range_filter(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", start="2024-01-01", end="2024-01-31")
        assert len(out) == 3
        assert out["date"].min() >= pd.Timestamp("2024-01-01")

    def test_column_projection(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["date", "code"])
        assert list(out.columns) == ["date", "code"]


class TestIdempotency:
    def test_rewrite_same_rows_does_not_duplicate(self, lake, bars_df):
        lake.write("bars", bars_df)
        lake.write("bars", bars_df)
        assert len(lake.read("bars")) == len(bars_df)

    def test_upsert_keeps_latest_value(self, lake, bars_df):
        lake.write("bars", bars_df)
        patch = bars_df.iloc[[0]].copy()
        patch.loc[:, "close"] = 99.9
        lake.write("bars", patch)
        out = lake.read("bars", years=[2023])
        row = out[(out["code"] == "sh.600000") & (out["date"] == pd.Timestamp("2023-12-28"))]
        assert len(row) == 1
        assert row["close"].iloc[0] == pytest.approx(99.9)
        assert len(out) == 3  # 2023 年仍是 3 行，没有新增

    def test_incremental_append(self, lake, bars_df):
        lake.write("bars", bars_df)
        extra = pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-04"]),
                "code": ["sh.600000"],
                "open": [10.3], "high": [10.5], "low": [10.0], "close": [10.4], "volume": [1300],
            }
        )
        lake.write("bars", extra)
        assert len(lake.read("bars")) == len(bars_df) + 1


class TestValidation:
    def test_missing_key_column_raises(self, lake):
        bad = pd.DataFrame({"date": pd.to_datetime(["2024-01-02"]), "close": [1.0]})
        with pytest.raises(LakeError, match="缺少主键列"):
            lake.write("bars", bad)

    def test_empty_write_is_noop(self, lake, bars_df):
        assert lake.write("bars", pd.DataFrame()) == 0
        assert lake.exists("bars") is False


class TestSingleFileDataset:
    def test_universe_roundtrip(self, lake):
        uni = pd.DataFrame(
            {
                "code": ["sh.600000", "sz.000001"],
                "name": ["浦发银行", "平安银行"],
                "board": ["主板", "主板"],
                "industry_l1": ["银行", "银行"],
                "list_date": pd.to_datetime(["1999-11-10", "1991-04-03"]),
            }
        )
        lake.write("universe", uni)
        assert lake.exists("universe") is True
        out = lake.read("universe")
        assert len(out) == 2
        assert lake.file_path("universe").exists()

    def test_universe_upsert_by_code(self, lake):
        lake.write("universe", pd.DataFrame({"code": ["sh.600000"], "name": ["旧名"]}))
        lake.write("universe", pd.DataFrame({"code": ["sh.600000"], "name": ["新名"]}))
        out = lake.read("universe")
        assert len(out) == 1
        assert out["name"].iloc[0] == "新名"


class TestCoverage:
    def test_coverage_stats(self, lake, bars_df):
        lake.write("bars", bars_df)
        cov = lake.coverage("bars").set_index("code")
        assert cov.loc["sh.600000", "rows"] == 4
        assert cov.loc["sh.600000", "first_date"] == pd.Timestamp("2023-12-28")
        assert cov.loc["sh.600000", "last_date"] == pd.Timestamp("2024-01-03")
        assert cov.loc["sz.000001", "rows"] == 2

    def test_latest_date(self, lake, bars_df):
        lake.write("bars", bars_df)
        assert lake.latest_date("bars") == "2024-01-03"


class TestIndexMembers:
    def test_snapshot_is_immutable(self, lake):
        lake.write_index_members("2024-06-30", "hs300", ["sh.600000"])
        lake.write_index_members("2024-12-31", "hs300", ["sh.600000", "sz.000001"])
        latest = lake.latest_index_snapshot("hs300")
        assert len(latest) == 2
        # 旧快照仍在
        folder = lake.dataset_path("universe").parent / "index_members"
        assert len(list(folder.glob("hs300_*.parquet"))) == 2

    def test_no_snapshot_returns_empty(self, lake):
        out = lake.latest_index_snapshot("hs300")
        assert out.empty
        assert "code" in out.columns


class TestSQL:
    def test_duckdb_query(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.sql(
            "SELECT code, count(*) AS n FROM read_parquet('{lake}/bars/year=*/part.parquet') GROUP BY code ORDER BY code"
        )
        assert len(out) == 2
        assert int(out["n"].sum()) == len(bars_df)


class TestReadFilters:
    """``read(filters=...)``：把行级过滤下推到 Parquet。

    这是 P4 加的能力：个股抽屉每次只关心一只票，整表拉进 pandas 再切片
    在 20 万行的复权因子上要 0.7s，下推后 0.05s。
    """

    def test_equality(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["code"], filters=[("code", "=", "sh.600000")])
        assert len(out) == 4
        assert set(out["code"]) == {"sh.600000"}

    def test_in_operator(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", filters=[("code", "in", ["sz.000001"])])
        assert len(out) == 2

    def test_combined_with_date_range(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read(
            "bars",
            filters=[("code", "=", "sh.600000")],
            start="2024-01-01",
            end="2024-01-02",
        )
        assert len(out) == 1
        assert out["date"].iloc[0] == pd.Timestamp("2024-01-02")

    def test_multiple_conditions(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read(
            "bars",
            filters=[("code", "=", "sh.600000"), ("close", ">=", 10.3)],
        )
        # 样本里 sh.600000 的收盘价是 10.2 / 10.4 / 10.1 / 10.3 → 两条满足 ≥10.3
        assert len(out) == 2
        assert sorted(out["close"].tolist()) == [pytest.approx(10.3), pytest.approx(10.4)]

    def test_no_match_returns_empty(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", filters=[("code", "=", "sh.999999")])
        assert out.empty

    def test_unknown_operator_raises(self, lake, bars_df):
        from backend.core.lake import LakeError

        lake.write("bars", bars_df)
        with pytest.raises(LakeError):
            lake.read("bars", filters=[("code", "~", "sh.600000")])


class TestDateFilterWithoutDateColumn:
    """回归：``columns=["code"]`` + 日期范围时，日期过滤不能被静默跳过。

    这是一个**沉默**的 bug —— 不报错、不警告，只是把「当日有多少只票」
    悄悄变成了「历史上出现过多少只票」。P6 的「数据到位」判定就挂在这上面：
    修复前 ``landed_report`` 会永远返回 True，pending 分支形同虚设。
    """

    def test_date_range_applied_without_date_column(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["code"], start="2024-01-02", end="2024-01-02")
        # 样本里 2024-01-02 有两只票
        assert len(out) == 2
        assert "date" not in out.columns

    def test_no_rows_outside_range(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["code"], start="2024-06-01", end="2024-06-30")
        assert out.empty

    def test_full_history_when_no_range(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["code"])
        assert len(out) == len(bars_df)

    def test_start_only(self, lake, bars_df):
        lake.write("bars", bars_df)
        out = lake.read("bars", columns=["code"], start="2024-01-01")
        # 样本里 2024 年之后有 3 条：600000 的 01-02/01-03 与 000001 的 01-02
        assert len(out) == 3

    def test_index_bars_and_valuation_same_behavior(self, lake):
        import pandas as pd

        lake.write("index_bars", pd.DataFrame({
            "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "code": ["sh.000300", "sh.000300"],
            "close": [3000.0, 3010.0], "open": [3000.0, 3000.0],
            "high": [3010.0, 3020.0], "low": [2990.0, 3000.0], "volume": [1e6, 1e6],
        }))
        out = lake.read("index_bars", columns=["code"], start="2024-01-03", end="2024-01-03")
        assert len(out) == 1
