"""打分编排：面板 → 因子 → 标准化 → 快照。

三个入口
--------
``score_range()``  全历史重算（首次建仓 / 改因子后重算）
``score_day()``    单日增量（每日任务，只算最近窗口）
``get_scorer()``   进程内单例

增量为什么要带历史窗口
----------------------
因子是滚动指标，只喂当天数据算不出 ``ma_bias_120``。所以增量模式也要往前
多读 ``LOOKBACK_DAYS`` 天，算完之后**只取最后一天**写快照。窗口取 300 天
（> 最大 lookback 260），保证每个因子在最后一天都是满窗口值。

版本与可复现
------------
版本配置存在 ``config/scoring/{version}.yaml``，连同数据快照一起记进
``score_versions`` 表。同一份数据 + 同一个版本 = 同样的结果（全程无随机数、
无并发、``rank`` 用固定 method）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
import yaml

from . import factors as factor_lib
from . import scoring
from .config import ROOT, Config, get_config
from .lake import Lake, get_lake
from .meta_db import get_meta_db
from .panels import Panels
from .snapshots import ScoreStore, get_score_store

logger = logging.getLogger(__name__)

#: 打分配置目录
SCORING_DIR = ROOT / "config" / "scoring"

#: 最大因子 lookback（交易日），增量窗口必须 ≥ 它
MAX_LOOKBACK = max((f.lookback for f in factor_lib.FACTORS.values()), default=250)

#: 增量窗口在最大 lookback 之外再留的交易日余量。
#:
#: 为什么需要 160 天这么多：MACD/ADX/RSI/ATR 都是**递归**指标
#: （EMA 与 Wilder 平滑），当前值理论上依赖全部历史。窗口只留 20 天时
#: 300 只里有 85 只的 score_pct 与全量重算不同（最大差 1.0 个百分位）；
#: 实测 400 个交易日（= 260 lookback + 140 余量）时差异归零，
#: 300/300 完全一致，耗时 1.4 秒仍在预算内。这里取 160 留一点安全边际。
LOOKBACK_BUFFER = 160

#: 快照列（与 snapshots.SCORE_COLUMNS 对齐，维度列动态补齐）
DIMENSION_COLUMNS = ["dim_trend", "dim_momentum", "dim_volume", "dim_risk",
                     "dim_valuation", "dim_quality"]


class Scorer:
    """打分器。"""

    def __init__(self, cfg: Optional[Config] = None, lake: Optional[Lake] = None,
                 store: Optional[ScoreStore] = None):
        self.cfg = cfg or get_config()
        self.lake = lake or get_lake()
        self.store = store or get_score_store()

    # ---------- 版本配置 ----------

    @staticmethod
    def available_versions() -> List[str]:
        if not SCORING_DIR.exists():
            return []
        return sorted(p.stem for p in SCORING_DIR.glob("*.yaml"))

    @staticmethod
    def load_version(version: str) -> dict:
        path = SCORING_DIR / f"{version}.yaml"
        if not path.exists():
            raise FileNotFoundError(f"找不到打分版本配置：{path}")
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        dimensions = raw.get("dimensions") or {}
        if not dimensions:
            raise ValueError(f"{version} 没有配置任何维度")

        weights: Dict[str, float] = {}
        dimension_of: Dict[str, str] = {}
        for name, spec in dimensions.items():
            spec = spec or {}
            weights[name] = float(spec.get("weight", 0.0))
            for factor in spec.get("factors") or []:
                dimension_of[factor] = name

        unknown = [f for f in dimension_of if f not in factor_lib.FACTORS]
        if unknown:
            raise ValueError(f"{version} 引用了未实现的因子：{unknown}")

        return {
            "version": raw.get("version", version),
            "description": raw.get("description", ""),
            "weights": weights,
            "dimension_of": dimension_of,
            "factors": list(dimension_of),
            "grades": raw.get("grades") or scoring.DEFAULT_GRADES,
            "raw": raw,
        }

    # ---------- 打分 ----------

    def score_range(
        self,
        version: Optional[str] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        codes: Optional[Sequence[str]] = None,
        write: bool = True,
        days: Optional[int] = None,
        progress=None,
    ) -> dict:
        """全历史重算。

        Args:
            days: 只把最近 N 个交易日写进快照（None 取配置 ``scoring.snapshot_days``）。
                  计算本身始终是全历史，这个参数只控制落盘规模 ——
                  快照按日一个文件，写 4500 天要建 4500 个目录，I/O 比计算还慢，
                  而回测默认只看最近 5 年，没必要为 2008 年的分数占着磁盘。
        """
        spec = self.load_version(version or self.cfg.get("scoring.default_version", "v1.0.0"))
        start = start or self.cfg.get("universe.start_date", "2008-01-01")
        if days is None:
            days = int(self.cfg.get("scoring.snapshot_days", 1250) or 0)

        _emit(progress, 0.02, "装配面板")
        panel = Panels.from_lake(lake=self.lake, cfg=self.cfg, codes=codes, start=start, end=end)
        logger.info("面板：%s", panel.describe())

        result = self._compute(panel, spec, progress=_sub(progress, 0.10, 0.75))

        summary = {
            "version": spec["version"],
            "mode": "range",
            "start": panel.describe()["first_date"],
            "end": panel.describe()["last_date"],
            "codes": int(len(panel.codes)),
            "dates": int(len(panel.dates)),
            "valid_dates": result["diagnostics"].valid_dates,
            "diagnostics": result["diagnostics"].to_dict(),
        }

        if write:
            _emit(progress, 0.80, "写入快照")
            written = self._persist(result, panel, spec, days=days,
                                    progress=_sub(progress, 0.80, 1.0))
            summary.update(written)

        return summary

    def score_day(
        self,
        trade_date: Optional[str] = None,
        version: Optional[str] = None,
        codes: Optional[Sequence[str]] = None,
        write: bool = True,
        progress=None,
    ) -> dict:
        """单日增量打分（自动带足够长的历史窗口）。

        ⚠️ 窗口必须覆盖**最大因子 lookback 个交易日**，否则滚动指标的历史长度
        与全量重算不同，同一天的分数会对不上 —— 那样「增量」和「重算」就是
        两套口径了。所以这里按交易日历精确回溯，而不是拍一个自然日数
        （300 自然日只有约 200 个交易日，根本不够 260 的 lookback）。
        """
        spec = self.load_version(version or self.cfg.get("scoring.default_version", "v1.0.0"))
        day = pd.Timestamp(trade_date) if trade_date else self.lake.latest_date("bars")
        if day is None or pd.isna(day):
            raise RuntimeError("本地没有行情数据，无法打分")
        day = pd.Timestamp(day)

        window_start = self._window_start(day)
        _emit(progress, 0.05, f"装配面板（{window_start} ~ {day:%Y-%m-%d}）")
        panel = Panels.from_lake(
            lake=self.lake, cfg=self.cfg, codes=codes,
            start=window_start, end=day.strftime("%Y-%m-%d"),
        )
        if day not in panel.dates:
            raise RuntimeError(f"{day:%Y-%m-%d} 不在行情日期内（可能不是交易日）")

        needed = MAX_LOOKBACK + LOOKBACK_BUFFER
        if len(panel.dates) < needed:
            logger.warning(
                "增量窗口只有 %d 个交易日（期望 ≥ %d），早期因子可能是短窗口值",
                len(panel.dates), needed,
            )

        result = self._compute(panel, spec, progress=_sub(progress, 0.15, 0.80))

        summary = {
            "version": spec["version"],
            "mode": "day",
            "trade_date": day.strftime("%Y-%m-%d"),
            "codes": int(len(panel.codes)),
            "window_days": int(len(panel.dates)),
            "diagnostics": result["diagnostics"].to_dict(),
        }

        if write:
            _emit(progress, 0.85, "写入快照")
            written = self._persist(result, panel, spec, only_dates=[day],
                                    progress=_sub(progress, 0.85, 1.0))
            summary.update(written)

        return summary

    def _window_start(self, day: pd.Timestamp) -> str:
        """按交易日历回溯 ``MAX_LOOKBACK + LOOKBACK_BUFFER`` 个交易日。"""
        span = MAX_LOOKBACK + LOOKBACK_BUFFER
        try:
            from .calendar import get_calendar

            start = get_calendar().shift(day, -span)
            if start is not None:
                return pd.Timestamp(start).strftime("%Y-%m-%d")
        except Exception as exc:  # 日历不可用时退化为自然日估算
            logger.debug("日历回溯失败，改用自然日估算：%s", exc)
        return (day - pd.Timedelta(days=int(span * 1.55))).strftime("%Y-%m-%d")

    # ---------- 内部 ----------

    def _compute(self, panel: Panels, spec: dict, progress=None) -> dict:
        names = spec["factors"]
        total = len(names)

        def on_factor(index: int, count: int, name: str) -> None:
            _emit(progress, 0.05 + 0.45 * index / max(count, 1), f"计算因子 {index}/{count}（{name}）")

        _emit(progress, 0.02, f"计算 {total} 个因子")
        frames = factor_lib.compute_many(names, panel, on_progress=on_factor)
        missing = [n for n in names if n not in frames]
        if missing:
            logger.warning("以下因子计算失败，将不参与合成：%s", missing)

        meta = {n: {"direction": factor_lib.FACTORS[n].direction} for n in frames}
        dimension_of = {n: spec["dimension_of"][n] for n in frames}

        def on_standardize(index: int, count: int, name: str) -> None:
            _emit(progress, 0.50 + 0.45 * index / max(count, 1), f"标准化 {index}/{count}（{name}）")

        return scoring.score_panels(
            frames,
            factor_meta=meta,
            dimension_of=dimension_of,
            weights=spec["weights"],
            industry=panel.industry,
            mad_k=float(self.cfg.get("scoring.winsor_mad", 5.0)),
            min_samples=int(self.cfg.get("scoring.min_industry_samples", 5)),
            grades=spec["grades"],
            on_progress=on_standardize,
        )

    def _persist(self, result: dict, panel: Panels, spec: dict,
                 only_dates: Optional[Sequence] = None,
                 days: Optional[int] = None,
                 progress=None) -> dict:
        """把打分结果转成长表写入快照。

        用 numpy 展平而不是 ``DataFrame.stack()``：
        14 个 4564×300 的面板 stack 一次要好几秒，还会触发 pandas 的
        ``FutureWarning``（旧 stack 实现已弃用）；``np.repeat``/``np.tile``
        是纯内存操作，同样的数据只要零点几秒。
        """
        frames = {
            "score_pct": result["score_pct"],
            "score_abs": result["score_abs"],
            "grade": result["grade"],
            "data_quality": result["data_quality"],
        }
        for dimension, frame in result["dims"].items():
            frames[f"dim_{dimension}"] = frame

        reference = result["score_pct"]
        dates = pd.DatetimeIndex(reference.index)
        codes = pd.Index(reference.columns)

        table = pd.DataFrame({
            "trade_date": np.repeat(dates.to_numpy(), len(codes)),
            "code": np.tile(codes.to_numpy().astype(str), len(dates)),
        })
        for name, frame in frames.items():
            table[name] = frame.to_numpy().reshape(-1)

        table = table[table["score_pct"].notna()]

        if only_dates:
            keep = {pd.Timestamp(d).normalize() for d in only_dates}
            table = table[table["trade_date"].isin(keep)]
        elif days:
            cutoff = dates[max(0, len(dates) - int(days))]
            table = table[table["trade_date"] >= cutoff]

        for column in DIMENSION_COLUMNS:
            if column not in table.columns:
                table[column] = np.nan

        table["code"] = table["code"].astype(str)
        table["grade"] = table["grade"].astype(object)
        table["trade_date"] = pd.to_datetime(table["trade_date"]).dt.normalize()

        # 与 universe 合并名称/行业，前端榜单直接可用（避免每次查询再 join）
        if self.lake.exists("universe"):
            uni = self.lake.read("universe", columns=["code", "name", "industry_l1", "board"])
            if not uni.empty:
                uni = uni.rename(columns={"industry_l1": "industry"})
                table = table.merge(uni, on="code", how="left")

        version = spec["version"]
        path = self.store.write(table, version=version)

        record_version(version, spec, panel, table)
        logger.info("快照写入 %s：%d 行 / %d 天", path, len(table), table["trade_date"].nunique())

        return {
            "rows": int(len(table)),
            "days_written": int(table["trade_date"].nunique()),
            "path": str(path),
            "distribution": _last_distribution(table),
        }


# ---------- 辅助 ----------


def _emit(progress, ratio: float, message: str) -> None:
    if progress:
        progress(ratio, message)


def _sub(progress, lo: float, hi: float):
    """把子步骤进度映射到整体区间。"""
    if progress is None:
        return None

    def mapped(ratio: float, message: str) -> None:
        if ratio is None or ratio < 0:
            progress(-1.0, message)
            return
        progress(lo + (hi - lo) * max(0.0, min(1.0, ratio)), message)

    return mapped


def _last_distribution(table: pd.DataFrame) -> Dict[str, int]:
    if table.empty:
        return {}
    last = table["trade_date"].max()
    counts = table.loc[table["trade_date"] == last, "grade"].value_counts()
    return {str(k): int(v) for k, v in counts.items()}


def record_version(version: str, spec: dict, panel: Panels, table: pd.DataFrame) -> None:
    """把版本与数据快照记进 meta.db（失败不影响打分）。"""
    try:
        db = get_meta_db()
        describe = panel.describe()
        db.add_score_version(
            version=version,
            config_yaml=yaml.safe_dump(spec.get("raw") or {}, allow_unicode=True, sort_keys=False),
            factor_set=list(spec["factors"]),
            data_snapshot=f"{describe.get('first_date')}~{describe.get('last_date')}"
                          f"@{describe.get('codes')}只",
            note=spec.get("description", ""),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("记录 score_versions 失败（不影响打分）：%s", exc)


_scorer: Optional[Scorer] = None


def get_scorer() -> Scorer:
    global _scorer
    if _scorer is None:
        _scorer = Scorer()
    return _scorer
