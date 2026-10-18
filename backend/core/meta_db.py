"""SQLite 元数据库：任务记录、打分版本、实验记录。

只存「几十到几万行」的元信息，行情与因子数据一律走 Parquet（见 lake.py）。
每次操作独立开连接，避免 Flask 多线程共享连接的问题。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .config import Config, get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS job_runs (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    status      TEXT NOT NULL,
    params      TEXT,
    progress    REAL DEFAULT 0,
    message     TEXT,
    log         TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_created ON job_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS score_versions (
    version       TEXT PRIMARY KEY,
    created_at    TEXT NOT NULL,
    config_yaml   TEXT,
    factor_set    TEXT,
    data_snapshot TEXT,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS experiments (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    hypothesis TEXT,
    factor_set TEXT,
    weights    TEXT,
    start_date TEXT,
    end_date   TEXT,
    metrics    TEXT,
    created_at TEXT NOT NULL
);
"""

JOB_STATUSES = ("pending", "running", "success", "failed", "cancelled", "partial")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class MetaDB:
    """元数据库访问对象。"""

    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or get_config()
        self.path = Path(self.cfg.paths.meta_db)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> "MetaDB":
        """建表（幂等）。"""
        with self.connect() as conn:
            conn.executescript(SCHEMA)
        return self

    # ---------- 任务 ----------

    def create_job(self, kind: str, params: Optional[Dict[str, Any]] = None, status: str = "pending") -> str:
        job_id = f"{kind[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO job_runs (id, kind, status, params, progress, created_at) VALUES (?,?,?,?,?,?)",
                (job_id, kind, status, json.dumps(params or {}, ensure_ascii=False), 0.0, _now()),
            )
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        """更新任务字段；允许 status/progress/message/log/started_at/finished_at。"""
        allowed = {"status", "progress", "message", "log", "started_at", "finished_at", "params"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "params" in updates and not isinstance(updates["params"], str):
            updates["params"] = json.dumps(updates["params"], ensure_ascii=False)
        if updates.get("status") == "running" and "started_at" not in updates:
            updates["started_at"] = _now()
        if updates.get("status") in ("success", "failed", "partial", "cancelled") and "finished_at" not in updates:
            updates["finished_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in updates)
        with self.connect() as conn:
            conn.execute(f"UPDATE job_runs SET {cols} WHERE id=?", (*updates.values(), job_id))

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM job_runs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_dict(row) if row else None

    def list_jobs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM job_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---------- 打分版本 ----------

    def add_score_version(
        self,
        version: str,
        config_yaml: str = "",
        factor_set: Optional[List[str]] = None,
        data_snapshot: str = "",
        note: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO score_versions (version, created_at, config_yaml, factor_set, data_snapshot, note)"
                " VALUES (?,?,?,?,?,?)",
                (version, _now(), config_yaml, json.dumps(factor_set or [], ensure_ascii=False), data_snapshot, note),
            )

    def list_score_versions(self) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM score_versions ORDER BY created_at DESC").fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---------- 实验 ----------

    def add_experiment(self, name: str, hypothesis: str = "", **fields: Any) -> str:
        exp_id = f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:4]}"
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO experiments (id, name, hypothesis, factor_set, weights, start_date, end_date, metrics, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    exp_id,
                    name,
                    hypothesis,
                    json.dumps(fields.get("factor_set", []), ensure_ascii=False),
                    json.dumps(fields.get("weights", {}), ensure_ascii=False),
                    fields.get("start_date"),
                    fields.get("end_date"),
                    json.dumps(fields.get("metrics", {}), ensure_ascii=False),
                    _now(),
                ),
            )
        return exp_id

    def list_experiments(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    # ---------- 工具 ----------

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for key in ("params", "factor_set", "weights", "metrics"):
            if key in data and isinstance(data[key], str):
                try:
                    data[key] = json.loads(data[key])
                except (json.JSONDecodeError, TypeError):
                    pass
        return data


def get_meta_db() -> MetaDB:
    """便捷入口：使用全局配置并确保表已创建。"""
    return MetaDB(get_config()).init()
