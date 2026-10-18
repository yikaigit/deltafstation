"""交易成本模型（规格 §6.2）。

三项成本
--------
| 项 | 口径 |
|---|---|
| 佣金 | 双边万 2.5（0.025%），单笔最低 5 元 |
| 印花税 | 仅卖出万 5（0.05%） |
| 滑点 | 双边万 5（0.05%） |
| 单轮合计 | ≈ 0.20%（买 0.075% + 卖 0.125%）|

**为什么比参考项目的「统一万 7.5」更细**：参考项目为了不低估成本，对买卖两端
统一按 0.075% 计费。本引擎按真实费种拆分——买入只有佣金+滑点 0.075%、
卖出佣金+滑点+印花税 0.125%，单轮 0.20%，比参考口径更保守。

复权空间与名义金额
----------------
引擎的记账分两层，两层各自自洽、不再互相换算：

* **现金与股数用真实货币**。目标金额 = 权重 × 组合净值，股数 = 目标金额 / **不复权价**。
  资金的进出、佣金的 5 元下限、印花税率、买得起几手，全都是真实钱的口径。
  —— 这里踩过一个坑：早期版本用「后复权净值 / 后复权价」算股数，等于把真实现金
  按个股的复权因子缩水。一只 back_factor=50 的股票，目标 5 万只能买到真实
  1,000 元，整个回测有一半以上资金趴在现金里，而报告上看不出来。
* **持仓估值用后复权价**，但记的是「time-0 股数」``n_eff``：买入时
  ``n_eff += 股数 / back_factor``，卖出时 ``n_eff -= 股数 / back_factor``。
  于是持仓市值 = ``n_eff × 后复权价(t)`` 恰好等于「这批钱按不复权价买入、
  持有期间分红再投、在 t 日的总值」。买卖瞬间 ``(股数/f) × (p×f) = 股数×p``
  与现金扣减严格相等，净值曲线连续，不会在每次调仓时跳变。

费用（``trade_cost``）一律按名义金额算、返回**真实元**：
佣金最低 5 元、印花税率这些规则服务的是真实的钱，按名义金额计才保持真实约束。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CostModel:
    """A 股交易成本（保守口径）。"""

    commission: float = 0.00025      # 双边佣金
    min_commission: float = 5.0      # 单笔最低佣金（元）
    stamp_duty: float = 0.0005       # 印花税，仅卖出
    slippage: float = 0.0005         # 双边滑点
    lot_size: int = 100              # 最小交易单位（股）

    def trade_cost(
        self,
        shares: float,
        price: float,
        side: str,
    ) -> float:
        """单笔交易的总成本，返回**真实元**（直接与真实现金对冲）。

        Args:
            shares: 成交股数
            price: 成交用的**不复权**价（佣金/印花税/滑点都按真实成交金额计）
            side: "buy" / "sell"
        """
        if shares <= 0 or price <= 0:
            return 0.0
        notional = shares * price                     # 名义成交金额（元）
        commission = max(notional * self.commission, self.min_commission)
        stamp = notional * self.stamp_duty if side == "sell" else 0.0
        slip = notional * self.slippage
        return commission + stamp + slip

    def round_lot(self, shares: float) -> float:
        """向下取整到「手」。lot_size<=0 表示关闭最小单位约束。"""
        if self.lot_size <= 0 or shares <= 0:
            return max(0.0, shares)
        return float(int(shares // self.lot_size) * self.lot_size)

    def max_affordable_shares(
        self,
        cash: float,
        raw_price: float,
        side: str,
    ) -> float:
        """给定真实现金最多能买多少股（把费用也算进去）。

        ⚠️ 不能用 ``cash / raw_price``：佣金有最低 5 元，滑点和印花税也要占钱，
        不考虑费用会算出「买了之后现金变负」的不可执行方案。

        两种情形分别解析求解：

        * 佣金按比例计（未触最低）：``cost = n × p × (1 + rate)``
          → ``n = cash / (p × (1 + rate))``
        * 触最低佣金：佣金是**常数偏移** ``min_commission``
          → ``n = (cash - 偏移) / (p × (1 + rate))``

        返回值是上界，调用方再按「手」向下取整只会更保守。
        """
        if cash <= 0 or raw_price <= 0:
            return 0.0
        rate = self.commission + self.slippage + (
            self.stamp_duty if side == "sell" else 0.0
        )
        proportional = cash / (raw_price * (1.0 + rate))
        if proportional * raw_price * self.commission >= self.min_commission:
            return proportional
        if cash <= self.min_commission:
            return 0.0
        return (cash - self.min_commission) / (raw_price * (1.0 + rate))

    def describe(self) -> dict:
        """写进回测结果，保证任何一份报告都能自证费率口径。"""
        return {
            "commission": self.commission,
            "min_commission": self.min_commission,
            "stamp_duty": self.stamp_duty,
            "slippage": self.slippage,
            "lot_size": self.lot_size,
            "one_way_buy": self.commission + self.slippage,
            "one_way_sell": self.commission + self.slippage + self.stamp_duty,
            "note": "佣金万2.5(最低5元)+印花税卖出万5+滑点万5；单轮约0.20%",
        }

    @classmethod
    def from_config(cls, cfg) -> "CostModel":
        """从配置 ``costs`` 段构造（缺项回落到默认保守值）。"""
        return cls(
            commission=float(cfg.get("costs.commission", 0.00025)),
            min_commission=float(cfg.get("costs.min_commission", 5.0)),
            stamp_duty=float(cfg.get("costs.stamp_duty", 0.0005)),
            slippage=float(cfg.get("costs.slippage", 0.0005)),
            lot_size=int(cfg.get("costs.lot_size", 100)),
        )


def limit_reason(
    open_price: float,
    preclose: float,
    limit_pct_value: float,
    tolerance: float = 0.002,
) -> str:
    """判定开盘价的涨跌停状态（规格 §6.2）。

    Args:
        open_price: T+1 开盘价（不复权）
        preclose: T 日收盘价（不复权，涨跌停的基准）
        limit_pct_value: 涨跌停幅度，如 0.10
        tolerance: 阈值容忍（2 分钱，规避四舍五入误差）

    Returns:
        "up" 涨停 / "down" 跌停 / "" 正常
    """
    if open_price is None or preclose is None or open_price <= 0 or preclose <= 0:
        return ""
    if open_price >= preclose * (1.0 + limit_pct_value) - tolerance:
        return "up"
    if open_price <= preclose * (1.0 - limit_pct_value) + tolerance:
        return "down"
    return ""


def limit_pct_for(code: str, on_date, is_st: bool = False) -> float:
    """按板块/日期取涨跌停幅度（复用 core.codes 的制度口径）。"""
    from ..codes import limit_pct

    return limit_pct(code, on_date=on_date, is_st=is_st)


def is_tradable(tradestatus: Optional[int], volume: Optional[float]) -> bool:
    """停牌判定：tradestatus=0 或缺量都视为不可交易。"""
    if tradestatus is not None and int(tradestatus) == 0:
        return False
    if volume is not None and volume == 0:
        return False
    return True
