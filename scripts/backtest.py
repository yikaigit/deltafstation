#!/usr/bin/env python
"""回测 CLI。

用法::

    python scripts/backtest.py                       # 默认参数跑一次（近 5 年，含 32 组网格）
    python scripts/backtest.py --top-n 10 --rebal-days 10 --regime
    python scripts/backtest.py --no-grid             # 只跑主回测
    python scripts/backtest.py --start 2021-01-01 --end 2026-09-30
    python scripts/backtest.py --json                # 机器可读输出

退出码：0 成功；1 参数/数据错误；2 运行失败。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.backtest.runner import run  # noqa: E402
from backend.core.config import get_config  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="按打分快照运行组合回测")
    parser.add_argument("--version", help="打分版本（默认最新）")
    parser.add_argument("--start", help="开始日期 YYYY-MM-DD（默认最近 backtest_years 年）")
    parser.add_argument("--end", help="结束日期 YYYY-MM-DD")
    parser.add_argument("--top-n", type=int, help="持仓只数（默认 20）")
    parser.add_argument("--rebal-days", type=int, help="调仓间隔交易日（默认 20）")
    parser.add_argument("--max-weight", type=float, help="单票权重上限（默认 0.10）")
    parser.add_argument("--weight-method", choices=["equal", "score"], help="加权方式")
    parser.add_argument("--regime", action="store_true", default=None,
                        help="启用大盘择时（默认启用）")
    parser.add_argument("--no-regime", dest="regime", action="store_false",
                        help="关闭大盘择时")
    parser.add_argument("--min-score-pct", type=float, help="只买百分位 ≥ 该值的股票")
    parser.add_argument("--initial-cash", type=float, help="初始资金")
    parser.add_argument("--is-ratio", type=float, help="样本内比例（默认 0.7）")
    parser.add_argument("--benchmark", help="基准指数代码（默认 sh.000300）")
    parser.add_argument("--no-grid", action="store_true", help="跳过 32 组参数网格")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    payload = {}
    for key in ("version", "start", "end", "top_n", "rebal_days", "max_weight",
                "weight_method", "min_score_pct", "initial_cash", "is_ratio",
                "benchmark"):
        value = getattr(args, key)
        if value is not None:
            payload[key] = value
    if args.regime is not None:
        payload["regime"] = args.regime

    cfg = get_config()
    cfg.paths.ensure()

    def progress(ratio, message):
        if not args.json:
            print(f"[{ratio * 100:5.1f}%] {message}", flush=True)

    try:
        result = run(payload, cfg=cfg, progress=progress, grid=not args.no_grid)
    except (ValueError, RuntimeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"回测失败：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover
        logging.exception("backtest crashed")
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"回测异常：{exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
        return 0

    metrics = result.get("metrics", {})
    period = result.get("period", {})
    print("=" * 62)
    print(f"回测完成：{result['id']}")
    print(f"  区间      {period.get('start')} ~ {period.get('end')}"
          f"（IS {period.get('is_start')}~{period.get('is_end')}，"
          f"OOS {period.get('oos_start')}~{period.get('oos_end')}）")
    print(f"  参数      top_n={result['params']['top_n']} "
          f"rebal_days={result['params']['rebal_days']} "
          f"regime={result['params']['regime']} "
          f"权重={result['params']['weight_method']}")
    for label in ("full", "is", "oos"):
        item = metrics.get(label) or {}
        if not item:
            continue
        print(f"  {label:<9} 总收益 {item.get('total_return')}% | "
              f"年化 {item.get('annual_return')}% | "
              f"Sharpe {item.get('sharpe')} | "
              f"回撤 {item.get('max_drawdown')}% | "
              f"超额 {item.get('excess_return')}%")
    print(f"  交易      {len(result.get('trades', []))} 笔 | "
          f"换手 {result.get('turnover')}x/年 | "
          f"阻挡 buy={result['blocked'].get('buy')} sell={result['blocked'].get('sell')} | "
          f"平均持仓 {result.get('avg_positions')}")
    best = (result.get("grid") or [])[:3]
    if best:
        print("  网格最优（IS Sharpe）:")
        for row in best:
            print(f"    top_n={row['top_n']:<3} rebal={row['rebal_days']:<3} "
                  f"regime={row['regime']!s:<5} IS={row['is_sharpe']} OOS={row['oos_sharpe']}")
    for warning in result.get("warnings", []):
        print(f"  ⚠️ {warning}")
    print(f"  结果文件  {result['path']}")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
