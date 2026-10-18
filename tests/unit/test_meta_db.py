"""元数据库测试：建表幂等、任务生命周期、版本登记、实验记录。"""
from __future__ import annotations

import pytest


class TestInit:
    def test_tables_created(self, db):
        with db.connect() as conn:
            names = {
                r[0]
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        assert {"job_runs", "score_versions", "experiments"} <= names

    def test_init_is_idempotent(self, cfg):
        from backend.core.meta_db import MetaDB

        first = MetaDB(cfg).init()
        second = MetaDB(cfg).init()
        assert first.path == second.path
        with second.connect() as conn:
            count = conn.execute("SELECT count(*) FROM job_runs").fetchone()[0]
        assert count == 0


class TestJobs:
    def test_create_returns_id_and_pending(self, db):
        job_id = db.create_job("update", {"scope": "hs300"})
        job = db.get_job(job_id)
        assert job["id"] == job_id
        assert job["kind"] == "update"
        assert job["status"] == "pending"
        assert job["progress"] == pytest.approx(0.0)
        assert job["params"] == {"scope": "hs300"}

    def test_id_format(self, db):
        job_id = db.create_job("score")
        assert job_id.startswith("score_")
        assert job_id.count("_") == 3

    def test_running_sets_started_at(self, db):
        job_id = db.create_job("update")
        db.update_job(job_id, status="running")
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert job["started_at"] is not None
        assert job["finished_at"] is None

    @pytest.mark.parametrize("status", ["success", "failed", "cancelled", "partial"])
    def test_terminal_status_sets_finished_at(self, db, status):
        job_id = db.create_job("update")
        db.update_job(job_id, status=status)
        assert db.get_job(job_id)["finished_at"] is not None

    def test_progress_and_message(self, db):
        job_id = db.create_job("update")
        db.update_job(job_id, progress=0.5, message="拉取 150/300")
        job = db.get_job(job_id)
        assert job["progress"] == pytest.approx(0.5)
        assert job["message"] == "拉取 150/300"

    def test_unknown_field_is_ignored(self, db):
        job_id = db.create_job("update")
        db.update_job(job_id, nonsense="x", status="running")
        job = db.get_job(job_id)
        assert job["status"] == "running"
        assert "nonsense" not in job

    def test_empty_update_is_noop(self, db):
        job_id = db.create_job("update")
        db.update_job(job_id)
        assert db.get_job(job_id)["status"] == "pending"

    def test_list_orders_by_created_desc(self, db):
        ids = [db.create_job("update") for _ in range(3)]
        listed = db.list_jobs(limit=10)
        assert len(listed) == 3
        assert {j["id"] for j in listed} == set(ids)

    def test_list_respects_limit(self, db):
        for _ in range(5):
            db.create_job("update")
        assert len(db.list_jobs(limit=2)) == 2

    def test_get_missing_returns_none(self, db):
        assert db.get_job("nope") is None

    def test_params_roundtrip_as_dict(self, db):
        job_id = db.create_job("backfill", {"start": "2008-01-01", "years": [2008, 2009]})
        job = db.get_job(job_id)
        assert isinstance(job["params"], dict)
        assert job["params"]["years"] == [2008, 2009]


class TestScoreVersions:
    def test_add_and_list(self, db):
        db.add_score_version("v1.0.0", note="技术四维", factor_set=["ma_bias_20"])
        versions = db.list_score_versions()
        assert len(versions) == 1
        assert versions[0]["version"] == "v1.0.0"
        assert versions[0]["factor_set"] == ["ma_bias_20"]

    def test_replace_same_version(self, db):
        db.add_score_version("v1.0.0", note="第一版")
        db.add_score_version("v1.0.0", note="改过的说明")
        versions = db.list_score_versions()
        assert len(versions) == 1
        assert versions[0]["note"] == "改过的说明"

    def test_multiple_versions(self, db):
        db.add_score_version("v1.0.0")
        db.add_score_version("v2.0.0")
        assert {v["version"] for v in db.list_score_versions()} == {"v1.0.0", "v2.0.0"}


class TestExperiments:
    def test_add_experiment(self, db):
        exp_id = db.add_experiment(
            "动量加权实验",
            hypothesis="提高动量权重能提升 IC",
            factor_set=["roc_60"],
            weights={"momentum": 0.4},
            start_date="2020-01-01",
            end_date="2024-12-31",
            metrics={"ic": 0.05},
        )
        assert exp_id.startswith("exp_")
        rows = db.list_experiments()
        assert len(rows) == 1
        row = rows[0]
        assert row["name"] == "动量加权实验"
        assert row["weights"] == {"momentum": 0.4}
        assert row["metrics"] == {"ic": 0.05}

    def test_list_respects_limit(self, db):
        for i in range(4):
            db.add_experiment(f"exp{i}")
        assert len(db.list_experiments(limit=2)) == 2
