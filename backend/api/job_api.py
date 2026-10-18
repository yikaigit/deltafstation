"""任务 API。

- GET  /api/jobs              最近任务列表
- POST /api/jobs             创建并执行任务（kind=update/backfill/score/evaluate/catchup）
- GET  /api/jobs/<id>        任务详情
- POST /api/jobs/<id>/cancel 请求取消（仅 pending/running 可取消）

任务在后台线程串行执行（见 backend/core/jobs.py），接口立即返回 job_id，
前端轮询 ``GET /api/jobs/<id>`` 获取 progress 与 message。
"""
from __future__ import annotations

import logging

from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.jobs import get_runner
from backend.core.meta_db import get_meta_db

logger = logging.getLogger(__name__)

job_bp = Blueprint("job", __name__)

SUPPORTED_KINDS = ("update", "backfill", "score", "evaluate", "catchup")

#: 尚未实现的类型（明确告知前端，而不是静默排队后失败）
#: P5 起 evaluate 已实现，这里留空 —— 保留结构是为了 P6 可能的新任务类型。
PENDING_KINDS: dict = {}

#: 各类型允许的参数（防止把任意字段塞进 params）
ALLOWED_PARAMS = {
    "update": {"trade_date", "with_fundamental", "code", "start"},
    "backfill": {"start", "end", "valuation_years", "with_fundamental"},
    "score": {"trade_date", "version", "full", "start", "end"},
    "evaluate": {"version", "start", "end", "factors", "forward_days"},
    "catchup": {"as_of", "lookback_days", "score", "version"},
}


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

    if kind in PENDING_KINDS:
        return fail(
            "not_implemented",
            f"{kind} 任务将在 {PENDING_KINDS[kind]} 阶段实现，当前仅支持 update / backfill",
            501,
        )

    raw_params = payload.get("params") or {}
    if not isinstance(raw_params, dict):
        return fail("invalid_params", "params 必须是对象", 400)
    allowed = ALLOWED_PARAMS.get(kind, set())
    params = {k: v for k, v in raw_params.items() if k in allowed}
    rejected = sorted(set(raw_params) - allowed)
    if rejected:
        logger.info("忽略未支持的参数 %s（kind=%s）", rejected, kind)

    try:
        job_id = get_runner().submit(kind=kind, params=params)
    except NotImplementedError as exc:
        return fail("not_implemented", str(exc), 501)
    except Exception as exc:
        logger.exception("提交任务失败")
        return fail("submit_failed", str(exc), 500)

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
    if job["status"] not in ("pending", "running"):
        return fail("invalid_status", f"仅 pending/running 可取消，当前 {job['status']}", 409)

    signalled = get_runner().cancel(job_id)
    if not signalled:
        # 任务记录存在但执行器里没有（例如进程重启过）→ 直接标记
        db.update_job(job_id, status="cancelled", message="用户取消（执行器已无该任务）")
        return ok({"job_id": job_id, "status": "cancelled", "note": "任务已不在执行队列"})

    return ok({"job_id": job_id, "status": "cancelling"})
