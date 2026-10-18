"""baostock 数据源：日线行情、复权因子、交易日历、HS300 成分、标的基础信息。

关键约束
--------
baostock 是**全局单连接**库（login/logout 是进程级状态），因此：
1. 所有调用必须串行 —— 用模块级 ``BS_LOCK`` 保护；
2. login/logout 各有一次网络往返，逐只股票开关连接会浪费大量时间，
   因此提供 ``session()`` 上下文，批量调用时只登录一次；
3. ``session()`` 可重入，方法内部自动包一层，未显式开 session 也不会错。

数据口径
--------
* 日线一律取 ``adjustflag="3"``（不复权），复权在本地用因子展开（见 adjustment.py）；
* 停牌日 baostock **会返回行**，表现为 ``O==H==L==C 且 volume==0``、``tradestatus=0``，
  入库时按 ``tradestatus`` 保留标记，由校验器与回测层决定是否剔除；
* 复权因子必须**拉全历史**：backAdjustFactor 是自上市起的累计因子，
  按区间截断会让区间前的价格因子退化为 1.0（后复权价严重失真）。
"""
from __future__ import annotations

import logging
import os
import socket
import sys
import threading
from contextlib import contextmanager
from typing import Iterator, List, Optional, Sequence

import pandas as pd

from ..codes import normalize_bs_code
from .base import ProviderError, RateLimiter, retry

logger = logging.getLogger(__name__)

try:  # pragma: no cover - 缺依赖时纯计算路径仍可用
    import baostock as bs
except Exception:  # pragma: no cover
    bs = None  # type: ignore[assignment]

#: 单次网络读超时（秒）。
#: baostock 用裸 socket 做 ``recv(8192)`` 的无限循环，服务端一卡就永久阻塞；
#: 实测过整段回填挂死的情况，因此必须给 socket 设超时。
SOCKET_TIMEOUT = 45.0

#: 已应用过默认超时（socket.setdefaulttimeout 是进程级全局设置，只需设一次）
_timeout_applied = False


def _apply_socket_timeout(timeout: float = SOCKET_TIMEOUT) -> None:
    global _timeout_applied
    if _timeout_applied:
        return
    socket.setdefaulttimeout(timeout)
    _timeout_applied = True


@contextmanager
def _quiet():
    """屏蔽 baostock 往 stdout 打印的 ``login success!`` 之类噪音。"""
    saved = sys.stdout
    try:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        yield
    finally:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = saved

#: baostock 全局单连接串行锁（可重入，支持 session 嵌套）
BS_LOCK = threading.RLock()

#: 日线字段（顺序即请求顺序）
BAR_FIELDS = (
    "date,code,open,high,low,close,preclose,volume,amount,turn,tradestatus,pctChg,isST"
)

NUMERIC_BAR_COLUMNS = ("open", "high", "low", "close", "preclose", "volume", "amount", "turn", "pct_chg")
INT_BAR_COLUMNS = ("tradestatus",)

#: 复权事件表起始日（覆盖 A 股全部历史）
EVENT_START = "1990-01-01"

#: baostock 的网络故障**文案**（见 ``util/socketutil.py`` 的 ``send_msg``）。
#:
#: ⚠️ 这个库把 socket 异常全部吞掉再转成错误码返回：
#:
#:     except Exception as ex:
#:         print(ex); print("接收数据异常，请稍后再试。"); return None
#:
#: 也就是说 ``recv`` 超时**不会**以 ``TimeoutError`` 冒泡到调用方，只会变成
#: ``error_msg="网络接收错误。"``。更糟的是超时点在 ``while True: recv()`` 循环
#: 中间时，socket 缓冲区里留着**半个包**，连接虽未关闭但已错位 ——
#: 后续请求会读到上次残留的字节，表现为 ``'utf-8' codec can't decode byte 0x8f``。
#: 因此必须靠错误文案识别网络故障并主动重置连接，不能只依赖异常类型。
NETWORK_ERROR_HINTS = (
    "网络接收错误",
    "网络发送错误",
    "服务器连接失败",
    "接收数据异常",
    "发送数据异常",
)


def _looks_like_network_error(message: str) -> bool:
    return any(hint in message for hint in NETWORK_ERROR_HINTS)


class BaostockProvider:
    """baostock 封装。线程安全，可批量复用同一连接。

    连接韧性
    --------
    服务端偶发卡住（``recv`` 永久阻塞）或主动断连，因此：
    * 所有 socket 设 45s 读超时，卡住会抛 ``TimeoutError`` 而不是挂死进程；
    * 一旦出错就把连接标记为 broken 并关闭，下一次调用自动重连；
    * 单只标的失败由上层记录并继续，不影响整批。
    """

    def __init__(self, min_interval: float = 0.15, socket_timeout: float = SOCKET_TIMEOUT):
        _apply_socket_timeout(socket_timeout)
        self.limiter = RateLimiter(min_interval)
        self._local = threading.local()
        self._broken = False

    # ---------- 连接管理 ----------

    @property
    def available(self) -> bool:
        return bs is not None

    def _require(self) -> None:
        if bs is None:
            raise ProviderError("未安装 baostock，请执行 pip install baostock")

    def _login(self) -> None:
        with _quiet():
            result = bs.login()
        if getattr(result, "error_code", "0") != "0":
            raise ProviderError(f"baostock 登录失败：{getattr(result, 'error_msg', '')}")
        self._broken = False

    def _hard_close(self) -> None:
        """强制关闭底层 socket（``logout`` 在连接已坏时本身也会阻塞）。"""
        try:
            from baostock.common import context

            sock = getattr(context, "default_socket", None)
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
                context.default_socket = None
        except Exception:
            pass
        try:
            with _quiet():
                bs.logout()
        except Exception:
            pass
        self._broken = True

    def _ensure_connection(self) -> None:
        if self._broken:
            logger.info("baostock 连接已断开，重新登录")
            self._login()

    @property
    def _depth(self) -> int:
        """当前线程的 session 嵌套深度。

        注意：``threading.local()`` 的属性只对**赋值过的线程**可见，
        在 ``__init__`` 里赋值只影响构造线程，别的线程读到就是 AttributeError。
        因此这里一律走 ``getattr`` 兜底，缺省视为 0（未进入 session）。
        """
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    @contextmanager
    def session(self) -> Iterator["BaostockProvider"]:
        """登录会话（可重入）。批量调用时包在外层，只登录一次。"""
        self._require()
        outermost = self._depth == 0
        if outermost:
            BS_LOCK.acquire()
            try:
                self._login()
            except Exception:
                BS_LOCK.release()
                raise
        else:
            # 内层调用：连接可能在上一次调用中坏掉，就地重连（RLock 同线程可重入）
            self._ensure_connection()

        self._depth = self._depth + 1
        try:
            yield self
        finally:
            self._depth = self._depth - 1
            if outermost:
                if not self._broken:
                    try:
                        with _quiet():
                            bs.logout()
                    except Exception:  # 登出失败不影响结果
                        pass
                BS_LOCK.release()

    # ---------- 内部工具 ----------

    @staticmethod
    def _drain(result, what: str) -> pd.DataFrame:
        """把 baostock 的 ResultData 游标读成 DataFrame。"""
        if result is None:
            raise ProviderError(f"{what}：返回为空")
        code = getattr(result, "error_code", "0")
        if code != "0":
            raise ProviderError(f"{what} 失败：{getattr(result, 'error_msg', code)}")
        fields = list(getattr(result, "fields", []) or [])
        rows: List[List[str]] = []
        while result.next():
            rows.append(result.get_row_data())
        return pd.DataFrame(rows, columns=fields) if fields else pd.DataFrame()

    def _call(self, what: str, fn, *args, **kwargs) -> pd.DataFrame:
        with self.session():
            self.limiter.wait()
            try:
                return self._drain(fn(*args, **kwargs), what)
            except (TimeoutError, socket.timeout, OSError) as exc:
                # 连接层面的问题：立刻放弃这条连接，交给 @retry 重新登录
                logger.warning("%s 网络异常（%s），重置连接", what, type(exc).__name__)
                self._hard_close()
                raise ProviderError(f"{what} 网络超时/中断：{exc}") from exc
            except ProviderError as exc:
                # 上面那条分支实际很少走到：baostock 把 socket 异常吞成了错误码。
                # 这里补上文案识别，否则连接会带着错位的缓冲区继续被复用。
                if _looks_like_network_error(str(exc)):
                    logger.warning("%s 数据源报告网络异常，重置连接：%s", what, exc)
                    self._hard_close()
                raise

    # ---------- 交易日历 ----------

    def trade_dates(self, start_date: str, end_date: str) -> pd.DataFrame:
        """交易日历，返回 ``[date, is_trading_day]``。"""
        raw = self._call(
            "query_trade_dates", bs.query_trade_dates, start_date=start_date, end_date=end_date
        )
        if raw.empty:
            return pd.DataFrame(columns=["date", "is_trading_day"])
        out = pd.DataFrame(
            {
                "date": pd.to_datetime(raw["calendar_date"], errors="coerce"),
                "is_trading_day": raw["is_trading_day"].astype(str).eq("1"),
            }
        )
        return out.dropna(subset=["date"]).reset_index(drop=True)

    # ---------- 指数成分 ----------

    def hs300_stocks(self) -> pd.DataFrame:
        """当前沪深300 成分，返回 ``[snapshot_date, code, name]``。

        ⚠️ baostock 只返回**当前**成分，无历史成分（见规格 §4.7 幸存者偏差）。
        """
        raw = self._call("query_hs300_stocks", bs.query_hs300_stocks)
        if raw.empty:
            return pd.DataFrame(columns=["snapshot_date", "code", "name"])
        out = pd.DataFrame(
            {
                "snapshot_date": pd.to_datetime(raw["updateDate"], errors="coerce"),
                "code": raw["code"].map(normalize_bs_code),
                "name": raw["code_name"],
            }
        )
        return out.dropna(subset=["snapshot_date"]).drop_duplicates("code").reset_index(drop=True)

    # ---------- 标的基础信息 ----------

    def stock_basic(self, code: str) -> dict:
        """单只标的基础信息：ipoDate / outDate / type / status / name。"""
        raw = self._call("query_stock_basic", bs.query_stock_basic, code=normalize_bs_code(code))
        if raw.empty:
            return {}
        row = raw.iloc[0].to_dict()
        return {
            "code": normalize_bs_code(row.get("code", code)),
            "name": row.get("code_name"),
            "list_date": _to_date(row.get("ipoDate")),
            "delist_date": _to_date(row.get("outDate")),
            "sec_type": row.get("type"),
            "status": row.get("status"),
        }

    # ---------- 日线 ----------

    @retry(times=3, delay=1.5)
    def daily_bars(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """不复权日线。返回列与数据湖 bars 一致（不含 code 排序问题）。"""
        bs_code = normalize_bs_code(code)
        raw = self._call(
            "query_history_k_data_plus",
            bs.query_history_k_data_plus,
            bs_code,
            BAR_FIELDS,
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="3",
        )
        if raw.empty:
            return _empty_bars()
        return _normalize_bars(raw, bs_code)

    # ---------- 复权因子 ----------

    @retry(times=3, delay=1.5)
    def adjust_factors(self, code: str) -> pd.DataFrame:
        """复权事件表（**全历史**）。返回 ``[code, ex_date, fore_factor, back_factor]``。

        指数无复权因子，返回空表。
        """
        bs_code = normalize_bs_code(code)
        raw = self._call(
            "query_adjust_factor",
            bs.query_adjust_factor,
            code=bs_code,
            start_date=EVENT_START,
            end_date=pd.Timestamp.today().strftime("%Y-%m-%d"),
        )
        if raw.empty:
            return pd.DataFrame(columns=["code", "ex_date", "fore_factor", "back_factor"])
        out = pd.DataFrame(
            {
                "code": bs_code,
                "ex_date": pd.to_datetime(raw["dividOperateDate"], errors="coerce"),
                "fore_factor": pd.to_numeric(raw["foreAdjustFactor"], errors="coerce"),
                "back_factor": pd.to_numeric(raw["backAdjustFactor"], errors="coerce"),
            }
        )
        return (
            out.dropna(subset=["ex_date", "back_factor"])
            .drop_duplicates("ex_date", keep="last")
            .sort_values("ex_date")
            .reset_index(drop=True)
        )

    # ---------- 批量 ----------

    def many_bars(
        self,
        codes: Sequence[str],
        start_date: str,
        end_date: str,
        on_item=None,
        on_error=None,
    ) -> tuple[pd.DataFrame, List[dict]]:
        """批量拉日线（单连接、串行、逐只容错）。

        Returns:
            (合并后的 DataFrame, 失败清单)
        """
        frames = []
        failures: List[dict] = []
        with self.session():
            for index, code in enumerate(codes, 1):
                try:
                    frame = self.daily_bars(code, start_date, end_date)
                    if not frame.empty:
                        frames.append(frame)
                    if on_item:
                        on_item(index, len(codes), code, len(frame))
                except Exception as exc:
                    logger.warning("拉取 %s 日线失败：%s", code, exc)
                    failures.append({"code": code, "error": str(exc)})
                    if on_error:
                        on_error(code, exc)
        merged = pd.concat(frames, ignore_index=True) if frames else _empty_bars()
        return merged, failures

    def many_adjust_factors(self, codes: Sequence[str], on_item=None) -> tuple[pd.DataFrame, List[dict]]:
        """批量拉复权因子。"""
        frames = []
        failures: List[dict] = []
        with self.session():
            for index, code in enumerate(codes, 1):
                try:
                    frame = self.adjust_factors(code)
                    if not frame.empty:
                        frames.append(frame)
                    if on_item:
                        on_item(index, len(codes), code, len(frame))
                except Exception as exc:
                    logger.warning("拉取 %s 复权因子失败：%s", code, exc)
                    failures.append({"code": code, "error": str(exc)})
        merged = (
            pd.concat(frames, ignore_index=True)
            if frames
            else pd.DataFrame(columns=["code", "ex_date", "fore_factor", "back_factor"])
        )
        return merged, failures


# ---------- 纯函数 ----------


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "date", "code", "open", "high", "low", "close", "preclose",
            "volume", "amount", "turn", "tradestatus", "pct_chg", "is_st",
        ]
    )


def _normalize_bars(raw: pd.DataFrame, bs_code: str) -> pd.DataFrame:
    """把 baostock 原始字符串表转成带类型的行情表。

    ⚠️ 停牌日（``tradestatus=0``）baostock 会返回行，但 ``volume/amount/turn/pctChg``
    给的是**空字符串**而不是 ``0``，直接 ``to_numeric`` 会变成 NaN。
    实测 19/300 只标的存在这种情况（如 sh.600150 2024-09-03~09-18 连续停牌）。
    NaN 会污染下游指标计算，也会被校验器判成脏数据，因此在源头补 0 ——
    停牌意味着没有成交，这些字段的语义就是 0，不是缺失。
    """
    status = pd.to_numeric(raw["tradestatus"], errors="coerce").fillna(0).astype("int8")
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(raw["date"], errors="coerce"),
            "code": bs_code,
            "open": pd.to_numeric(raw["open"], errors="coerce"),
            "high": pd.to_numeric(raw["high"], errors="coerce"),
            "low": pd.to_numeric(raw["low"], errors="coerce"),
            "close": pd.to_numeric(raw["close"], errors="coerce"),
            "preclose": pd.to_numeric(raw["preclose"], errors="coerce"),
            "volume": pd.to_numeric(raw["volume"], errors="coerce"),
            "amount": pd.to_numeric(raw["amount"], errors="coerce"),
            "turn": pd.to_numeric(raw["turn"], errors="coerce"),
            "tradestatus": status,
            "pct_chg": pd.to_numeric(raw["pctChg"], errors="coerce"),
            "is_st": raw["isST"].astype(str).eq("1"),
        }
    )
    halted = out["tradestatus"] == 0
    if halted.any():
        for col in ("volume", "amount", "turn", "pct_chg"):
            out.loc[halted, col] = out.loc[halted, col].fillna(0.0)
    # 成交股数语义上是整数；转 int64 可避免下游把 1.0 当浮点显示，
    # 也顺带让校验器 R1.6 的浮点精度告警只在真丢精度时触发。
    if not out["volume"].isna().any():
        out["volume"] = out["volume"].astype("int64")
    return out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)


def _to_date(value) -> Optional[pd.Timestamp]:
    if value in (None, "", "None"):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed


_provider: Optional[BaostockProvider] = None


def get_baostock() -> BaostockProvider:
    """进程内共享实例（共享限速器，避免并发时把数据源打爆）。"""
    global _provider
    if _provider is None:
        _provider = BaostockProvider()
    return _provider
