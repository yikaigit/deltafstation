"""收盘后补齐：缺口体检 → 补数据 → 判到位 → 打分 → 自检。

为什么需要这一层（而不只是 ``update_daily``）
--------------------------------------------
``update_daily`` 的语义是「把今天的数据对齐」：``backfill_bars(codes, day, day)``
只拉**当天**。所以它有一个致命的盲区 —— 昨天失败、前天也失败，今天跑成功之后，
昨天和前天**永远是空的**，而且没有任何地方会告诉你这件事。

P6 补的就是这个盲区：先拿**交易日历**和本地数据对账，算出到底缺哪几天，
再按区间去拉。这也是「点一下把所有缺失补齐」的真实含义。

方案边界（与用户确认过）
------------------------
* **只补「从最后一次成功日到今天」的连续区间**（方案 A），不做全历史空洞扫描。
  理由：停牌日本来就没数据，全量扫描会把停牌日当成缺口反复去拉、反复失败，
  而且慢。历史异常空洞只在体检报告里**列出来让人决定**，不自动动手。
* 数据没到位时**明确报 pending**，不假装成功。baostock 通常 17:00 后才出当日
  数据，15:30 点按钮拿到的是空帧 —— 如果返回「成功」，用户会以为已经有数据了。
  按用户选择，这里不自动重试，只告诉「稍后重试」。

判定「到位」的标准
------------------
不能只看「有没有行」。某天只拉到 3 只票的行情也算「有行」，但那天的打分是废的。
所以每个数据集各有自己的判据：

* ``bars``：当日有行情的标的数 ≥ 池子的 **80%**（与 ``ingest._has_bars_on`` 一致）
* ``index_bars``：基准指数当日有 1 行
* ``valuation``：当日横截面 ≥ 池子的 **50%**（估值接口偶发只返回部分板块）

80% 而不是 100%：停牌股本来就没有 bar，再加上 ST 剔除、退市等，
要求 100% 会让「正常的一天」永远判为未到位。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import Config, get_config
from .lake import Lake, get_lake

logger = logging.getLogger(__name__)

#: 行情到位阈值（占标的池的比例）
BARS_LANDED_RATIO = 0.80

#: 估值到位阈值
VALUATION_LANDED_RATIO = 0.50

#: 体检时默认回看的交易日数：超过这个范围的缺口当作「历史空洞」只报告不补。
#: 用户是本地自用，停机一周已经算极限；三个月的缺口应该是刻意不补。
DEFAULT_LOOKBACK_DAYS = 60


@dataclass
class DatasetGap:
    """单个数据集的缺口情况。"""

    name: str
    latest: Optional[str] = None
    missing_days: List[str] = field(default_factory=list)
    missing_count: int = 0
    #: 有缺口但超出回看窗口的日子（只报告，不自动补）
    stale_days: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "latest": self.latest,
            "missing_days": self.missing_days,
            "missing_count": self.missing_count,
            "stale_days": self.stale_days,
            "stale_count": len(self.stale_days),
        }


@dataclass
class GapReport:
    """体检结果。"""

    as_of: Optional[str] = None
    target: Optional[str] = None
    complete: bool = False
    datasets: Dict[str, DatasetGap] = field(default_factory=dict)
    #: 需要补的交易日（各数据集缺口的并集，升序）
    days_to_fill: List[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of,
            "target": self.target,
            "complete": self.complete,
            "datasets": {k: v.to_dict() for k, v in self.datasets.items()},
            "days_to_fill": self.days_to_fill,
            "days_to_fill_count": len(self.days_to_fill),
            "note": self.note,
        }


def _trading_days_between(calendar, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    """区间内的交易日（含首尾）。日历不可用时退化成工作日。"""
    try:
        days = calendar.trading_days(start, end)
        if len(days):
            return pd.DatetimeIndex(days)
    except Exception as exc:  # pragma: no cover
        logger.debug("交易日历不可用，退化为工作日：%s", exc)
    return pd.bdate_range(start, end)


def _present_dates(lake: Lake, name: str, start: str, end: str) -> set:
    """某数据集在区间内**已存在**的日期集合。

    用 pandas 去重而不是 groupby.count：这里只关心「这一天有没有」，
    不关心有几行（行数判据由 landed 检查单独负责）。
    """
    if not lake.exists(name):
        return set()
    try:
        frame = lake.read(name, columns=["date"], start=start, end=end)
    except Exception as exc:  # pragma: no cover
        logger.warning("读取 %s 失败，视为全部缺失：%s", name, exc)
        return set()
    if frame is None or frame.empty:
        return set()
    return set(pd.to_datetime(frame["date"]).dt.normalize().unique())


def gap_report(
    cfg: Optional[Config] = None,
    lake: Optional[Lake] = None,
    as_of: Optional[str] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> GapReport:
    """体检：本地数据相对交易日历缺了哪些天。

    Args:
        as_of: 基准日（默认「最近应该有数据的交易日」）
        lookback_days: 只自动补这么多交易日以内的缺口，更早的只报告

    设计说明
    --------
    起点不是「最后成功日」而是「回看窗口的起点」。原因：如果只从最后成功日往后看，
    中间断掉的那几天会被「最后成功日」这个值本身挡住 —— 而最后成功日恰恰是
    体检要发现的东西。这里统一按窗口扫，用「已存在日期集合」做差集，
    逻辑简单且不会漏。
    """
    from .calendar import get_calendar

    cfg = cfg or get_config()
    lake = lake or get_lake()
    calendar = get_calendar()

    today = pd.Timestamp(as_of).normalize() if as_of else pd.Timestamp.today().normalize()
    # 目标日 = 不晚于 as_of 的最近交易日。
    # 若今天是周六，目标就是周五 —— 周末点按钮不应该报「缺 2 天」。
    target = calendar.latest_trading_day(today)
    if target is None:  # pragma: no cover - 日历空时退化
        target = today
    target = pd.Timestamp(target).normalize()

    window_start = calendar.shift(target, -(int(lookback_days) - 1))
    if window_start is None:  # pragma: no cover
        window_start = target - pd.Timedelta(days=int(lookback_days * 1.6))
    window_start = pd.Timestamp(window_start).normalize()

    days = _trading_days_between(calendar, window_start, target)
    if len(days) == 0:
        return GapReport(as_of=str(today.date()), target=str(target.date()),
                         complete=True, note="区间内没有交易日")

    start_str = days[0].strftime("%Y-%m-%d")
    end_str = days[-1].strftime("%Y-%m-%d")
    expected = {pd.Timestamp(d).normalize() for d in days}

    report = GapReport(as_of=str(today.date()), target=str(target.date()))
    union: set = set()

    for name in ("bars", "index_bars", "valuation"):
        present = _present_dates(lake, name, start_str, end_str)
        missing = sorted(expected - present)
        latest_frame = None if not lake.exists(name) else lake.latest_date(name)
        gap = DatasetGap(
            name=name,
            latest=str(latest_frame) if latest_frame else None,
            missing_days=[pd.Timestamp(d).strftime("%Y-%m-%d") for d in missing],
            missing_count=len(missing),
        )
        report.datasets[name] = gap
        union |= set(missing)

    report.days_to_fill = sorted(pd.Timestamp(d).strftime("%Y-%m-%d") for d in union)
    report.complete = len(report.days_to_fill) == 0
    if report.complete:
        report.note = f"{start_str} ~ {end_str} 共 {len(days)} 个交易日，数据已齐"
    else:
        report.note = (
            f"{start_str} ~ {end_str} 共 {len(days)} 个交易日，"
            f"缺 {len(report.days_to_fill)} 天需要补"
        )
    return report


def landed_report(
    lake: Lake,
    day: str,
    codes: Sequence[str],
    benchmark: str = "sh.000300",
) -> Dict[str, object]:
    """某一天的数据是否**真的到位**（不只是「有行」）。

    这是 P6 最容易被忽略的一环：``backfill_bars`` 在 baostock 还没出数据时
    会返回 0 行，而不是报错。如果只看「任务成功」，用户会以为今天已经好了。
    """
    day = str(day)
    total = max(1, len(codes))
    result: Dict[str, object] = {"date": day, "codes": total}

    # bars：当日有行情的标的数
    bars_count = 0
    if lake.exists("bars"):
        frame = lake.read("bars", columns=["code"], start=day, end=day)
        if frame is not None and not frame.empty:
            bars_count = int(frame["code"].nunique())
    result["bars"] = bars_count
    result["bars_ratio"] = round(bars_count / total, 4)
    result["bars_ok"] = bars_count >= max(1, int(total * BARS_LANDED_RATIO))

    # index_bars：基准指数当日
    index_ok = False
    if lake.exists("index_bars"):
        frame = lake.read("index_bars", columns=["code"], start=day, end=day)
        if frame is not None and not frame.empty:
            index_ok = benchmark in set(frame["code"].astype(str))
    result["index_ok"] = index_ok

    # valuation：当日横截面
    val_count = 0
    if lake.exists("valuation"):
        frame = lake.read("valuation", columns=["code"], start=day, end=day)
        if frame is not None and not frame.empty:
            val_count = int(frame["code"].nunique())
    result["valuation"] = val_count
    result["valuation_ratio"] = round(val_count / total, 4)
    result["valuation_ok"] = val_count >= max(1, int(total * VALUATION_LANDED_RATIO))

    result["ok"] = bool(result["bars_ok"] and result["index_ok"])
    # 估值不参与「到位」硬判定：东财偶尔缺当日，但打分可以用前值（ffill 20 天）
    result["warning"] = None if result["valuation_ok"] else f"当日估值只有 {val_count}/{total} 只"
    return result


def catch_up(
    cfg: Optional[Config] = None,
    lake: Optional[Lake] = None,
    as_of: Optional[str] = None,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    score: bool = True,
    version: Optional[str] = None,
    progress=None,
    report: Optional[GapReport] = None,
) -> Dict[str, object]:
    """补齐 → 判到位 → 打分 → 自检。

    Returns 里 ``status`` 有三态：
    * ``complete``：本来就齐，什么都没做
    * ``updated``：补了 N 天并成功打分
    * ``pending``：数据源还没出当日数据，明确告诉用户稍后重试
    * ``partial``：部分天数补上了，但最新一天没到位
    """
    from .ingest import get_ingest
    from .scorer import get_scorer

    cfg = cfg or get_config()
    lake = lake or get_lake()
    benchmark = str(cfg.get("backtest.benchmark", "sh.000300"))

    def emit(ratio: float, message: str) -> None:
        if progress:
            progress(max(0.0, min(1.0, ratio)), message)

    report = report or gap_report(cfg=cfg, lake=lake, as_of=as_of,
                                 lookback_days=lookback_days)
    result: Dict[str, object] = {
        "gap": report.to_dict(),
        "filled_days": [],
        "status": "complete",
        "message": "",
    }

    ingest = get_ingest()
    # ⚠️ 标的池必须来自**传入的 lake**，不能直接问 ingest.universe：
    # 后者绑的是全局配置的数据湖，测试里传临时 lake 时会拿到 300 只真实标的，
    # 而临时湖里只有 3 只 —— 到位判定就会算成「3/300 = 1%，未到位」。
    # 生产环境两者一致，但这类「隐式依赖全局」正是测试覆盖不到的坑。
    codes = _universe_codes(lake, day=report.target or "")
    if not codes:
        raise RuntimeError("标的池为空，请先执行数据回填（scripts/backfill.py）")

    days = report.days_to_fill
    if not days:
        emit(0.15, "数据已齐，无需补拉")
    else:
        emit(0.05, f"需要补 {len(days)} 个交易日：{days[0]} ~ {days[-1]}")
        filled: List[str] = []
        for index, day in enumerate(days, 1):
            emit(0.05 + 0.45 * (index - 1) / len(days), f"补 {day}（{index}/{len(days)}）")
            try:
                ingest.backfill_bars(
                    codes, day, day,
                    progress=_sub(progress, 0.05 + 0.45 * (index - 1) / len(days),
                                  0.05 + 0.45 * index / len(days)),
                    incremental=False,
                )
                ingest.backfill_index(start=day, end=day)
                ingest.refresh_valuation(day, codes=codes)
                filled.append(day)
            except Exception as exc:  # 单日失败继续补后面，最后统一报告
                logger.warning("补 %s 失败：%s", day, exc)
        result["filled_days"] = filled

    # 复权因子：有新除权事件才变化，幂等覆盖。
    #
    # ⚠️ 只在**真的补了数据**时才刷新。这个接口要走 300 次网络往返，
    # 实测 90 秒；而绝大多数「点一下」的场景是「数据本来就齐」，
    # 为了一个不会被改动的表白等一分半完全没必要。
    if days:
        emit(0.52, "刷新复权因子")
        try:
            ingest.backfill_factors(codes)
        except Exception as exc:  # pragma: no cover
            logger.warning("刷新复权因子失败：%s", exc)
            result["adj_factor_error"] = str(exc)
    else:
        emit(0.52, "无新增交易日，跳过复权因子刷新")

    # 到位判定
    target = report.target
    emit(0.60, f"检查 {target} 数据是否到位")
    landed = landed_report(lake, target, codes, benchmark=benchmark)
    result["landed"] = landed

    if not landed["ok"]:
        result["status"] = "pending" if not days else "partial"
        result["message"] = (
            f"baostock 尚未提供 {target} 的完整行情"
            f"（已到 {landed['bars']}/{landed['codes']} 只），请稍后重试。"
            f"通常收盘后 17:00~18:00 才可用。"
        )
        return result

    result["status"] = "updated" if days else "complete"
    result["message"] = (
        f"已补齐 {len(days)} 个交易日（{days[0]} ~ {days[-1]}）" if days else "数据已是最新"
    )

    if not score:
        return result

    # 打分：对**缺口区间每一天**都打分，而不是只打最后一天 ——
    # 否则补了 3 天数据却只有 1 天有分数，第二天做回测会发现中间是空的。
    emit(0.65, "打分")
    scorer = get_scorer()
    score_days = days or [target]
    summaries = []
    for index, day in enumerate(score_days, 1):
        emit(0.65 + 0.25 * (index - 1) / len(score_days), f"打分 {day}（{index}/{len(score_days)}）")
        try:
            summaries.append(
                scorer.score_day(trade_date=day, version=version, write=True)
            )
        except Exception as exc:
            logger.warning("打分 %s 失败：%s", day, exc)
            summaries.append({"trade_date": day, "error": str(exc)})
    result["scored_days"] = [s.get("trade_date") or s.get("error") for s in summaries]

    # 自检：分数覆盖与六维完整度
    emit(0.92, "自检")
    result["self_check"] = self_check(lake, target, version=version)
    return result


def self_check(
    lake: Lake,
    day: str,
    version: Optional[str] = None,
) -> Dict[str, object]:
    """打分结果自检：分数覆盖数、六维非空率、data_quality 中位数。

    为什么要自检：打分任务「成功」不代表结果可用。如果估值表没拉到，
    估值维度会整片 NaN，``score_pct`` 照样能算出来（权重自动再归一），
    但那个分数其实是「四维」而不是「六维」—— 页面上看不出来。
    """
    from .snapshots import get_score_store

    store = get_score_store()
    version = version or store.versions()[-1] if store.versions() else None
    if version is None:
        return {"ok": False, "note": "没有任何打分版本"}

    try:
        frame = store.read(version=version, trade_date=day)
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "note": f"读取快照失败：{exc}"}
    if frame is None or frame.empty:
        return {"ok": False, "version": version, "note": f"{day} 没有打分快照"}

    dim_columns = [c for c in frame.columns if c.startswith("dim_")]
    dim_coverage = {
        c.replace("dim_", ""): round(float(frame[c].notna().mean()), 4)
        for c in dim_columns
    }
    quality = frame["data_quality"] if "data_quality" in frame.columns else None
    quality_median = (
        round(float(pd.to_numeric(quality, errors="coerce").median()), 4)
        if quality is not None else None
    )
    score_count = int(pd.to_numeric(frame["score_pct"], errors="coerce").notna().sum())

    # 异常判定：任一一维覆盖率 < 70% 视为该维度基本失效
    weak = [k for k, v in dim_coverage.items() if v < 0.7]
    return {
        "ok": len(weak) == 0,
        "version": version,
        "date": day,
        "count": score_count,
        "dimension_coverage": dim_coverage,
        "weak_dimensions": weak,
        "data_quality_median": quality_median,
        "note": (
            "各维度覆盖正常"
            if not weak
            else f"以下维度覆盖不足 70%：{', '.join(weak)}"
        ),
    }


def _universe_codes(lake: Lake, day: str = "") -> List[str]:
    """标的池：universe 表 → 最近一日 bars 里出现过的代码 → 全局 ingest。

    三层回落是必要的：
    * 生产环境永远走第一层；
    * 临时湖/测试里通常没有 universe 表，但**有 bars** —— 用 bars 里的代码
      当池子才是「这只湖里到底该有多少只」的正确答案。早期没有这一层时，
      临时湖会回落到全局 300 只，导致「3/300 → 未到位」的误判；
    * 只有连 bars 都没有（空库）时才问全局，只为给出「请先回填」的明确报错。
    """
    if lake.exists("universe"):
        try:
            frame = lake.read("universe", columns=["code"])
            if frame is not None and not frame.empty:
                return [str(c) for c in frame["code"].dropna().unique()]
        except Exception as exc:  # pragma: no cover
            logger.warning("读取 universe 失败，尝试用 bars 推断标的池：%s", exc)

    if lake.exists("bars"):
        try:
            latest = day or lake.latest_date("bars")
            frame = None
            if latest:
                frame = lake.read("bars", columns=["code"], start=latest, end=latest)
            if frame is None or frame.empty:
                frame = lake.read("bars", columns=["code"])
            if frame is not None and not frame.empty:
                return [str(c) for c in frame["code"].dropna().unique()]
        except Exception as exc:  # pragma: no cover
            logger.warning("用 bars 推断标的池失败：%s", exc)

    from .ingest import get_ingest

    return list(get_ingest().universe.codes(in_hs300_only=True, exclude_st=False))


def _sub(progress, start: float, end: float):
    """把外部进度回调压缩到 [start, end] 区间。"""
    if progress is None:
        return None

    def inner(ratio: float, message: str) -> None:
        ratio = 0.0 if ratio is None or ratio < 0 else min(1.0, ratio)
        progress(start + (end - start) * ratio, message)

    return inner
