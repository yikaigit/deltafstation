"""代码归一、板块判定、涨跌停幅度。"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from backend.core.codes import (
    BOARD_BJ,
    BOARD_GEM,
    BOARD_MAIN,
    BOARD_STAR,
    board_label,
    board_of,
    is_index,
    limit_pct,
    limit_price,
    normalize_bs_code,
    to_bs_code,
    to_em_code,
    to_secucode,
)


class TestNormalize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("sh.600000", "sh.600000"),
            ("SH.600000", "sh.600000"),
            ("600000", "sh.600000"),
            ("600000.SH", "sh.600000"),
            ("600000.SH".lower(), "sh.600000"),
            ("000001", "sz.000001"),
            ("000001.SZ", "sz.000001"),
            ("300750", "sz.300750"),
            ("688981", "sh.688981"),
            ("830799", "bj.830799"),
            ("sh.600000.csv", "sh.600000"),
            ("  sh.600000  ", "sh.600000"),
        ],
    )
    def test_common_forms(self, raw, expected):
        assert normalize_bs_code(raw) == expected

    def test_empty(self):
        assert normalize_bs_code("") == ""
        assert normalize_bs_code(None) == ""

    def test_unrecognized_passthrough(self):
        assert normalize_bs_code("abc") == "abc"


class TestConversions:
    def test_to_em_code(self):
        assert to_em_code("sh.600000") == "600000"
        assert to_em_code("000001") == "000001"

    def test_to_bs_code(self):
        assert to_bs_code("600519.SH") == "sh.600519"
        assert to_bs_code("600519") == "sh.600519"

    def test_to_secucode(self):
        assert to_secucode("sh.600000") == "600000.SH"
        assert to_secucode("sz.000001") == "000001.SZ"

    def test_roundtrip(self):
        for code in ("sh.600000", "sz.000001", "sh.688981", "sz.300750"):
            assert to_bs_code(to_em_code(code)) == code
            assert to_bs_code(to_secucode(code)) == code


class TestBoard:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("sh.600000", BOARD_MAIN),
            ("sh.601398", BOARD_MAIN),
            ("sh.688981", BOARD_STAR),
            ("sz.000001", BOARD_MAIN),
            ("sz.002415", BOARD_MAIN),
            ("sz.003816", BOARD_MAIN),
            ("sz.300750", BOARD_GEM),
            ("sz.301029", BOARD_GEM),
            # 创业板代码段 2024 年后扩容到 302，只认 300/301 会把 302 误判成主板
            ("sz.302132", BOARD_GEM),
            ("bj.830799", BOARD_BJ),
        ],
    )
    def test_board_of(self, code, expected):
        assert board_of(code) == expected

    def test_board_label(self):
        assert board_label("sh.600000") == "主板"
        assert board_label("sz.300750") == "创业板"
        assert board_label("sh.688981") == "科创板"


class TestLimitPct:
    def test_main_board(self):
        assert limit_pct("sh.600000") == pytest.approx(0.10)

    def test_star_board(self):
        assert limit_pct("sh.688981") == pytest.approx(0.20)

    def test_bj(self):
        assert limit_pct("bj.830799") == pytest.approx(0.30)

    def test_gem_before_reform(self):
        assert limit_pct("sz.300750", "2020-08-21") == pytest.approx(0.10)

    def test_gem_on_reform_date(self):
        # 2020-08-24 当天即适用新规
        assert limit_pct("sz.300750", "2020-08-24") == pytest.approx(0.20)

    def test_gem_after_reform(self):
        assert limit_pct("sz.300750", "2026-10-16") == pytest.approx(0.20)

    def test_gem_302_segment_uses_20pct(self):
        """回归：sz.302132 曾被误判成主板，阈值掉到 10%，产生 7 天假告警。"""
        assert limit_pct("sz.302132", "2026-10-16") == pytest.approx(0.20)

    def test_main_board_st(self):
        assert limit_pct("sh.600000", "2026-10-16", is_st=True) == pytest.approx(0.05)

    def test_star_st_keeps_20pct(self):
        # 科创板/创业板 ST 仍为 20%
        assert limit_pct("sh.688981", "2026-10-16", is_st=True) == pytest.approx(0.20)

    def test_gem_accepts_timestamp(self):
        """回归：``pd.Timestamp`` 是 ``datetime`` 的子类，也是 ``date`` 的子类，
        原样拿去和 ``date(2020,8,24)`` 比会抛 TypeError。回测引擎按交易日历
        喂进来的正是 Timestamp —— 创业板成分股一多，整个回测直接 500。"""
        assert limit_pct("sz.301236", pd.Timestamp("2022-03-16")) == pytest.approx(0.20)
        assert limit_pct("sz.301236", pd.Timestamp("2020-01-02")) == pytest.approx(0.10)
        assert limit_pct("sz.301236", datetime(2020, 8, 24, 15, 0)) == pytest.approx(0.20)
        # 主板/科创/北交所不吃 on_date，任何类型都不该炸
        assert limit_pct("sh.600000", pd.Timestamp("2022-03-16")) == pytest.approx(0.10)
        assert limit_pct("sh.688981", pd.Timestamp("2022-03-16")) == pytest.approx(0.20)
        assert limit_pct("bj.830799", pd.Timestamp("2022-03-16")) == pytest.approx(0.30)


class TestLimitPrice:
    def test_main_board_prices(self):
        up, down = limit_price(10.0, "sh.600000", "2026-10-16")
        assert up == pytest.approx(11.0)
        assert down == pytest.approx(9.0)

    def test_rounding_to_cent(self):
        up, down = limit_price(9.87, "sh.600000", "2026-10-16")
        assert up == pytest.approx(10.86)
        assert down == pytest.approx(8.88)

    def test_gem(self):
        up, _ = limit_price(10.0, "sz.300750", "2026-10-16")
        assert up == pytest.approx(12.0)


class TestIsIndex:
    @pytest.mark.parametrize(
        "code,expected",
        [
            ("sh.000300", True),   # 沪深300
            ("sh.000001", True),   # 上证指数
            ("sh.999999", True),   # 上证旧编码
            ("sz.399001", True),   # 深证成指
            ("sh.600000", False),  # 浦发银行
            ("sz.000001", False),  # 平安银行（不是指数！）
            ("sz.300750", False),
            ("bj.830799", False),
        ],
    )
    def test_is_index(self, code, expected):
        assert is_index(code) is expected
