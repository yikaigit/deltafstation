"""任务 API（P0 骨架）。

- GET  /api/jobs            最近任务列表
- POST /api/jobs            创建任务（kind=update 等；执行器在 P1 接入）
- GET  /api/jobs/<id>       任务详情
- POST /api/jobs/<id>/cancel  取消（仅 pending 可取消）
"""
from __future__ import annotations

from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.meta_db import get_meta_db

job_bp = Blueprint("job", __name__)

SUPPORTED_KINDS = ("update", "backfill", "score", "evaluate")


@job_bp.route("/jobs", methods=["GET", "POST"])
def handle_jobs():
    db = get_meta_db()

    if request.method == "GET":
        limit = min(int(request.args.get("limit", 20)), 200)
        return ok({"items": db.list_jobs(limit=limit)})

    payload = request.get_json(silent=True) or {}
    kind = (payload.get("kind") or "").strip()
    if kind not in SUPPORTED_KINDS:
        return fail("invalid_kind", f"kind 必须是 {SUPPORTED_KINDS} 之一", 400)
    job_id = db.create_job(kind=kind, params=payload.get("params") or {})
    return ok({"job_id": job_id, "status": "pending", "kind": kind}, status=201)


@job_bp.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id: str):
    job = get_meta_db().get_job(job_id)
    if not job:
        return fail("not_found", f"任务 {job_id} 不存在", 404)
    return ok(job)


@job_bp.route("/jobs/<job_id>/cancel", methods=["POST"])
def cancel_job(job_id: str):
    db = get_meta_db()
    job = db.get_job(job_id)
    if not job:
        return fail("not_found", f"任务 {job_id} 不存在", 404)
    if job["status"] != "pending":
        return fail("invalid_status", f"仅 pending 任务可取消，当前 {job['status']}", 409)
    db.update_job(job_id, status="cancelled", message="用户取消")
    return ok({"job_id": job_id, "status": "cancelled"})
