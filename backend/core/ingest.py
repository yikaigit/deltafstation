"""数据摄入编排：回填、增量更新、校验。

对外两个主流程
--------------
``backfill_all()``   首次建仓：日历 → 成分 → 日线 → 复权因子 → 估值历史 → 财务 → 校验
``update_daily()``   每日增量：判断交易日 → 成分 → 日线 → 因子 → 当日估值 → 财务 → 校验

两条流程共用同一批原子步骤，每步都是幂等的：
写入按主键 upsert（读旧 → 合并 keep last → 原子替换），重跑不产生重复行。

断点续传
--------
日线增量按「每只标的本地 max(date)+1」决定起点，因此中断后重跑会自动跳过已完成部分，
不需要额外的进度文件。

失败策略
--------
单只标的失败**不中断整体**：记入 ``failures`` 并继续，最后汇总进返回值与 job_runs。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

from . import providers
from .adjustment import check_factor_integrity
from .calendar import Calendar, get_calendar
from .config import Config, get_config
from .lake import Lake, get_lake
from .universe import Universe, get_universe
from .validator import LEVEL_ERROR, validate_dataframe

logger = logging.getLogger(__name__)

ProgressFn = Optional[Callable[[float, str], None]]

#: 日线分批写入的批大小（控制内存与单次落盘耗时，同时限制崩溃时的损失）
BAR_BATCH = 50

#: 校验门禁：ERROR 标的占比超过该值即标记 degraded
DEGRADED_ERROR_RATIO = 0.02


def _emit(progress: ProgressFn, ratio: float, message: str) -> None:
    if progress:
        progress(ratio, message)


def _day(value) -> str:
    """统一成 ``YYYY-MM-DD``。

    baostock 对带时分秒的日期会直接报「日期格式不正确」，
    而 ``str(pd.Timestamp(...))`` 恰恰带 ``00:00:00`` —— 这是最容易踩的坑，
    所有传给数据源的日期都必须过这里。
    """
    if value is None:
        return ""
    return pd.Timestamp(value).strftime("%Y-%m-%d")


class Ingest:
    """数据摄入编排器。"""

    def __init__(
        self,
        cfg: Optional[Config] = None,
        lake: Optional[Lake] = None,
        calendar: Optional[Calendar] = None,
        universe: Optional[Universe] = None,
    ):
        self.cfg = cfg or get_config()
        self.lake = lake or get_lake()
        self.calendar = calendar or get_calendar()
        self.universe = universe or get_universe()

    # ================= 原子步骤 =================

    def refresh_calendar(self, start: str = "2008-01-01", end: Optional[str] = None,
                         progress: ProgressFn = None) -> dict:
        _emit(progress, -1.0, "更新交易日历")
        total = self.calendar.refresh(start, end)
        return {"rows": total, **self.calendar.coverage()}

    def refresh_universe(self, progress: ProgressFn = None, light: bool = False) -> dict:
        """更新标的池。

        Args:
            light: True 时只查一次成分接口，若快照日未变化就跳过重建。
                   每日任务必须用 light=True —— 完整重建要逐只拉基础信息（300 次调用，
                   约 2 分钟），而成分每半年才调整一次（规格 §4.5 第 3 步：月度更新）。
        """
        _emit(progress, -1.0, "更新标的池")
        if light:
            members = providers.get_baostock().hs300_stocks()
            if members.empty:
                return {"skipped": True, "reason": "成分为空"}
            snapshot = pd.Timestamp(members["snapshot_date"].iloc[0])
            existing = set(self.universe.snapshots())
            if snapshot.strftime("%Y%m%d") in existing:
                return {
                    "skipped": True,
                    "reason": "成分未变化",
                    "snapshot_date": snapshot.strftime("%Y-%m-%d"),
                    "symbols": int(len(members)),
                }
        return self.universe.refresh(on_progress=progress)

    def refresh_valuation(self, trade_date: str, codes: Optional[Sequence[str]] = None,
                          progress: ProgressFn = None) -> dict:
        """拉某日全市场估值横截面（可按标的裁剪后入库）。"""
        day = _day(trade_date)
        _emit(progress, -1.0, f"拉取 {day} 估值横截面")
        frame = providers.get_eastmoney().valuation_snapshot(day)
        if frame.empty:
            return {"date": day, "rows": 0, "note": "当日无估值数据（可能非交易日或数据源未更新）"}
        if codes:
            frame = frame[frame["code"].isin(set(codes))]
        total = self.lake.write("valuation", frame)
        return {"date": day, "rows": int(len(frame)), "total": total}

    def refresh_fundamental(self, codes: Sequence[str], progress: ProgressFn = None) -> dict:
        """批量拉财务（含公告日）。"""
        provider = providers.get_eastmoney()

        def on_item(done: int, total: int, code: str, rows: int) -> None:
            _emit(progress, done / max(total, 1) * 0.9, f"财务 {done}/{total}（{code} {rows} 期）")

        frame, failures = provider.many_financials(codes, on_item=on_item)
        if frame.empty:
            return {"rows": 0, "failures": failures}
        total = self.lake.write("fundamental", frame)
        estimated = int(frame["notice_estimated"].sum()) if "notice_estimated" in frame else 0
        return {
            "rows": int(len(frame)),
            "total": total,
            "failures": failures,
            "notice_estimated": estimated,
        }

    def backfill_bars(self, codes: Sequence[str], start: str, end: Optional[str] = None,
                      progress: ProgressFn = None, incremental: bool = True) -> dict:
        """拉日线并分批入库。

        Args:
            incremental: True 时每只从本地 ``max(date)+1`` 开始（断点续传/增量）；
                         False 时从 ``start`` 全量重拉。
        """
        end = _day(end or pd.Timestamp.today())
        start = _day(start)
        latest = self._latest_dates() if incremental else {}
        provider = providers.get_baostock()

        frames: List[pd.DataFrame] = []
        failures: List[dict] = []
        skipped = 0
        rows_fetched = 0

        # 关键：整个循环共用一次 login。baostock 的 login/logout 各是一次网络往返，
        # 逐只开关连接会让 300 只标的凭空多出 600 次往返（实测慢 3~5 倍）。
        with provider.session():
            for index, code in enumerate(codes, 1):
                begin = start
                if incremental and code in latest:
                    last = pd.Timestamp(latest[code])
                    if last >= pd.Timestamp(end):
                        skipped += 1
                        _emit(progress, index / len(codes) * 0.95, f"日线 {index}/{len(codes)}（已最新）")
                        continue
                    begin = (last + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                try:
                    frame = provider.daily_bars(code, begin, end)
                    if not frame.empty:
                        frames.append(frame)
                        rows_fetched += len(frame)
                except Exception as exc:
                    logger.warning("拉取 %s 日线失败：%s", code, exc)
                    failures.append({"code": code, "error": str(exc)})

                if len(frames) >= BAR_BATCH:
                    self.lake.write("bars", pd.concat(frames, ignore_index=True))
                    frames = []

                _emit(progress, index / len(codes) * 0.95, f"日线 {index}/{len(codes)}")

        if frames:
            self.lake.write("bars", pd.concat(frames, ignore_index=True))

        # 注意：Lake.write 返回的是「写入后分区总行数」，不是本次新增行数。
        # 分批写时把它累加会得到虚高数字（实测报 383 万，实际只有 104.8 万），
        # 所以这里统计的是**本次从数据源拿到的行数**，库存总量另由 coverage() 报。
        return {
            "requested": len(codes),
            "skipped_latest": skipped,
            "failures": failures,
            "rows_fetched": rows_fetched,
            "rows_written": rows_fetched,
            "start": start,
            "end": end,
        }

    def backfill_index(self, codes: Optional[Sequence[str]] = None, start: str = "2008-01-01",
                       end: Optional[str] = None, progress: ProgressFn = None) -> dict:
        """拉基准指数日线。

        指数没有复权概念（``query_adjust_factor`` 对指数返回空），所以直接存不复权
        OHLCV 到独立的 ``index_bars`` 数据集 —— 不能混进 ``bars``，否则会把
        「300 只成分股」的统计口径污染成 301 只。

        用途：P2 的 ``rs_60`` 相对强度因子、P3 回测的超额收益基准。
        """
        codes = list(codes or [self.cfg.get("backtest.benchmark", "sh.000300")])
        start = _day(start)
        end = _day(end or self.calendar.latest_trading_day() or pd.Timestamp.today())
        provider = providers.get_baostock()

        frames: List[pd.DataFrame] = []
        failures: List[dict] = []
        with provider.session():
            for index, code in enumerate(codes, 1):
                try:
                    frame = provider.daily_bars(code, start, end)
                    if not frame.empty:
                        frames.append(frame)
                except Exception as exc:
                    logger.warning("拉取指数 %s 失败：%s", code, exc)
                    failures.append({"code": code, "error": str(exc)})
                _emit(progress, index / max(len(codes), 1), f"指数 {index}/{len(codes)}")

        if not frames:
            return {"codes": codes, "rows": 0, "failures": failures}
        merged = pd.concat(frames, ignore_index=True)
        self.lake.write("index_bars", merged)
        return {"codes": codes, "rows": int(len(merged)), "failures": failures}

    def backfill_factors(self, codes: Sequence[str], progress: ProgressFn = None) -> dict:
        """拉复权事件表（全历史，幂等 upsert）。"""
        provider = providers.get_baostock()

        def on_item(done: int, total: int, code: str, rows: int) -> None:
            _emit(progress, done / max(total, 1), f"复权因子 {done}/{total}（{code} {rows} 条）")

        frame, failures = provider.many_adjust_factors(codes, on_item=on_item)
        if frame.empty:
            return {"rows": 0, "failures": failures}
        total = self.lake.write("adj_factor", frame)
        integrity = check_factor_integrity(frame)
        return {"rows": int(len(frame)), "total": total, "failures": failures, "integrity": integrity}

    def backfill_valuation(self, start: str, end: Optional[str] = None,
                           codes: Optional[Sequence[str]] = None,
                           progress: ProgressFn = None) -> dict:
        """按交易日逐日回填估值历史。

        每个交易日一次请求（全市场横截面），因此耗时与年数成正比：
        5 年约 1200 个交易日，按 0.3s 限速约 10~15 分钟。
        """
        end = _day(end or self.calendar.latest_trading_day())
        days = self.calendar.trading_days(start, end)
        if len(days) == 0:
            return {"days": 0, "rows": 0, "note": "区间内无交易日"}

        existing = self._valuation_dates()
        frames: List[pd.DataFrame] = []
        failures: List[dict] = []
        skipped = 0
        rows = 0

        for index, day in enumerate(days, 1):
            key = pd.Timestamp(day).strftime("%Y-%m-%d")
            if key in existing:
                skipped += 1
                _emit(progress, index / len(days), f"估值 {index}/{len(days)}（已有，跳过）")
                continue
            try:
                result = self.refresh_valuation(key, codes=codes)
                if result["rows"] == 0:
                    failures.append({"date": key, "error": result.get("note", "空")})
                else:
                    rows += result["rows"]
            except Exception as exc:
                logger.warning("拉取 %s 估值失败：%s", key, exc)
                failures.append({"date": key, "error": str(exc)})
            _emit(progress, index / len(days), f"估值 {index}/{len(days)}（{key}）")

        return {
            "days": len(days),
            "skipped_existing": skipped,
            "rows": rows,
            "failures": failures[:50],
            "failure_count": len(failures),
        }

    # ================= 校验 =================

    def validate(self, trade_date: Optional[str] = None,
                 codes: Optional[Sequence[str]] = None,
                 progress: ProgressFn = None) -> dict:
        """对指定交易日（默认最新）跑 L1/L2 校验，产出 quality 报告。"""
        day = _day(trade_date or self.lake.latest_date("bars"))
        if not day:
            return {"available": False, "note": "本地无行情数据"}

        if codes is None:
            codes = self.universe.codes(in_hs300_only=True) or None

        frame = self.lake.read("bars", columns=["code"], start=day, end=day)
        if frame.empty:
            return {"available": False, "note": f"{day} 无行情数据"}
        present = set(frame["code"].astype(str).unique())
        if codes:
            present &= set(codes)
        if not present:
            return {"available": False, "note": f"{day} 无标的池内行情"}

        events = self.lake.read("adj_factor") if self.lake.exists("adj_factor") else pd.DataFrame()

        # 校验需要历史窗口（涨跌幅、日期间隔），只读最近 3 年，避免全量装载
        window_start = (pd.Timestamp(day) - pd.DateOffset(years=3)).strftime("%Y-%m-%d")
        history_all = self.lake.read("bars", start=window_start, end=day)
        history_all = history_all[history_all["code"].astype(str).isin(present)]

        # R2.8 要用交易日历判「是否漏拉」，否则春节/国庆长假会被误报成数据缺失
        calendar_days = self.calendar.trading_days(window_start, day)

        reports = []
        for index, (code, group) in enumerate(history_all.groupby("code"), 1):
            history = group.sort_values("date").tail(400)
            events_for_code = (
                events[events["code"].astype(str) == str(code)] if not events.empty else None
            )
            report = validate_dataframe(
                history, symbol=code, events=events_for_code, trading_days=calendar_days
            )
            reports.append(report.to_dict())
            _emit(progress, index / max(len(present), 1), f"校验 {index}/{len(present)}")

        error_symbols = [r for r in reports if not r["ok"]]
        ratio = len(error_symbols) / max(len(reports), 1)
        payload = {
            "trade_date": day,
            "symbols": len(reports),
            "error_symbols": len(error_symbols),
            "warn_symbols": sum(1 for r in reports if r["warn_count"] > 0),
            "error_ratio": round(ratio, 4),
            "degraded": ratio > DEGRADED_ERROR_RATIO,
            "reports": reports,
        }

        folder = Path(self.cfg.paths.quality)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"report_{day.replace('-', '')}.json"
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)

        return {**payload, "file": target.name}

    # ================= 主流程 =================

    def backfill_all(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        valuation_years: int = 5,
        with_fundamental: bool = True,
        progress: ProgressFn = None,
    ) -> dict:
        """首次建仓全流程。"""
        cfg_start = _day(start or self.cfg.get("universe.start_date", "2008-01-01"))
        end = _day(end or self.calendar.latest_trading_day() or pd.Timestamp.today())

        summary: Dict[str, object] = {"start": cfg_start, "end": end}

        # 1. 日历（后续所有日期判断都依赖它，必须最先做）
        summary["calendar"] = self.refresh_calendar(cfg_start, end, progress=_sub(progress, 0, 5))

        # 2. 标的池
        summary["universe"] = self.refresh_universe(progress=_sub(progress, 5, 15))
        codes = self.universe.codes(in_hs300_only=True, exclude_st=False)
        if not codes:
            raise RuntimeError("标的池为空，无法回填")
        summary["codes"] = len(codes)

        # 3. 日线
        summary["bars"] = self.backfill_bars(codes, cfg_start, end, progress=_sub(progress, 15, 52))

        # 3b. 基准指数（rs_60 与回测超额收益要用）
        summary["index"] = self.backfill_index(start=cfg_start, end=end,
                                              progress=_sub(progress, 52, 55))

        # 4. 复权因子
        summary["adj_factor"] = self.backfill_factors(codes, progress=_sub(progress, 55, 70))

        # 5. 估值历史（近 N 年）
        val_start = (pd.Timestamp(end) - pd.DateOffset(years=valuation_years)).strftime("%Y-%m-%d")
        summary["valuation"] = self.backfill_valuation(
            val_start, end, codes=codes, progress=_sub(progress, 70, 90)
        )

        # 6. 财务
        if with_fundamental:
            summary["fundamental"] = self.refresh_fundamental(codes, progress=_sub(progress, 90, 97))

        # 7. 校验
        summary["quality"] = self.validate(end, codes=codes, progress=_sub(progress, 97, 100))
        summary["coverage"] = self.coverage()
        return summary

    def update_daily(
        self,
        trade_date: Optional[str] = None,
        with_fundamental: bool = False,
        progress: ProgressFn = None,
    ) -> dict:
        """每日增量更新（规格 §4.5 的第 1~8 步；第 9 步打分属 P2）。"""
        target = pd.Timestamp(trade_date) if trade_date else pd.Timestamp.today()

        # 1. 判断交易日
        if not self.calendar.is_trading_day(target):
            return {
                "trade_date": target.strftime("%Y-%m-%d"),
                "skipped": True,
                "reason": "非交易日",
            }

        day = self.calendar.latest_trading_day(target) or target
        day_str = pd.Timestamp(day).strftime("%Y-%m-%d")
        summary: Dict[str, object] = {"trade_date": day_str}
        _emit(progress, 0.0, f"开始更新 {day_str}")

        # 2. 成分（light：成分快照日未变则跳过重建，避免每天 300 次基础信息调用）
        summary["universe"] = self.refresh_universe(progress=_sub(progress, 0, 15), light=True)
        codes = self.universe.codes(in_hs300_only=True, exclude_st=False)
        summary["codes"] = len(codes)

        # 3. 日线增量
        summary["bars"] = self.backfill_bars(codes, day_str, day_str, progress=_sub(progress, 15, 50))

        # 3b. 基准指数当日
        summary["index"] = self.backfill_index(start=day_str, end=day_str,
                                              progress=_sub(progress, 50, 55))

        # 4. 复权因子（有新事件才变化，幂等覆盖）
        summary["adj_factor"] = self.backfill_factors(codes, progress=_sub(progress, 55, 65))

        # 5. 当日估值
        summary["valuation"] = self.refresh_valuation(day_str, codes=codes,
                                                     progress=_sub(progress, 65, 75))

        # 6. 财务（财报季才需要，默认关闭以免每天 300 次请求）
        if with_fundamental:
            summary["fundamental"] = self.refresh_fundamental(codes, progress=_sub(progress, 75, 90))

        # 7. 校验
        summary["quality"] = self.validate(day_str, codes=codes, progress=_sub(progress, 90, 100))

        # 8. 当日数据是否真的到位（baostock 收盘后 1~2 小时才可用）
        landed = self._has_bars_on(day_str, codes)
        summary["bars_landed"] = landed
        if not landed:
            summary["pending"] = True
            summary["note"] = f"{day_str} 行情尚未到位，请稍后重试（调度器会在 22:00 前每 30 分钟重试）"

        summary["coverage"] = self.coverage()
        return summary

    # ================= 辅助 =================

    def fill_symbol(
        self,
        code: str,
        start: Optional[str] = None,
        progress: ProgressFn = None,
    ) -> dict:
        """单只标的补拉：日线 + 复权因子（+ 当日估值）。

        与 ``update_daily`` 的区别只有「范围」：这里 ``codes=[code]``，
        所以不会顺带重刷整个池子。这也是数据管理页「单个标的补拉」的后端。

        ``start`` 缺省时从本地已有数据的后一天开始（即纯增量）；
        本地从来没有这只票的数据才回落到全历史起点。
        """
        code = str(code).strip()
        if not code:
            raise ValueError("fill_symbol 需要 code")

        _emit(progress, 0.0, f"补拉 {code}")
        latest = self._latest_dates().get(code)
        if start:
            begin = _day(start)
        elif latest:
            begin = _day((pd.Timestamp(latest) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
        else:
            begin = "2008-01-01"

        summary: Dict[str, object] = {"code": code, "start": begin, "incremental_from": latest}
        if pd.Timestamp(begin) > pd.Timestamp.today().normalize():
            summary["skipped"] = True
            summary["reason"] = "已是最新，起始日晚于今天"
            return summary

        summary["bars"] = self.backfill_bars(
            [code], begin, None, progress=_sub(progress, 5, 70), incremental=False
        )
        summary["adj_factor"] = self.backfill_factors([code], progress=_sub(progress, 70, 90))

        # 估值是「全市场横截面」接口，这里按 code 裁剪只留这一只
        try:
            summary["valuation"] = self.refresh_valuation(
                self.calendar.latest_trading_day(pd.Timestamp.today()) or pd.Timestamp.today(),
                codes=[code],
                progress=_sub(progress, 90, 100),
            )
        except Exception as exc:  # 估值源偶发失败不该让整个补拉报错
            summary["valuation"] = {"error": str(exc)}
        return summary

    def _latest_dates(self) -> Dict[str, str]:
        """各标的本地最新日期（用于增量起点）。"""
        if not self.lake.exists("bars"):
            return {}
        coverage = self.lake.coverage("bars")
        if coverage.empty:
            return {}
        return {
            str(row["code"]): pd.Timestamp(row["last_date"]).strftime("%Y-%m-%d")
            for _, row in coverage.iterrows()
        }

    def _valuation_dates(self) -> set:
        if not self.lake.exists("valuation"):
            return set()
        frame = self.lake.read("valuation", columns=["date"])
        if frame.empty:
            return set()
        return {pd.Timestamp(d).strftime("%Y-%m-%d") for d in frame["date"].unique()}

    def _has_bars_on(self, day: str, codes: Sequence[str]) -> bool:
        """当日是否已有足够标的的行情（≥ 80% 视为到位）。"""
        if not self.lake.exists("bars"):
            return False
        frame = self.lake.read("bars", columns=["code"], start=day, end=day)
        if frame.empty:
            return False
        present = frame["code"].nunique()
        return present >= max(1, int(len(codes) * 0.8))

    def coverage(self) -> dict:
        """数据仓覆盖概况（供前端与报告使用）。"""
        result: Dict[str, object] = {}
        for name in ("bars", "index_bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"):
            if not self.lake.exists(name):
                result[name] = {"exists": False, "rows": 0}
                continue
            try:
                frame = (
                    self.lake.read(name, columns=["date"])
                    if name in ("bars", "index_bars", "valuation")
                    else self.lake.read(name)
                )
                info = {"exists": True, "rows": int(len(frame))}
                if name in ("bars", "index_bars", "valuation") and not frame.empty:
                    info["first_date"] = pd.Timestamp(frame["date"].min()).strftime("%Y-%m-%d")
                    info["last_date"] = pd.Timestamp(frame["date"].max()).strftime("%Y-%m-%d")
                result[name] = info
            except Exception as exc:
                result[name] = {"exists": True, "error": str(exc)}

        if self.lake.exists("bars"):
            bars = self.lake.coverage("bars")
            if not bars.empty:
                result["symbols"] = int(len(bars))
                result["avg_rows"] = int(bars["rows"].mean())
        result["calendar"] = self.calendar.coverage()
        return result


def _sub(progress: ProgressFn, lo: float, hi: float) -> ProgressFn:
    """把子步骤的 0~1 进度映射到整体 [lo, hi] 区间。"""
    if progress is None:
        return None

    def wrapped(ratio: float, message: str) -> None:
        if ratio is None or ratio < 0:
            progress(-1.0, message)
            return
        progress(lo + (hi - lo) * max(0.0, min(1.0, ratio)), message)

    return wrapped


def get_ingest() -> Ingest:
    return Ingest(get_config(), get_lake(), get_calendar(), get_universe())
