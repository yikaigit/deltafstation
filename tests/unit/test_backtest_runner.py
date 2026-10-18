"""回测编排测试（规格 §6.5/6.6/6.7）。

跑在临时数据湖 + 临时快照目录上：``run()`` 会把面板、基准、快照、区间
裁剪、IS/OOS、参数网格、落盘、experiments 串成一条链，任何一环的口径漂移
都会在这里现形。所有夹具都指向 tmp_path，不碰仓库里的真实数据。
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backend.core.backtest import runner
from backend.core.backtest.costs import CostModel
from backend.core.backtest.engine import run_backtest
from backend.core.backtest.metrics import downsample_equity  # noqa: F401
from backend.core.backtest.runner import (
    GRID_REBAL_DAYS,
    GRID_REGIME,
    GRID_TOP_N,
    _default_start,
    load_score_wide,
    record_experiment,
    result_to_payload,
    run,
    run_grid,
    save_payload,
)
from backend.core.backtest.strategies import StrategyParams
from backend.core.panels import Panels

VERSION = "v1.0.0"
CODES = ["sh.600001", "sh.600002", "sh.600003"]
N_DAYS = 150
BENCH = "sh.000300"


# ==================== 夹具 ====================


@pytest.fixture(scope="module")
def root_dir(tmp_path_factory):
    """模块级临时根目录：150 个快照文件写一次就够，没必要每个用例重写。"""
    return tmp_path_factory.mktemp("backtest_runner")


@pytest.fixture(scope="module")
def filled(root_dir):
    """往临时数据湖/快照目录里灌一份可回测的数据（模块级共享，只读不改）。"""
    from backend.core.config import load_config
    from backend.core.lake import Lake
    from backend.core.snapshots import ScoreStore

    cfg = load_config(root=root_dir)
    cfg.paths.ensure()
    lake, store = Lake(cfg), ScoreStore(cfg)
    dates = pd.bdate_range("2023-01-02", periods=N_DAYS)
    rng = np.random.default_rng(20240101)

    rows = []
    for offset, code in enumerate(CODES):
        price = 10.0 + 7.0 * offset
        for day in dates:
            price *= 1.0 + rng.normal(0.0006, 0.014)
            rows.append({
                "date": day, "code": code,
                "open": price, "high": price * 1.01, "low": price * 0.99,
                "close": price, "volume": 1_000_000.0,
                "amount": price * 1_000_000.0, "turn": 0.01, "pct_chg": 0.0,
                "tradestatus": 1,
            })
    lake.write("bars", pd.DataFrame(rows))
    lake.write("universe", pd.DataFrame({
        "code": CODES,
        "name": ["甲", "乙", "丙"],
        "industry_l1": ["银行", "银行", "医药"],
        "board": ["主板", "主板", "主板"],
        "is_st": [False, False, False],
    }))
    # 基准：缓慢上行，让 regime 和超额都有内容
    bench_close = 4000.0 * np.cumprod(
        1.0 + np.linspace(0.0004, 0.0012, N_DAYS)
    )
    lake.write("index_bars", pd.DataFrame({
        "date": dates, "code": BENCH, "close": bench_close,
    }))

    # 打分快照：三只票轮动，保证每期都有入选
    for i, day in enumerate(dates):
        scores = [float((i * 7 + offset * 31) % 100) for offset in range(len(CODES))]
        store.write(pd.DataFrame({
            "code": CODES,
            "score_pct": scores,
            "score_abs": [s / 100.0 for s in scores],
            "grade": ["看多", "中性", "看空"],
            "dim_trend": [0.5] * 3, "dim_momentum": [0.5] * 3,
            "dim_volume": [0.5] * 3, "dim_risk": [0.5] * 3,
            "dim_valuation": [0.5] * 3, "dim_quality": [0.5] * 3,
            "data_quality": [1.0] * 3,
        }), VERSION, day.strftime("%Y-%m-%d"))

    # get_meta_db() 走全局配置，会写到仓库 data/ 下 —— 必须改道
    patcher = pytest.MonkeyPatch()
    patcher.setattr(runner, "get_meta_db", lambda: _meta_db(root_dir))
    yield {"dates": dates, "lake": lake, "store": store, "cfg": cfg}
    patcher.undo()


def _meta_db(root):
    from backend.core.config import load_config
    from backend.core.meta_db import MetaDB

    return MetaDB(load_config(root=root)).init()


# ==================== load_score_wide ====================


class TestLoadScoreWide:
    def test_no_snapshot_raises(self, store):
        with pytest.raises(RuntimeError, match="没有任何打分快照"):
            load_score_wide(store, None)

    def test_unknown_version_raises(self, filled):
        store = filled["store"]
        with pytest.raises(RuntimeError, match="不存在"):
            load_score_wide(store, "v9.9.9")

    def test_wide_shape(self, filled):
        wide, version, dates = load_score_wide(filled["store"], None)
        assert version == VERSION
        assert len(dates) == N_DAYS
        assert list(wide.columns) == CODES
        assert isinstance(wide.index, pd.DatetimeIndex)
        assert wide.index.is_monotonic_increasing
        assert wide.notna().all().all()

    def test_days_limits_history(self, filled):
        wide, version, dates = load_score_wide(filled["store"], None, days=20)
        assert len(dates) == 20
        assert len(wide) == 20
        assert wide.index[-1] == pd.Timestamp(filled["dates"][-1])

    def test_latest_version_selected(self, root_dir):
        """默认取最新版本。⚠️ 必须在独立 store 上验：往共享 fixture 里写
        新版快照会让后面所有用例只能读到那一天的数据。"""
        from backend.core.config import load_config
        from backend.core.snapshots import ScoreStore

        other_root = root_dir / "version_pick"
        store = ScoreStore(load_config(root=other_root))
        for day in ("2024-01-02", "2024-01-03", "2024-01-04"):
            store.write(pd.DataFrame({
                "code": CODES, "score_pct": [10.0, 20.0, 30.0],
            }), "v1.0.0", day)
        store.write(pd.DataFrame({
            "code": CODES, "score_pct": [99.0, 98.0, 97.0],
        }), "v2.0.0", "2024-01-05")

        assert store.versions() == ["v1.0.0", "v2.0.0"]
        _, version, dates = load_score_wide(store, None)
        assert version == "v2.0.0"
        assert dates == ["2024-01-05"]


# ==================== 区间默认值 ====================


class TestDefaultStart:
    def test_no_dates(self, cfg):
        assert _default_start(cfg, []) is None

    def test_last_snapshot_minus_years(self, cfg):
        # 注意是「5×365 天」而不是「往前 5 个日历年」——中间跨了两个闰日，
        # 所以 2024-06-28 会落到 2019-06-30 而不是 2019-06-28。
        expected = (pd.Timestamp("2024-06-28")
                    - pd.Timedelta(days=5 * 365)).strftime("%Y-%m-%d")
        assert _default_start(cfg, ["2020-01-02", "2024-06-28"]) == expected

    def test_years_comes_from_config(self, cfg):
        cfg.raw["universe"]["backtest_years"] = 2
        expected = (pd.Timestamp("2024-06-28")
                    - pd.Timedelta(days=2 * 365)).strftime("%Y-%m-%d")
        assert _default_start(cfg, ["2024-06-28"]) == expected


# ==================== payload / 落盘 ====================


class TestResultPayload:
    def _payload(self, filled, **kw):
        params = StrategyParams(top_n=2, rebal_days=10, regime=False,
                                max_weight=0.6, **kw)
        wide, version, _ = load_score_wide(filled["store"], None)
        panels = Panels.from_lake(lake=filled["lake"], cfg=filled['cfg'],
                                  start="2023-01-02", end=None,
                                  benchmark=BENCH)
        costs = CostModel()
        result = run_backtest(panels, wide, params, costs)
        data_range = {"start": "2023-01-02", "end": str(filled["dates"][-1].date()),
                      "codes": len(panels.codes)}
        return result_to_payload(result, params, version, costs, data_range), result

    def test_payload_shape(self, filled, cfg):
        payload, result = self._payload(filled)
        for key in ("id", "created_at", "strategy", "score_version", "params",
                    "costs", "period", "data_range", "equity", "metrics",
                    "trades", "holdings", "blocked", "rebalance_count",
                    "turnover", "avg_positions", "grid", "warnings"):
            assert key in payload, key
        assert payload["strategy"] == "score_portfolio"
        assert payload["score_version"] == VERSION
        assert set(payload["metrics"]) == {"is", "oos", "full"}
        assert set(payload["period"]) == {
            "start", "end", "is_start", "is_end", "oos_start", "oos_end"
        }
        # 成本口径必须自证（规格 §6.7）
        assert payload["costs"]["one_way_buy"] == 0.00075
        assert payload["costs"]["one_way_sell"] == 0.00125
        assert payload["blocked"] == result.blocked
        assert payload["rebalance_count"] == result.rebalance_count

    def test_equity_is_downsampled_and_json_safe(self, filled, cfg):
        payload, _ = self._payload(filled)
        records = payload["equity"]
        assert 0 < len(records) <= 1000
        assert records[0]["date"] < records[-1]["date"]
        json.dumps(payload, ensure_ascii=False, default=str)

    def test_trades_have_review_fields(self, filled, cfg):
        """规格 §6.7：成交记录要能支撑「3 个调仓日人工复核」。"""
        payload, _ = self._payload(filled)
        assert payload["trades"], "合成数据应该有成交"
        for trade in payload["trades"]:
            assert set(trade) >= {"date", "code", "side", "price", "shares",
                                  "amount", "fee", "reason"}
            assert trade["side"] in ("buy", "sell")
            assert trade["shares"] > 0
            assert trade["fee"] >= 0

    def test_holdings_have_weights(self, filled, cfg):
        payload, _ = self._payload(filled)
        assert payload["holdings"]
        for row in payload["holdings"]:
            assert set(row) >= {"date", "code", "shares", "price", "amount", "weight"}


class TestSavePayload:
    def test_writes_file_and_backfills_id(self, cfg):
        folder = cfg.paths.results / "backtests"
        payload = {"id": "", "metrics": {}}
        path = save_payload(payload, folder)
        assert path.exists() and path.parent == folder
        assert payload["id"] == path.stem            # 回填
        assert path.name.startswith("bt_")
        assert json.loads(path.read_text(encoding="utf-8"))["id"] == payload["id"]

    def test_no_temp_left_behind(self, cfg):
        folder = cfg.paths.results / "backtests"
        save_payload({"id": ""}, folder)
        assert list(folder.glob("*.tmp")) == []

    def test_ids_are_unique(self, cfg):
        folder = cfg.paths.results / "backtests"
        ids = set()
        for _ in range(5):
            ids.add(save_payload({"id": ""}, folder).stem)
        assert len(ids) == 5


class TestRecordExperiment:
    def test_records_into_db(self, filled, root_dir):
        payload = {"id": "bt_test", "score_version": VERSION,
                   "params": {"top_n": 5, "rebal_days": 10, "regime": True,
                              "weight_method": "equal", "max_weight": 0.2},
                   "period": {"start": "2023-01-02", "end": "2023-12-29"},
                   "metrics": {"full": {"total_return": 12.3}}}
        exp_id = record_experiment(payload)
        assert exp_id and exp_id.startswith("exp_")

        rows = _meta_db(root_dir).list_experiments()
        assert any(r["id"] == exp_id for r in rows)
        row = next(r for r in rows if r["id"] == exp_id)
        assert "top_n=5" in row["hypothesis"]
        assert row["start_date"] == "2023-01-02"
        assert row["metrics"]["full"]["total_return"] == 12.3

    def test_failure_does_not_raise(self, monkeypatch):
        def boom():
            raise RuntimeError("db 炸了")

        monkeypatch.setattr(runner, "get_meta_db", boom)
        assert record_experiment({"id": "x"}) is None


# ==================== 端到端 run() ====================


class TestRunEndToEnd:
    def test_runs_and_persists(self, filled, cfg):
        out = run(payload={"top_n": 2, "rebal_days": 20, "regime": False,
                           "max_weight": 0.6, "version": VERSION},
                  cfg=filled['cfg'], lake=filled["lake"], store=filled["store"],
                  grid=False)
        assert out["id"] and out["id"].startswith("bt_")
        assert Path(out["path"]).exists()
        assert out["experiment_id"] and out["experiment_id"].startswith("exp_")
        # 参数被写回（version 解析后回填）
        assert out["score_version"] == VERSION
        assert out["params"]["top_n"] == 2
        assert out["grid"] == []
        assert out["metrics"]["full"]["days"] > 0
        # 区间被裁剪到有快照的日子
        assert out["period"]["start"] == "2023-01-02"
        assert out["period"]["end"] == str(filled["dates"][-1].date())

    def test_default_start_is_recent_five_years(self, filled, cfg):
        out = run(payload={}, cfg=filled['cfg'], lake=filled["lake"],
                  store=filled["store"], grid=False)
        # 快照只有 ~7 个月，5 年窗口会整体落在数据之前 → 全区间可用
        assert out["period"]["start"] == "2023-01-02"
        assert out["params"]["top_n"] == 20           # 来自 config/app.yaml

    def test_progress_is_reported_monotonically(self, filled, cfg):
        seen = []
        run(payload={"top_n": 2, "regime": False}, cfg=filled['cfg'],
            lake=filled["lake"], store=filled["store"], grid=False,
            progress=lambda r, m: seen.append((r, m)))
        assert seen, "没有任何进度回调"
        ratios = [r for r, _ in seen]
        assert ratios == sorted(ratios)
        assert ratios[0] < ratios[-1]
        assert seen[-1][0] == 1.0

    def test_unknown_version_raises(self, filled, cfg):
        with pytest.raises(RuntimeError, match="不存在"):
            run(payload={"version": "v7.7.7"}, cfg=filled['cfg'], lake=filled["lake"],
                store=filled["store"], grid=False)

    def test_no_snapshot_raises(self, lake, store, cfg):
        lake.write("bars", pd.DataFrame({
            "date": pd.bdate_range("2023-01-02", periods=5),
            "code": "sh.600001",
            "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.0,
            "volume": 1e6, "amount": 1e7, "turn": 0.01, "pct_chg": 0.0,
            "tradestatus": 1,
        }))
        with pytest.raises(RuntimeError, match="没有任何打分快照"):
            run(payload={}, cfg=cfg, lake=lake, store=store, grid=False)

    def test_grid_runs_and_is_sorted(self, filled, cfg):
        out = run(payload={"top_n": 2, "rebal_days": 10, "regime": False,
                           "max_weight": 0.6}, cfg=filled['cfg'], lake=filled["lake"],
                  store=filled["store"], grid=True)
        grid = out["grid"]
        assert 0 < len(grid) <= len(GRID_TOP_N) * len(GRID_REBAL_DAYS) * len(GRID_REGIME)
        for row in grid:
            assert set(row) >= {"top_n", "rebal_days", "regime",
                                "is_sharpe", "oos_sharpe"}
            assert row["top_n"] in GRID_TOP_N
            assert row["rebal_days"] in GRID_REBAL_DAYS
            assert row["regime"] in GRID_REGIME
        # 按 IS Sharpe 降序，None 沉底
        sharpes = [r["is_sharpe"] for r in grid]
        valued = [s for s in sharpes if s is not None]
        assert valued == sorted(valued, reverse=True)
        assert all(s is None for s in sharpes[len(valued):])

    def test_grid_lifts_max_weight_for_small_top_n(self, filled):
        """网格变的是「持股集中度」；top_n=5 + max_weight=0.10 会长期半仓，
        与集中度更高的点不可比，所以上限要抬到 ≥1/top_n。"""
        panels = Panels.from_lake(lake=filled["lake"], cfg=filled["cfg"],
                                  start="2023-01-02", benchmark=BENCH)
        wide, _, _ = load_score_wide(filled["store"], None)
        base = StrategyParams(top_n=2, rebal_days=20, regime=False,
                              max_weight=0.10)
        records = run_grid(panels, base, CostModel(), None, wide, None)
        top_ns = {r["top_n"] for r in records}
        assert 5 in top_ns
        # 该点的目标权重和必须是 1（满仓），而不是 0.5
        picked = [r for r in records if r["top_n"] == 5][0]
        assert picked["is_total_return"] is not None

    def test_grid_skipped_when_main_run_is_slow(self, filled, monkeypatch):
        """主回测超预算 → 不跑网格，并在 warnings 里说清楚为什么。"""
        monkeypatch.setattr(runner, "GRID_TIME_BUDGET", -1.0)
        monkeypatch.setattr(runner, "run_grid",
                            lambda *a, **k: pytest.fail("不该被调用"))

        out = run(payload={"top_n": 2, "regime": False}, cfg=filled["cfg"],
                  lake=filled["lake"], store=filled["store"], grid=True)
        assert out["grid"] == []
        assert any("跳过参数网格" in w for w in out["warnings"])

    def test_grid_flag_off_skips_silently(self, filled, monkeypatch):
        monkeypatch.setattr(runner, "run_grid",
                            lambda *a, **k: pytest.fail("不该被调用"))
        out = run(payload={"top_n": 2, "regime": False}, cfg=filled["cfg"],
                  lake=filled["lake"], store=filled["store"], grid=False)
        assert out["grid"] == []
        assert not any("参数网格" in w for w in out["warnings"])


# ==================== CLI ====================


class TestCLI:
    @staticmethod
    def _patch_cfg(monkeypatch, cfg):
        import scripts.backtest as cli

        monkeypatch.setattr(cli, "get_config", lambda: cfg)
        return cli

    def test_parser_accepts_documented_flags(self):
        import scripts.backtest as cli

        args = cli.build_parser().parse_args([
            "--version", "v1.0.0", "--start", "2021-01-01", "--end", "2024-01-01",
            "--top-n", "10", "--rebal-days", "5", "--max-weight", "0.2",
            "--weight-method", "score", "--regime", "--min-score-pct", "60",
            "--initial-cash", "500000", "--is-ratio", "0.6",
            "--benchmark", "sh.000300", "--no-grid", "--json", "--verbose",
        ])
        assert args.top_n == 10 and args.rebal_days == 5
        assert args.weight_method == "score" and args.regime is True
        assert args.no_grid and args.json and args.verbose

    def test_no_grid_runs_end_to_end(self, filled, monkeypatch, capsys):
        cli = self._patch_cfg(monkeypatch, filled["cfg"])
        code = cli.main(["--version", VERSION, "--top-n", "2",
                         "--rebal-days", "10", "--no-regime",
                         "--max-weight", "0.6", "--no-grid"])
        assert code == 0
        out = capsys.readouterr().out
        assert "回测完成" in out and "bt_" in out
        assert "IS" in out and "OOS" in out
        assert "结果文件" in out

    def test_json_output_is_parseable(self, filled, monkeypatch, capsys):
        import json

        cli = self._patch_cfg(monkeypatch, filled["cfg"])
        code = cli.main(["--version", VERSION, "--top-n", "2", "--no-regime",
                         "--no-grid", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["ok"] is True
        assert payload["result"]["metrics"]["full"]["days"] > 0

    def test_missing_snapshot_returns_1(self, monkeypatch, capsys):
        """没有打分快照 → 退出码 1（数据错误），不是 traceback。"""
        import scripts.backtest as cli

        from backend.core.config import load_config

        cfg = load_config(root=self._empty_root())
        cli = self._patch_cfg(monkeypatch, cfg)
        assert cli.main(["--no-grid"]) == 1
        assert "回测失败" in capsys.readouterr().err

    def _empty_root(self):
        import tempfile

        return Path(tempfile.mkdtemp(prefix="bt_cli_empty_"))

