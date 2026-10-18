"""每日增量更新 CLI。

按规格 §4.5 执行第 1~8 步（第 9 步打分属 P2）：
    1. 判断交易日（非交易日直接退出）
    2. 更新 HS300 成分（幂等，快照日变化才真正新增文件）
    3. 更新 universe
    4. 增量拉日线（每只从本地 max(date)+1）
    5. 更新复权事件
    6. 拉当日估值横截面
    7. 更新财务（默认关闭，--with-fundamental 开启）
    8. 数据校验

用法
----
    .venv/Scripts/python.exe scripts/update_daily.py
    .venv/Scripts/python.exe scripts/update_daily.py --date 2026-10-16
    .venv/Scripts/python.exe scripts/update_daily.py --with-fundamental --json

退出码
------
    0  成功（含「非交易日跳过」）
    1  失败
    2  当日行情尚未到位（需稍后重试，调度器据此重排）
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DeltaFStation 每日增量更新")
    parser.add_argument("--date", default=None, help="目标交易日（默认今天）")
    parser.add_argument("--with-fundamental", action="store_true", help="同时更新财务（财报季用）")
    parser.add_argument("--json", action="store_true", help="打印完整 JSON 摘要")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    from backend.core.ingest import get_ingest

    ingest = get_ingest()
    bar = ConsoleProgress()

    print("=" * 72)
    print("DeltaFStation 每日增量更新")
    print("=" * 72)

    try:
        summary = ingest.update_daily(
            trade_date=args.date,
            with_fundamental=args.with_fundamental,
            progress=bar,
        )
    except KeyboardInterrupt:
        print("\n\n已中断（已写入的数据保留，重跑会自动续传）")
        record_job("update", {"trade_date": args.date}, "cancelled", "用户中断")
        return 130
    except Exception as exc:
        print(f"\n\n更新失败：{type(exc).__name__}: {exc}")
        record_job("update", {"trade_date": args.date}, "failed", f"{type(exc).__name__}: {exc}")
        raise

    if summary.get("skipped"):
        print(f"  {summary.get('trade_date')} 非交易日，跳过")
        record_job("update", {"trade_date": args.date}, "success", "非交易日跳过")
        return 0

    day = summary.get("trade_date")
    bars = summary.get("bars") or {}
    valuation = summary.get("valuation") or {}
    quality = summary.get("quality") or {}
    adj = summary.get("adj_factor") or {}

    print(f"\n  交易日          {day}")
    print(f"  标的数          {summary.get('codes', 0)}")
    print(f"  行情写入        {human_int(bars.get('rows_written', 0))} 行"
          f"（已最新 {bars.get('skipped_latest', 0)} 只，失败 {len(bars.get('failures', []))} 只）")
    print(f"  复权事件        {human_int(adj.get('rows', 0))} 条")
    print(f"  估值            {human_int(valuation.get('rows', 0))} 行")
    if quality.get("symbols"):
        print(f"  校验            {quality['symbols']} 只，ERROR {quality.get('error_symbols', 0)}，"
              f"WARN {quality.get('warn_symbols', 0)}"
              + ("（已降级）" if quality.get("degraded") else ""))

    record_job(
        "update",
        {"trade_date": args.date, "with_fundamental": args.with_fundamental},
        "success",
        f"{day} 更新完成，行情 {bars.get('rows_written', 0)} 行",
        json.dumps(summary, ensure_ascii=False, default=str)[:20000],
    )

    if args.json:
        print("\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    if summary.get("pending"):
        print(f"\n  ! {summary.get('note')}")
        return 2

    print("\n  更新完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
