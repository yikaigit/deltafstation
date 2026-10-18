"""带样例数据的预览服务（不写入仓库真实 data/ 目录）。

把数据根指向系统临时目录，灌入少量合成数据，再启动 Web，
用于 P1 真实数据到位前调试前端渲染。

用法：
    .venv/Scripts/python.exe scripts/preview_with_sample.py [--port 5174] [--keep]
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDUSTRIES = ["银行", "食品饮料", "电子", "医药生物", "电力设备", "非银金融"]
NAMES = [
    "浦发银行", "招商银行", "贵州茅台", "五粮液", "立讯精密",
    "海康威视", "恒瑞医药", "迈瑞医疗", "宁德时代", "阳光电源",
    "中国平安", "东方财富",
]


def build_sample(tmp: Path) -> None:
    import backend.core.config as config_mod

    real_load = config_mod.load_config
    config_mod.load_config = lambda path=None, root=tmp: real_load(path=path, root=root)
    config_mod.get_config.cache_clear()

    from backend.core.lake import get_lake
    from backend.core.meta_db import get_meta_db
    from backend.core.snapshots import get_score_store

    lake = get_lake()
    rng = np.random.default_rng(20261018)

    codes = [f"sh.6{i:05d}" for i in range(1, 13)]
    universe = pd.DataFrame(
        {
            "code": codes,
            "name": NAMES,
            "board": ["主板"] * len(codes),
            "industry_l1": [INDUSTRIES[i % len(INDUSTRIES)] for i in range(len(codes))],
            "list_date": pd.to_datetime(["2005-01-04"] * len(codes)),
            "is_st": [False] * len(codes),
            "in_hs300": [True] * len(codes),
        }
    )
    lake.write("universe", universe)

    # 近 3 年日线（仅工作日，不做节假日精确处理）
    dates = pd.bdate_range("2023-10-18", "2026-10-16")
    frames = []
    for code in codes:
        steps = rng.normal(0.0003, 0.018, len(dates))
        close = 20 * np.exp(np.cumsum(steps))
        frames.append(
            pd.DataFrame(
                {
                    "date": dates,
                    "code": code,
                    "open": close * (1 + rng.normal(0, 0.004, len(dates))),
                    "high": close * (1 + abs(rng.normal(0, 0.006, len(dates)))),
                    "low": close * (1 - abs(rng.normal(0, 0.006, len(dates)))),
                    "close": close,
                    "volume": rng.integers(50_000, 900_000, len(dates)),
                }
            )
        )
    bars = pd.concat(frames, ignore_index=True)
    lake.write("bars", bars)

    lake.write(
        "valuation",
        bars[["date", "code"]].assign(pe_ttm=rng.uniform(8, 45, len(bars)), pb=rng.uniform(1, 9, len(bars))),
    )
    lake.write(
        "adj_factor",
        pd.DataFrame(
            {
                "code": codes,
                "ex_date": pd.to_datetime(["2024-06-20"] * len(codes)),
                "factor": [1.0 + 0.01 * i for i in range(len(codes))],
            }
        ),
    )
    lake.write(
        "calendar",
        pd.DataFrame({"date": dates, "is_trading_day": True}),
    )

    # 打分快照：最近 30 个交易日 × 12 只
    store = get_score_store()
    score_days = dates[-30:]
    for day in score_days:
        raw = rng.normal(50, 18, len(codes))
        pct = pd.Series(raw).rank(pct=True).to_numpy() * 100
        frames = pd.DataFrame(
            {
                "trade_date": day,
                "code": codes,
                "score_pct": pct.round(2),
                "score_abs": np.clip(raw + 50, 0, 100).round(2),
                "dim_trend": np.clip(raw + rng.normal(0, 8, len(codes)) + 50, 0, 100).round(2),
                "dim_momentum": np.clip(raw + rng.normal(0, 10, len(codes)) + 50, 0, 100).round(2),
                "dim_volume": np.clip(raw + rng.normal(0, 12, len(codes)) + 50, 0, 100).round(2),
                "dim_risk": np.clip(rng.normal(50, 18, len(codes)), 0, 100).round(2),
            }
        )
        frames["grade"] = pd.cut(
            frames["score_pct"],
            bins=[-1, 10, 30, 70, 90, 101],
            labels=["强烈看空", "看空", "中性", "看多", "强烈看多"],
        ).astype(str)
        store.write(frames, version="v1.0.0")

    db = get_meta_db()
    db.add_score_version("v1.0.0", note="技术四维（样例数据）")
    for kind, status, msg in [
        ("update", "success", "更新 12 只标的，新增 12 行"),
        ("score", "success", "生成 2026-10-16 打分快照"),
        ("backfill", "failed", "样例：baostock 连接超时"),
    ]:
        job_id = db.create_job(kind, {"sample": True})
        db.update_job(job_id, status=status, progress=1.0, message=msg)

    results = Path(config_mod.get_config().paths.results) / "backtests"
    results.mkdir(parents=True, exist_ok=True)
    import json

    (results / "bt_sample.json").write_text(
        json.dumps(
            {
                "id": "bt_sample",
                "created_at": "2026-10-16T18:30:00",
                "params": {"top_n": 20, "rebal_days": 20, "max_weight": 0.1},
                "period": {"start": "2021-10-18", "end": "2026-10-16"},
                "metrics": {
                    "full": {
                        "total_return": 63.4,
                        "annual_return": 10.3,
                        "sharpe": 0.86,
                        "max_drawdown": -22.7,
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"样例数据已写入：{tmp}")
    print(f"  标的 {len(codes)} 只，行情 {len(bars)} 行，打分 {len(score_days)} 天")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5174)
    parser.add_argument("--keep", action="store_true", help="保留临时目录（打印路径）")
    args = parser.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="deltaf_sample_"))
    build_sample(tmp)

    from backend.app import create_app

    app = create_app()
    print(f"预览服务：http://127.0.0.1:{args.port}")
    try:
        app.run(host="127.0.0.1", port=args.port, debug=False)
    finally:
        if not args.keep:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
