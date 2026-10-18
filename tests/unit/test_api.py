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
        assert body(res)["error"]["code"] == "not_found"


class TestJobAPI:
    def test_list_empty(self, client):
        assert body(client.get("/api/jobs"))["data"]["items"] == []

    def test_create_update_job(self, client):
        res = client.post("/api/jobs", json={"kind": "update", "params": {"scope": "hs300"}})
        assert res.status_code == 201
        data = body(res)["data"]
        assert data["status"] == "pending"
        assert data["kind"] == "update"
        assert data["job_id"]

        listed = body(client.get("/api/jobs"))["data"]["items"]
        assert len(listed) == 1
        assert listed[0]["id"] == data["job_id"]

    def test_invalid_kind_rejected(self, client):
        res = client.post("/api/jobs", json={"kind": "hack"})
        assert res.status_code == 400
        assert body(res)["error"]["code"] == "invalid_kind"

    def test_missing_kind_rejected(self, client):
        res = client.post("/api/jobs", json={})
        assert res.status_code == 400

    def test_get_detail(self, client):
        job_id = body(client.post("/api/jobs", json={"kind": "score"}))["data"]["job_id"]
        data = body(client.get(f"/api/jobs/{job_id}"))["data"]
        assert data["id"] == job_id
        assert data["kind"] == "score"

    def test_get_missing_404(self, client):
        assert client.get("/api/jobs/nope").status_code == 404

    def test_cancel_pending(self, client):
        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        data = body(client.post(f"/api/jobs/{job_id}/cancel"))["data"]
        assert data["status"] == "cancelled"

    def test_cancel_running_conflicts(self, client):
        from backend.core.meta_db import get_meta_db

        job_id = body(client.post("/api/jobs", json={"kind": "update"}))["data"]["job_id"]
        get_meta_db().update_job(job_id, status="running")
        res = client.post(f"/api/jobs/{job_id}/cancel")
        assert res.status_code == 409
        assert body(res)["error"]["code"] == "invalid_status"


class TestBacktestAPI:
    def test_list_empty(self, client):
        data = body(client.get("/api/backtests"))["data"]
        assert data["items"] == []
        assert data["count"] == 0

    def test_post_not_implemented(self, client):
        res = client.post("/api/backtests", json={"top_n": 20})
        assert res.status_code == 501
        assert body(res)["error"]["code"] == "not_implemented"

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
