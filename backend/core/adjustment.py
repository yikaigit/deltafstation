"""
复权模块 —— baostock 口径

设计原则（与 docs/data-source-and-adjustment.md 一致）
-------------------------------------------------
1. 本地只存「不复权价 + 除权事件表」，**不预计算每日复权价落盘**。
   事件表通常只有几十行；展开成每日序列会到 5000 行/只，存储与 IO 都不划算。
2. 后复权价 = 不复权价 × backAdjustFactor
   → 只依赖截至当日的事件，**点-in-time 天然安全**，回测主价格序列用它。
3. 前复权价 = 后复权价 / 基准日 backAdjustFactor
   → 分母用到未来事件，**天然带未来函数**，仅用于图表展示。
4. 除权日**当天**即启用新因子（不是次日）。
   已用浦发银行 2026-07-16 逐位验证：
       不复权 9.31 → 8.85       看似跌 4.9%
       后复权 118.833 → 118.298   实际跌 0.45%
       反推因子 12.7637(07-15) / 13.3670(07-16)
       13.367013 == 事件表 2026-07-16 行 backAdjustFactor
5. 指数没有复权因子（实测 query_adjust_factor 返回 0 行）→ 因子恒为 1.0，
   后复权价 == 不复权价，基准对比天然正确。

对外
    BS_LOCK                 baostock 全局单连接串行锁
    normalize_bs_code       标的代码归一到 sh./sz. 格式
    AdjustmentStore         事件表存取 + 因子展开 + 复权价计算
"""
import os
import threading
from typing import Optional, Sequence

import numpy as np
import pandas as pd

try:  # pragma: no cover - baostock 缺失时模块仍可导入（纯计算路径可用）
    import baostock as bs
except Exception:
    bs = None


#: baostock 依赖全局单连接（每次调用 login/logout），所有网络请求必须串行。
#: data_manager 的 _FETCH_LOCK 直接复用本锁，避免两把锁互相打架。
BS_LOCK = threading.RLock()

PRICE_COLUMNS = ("Open", "High", "Low", "Close")
EVENT_COLUMNS = ("dividOperateDate", "foreAdjustFactor", "backAdjustFactor", "adjustFactor")

MODE_NONE = "none"     # 不复权
MODE_BACK = "back"     # 后复权（回测默认，点-in-time 安全）
MODE_FORWARD = "forward"  # 前复权（仅用于展示）

_DEFAULT_EVENT_START = "1990-01-01"


def normalize_bs_code(symbol: str) -> str:
    """归一到 baostock 代码格式。

    支持 ``600000.SH`` / ``000001.SZ`` / ``sh.600000`` / ``600000`` / ``sh.600000.csv``。
    """
    s = (symbol or "").strip()
    if not s:
        return s
    # 去掉可能的 .csv 后缀
    if s.lower().endswith(".csv"):
        s = s[:-4]
    low = s.lower()
    if low[:3] in ("sh.", "sz.", "bj."):
        return low
    upper = s.upper()
    if upper.endswith(".SH") or upper.endswith(".SZ") or upper.endswith(".BJ"):
        return upper[-2:].lower() + "." + s.split(".")[0]
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 6:
        head = digits[-6:]
        first = head[0]
        if first in ("6", "9"):
            return "sh." + head
        if first in ("0", "2", "3"):
            return "sz." + head
        if first in ("4", "8"):
            return "bj." + head
    return low


class AdjustmentStore:
    """复权因子事件表的存取与复权价计算。"""

    def __init__(self, data_folder: str):
        self.data_folder = data_folder
        self.raw_folder = os.path.join(data_folder, "raw")
        self.adjust_folder = os.path.join(data_folder, "adjust")
        self.meta_folder = os.path.join(data_folder, "meta")
        for path in (self.raw_folder, self.adjust_folder, self.meta_folder):
            os.makedirs(path, exist_ok=True)

    # ---------- 事件表存取 ----------

    def event_path(self, symbol: str) -> str:
        return os.path.join(self.adjust_folder, f"{normalize_bs_code(symbol)}.csv")

    def load_events(self, symbol: str) -> Optional[pd.DataFrame]:
        """读取本地事件表；不存在返回 None（与「无除权事件」区分开）。"""
        path = self.event_path(symbol)
        if not os.path.exists(path):
            return None
        try:
            df = pd.read_csv(path)
        except Exception:
            return None
        if df.empty or "dividOperateDate" not in df.columns:
            return pd.DataFrame(columns=list(EVENT_COLUMNS))
        return df

    def save_events(self, symbol: str, events: pd.DataFrame) -> str:
        path = self.event_path(symbol)
        if events is None:
            events = pd.DataFrame(columns=list(EVENT_COLUMNS))
        columns = [c for c in EVENT_COLUMNS if c in events.columns]
        events = events.reindex(columns=columns)
        events.to_csv(path, index=False)
        return path

    def fetch_events(self, symbol: str, start_date: Optional[str] = None,
                     end_date: Optional[str] = None) -> pd.DataFrame:
        """从 baostock 拉取除权事件表（串行，内部 login/logout）。

        ⚠️ 始终拉取**全历史**（忽略 start_date/end_date）。
        backAdjustFactor 是从上市日起的**累计**因子：若按请求区间过滤，
        早于首个返回事件的日期会缺失因子，后复权价会算成不复权价。
        实测反例：浦发 sh.600000 只拉 2026-06-01 之后 → 仅 1 条事件，
        2026-06-01~07-15 的因子被当成 1.0，后复权价从 118 掉到 9（差 92%）。
        事件表通常只有几十行，全拉成本可忽略。
        """
        if bs is None:
            raise RuntimeError("baostock is not installed")
        code = normalize_bs_code(symbol)
        start = _DEFAULT_EVENT_START
        end = pd.Timestamp.now().strftime("%Y-%m-%d")

        with BS_LOCK:
            bs.login()
            try:
                rs = bs.query_adjust_factor(code=code, start_date=start, end_date=end)
                fields = list(getattr(rs, "fields", []) or [])
                rows = []
                while rs.error_code == "0" and rs.next():
                    rows.append(rs.get_row_data())
            finally:
                try:
                    bs.logout()
                except Exception:
                    pass

        if not fields:
            fields = list(EVENT_COLUMNS)
        df = pd.DataFrame(rows, columns=fields)
        if "dividOperateDate" in df.columns and not df.empty:
            df = df.sort_values("dividOperateDate").reset_index(drop=True)
        return df

    def ensure_events(self, symbol: str, start_date: Optional[str] = None,
                      end_date: Optional[str] = None, force: bool = False) -> pd.DataFrame:
        """优先读本地，缺失或 force 时联网拉取并落盘。"""
        if not force:
            cached = self.load_events(symbol)
            if cached is not None:
                return cached
        events = self.fetch_events(symbol, start_date=start_date, end_date=end_date)
        self.save_events(symbol, events)
        return events

    # ---------- 因子展开 ----------

    @staticmethod
    def factor_from_events(events: Optional[pd.DataFrame], dates: pd.DatetimeIndex) -> np.ndarray:
        """把除权事件表展开为每日 backAdjustFactor。

        规则：``factor(d)`` = 最后一条 ``dividOperateDate <= d`` 的 backAdjustFactor；
        d 早于首个除权日则为 1.0。**晚于窗口末日的事件不参与**（点-in-time）。
        """
        n = len(dates)
        if n == 0:
            return np.ones(0, dtype="float64")
        if events is None or len(events) == 0 or "dividOperateDate" not in events.columns:
            return np.ones(n, dtype="float64")

        ev = events.copy()
        ev["_d"] = pd.to_datetime(ev["dividOperateDate"], errors="coerce")
        ev["_f"] = pd.to_numeric(ev.get("backAdjustFactor"), errors="coerce")
        ev = ev.dropna(subset=["_d", "_f"]).sort_values("_d")
        if ev.empty:
            return np.ones(n, dtype="float64")

        ev_d = ev["_d"].to_numpy()
        ev_f = ev["_f"].to_numpy(dtype="float64")
        target = pd.to_datetime(dates).to_numpy()
        # 点-in-time：剔除晚于窗口末日的事件
        horizon = target.max()
        keep = ev_d <= horizon
        ev_d, ev_f = ev_d[keep], ev_f[keep]
        if len(ev_d) == 0:
            return np.ones(n, dtype="float64")

        pos = np.searchsorted(ev_d, target, side="right") - 1
        factor = np.where(pos >= 0, ev_f[np.clip(pos, 0, len(ev_f) - 1)], 1.0)
        return factor.astype("float64")

    @staticmethod
    def looks_truncated(events: Optional[pd.DataFrame], first_date) -> bool:
        """事件表疑似被截断（会把后复权价算成不复权价）。

        判据：首个事件日晚于数据首日，且首个事件的 backAdjustFactor 明显大于 1
        —— 说明该股在首个事件之前还有未被拉到的除权历史。
        """
        if events is None or len(events) == 0 or first_date is None:
            return False
        try:
            ev_first = pd.to_datetime(events["dividOperateDate"]).min()
            data_first = pd.Timestamp(first_date)
        except Exception:
            return False
        if ev_first <= data_first:
            return False
        try:
            head_factor = float(pd.to_numeric(events["backAdjustFactor"]).iloc[0])
        except Exception:
            return False
        return head_factor > 1.5

    def daily_factor(self, symbol: str, dates: Sequence,
                     events: Optional[pd.DataFrame] = None) -> np.ndarray:
        if events is None:
            events = self.load_events(symbol)
        return self.factor_from_events(events, pd.DatetimeIndex(pd.to_datetime(list(dates))))

    # ---------- 复权价计算 ----------

    def apply(self, df: pd.DataFrame, symbol: str, mode: str = MODE_BACK,
              base_date: Optional[str] = None, adjust_volume: bool = False,
              events: Optional[pd.DataFrame] = None) -> pd.DataFrame:
        """按指定口径计算复权价，返回新 DataFrame（不改动入参）。

        Args:
            df: 含 Date/Open/High/Low/Close/Volume 的不复权数据
            mode: none | back | forward
            base_date: 前复权基准日，None 表示以数据末日为基准
            adjust_volume: 是否同步复权成交量（Volume ÷ factor）。首期默认 False。
        """
        if df is None or df.empty or "Date" not in df.columns:
            return df
        if mode == MODE_NONE:
            return df.copy()

        dates = pd.to_datetime(df["Date"], errors="coerce")
        if events is None:
            events = self.load_events(symbol)
        factor = self.factor_from_events(events, pd.DatetimeIndex(dates))

        if mode == MODE_BACK:
            scale = factor
        elif mode == MODE_FORWARD:
            if base_date is None:
                base = float(factor[-1])
            else:
                target = pd.to_datetime(dates).to_numpy()
                idx = int(np.searchsorted(target, np.datetime64(pd.Timestamp(base_date)), side="right")) - 1
                base = float(factor[idx]) if idx >= 0 and idx < len(factor) else 1.0
            scale = factor / base if base else factor
        else:
            raise ValueError(f"Unsupported adjust mode: {mode}")

        out = df.copy()
        for col in PRICE_COLUMNS:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce") * scale
        if adjust_volume and "Volume" in out.columns:
            vol = pd.to_numeric(out["Volume"], errors="coerce")
            out["Volume"] = vol / np.where(scale == 0, 1.0, scale)
        return out

    # ---------- 辅助 ----------

    @staticmethod
    def is_index(symbol: str) -> bool:
        """粗略判断是否指数（指数无复权因子）。"""
        code = normalize_bs_code(symbol)
        head = code.split(".")[-1]
        return head.startswith(("000", "999", "399")) and len(head) == 6
