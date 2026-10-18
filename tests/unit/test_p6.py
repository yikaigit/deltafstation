"""P6 测试：收盘后补齐（缺口体检 → 补缺 → 判到位 → 打分 → 自检）。

最重要的两个断言：
1. **缺口能被发现** —— 这是 `update_daily` 的盲区（它只对齐今天），
   如果体检也漏掉，那么「停三天再回来」就永远补不上了。
2. **数据没到位时必须报 pending** —— 不能把 baostock 还没出数据
   （返回 0 行而不是报错）当成「成功」，否则用户以为今天已经好了。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core import catchup as cu


@pytest.fixture()
def lake(tmp_path):
    from backend.core.config import load_config
    from backend.core.lake import Lake

    cfg = load_config(root=tmp_path)
    cfg.paths.ensure()
    return Lake(cfg)


def _write_bars(lake, dates, codes, close=10.0):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(pd.to_datetime(dates), len(codes))),
            "code": list(codes) * len(dates),
            "open": close, "high": close, "low": close, "close": close,
            "volume": 1000, "tradestatus": 1,
        }
    )
    lake.write("bars", frame)
    return frame


def _write_index(lake, dates, codes=("sh.000300",)):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(pd.to_datetime(dates), len(codes))),
            "code": list(codes) * len(dates),
            "open": 3000.0, "high": 3000.0, "low": 3000.0, "close": 3000.0,
            "volume": 1e6,
        }
    )
    lake.write("index_bars", frame)


def _write_valuation(lake, dates, codes):
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(np.repeat(pd.to_datetime(dates), len(codes))),
            "code": list(codes) * len(dates),
            "close": 10.0, "pe_ttm": 15.0, "pb_mrq": 2.0, "ps_ttm": 3.0,
            "peg": 1.0, "total_mv": 1e10, "float_mv": 1e10,
        }
    )
    lake.write("valuation", frame)


CODES = ["sh.600000", "sh.600009", "sz.000001"]


class TestGapReport:
    def test_complete_when_data_present(self, lake):
        # 用真实日期范围，避免日历边界问题
        days = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]
        _write_bars(lake, days, CODES)
        _write_index(lake, days)
        _write_valuation(lake, days, CODES)

        report = cu.gap_report(lake=lake, as_of="2026-10-16", lookback_days=5)
        assert report.complete is True
        assert report.days_to_fill == []
        assert report.target == "2026-10-16"

    def test_detects_missing_days(self, lake):
        """中间缺 3 天必须被发现 —— 这是 update_daily 的盲区。"""
        all_days = ["2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14"]
        present = ["2026-10-08", "2026-10-14"]  # 中间三天没有
        _write_bars(lake, present, CODES)
        _write_index(lake, present)
        _write_valuation(lake, present, CODES)

        report = cu.gap_report(lake=lake, as_of="2026-10-14", lookback_days=10)
        assert report.complete is False
        expected = ["2026-10-09", "2026-10-12", "2026-10-13"]
        assert [d for d in report.days_to_fill if d in expected] == expected
        assert report.datasets["bars"].missing_count >= 3

    def test_holidays_are_not_gaps(self, lake):
        """国庆假期（10-01~10-07）不是缺口，不能被算进去。

        ⚠️ 回看窗口必须正好只覆盖「9-30 与 10-08/09」—— 窗口开大了会回看到
        9 月中旬那片测试根本没造数据的日期，那些「缺口」是真实的，
        但与被测的假期判断无关（早期写成 lookback=10 就是这么误报的）。
        """
        days = ["2026-09-30", "2026-10-08", "2026-10-09"]
        _write_bars(lake, days, CODES)
        _write_index(lake, days)
        _write_valuation(lake, days, CODES)

        report = cu.gap_report(lake=lake, as_of="2026-10-09", lookback_days=2)
        assert report.complete is True, report.days_to_fill

    def test_weekend_as_of_uses_last_trading_day(self, lake):
        """周六打开页面时，目标日应是周五，而不是报「缺 2 天」。"""
        days = ["2026-10-14", "2026-10-15", "2026-10-16"]
        _write_bars(lake, days, CODES)
        _write_index(lake, days)
        _write_valuation(lake, days, CODES)

        report = cu.gap_report(lake=lake, as_of="2026-10-17", lookback_days=3)  # 周六
        assert report.target == "2026-10-16"
        assert report.complete is True

    def test_per_dataset_gap_isolation(self, lake):
        """只有估值缺，也要单独报出来（行情齐但估值缺会拖低估值维度）。"""
        days = ["2026-10-15", "2026-10-16"]
        _write_bars(lake, days, CODES)
        _write_index(lake, days)
        _write_valuation(lake, ["2026-10-15"], CODES)  # 估值少一天

        report = cu.gap_report(lake=lake, as_of="2026-10-16", lookback_days=2)
        assert report.datasets["bars"].missing_count == 0
        assert "2026-10-16" in report.datasets["valuation"].missing_days
        assert "2026-10-16" in report.days_to_fill


class TestLandedReport:
    def test_pending_when_no_bars(self, lake):
        """今天没行情 → ok=False，调用方必须报 pending。"""
        out = cu.landed_report(lake, "2026-10-16", CODES)
        assert out["ok"] is False
        assert out["bars"] == 0

    def test_pending_when_partial_bars(self, lake):
        """只有 1/3 的票有数据 → 不算到位（阈值 80%）。"""
        _write_bars(lake, ["2026-10-16"], ["sh.600000"])
        _write_index(lake, ["2026-10-16"])
        out = cu.landed_report(lake, "2026-10-16", CODES)
        assert out["bars"] == 1
        assert out["ok"] is False

    def test_ok_when_enough_bars(self, lake):
        _write_bars(lake, ["2026-10-16"], CODES)
        _write_index(lake, ["2026-10-16"])
        _write_valuation(lake, ["2026-10-16"], CODES)
        out = cu.landed_report(lake, "2026-10-16", CODES)
        assert out["ok"] is True
        assert out["bars_ratio"] == pytest.approx(1.0)
        assert out["valuation_ok"] is True
        assert out["warning"] is None

    def test_missing_index_blocks_ok(self, lake):
        """行情齐但指数缺 → 仍判未到位（rs_60 与超额都依赖指数）。"""
        _write_bars(lake, ["2026-10-16"], CODES)
        out = cu.landed_report(lake, "2026-10-16", CODES)
        assert out["bars_ok"] is True
        assert out["index_ok"] is False
        assert out["ok"] is False

    def test_valuation_shortage_is_warning_not_blocker(self, lake):
        """估值缺只是 warning：打分可以用前值，不该卡住整个流程。"""
        _write_bars(lake, ["2026-10-16"], CODES)
        _write_index(lake, ["2026-10-16"])
        out = cu.landed_report(lake, "2026-10-16", CODES)
        assert out["ok"] is True
        assert out["valuation_ok"] is False
        assert "估值" in (out["warning"] or "")


class TestCatchUp:
    """端到端：用假的 ingest / scorer 替换掉真实网络与重计算。"""

    def _patch(self, monkeypatch, bars_after_fill=True):
        from backend.core import ingest as ingest_mod
        from backend.core import scorer as scorer_mod

        calls = {"bars": [], "index": [], "valuation": [], "factors": 0, "score": []}
        state = {"filled": False}

        class FakeUniverse:
            def codes(self, **kwargs):
                return list(CODES)

        class FakeIngest:
            universe = FakeUniverse()

            def backfill_bars(self, codes, start, end=None, progress=None, incremental=True):
                calls["bars"].append((start, end))
                state["filled"] = bars_after_fill
                return {"rows": 3}

            def backfill_index(self, codes=None, start="", end=None, progress=None):
                calls["index"].append((start, end))
                return {"rows": 1}

            def refresh_valuation(self, day, codes=None, progress=None):
                calls["valuation"].append(day)
                return {"rows": 3}

            def backfill_factors(self, codes, progress=None):
                calls["factors"] += 1
                return {"rows": 1}

        class FakeScorer:
            def score_day(self, trade_date=None, version=None, write=True):
                calls["score"].append(trade_date)
                return {"trade_date": trade_date, "version": version or "v2.0.0"}

        monkeypatch.setattr(ingest_mod, "get_ingest", lambda: FakeIngest())
        monkeypatch.setattr(scorer_mod, "get_scorer", lambda: FakeScorer())
        return calls, state

    def test_complete_skips_network(self, lake, monkeypatch):
        """数据已齐时一次网络请求都不该发。"""
        days = ["2026-10-15", "2026-10-16"]
        _write_bars(lake, days, CODES)
        _write_index(lake, days)
        _write_valuation(lake, days, CODES)

        calls, _ = self._patch(monkeypatch)
        out = cu.catch_up(lake=lake, as_of="2026-10-16", lookback_days=2, score=False)

        assert out["status"] == "complete"
        assert calls["bars"] == []
        # 复权因子也是最贵的调用（300 次往返），数据齐时同样跳过
        assert calls["factors"] == 0

    def test_fills_missing_days(self, lake, monkeypatch):
        days_present = ["2026-10-15"]
        _write_bars(lake, days_present, CODES)
        _write_index(lake, days_present)
        _write_valuation(lake, days_present, CODES)

        calls, _ = self._patch(monkeypatch, bars_after_fill=True)
        # 补完后目标日仍缺（fake 不会真的写数据），所以应当是 pending
        out = cu.catch_up(lake=lake, as_of="2026-10-16", lookback_days=2, score=False)

        assert calls["bars"], "应该尝试补缺"
        assert out["filled_days"]
        # 到位判定读的是真实数据湖 → 目标日依然没有 → pending
        assert out["status"] in ("pending", "partial")
        assert "稍后重试" in out["message"]

    def test_pending_does_not_score(self, lake, monkeypatch):
        """数据没到位时不能打分 —— 否则会把半天的数据算成正式分数。"""
        _write_bars(lake, ["2026-10-15"], CODES)
        _write_index(lake, ["2026-10-15"])
        _write_valuation(lake, ["2026-10-15"], CODES)

        calls, _ = self._patch(monkeypatch)
        cu.catch_up(lake=lake, as_of="2026-10-16", lookback_days=2, score=True)
        assert calls["score"] == []

    def test_scores_every_filled_day(self, lake, monkeypatch):
        """补了 3 天就要打 3 天的分，不能只打最后一天。"""
        _write_bars(lake, ["2026-10-16"], CODES)
        _write_index(lake, ["2026-10-16"])
        _write_valuation(lake, ["2026-10-16"], CODES)

        calls, _ = self._patch(monkeypatch)
        out = cu.catch_up(lake=lake, as_of="2026-10-16", lookback_days=6, score=True)

        filled = out["filled_days"]
        assert len(filled) >= 2
        assert calls["score"] == filled


class TestSelfCheck:
    def _write_snapshot(self, days, dims=None, quality=1.0):
        from backend.core.snapshots import get_score_store

        frames = []
        for day in days:
            for code in CODES:
                row = {
                    "trade_date": pd.Timestamp(day),
                    "code": code,
                    "score_pct": 50.0,
                    "score_abs": 50.0,
                    "grade": "中性",
                    "data_quality": quality,
                }
                for key, value in (dims or {}).items():
                    row[f"dim_{key}"] = value
                frames.append(row)
        get_score_store().write(pd.DataFrame(frames), version="v2.0.0")

    def test_ok_when_all_dimensions_covered(self, lake):
        self._write_snapshot(
            ["2026-10-16"],
            dims={"trend": 50.0, "momentum": 50.0, "volume": 50.0,
                  "risk": 50.0, "valuation": 50.0, "quality": 50.0},
        )
        out = cu.self_check(lake, "2026-10-16", version="v2.0.0")
        assert out["ok"] is True
        assert out["count"] == 3
        assert out["weak_dimensions"] == []

    def test_flags_weak_dimension(self, lake):
        """估值维度整片 NaN → 必须被标出来，而不是「看起来正常」。"""
        self._write_snapshot(
            ["2026-10-16"],
            dims={"trend": 50.0, "valuation": float("nan")},
            quality=0.83,
        )
        out = cu.self_check(lake, "2026-10-16", version="v2.0.0")
        assert out["ok"] is False
        assert "valuation" in out["weak_dimensions"]
        assert "估值" in out["note"] or "valuation" in out["note"]

    def test_missing_snapshot(self, lake):
        out = cu.self_check(lake, "2020-01-01", version="v2.0.0")
        assert out["ok"] is False


class TestCatchupAPI:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        import backend.core.config as config_mod

        real_load = config_mod.load_config
        monkeypatch.setattr(
            config_mod, "load_config",
            lambda path=None, root=tmp_path: real_load(path=path, root=tmp_path),
        )
        config_mod.get_config.cache_clear()

        from backend.app import create_app

        app = create_app()
        app.config.update(TESTING=True)
        with app.test_client() as c:
            yield c
        config_mod.get_config.cache_clear()

    def test_status_shape(self, client):
        data = client.get("/api/catchup/status").get_json()["data"]
        assert "gap" in data
        assert "target" in data["gap"]
        assert "score" in data
        assert "last_run" in data

    def test_run_dedupes_running_job(self, client, monkeypatch):
        """已有 catchup 在跑时不排第二个队列。"""
        import backend.api.catchup_api as api

        class FakeDB:
            def list_jobs(self, limit=20):
                return [{"id": "catchup_x", "kind": "catchup", "status": "running"}]

        monkeypatch.setattr(
            "backend.core.meta_db.get_meta_db", lambda: FakeDB()
        )
        res = client.post("/api/catchup/run", json={"score": True})
        assert res.status_code == 200
        payload = res.get_json()["data"]
        assert payload["already_running"] is True
        assert payload["job_id"] == "catchup_x"

    def test_run_creates_job(self, client, monkeypatch):
        class FakeRunner:
            def submit(self, kind, params=None):
                return f"{kind}_fake"

        monkeypatch.setattr("backend.core.jobs.get_runner", lambda: FakeRunner())
        res = client.post("/api/catchup/run", json={"score": False, "lookback_days": 10})
        assert res.status_code == 201
        data = res.get_json()["data"]
        assert data["job_id"] == "catchup_fake"
        assert data["params"]["score"] is False
        assert data["params"]["lookback_days"] == 10

    def test_invalid_lookback_rejected(self, client, monkeypatch):
        class FakeRunner:
            def submit(self, kind, params=None):
                return "x"

        monkeypatch.setattr("backend.core.jobs.get_runner", lambda: FakeRunner())
        res = client.post("/api/catchup/run", json={"lookback_days": "abc"})
        assert res.status_code == 400
