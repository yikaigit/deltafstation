"""回测 API。

- GET    /api/backtests        回测列表
- POST   /api/backtests        运行回测（异步？否——单机串行，直接同步跑）
- GET    /api/backtests/<id>   回测详情
- DELETE /api/backtests/<id>   删除回测

POST 的入参就是 ``StrategyParams.from_payload`` 认识的那几个键
（``top_n`` / ``rebal_days`` / ``regime`` / ``version`` / ``grid`` 等），
缺省值取 ``config/app.yaml`` 的 ``backtest`` 段。引擎跑完会顺手落盘并记
一条 ``experiments``，所以 POST 的返回体里直接带 ``path`` 和 ``experiment_id``。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.backtest import runner
from backend.core.config import get_config

logger = logging.getLogger(__name__)

backtest_bp = Blueprint("backtest", __name__)


def _folder() -> Path:
    path = Path(get_config().paths.results) / "backtests"
    path.mkdir(parents=True, exist_ok=True)
    return path


@backtest_bp.route("/backtests", methods=["GET"])
def list_backtests():
    items = []
    for file in sorted(_folder().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(file, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        metrics = (payload.get("metrics") or {}).get("full") or {}
        items.append(
            {
                "id": payload.get("id", file.stem),
                "created_at": payload.get("created_at"),
                "params": payload.get("params", {}),
                "period": payload.get("period", {}),
                "total_return": metrics.get("total_return"),
                "annual_return": metrics.get("annual_return"),
                "sharpe": metrics.get("sharpe"),
                "max_drawdown": metrics.get("max_drawdown"),
            }
        )
    return ok({"items": items, "count": len(items)})


@backtest_bp.route("/backtests", methods=["POST"])
def run_backtest():
    payload = request.get_json(silent=True) or {}

    def progress(ratio: float, message: str) -> None:
        logger.info("回测进度 %.0f%%：%s", ratio * 100, message)

    try:
        # grid 默认 True：规格 §6.6 的参数网格是回测中心的一部分。
        # 想只要主回测，传 "grid": false。
        result = runner.run(
            payload=payload,
            grid=bool(payload.get("grid", True)),
            progress=progress,
        )
    except (RuntimeError, ValueError) as exc:
        # 这类是「没数据/没快照/参数不合法」——用户改一改就能重跑，
        # 所以给 400 而不是 500，并带上原始信息方便定位。
        logger.warning("回测失败：%s", exc)
        return fail("backtest_failed", str(exc), 400, request=payload)
    except Exception as exc:  # pragma: no cover - 真·未预期错误
        logger.exception("回测出现未预期错误")
        return fail("internal_error", str(exc), 500)

    return ok(result, 201)


@backtest_bp.route("/backtests/<bt_id>", methods=["GET", "DELETE"])
def handle_backtest(bt_id: str):
    path = _folder() / f"{bt_id}.json"
    if not path.exists():
        return fail("not_found", f"回测 {bt_id} 不存在", 404)

    if request.method == "DELETE":
        path.unlink()
        return ok({"deleted": bt_id})

    try:
        with open(path, "r", encoding="utf-8") as f:
            return ok(json.load(f))
    except (OSError, json.JSONDecodeError) as exc:
        return fail("read_error", str(exc), 500)
