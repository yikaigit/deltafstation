#!/usr/bin/env python
"""打分 CLI。

用法
----
```
# 每日单日打分（默认，取本地最新交易日）
python scripts/score.py

# 指定日期
python scripts/score.py --date 2026-10-16

# 全历史重算（改因子后必须重跑）
python scripts/score.py --full

# 只算某区间
python scripts/score.py --full --start 2024-01-01 --end 2026-10-16

# 只看结果不落盘（调试用）
python scripts/score.py --date 2026-10-16 --dry-run
```

退出码：0 成功；1 失败；2 数据不足。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._cli import ConsoleProgress, human_int, record_job  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeltaFStation 打分")
    parser.add_argument("--version", default=None, help="打分版本，默认取配置 scoring.default_version")
    parser.add_argument("--date", default=None, help="单日打分的目标日期，默认最新交易日")
    parser.add_argument("--full", action="store_true", help="全历史重算（而不是单日）")
    parser.add_argument("--start", default=None, help="全历史重算的起点")
    parser.add_argument("--end", default=None, help="全历史重算的终点")
    parser.add_argument("--codes", nargs="*", default=None, help="只对指定标的打分（调试用）")
    parser.add_argument("--days", type=int, default=None,
                        help="全量重算时只写最近 N 个交易日的快照（0=全部，默认取配置 snapshot_days）")
    parser.add_argument("--dry-run", action="store_true", help="只计算不写快照")
    parser.add_argument("--json", action="store_true", help="结束时打印完整 JSON 摘要")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    from backend.core.scorer import get_scorer

    scorer = get_scorer()
    version = args.version or scorer.cfg.get("scoring.default_version", "v1.0.0")

    print("=" * 72)
    print("DeltaFStation 打分")
    print("=" * 72)
    print(f"  版本        {version}")
    print(f"  模式        {'全历史重算' if args.full else '单日'}")
    if args.full:
        print(f"  区间        {args.start or '配置起点'} ~ {args.end or '最新'}")
    else:
        print(f"  日期        {args.date or '最新交易日'}")
    if args.codes:
        print(f"  标的        {len(args.codes)} 只（调试模式）")
    print("-" * 72)

    progress = ConsoleProgress()
    started = time.monotonic()
    params = {"version": version, "mode": "full" if args.full else "day",
              "date": args.date, "start": args.start, "end": args.end}

    try:
        if args.full:
            result = scorer.score_range(
                version=version, start=args.start, end=args.end,
                codes=args.codes, write=not args.dry_run,
                days=args.days, progress=progress,
            )
        else:
            result = scorer.score_day(
                trade_date=args.date, version=version,
                codes=args.codes, write=not args.dry_run, progress=progress,
            )
    except RuntimeError as exc:
        progress.done(f"失败：{exc}")
        record_job("score", params, "failed", str(exc))
        return 2 if "没有行情" in str(exc) or "不在行情" in str(exc) else 1
    except Exception as exc:
        progress.done(f"失败：{type(exc).__name__}: {exc}")
        record_job("score", params, "failed", f"{type(exc).__name__}: {exc}")
        raise

    elapsed = time.monotonic() - started
    progress.done(f"完成，耗时 {elapsed:.1f}s")

    print()
    print("=" * 72)
    print("结果")
    print("=" * 72)
    if args.full:
        print(f"  区间        {result.get('start')} ~ {result.get('end')}")
        print(f"  标的        {result.get('codes')} 只")
        print(f"  有效交易日  {result.get('valid_dates')} 天")
    else:
        print(f"  日期        {result.get('trade_date')}")
        print(f"  标的        {result.get('codes')} 只")
        print(f"  窗口        {result.get('window_days')} 个交易日")
    if result.get("rows") is not None:
        print(f"  写入        {human_int(result['rows'])} 行 / {result.get('days_written')} 天")
        print(f"  快照        {result.get('path')}")
        dist = result.get("distribution") or {}
        if dist:
            print(f"  评级分布    " + "  ".join(f"{k} {v}" for k, v in dist.items()))

    diagnostics = result.get("diagnostics") or {}
    notes = diagnostics.get("notes") or []
    if notes:
        print()
        print("  诊断")
        for note in notes:
            print(f"    · {note}")

    print()
    print(f"耗时 {elapsed:.1f}s")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))

    record_job("score", params, "success", _summarize(result),
               log=json.dumps(result, ensure_ascii=False, default=str)[:20000])
    return 0


def _summarize(result: dict) -> str:
    if result.get("mode") == "full":
        return (f"全量打分 {result.get('start')}~{result.get('end')}，"
                f"{result.get('codes')} 只，写入 {result.get('rows', 0)} 行")
    return (f"{result.get('trade_date')} 打分完成，"
            f"{result.get('codes')} 只，写入 {result.get('rows', 0)} 行")


if __name__ == "__main__":
    raise SystemExit(main())
