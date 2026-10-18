"""数据源封装。

对外只暴露两个入口：

    from backend.core.providers import get_baostock, get_eastmoney

分工（见 docs/system-spec.md §4.1）：
* baostock —— 日线行情（不复权）、复权因子、交易日历、HS300 成分、标的基础信息
* 东财 —— 全市场估值横截面、财务报表（含公告日）
"""
from .baostock_provider import BaostockProvider, get_baostock
from .base import ProviderError, Progress, RateLimiter, retry
from .eastmoney_provider import EastmoneyProvider, get_eastmoney

__all__ = [
    "BaostockProvider",
    "EastmoneyProvider",
    "ProviderError",
    "Progress",
    "RateLimiter",
    "get_baostock",
    "get_eastmoney",
    "retry",
]
