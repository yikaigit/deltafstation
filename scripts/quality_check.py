#!/usr/bin/env python
"""
数据质量抽查 CLI

用法
----
    # L1+L2 全量（离线，5000 只也就几分钟）
    python scripts/quality_check.py --all

    # 单只
    python scripts/quality_check.py --symbol sh.600000

    # L3 外部对账（联网，分层抽样，默认 5 只）
    python scripts/quality_check.py --sample --sample-size 5

    # L4 策略回归（买入持有年化）
    python scripts/quality_check.py --regression --symbol sh.600519

    # 停牌行清洗（默认 dry-run，--apply 才写盘）
    python scripts/quality_check.py --all --clean-suspensions --apply

    # 基线指纹
    python scripts/quality_check.py --all --write-baseline
    python scripts/quality_check.py --all --check-baseline

    # 导出 JSON 报告
    python scripts/quality_check.py --all --json data/quality/report.json
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.adjustment import AdjustmentStore, normalize_bs_code  # noqa: E402
from backend.core.data_validator import (  # noqa: E402
    LEVEL_ERROR, LEVEL_WARN, clean_suspensions, validate_file,
)

RAW_DIR = os.path.join("data", "raw")
QUALITY_DIR = os.path.join("data", "quality")
BASELINE_PATH = os.path.join(QUALITY_DIR, "baseline.json")
LEGACY_DIRNAME = "_legacy_yfinance"


def list_symbols(raw_dir=RAW_DIR):
    """列出所有标的代码（跳过归档目录与 symbols_dict）。"""
    out = []
    if not os.path.isdir(raw_dir):
        return out
    for name in sorted(os.listdir(raw_dir)):
        if not name.lower().endswith(".csv"):
            continue
        if name.startswith("symbols_dict"):
            continue
        out.append(name[:-4])
    return out


def pick_sample(symbols, store, size):
    """分层抽样：优先选有除权事件的（复权最容易错），其余随机。"""
    if not symbols:
        return []
    with_events = []
    for s in symbols:
        ev = store.load_events(s)
        if ev is not None and len(ev) > 0:
            with_events.append(s)
    done = set(with_events)
    rest = [s for s in symbols if s not in done]
    random.shuffle(with_events)
    random.shuffle(rest)
    half = max(1, size // 2)
    picked = with_events[:half]
    seen = set(picked)
    remaining = [s for s in (with_events[half:] + rest) if s not in seen]
    picked += remaining[:max(0, size - len(picked))]
    return picked


def print_report(rep, verbose=False):
    flag = "OK  " if rep.ok else "FAIL"
    warn = f", warn={len(rep.warns)}" if rep.warns else ""
    print(f"  [{flag}] {rep.symbol:<12} rows={rep.rows:<6} "
          f"{rep.first_date}~{rep.last_date} errors={len(rep.errors)}{warn}")
    for issue in rep.issues:
        if not verbose and issue.level == LEVEL_WARN and issue.rule in ("R2.5",):
            continue
        mark = "E" if issue.level == LEVEL_ERROR else "W"
        print(f"        {mark} {issue.rule} {issue.message}")
        if issue.samples:
            print(f"          样例: {issue.samples[:5]}")


def run_l1l2(symbols, store, verbose=False):
    reports = []
    for sym in symbols:
        path = os.path.join(RAW_DIR, f"{sym}.csv")
        rep = validate_file(path, symbol=sym, adjust_store=store)
        reports.append(rep)
        print_report(rep, verbose)
    return reports


def run_l3(symbols, store, window=30, verbose=False):
    """外部对账：交易日历对齐 + 重拉比对（不复权与后复权各一次）。"""
    import baostock as bs

    from backend.core.adjustment import BS_LOCK

    results = []
    for sym in symbols:
        path = os.path.join(RAW_DIR, f"{sym}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        if df.empty or "Date" not in df.columns:
            continue
        tail = df.tail(window)
        start, end = tail["Date"].iloc[0], tail["Date"].iloc[-1]
        code = normalize_bs_code(sym)

        # baostock end_date 是排他的 → 结束后推一天
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

        with BS_LOCK:
            bs.login()
            try:
                # R3.1 交易日历
                rs = bs.query_trade_dates(start_date=start, end_date=end)
                cal = []
                while rs.error_code == "0" and rs.next():
                    cal.append(rs.get_row_data())
                trade_days = {r[0] for r in cal if len(r) >= 2 and r[1] == "1"}

                def _bars(flag):
                    r = bs.query_history_k_data_plus(
                        code, "date,open,high,low,close,volume",
                        start_date=start, end_date=end_excl,
                        frequency="d", adjustflag=flag)
                    rows = []
                    while r.error_code == "0" and r.next():
                        rows.append(r.get_row_data())
                    f = list(r.fields)
                    d = pd.DataFrame(rows, columns=f)
                    # baostock 返回小写列名，统一成项目口径，merge 时后缀才会生效
                    return d.rename(columns={
                        "date": "Date", "open": "Open", "high": "High",
                        "low": "Low", "close": "Close", "volume": "Volume"})

                raw_bs = _bars("3")
                post_bs = _bars("1")
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass

        notes = []
        local_days = set(tail["Date"].astype(str))
        extra = sorted(local_days - trade_days)
        if extra:
            notes.append(("R3.1", LEVEL_ERROR, f"本地存在非交易日 {len(extra)} 天: {extra[:3]}"))
        missing = sorted(trade_days - local_days)
        if missing:
            notes.append(("R3.1", LEVEL_WARN, f"交易日历中有 {len(missing)} 天本地缺失: {missing[:3]}"))

        # R3.2 不复权重拉比对
        if not raw_bs.empty and "date" in raw_bs.columns:
            m = tail.merge(raw_bs, left_on="Date", right_on="date", suffixes=("_loc", "_bs"))
            if len(m):
                d = (pd.to_numeric(m["Close_loc"]) - pd.to_numeric(m["Close_bs"])).abs()
                bad = int((d > 1e-6).sum())
                if bad:
                    notes.append(("R3.2", LEVEL_ERROR, f"不复权重拉不一致 {bad}/{len(m)} 行"))
                else:
                    notes.append(("R3.2", LEVEL_WARN, f"不复权重拉一致 ({len(m)} 行)"))
            else:
                notes.append(("R3.2", LEVEL_ERROR, f"无法对齐，本地 {len(tail)} 行 / 上游 {len(raw_bs)} 行"))

        # R3.3 后复权重拉比对（验证因子口径）
        if not post_bs.empty and "date" in post_bs.columns:
            events = store.load_events(sym)
            mine = store.apply(tail, sym, mode="back", events=events)
            m = mine.merge(post_bs, left_on="Date", right_on="date", suffixes=("_loc", "_bs"))
            if len(m):
                d = (pd.to_numeric(m["Close_loc"]) - pd.to_numeric(m["Close_bs"])).abs()
                bad = int((d > 1e-2).sum())
                if bad:
                    notes.append(("R3.3", LEVEL_ERROR, f"后复权对账不一致 {bad}/{len(m)} 行"))
                else:
                    notes.append(("R3.3", LEVEL_WARN, f"后复权对账一致 ({len(m)} 行)"))

        errors = [n for n in notes if n[1] == LEVEL_ERROR]
        print(f"  [{'FAIL' if errors else 'OK  '}] {sym:<12} 对账窗口 {start}~{end} ({len(tail)} 行)")
        for rule, level, msg in notes:
            print(f"        {'E' if level == LEVEL_ERROR else 'W'} {rule} {msg}")
        results.append({"symbol": sym, "ok": not errors,
                        "notes": [{"rule": r, "level": lv, "message": m} for r, lv, m in notes]})
    return results


def run_regression(symbols, store):
    """L4 策略回归：买入持有，比较不复权/后复权年化差异。"""
    print("\nL4 买入持有回归（后复权应 >= 不复权，高分红股差异显著）")
    all_ok = True
    for sym in symbols:
        path = os.path.join(RAW_DIR, f"{sym}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        if df.empty or len(df) < 2:
            continue
        events = store.load_events(sym)
        adj = store.apply(df, sym, mode="back", events=events)
        raw_c = pd.to_numeric(df["Close"], errors="coerce")
        adj_c = pd.to_numeric(adj["Close"], errors="coerce")
        days = (pd.to_datetime(df["Date"].iloc[-1]) - pd.to_datetime(df["Date"].iloc[0])).days
        years = max(days / 365.25, 1e-6)
        raw_ann = (raw_c.iloc[-1] / raw_c.iloc[0]) ** (1 / years) - 1
        adj_ann = (adj_c.iloc[-1] / adj_c.iloc[0]) ** (1 / years) - 1
        sane = -0.5 <= adj_ann <= 1.0
        ge = adj_ann >= raw_ann - 1e-6
        ok = sane and ge
        all_ok = all_ok and ok
        print(f"  [{'OK  ' if ok else 'FAIL'}] {sym:<12} {years:5.1f}年  "
              f"不复权年化={raw_ann:7.2%}  后复权年化={adj_ann:7.2%}  "
              f"{'' if ge else '<- 后复权低于不复权，因子方向可能反了'}"
              f"{'' if sane else '<- 年化超出合理区间'}")
    return all_ok


def clean_all(symbols, store, apply_write=False):
    print(f"\n停牌行清洗（{'写盘' if apply_write else 'dry-run'}）")
    total = 0
    for sym in symbols:
        path = os.path.join(RAW_DIR, f"{sym}.csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path)
        cleaned = clean_suspensions(df)
        removed = len(df) - len(cleaned)
        if not removed:
            continue
        total += removed
        if len(cleaned) == 0:
            # 整年停牌（如 sh.600485 2017 全年），清洗后为空。
            # 写空文件会让下游读到空 DataFrame 且难以定位，故保留原文件并告警。
            print(f"  {sym:<12} 全部 {len(df)} 行均为停牌行，清洗后为空 —— "
                  f"未写盘，建议直接从标的池移除")
            continue
        print(f"  {sym:<12} 剔除 {removed} 行（{len(df)} → {len(cleaned)}）")
        if apply_write:
            cleaned.to_csv(path, index=False)
    print(f"  合计剔除 {total} 行")
    return total


def main():
    ap = argparse.ArgumentParser(description="数据质量抽查")
    ap.add_argument("--all", action="store_true", help="扫描 data/raw 下全部标的")
    ap.add_argument("--symbol", action="append", default=[], help="指定标的，可重复")
    ap.add_argument("--sample", action="store_true", help="L3 外部对账（联网，分层抽样）")
    ap.add_argument("--sample-size", type=int, default=5)
    ap.add_argument("--regression", action="store_true", help="L4 买入持有回归")
    ap.add_argument("--clean-suspensions", action="store_true", help="剔除停牌填充行")
    ap.add_argument("--apply", action="store_true", help="配合 --clean-suspensions 真正写盘")
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--check-baseline", action="store_true")
    ap.add_argument("--json", default=None, help="导出 JSON 报告路径")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    store = AdjustmentStore("data")
    os.makedirs(QUALITY_DIR, exist_ok=True)

    if args.symbol:
        symbols = [normalize_bs_code(s) for s in args.symbol]
    elif args.all:
        symbols = list_symbols()
    else:
        ap.error("需指定 --all 或 --symbol")

    print(f"标的数: {len(symbols)}")
    reports = run_l1l2(symbols, store, verbose=args.verbose)

    failed = [r for r in reports if not r.ok]
    total_err = sum(len(r.errors) for r in reports)
    total_warn = sum(len(r.warns) for r in reports)
    print(f"\nL1/L2 汇总: {len(reports)} 个标的, 失败 {len(failed)}, "
          f"ERROR {total_err} 条, WARN {total_warn} 条")

    payload = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "reports": [r.to_dict() for r in reports]}

    if args.regression:
        payload["regression_ok"] = run_regression(symbols, store)

    if args.sample:
        print(f"\nL3 外部对账（分层抽样，size={args.sample_size}）")
        payload["l3"] = run_l3(pick_sample(symbols, store, args.sample_size), store,
                               verbose=args.verbose)

    if args.clean_suspensions:
        payload["suspensions_removed"] = clean_all(symbols, store, apply_write=args.apply)

    if args.write_baseline:
        baseline = {r.symbol: {"rows": r.rows, "first_date": r.first_date,
                               "last_date": r.last_date, "errors": len(r.errors),
                               "warns": len(r.warns),
                               "checked_at": payload["generated_at"]}
                    for r in reports}
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False, indent=2)
        print(f"\n基线已写入 {BASELINE_PATH}（{len(baseline)} 条）")

    if args.check_baseline:
        if not os.path.exists(BASELINE_PATH):
            print(f"\n基线不存在：{BASELINE_PATH}")
        else:
            with open(BASELINE_PATH, "r", encoding="utf-8") as f:
                baseline = json.load(f)
            print("\n基线比对（只看行数/日期范围/错误数变化）")
            for r in reports:
                b = baseline.get(r.symbol)
                if not b:
                    print(f"  NEW  {r.symbol} rows={r.rows}")
                    continue
                if b["rows"] != r.rows or b["errors"] != len(r.errors):
                    print(f"  DIFF {r.symbol} rows {b['rows']}→{r.rows}, "
                          f"errors {b['errors']}→{len(r.errors)}")
            print("  比对完成")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告: {args.json}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
