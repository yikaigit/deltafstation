"""数据管理 API。

- GET /api/universe        标的列表（含行业/板块）
- GET /api/data/status     数据覆盖情况（数据集 + 每标的）
- GET /api/data/quality    最近一次数据校验报告
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.config import get_config
from backend.core.lake import LakeError, get_lake

data_bp = Blueprint("data", __name__)


def _universe_frame():
    lake = get_lake()
    df = lake.read("universe")
    if df.empty:
        return df
    for col in ("name", "board", "industry_l1", "list_date", "is_st", "in_hs300"):
        if col not in df.columns:
            df[col] = None
    return df


@data_bp.route("/universe", methods=["GET"])
def list_universe():
    try:
        df = _universe_frame()
        if df.empty:
            return ok({"items": [], "count": 0, "note": "标的表尚未建立，请先执行数据回填"})

        industry = (request.args.get("industry") or "").strip()
        board = (request.args.get("board") or "").strip()
        keyword = (request.args.get("q") or "").strip()
        limit = min(int(request.args.get("limit", 500)), 5000)

        if industry:
            df = df[df["industry_l1"] == industry]
        if board:
            df = df[df["board"] == board]
        if keyword:
            mask = df["code"].astype(str).str.contains(keyword, case=False, na=False) | df[
                "name"
            ].astype(str).str.contains(keyword, na=False)
            df = df[mask]

        items = df.head(limit).to_dict("records")
        for item in items:
            for key, value in list(item.items()):
                if hasattr(value, "isoformat"):
                    item[key] = value.isoformat()
        return ok({"items": items, "count": len(items), "total": int(len(df))})
    except LakeError as exc:
        return fail("lake_unavailable", str(exc), 503)
    except Exception as exc:  # pragma: no cover - 兜底
        return fail("universe_error", str(exc), 500)


@data_bp.route("/data/status", methods=["GET"])
def data_status():
    """返回数据湖各数据集的覆盖概况 + 每标的明细。"""
    try:
        lake = get_lake()
        datasets: Dict[str, Dict[str, Any]] = {}
        for name in ("bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"):
            exists = lake.exists(name)
            info: Dict[str, Any] = {"exists": exists}
            if exists:
                try:
                    if name in ("bars", "valuation"):
                        df = lake.read(name, columns=["date", "code"])
                        info["rows"] = int(len(df))
                        if not df.empty:
                            info["first_date"] = df["date"].min().strftime("%Y-%m-%d")
                            info["last_date"] = df["date"].max().strftime("%Y-%m-%d")
                            info["symbols"] = int(df["code"].nunique())
                    else:
                        info["rows"] = int(len(lake.read(name)))
                except Exception as exc:  # 单个数据集异常不影响整体
                    info["error"] = str(exc)
            datasets[name] = info

        symbols = []
        if lake.exists("bars"):
            cov = lake.coverage("bars")
            names = {}
            if lake.exists("universe"):
                uni = lake.read("universe", columns=["code", "name", "industry_l1", "board"])
                names = {r["code"]: r for r in uni.to_dict("records")}
            for row in cov.to_dict("records"):
                meta = names.get(row["code"], {})
                symbols.append(
                    {
                        "code": row["code"],
                        "name": meta.get("name"),
                        "industry": meta.get("industry_l1"),
                        "board": meta.get("board"),
                        "first_date": row["first_date"].strftime("%Y-%m-%d") if row.get("first_date") is not None else None,
                        "last_date": row["last_date"].strftime("%Y-%m-%d") if row.get("last_date") is not None else None,
                        "rows": int(row["rows"]),
                    }
                )

        return ok({"datasets": datasets, "symbols": symbols, "symbol_count": len(symbols)})
    except LakeError as exc:
        return fail("lake_unavailable", str(exc), 503)
    except Exception as exc:  # pragma: no cover
        return fail("status_error", str(exc), 500)


@data_bp.route("/data/quality", methods=["GET"])
def data_quality():
    """最近一次数据校验报告（P1 生成）。"""
    folder = Path(get_config().paths.quality)
    reports = sorted(folder.glob("report_*.json")) if folder.exists() else []
    if not reports:
        return ok({"available": False, "note": "尚未生成校验报告"})
    latest = reports[-1]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return fail("quality_read_error", str(exc), 500)
    return ok({"available": True, "file": latest.name, "report": payload})
