"""标的池：代码筛选、行业映射、成分快照。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.universe import Universe


@pytest.fixture()
def uni(cfg, lake):
    lake.write(
        "universe",
        pd.DataFrame(
            {
                "code": ["sh.600000", "sz.000001", "sh.688981", "sz.300750", "sh.600519"],
                "name": ["浦发银行", "平安银行", "中芯国际", "宁德时代", "贵州茅台"],
                "board": ["main", "main", "star", "gem", "main"],
                "industry_l1": ["银行Ⅱ", "银行Ⅱ", "半导体", "电池", "白酒Ⅱ"],
                "list_date": pd.to_datetime(["1999-11-10"] * 5),
                "delist_date": pd.NaT,
                "is_st": [False, False, False, False, True],
                "in_hs300": [True, True, True, True, False],
                "updated_at": pd.to_datetime(["2026-10-18"] * 5),
            }
        ),
    )
    return Universe(cfg, lake)


class TestCodes:
    def test_all(self, uni):
        assert len(uni.codes(in_hs300_only=False, exclude_st=False)) == 5

    def test_hs300_only(self, uni):
        codes = uni.codes(in_hs300_only=True, exclude_st=False)
        assert "sh.600519" not in codes
        assert len(codes) == 4

    def test_exclude_st(self, uni):
        codes = uni.codes(in_hs300_only=False, exclude_st=True)
        assert "sh.600519" not in codes

    def test_exclude_st_default_from_config(self, uni):
        # app.yaml 里 universe.exclude_st = true
        assert "sh.600519" not in uni.codes(in_hs300_only=False)

    def test_sorted(self, uni):
        codes = uni.codes(in_hs300_only=False, exclude_st=False)
        assert codes == sorted(codes)

    def test_empty(self, cfg, lake):
        assert Universe(cfg, lake).codes() == []


class TestMaps:
    def test_industry_map(self, uni):
        mapping = uni.industry_map()
        assert mapping["sh.600000"] == "银行Ⅱ"
        assert mapping["sz.300750"] == "电池"

    def test_name_map(self, uni):
        assert uni.name_map()["sh.600519"] == "贵州茅台"

    def test_maps_empty(self, cfg, lake):
        empty = Universe(cfg, lake)
        assert empty.industry_map() == {}
        assert empty.name_map() == {}

    def test_count(self, uni):
        assert uni.count() == 5


class TestSnapshots:
    def test_write_and_read(self, uni):
        path = uni.lake.write_index_members("2026-10-12", "hs300", ["sh.600000", "sz.000001"])
        assert path.exists()
        assert uni.snapshots() == ["20261012"]
        assert uni.members_on("2026-10-12") == ["sh.600000", "sz.000001"]

    def test_multiple_snapshots_kept(self, uni):
        uni.lake.write_index_members("2026-06-30", "hs300", ["sh.600000"])
        uni.lake.write_index_members("2026-12-31", "hs300", ["sh.600000", "sz.000001"])
        assert uni.snapshots() == ["20260630", "20261231"]

    def test_missing_snapshot(self, uni):
        assert uni.members_on("2020-01-01") == []
        assert uni.snapshots() == []
