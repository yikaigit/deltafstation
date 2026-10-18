"""收盘后补齐 API（P6）。

- GET  /api/catchup/status   数据缺口体检 + 各数据集最新日期 + 上次补齐结果
- POST /api/catchup/run      触发补齐流水线（后台任务）
- GET  /api/catchup/last     最近一次补齐任务的执行结果（供首页卡片）

为什么要单独一组接口，而不是复用 ``/api/jobs``
---------------------------------------------
首页那张卡片需要**一次请求**拿到「数据到哪天、缺几天、上次跑得怎么样」，
如果让前端自己去拼 ``/api/data/status`` + ``/api/jobs`` + 多个快照接口，
不仅慢，而且「缺几天」这个判断逻辑会在前后端各写一遍 —— 两套口径必然漂移。
所以缺口体检放在服务端，前端只负责画。
"""
from __future__ import annotations

import logging

from flask import Blueprint, request

from backend.api import fail, ok

logger = logging.getLogger(__name__)

catchup_bp = Blueprint("catchup", __name__)

#: 首页卡片展示的上次任务类型
_CATCHUP_KIND = "catchup"


def _last_catchup() -> dict:
    """最近一次 catchup 任务（含结果摘要）。"""
    try:
        from backend.core.meta_db import get_meta_db

        db = get_meta_db()
        rows = db.list_jobs(limit=50) if hasattr(db, "list_jobs") else []
    except Exception:  # pragma: no cover
        return {}

    for row in rows or []:
        if row.get("kind") != _CATCHUP_KIND:
            continue
        return {
            "job_id": row.get("id") or row.get("job_id"),
            "status": row.get("status"),
            "progress": row.get("progress"),
            "message": row.get("message"),
            "created_at": row.get("created_at"),
            "finished_at": row.get("finished_at"),
        }
    return {}


@catchup_bp.route("/catchup/status", methods=["GET"])
def catchup_status():
    """缺口体检（读操作，不产生任何网络请求）。

    回看窗口默认 60 个交易日，可用 ``?lookback_days=`` 调整。
    用户关心的其实是「我停了好久，还能不能补」—— 所以这里同时返回
    ``stale_days``：超出窗口、只报告不自动补的日子。
    """
    from backend.core import catchup as catchup_lib
    from backend.core.config import get_config

    try:
        lookback = int(request.args.get("lookback_days", 60))
    except (TypeError, ValueError):
        lookback = 60
    lookback = max(1, min(lookback, 250))

    try:
        report = catchup_lib.gap_report(
            as_of=request.args.get("as_of"), lookback_days=lookback
        )
    except Exception as exc:
        logger.exception("缺口体检失败")
        return fail("catchup_status_error", str(exc), 500)

    # 打分的进度：最新快照日 vs 目标交易日
    snapshot = {}
    try:
        from backend.core.snapshots import get_score_store

        store = get_score_store()
        versions = store.versions()
        version = get_config().get("scoring.default_version")
        if version not in versions and versions:
            version = versions[-1]
        if version:
            snapshot = {
                "version": version,
                "latest_date": store.latest_date(version),
                "dates": len(store.dates(version)),
            }
    except Exception:  # pragma: no cover
        snapshot = {}

    return ok(
        {
            "gap": report.to_dict(),
            "lookback_days": lookback,
            "score": snapshot,
            "last_run": _last_catchup(),
            "default_version": get_config().get("scoring.default_version"),
        }
    )


@catchup_bp.route("/catchup/run", methods=["POST"])
def catchup_run():
    """触发补齐流水线。

    参数：
      * ``as_of``：基准日（默认今天）
      * ``lookback_days``：回看窗口（默认 60）
      * ``score``：补完是否打分（默认 True）
      * ``version``：打分版本（默认配置）
    """
    from backend.core.jobs import get_runner

    payload = request.get_json(silent=True) or {}

    # 防重复点击：已有 catchup 在跑时直接告诉用户，而不是排两个队
    try:
        from backend.core.meta_db import get_meta_db

        rows = get_meta_db().list_jobs(limit=20) or []
        for row in rows:
            if row.get("kind") == _CATCHUP_KIND and row.get("status") in ("pending", "running"):
                return ok(
                    {
                        "job_id": row.get("id") or row.get("job_id"),
                        "status": row.get("status"),
                        "already_running": True,
                        "message": "已有一轮补齐任务在运行，请等待其完成",
                    },
                    status=200,
                )
    except Exception:  # pragma: no cover
        pass

    params = {}
    if payload.get("as_of"):
        params["as_of"] = str(payload["as_of"])
    if payload.get("lookback_days"):
        try:
            params["lookback_days"] = int(payload["lookback_days"])
        except (TypeError, ValueError):
            return fail("invalid_params", "lookback_days 必须是整数", 400)
    params["score"] = bool(payload.get("score", True))
    if payload.get("version"):
        params["version"] = str(payload["version"])

    try:
        job_id = get_runner().submit(kind=_CATCHUP_KIND, params=params)
    except Exception as exc:  # pragma: no cover
        logger.exception("提交补齐任务失败")
        return fail("job_error", str(exc), 500)

    return ok({"job_id": job_id, "status": "pending", "params": params}, status=201)
