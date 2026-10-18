"""回测编排：装数据 → 定区间 → 主回测 → 参数网格 → 落盘。

流程
----
1. 从数据湖装配面板（后复权）与基准指数收盘价；
2. 从打分快照读出 ``date × code`` 的 score_pct 面板；
3. 定区间：默认最近 ``universe.backtest_years`` 年（规格与用户约定「先回测近 5 年」），
   并自动裁剪到**有打分快照**的日期——早于首个快照的日子没有信号，跑出来只是一条平线；
4. 跑一次主回测（用户给的参数）；
5. 参数网格（``top_n × rebal_days × regime`` = 32 组）在 IS 上按 Sharpe 寻优，
   最优参数的 OOS 一并汇报；
6. 结果写 ``data/results/backtests/bt_<时间戳>_<随机>.json``，并记一条 ``experiments``。

自洽校验
--------
全区间只跑一次回测，IS/OOS 指标由同一条净值曲线切出来，因此必然满足
``(1+IS)×(1+OOS)-1 == 全区间``。校验失败说明有未来函数或口径漂移，
会在 ``warnings`` 里明确报出来，而不是悄悄给一份对不上的报告。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from ..lake import Lake
from ..meta_db import get_meta_db
from ..panels import Panels
from ..snapshots import ScoreStore
from .costs import CostModel
from .engine import BacktestEngine, BacktestResult
from .metrics import downsample_equity
from .strategies import StrategyParams

logger = logging.getLogger(__name__)

#: 参数网格（规格 §6.6）
GRID_TOP_N = (5, 10, 20, 30)
GRID_REBAL_DAYS = (5, 10, 20, 60)
GRID_REGIME = (True, False)

#: 主回测的耗时预算（秒）。超过它就只跑主回测、跳过参数网格 ——
#: 网格是 32 次完整回测，主回测都要几分钟的话，网格会把请求挂上一小时。
#: 注意这里只管「要不要开跑网格」，管不住网格自己跑多久（见 run() 里的说明）。
GRID_TIME_BUDGET = 120.0


def _default_start(cfg, available_dates: Sequence[str]) -> Optional[str]:
    """默认起点 = 最后一个快照日往前 backtest_years 年。"""
    if not available_dates:
        return None
    last = pd.Timestamp(max(available_dates))
    years = float(cfg.get("universe.backtest_years", 5) or 5)
    return (last - pd.Timedelta(days=int(years * 365))).strftime("%Y-%m-%d")


def load_score_wide(store: ScoreStore, version: Optional[str], days: Optional[int] = None) -> tuple:
    """读打分快照 → (score_wide[date×code], 版本号, 快照日期列表)。

    ``days`` 非空时只读最近 N 个交易日（快照按日一文件，读 1250 个要好几秒）。
    """
    versions = store.versions()
    if not versions:
        raise RuntimeError("没有任何打分快照，请先执行 scripts/score.py")
    version = version or (versions[-1])
    if version not in versions:
        raise RuntimeError(f"打分版本 {version} 不存在，可用：{versions}")

    dates = store.dates(version)
    if not dates:
        raise RuntimeError(f"版本 {version} 下没有任何快照")
    if days:
        dates = dates[-int(days):]
    frame = store.read_tail(version, days=len(dates)) if days else store.read(version)
    if frame.empty:
        raise RuntimeError("打分快照为空")
    frame = frame[frame["code"].notna()].copy()
    frame["trade_date"] = pd.to_datetime(frame["trade_date"])
    wide = frame.pivot_table(index="trade_date", columns="code", values="score_pct")
    wide.index = pd.DatetimeIndex(wide.index)
    wide = wide.sort_index()
    return wide, version, dates


def result_to_payload(
    result: BacktestResult,
    params: StrategyParams,
    version: str,
    costs: CostModel,
    data_range: Dict[str, str],
    grid: Optional[List[dict]] = None,
) -> dict:
    """把回测结果整理成规格 §6.7 的 JSON 结构。"""
    metrics = result.metrics
    equity = downsample_equity(result.equity, max_points=1000)
    payload = {
        "id": "",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": "score_portfolio",
        "score_version": version,
        "params": params.to_dict(),
        "costs": costs.describe(),
        "period": {
            "start": data_range.get("start"),
            "end": data_range.get("end"),
            "is_start": result.period.get("is_start"),
            "is_end": result.period.get("is_end"),
            "oos_start": result.period.get("oos_start"),
            "oos_end": result.period.get("oos_end"),
        },
        "data_range": data_range,
        "equity": equity,
        "metrics": {
            "is": metrics.get("is"),
            "oos": metrics.get("oos"),
            "full": metrics.get("full"),
        },
        "trades": result.trades,
        "holdings": result.holdings,
        "blocked": result.blocked,
        "rebalance_count": result.rebalance_count,
        "turnover": round(result.turnover, 2),
        "avg_positions": round(result.avg_positions, 2),
        "grid": grid or [],
        "warnings": list(result.warnings),
    }
    return payload


def save_payload(payload: dict, folder: Path) -> Path:
    """落盘并回填 id（``bt_<日期>_<时间>_<随机4位>``）。"""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bt_id = f"bt_{stamp}_{uuid.uuid4().hex[:4]}"
    payload["id"] = bt_id
    path = folder / f"{bt_id}.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
    tmp.replace(path)
    return path


def record_experiment(payload: dict) -> Optional[str]:
    """把回测记进 experiments 表（失败不影响回测本身）。"""
    try:
        db = get_meta_db()
        params = payload.get("params", {})
        return db.add_experiment(
            name=f"backtest_{payload.get('id')}",
            hypothesis=f"top_n={params.get('top_n')} rebal_days={params.get('rebal_days')} "
                       f"regime={params.get('regime')}",
            factor_set=[f"score:{payload.get('score_version')}"],
            weights={
                "weight_method": params.get("weight_method"),
                "max_weight": params.get("max_weight"),
            },
            start_date=(payload.get("period") or {}).get("start"),
            end_date=(payload.get("period") or {}).get("end"),
            metrics=payload.get("metrics", {}),
        )
    except Exception as exc:  # pragma: no cover
        logger.warning("记录 experiments 失败（不影响回测）：%s", exc)
        return None


def run(
    payload: Optional[dict] = None,
    cfg=None,
    lake: Optional[Lake] = None,
    store: Optional[ScoreStore] = None,
    progress=None,
    grid: bool = True,
) -> dict:
    """跑一次回测，返回 {**payload, path, experiment_id}。"""
    from ..config import get_config

    cfg = cfg or get_config()
    # lake/store 必须跟着 cfg 走，不能回落到全局配置：调用方显式传了 cfg
    # （CLI/API/测试都这么干）却从别处读数据，会出现「结果写到 A 目录、
    # 数据读自 B 目录」的错配，而且让单测无法隔离 —— 实测会静默读到真实
    # 仓库的 1250 天快照，既慢又在空库里给出错误结果。
    lake = lake or Lake(cfg)
    store = store or ScoreStore(cfg)

    def emit(ratio: float, message: str) -> None:
        if progress:
            progress(ratio, message)

    started = time.monotonic()
    params = StrategyParams.from_payload(payload, cfg=cfg)
    costs = CostModel.from_config(cfg)

    emit(0.05, "读取打分快照")
    score_wide, version, score_dates = load_score_wide(store, params.score_version)
    params.score_version = version

    # 区间：默认最近 N 年，并裁剪到有快照的日期
    if not params.start:
        params.start = _default_start(cfg, score_dates)
    usable = [d for d in score_dates
              if (params.start is None or d >= params.start)
              and (params.end is None or d <= params.end)]
    if not usable:
        raise RuntimeError(
            f"区间 {params.start} ~ {params.end} 内没有打分快照；"
            f"可用范围 {min(score_dates)} ~ {max(score_dates)}"
        )
    panel_start = min(usable)
    panel_end = max(usable)

    emit(0.15, f"装配行情面板（{panel_start} ~ {panel_end}）")
    panels = Panels.from_lake(
        lake=lake, cfg=cfg, start=panel_start, end=panel_end,
        benchmark=params.benchmark,
    )
    universe = None
    if lake.exists("universe"):
        universe = lake.read("universe", columns=["code", "name", "industry_l1", "board", "is_st"])

    # 基准指数收盘价（不复权，指数无复权因子）
    #
    # ⚠️ 必须比 panel_start 多读一段历史：regime_exposure 的 MA200 若只拿到
    # 回测窗口内的序列，前 200 个交易日恒为 NaN，「均线未成型 → 半仓」，
    # 等于给回测前 1/5 的时间凭空加了一层固定半仓 —— 择时信号全是假的。
    # 多读 500 个自然日足够覆盖 200 个交易日均线 + 节假日。
    bench_close = None
    if lake.exists("index_bars"):
        lookback_start = (pd.Timestamp(panel_start) - pd.Timedelta(days=500)).strftime("%Y-%m-%d")
        index_frame = lake.read("index_bars", columns=["date", "code", "close"],
                                start=lookback_start, end=panel_end)
        if not index_frame.empty:
            picked = index_frame[index_frame["code"].astype(str) == params.benchmark]
            if not picked.empty:
                bench_close = pd.Series(
                    pd.to_numeric(picked["close"], errors="coerce").to_numpy(dtype="float64"),
                    index=pd.DatetimeIndex(picked["date"]),
                ).sort_index()
    if bench_close is None:
        logger.warning("缺少基准指数 %s 行情，超额与择时将不可用", params.benchmark)

    emit(0.35, "运行主回测")
    engine = BacktestEngine(
        panels, params, costs=costs, universe=universe,
        score_wide=score_wide, bench_close=bench_close,
    )
    result = engine.run()

    data_range = {"start": panel_start, "end": panel_end,
                  "codes": int(len(panels.codes))}

    emit(0.60, "运行参数网格" if grid else "整理结果")
    grid_records: List[dict] = []
    grid_note = ""
    if grid:
        elapsed = time.monotonic() - started
        if elapsed > GRID_TIME_BUDGET:
            grid_note = (f"主回测耗时 {elapsed:.0f}s 已超过预算 {GRID_TIME_BUDGET:.0f}s，"
                         f"跳过参数网格（32 组会再乘上几十倍时间）")
            logger.info("%s", grid_note)
        else:
            grid_records = run_grid(panels, params, costs, universe, score_wide,
                                    bench_close)

    payload = result_to_payload(result, params, version, costs, data_range, grid_records)
    if grid_note:
        payload["warnings"].append(grid_note)

    emit(0.85, "写入结果")
    folder = Path(cfg.paths.results) / "backtests"
    path = save_payload(payload, folder)
    experiment_id = record_experiment(payload)

    emit(1.0, "完成")
    return {**payload, "path": str(path), "experiment_id": experiment_id}


def run_grid(panels, base: StrategyParams, costs: CostModel, universe,
             score_wide, bench_close) -> List[dict]:
    """32 组参数网格，在 IS 上按 Sharpe 寻优，附 OOS Sharpe。

    ``max_weight`` 也随 ``top_n`` 抬到不小于 ``1/top_n``：网格想比较的是
    **持股集中度**，而固定 ``max_weight=0.10`` 会让 ``top_n=5`` 的组合
    每只票都触顶（等权 0.2 > 0.1），常态只用一半资金 ——
    那样的点和 ``top_n=20`` 满仓的点放在一张表里没有可比性。
    """
    records: List[dict] = []
    for top_n in GRID_TOP_N:
        for rebal_days in GRID_REBAL_DAYS:
            for regime in GRID_REGIME:
                params = StrategyParams(
                    **{**base.to_dict(), "top_n": top_n, "rebal_days": rebal_days,
                       "regime": regime,
                       "max_weight": max(base.max_weight, 1.0 / top_n)}
                )
                try:
                    outcome = BacktestEngine(
                        panels, params, costs=costs, universe=universe,
                        score_wide=score_wide, bench_close=bench_close,
                    ).run()
                except Exception as exc:
                    logger.warning("网格组合 top_n=%s rebal=%s regime=%s 失败：%s",
                                   top_n, rebal_days, regime, exc)
                    continue
                records.append(
                    {
                        "top_n": top_n,
                        "rebal_days": rebal_days,
                        "regime": regime,
                        "is_sharpe": (outcome.metrics.get("is") or {}).get("sharpe"),
                        "oos_sharpe": (outcome.metrics.get("oos") or {}).get("sharpe"),
                        "is_total_return": (outcome.metrics.get("is") or {}).get("total_return"),
                        "oos_total_return": (outcome.metrics.get("oos") or {}).get("total_return"),
                    }
                )
    records.sort(key=lambda r: (r.get("is_sharpe") is not None, r.get("is_sharpe") or -99),
                 reverse=True)
    return records
