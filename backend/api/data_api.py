"""数据管理 API。

- GET /api/universe            标的列表（含行业/板块）
- GET /api/data/status         数据覆盖情况（数据集 + 每标的）
- GET /api/data/coverage       年份 × 标的 的覆盖矩阵（热力图用）
- GET /api/data/quality        最近一次数据校验报告
- GET /api/data/quality/export 校验报告下载（JSON 附件）
- POST /api/data/fill          单只标的补拉（建一条 update 任务，params.code 指到那只）
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd
from flask import Blueprint, Response, request

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
    """返回数据湖各数据集的覆盖概况 + 每标的明细 + 最近校验摘要。"""
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

        return ok(
            {
                "datasets": datasets,
                "symbols": symbols,
                "symbol_count": len(symbols),
                "calendar": _calendar_summary(datasets.get("calendar") or {}),
                "quality": _quality_summary(),
            }
        )
    except LakeError as exc:
        return fail("lake_unavailable", str(exc), 503)
    except Exception as exc:  # pragma: no cover
        return fail("status_error", str(exc), 500)


def _calendar_summary(info: Dict[str, Any]) -> Dict[str, Any]:
    """交易日历摘要：交易日数量与覆盖区间。"""
    if not info.get("exists"):
        return {"exists": False}
    try:
        from backend.core.calendar import get_calendar

        return get_calendar().coverage()
    except Exception as exc:
        return {"exists": True, "error": str(exc)}


def _quality_summary() -> Dict[str, Any]:
    """最近一次校验报告的精简摘要（完整报告走 /api/data/quality）。"""
    folder = Path(get_config().paths.quality)
    reports = sorted(folder.glob("report_*.json")) if folder.exists() else []
    if not reports:
        return {"available": False}
    latest = reports[-1]
    try:
        with open(latest, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"available": False}
    return {
        "available": True,
        "file": latest.name,
        "trade_date": payload.get("trade_date"),
        "symbols": payload.get("symbols"),
        "error_symbols": payload.get("error_symbols"),
        "warn_symbols": payload.get("warn_symbols"),
        "error_ratio": payload.get("error_ratio"),
        "degraded": payload.get("degraded"),
    }


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


@data_bp.route("/data/coverage", methods=["GET"])
def data_coverage():
    """年份 × 标的 的覆盖矩阵。

    每个格子是「该标的在该年有多少个交易日有行情」。前端把它画成热力图，
    一眼就能看出哪一年、哪一批标的是整片空白（比如新上市票的早年数据）。

    只用 ``columns=["date", "code"]`` 读，百万行也能在几百毫秒内出结果；
    真正的按年聚合在 pandas 里做，不额外落盘。
    """
    try:
        lake = get_lake()
        if not lake.exists("bars"):
            return ok({"years": [], "symbols": [], "matrix": {}, "note": "行情数据尚未建立"})
        df = lake.read("bars", columns=["date", "code"])
        if df is None or df.empty:
            return ok({"years": [], "symbols": [], "matrix": {}})

        frame = pd.DataFrame(
            {
                "year": pd.to_datetime(df["date"]).dt.year.astype(int),
                "code": df["code"].astype(str),
            }
        )
        grouped = frame.groupby(["code", "year"]).size().reset_index(name="days")
        years = sorted(grouped["year"].unique().tolist())

        # 每个标的的总覆盖情况，用于排序（行数多的排前面，便于对照）
        totals = grouped.groupby("code")["days"].sum().sort_values(ascending=False)
        codes = totals.index.tolist()

        calendar = None
        try:
            from backend.core.calendar import get_calendar

            calendar = get_calendar()
        except Exception:
            calendar = None

        # 每年的交易日总数（有日历就用日历，没有就退化成「该年最大覆盖」
        year_days: Dict[int, int] = {}
        for year in years:
            count = 0
            if calendar is not None:
                try:
                    count = int(len(calendar.trading_days(f"{year}-01-01", f"{year}-12-31")))
                except Exception:
                    count = 0
            if not count:
                subset = grouped[grouped["year"] == year]
                count = int(subset["days"].max()) if not subset.empty else 0
            year_days[int(year)] = count

        matrix: Dict[str, Dict[str, int]] = {}
        for row in grouped.itertuples(index=False):
            matrix.setdefault(row.code, {})[str(int(row.year))] = int(row.days)

        names = {}
        if lake.exists("universe"):
            uni = lake.read("universe", columns=["code", "name"])
            names = {r["code"]: r["name"] for r in uni.to_dict("records")}

        return ok(
            {
                "years": [str(y) for y in years],
                "year_days": {str(k): v for k, v in year_days.items()},
                "symbols": [
                    {"code": c, "name": names.get(c), "total_days": int(totals[c])}
                    for c in codes
                ],
                "matrix": matrix,
            }
        )
    except LakeError as exc:
        return fail("lake_unavailable", str(exc), 503)
    except Exception as exc:  # pragma: no cover
        return fail("coverage_error", str(exc), 500)


@data_bp.route("/data/quality/export", methods=["GET"])
def export_quality():
    """把最近一次校验报告原样下载成 JSON 文件。"""
    folder = Path(get_config().paths.quality)
    reports = sorted(folder.glob("report_*.json")) if folder.exists() else []
    if not reports:
        return fail("no_report", "尚未生成校验报告", 404)
    latest = reports[-1]
    try:
        body = latest.read_text(encoding="utf-8")
    except OSError as exc:
        return fail("read_error", str(exc), 500)
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{latest.name}"'},
    )


@data_bp.route("/data/fill", methods=["POST"])
def fill_symbol():
    """单只标的补拉（后台任务，只处理这一只）。

    ``update`` 任务本来就支持 ``params.code``：执行器看到它就只拉这一只的
    行情与复权因子，不会把 300 只全刷一遍。这里只做参数校验与任务提交。

    校验前置的理由：代码拼错时 baostock 会返回空帧，任务照样「成功但 0 行」，
    那种静默失败最难查 —— 所以先在标的表里确认这只票存在。
    """
    from backend.core.jobs import get_runner

    payload = request.get_json(silent=True) or {}
    code = (payload.get("code") or "").strip()
    if not code:
        return fail("missing_code", "缺少 code 参数", 400)

    try:
        uni = _universe_frame()
    except Exception as exc:  # pragma: no cover
        return fail("universe_error", str(exc), 500)
    if uni.empty or code not in set(uni["code"].astype(str)):
        return fail("unknown_code", f"标的 {code} 不在标的表中，请先刷新成分股", 400)

    start = payload.get("start") or None
    try:
        job_id = get_runner().submit(kind="update", params={"code": code, "start": start})
    except Exception as exc:  # pragma: no cover
        return fail("job_error", str(exc), 500)
    return ok({"job_id": job_id, "code": code, "start": start}, status=201)
