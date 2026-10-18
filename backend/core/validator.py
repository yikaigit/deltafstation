"""
数据校验模块 —— L1 结构校验 + L2 逻辑自洽

分层设计见 docs/data-source-and-adjustment.md：
    L1 结构校验（离线/全量/秒级）  文件格式级损坏
    L2 逻辑自洽（离线/全量/秒级）  能抓住绝大多数脏数据
    L3 外部对账（联网/抽样）       见 scripts/quality_check.py
    L4 策略回归                   见 scripts/quality_check.py --regression

关键规则说明
------------
R2.6 停牌填充行：baostock 对停牌日**不是不返回**，而是返回一串
     ``O==H==L==C 且 Volume==0`` 的假 K 线（实测 sh.600485 2017 年 244 行
     全部如此）。不清洗会让回测在停牌期产生假信号、假持仓，且不报错。
     已拍板：**直接剔除**（clean_suspensions）。

R2.7 涨跌幅超限：**必须用后复权价算**。用不复权价算，除权日会出反向假信号
     （浦发 2026-07-16：不复权看似 -4.9%，后复权实际 -0.45%）。
     硬阈值 35% 判 ERROR（任何时期超过一定是数据错）；
     板块阈值只出 WARN（历史上创业板/科创板涨跌幅限制变过，避免误报）。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np
import pandas as pd

from .adjustment import AdjustmentStore, normalize_bs_code

LEVEL_ERROR = "ERROR"
LEVEL_WARN = "WARN"

EXPECTED_COLUMNS = ["Date", "Open", "High", "Low", "Close", "Volume"]
PRICE_COLUMNS = ["Open", "High", "Low", "Close"]

#: 硬阈值：任何板块任何时期超过此幅度的日涨跌幅一定是数据错误
HARD_RETURN_LIMIT = 0.35
#: 日历日间隔阈值（无交易日历时的近似判据）
GAP_CALENDAR_DAYS = 5
EPS = 1e-6


def soft_return_limit(symbol: Optional[str]) -> float:
    """按板块返回软阈值（仅用于 WARN，历史规则变化可能误报）。"""
    code = normalize_bs_code(symbol or "")
    head = code.split(".")[-1]
    if head.startswith("30") or head.startswith("688"):
        return 0.20      # 创业板 / 科创板
    if head.startswith(("4", "8")):
        return 0.30      # 北交所
    return 0.10          # 主板


@dataclass
class Issue:
    """一条校验结果。"""
    rule: str
    level: str
    message: str
    count: int = 0
    samples: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "rule": self.rule, "level": self.level, "message": self.message,
            "count": self.count, "samples": self.samples[:10],
        }


@dataclass
class Report:
    """单个标的的校验报告。"""
    symbol: str
    path: str = ""
    rows: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    issues: List[Issue] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.level == LEVEL_ERROR]

    @property
    def warns(self) -> List[Issue]:
        return [i for i in self.issues if i.level == LEVEL_WARN]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self):
        return {
            "symbol": self.symbol, "path": self.path, "rows": self.rows,
            "first_date": self.first_date, "last_date": self.last_date,
            "ok": self.ok,
            "error_count": len(self.errors), "warn_count": len(self.warns),
            "issues": [i.to_dict() for i in self.issues],
            "skipped": self.skipped,
        }


def suspension_mask(df: pd.DataFrame) -> pd.Series:
    """R2.6：停牌填充行掩码（OHLC 四值全等且成交量为 0）。"""
    ohlc = [pd.to_numeric(df[c], errors="coerce") for c in PRICE_COLUMNS if c in df.columns]
    if len(ohlc) != 4 or "Volume" not in df.columns:
        return pd.Series(False, index=df.index)
    vol = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
    first = ohlc[0]
    all_equal = pd.concat(ohlc, axis=1).nunique(axis=1, dropna=False) <= 1
    return all_equal & (vol <= 0) & first.notna()


def clean_suspensions(df: pd.DataFrame) -> pd.DataFrame:
    """剔除停牌填充行（已拍板：直接剔除，不保留标记）。"""
    if df is None or df.empty or "Date" not in df.columns:
        return df
    mask = suspension_mask(df)
    if not mask.any():
        return df
    return df.loc[~mask].reset_index(drop=True)


def validate_dataframe(df: pd.DataFrame, symbol: Optional[str] = None, path: str = "",
                       events: Optional[pd.DataFrame] = None,
                       adjust_store: Optional[AdjustmentStore] = None) -> Report:
    """对一个不复权 OHLCV DataFrame 跑 L1 + L2。

    Args:
        df: 含 Date/Open/High/Low/Close/Volume 的不复权数据
        events: 复权事件表；提供后 R2.7 用后复权价计算（更准确）
        adjust_store: 未提供 events 时用它加载事件表
    """
    symbol = normalize_bs_code(symbol) if symbol else ""
    report = Report(symbol=symbol, path=str(path), rows=0 if df is None else int(len(df)))
    if df is None or df.empty:
        report.issues.append(Issue("R0.0", LEVEL_ERROR, "数据为空"))
        return report

    # ---------------- L1 结构 ----------------
    cols = list(df.columns)
    if cols != EXPECTED_COLUMNS:
        missing = [c for c in EXPECTED_COLUMNS if c not in cols]
        extra = [c for c in cols if c not in EXPECTED_COLUMNS]
        report.issues.append(Issue(
            "R1.1", LEVEL_ERROR,
            f"列名不符：缺少={missing} 多余={extra}", 1, [str(cols)]))

    dates = pd.to_datetime(df["Date"], errors="coerce") if "Date" in df.columns else pd.Series(dtype="datetime64[ns]")
    bad_date = int(dates.isna().sum())
    if bad_date:
        report.issues.append(Issue(
            "R1.2", LEVEL_ERROR, f"Date 无法解析 {bad_date} 行", bad_date,
            [str(v) for v in df.loc[dates.isna(), "Date"].head(5).tolist()]))

    valid_dates = dates.dropna()
    if len(valid_dates):
        report.first_date = valid_dates.min().strftime("%Y-%m-%d")
        report.last_date = valid_dates.max().strftime("%Y-%m-%d")

        dup = int(valid_dates.duplicated().sum())
        if dup:
            report.issues.append(Issue(
                "R1.4", LEVEL_ERROR, f"重复日期 {dup} 行", dup,
                [str(d.date()) for d in valid_dates[valid_dates.duplicated()].head(5)]))

        non_mono = int((valid_dates.diff().dt.total_seconds() <= 0).sum())
        if non_mono:
            report.issues.append(Issue(
                "R1.3", LEVEL_ERROR, f"日期非严格递增 {non_mono} 处", non_mono))

    numeric_cols = [c for c in PRICE_COLUMNS + ["Volume"] if c in df.columns]
    for c in numeric_cols:
        vals = pd.to_numeric(df[c], errors="coerce")
        nan_cnt = int(vals.isna().sum())
        if nan_cnt:
            report.issues.append(Issue("R1.5", LEVEL_ERROR, f"{c} 存在 NaN/非数值 {nan_cnt} 行", nan_cnt))

    if "Volume" in df.columns:
        vol_raw = df["Volume"]
        if pd.api.types.is_float_dtype(vol_raw):
            non_int = int((vol_raw.dropna() % 1 != 0).sum())
            if non_int == 0 and len(df):
                # 可能是被 pandas 转成了 float（科学计数法落盘），精度已丢
                report.issues.append(Issue(
                    "R1.6", LEVEL_WARN,
                    "Volume 为浮点类型，可能已被科学计数法落盘", int(len(df))))

    # ---------------- L2 自洽 ----------------
    nums = {c: pd.to_numeric(df[c], errors="coerce") for c in PRICE_COLUMNS if c in df.columns}
    o, h, l, c = (nums.get(k) for k in PRICE_COLUMNS)

    if h is not None and l is not None and o is not None and c is not None:
        hi_bad = h < pd.concat([o, c], axis=1).max(axis=1) - EPS
        lo_bad = l > pd.concat([o, c], axis=1).min(axis=1) + EPS
        for mask, rule, name in ((hi_bad, "R2.1", "High < max(Open,Close)"),
                                 (lo_bad, "R2.1", "Low > min(Open,Close)")):
            n = int(mask.sum())
            if n:
                report.issues.append(Issue(
                    rule, LEVEL_ERROR, f"{name} {n} 行", n,
                    [str(d) for d in df.loc[mask, "Date"].head(5).tolist()]))

        hi_strict = h < pd.concat([o, h, l, c], axis=1).max(axis=1) - EPS
        lo_strict = l > pd.concat([o, h, l, c], axis=1).min(axis=1) + EPS
        n = int((hi_strict | lo_strict).sum())
        if n:
            report.issues.append(Issue(
                "R2.2", LEVEL_ERROR, f"High/Low 不是四价极值 {n} 行", n,
                [str(d) for d in df.loc[hi_strict | lo_strict, "Date"].head(5).tolist()]))

        non_pos = (pd.concat([o, h, l, c], axis=1) <= 0).any(axis=1)
        n = int(non_pos.sum())
        if n:
            report.issues.append(Issue(
                "R2.3", LEVEL_ERROR, f"价格非正 {n} 行", n,
                [str(d) for d in df.loc[non_pos, "Date"].head(5).tolist()]))

    if "Volume" in df.columns:
        vol = pd.to_numeric(df["Volume"], errors="coerce")
        n = int((vol < 0).sum())
        if n:
            report.issues.append(Issue("R2.4", LEVEL_ERROR, f"成交量为负 {n} 行", n))

    # R2.5 / R2.6 停牌
    if "Volume" in df.columns:
        vol = pd.to_numeric(df["Volume"], errors="coerce").fillna(0)
        zero = vol <= 0
        n = int(zero.sum())
        if n:
            report.issues.append(Issue("R2.5", LEVEL_WARN, f"零成交量 {n} 行（疑似停牌）", n))
        susp = suspension_mask(df)
        n = int(susp.sum())
        if n:
            report.issues.append(Issue(
                "R2.6", LEVEL_WARN,
                f"停牌填充行 {n} 行（OHLC 四值全等且成交量为 0，建议剔除）", n,
                [str(d) for d in df.loc[susp, "Date"].head(5).tolist()]))

    # R2.7 涨跌幅（优先用后复权价）
    if c is not None:
        if events is None and adjust_store is not None and symbol:
            try:
                events = adjust_store.load_events(symbol)
            except Exception:
                events = None
        if events is not None and len(events):
            factor = AdjustmentStore.factor_from_events(events, pd.DatetimeIndex(dates))
            series = c.to_numpy(dtype="float64") * factor
            basis = "后复权价"
        else:
            series = c.to_numpy(dtype="float64")
            basis = "不复权价（缺事件表，可能误报除权日）"
            report.skipped.append("R2.7 未用后复权价：无复权事件表")
        prev = series[:-1]
        with np.errstate(divide="ignore", invalid="ignore"):
            ret = np.where(prev > 0, series[1:] / prev - 1.0, np.nan)
        ret = np.abs(np.nan_to_num(ret, nan=0.0))

        hard = ret > HARD_RETURN_LIMIT
        n = int(hard.sum())
        if n:
            idx = np.flatnonzero(hard) + 1
            report.issues.append(Issue(
                "R2.7", LEVEL_ERROR,
                f"涨跌幅超过硬阈值 {HARD_RETURN_LIMIT:.0%}（{basis}）{n} 天", n,
                [str(df["Date"].iloc[i]) for i in idx[:5]]))

        soft = soft_return_limit(symbol)
        soft_mask = (ret > soft + EPS) & (ret <= HARD_RETURN_LIMIT)
        n = int(soft_mask.sum())
        if n:
            idx = np.flatnonzero(soft_mask) + 1
            report.issues.append(Issue(
                "R2.7", LEVEL_WARN,
                f"涨跌幅超过板块阈值 {soft:.0%}（{basis}）{n} 天，需人工复核", n,
                [str(df["Date"].iloc[i]) for i in idx[:5]]))

    # R2.8 日期间隔
    if len(valid_dates) > 1:
        gaps = valid_dates.sort_values().diff().dt.days
        big = gaps > GAP_CALENDAR_DAYS
        n = int(big.sum())
        if n:
            report.issues.append(Issue(
                "R2.8", LEVEL_WARN,
                f"日期间隔 > {GAP_CALENDAR_DAYS} 天 {n} 处（长假或漏拉，需确认）", n,
                [f"{valid_dates.sort_values().iloc[i-1].date()}→{valid_dates.sort_values().iloc[i].date()}"
                 for i in np.flatnonzero(big.to_numpy())[:5]]))

    # R2.9 复权因子单调不减
    # 判为 WARN 而非 ERROR：实测平安银行 sz.000001 在 2020-12-31 因子从
    # 119.96 降到 99.79（-16%），但 baostock 自己的后复权价（adjustflag=1）
    # 在同一天也是这么跳的，我方计算结果与之逐位一致（差 4.5e-13）。
    # 即这是**数据源的因子重算**，不是我们算错。但它意味着该日前后
    # 的后复权价不可直接比较，长周期回测收益会有偏差，必须提示人工。
    if events is not None and len(events) and "backAdjustFactor" in events.columns:
        f = pd.to_numeric(events["backAdjustFactor"], errors="coerce")
        drop = f.diff() < -EPS
        n = int(drop.sum())
        if n:
            samples = [f"{d} {v:.6f}" for d, v in
                       zip(events.loc[drop, "dividOperateDate"], f[drop])][:5]
            report.issues.append(Issue(
                "R2.9", LEVEL_WARN,
                f"backAdjustFactor 非单调不减 {n} 处（数据源因子重算，"
                f"该日前后的后复权价不可直接比较）", n, samples))

    return report


def validate_file(path: str, symbol: Optional[str] = None,
                  adjust_store: Optional[AdjustmentStore] = None) -> Report:
    """读取 CSV 并校验。"""
    name = symbol
    if not name:
        import os
        name = os.path.basename(path)
    try:
        df = pd.read_csv(path)
    except Exception as e:
        rep = Report(symbol=normalize_bs_code(name), path=str(path))
        rep.issues.append(Issue("R0.1", LEVEL_ERROR, f"读取失败: {e}"))
        return rep
    return validate_dataframe(df, symbol=name, path=path, adjust_store=adjust_store)
