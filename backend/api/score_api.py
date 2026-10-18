"""打分查询 API。

- GET /api/scores                  当日榜单（可按行业/评级/关键词筛选）
- GET /api/scores/industries       行业汇总
- GET /api/scores/<code>           个股六维明细 + 历史分曲线 + K线 + 关键指标
- GET /api/score-versions          打分版本列表
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from flask import Blueprint, request

from backend.api import fail, ok
from backend.core.lake import get_lake
from backend.core.meta_db import get_meta_db
from backend.core.snapshots import get_score_store

score_bp = Blueprint("score", __name__)

#: 抽屉里 K 线的回看交易日数
KLINE_DAYS = 120

#: 抽屉里的关键指标字段（估值 + 财务）
_VALUATION_COLS = ("close", "pe_ttm", "pb_mrq", "ps_ttm", "total_mv", "float_mv")
_FUNDAMENTAL_COLS = ("roe_wa", "eps", "bps", "rev_yoy", "profit_yoy", "gross_margin", "div_yield")


def _clean(value: Any) -> Any:
    """把 NaN / inf / Timestamp 规整成 JSON 可序列化的值。"""
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat() if not pd.isna(value) else None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return None if math.isnan(number) or math.isinf(number) else number
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    return value


def _jsonable(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 Timestamp / NaN 转成 JSON 可序列化的值。"""
    for item in records:
        for key, value in list(item.items()):
            item[key] = _clean(value)
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

    # 只读最近 250 个交易日：详情页画一年曲线足够，
    # 全量 read() 要扫几千个按日分区的文件，会慢到几秒。
    df = store.read_tail(version, days=250, codes=[code])
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

    return ok(
        {
            "latest": _jsonable([latest])[0],
            "history": history,
            "meta": meta,
            "version": version,
            "kline": _kline(code, latest.get("trade_date")),
            "indicators": _indicators(code, latest.get("trade_date")),
        }
    )


def _kline(code: str, asof: Any = None) -> List[Dict[str, Any]]:
    """近 ``KLINE_DAYS`` 个交易日的**前复权**日线（画 K 线用）。

    展示口径是前复权：后复权价会把茅台画成几千元、把格力的早期价格放大
    上百倍，用户在 K 线上找不到熟悉的价位。前复权 = 不复权价 × f(t)/f(最新)。
    这里只用当日快照往前取一段，f(最新) 由这段数据里的最大日期决定，
    所以每只票的「最新」是它自己的最后交易日（停牌股也不会画出断崖）。

    ``asof`` 是打分快照日，必须作为**时间基准**：早先这里锚在
    ``pd.Timestamp.today()``，导致「翻看历史某天的打分」时抽屉里画的是
    今天的 K 线（未来函数），而数据湖的时间戳一旦与实际日期拉开
    （补跑历史、或测试用固定日期）就干脆返回空数组。
    """
    try:
        lake = get_lake()
        end = pd.Timestamp(asof).normalize() if asof is not None else pd.Timestamp.today().normalize()
        # 回看窗口要足够宽：120 个交易日 ≈ 半年，但如果这只票期间停牌过，
        # 或用户翻看的是很久以前的快照，按「交易日数 × 2」自然日去切会把
        # 需要的 K 线切掉。这里给 3 倍 + 90 天缓冲，再 tail(120) 兜底。
        start = (end - pd.Timedelta(days=KLINE_DAYS * 3 + 90)).strftime("%Y-%m-%d")
        one = lake.read(
            "bars",
            columns=["date", "code", "open", "high", "low", "close", "volume"],
            start=start,
            end=end.strftime("%Y-%m-%d"),
            filters=[("code", "=", code)],
        )
    except Exception:
        return []
    if one is None or one.empty:
        return []
    one["date"] = pd.to_datetime(one["date"])
    one = one.sort_values("date").tail(KLINE_DAYS)

    # 前复权系数 = f(t) / f(最新)。
    #
    # ⚠️ 必须按**日期**去查因子表，不能拿 ``one.index`` 当对齐键：
    # 早期版本写成 ``pd.Series(1.0, index=one.index)`` 作兜底、
    # 又把 ``map`` 的结果赋回 DataFrame 的位置索引，一旦 index 不是日期，
    # 整列都取不到值 → 全部退化成 1.0 → 画出来的是**不复权**价，
    # 除权日一条腰斩的假跳空。这里统一用 DatetimeIndex 对齐，缺值按
    # 「最近一次已知因子」前值填充（除权日之间因子恒定）。
    factors = _factor_lookup(code)
    dates = pd.DatetimeIndex(one["date"])
    if factors is None or factors.empty:
        ratio = np.ones(len(one), dtype="float64")
    else:
        aligned = factors.reindex(factors.index.union(dates)).ffill().reindex(dates)
        last = aligned.iloc[-1] if len(aligned) else np.nan
        if not np.isfinite(last) or last <= 0:
            ratio = np.ones(len(one), dtype="float64")
        else:
            ratio = (aligned / last).fillna(1.0).to_numpy(dtype="float64")

    out: List[Dict[str, Any]] = []
    for (_, row), k in zip(one.iterrows(), ratio):
        def px(col):
            value = row.get(col)
            return _clean(float(value) * float(k)) if pd.notna(value) else None

        out.append(
            {
                "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                "open": px("open"),
                "high": px("high"),
                "low": px("low"),
                "close": px("close"),
                "volume": _clean(row.get("volume")),
            }
        )
    return out


def _factor_lookup(code: str):
    """后复权因子序列 ``f(t)``（按除权日展开成日频，缺口前值填充）。

    返回 ``None`` 表示没有复权因子数据，调用方应回退成「不复权」。
    """
    try:
        # 过滤下推到 Parquet：整表 20 万行全拉进 pandas 再切片要 0.7s，
        # 现在 0.05s。
        one = get_lake().read(
            "adj_factor",
            columns=["code", "ex_date", "back_factor"],
            filters=[("code", "=", code)],
        )
    except Exception:
        return None
    if one is None or one.empty:
        return None
    one = one.copy()
    one["ex_date"] = pd.to_datetime(one["ex_date"])
    one = one.sort_values("ex_date")
    series = pd.Series(
        pd.to_numeric(one["back_factor"], errors="coerce").to_numpy(dtype="float64"),
        index=pd.DatetimeIndex(one["ex_date"]),
    )
    series = series[~series.index.duplicated(keep="last")].dropna()
    if series.empty:
        return None
    # 除权日之前没有记录 → 用首个已知因子兜底（合理近似：上市首日因子≈1）
    series = pd.concat([pd.Series([series.iloc[0]], index=pd.DatetimeIndex([pd.Timestamp("1990-01-01")])), series])
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def _indicators(code: str, trade_date: Any) -> Dict[str, Any]:
    """估值 / 财务 / 波动率快照（抽屉「关键指标」用）。"""
    out: Dict[str, Any] = {}
    asof = pd.Timestamp(trade_date) if trade_date is not None else pd.Timestamp.today()

    # --- 估值：取 asof 当日（含）之前最近一条 ---
    try:
        one = get_lake().read(
            "valuation", columns=["date", "code", "industry", *_VALUATION_COLS],
            start=(asof - pd.Timedelta(days=20)).strftime("%Y-%m-%d"),
            end=asof.strftime("%Y-%m-%d"),
            filters=[("code", "=", code)],
        )
        if one is not None and not one.empty:
            one = one.sort_values("date")
            if not one.empty:
                row = one.iloc[-1]
                out["valuation"] = {
                    "date": pd.Timestamp(row["date"]).strftime("%Y-%m-%d"),
                    **{k: _clean(row.get(k)) for k in (k for k in _VALUATION_COLS if k in row.index)},
                    "industry": _clean(row.get("industry")),
                }
    except Exception:
        pass

    # --- 财务：报告期 ≤ asof 的最新一条（避免用到未来披露的数据） ---
    try:
        one = get_lake().read(
            "fundamental",
            columns=["code", "report_date", "notice_date", *_FUNDAMENTAL_COLS],
            filters=[("code", "=", code)],
        )
        if one is not None and not one.empty:
            one = one.copy()
            if not one.empty:
                one["report_date"] = pd.to_datetime(one["report_date"])
                one["notice_date"] = pd.to_datetime(one["notice_date"])
                known = one[(one["notice_date"].isna()) | (one["notice_date"] <= asof)]
                if known.empty:
                    known = one
                row = known.sort_values("report_date").iloc[-1]
                out["fundamental"] = {
                    "report_date": pd.Timestamp(row["report_date"]).strftime("%Y-%m-%d"),
                    **{k: _clean(row.get(k)) for k in _FUNDAMENTAL_COLS if k in row.index},
                }
    except Exception:
        pass

    # --- ATR%：近 60 个交易日真实波幅 / 收盘价 ---
    try:
        end = asof.strftime("%Y-%m-%d")
        start = (asof - pd.Timedelta(days=140)).strftime("%Y-%m-%d")
        one = get_lake().read(
            "bars", columns=["date", "code", "high", "low", "close"],
            start=start, end=end, filters=[("code", "=", code)],
        )
        if one is not None and not one.empty:
            one = one.sort_values("date").tail(60)
            if len(one) >= 5:
                high = pd.to_numeric(one["high"], errors="coerce")
                low = pd.to_numeric(one["low"], errors="coerce")
                close = pd.to_numeric(one["close"], errors="coerce")
                prev = close.shift(1)
                tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
                atr = float(tr.tail(20).mean())
                last_close = float(close.iloc[-1])
                out["atr"] = {
                    "atr": _clean(round(atr, 4)),
                    "close": _clean(round(last_close, 4)),
                    "atr_pct": _clean(round(atr / last_close * 100, 2)) if last_close else None,
                    "days": int(len(one)),
                }
    except Exception:
        pass

    return out



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
