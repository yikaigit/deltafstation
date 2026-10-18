"""标的代码归一、板块判定、涨跌停幅度。

内部统一使用 baostock 原生格式 ``sh.600000`` / ``sz.000001`` / ``bj.830799``。
东财接口用 6 位纯数字（``600000``）或 ``600000.SH``，统一在这里互转。

板块与涨跌停口径（A 股制度，回测与校验都要用）：
| 板块 | 代码特征 | 涨跌停 |
|---|---|---|
| 上交所主板 | sh.60xxxx | 10% |
| 科创板 | sh.688xxx | 20% |
| 深交所主板 | sz.000/001/002/003 | 10% |
| 创业板 | sz.300/301 | 2020-08-24 起 20%，此前 10% |
| 北交所 | bj.4/8 | 30% |
| ST（仅主板） | — | 5% |
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

#: 创业板注册制改革（涨跌幅由 10% 放宽到 20%）的生效日
GEM_REFORM_DATE = date(2020, 8, 24)

BOARD_MAIN = "main"
BOARD_GEM = "gem"
BOARD_STAR = "star"
BOARD_BJ = "bj"

BOARD_LABEL = {
    BOARD_MAIN: "主板",
    BOARD_GEM: "创业板",
    BOARD_STAR: "科创板",
    BOARD_BJ: "北交所",
}


def normalize_bs_code(symbol: str) -> str:
    """归一到 baostock 代码格式。

    支持 ``600000.SH`` / ``000001.SZ`` / ``sh.600000`` / ``600000`` / ``sh.600000.csv``。
    无法识别时原样返回小写（交由上层报错，不静默猜测）。
    """
    s = (symbol or "").strip()
    if not s:
        return s
    if s.lower().endswith(".csv"):
        s = s[:-4]
    low = s.lower()
    if low[:3] in ("sh.", "sz.", "bj."):
        return low

    upper = s.upper()
    for suffix, prefix in ((".SH", "sh."), (".SZ", "sz."), (".BJ", "bj.")):
        if upper.endswith(suffix):
            return prefix + s.split(".")[0]

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


def to_em_code(symbol: str) -> str:
    """转东财 6 位代码：``sh.600000`` → ``600000``。"""
    code = normalize_bs_code(symbol)
    return code.split(".")[-1] if "." in code else code


def to_bs_code(symbol: str) -> str:
    """把东财 ``600000`` / ``600000.SH`` 转成 ``sh.600000``。"""
    return normalize_bs_code(symbol)


def to_secucode(symbol: str) -> str:
    """转东财 SECUCODE 格式：``sh.600000`` → ``600000.SH``。"""
    code = normalize_bs_code(symbol)
    if "." not in code:
        return code
    prefix, digits = code.split(".", 1)
    return f"{digits}.{prefix.upper()}"


def board_of(symbol: str) -> str:
    """判定板块，返回 main / gem / star / bj。

    代码段（截至 2026 年）
    --------------------
    * 沪市主板 ``600/601/603/605``；科创板 ``688``；``900`` 为沪市 B 股
    * 深市主板 ``000/001/002/003``；创业板 ``300/301/302``；``200`` 为深市 B 股
    * 北交所 ``43/83/87/88/92``（新三板精选层平移后启用了 920 段）

    创业板代码段是逐步扩容的：2009 年开板用 ``300``，2023 年启用 ``301``，
    2024 年后启用 ``302``。只认 ``300/301`` 会把 ``sz.302132`` 误判成主板，
    涨跌停阈值从 20% 掉到 10%，回测里直接变成「涨停拒单」的假信号
    （实测该票 2025 年有 7 个 10%~20% 的正常波动被误报）。
    """
    code = normalize_bs_code(symbol)
    digits = code.split(".")[-1]
    if len(digits) != 6:
        return BOARD_MAIN
    if code.startswith("sh."):
        return BOARD_STAR if digits.startswith("688") else BOARD_MAIN
    if code.startswith("sz."):
        return BOARD_GEM if digits.startswith(("300", "301", "302")) else BOARD_MAIN
    if code.startswith("bj."):
        return BOARD_BJ
    return BOARD_MAIN


def board_label(symbol: str) -> str:
    return BOARD_LABEL.get(board_of(symbol), "主板")


def limit_pct(symbol: str, on_date: Optional[date | str] = None, is_st: bool = False) -> float:
    """涨跌停幅度（0.10 表示 10%）。

    Args:
        symbol: 标的代码
        on_date: 判定日期，创业板需按 2020-08-24 前后区分
        is_st: 是否 ST（主板 ST 收窄到 5%）
    """
    board = board_of(symbol)
    if board == BOARD_STAR:
        return 0.20
    if board == BOARD_BJ:
        return 0.30
    if board == BOARD_GEM:
        if on_date is None:
            return 0.20
        day = _as_date(on_date)
        return 0.20 if day >= GEM_REFORM_DATE else 0.10
    # 主板
    return 0.05 if is_st else 0.10


def limit_price(
    preclose: float, symbol: str, on_date: Optional[date | str] = None, is_st: bool = False
) -> tuple[float, float]:
    """按昨收计算涨停价与跌停价（四舍五入到分）。"""
    pct = limit_pct(symbol, on_date=on_date, is_st=is_st)
    return round(preclose * (1 + pct), 2), round(preclose * (1 - pct), 2)


def is_index(symbol: str) -> bool:
    """判断是否指数（指数无复权因子，因子恒 1.0）。

    指数代码空间：
    * ``sh.000xxx`` 上证系列指数（sh.000001 上证指数、sh.000300 沪深300）
    * ``sh.999xxx`` 上证旧编码
    * ``sz.399xxx`` 深证系列指数

    ⚠️ 不能只看数字前缀：``sz.000001`` 是平安银行（个股），``sh.000001`` 才是上证指数。
    """
    code = normalize_bs_code(symbol)
    if "." not in code:
        return False
    prefix, digits = code.split(".", 1)
    if len(digits) != 6:
        return False
    if prefix == "sh":
        return digits.startswith(("000", "999"))
    if prefix == "sz":
        return digits.startswith("399")
    return False


def _as_date(value: date | str) -> date:
    """统一成 ``date``。

    ⚠️ ``datetime``（含 ``pd.Timestamp``，它是 ``datetime`` 的子类）必须先
    降级成 ``date``：``isinstance(Timestamp, date)`` 为真，原样返回后跟
    ``date(2020, 8, 24)`` 比较会抛
    ``TypeError: Cannot compare Timestamp with datetime.date``。
    回测引擎按交易日历喂进来的正是 Timestamp，创业板成分股一多就炸。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)[:10]
    year, month, day = text.split("-")
    return date(int(year), int(month), int(day))
