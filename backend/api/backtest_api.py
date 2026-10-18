"""回测 API（P0 骨架，引擎在 P3 实现）。

- GET    /api/backtests        回测列表
- POST   /api/backtests        运行回测（P3 开放）
- GET    /api/backtests/<id>   回测详情
- DELETE /api/backtests/<id>   删除回测
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.config import get_config

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
    return fail(
        "not_implemented",
        "回测引擎将在 P3 阶段实现；当前仅提供接口骨架。",
        501,
        received=payload,
    )


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
