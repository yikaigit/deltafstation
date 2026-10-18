"""打分查询 API。

- GET /api/scores                  当日榜单（可按行业/评级/关键词筛选）
- GET /api/scores/industries       行业汇总
- GET /api/scores/<code>           个股六维明细 + 历史分曲线
- GET /api/score-versions          打分版本列表
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd
from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.lake import get_lake
from backend.core.meta_db import get_meta_db
from backend.core.snapshots import get_score_store

score_bp = Blueprint("score", __name__)


def _jsonable(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 Timestamp / NaN 转成 JSON 可序列化的值。"""
    for item in records:
        for key, value in list(item.items()):
            if hasattr(value, "isoformat"):
                item[key] = value.isoformat()
            elif isinstance(value, float) and value != value:
                item[key] = None
    return records


def _resolve(version: str | None, date: str | None):
    store = get_score_store()
    available = store.versions()
    version = version or (available[-1] if available else None)
    if date in (None, "", "latest"):
        date = store.latest_date(version)
    return store, version, date


@score_bp.route("/scores", methods=["GET"])
def list_scores():
    store, version, date = _resolve(request.args.get("version"), request.args.get("date"))
    if not version or not date:
        return ok(
            {
                "items": [],
                "meta": {"version": version, "date": None, "count": 0, "distribution": {}},
                "note": "尚无打分快照，请先执行每日任务",
            }
        )
    try:
        df = store.read(version=version, trade_date=date)
    except Exception as exc:  # pragma: no cover
        return fail("score_read_error", str(exc), 500)
    if df.empty:
        return ok({"items": [], "meta": {"version": version, "date": date, "count": 0, "distribution": {}}})

    if "industry" not in df.columns:
        try:
            uni = get_lake().read("universe", columns=["code", "name", "industry_l1"])
            if not uni.empty:
                df = df.merge(uni.rename(columns={"industry_l1": "industry"}), on="code", how="left")
        except Exception:
            pass

    industry = (request.args.get("industry") or "").strip()
    grade = (request.args.get("grade") or "").strip()
    keyword = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 500)), 5000)

    distribution = df["grade"].value_counts().to_dict() if "grade" in df.columns else {}
    filtered = df
    if industry and "industry" in filtered.columns:
        filtered = filtered[filtered["industry"] == industry]
    if grade:
        filtered = filtered[filtered["grade"] == grade]
    if keyword:
        mask = filtered["code"].astype(str).str.contains(keyword, case=False, na=False)
        if "name" in filtered.columns:
            mask |= filtered["name"].astype(str).str.contains(keyword, na=False)
        filtered = filtered[mask]

    filtered = filtered.sort_values("score_pct", ascending=False).head(limit)
    items = _jsonable(filtered.to_dict("records"))
    return ok(
        {
            "items": items,
            "meta": {"version": version, "date": date, "count": len(items), "distribution": distribution},
        }
    )


@score_bp.route("/scores/industries", methods=["GET"])
def score_industries():
    store, version, date = _resolve(request.args.get("version"), request.args.get("date"))
    if not version or not date:
        return ok({"items": [], "meta": {"version": version, "date": None}})
    df = store.read(version=version, trade_date=date)
    if df.empty:
        return ok({"items": [], "meta": {"version": version, "date": date}})

    if "industry" not in df.columns:
        try:
            uni = get_lake().read("universe", columns=["code", "industry_l1"])
            if not uni.empty:
                df = df.merge(uni.rename(columns={"industry_l1": "industry"}), on="code", how="left")
        except Exception:
            pass
    if "industry" not in df.columns:
        return ok({"items": [], "meta": {"version": version, "date": date}, "note": "缺少行业字段"})

    grouped = (
        df.groupby("industry")
        .agg(
            count=("code", "count"),
            avg_score=("score_pct", "mean"),
            median_score=("score_pct", "median"),
            strong_buy=("grade", lambda s: int((s == "强烈看多").sum())),
            buy=("grade", lambda s: int((s == "看多").sum())),
        )
        .reset_index()
        .sort_values("avg_score", ascending=False)
    )
    for col in ("avg_score", "median_score"):
        grouped[col] = grouped[col].round(2)
    return ok({"items": grouped.to_dict("records"), "meta": {"version": version, "date": date}})


@score_bp.route("/scores/<code>", methods=["GET"])
def score_detail(code: str):
    store = get_score_store()
    version = request.args.get("version") or (store.versions()[-1] if store.versions() else None)
    if not version:
        return fail("no_version", "尚无打分版本", 404)
    df = store.read(version=version)
    if df.empty:
        return fail("no_score", "尚无打分数据", 404)

    one = df[df["code"] == code].sort_values("trade_date")
    if one.empty:
        return fail("not_found", f"未找到 {code} 的打分记录", 404)

    latest = one.iloc[-1].to_dict()
    history = [
        {"date": pd.Timestamp(r["trade_date"]).strftime("%Y-%m-%d"), "score_pct": float(r["score_pct"])}
        for _, r in one.tail(250).iterrows()
        if pd.notna(r.get("score_pct"))
    ]

    meta: Dict[str, Any] = {}
    try:
        uni = get_lake().read("universe")
        if not uni.empty:
            row = uni[uni["code"] == code]
            if not row.empty:
                meta = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.iloc[0].to_dict().items()}
    except Exception:
        pass

    return ok({"latest": _jsonable([latest])[0], "history": history, "meta": meta, "version": version})


@score_bp.route("/score-versions", methods=["GET"])
def score_versions():
    store = get_score_store()
    versions = []
    try:
        registered = {v["version"]: v for v in get_meta_db().list_score_versions()}
    except Exception:
        registered = {}
    for v in store.versions():
        info = registered.get(v, {})
        dates = store.dates(v)
        versions.append(
            {
                "version": v,
                "created_at": info.get("created_at"),
                "note": info.get("note"),
                "dates": len(dates),
                "latest_date": dates[-1] if dates else None,
            }
        )
    return ok({"items": versions, "count": len(versions)})
