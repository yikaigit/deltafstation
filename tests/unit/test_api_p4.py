"""P4 新增接口的契约测试：覆盖矩阵、单只补拉、个股 K 线与关键指标。

这些接口都是「给前端某一区块供数」，所以断言重点放在
**形状（前端能直接画）** 和 **口径（数据对不对）** 两件事上，
而不是逐字段比对实现细节。
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture()
def root(tmp_path, monkeypatch):
    """把 get_config() 的数据根指向 tmp_path。

    ⚠️ 必须让 client 和 lake 共用**同一个** root：conftest 里的 ``cfg`` 夹具
    用的是它自己的 tmp_path，与这里不是同一个目录 —— 早期测试文件把
    ``cfg.paths.lake.parent`` 当根传给 seed，写进去的数据和接口读的目录
    根本不是一处，于是「数据造好了但接口返回空」。这里用一个夹具统一。
    """
    import backend.core.config as config_mod

    real_load = config_mod.load_config
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda path=None, root=tmp_path: real_load(path=path, root=tmp_path),
    )
    config_mod.get_config.cache_clear()
    yield tmp_path
    config_mod.get_config.cache_clear()


@pytest.fixture()
def client(root):
    from backend.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def body(res):
    payload = res.get_json()
    assert isinstance(payload, dict)
    assert "ok" in payload
    return payload


#: 行情样本的日期：与打分快照日（SNAP_DATE）必须对齐 ——
#: K 线接口以快照日为终点往回取 120 个交易日，若行情停在更早的年份，
#: 接口会老老实实返回空数组（这是正确行为，不是 bug）。
#: 早期把样本放在 2024/2025 而快照在 2026，测试就一直在验「空数组」。
BAR_DATES = ("2026-01-02", "2026-01-05", "2026-01-06", "2026-10-15", "2026-10-16")
SNAP_DATE = "2026-10-16"


def seed_bars() -> None:
    """两个标的、跨两年、含一天的缺口，用于覆盖矩阵与 K 线。"""
    from backend.core.lake import get_lake

    lake = get_lake()
    lake.write(
        "bars",
        pd.DataFrame(
            {
                "date": pd.to_datetime(
                    [
                        BAR_DATES[0], BAR_DATES[1], BAR_DATES[2],
                        BAR_DATES[3], BAR_DATES[4],
                        BAR_DATES[0], BAR_DATES[3],
                    ]
                ),
                "code": [
                    "sh.600000", "sh.600000", "sh.600000",
                    "sh.600000", "sh.600000",
                    "sz.000001", "sz.000001",
                ],
                # 不复权价：3 → 6 是「1 拆 2」，配合 2 倍因子后前复权应无跳空
                "open": [9.9, 10.0, 10.1, 5.1, 5.2, 20.0, 21.0],
                "high": [10.2, 10.3, 10.4, 5.3, 5.4, 20.5, 21.5],
                "low": [9.8, 9.9, 10.0, 5.0, 5.1, 19.8, 20.8],
                "close": [10.0, 10.1, 10.2, 5.2, 5.3, 20.2, 21.2],
                "volume": [1000, 1100, 1200, 5000, 5100, 800, 900],
            }
        ),
    )
    lake.write(
        "universe",
        pd.DataFrame(
            {
                "code": ["sh.600000", "sz.000001"],
                "name": ["浦发银行", "平安银行"],
                "board": ["main", "main"],
                "industry_l1": ["银行", "银行"],
            }
        ),
    )
    lake.write(
        "adj_factor",
        pd.DataFrame(
            {
                "code": ["sh.600000", "sh.600000", "sz.000001"],
                "ex_date": pd.to_datetime(["1999-11-10", "2026-06-30", "1999-11-10"]),
                # 2026-06-30 起因子 1 → 2：等价于「1 拆 2」，拆股前价格对半折到前复权
                "back_factor": [1.0, 2.0, 1.0],
            }
        ),
    )


class TestCoverage:
    def test_empty_lake(self, client):
        data = body(client.get("/api/data/coverage"))["data"]
        assert data["years"] == []
        assert data["symbols"] == []
        assert "note" in data

    def test_matrix_shape_and_counts(self, client, root):
        seed_bars()
        data = body(client.get("/api/data/coverage"))["data"]
        assert data["years"] == ["2026"]
        codes = {s["code"]: s for s in data["symbols"]}
        assert set(codes) == {"sh.600000", "sz.000001"}
        # sh.600000：2026 全年 5 个交易日
        assert data["matrix"]["sh.600000"]["2026"] == 5
        # sz.000001：只有 2 天
        assert data["matrix"]["sz.000001"]["2026"] == 2
        assert codes["sh.600000"]["total_days"] == 5
        assert codes["sh.600000"]["name"] == "浦发银行"

    def test_year_capacity_present(self, client, root):
        seed_bars()
        data = body(client.get("/api/data/coverage"))["data"]
        # 没有日历文件时退化成本年最大覆盖天数，不能是 0
        assert data["year_days"]["2026"] >= 5


class TestFillSymbol:
    def test_missing_code_rejected(self, client):
        res = client.post("/api/data/fill", json={})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "missing_code"

    def test_unknown_code_rejected(self, client, root):
        seed_bars()
        res = client.post("/api/data/fill", json={"code": "sh.999999"})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "unknown_code"

    def test_known_code_creates_job(self, client, root, monkeypatch):
        seed_bars()

        captured = {}

        class FakeRunner:
            def submit(self, kind, params=None):
                captured["kind"] = kind
                captured["params"] = params
                return "update_fake_1"

        import backend.core.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "get_runner", lambda: FakeRunner())
        res = client.post("/api/data/fill", json={"code": "sh.600000", "start": "2024-01-01"})
        assert res.status_code == 201
        data = body(res)["data"]
        assert data["job_id"] == "update_fake_1"
        assert captured["kind"] == "update"
        assert captured["params"] == {"code": "sh.600000", "start": "2024-01-01"}


class TestQualityExport:
    def test_no_report_is_404(self, client):
        res = client.get("/api/data/quality/export")
        assert res.status_code == 404
        assert body(res)["error"]["code"] == "no_report"

    def test_downloads_latest_report(self, client, root):
        from backend.core.config import get_config

        folder = Path(get_config().paths.quality)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "report_20260101.json").write_text('{"trade_date": "2026-01-01"}', encoding="utf-8")
        (folder / "report_20261016.json").write_text('{"trade_date": "2026-10-16"}', encoding="utf-8")

        res = client.get("/api/data/quality/export")
        assert res.status_code == 200
        # 取最新一份（按文件名排序的最后一个）
        assert json.loads(res.data)["trade_date"] == "2026-10-16"
        assert "report_20261016.json" in res.headers["Content-Disposition"]


class TestScoreDetailExtras:
    def _seed(self):
        from backend.core.snapshots import get_score_store

        get_score_store().write(
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-10-15", SNAP_DATE]),
                    "code": ["sh.600000", "sh.600000"],
                    "score_pct": [70.0, 95.0],
                    "grade": ["看多", "强烈看多"],
                    "dim_trend": [60.0, 90.0],
                    "dim_momentum": [55.0, 88.0],
                }
            ),
            version="v1.0.0",
        )

    def test_kline_session_shape(self, client, root):
        self._seed()
        seed_bars()
        data = body(client.get("/api/scores/sh.600000"))["data"]
        kl = data["kline"]
        assert len(kl) == 5  # 只返回 sh.600000 的 5 个交易日，且不越过快照日
        assert set(kl[0]) == {"date", "open", "high", "low", "close", "volume"}
        assert kl[-1]["date"] == SNAP_DATE
        # 全部是正数价格（null 只应出现在停牌日，这里没有）
        assert all(kl[i]["close"] and kl[i]["close"] > 0 for i in range(len(kl)))

    def test_kline_is_forward_adjusted(self, client, root):
        """前复权：``价格 × f(t)/f(最新)``。

        样本里 2026-06-30 因子从 1 变 2，最新日（2026-10-16）因子仍是 2，
        所以拆股前的价格要整体乘 0.5 —— 这样「10.2 元拆成 5.1 元」的
        跳空在 K 线上被抹平，画出来是连续的。
        """
        self._seed()
        seed_bars()
        data = body(client.get("/api/scores/sh.600000"))["data"]
        kl = {row["date"]: row for row in data["kline"]}
        # 2026-01-06 收盘 10.2，f=1；latest f=2 → 10.2 × 0.5 = 5.1
        assert kl["2026-01-06"]["close"] == pytest.approx(5.1, abs=1e-6)
        assert kl["2026-01-06"]["high"] == pytest.approx(10.4 * 0.5, abs=1e-6)
        # 2026-10-16 收盘 5.3，f=2；×2/2 = 5.3（最新日不缩放）
        assert kl[SNAP_DATE]["close"] == pytest.approx(5.3, abs=1e-6)
        # 拆股后第一天 5.2 与前一日前复权价 5.1 连续，没有腰斩跳空
        assert kl["2026-10-15"]["close"] == pytest.approx(5.2, abs=1e-6)

    def test_kline_requires_adj_factor_gracefully(self, client, root):
        """没有复权因子表时回退成不复权，而不是报错或返回空。"""
        self._seed()
        seed_bars()
        import shutil

        from backend.core.lake import get_lake

        lake = get_lake()
        # 整个数据集重置成「只有别家票的因子」，这样查 sh.600000 返回空表
        shutil.rmtree(lake.dataset_path("adj_factor"))
        lake.write(
            "adj_factor",
            pd.DataFrame(
                {
                    "code": ["sz.000001"],
                    "ex_date": pd.to_datetime(["1999-11-10"]),
                    "back_factor": [1.0],
                }
            ),
        )
        data = body(client.get("/api/scores/sh.600000"))["data"]
        kl = {row["date"]: row for row in data["kline"]}
        assert kl["2026-01-06"]["close"] == pytest.approx(10.2, abs=1e-6)

    def test_indicators_block_present(self, client, root):
        self._seed()
        seed_bars()
        data = body(client.get("/api/scores/sh.600000"))["data"]
        ind = data["indicators"]
        assert isinstance(ind, dict)
        # atr 只有 3 个交易日，样本不足 5 天 → 不应出现，而不是给个错值
        assert "atr" not in ind

    def test_missing_market_data_degrades_gracefully(self, client):
        """只有打分快照、没有行情/估值/财务时，接口仍要返回 200。"""
        self._seed()
        data = body(client.get("/api/scores/sh.600000"))["data"]
        assert data["kline"] == []
        assert isinstance(data["indicators"], dict)
