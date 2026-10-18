"""API 契约测试：响应格式、状态码、无数据时的降级行为。

用 monkeypatch 把全局配置指向临时目录，确保不读写真实数据湖。
"""
from __future__ import annotations

import pandas as pd
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """把 get_config() 的数据根指向 tmp_path，返回 Flask 测试客户端。"""
    import backend.core.config as config_mod

    real_load = config_mod.load_config
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda path=None, root=tmp_path: real_load(path=path, root=tmp_path),
    )
    config_mod.get_config.cache_clear()

    from backend.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c

    config_mod.get_config.cache_clear()


def body(res):
    payload = res.get_json()
    assert isinstance(payload, dict), "响应必须是 JSON 对象"
    assert "ok" in payload, "响应必须带 ok 字段"
    return payload


class TestPages:
    @pytest.mark.parametrize("path", ["/", "/data", "/scores", "/backtest"])
    def test_page_renders(self, client, path):
        res = client.get(path)
        assert res.status_code == 200
        html = res.data.decode("utf-8")
        assert "<html" in html.lower()
        assert "DeltaFStation" in html

    def test_unknown_page_falls_back_to_index(self, client):
        res = client.get("/no-such-page")
        assert res.status_code == 404
        assert "<html" in res.data.decode("utf-8").lower()

    def test_static_css_served(self, client):
        res = client.get("/static/css/app.css")
        assert res.status_code == 200
        assert b"--primary" in res.data

    @pytest.mark.parametrize("name", ["common.js", "index.js", "data.js", "scores.js", "backtest.js"])
    def test_static_js_served(self, client, name):
        res = client.get(f"/static/js/{name}")
        assert res.status_code == 200
        assert len(res.data) > 500


class TestHealth:
    def test_shape(self, client):
        payload = body(client.get("/api/health"))
        assert payload["ok"] is True
        data = payload["data"]
        assert set(data) >= {"version", "datasets", "universe_count", "score", "schedule"}
        assert set(data["datasets"]) == {
            "bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"
        }
        assert data["schedule"]["update_time"] == "18:00"

    def test_empty_state(self, client):
        data = body(client.get("/api/health"))["data"]
        assert data["universe_count"] == 0
        assert all(v["exists"] is False for v in data["datasets"].values())
        assert data["score"]["versions"] == []
        assert data["score"]["latest_date"] is None


class TestUniverseAPI:
    def test_empty_returns_note(self, client):
        data = body(client.get("/api/universe"))["data"]
        assert data["items"] == []
        assert data["count"] == 0
        assert "note" in data

    def test_filters_after_seed(self, client):
        from backend.core.lake import get_lake

        get_lake().write(
            "universe",
            pd.DataFrame(
                {
                    "code": ["sh.600000", "sz.000001", "sh.601398"],
                    "name": ["浦发银行", "平安银行", "工商银行"],
                    "board": ["主板", "主板", "主板"],
                    "industry_l1": ["银行", "银行", "非银金融"],
                }
            ),
        )
        assert body(client.get("/api/universe"))["data"]["count"] == 3
        assert body(client.get("/api/universe?industry=银行"))["data"]["count"] == 2
        assert body(client.get("/api/universe?q=浦发"))["data"]["count"] == 1
        assert body(client.get("/api/universe?q=600000"))["data"]["count"] == 1

    def test_limit_applied(self, client):
        from backend.core.lake import get_lake

        get_lake().write(
            "universe",
            pd.DataFrame({"code": [f"sh.{i:06d}" for i in range(10)], "name": list("abcdefghij")}),
        )
        data = body(client.get("/api/universe?limit=3"))["data"]
        assert data["count"] == 3
        assert data["total"] == 10


class TestDataStatusAPI:
    def test_empty_state(self, client):
        data = body(client.get("/api/data/status"))["data"]
        assert set(data["datasets"]) == {
            "bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"
        }
        assert data["symbols"] == []
        assert data["symbol_count"] == 0

    def test_after_bars_written(self, client):
        from backend.core.lake import get_lake

        get_lake().write(
            "bars",
            pd.DataFrame(
                {
                    "date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                    "code": ["sh.600000", "sh.600000"],
                    "close": [10.0, 10.2],
                }
            ),
        )
        data = body(client.get("/api/data/status"))["data"]
        assert data["datasets"]["bars"]["exists"] is True
        assert data["datasets"]["bars"]["rows"] == 2
        assert data["datasets"]["bars"]["first_date"] == "2024-01-02"
        assert data["datasets"]["bars"]["last_date"] == "2024-01-03"
        assert data["symbol_count"] == 1
        assert data["symbols"][0]["code"] == "sh.600000"
        assert data["symbols"][0]["rows"] == 2

    def test_quality_without_report(self, client):
        data = body(client.get("/api/data/quality"))["data"]
        assert data["available"] is False


class TestScoreAPI:
    def test_empty_state(self, client):
        data = body(client.get("/api/scores"))["data"]
        assert data["items"] == []
        assert data["meta"]["count"] == 0
        assert "note" in data

    def test_industries_empty(self, client):
        data = body(client.get("/api/scores/industries"))["data"]
        assert data["items"] == []

    def test_detail_without_version_is_404(self, client):
        res = client.get("/api/scores/600519")
        assert res.status_code == 404
        assert body(res)["error"]["code"] == "no_version"

    def test_versions_empty(self, client):
        data = body(client.get("/api/score-versions"))["data"]
        assert data["items"] == []
        assert data["count"] == 0

    def test_list_after_snapshot(self, client):
        from backend.core.snapshots import get_score_store

        get_score_store().write(
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-10-16"] * 3),
                    "code": ["sh.600000", "sz.000001", "sh.601398"],
                    "score_pct": [95.0, 55.0, 12.0],
                    "score_abs": [80.0, 52.0, 20.0],
                    "grade": ["强烈看多", "中性", "强烈看空"],
                    "industry": ["银行", "银行", "非银金融"],
                }
            ),
            version="v1.0.0",
        )
        data = body(client.get("/api/scores"))["data"]
        assert data["meta"]["version"] == "v1.0.0"
        assert data["meta"]["date"] == "2026-10-16"
        assert data["meta"]["count"] == 3
        # 按综合分降序
        assert [i["code"] for i in data["items"]] == ["sh.600000", "sz.000001", "sh.601398"]
        assert data["meta"]["distribution"]["中性"] == 1

    def test_grade_filter(self, client):
        from backend.core.snapshots import get_score_store

        get_score_store().write(
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-10-16"] * 2),
                    "code": ["sh.600000", "sz.000001"],
                    "score_pct": [95.0, 55.0],
                    "grade": ["强烈看多", "中性"],
                }
            ),
            version="v1.0.0",
        )
        data = body(client.get("/api/scores?grade=中性"))["data"]
        assert data["meta"]["count"] == 1
        assert data["items"][0]["code"] == "sz.000001"

    def test_detail_after_snapshot(self, client):
        from backend.core.snapshots import get_score_store

        get_score_store().write(
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-10-15", "2026-10-16"]),
                    "code": ["sh.600000", "sh.600000"],
                    "score_pct": [70.0, 95.0],
                    "grade": ["看多", "强烈看多"],
                    "dim_trend": [60.0, 90.0],
                }
            ),
            version="v1.0.0",
        )
        data = body(client.get("/api/scores/sh.600000"))["data"]
        assert data["latest"]["score_pct"] == pytest.approx(95.0)
        assert len(data["history"]) == 2
        assert data["history"][-1]["date"] == "2026-10-16"
        assert data["version"] == "v1.0.0"

    def test_detail_unknown_code_is_404(self, client):
        from backend.core.snapshots import get_score_store

        get_score_store().write(
            pd.DataFrame(
                {
                    "trade_date": pd.to_datetime(["2026-10-16"]),
                    "code": ["sh.600000"],
                    "score_pct": [95.0],
                }
            ),
            version="v1.0.0",
        )
        res = client.get("/api/scores/sh.999999")
        assert res.status_code == 404
        # read_tail 按 code 过滤后整表为空 → no_score；有数据但缺该 code 才是 not_found
        assert body(res)["error"]["code"] == "no_score"


class FakeRunner:
    """测试用执行器：只记录提交，不真正跑（避免打网络、避免后台线程）。"""

    def __init__(self):
        self.submitted = []
        self.cancelled = []

    def submit(self, kind, params=None):
        from backend.core.meta_db import get_meta_db

        job_id = get_meta_db().create_job(kind=kind, params=params or {})
        self.submitted.append({"job_id": job_id, "kind": kind, "params": params or {}})
        return job_id

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return True


@pytest.fixture()
def runner(client, monkeypatch):
    """替换 job_api 里的执行器，让 POST /api/jobs 只落库不执行。"""
    import backend.api.job_api as job_api

    fake = FakeRunner()
    monkeypatch.setattr(job_api, "get_runner", lambda: fake)
    return fake


class TestJobAPI:
    def test_list_empty(self, client):
        assert body(client.get("/api/jobs"))["data"]["items"] == []

    def test_create_update_job(self, client, runner):
        res = client.post("/api/jobs", json={"kind": "update", "params": {"trade_date": "2026-10-16"}})
        assert res.status_code == 201
        data = body(res)["data"]
        assert data["status"] == "pending"
        assert data["kind"] == "update"
        assert data["job_id"]

        assert len(runner.submitted) == 1
        assert runner.submitted[0]["kind"] == "update"
        assert runner.submitted[0]["params"] == {"trade_date": "2026-10-16"}

        listed = body(client.get("/api/jobs"))["data"]["items"]
        assert len(listed) == 1
        assert listed[0]["id"] == data["job_id"]

    def test_unknown_params_are_filtered(self, client, runner):
        client.post(
            "/api/jobs",
            json={"kind": "update", "params": {"trade_date": "2026-10-16", "evil": "x"}},
        )
        assert runner.submitted[0]["params"] == {"trade_date": "2026-10-16"}

    def test_backfill_params_accepted(self, client, runner):
        client.post(
            "/api/jobs",
            json={
                "kind": "backfill",
                "params": {"start": "2008-01-01", "valuation_years": 5, "with_fundamental": True},
            },
        )
        assert runner.submitted[0]["params"]["valuation_years"] == 5

    def test_invalid_kind_rejected(self, client):
        res = client.post("/api/jobs", json={"kind": "hack"})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "invalid_kind"

    def test_missing_kind_rejected(self, client):
        res = client.post("/api/jobs", json={})
        assert res.status_code == 400

    def test_invalid_params_type_rejected(self, client):
        res = client.post("/api/jobs", json={"kind": "update", "params": "not-a-dict"})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "invalid_params"

    def test_evaluate_kind_accepted(self, client, runner):
        """evaluate 在 P5 已实现：不再是 501，而是正常入队。

        （历史上这里是 ``test_unimplemented_kinds`` 断言 501 —— 那个
        占位断言在功能落地后必须删掉，否则会永远拦住新功能的验收。）
        """
        res = client.post("/api/jobs", json={"kind": "evaluate", "params": {"version": "v2.0.0"}})
        assert res.status_code == 201
        assert body(res)["data"]["kind"] == "evaluate"

    def test_get_detail(self, client, runner):
        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        data = body(client.get(f"/api/jobs/{job_id}"))["data"]
        assert data["id"] == job_id
        assert data["kind"] == "update"

    def test_get_missing_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404

    def test_cancel_pending(self, client, runner):
        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        data = body(client.post(f"/api/jobs/{job_id}/cancel"))["data"]
        assert data["status"] == "cancelling"
        assert runner.cancelled == [job_id]

    def test_cancel_running_signals(self, client, runner):
        from backend.core.meta_db import get_meta_db

        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        get_meta_db().update_job(job_id, status="running")
        res = client.post(f"/api/jobs/{job_id}/cancel")
        assert res.status_code == 200
        assert body(res)["data"]["status"] == "cancelling"

    def test_cancel_finished_conflicts(self, client, runner):
        from backend.core.meta_db import get_meta_db

        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        get_meta_db().update_job(job_id, status="success")
        res = client.post(f"/api/jobs/{job_id}/cancel")
        assert res.status_code == 409
        assert body(res)["error"]["code"] == "invalid_status"

    def test_cancel_orphan_marks_cancelled(self, client, monkeypatch):
        """执行器里没有该任务（如进程重启过）时，直接标记 cancelled。"""
        import backend.api.job_api as job_api
        from backend.core.meta_db import get_meta_db

        class OrphanRunner(FakeRunner):
            def cancel(self, job_id):
                return False

        monkeypatch.setattr(job_api, "get_runner", lambda: OrphanRunner())
        job_id = get_meta_db().create_job(kind="update")
        data = body(client.post(f"/api/jobs/{job_id}/cancel"))["data"]
        assert data["status"] == "cancelled"
        assert get_meta_db().get_job(job_id)["status"] == "cancelled"


class TestBacktestAPI:
    def test_list_empty(self, client):
        data = body(client.get("/api/backtests"))["data"]
        assert data["items"] == []
        assert data["count"] == 0

    def test_post_without_data_fails_cleanly(self, client):
        """引擎已接入：没数据时是 400 + 明确原因，不再是 501 骨架。"""
        res = client.post("/api/backtests", json={"top_n": 20})
        assert res.status_code == 400
        error = body(res)["error"]
        assert error["code"] == "backtest_failed"
        assert error["message"]

    def test_post_bad_payload_is_not_500(self, client):
        """version 写错 → 400；payload 不是 JSON → 走空 dict → 同样 400。"""
        res = client.post("/api/backtests", json={"version": "v9.9.9"})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "backtest_failed"

    def test_post_runs_engine_end_to_end(self, client):
        """灌一份最小数据 → POST 真的跑完并落盘 + 记 experiments。"""
        import numpy as np

        from backend.core.config import get_config
        from backend.core.lake import Lake
        from backend.core.snapshots import ScoreStore

        cfg = get_config()
        lake, store = Lake(cfg), ScoreStore(cfg)
        dates = pd.bdate_range("2023-01-02", periods=60)
        rows = []
        for code in ("sh.600001", "sh.600002"):
            price = 10.0
            for day in dates:
                price *= 1.001
                rows.append({"date": day, "code": code, "open": price,
                             "high": price * 1.01, "low": price * 0.99,
                             "close": price, "volume": 1e6, "amount": price * 1e6,
                             "turn": 0.01, "pct_chg": 0.0, "tradestatus": 1})
        lake.write("bars", pd.DataFrame(rows))
        for day in dates:
            store.write(pd.DataFrame({
                "code": ["sh.600001", "sh.600002"],
                "score_pct": [80.0, 20.0],
            }), "v1.0.0", day.strftime("%Y-%m-%d"))

        res = client.post("/api/backtests", json={
            "top_n": 2, "rebal_days": 10, "regime": False,
            "max_weight": 0.6, "version": "v1.0.0", "grid": False,
        })
        assert res.status_code == 201, body(res)
        data = body(res)["data"]
        assert data["id"].startswith("bt_")
        assert data["score_version"] == "v1.0.0"
        assert data["metrics"]["full"]["days"] > 0
        assert data["experiment_id"].startswith("exp_")

        # 落盘后可被 GET/DELETE 读到
        detail = body(client.get(f"/api/backtests/{data['id']}"))
        assert detail["data"]["id"] == data["id"]
        assert body(client.delete(f"/api/backtests/{data['id']}"))["data"]["deleted"] == data["id"]

    def test_detail_missing_404(self, client):
        assert client.get("/api/backtests/nope").status_code == 404

    def test_list_and_delete(self, client):
        import json

        from backend.core.config import get_config

        folder = get_config().paths.results / "backtests"
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": "bt_demo",
            "created_at": "2026-10-16T18:00:00",
            "params": {"top_n": 20, "rebal_days": 20},
            "period": {"start": "2021-10-16", "end": "2026-10-16"},
            "metrics": {"full": {"total_return": 42.5, "annual_return": 8.1, "sharpe": 1.2, "max_drawdown": -18.3}},
        }
        (folder / "bt_demo.json").write_text(json.dumps(payload), encoding="utf-8")

        items = body(client.get("/api/backtests"))["data"]["items"]
        assert len(items) == 1
        assert items[0]["id"] == "bt_demo"
        assert items[0]["total_return"] == pytest.approx(42.5)

        assert body(client.get("/api/backtests/bt_demo"))["data"]["id"] == "bt_demo"
        assert body(client.delete("/api/backtests/bt_demo"))["data"]["deleted"] == "bt_demo"
        assert body(client.get("/api/backtests"))["data"]["items"] == []


class TestErrorContract:
    def test_api_404_is_json(self, client):
        res = client.get("/api/does-not-exist")
        assert res.status_code == 404
        assert res.is_json
        assert body(res)["error"]["code"] == "not_found"

    def test_all_error_responses_have_code_and_message(self, client):
        for path in ("/api/jobs/nope", "/api/backtests/nope", "/api/scores/sh.000000", "/api/nope"):
            payload = body(client.get(path))
            assert payload["ok"] is False
            assert payload["error"]["code"]
            assert payload["error"]["message"]
