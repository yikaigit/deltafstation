"""providers 纯函数与字段映射（不打网络）。"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.core.providers.baostock_provider import (
    BaostockProvider,
    _looks_like_network_error,
    _normalize_bars,
    _to_date,
)
from backend.core.providers.base import ProviderError, RateLimiter, retry
from backend.core.providers.eastmoney_provider import (
    _map_financial,
    _map_valuation,
    _secucode_to_bs,
    statutory_notice_deadline,
)


class TestSecucode:
    @pytest.mark.parametrize(
        "raw,expected",
        [("600000.SH", "sh.600000"), ("000001.SZ", "sz.000001"), ("688981.SH", "sh.688981")],
    )
    def test_convert(self, raw, expected):
        assert _secucode_to_bs(raw) == expected

    def test_invalid(self):
        assert _secucode_to_bs(None) is None
        assert _secucode_to_bs(float("nan")) is None
        assert _secucode_to_bs("") is None


class TestValuationMapping:
    def test_maps_all_columns(self):
        raw = pd.DataFrame(
            {
                "SECUCODE": ["600000.SH", "000001.SZ"],
                "TRADE_DATE": ["2026-10-16 00:00:00"] * 2,
                "CLOSE_PRICE": [9.73, 11.5],
                "PE_TTM": [12.5, 5.1],
                "PB_MRQ": [0.9, 0.48],
                "PS_TTM": [1.2, 1.68],
                "PEG_CAR": [0.8, -1.23],
                "TOTAL_MARKET_CAP": [1e11, 2.2e11],
                "NOTLIMITED_MARKETCAP_A": [9e10, 2.2e11],
                "BOARD_NAME": ["银行Ⅱ", "银行Ⅱ"],
            }
        )
        out = _map_valuation(raw, "2026-10-16")
        assert list(out.columns) == [
            "code", "date", "close", "pe_ttm", "pb_mrq", "ps_ttm",
            "peg", "total_mv", "float_mv", "industry",
        ]
        assert out["code"].tolist() == ["sh.600000", "sz.000001"]
        assert out["pe_ttm"].iloc[0] == pytest.approx(12.5)
        assert out["industry"].iloc[0] == "银行Ⅱ"
        assert (out["date"] == pd.Timestamp("2026-10-16")).all()

    def test_missing_fields_become_na(self):
        raw = pd.DataFrame({"SECUCODE": ["600000.SH"], "TRADE_DATE": ["2026-10-16"]})
        out = _map_valuation(raw, "2026-10-16")
        assert pd.isna(out["pe_ttm"].iloc[0])

    def test_dedupes_by_date_code(self):
        raw = pd.DataFrame(
            {
                "SECUCODE": ["600000.SH", "600000.SH"],
                "TRADE_DATE": ["2026-10-16"] * 2,
                "CLOSE_PRICE": [9.0, 9.7],
            }
        )
        out = _map_valuation(raw, "2026-10-16")
        assert len(out) == 1
        assert out["close"].iloc[0] == pytest.approx(9.7)

    def test_drops_rows_without_code(self):
        raw = pd.DataFrame({"SECUCODE": [None], "TRADE_DATE": ["2026-10-16"]})
        assert _map_valuation(raw, "2026-10-16").empty


class TestFinancialMapping:
    def test_maps_and_keeps_notice_date(self):
        raw = pd.DataFrame(
            {
                "SECUCODE": ["600519.SH"],
                "REPORTDATE": ["2026-06-30 00:00:00"],
                "NOTICE_DATE": ["2026-08-15 00:00:00"],
                "WEIGHTAVG_ROE": [16.75],
                "BASIC_EPS": [35.57],
                "BPS": [200.99],
                "TOTAL_OPERATE_INCOME": [9.2e10],
                "YSTZ": [1.3],
                "PARENT_NETPROFIT": [4.45e10],
                "SJLTZ": [-1.95],
                "XSMLL": [89.56],
                "MGJYXJJE": [56.55],
                "ZXGXL": [None],
            }
        )
        out = _map_financial(raw)
        row = out.iloc[0]
        assert row["code"] == "sh.600519"
        assert row["report_date"] == pd.Timestamp("2026-06-30")
        assert row["notice_date"] == pd.Timestamp("2026-08-15")
        assert row["roe_wa"] == pytest.approx(16.75)
        assert row["notice_estimated"] is False or row["notice_estimated"] == False  # noqa: E712

    def test_missing_notice_date_uses_statutory_fallback(self):
        raw = pd.DataFrame(
            {"SECUCODE": ["600000.SH"], "REPORTDATE": ["2026-03-31"], "NOTICE_DATE": [None]}
        )
        out = _map_financial(raw)
        row = out.iloc[0]
        assert row["notice_date"] == pd.Timestamp("2026-04-30")  # Q1 + 30 天
        assert bool(row["notice_estimated"]) is True

    def test_dedupes_same_report_period(self):
        raw = pd.DataFrame(
            {
                "SECUCODE": ["600000.SH", "600000.SH"],
                "REPORTDATE": ["2026-06-30", "2026-06-30"],
                "NOTICE_DATE": ["2026-08-20", "2026-08-15"],
                "BASIC_EPS": [1.0, 0.9],
            }
        )
        out = _map_financial(raw)
        assert len(out) == 1
        assert out["eps"].iloc[0] == pytest.approx(1.0)  # 保留第一条（东财按时间倒序，最新在前）


class TestStatutoryDeadline:
    @pytest.mark.parametrize(
        "report,expected",
        [
            ("2026-03-31", "2026-04-30"),  # 一季报 +30
            ("2026-06-30", "2026-08-29"),  # 半年报 +60
            ("2026-09-30", "2026-10-30"),  # 三季报 +30
            ("2025-12-31", "2026-03-31"),  # 年报 +90
        ],
    )
    def test_offsets(self, report, expected):
        assert statutory_notice_deadline(report) == pd.Timestamp(expected)

    def test_nat_returns_none(self):
        assert statutory_notice_deadline(pd.NaT) is None


class TestBarNormalization:
    def test_types_and_columns(self):
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02", "2024-01-03"],
                "open": ["6.63", "6.59"],
                "high": ["6.65", "6.65"],
                "low": ["6.60", "6.59"],
                "close": ["6.60", "6.64"],
                "preclose": ["6.62", "6.60"],
                "volume": ["22066700", "18203654"],
                "amount": ["146066303.72", "120639706.01"],
                "turn": ["0.0752", "0.0620"],
                "tradestatus": ["1", "1"],
                "pctChg": ["-0.3021", "0.6061"],
                "isST": ["0", "0"],
            }
        )
        out = _normalize_bars(raw, "sh.600000")
        assert list(out.columns) == [
            "date", "code", "open", "high", "low", "close", "preclose",
            "volume", "amount", "turn", "tradestatus", "pct_chg", "is_st",
        ]
        assert out["code"].iloc[0] == "sh.600000"
        assert out["tradestatus"].dtype == "int8"
        assert out["is_st"].dtype == bool
        assert out["close"].iloc[0] == pytest.approx(6.60)
        assert out["volume"].iloc[0] == 22066700

    def test_suspended_row_flag(self):
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02"], "open": ["6.6"], "high": ["6.6"], "low": ["6.6"],
                "close": ["6.6"], "preclose": ["6.6"], "volume": ["0"], "amount": ["0"],
                "turn": ["0"], "tradestatus": ["0"], "pctChg": ["0"], "isST": ["0"],
            }
        )
        out = _normalize_bars(raw, "sh.600000")
        assert out["tradestatus"].iloc[0] == 0

    def test_st_flag(self):
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02"], "open": ["1"], "high": ["1"], "low": ["1"],
                "close": ["1"], "preclose": ["1"], "volume": ["100"], "amount": ["100"],
                "turn": ["1"], "tradestatus": ["1"], "pctChg": ["0"], "isST": ["1"],
            }
        )
        assert _normalize_bars(raw, "sh.600000")["is_st"].iloc[0]

    def test_halted_row_empty_strings_become_zero(self):
        """回归：停牌日 baostock 给空字符串，不能让 NaN 流到下游。

        实测 sh.600150 在 2024-09-03~09-18 连续停牌，volume/amount/turn/pctChg
        全是空串，旧实现直接 to_numeric 得到 NaN，校验器判成 R1.5 ERROR。
        """
        raw = pd.DataFrame(
            {
                "date": ["2024-09-03"], "open": ["34.9"], "high": ["34.9"], "low": ["34.9"],
                "close": ["34.9"], "preclose": ["34.9"], "volume": [""], "amount": [""],
                "turn": [""], "tradestatus": ["0"], "pctChg": [""], "isST": ["0"],
            }
        )
        out = _normalize_bars(raw, "sh.600150")
        assert out["tradestatus"].iloc[0] == 0
        for col in ("volume", "amount", "turn", "pct_chg"):
            assert out[col].iloc[0] == 0, col
            assert not pd.isna(out[col].iloc[0]), col
        # 停牌价保留，供上层判断停牌前收盘
        assert out["close"].iloc[0] == pytest.approx(34.9)

    def test_trading_row_keeps_real_values(self):
        """停牌补零不能误伤正常交易日。"""
        raw = pd.DataFrame(
            {
                "date": ["2024-09-02"], "open": ["34.9"], "high": ["35.2"], "low": ["34.8"],
                "close": ["35.0"], "preclose": ["34.9"], "volume": ["1234500"],
                "amount": ["43000000"], "turn": ["0.83"], "tradestatus": ["1"],
                "pctChg": ["0.2865"], "isST": ["0"],
            }
        )
        out = _normalize_bars(raw, "sh.600150")
        assert out["volume"].iloc[0] == 1234500
        assert out["pct_chg"].iloc[0] == pytest.approx(0.2865)

    def test_volume_is_integer_dtype(self):
        """股数应为整数类型，避免 UI 显示 22066700.0。"""
        raw = pd.DataFrame(
            {
                "date": ["2024-01-02"], "open": ["6.6"], "high": ["6.6"], "low": ["6.6"],
                "close": ["6.6"], "preclose": ["6.6"], "volume": ["22066700"],
                "amount": ["1"], "turn": ["1"], "tradestatus": ["1"], "pctChg": ["0"],
                "isST": ["0"],
            }
        )
        assert str(_normalize_bars(raw, "sh.600000")["volume"].dtype) == "int64"


class TestToDate:
    def test_valid(self):
        assert _to_date("1999-11-10") == pd.Timestamp("1999-11-10")

    @pytest.mark.parametrize("value", ["", None, "None"])
    def test_empty(self, value):
        assert _to_date(value) is None


class TestRetry:
    def test_succeeds_after_failures(self):
        state = {"n": 0}

        @retry(times=3, delay=0.001)
        def flaky():
            state["n"] += 1
            if state["n"] < 3:
                raise ValueError("boom")
            return "ok"

        assert flaky() == "ok"
        assert state["n"] == 3

    def test_raises_after_exhausting(self):
        state = {"n": 0}

        @retry(times=2, delay=0.001)
        def always_fail():
            state["n"] += 1
            raise ValueError("boom")

        with pytest.raises(ValueError):
            always_fail()
        assert state["n"] == 2

    def test_on_error_callback(self):
        seen = []

        @retry(times=2, delay=0.001, on_error=lambda n, e: seen.append(n))
        def fail():
            raise ValueError("x")

        with pytest.raises(ValueError):
            fail()
        assert seen == [1, 2]


class TestRateLimiter:
    def test_serialises_calls(self):
        import time

        limiter = RateLimiter(0.05)
        started = time.monotonic()
        for _ in range(3):
            limiter.wait()
        assert time.monotonic() - started >= 0.09

    def test_zero_interval_is_noop(self):
        import time

        limiter = RateLimiter(0)
        started = time.monotonic()
        for _ in range(5):
            limiter.wait()
        assert time.monotonic() - started < 0.05


class TestProviderError:
    def test_is_runtime_error(self):
        assert issubclass(ProviderError, RuntimeError)


class TestNetworkErrorDetection:
    """回归：baostock 把 socket 异常吞成错误码，只能靠文案识别。

    真实故障现场（HS300 全量回填时）：
        daily_bars 第 1/3 次失败：query_history_k_data_plus 失败：网络接收错误。
        'utf-8' codec can't decode byte 0x8f in position 0: invalid start byte
    前半句是 send_msg 吞掉的超时，后半句是缓冲区残留半个包导致的解码失败。
    """

    @pytest.mark.parametrize(
        "message",
        ["网络接收错误。", "网络发送错误。", "服务器连接失败，请稍后再试。", "接收数据异常"],
    )
    def test_recognises_network_failures(self, message):
        assert _looks_like_network_error(message)

    @pytest.mark.parametrize("message", ["返回为空", "日期格式不正确，请修改。", "参数错误"])
    def test_business_errors_are_not_network(self, message):
        assert not _looks_like_network_error(message)

    def test_call_resets_connection_on_network_error(self, monkeypatch):
        """识别到网络故障必须重置连接，否则错位缓冲会被继续复用。"""
        provider = BaostockProvider(min_interval=0)
        resets = []

        def fake_session():
            from contextlib import nullcontext

            return nullcontext(provider)

        monkeypatch.setattr(provider, "session", fake_session)
        monkeypatch.setattr(provider, "_hard_close", lambda: resets.append(1))

        def boom(*a, **k):
            raise ProviderError("query_history_k_data_plus 失败：网络接收错误。")

        with pytest.raises(ProviderError):
            provider._call("query_history_k_data_plus", boom)
        assert resets == [1]

    def test_call_keeps_connection_on_business_error(self, monkeypatch):
        provider = BaostockProvider(min_interval=0)
        resets = []

        def fake_session():
            from contextlib import nullcontext

            return nullcontext(provider)

        monkeypatch.setattr(provider, "session", fake_session)
        monkeypatch.setattr(provider, "_hard_close", lambda: resets.append(1))

        def boom(*a, **k):
            raise ProviderError("query_history_k_data_plus 失败：返回为空")

        with pytest.raises(ProviderError):
            provider._call("query_history_k_data_plus", boom)
        assert resets == []


class TestSessionDepth:
    def test_depth_is_zero_before_any_session(self):
        """threading.local 的属性只对赋值过的线程可见，读未赋值必须兜底为 0。"""
        provider = BaostockProvider(min_interval=0)
        assert provider._depth == 0

    def test_session_is_reentrant(self, monkeypatch):
        provider = BaostockProvider(min_interval=0)
        logins = []
        logouts = []
        monkeypatch.setattr(provider, "_login", lambda: logins.append(1))
        monkeypatch.setattr(provider, "_broken", False)

        import backend.core.providers.baostock_provider as module

        monkeypatch.setattr(module.bs, "logout", lambda: logouts.append(1))
        with provider.session():
            assert provider._depth == 1
            with provider.session():
                assert provider._depth == 2
            assert provider._depth == 1
        assert provider._depth == 0
        assert len(logins) == 1  # 只登录一次
        assert len(logouts) == 1
