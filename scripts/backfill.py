"""全量回填 CLI（首次建仓用）。

做的事（全部幂等，可反复运行）：
    1. 交易日历      2008 起
    2. 标的池        沪深300 当前成分 + 元信息 + 行业
    3. 日线          不复权 OHLCV（增量：每只从本地 max(date)+1 开始）
    4. 复权因子      全历史事件表
    5. 估值历史      近 N 年逐日横截面（最慢的一步）
    6. 财务          季频报告（含公告日）
    7. 数据校验      产出 data/quality/report_YYYYMMDD.json

用法
----
    # 默认：2008 起、估值近 5 年、含财务
    .venv/Scripts/python.exe scripts/backfill.py

    # 只补日线与因子，跳过估值（快）
    .venv/Scripts/python.exe scripts/backfill.py --skip-valuation --skip-fundamental

    # 指定区间与估值年数
    .venv/Scripts/python.exe scripts/backfill.py --start 2015-01-01 --valuation-years 3

    # 分步执行
    .venv/Scripts/python.exe scripts/backfill.py --only bars
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts._cli import ConsoleProgress, human_int, record_job  # noqa: E402

STEPS = ("calendar", "universe", "bars", "index", "adj_factor", "valuation", "fundamental", "quality")
TOTAL_STEPS = len(STEPS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeltaFStation 全量回填")
    parser.add_argument("--start", default=None, help="行情起点，默认取配置 universe.start_date")
    parser.add_argument("--end", default=None, help="终点，默认最新交易日")
    parser.add_argument("--valuation-years", type=int, default=5, help="估值回填年数（默认 5）")
    parser.add_argument("--skip-valuation", action="store_true", help="跳过估值历史回填")
    parser.add_argument("--skip-fundamental", action="store_true", help="跳过财务回填")
    parser.add_argument("--skip-quality", action="store_true", help="跳过数据校验")
    parser.add_argument(
        "--only",
        choices=STEPS,
        action="append",
        help="只执行指定步骤（可重复），例如 --only bars --only adj_factor",
    )
    parser.add_argument("--json", action="store_true", help="结束时打印完整 JSON 摘要")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from backend.core.calendar import get_calendar
    from backend.core.ingest import get_ingest
    from backend.core.universe import get_universe

    ingest = get_ingest()
    calendar = get_calendar()
    universe = get_universe()

    wanted = set(args.only) if args.only else set(STEPS)
    if args.skip_valuation:
        wanted.discard("valuation")
    if args.skip_fundamental:
        wanted.discard("fundamental")
    if args.skip_quality:
        wanted.discard("quality")

    start = args.start or str(ingest.cfg.get("universe.start_date", "2008-01-01"))

    print("=" * 72)
    print("DeltaFStation 全量回填")
    print("=" * 72)
    print(f"  行情区间     {start} ~ {args.end or '最新交易日'}")
    print(f"  估值回填     {'跳过' if 'valuation' not in wanted else f'近 {args.valuation_years} 年'}")
    print(f"  财务         {'跳过' if 'fundamental' not in wanted else '回填'}")
    print(f"  步骤         {', '.join(s for s in STEPS if s in wanted)}")
    print("-" * 72)

    summary: dict = {"start": start, "steps": [s for s in STEPS if s in wanted]}
    failed = False

    try:
        # 1. 日历
        if "calendar" in wanted:
            print(f"\n[1/{TOTAL_STEPS}] 交易日历")
            bar = ConsoleProgress()
            summary["calendar"] = ingest.refresh_calendar(start, args.end, progress=bar)
            bar.done(f"日历 {human_int(summary['calendar'].get('rows', 0))} 行")

        end = args.end or str(calendar.latest_trading_day() or "")[:10]
        if not end:
            print("  ! 无法确定最新交易日，请先回填日历")
            return 1
        summary["end"] = end

        # 2. 标的池
        if "universe" in wanted:
            print(f"\n[2/{TOTAL_STEPS}] 标的池")
            bar = ConsoleProgress()
            summary["universe"] = ingest.refresh_universe(progress=bar)
            bar.done(
                f"{summary['universe'].get('symbols', 0)} 只，行业覆盖 "
                f"{summary['universe'].get('industry_covered', 0)}"
            )

        codes = universe.codes(in_hs300_only=True, exclude_st=False)
        if not codes:
            print("  ! 标的池为空，请先执行 --only universe")
            return 1
        summary["codes"] = len(codes)

        # 3. 日线
        if "bars" in wanted:
            print(f"\n[3/{TOTAL_STEPS}] 日线（{len(codes)} 只）")
            bar = ConsoleProgress()
            summary["bars"] = ingest.backfill_bars(codes, start, end, progress=bar)
            info = summary["bars"]
            bar.done(
                f"写入 {human_int(info.get('rows_written', 0))} 行，"
                f"已最新 {info.get('skipped_latest', 0)} 只，失败 {len(info.get('failures', []))} 只"
            )

        # 3b. 基准指数
        if "index" in wanted:
            print(f"\n[4/{TOTAL_STEPS}] 基准指数")
            bar = ConsoleProgress()
            summary["index"] = ingest.backfill_index(start=start, end=end, progress=bar)
            info = summary["index"]
            bar.done(
                f"{','.join(info.get('codes', []))} {human_int(info.get('rows', 0))} 行，"
                f"失败 {len(info.get('failures', []))} 只"
            )

        # 4. 复权因子
        if "adj_factor" in wanted:
            print(f"\n[5/{TOTAL_STEPS}] 复权因子（{len(codes)} 只）")
            bar = ConsoleProgress()
            summary["adj_factor"] = ingest.backfill_factors(codes, progress=bar)
            info = summary["adj_factor"]
            integrity = info.get("integrity") or {}
            bar.done(
                f"{human_int(info.get('rows', 0))} 条事件，"
                f"非单调 {integrity.get('non_monotonic', 0)} 只"
            )

        # 5. 估值历史
        if "valuation" in wanted:
            import pandas as pd

            val_start = (
                pd.Timestamp(end) - pd.DateOffset(years=args.valuation_years)
            ).strftime("%Y-%m-%d")
            print(f"\n[6/{TOTAL_STEPS}] 估值历史 {val_start} ~ {end}（逐日请求，较慢）")
            bar = ConsoleProgress()
            summary["valuation"] = ingest.backfill_valuation(
                val_start, end, codes=codes, progress=bar
            )
            info = summary["valuation"]
            bar.done(
                f"{human_int(info.get('rows', 0))} 行，"
                f"已有 {info.get('skipped_existing', 0)} 天，失败 {info.get('failure_count', 0)} 天"
            )

        # 6. 财务
        if "fundamental" in wanted:
            print(f"\n[7/{TOTAL_STEPS}] 财务（{len(codes)} 只）")
            bar = ConsoleProgress()
            summary["fundamental"] = ingest.refresh_fundamental(codes, progress=bar)
            info = summary["fundamental"]
            bar.done(
                f"{human_int(info.get('rows', 0))} 期，"
                f"公告日估算 {info.get('notice_estimated', 0)} 期，"
                f"失败 {len(info.get('failures', []))} 只"
            )

        # 7. 校验
        if "quality" in wanted:
            print(f"\n[8/{TOTAL_STEPS}] 数据校验（{end}）")
            bar = ConsoleProgress()
            summary["quality"] = ingest.validate(end, codes=codes, progress=bar)
            info = summary["quality"]
            bar.done(
                f"{info.get('symbols', 0)} 只，ERROR {info.get('error_symbols', 0)} 只，"
                f"WARN {info.get('warn_symbols', 0)} 只"
                + ("（已降级）" if info.get("degraded") else "")
            )

        summary["coverage"] = ingest.coverage()

    except KeyboardInterrupt:
        print("\n\n已中断（已写入的数据保留，重跑会自动续传）")
        record_job("backfill", summary, "cancelled", "用户中断")
        return 130
    except Exception as exc:
        failed = True
        print(f"\n\n回填失败：{type(exc).__name__}: {exc}")
        record_job("backfill", summary, "failed", f"{type(exc).__name__}: {exc}")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
        raise

    # 汇总
    print("\n" + "=" * 72)
    print("覆盖情况")
    print("=" * 72)
    for name in ("bars", "index_bars", "valuation", "adj_factor", "fundamental", "universe", "calendar"):
        info = summary.get("coverage", {}).get(name) or {}
        if not info.get("exists"):
            print(f"  {name:<12} 未建立")
            continue
        span = ""
        if info.get("first_date"):
            span = f"  {info['first_date']} ~ {info['last_date']}"
        print(f"  {name:<12} {human_int(info.get('rows', 0)):>10} 行{span}")

    symbols = summary.get("coverage", {}).get("symbols")
    if symbols:
        print(f"  {'标的':<12} {symbols:>10} 只，平均 {summary['coverage'].get('avg_rows', 0)} 行/只")

    print("-" * 72)
    if not failed:
        print("回填完成。下一步：启动 Web 查看，或运行 scripts/update_daily.py 做增量更新。")
    record_job(
        "backfill",
        {k: v for k, v in summary.items() if k in ("start", "end", "codes")},
        "success" if not failed else "failed",
        f"回填 {summary.get('start')} ~ {summary.get('end')}，{summary.get('codes', 0)} 只标的",
        json.dumps(summary, ensure_ascii=False, default=str)[:20000],
    )

    if args.json:
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
