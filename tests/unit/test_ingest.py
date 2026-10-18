"""ingest 编排：幂等、断点续传、失败隔离、降级判定（provider 全部打桩，不打网络）。"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.core.calendar import Calendar
from backend.core.ingest import Ingest
from backend.core.universe import Universe


def make_bars(code: str, dates, base: float = 10.0) -> pd.DataFrame:
    n = len(dates)
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(dates)),
            "code": code,
            "open": [base] * n,
            "high": [base * 1.01] * n,
            "low": [base * 0.99] * n,
            "close": [base] * n,
            "preclose": [base] * n,
            "volume": [10000] * n,
            "amount": [base * 10000] * n,
            "turn": [0.1] * n,
            "tradestatus": [1] * n,
            "pct_chg": [0.0] * n,
            "is_st": [False] * n,
        }
    )


class FakeBaostock:
    def __init__(self, dates):
        self.dates = dates
        self.calls = []
        self.fail_codes = set()

    def session(self):
        from contextlib import nullcontext

        return nullcontext(self)

    def trade_dates(self, start, end):
        self.calls.append(("trade_dates", start, end))
        subset = [d for d in self.dates if pd.Timestamp(start) <= pd.Timestamp(d) <= pd.Timestamp(end)]
        return pd.DataFrame({"date": pd.to_datetime(subset), "is_trading_day": True})

    def hs300_stocks(self):
        self.calls.append(("hs300",))
        return pd.DataFrame(
            {
                "snapshot_date": pd.to_datetime(["2024-01-02", "2024-01-02"]),
                "code": ["sh.600000", "sz.000001"],
                "name": ["浦发银行", "平安银行"],
            }
        )

    def stock_basic(self, code):
        return {
            "code": code,
            "name": "测试",
            "list_date": pd.Timestamp("2000-01-01"),
            "delist_date": None,
        }

    def daily_bars(self, code, start, end):
        self.calls.append(("bars", code, start, end))
        if code in self.fail_codes:
            raise RuntimeError("模拟网络失败")
        subset = [d for d in self.dates if pd.Timestamp(start) <= pd.Timestamp(d) <= pd.Timestamp(end)]
        if not subset:
            return pd.DataFrame()
        return make_bars(code, subset)

    def adjust_factors(self, code):
        self.calls.append(("adj", code))
        return pd.DataFrame(
            {
                "code": [code],
                "ex_date": pd.to_datetime(["2024-06-01"]),
                "fore_factor": [0.9],
                "back_factor": [1.2],
            }
        )

    def many_adjust_factors(self, codes, on_item=None):
        frames = []
        for index, code in enumerate(codes, 1):
            frames.append(self.adjust_factors(code))
            if on_item:
                on_item(index, len(codes), code, 1)
        return pd.concat(frames, ignore_index=True), []


class FakeEastmoney:
    def __init__(self):
        self.calls = []

    def valuation_snapshot(self, trade_date):
        self.calls.append(trade_date)
        return pd.DataFrame(
            {
                "code": ["sh.600000", "sz.000001", "sh.999999"],
                "date": pd.to_datetime([trade_date] * 3),
                "close": [9.7, 11.5, 1.0],
                "pe_ttm": [12.0, 5.0, 3.0],
                "pb_mrq": [0.9, 0.5, 0.4],
                "ps_ttm": [1.2, 1.7, 1.0],
                "peg": [0.8, -1.2, 0.5],
                "total_mv": [1e11, 2e11, 1e10],
                "float_mv": [9e10, 2e11, 1e10],
                "industry": ["银行Ⅱ", "银行Ⅱ", "其他"],
            }
        )

    def many_financials(self, codes, on_item=None):
        frames = []
        for index, code in enumerate(codes, 1):
            frames.append(
                pd.DataFrame(
                    {
                        "code": [code],
                        "report_date": pd.to_datetime(["2026-06-30"]),
                        "notice_date": pd.to_datetime(["2026-08-15"]),
                        "roe_wa": [10.0],
                        "eps": [1.0],
                        "bps": [10.0],
                        "rev": [1e9],
                        "rev_yoy": [5.0],
                        "net_profit": [1e8],
                        "profit_yoy": [3.0],
                        "gross_margin": [30.0],
                        "ocf_ps": [1.5],
                        "div_yield": [None],
                        "notice_estimated": [False],
                    }
                )
            )
            if on_item:
                on_item(index, len(codes), code, 1)
        return pd.concat(frames, ignore_index=True), []


DATES = pd.bdate_range("2024-01-02", "2024-01-12")


@pytest.fixture()
def env(cfg, lake, monkeypatch):
    """构造带日历的 Ingest，provider 全部打桩。"""
    calendar = Calendar(cfg, lake)
    lake.write(
        "calendar",
        pd.DataFrame({"date": DATES, "is_trading_day": True}),
    )
    fake_bs = FakeBaostock(DATES)
    fake_em = FakeEastmoney()

    import backend.core.providers as providers_module

    monkeypatch.setattr(providers_module, "get_baostock", lambda: fake_bs)
    monkeypatch.setattr(providers_module, "get_eastmoney", lambda: fake_em)

    universe = Universe(cfg, lake)
    lake.write(
        "universe",
        pd.DataFrame(
            {
                "code": ["sh.600000", "sz.000001"],
                "name": ["浦发银行", "平安银行"],
                "board": ["main", "main"],
                "industry_l1": ["银行Ⅱ", "银行Ⅱ"],
                "in_hs300": [True, True],
                "is_st": [False, False],
            }
        ),
    )
    ing = Ingest(cfg, lake, calendar, universe)
    return {"ingest": ing, "bs": fake_bs, "em": fake_em, "lake": lake, "cfg": cfg, "calendar": calendar}


class TestBackfillBars:
    def test_writes_all_rows(self, env):
        result = env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        assert result["rows_written"] == len(DATES)
        assert result["failures"] == []
        assert len(env["lake"].read("bars")) == len(DATES)

    def test_rerun_is_idempotent(self, env):
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12", incremental=False)
        assert len(env["lake"].read("bars")) == len(DATES)

    def test_incremental_skips_up_to_date(self, env):
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        result = env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        assert result["skipped_latest"] == 1
        assert result["rows_written"] == 0

    def test_incremental_resumes_from_last_date(self, env):
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-05")
        env["bs"].calls.clear()
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        bars_calls = [c for c in env["bs"].calls if c[0] == "bars"]
        # 起点是「本地 max(date) + 1 个自然日」（2024-01-05 是周五 → 01-06 周六，
        # baostock 对非交易日不返回数据，无需特意跳过）
        assert bars_calls[0][2] == "2024-01-06"
        assert len(env["lake"].read("bars")) == len(DATES)

    def test_single_failure_does_not_abort(self, env):
        env["bs"].fail_codes.add("sz.000001")
        result = env["ingest"].backfill_bars(["sh.600000", "sz.000001"], "2024-01-02", "2024-01-12")
        assert len(result["failures"]) == 1
        assert result["failures"][0]["code"] == "sz.000001"
        assert len(env["lake"].read("bars")) == len(DATES)  # 另一只正常写入

    def test_progress_called(self, env):
        seen = []
        env["ingest"].backfill_bars(
            ["sh.600000", "sz.000001"], "2024-01-02", "2024-01-12",
            progress=lambda r, m: seen.append((r, m)),
        )
        assert seen
        assert seen[-1][0] > 0.5


class TestBackfillFactors:
    def test_writes_events(self, env):
        result = env["ingest"].backfill_factors(["sh.600000", "sz.000001"])
        assert result["rows"] == 2
        assert len(env["lake"].read("adj_factor")) == 2
        assert result["integrity"]["symbols"] == 2

    def test_idempotent(self, env):
        env["ingest"].backfill_factors(["sh.600000"])
        env["ingest"].backfill_factors(["sh.600000"])
        assert len(env["lake"].read("adj_factor")) == 1


class TestValuation:
    def test_filters_to_requested_codes(self, env):
        result = env["ingest"].refresh_valuation("2024-01-05", codes=["sh.600000", "sz.000001"])
        assert result["rows"] == 2
        stored = env["lake"].read("valuation")
        assert set(stored["code"]) == {"sh.600000", "sz.000001"}

    def test_keeps_all_codes_when_unspecified(self, env):
        result = env["ingest"].refresh_valuation("2024-01-05")
        assert result["rows"] == 3

    def test_backfill_valuation_skips_existing_days(self, env):
        env["ingest"].backfill_valuation("2024-01-02", "2024-01-05", codes=["sh.600000"])
        result = env["ingest"].backfill_valuation("2024-01-02", "2024-01-05", codes=["sh.600000"])
        assert result["skipped_existing"] == result["days"]
        assert result["rows"] == 0

    def test_backfill_valuation_covers_range(self, env):
        result = env["ingest"].backfill_valuation("2024-01-02", "2024-01-05", codes=["sh.600000"])
        assert result["days"] == 4
        assert result["rows"] == 4


class TestFundamental:
    def test_writes_reports(self, env):
        result = env["ingest"].refresh_fundamental(["sh.600000", "sz.000001"])
        assert result["rows"] == 2
        stored = env["lake"].read("fundamental")
        assert "notice_date" in stored.columns


class TestUpdateDaily:
    def test_non_trading_day_skips(self, env):
        result = env["ingest"].update_daily("2024-01-06")  # 周六
        assert result["skipped"] is True
        assert result["reason"] == "非交易日"

    def test_normal_flow(self, env):
        result = env["ingest"].update_daily("2024-01-10")
        assert not result.get("skipped")
        assert result["trade_date"] == "2024-01-10"
        assert result["bars_landed"] is True
        assert "pending" not in result
        assert env["lake"].exists("bars")
        assert env["lake"].exists("valuation")

    def test_detects_missing_bars(self, env):
        env["bs"].fail_codes.update({"sh.600000", "sz.000001"})
        result = env["ingest"].update_daily("2024-01-10")
        assert result["bars_landed"] is False
        assert result["pending"] is True

    def test_rerun_idempotent(self, env):
        env["ingest"].update_daily("2024-01-10")
        before = len(env["lake"].read("bars"))
        env["ingest"].update_daily("2024-01-10")
        assert len(env["lake"].read("bars")) == before


class TestDateNormalization:
    """回归：传给数据源的日期必须是纯 YYYY-MM-DD。

    baostock 对 ``2026-10-16 00:00:00`` 会直接报「日期格式不正确」，
    而 ``str(pd.Timestamp(...))`` 恰好带时分秒 —— 这个坑踩过一次，用测试锁住。
    """

    def test_bars_start_end_are_plain_dates(self, env):
        env["ingest"].backfill_bars(["sh.600000"], pd.Timestamp("2024-01-02"), pd.Timestamp("2024-01-12"))
        bars_calls = [c for c in env["bs"].calls if c[0] == "bars"]
        for _, _, start, end in bars_calls:
            assert len(start) == 10 and start[4] == "-" and " " not in start
            assert len(end) == 10 and end[4] == "-" and " " not in end

    def test_valuation_date_is_plain(self, env):
        env["ingest"].refresh_valuation(pd.Timestamp("2024-01-05"))
        assert env["em"].calls == ["2024-01-05"]

    def test_validate_date_is_plain(self, env):
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        result = env["ingest"].validate(pd.Timestamp("2024-01-12"))
        assert result["trade_date"] == "2024-01-12"

    def test_backfill_all_dates_are_plain(self, env):
        env["ingest"].backfill_all(
            start=pd.Timestamp("2024-01-02"),
            end=pd.Timestamp("2024-01-12"),
            valuation_years=0,
            with_fundamental=False,
            progress=None,
        )
        for call in env["bs"].calls:
            if call[0] == "bars":
                assert " " not in call[2] and " " not in call[3]


class TestValidate:
    def test_report_file_written(self, env):
        env["ingest"].backfill_bars(["sh.600000", "sz.000001"], "2024-01-02", "2024-01-12")
        env["ingest"].backfill_factors(["sh.600000", "sz.000001"])
        result = env["ingest"].validate("2024-01-12")
        assert result["symbols"] == 2
        assert result["degraded"] is False
        path = env["cfg"].paths.quality / result["file"]
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["trade_date"] == "2024-01-12"

    def test_no_data_returns_unavailable(self, env):
        assert env["ingest"].validate("2024-01-12")["available"] is False


class TestCoverage:
    def test_after_backfill(self, env):
        env["ingest"].backfill_bars(["sh.600000"], "2024-01-02", "2024-01-12")
        coverage = env["ingest"].coverage()
        assert coverage["bars"]["exists"] is True
        assert coverage["bars"]["rows"] == len(DATES)
        assert coverage["bars"]["first_date"] == "2024-01-02"
        assert coverage["symbols"] == 1
        assert coverage["calendar"]["exists"] is True

    def test_empty(self, cfg, lake):
        ing = Ingest(cfg, lake, Calendar(cfg, lake), Universe(cfg, lake))
        coverage = ing.coverage()
        assert coverage["bars"] == {"exists": False, "rows": 0}
