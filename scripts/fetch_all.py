#!/usr/bin/env python
"""
批量拉取 A 股历史数据（baostock）

设计约束
--------
* baostock 是**全局单连接**库（每次调用 login/logout），必须串行。
  DataManager._FETCH_LOCK 已做保护，这里也不引入并发。
* baostock 无官方限流承诺，属免费无鉴权服务，需自觉限速。
  默认每只间隔 0.2s；5763 只全历史约 2-4 小时。
* **断点续传**：进度写入 data/quality/fetch_progress.json，中断后重跑自动跳过已完成。
* 失败重试：单只最多 3 次，仍失败则记录到 skipped，不中断整体任务。
* 同时落复权事件表（data/adjust/）与来源标记（data/meta/）。

用法
----
    # 少量测试
    python scripts/fetch_all.py --symbols sh.600000,sh.600519,sh.000001 --start 2024-01-01

    # 全量（按标的字典）
    python scripts/fetch_all.py --start 2015-01-01 --limit 50      # 先跑 50 只试试
    python scripts/fetch_all.py --start 2015-01-01                 # 全量 5763 只

    # 每日增量（只补最近 7 天）
    python scripts/fetch_all.py --incremental 7

    # 重置进度
    python scripts/fetch_all.py --reset-progress
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.data_manager import DataManager  # noqa: E402

CATALOG_PATH = os.path.join("data", "raw", "symbols_dict_baostock.json")
PROGRESS_PATH = os.path.join("data", "quality", "fetch_progress.json")
DEFAULT_INTERVAL = 0.2
MAX_RETRY = 3


def load_catalog(path=CATALOG_PATH):
    """从标的字典读取全部代码。"""
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    items = payload.get("items") if isinstance(payload, dict) else payload
    return [str(i.get("code", "")).strip() for i in items if str(i.get("code", "")).strip()]


def load_progress():
    if not os.path.exists(PROGRESS_PATH):
        return {"done": [], "skipped": {}}
    try:
        with open(PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("done", [])
        data.setdefault("skipped", {})
        return data
    except Exception:
        return {"done": [], "skipped": {}}


def save_progress(progress):
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    tmp = PROGRESS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS_PATH)


def main():
    ap = argparse.ArgumentParser(description="批量拉取 baostock 历史数据")
    ap.add_argument("--symbols", default=None, help="逗号分隔的标的代码（不传则用标的字典）")
    ap.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD，默认 20 年前")
    ap.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（排他，默认今天）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只，0 表示全部")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="每只间隔秒数")
    ap.add_argument("--incremental", type=int, default=0, help="增量模式：只补最近 N 天")
    ap.add_argument("--skip-existing", action="store_true", default=True,
                    help="跳过进度文件中已完成的（默认开启）")
    ap.add_argument("--reset-progress", action="store_true", help="清空进度后退出")
    ap.add_argument("--with-adjust", action="store_true", default=True,
                    help="同步落复权事件表（默认开启）")
    ap.add_argument("--force", action="store_true", help="强制刷新（忽略本地已有数据，全量重拉）")
    args = ap.parse_args()

    if args.reset_progress:
        if os.path.exists(PROGRESS_PATH):
            os.remove(PROGRESS_PATH)
        print(f"进度已重置：{PROGRESS_PATH}")
        return 0

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = load_catalog()
    if args.limit:
        symbols = symbols[:args.limit]

    progress = load_progress()
    done_set = set(progress["done"]) if args.skip_existing else set()
    pending = [s for s in symbols if s not in done_set]

    print(f"标的总数 {len(symbols)}，已完成 {len(symbols) - len(pending)}，待处理 {len(pending)}")
    if args.incremental:
        print(f"增量模式：只补最近 {args.incremental} 天")

    dm = DataManager("data")
    started = time.time()
    ok_count = 0

    for idx, sym in enumerate(pending, 1):
        # 增量模式：从本地最后日期往前推 N 天开始补
        start, end = args.start, args.end
        if args.incremental:
            meta = dm.load_meta(sym)
            if meta and meta.get("last_date"):
                start = (datetime.fromisoformat(meta["last_date"]).date()).isoformat()
            else:
                start = args.start

        last_err = None
        for attempt in range(1, MAX_RETRY + 1):
            try:
                filename, df, status, source = dm.fetch_data(
                    sym, start_date=start, end_date=end,
                    data_source="baostock",
                    force_refresh=bool(args.incremental) or args.force,
                    with_adjust=args.with_adjust,
                )
                rows = 0 if df is None else len(df)
                print(f"[{idx}/{len(pending)}] {sym:<12} {status:<16} rows={rows:<6} {filename}")
                ok_count += 1
                last_err = None
                break
            except Exception as e:
                last_err = str(e)
                if attempt < MAX_RETRY:
                    time.sleep(1.5 * attempt)
                    continue
                print(f"[{idx}/{len(pending)}] {sym:<12} FAILED after {MAX_RETRY} 次: {last_err}")
                progress["skipped"][sym] = last_err

        progress["done"].append(sym)
        # 每 20 只落一次盘，避免中途中断丢太多进度
        if idx % 20 == 0:
            save_progress(progress)
        time.sleep(args.interval)

    save_progress(progress)
    elapsed = time.time() - started
    print(f"\n完成：成功 {ok_count}，跳过 {len(progress['skipped'])}，耗时 {elapsed / 60:.1f} 分钟")
    if progress["skipped"]:
        print("失败标的（前 10）:")
        for s, err in list(progress["skipped"].items())[:10]:
            print(f"  {s}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
