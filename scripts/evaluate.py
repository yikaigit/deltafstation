"""因子评价 CLI：IC / ICIR / 分层 / 相关性 / 换手 → data/reports/。

用法::

    # 评价某个版本里用到的全部因子
    .venv/Scripts/python.exe scripts/evaluate.py --version v2.0.0

    # 只评一部分（调试）
    .venv/Scripts/python.exe scripts/evaluate.py --factors ep bp roe_ttm

    # 指定区间与未来收益窗口
    .venv/Scripts/python.exe scripts/evaluate.py --start 2022-01-01 --forward-days 20

输出两个文件：

* ``data/reports/factor_eval_{version}.csv``    每因子一行
* ``data/reports/factor_corr_{version}.csv``    因子相关矩阵

以及终端上的一份摘要（含每个维度里过线与未过线的因子数）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from backend.core import evaluation as evaluation_lib  # noqa: E402
from backend.core import factors as factor_lib  # noqa: E402
from backend.core.config import get_config  # noqa: E402
from backend.core.panels import Panels  # noqa: E402
from backend.core.scorer import Scorer  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="因子评价（IC / 分层 / 相关性）")
    parser.add_argument("--version", default=None,
                        help="打分版本（决定评价哪些因子），默认取配置")
    parser.add_argument("--factors", nargs="*", default=None,
                        help="只评这些因子（不给则用版本配置里的全部）")
    parser.add_argument("--start", default=None, help="评价区间起点")
    parser.add_argument("--end", default=None, help="评价区间终点")
    parser.add_argument("--forward-days", type=int, default=20,
                        help="未来收益窗口（交易日），默认 20")
    parser.add_argument("--out", default=None, help="输出目录，默认 data/reports")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = get_config()
    print("=" * 72)
    print("DeltaFStation 因子评价")
    print("=" * 72)

    spec = Scorer.load_version(args.version or cfg.get("scoring.default_version"))
    names = args.factors or spec["factors"]
    unknown = [n for n in names if n not in factor_lib.FACTORS]
    if unknown:
        print(f"未实现的因子：{unknown}", file=sys.stderr)
        return 2

    start = args.start or cfg.get("universe.start_date", "2008-01-01")
    print(f"  区间        {start} ~ {args.end or '最新'}")
    print(f"  因子数      {len(names)}")
    print(f"  未来窗口    {args.forward_days} 个交易日")
    print("-" * 72)

    # 评价必须用**真实可交易**的价格，所以面板要带估值/财务
    # （valuation/quality 因子需要基本面字段；否则 compute 会直接 KeyError）
    panel = Panels.from_lake(start=start, end=args.end)
    print(f"  面板        {len(panel.dates)} 交易日 × {len(panel.codes)} 标的")

    frames = factor_lib.compute_many(names, panel)
    missing = [n for n in names if n not in frames]
    if missing:
        print(f"  ⚠ 计算失败的因子：{missing}", file=sys.stderr)

    directions = {n: factor_lib.FACTORS[n].direction for n in frames}
    categories = {n: factor_lib.FACTORS[n].category for n in frames}

    def progress(index: int, total: int, name: str) -> None:
        print(f"\r  评价 {index}/{total}  {name:16s}", end="", flush=True)

    outcome = evaluation_lib.evaluate_all(
        frames,
        panel.close.astype("float64"),
        directions=directions,
        categories=categories,
        forward_days=args.forward_days,
        progress=progress,
    )
    print("\r" + " " * 60 + "\r", end="")

    table: pd.DataFrame = outcome["table"]
    correlation: pd.DataFrame = outcome["correlation"]

    out_dir = Path(args.out) if args.out else Path(cfg.paths.data) / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    eval_path = out_dir / f"factor_eval_{spec['version']}.csv"
    corr_path = out_dir / f"factor_corr_{spec['version']}.csv"
    table.to_csv(eval_path, index=False, encoding="utf-8-sig")
    correlation.to_csv(corr_path, encoding="utf-8-sig")

    _print_summary(table)
    print(f"\n  写出        {eval_path}")
    print(f"              {corr_path}")
    return 0


def _print_summary(table: pd.DataFrame) -> None:
    """终端摘要：按维度分组，标出过线/未过线。"""
    if table.empty:
        print("  没有可评价的因子")
        return

    columns = ["name", "category", "ic_mean", "ic_tstat", "icir", "ic_win_rate",
               "monotonicity", "long_short", "turnover", "coverage", "passed"]
    display = table[columns].copy()
    display["passed"] = display["passed"].map({True: "✓", False: "✗"})
    display.columns = ["因子", "维度", "IC均值", "t值", "ICIR", "IC胜率",
                       "单调性", "多空年化", "换手", "覆盖", "过线"]
    print("\n" + "=" * 72)
    print("逐因子结果")
    print("=" * 72)
    print(display.to_string(index=False, float_format=lambda v: f"{v:7.3f}"))

    print("\n" + "-" * 72)
    passed = int(table["passed"].sum())
    print(f"过线 {passed} / {len(table)} 个因子")
    if "category" in table.columns:
        grouped = table.groupby("category").agg(
            因子数=("name", "size"), 过线数=("passed", "sum")
        )
        print(grouped.to_string())

    if "failed_on" in table.columns:
        print("\n未过线的具体指标（前 5 个因子）：")
        for _, row in table[~table["passed"]].head(5).iterrows():
            reasons = row["failed_on"]
            print(f"  {row['name']:16s} 未达：{', '.join(reasons)}")


if __name__ == "__main__":
    raise SystemExit(main())
