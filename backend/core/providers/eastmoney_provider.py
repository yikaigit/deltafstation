"""东方财富数据源：估值横截面、财务报表（含公告日）。

两个报表
--------
``RPT_VALUEANALYSIS_DET``
    按 ``TRADE_DATE`` 取**全市场当日横截面**（实测 2026-10-16 返回 5577 只），
    含 PE/PB/PS/PEG/市值与 ``BOARD_NAME`` 行业 —— 一次请求就能拿到打分需要的估值维。

``RPT_LICO_FN_CPD``
    按 ``SECURITY_CODE`` 取单只标的全部历史报告，**含 ``NOTICE_DATE``（公告日）**。
    这是本系统能"按公告日对齐财务"的关键：用 REPORTDATE 会产生 1~3 个月的前视。

接口特点
--------
* 同一 base url，用 ``reportName`` 区分报表；
* 分页：``result.count`` / ``result.pages``，pageSize 上限实测 5000；
* 字段名大写蛇形，值为数字或 null；日期为 ``"2026-06-30 00:00:00"`` 字符串。
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

import pandas as pd

from ..codes import normalize_bs_code, to_em_code
from .base import ProviderError, RateLimiter, get_json, make_session, retry

logger = logging.getLogger(__name__)

BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

REPORT_VALUATION = "RPT_VALUEANALYSIS_DET"
REPORT_FINANCIAL = "RPT_LICO_FN_CPD"

#: 单页最大条数（实测 5000 可用）
PAGE_SIZE = 5000

#: 估值字段映射：东财列名 → 数据湖列名
VALUATION_MAP = {
    "SECUCODE": "code",
    "TRADE_DATE": "date",
    "CLOSE_PRICE": "close",
    "PE_TTM": "pe_ttm",
    "PB_MRQ": "pb_mrq",
    "PS_TTM": "ps_ttm",
    "PEG_CAR": "peg",
    "TOTAL_MARKET_CAP": "total_mv",
    "NOTLIMITED_MARKETCAP_A": "float_mv",
    "BOARD_NAME": "industry",
}

#: 财务字段映射：东财列名 → 数据湖列名
FINANCIAL_MAP = {
    "SECUCODE": "code",
    "REPORTDATE": "report_date",
    "NOTICE_DATE": "notice_date",
    "WEIGHTAVG_ROE": "roe_wa",
    "BASIC_EPS": "eps",
    "BPS": "bps",
    "TOTAL_OPERATE_INCOME": "rev",
    "YSTZ": "rev_yoy",
    "PARENT_NETPROFIT": "net_profit",
    "SJLTZ": "profit_yoy",
    "XSMLL": "gross_margin",
    "MGJYXJJE": "ocf_ps",
    "ZXGXL": "div_yield",
}

NUMERIC_VALUATION = ("close", "pe_ttm", "pb_mrq", "ps_ttm", "peg", "total_mv", "float_mv")
NUMERIC_FINANCIAL = (
    "roe_wa", "eps", "bps", "rev", "rev_yoy",
    "net_profit", "profit_yoy", "gross_margin", "ocf_ps", "div_yield",
)


class EastmoneyProvider:
    """东财 datacenter 封装。"""

    def __init__(self, min_interval: float = 0.3, timeout: float = 25.0):
        self.limiter = RateLimiter(min_interval)
        self.session = make_session(timeout=timeout)

    # ---------- 底层 ----------

    def _fetch_page(self, report: str, filter_expr: str, page: int,
                    page_size: int, sort_columns: str = "", sort_types: int = 1) -> dict:
        params = {
            "reportName": report,
            "columns": "ALL",
            "filter": filter_expr,
            "pageNumber": page,
            "pageSize": page_size,
            "source": "WEB",
            "client": "WEB",
        }
        if sort_columns:
            params["sortColumns"] = sort_columns
            params["sortTypes"] = sort_types
        self.limiter.wait()
        payload = get_json(self.session, BASE_URL, params=params)
        if not payload.get("success", False):
            message = payload.get("message") or "未知错误"
            raise ProviderError(f"{report} 查询失败：{message}（filter={filter_expr}）")
        return payload

    def _fetch_all(self, report: str, filter_expr: str, max_pages: int = 40) -> pd.DataFrame:
        """按页拉全量。"""
        first = self._fetch_page(report, filter_expr, page=1, page_size=PAGE_SIZE)
        result = first.get("result") or {}
        rows: List[dict] = list(result.get("data") or [])
        total_pages = int(result.get("pages") or 1)

        for page in range(2, min(total_pages, max_pages) + 1):
            payload = self._fetch_page(report, filter_expr, page=page, page_size=PAGE_SIZE)
            chunk = (payload.get("result") or {}).get("data") or []
            if not chunk:
                break
            rows.extend(chunk)

        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows)

    # ---------- 估值 ----------

    @retry(times=3, delay=2.0)
    def valuation_snapshot(self, trade_date: str) -> pd.DataFrame:
        """某交易日的全市场估值横截面。

        返回列：date / code / close / pe_ttm / pb_mrq / ps_ttm / peg / total_mv / float_mv / industry
        """
        day = str(trade_date)[:10]
        raw = self._fetch_all(REPORT_VALUATION, f"(TRADE_DATE='{day}')")
        if raw.empty:
            return pd.DataFrame(columns=list(VALUATION_MAP.values()))
        return _map_valuation(raw, day)

    # ---------- 财务 ----------

    @retry(times=3, delay=2.0)
    def financials(self, code: str) -> pd.DataFrame:
        """单只标的的全部历史报告（季频），含公告日。"""
        em = to_em_code(code)
        raw = self._fetch_all(
            REPORT_FINANCIAL, f'(SECURITY_CODE="{em}")', max_pages=10
        )
        if raw.empty:
            return pd.DataFrame(columns=list(FINANCIAL_MAP.values()) + ["notice_estimated"])
        return _map_financial(raw)

    def many_financials(
        self, codes: Sequence[str], on_item=None
    ) -> tuple[pd.DataFrame, List[dict]]:
        """批量拉财务（HTTP 限速串行，单只容错）。"""
        frames = []
        failures: List[dict] = []
        for index, code in enumerate(codes, 1):
            try:
                frame = self.financials(code)
                if not frame.empty:
                    frames.append(frame)
                if on_item:
                    on_item(index, len(codes), code, len(frame))
            except Exception as exc:
                logger.warning("拉取 %s 财务失败：%s", code, exc)
                failures.append({"code": normalize_bs_code(code), "error": str(exc)})
        merged = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=list(FINANCIAL_MAP.values()) + ["notice_estimated"])
        )
        return merged, failures


# ---------- 纯函数：字段映射 ----------


def _map_valuation(raw: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    out = pd.DataFrame()
    for src, dst in VALUATION_MAP.items():
        if src == "TRADE_DATE":
            out["date"] = pd.to_datetime(raw[src], errors="coerce") if src in raw.columns else pd.Timestamp(trade_date)
        elif src == "SECUCODE":
            out["code"] = raw[src].map(_secucode_to_bs) if src in raw.columns else None
        elif src == "BOARD_NAME":
            out["industry"] = raw[src] if src in raw.columns else None
        elif src in raw.columns:
            out[dst] = pd.to_numeric(raw[src], errors="coerce")
        else:
            out[dst] = pd.NA
    if "date" in out.columns:
        out["date"] = out["date"].fillna(pd.Timestamp(trade_date))
    out = out.dropna(subset=["code"])
    return out.drop_duplicates(["date", "code"], keep="last").reset_index(drop=True)


def _map_financial(raw: pd.DataFrame) -> pd.DataFrame:
    out = pd.DataFrame()
    for src, dst in FINANCIAL_MAP.items():
        if src == "SECUCODE":
            out["code"] = raw[src].map(_secucode_to_bs) if src in raw.columns else None
        elif src in ("REPORTDATE", "NOTICE_DATE"):
            out[dst] = pd.to_datetime(raw[src], errors="coerce") if src in raw.columns else pd.NaT
        elif src in raw.columns:
            out[dst] = pd.to_numeric(raw[src], errors="coerce")
        else:
            out[dst] = pd.NA

    out = out.dropna(subset=["code", "report_date"])
    # 同一报告期可能有更正版，保留最后一条（东财按时间倒序返回，ISNEW 标最新）
    out = out.drop_duplicates(["code", "report_date"], keep="first")

    # 公告日缺失时按法定披露截止日回退，并打标（规格 §4.3）
    missing = out["notice_date"].isna()
    if missing.any():
        out.loc[missing, "notice_date"] = out.loc[missing, "report_date"].map(statutory_notice_deadline)
    out["notice_estimated"] = missing
    return out.sort_values(["code", "report_date"]).reset_index(drop=True)


def statutory_notice_deadline(report_date) -> Optional[pd.Timestamp]:
    """法定披露截止日回退规则：Q1/Q3 +30 天、H1 +60 天、年报 +90 天。"""
    if pd.isna(report_date):
        return None
    ts = pd.Timestamp(report_date)
    month, day = ts.month, ts.day
    if month == 12:          # 年报
        offset = 90
    elif month == 6:         # 半年报
        offset = 60
    else:                    # 一季报 / 三季报
        offset = 30
    return ts + pd.Timedelta(days=offset)


def _secucode_to_bs(value) -> Optional[str]:
    """``000001.SZ`` → ``sz.000001``；``600519.SH`` → ``sh.600519``。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text:
        return None
    return normalize_bs_code(text)


_provider: Optional[EastmoneyProvider] = None


def get_eastmoney() -> EastmoneyProvider:
    global _provider
    if _provider is None:
        _provider = EastmoneyProvider()
    return _provider
