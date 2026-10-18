"""回测引擎（规格 §6.2/6.3）。

口径（写进每一份回测报告，防止口径漂移）
--------------------------------------
* **信号时点**：T 日收盘后计算目标权重；
* **成交时点**：T+1 **开盘价**（调仓信号日的次一交易日）；
* **记账空间**：现金与股数按**真实货币**计 —— 目标金额 = 权重 × 组合净值，
  股数 = 目标金额 / **不复权价**，费用按名义金额从真实现金里扣；
  持仓估值按**后复权价**记「time-0 股数」``n_eff``（买卖时 ``n_eff ± 股数/back_factor``），
  于是 ``n_eff × 后复权价(t)`` = 这批钱按不复权价买入、持有期分红再投、在 t 日的总值。
  净值曲线因此自带红利再投资，与因子面板同口径；
* **不可买**：停牌（``tradestatus=0`` 或缺量）、涨停 → 放弃买入、资金留存；
* **不可卖**：停牌、跌停 → 卖不出、持仓顺延到下一调仓日；
* **先卖后买**：先释放现金再买入，避免同一笔钱被用两次；
* **不临时补配**：被挡下来的那一份权重留在现金里，不会分给别的股票 ——
  否则「目标权重」就不再是信号日算定的那个配方，回测不可复现。

⚠️ 曾经的坑：用「后复权净值 / 后复权价」算股数。后复权价 = 不复权价 × back_factor，
于是同样一笔「5% 权重」的钱，在 back_factor=50 的股票上只买到真实 1/50 的仓位。
实测后果：整个 5 年回测只投出去 14.5% 的资金、平均持仓 10 只而 top_n=20，
报告上却看不出来。现在的口径见上文「记账空间」与 ``costs.py`` 模块文档。

为什么不用 vectorbt
------------------
规格 D8：自研，不引入 vectorbt/numba（与 pandas 2.x 冲突）。
这里的实现是「逐日循环 + 只遍历持仓」：每天只碰 ≤ top_n 只股票，
4564 个交易日 × 30 只 ≈ 14 万次字典操作，比重建 300 列面板便宜两个数量级。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from . import metrics as mt
from .costs import CostModel, is_tradable, limit_pct_for, limit_reason
from .strategies import StrategyParams, build_target_weights, regime_exposure, resolve_st_codes

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """一次回测的全部产物。"""

    nav: pd.Series
    bench_nav: pd.Series
    returns: pd.Series
    bench_returns: pd.Series
    equity: pd.DataFrame
    trades: List[dict] = field(default_factory=list)
    holdings: List[dict] = field(default_factory=list)
    blocked: Dict[str, int] = field(default_factory=dict)
    rebalance_count: int = 0
    turnover: float = 0.0
    avg_positions: float = 0.0
    metrics: Dict[str, dict] = field(default_factory=dict)
    period: Dict[str, Optional[str]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    @property
    def total_return(self) -> float:
        if len(self.nav) < 2:
            return 0.0
        return float(self.nav.iloc[-1] / self.nav.iloc[0] - 1.0)


@dataclass
class _Account:
    """真实现金 + 「time-0 股数」。

    * ``cash``：真实元。资金进出、费用全部按真实口径。
    * ``eff``：「time-0 股数」。买入时 ``+= 股数/back_factor``，
      卖出时 ``-= 股数/back_factor``；持仓市值 = ``eff × 后复权价(t)``，
      **当前持股数** = ``eff × 当日 back_factor``。

    为什么需要 ``eff``：后复权价 ``adj(t) = 不复权价(t) × f(t)`` 的含义是
    「1 股自上市持有、分红再投、到 t 日的真实现金价值」。按 eff 记账时
    买卖瞬间 ``(股数/f) × (p×f) = 股数 × p``，与现金扣减严格相等，
    净值连续；同时持仓自带红利再投资，与因子面板同一口径。

    ⚠️ **对外暴露的股数必须是 ``eff × f(t)``，不是买入时的原始股数**：
    除息会抬升 f，等效股数随之同比增加（分红已再投）。若仍按原始股数
    去和目标比，清仓时只会卖掉一半 —— 实测留下几十只「幽灵持仓」，
    净值莫名其妙虚高。本类按 ``eff × 当日 f`` 取当前持仓股数。
    """

    cash: float
    #: time-0 股数。持仓市值 = ``eff × 后复权价(t)``；当前持股 = ``eff × f(t)``
    eff: Dict[str, float] = field(default_factory=dict)


class BacktestEngine:
    """按打分快照运行一次组合回测。"""

    def __init__(self, panels, params: StrategyParams, costs: Optional[CostModel] = None,
                 universe: Optional[pd.DataFrame] = None,
                 score_wide: Optional[pd.DataFrame] = None,
                 bench_close: Optional[pd.Series] = None):
        self.panels = panels
        self.params = params
        self.costs = costs or CostModel(
            **{k: v for k, v in (params.costs or {}).items()
               if k in CostModel().__dict__}
        )
        self.universe = universe
        self.score_wide = score_wide if score_wide is not None else pd.DataFrame()
        self.bench_close = bench_close
        self._st_codes: set = resolve_st_codes(universe) if universe is not None else set()
        #: 本调仓周期内累计成交的名义金额（_rebalance 读取后清零）
        self._period_amount = 0.0

    # ---------- 数据准备 ----------

    def _prepare_arrays(self) -> None:
        """面板 → numpy 数组 + 位置映射（逐日循环才能跑得动）。"""
        p = self.panels
        adj_open = p.open
        factor = p.factor
        # 不复权开盘价 = 复权开盘价 / 当日因子（复权价 = 不复权价 × 因子）
        with np.errstate(divide="ignore", invalid="ignore"):
            raw_open = adj_open / factor.replace(0, np.nan)

        self._date_pos = {d: i for i, d in enumerate(p.dates)}
        self._code_pos = {c: j for j, c in enumerate(p.codes)}
        self._adj_open = adj_open.to_numpy(dtype="float64", copy=False)
        self._raw_open = raw_open.to_numpy(dtype="float64", copy=False)
        self._adj_close = p.close.to_numpy(dtype="float64", copy=False)
        self._factor = factor.to_numpy(dtype="float64", copy=False)
        self._status = p.tradestatus.to_numpy(dtype="float64", copy=False)
        self._volume = p.volume.to_numpy(dtype="float64", copy=False)
        self._raw_close = p.raw_close.to_numpy(dtype="float64", copy=False)

    def _bench_nav(self, dates: pd.DatetimeIndex, initial_cash: float) -> pd.Series:
        """基准净值（区间起点归一到 initial_cash）。"""
        if self.bench_close is None or len(self.bench_close) == 0:
            return pd.Series(np.nan, index=dates)
        close = self.bench_close.reindex(dates).ffill()
        if close.isna().all():
            return pd.Series(np.nan, index=dates)
        base = close.dropna().iloc[0]
        return (initial_cash * close / base).astype("float64")

    # ---------- 主流程 ----------

    def run(self) -> BacktestResult:
        self._prepare_arrays()
        p = self.panels
        params = self.params
        warnings: List[str] = []

        dates = p.dates
        if params.start:
            dates = dates[dates >= pd.Timestamp(params.start)]
        if params.end:
            dates = dates[dates <= pd.Timestamp(params.end)]
        if len(dates) < 3:
            raise RuntimeError(f"回测区间太短（{len(dates)} 个交易日），至少需要 3 天")

        warnings.extend(self.params.warnings())

        if self.score_wide.empty:
            raise RuntimeError("没有打分快照，无法运行 score_portfolio 策略")
        score = self.score_wide.reindex(dates)
        covered = score.notna().any(axis=1)
        if not covered.all():
            if not covered.any():
                raise RuntimeError("回测区间内没有任何打分快照")
            first_ok = dates[covered][0]
            warnings.append(f"{first_ok:%Y-%m-%d} 之前没有打分快照，已从该日起回测")
            dates = dates[dates >= first_ok]
            score = score.loc[dates]

        exposure = (
            regime_exposure(self.bench_close, params.regime_ma_fast, params.regime_ma_slow)
            if params.regime else None
        )
        targets = build_target_weights(
            score, dates, params, st_codes=self._st_codes, exposure=exposure
        )

        # 入选但整个区间都没有行情的代码：提前点名，别让用户对着一份
        # 「top_n=20 却只持仓 8 只」的报告猜原因。
        unavailable = sorted({c for w in targets.values() for c in w
                              if c not in self._code_pos})
        if unavailable:
            head = ", ".join(unavailable[:5])
            more = f" 等 {len(unavailable)} 只" if len(unavailable) > 5 else ""
            warnings.append(
                f"{len(unavailable)} 只入选股票在区间内没有任何行情{more}（{head}）"
                f"——多为快照日尚未上市的次新股；对应权重按规则留现金"
            )

        # 信号日 → 执行日（次一交易日），且执行日必须落在回测区间内
        exec_targets: Dict[pd.Timestamp, Dict[str, float]] = {}
        for signal_day, weights in targets.items():
            pos = self._date_pos.get(signal_day)
            if pos is None or pos + 1 >= len(p.dates):
                continue
            exec_day = p.dates[pos + 1]
            if dates[0] <= exec_day <= dates[-1]:
                exec_targets[exec_day] = weights
        if not exec_targets:
            raise RuntimeError("区间内没有可执行的调仓（信号日都落在区间末尾或早于首个快照）")

        return self._simulate(dates, exec_targets, warnings)

    # ---------- 逐日模拟 ----------

    def _simulate(self, dates, exec_targets, warnings) -> BacktestResult:
        params = self.params
        initial_cash = float(params.initial_cash)
        account = _Account(cash=initial_cash)

        nav = np.full(len(dates), np.nan, dtype="float64")
        trades: List[dict] = []
        holdings: List[dict] = []
        blocked = {"buy": 0, "sell": 0, "suspended": 0, "cash": 0,
                   "no_data": 0, "lot": 0}
        turnover_amounts: List[float] = []
        position_days = 0

        date_pos, code_pos = self._date_pos, self._code_pos
        adj_close = self._adj_close

        for i, day in enumerate(dates):
            gi = date_pos[day]                     # 在整表面板中的行号

            # ---- 1) 当日为执行日 → 先按开盘价成交，再按收盘计净值 ----
            if day in exec_targets:
                turnover_amounts.append(
                    self._rebalance(
                        day_index=gi,
                        target=exec_targets[day],
                        account=account,
                        trades=trades,
                        holdings=holdings,
                        blocked=blocked,
                    )
                )

            # ---- 2) 按收盘价计净值 ----
            # 真实现金 + Σ(time-0 股数 × 后复权价) = 真实元（含分红再投）
            close_row = adj_close[gi]
            total = account.cash
            for code, count in account.eff.items():
                j = code_pos.get(code)
                if j is None or count <= 0:
                    continue
                price = close_row[j]
                if np.isfinite(price):
                    total += count * price
            nav[i] = total
            # 只数**可交易**的持仓（≥ 一手）。不足一手的残余留在账上计净值，
            # 但不该被算成「持有一只股票」—— 否则 avg_positions 会被
            # 几十个除息残值顶到超过 top_n。
            factor_row = self._factor[gi]
            position_days += sum(
                1 for c, n in account.eff.items()
                if n > 1e-9 and c in code_pos
                and np.isfinite(factor_row[code_pos[c]])
                and self.costs.round_lot(n * factor_row[code_pos[c]]) > 0
            )

        nav_series = pd.Series(nav, index=pd.DatetimeIndex(dates))
        returns = nav_series.pct_change(fill_method=None)
        bench_level = self._bench_nav(pd.DatetimeIndex(dates), initial_cash)
        bench_returns = bench_level.pct_change(fill_method=None)

        equity = pd.DataFrame(
            {"nav": nav_series, "bench": bench_level,
             "drawdown": mt.drawdown_series(nav_series)}
        )

        period = mt.split_period(pd.DatetimeIndex(dates), params.is_ratio)
        is_ret = returns.loc[period["is_start"]:period["is_end"]]
        oos_ret = returns.loc[period["oos_start"]:period["oos_end"]]

        # 单期换手 = 一次调仓变动过的金额 / 当时净值；年化 = 均值 × 年调仓次数
        per_turnover = float(np.mean(turnover_amounts)) if turnover_amounts else 0.0
        annual_turnover = per_turnover * (250.0 / max(params.rebal_days, 1))

        extras = {
            "turnover": annual_turnover,
            "trades": len(trades),
            "blocked_trades": blocked["buy"] + blocked["sell"],
            "avg_positions": position_days / max(len(dates), 1),
        }
        metrics = {
            "full": mt.compute_metrics(returns, bench_returns, "full", extras),
            "is": mt.compute_metrics(is_ret, bench_returns, "is"),
            "oos": mt.compute_metrics(oos_ret, bench_returns, "oos"),
        }
        problem = mt.check_self_consistency(metrics["is"], metrics["oos"], metrics["full"])
        if problem:
            warnings.append(problem)
        if extras["blocked_trades"] > 0.2 * max(len(trades), 1):
            warnings.append(
                f"被涨跌停/停牌阻挡的交易占比偏高（buy={blocked['buy']}, "
                f"sell={blocked['sell']}），实际成交可能明显偏离目标权重"
            )
        if blocked["cash"]:
            warnings.append(
                f"有 {blocked['cash']} 次想加仓但现金不足（多是卖出被跌停/停牌挡下）"
            )
        if blocked["no_data"]:
            warnings.append(
                f"有 {blocked['no_data']} 次入选股票当日没有行情（未上市/已退市/未入库），"
                f"对应权重按规则留现金 —— 实际持仓会少于 top_n"
            )
        if blocked["lot"]:
            warnings.append(
                f"有 {blocked['lot']} 次目标金额买不起一手（不复权价 × 100 > 该票目标金额），"
                f"这部分权重也留在现金里；加大 initial_cash 或调低 top_n 可缓解"
            )
        expected = params.top_n
        if extras["avg_positions"] < 0.5 * expected:
            warnings.append(
                f"平均持仓 {extras['avg_positions']:.1f} 只，明显低于 top_n={expected}"
                f"（见上方 no_data / lot 计数）；名义上的「前 {expected} 名」并没有真正买到"
            )

        return BacktestResult(
            nav=nav_series,
            bench_nav=bench_level,
            returns=returns,
            bench_returns=bench_returns,
            equity=equity,
            trades=trades,
            holdings=holdings,
            blocked=blocked,
            rebalance_count=len(turnover_amounts),
            turnover=annual_turnover,
            avg_positions=extras["avg_positions"],
            metrics=metrics,
            period=period,
            warnings=warnings,
        )

    # ---------- 单次调仓 ----------

    def _rebalance(self, day_index: int, target: Dict[str, float], account: _Account,
                   trades: List[dict], holdings: List[dict], blocked: Dict[str, int]) -> float:
        """在 day_index 这天按开盘价把组合调到 target，返回本期换手率。

        顺序：算期初净值 → 先卖（清仓不在目标里的 + 把在目标里的减到目标股数）
        → 算卖后净值 → 再买（补到目标股数，现金不够就停）。

        卖出阶段也要「减到目标」而不是「只清不在目标里的」——否则
        ``weight_method="score"`` 下调某只票的权重时，超配的部分永远不砍，
        「目标权重」就成了空话。
        """
        code_pos = self._code_pos
        adj_open_day = self._adj_open[day_index]
        raw_open_day = self._raw_open[day_index]
        factor_day = self._factor[day_index]
        status_day = self._status[day_index]
        volume_day = self._volume[day_index]
        raw_close_row = self._raw_close[day_index - 1] if day_index > 0 else None
        day = self.panels.dates[day_index]

        def value_of() -> float:
            """现金 + 全部持仓市值（按当日开盘价，**真实元**）。

            持仓按 ``eff × 后复权开盘价`` 计 —— 这不是「复权市值」，
            而是这批钱按不复权价买入、持有期分红再投、在当日的真实总值。
            """
            total = account.cash
            for code, count in account.eff.items():
                j = code_pos.get(code)
                if j is None or count <= 0:
                    continue
                price = adj_open_day[j]
                if np.isfinite(price):
                    total += count * price
            return total

        def tradable(code: str) -> bool:
            j = code_pos.get(code)
            if j is None:
                return False
            price = adj_open_day[j]
            if not np.isfinite(price) or price <= 0:
                return False
            return is_tradable(status_day[j], volume_day[j])

        def limit_state(code: str) -> str:
            j = code_pos.get(code)
            if j is None or raw_close_row is None or not np.isfinite(raw_close_row[j]):
                return ""
            pct = limit_pct_for(code, day, is_st=code in self._st_codes)
            return limit_reason(raw_open_day[j], raw_close_row[j], pct)

        # ---------- 期初净值 ----------
        nav_start = value_of()

        def current_shares(code: str, j: int) -> float:
            """该股当前的**可交易**持股数（整手）。

            ``eff × 当日因子`` 是分红再投后的等效股数，除息后通常不是整数，
            更不能是一手 100 股的整数倍。A 股只能整手买卖，所以取数时向下
            取整到一手 —— 不足一手的残值留在 eff 里、继续计入净值，
            但不参与交易（与真实券商行为一致，也保证成交记录里的股数
            是整数，能逐笔复算出净值）。
            """
            held_eff = account.eff.get(code, 0.0)
            if held_eff <= 1e-9 or j is None:
                return 0.0
            f = float(factor_day[j])
            if not np.isfinite(f) or f <= 0:
                f = 1.0
            return self.costs.round_lot(held_eff * f)

        # ---------- 先卖：清仓 + 减到目标 ----------
        for code in list(account.eff):
            held_eff = account.eff.get(code, 0.0)
            if held_eff <= 1e-9:
                account.eff.pop(code, None)
                continue
            j = code_pos.get(code)
            weight = float((target or {}).get(code, 0.0))
            raw_price = float(raw_open_day[j]) if j is not None else float("nan")
            count = current_shares(code, j)
            want = 0.0
            if weight > 0 and np.isfinite(raw_price) and raw_price > 0:
                # 目标股数按**不复权价**算：权重是真实货币口径，不能拿复权价去除，
                # 否则 back_factor 越大买得越少（实测因此有一半资金投不出去）。
                want = self.costs.round_lot(nav_start * weight / raw_price)
            excess = count - want
            if excess <= 0:
                continue
            if not tradable(code):
                blocked["sell"] += 1
                if limit_state(code) == "":
                    blocked["suspended"] += 1
                continue
            if limit_state(code) == "down":
                blocked["sell"] += 1          # 跌停卖不出 → 顺延
                continue
            self._execute(code, "sell", excess, day_index, day, trades, account)

        # ---------- 当前净值（按当日开盘价，卖完之后） ----------
        nav_now = value_of()

        # ---------- 再买 ----------
        for code, weight in (target or {}).items():
            weight = float(weight)
            if weight <= 0:
                continue
            j = code_pos.get(code)
            if j is None or not np.isfinite(adj_open_day[j]) or adj_open_day[j] <= 0:
                # 入选但当日没有行情（未上市/已退市/未入库）。权重按规则留现金，
                # 但要记账 —— 静默跳过会让「目标 top_n=20」和「实际持仓 8 只」
                # 之间的落差无从解释。
                blocked["no_data"] += 1
                continue
            raw_price = float(raw_open_day[j])
            # 当前持股 = time-0 股数 × 当日因子，再向下取整到一手
            held = current_shares(code, j)
            desired = self.costs.round_lot(nav_now * weight / raw_price)
            if desired <= 0:
                # 连一手都买不起（不复权价 × 100 > 目标金额）—— 高价股的真实约束。
                blocked["lot"] += 1
                continue
            gap = desired - held
            if gap <= 0:
                continue                      # 已达到目标，无需动作
            if not tradable(code):
                blocked["buy"] += 1
                if limit_state(code) == "":
                    blocked["suspended"] += 1
                continue
            if limit_state(code) == "up":
                blocked["buy"] += 1              # 涨停买不进 → 资金留存
                continue
            affordable = self.costs.round_lot(
                self.costs.max_affordable_shares(
                    cash=account.cash, raw_price=raw_price, side="buy",
                )
            )
            if affordable <= 0:
                # 想加仓但现金不足（通常是该卖的票被跌停/停牌挡下了）。
                # 必须记一笔：静默跳过会让「目标权重」和「实际持仓」悄悄背离，
                # 而报告里看不出来。也不把这份权重临时分给别的股票。
                blocked["cash"] += 1
                continue
            self._execute(code, "buy", min(gap, affordable), day_index, day, trades, account)

        # ---------- 记录持仓（供人工复核） ----------
        final_value = account.cash + sum(
            account.eff[c] * adj_open_day[code_pos[c]]
            for c in account.eff
            if c in code_pos and np.isfinite(adj_open_day[code_pos[c]])
        )
        for code, held_eff in sorted(account.eff.items()):
            if held_eff <= 1e-9:
                continue
            j = code_pos.get(code)
            if j is None or not np.isfinite(adj_open_day[j]) or not np.isfinite(raw_open_day[j]):
                continue
            f = float(factor_day[j]) if np.isfinite(factor_day[j]) and factor_day[j] > 0 else 1.0
            # 报告里的股数是**分红再投后的等效股数**（= time-0 股数 × 当日因子），
            # 向下取整到一手；金额 = 股数 × 不复权价，就是这笔持仓当前的真实市值。
            count = current_shares(code, j)
            holdings.append(
                {
                    "date": day.strftime("%Y-%m-%d"),
                    "code": code,
                    "shares": int(count),
                    "price": round(float(raw_open_day[j]), 4),
                    "amount": round(float(count * raw_open_day[j]), 2),
                    # 权重用真实市值/真实净值 —— 这才是用户关心的「这只票占我多少钱」。
                    # 用复权市值会得到被 back_factor 扭曲的比例。
                    "weight": (round(float(count * raw_open_day[j] / final_value), 6)
                               if final_value > 0 else None),
                }
            )

        # 本期换手 = 这一轮买卖变动的金额 / 当日净值
        moved = self._period_amount
        self._period_amount = 0.0
        return min(moved / nav_now, 10.0) if nav_now > 0 else 0.0

    # ---------- 单笔成交 ----------

    def _execute(self, code: str, side: str, count: float, day_index: int,
                 day, trades: List[dict], account: _Account) -> None:
        """成交一笔：真实现金与 time-0 股数同步变动。

        ``eff += count / factor`` 的含义：这 ``count`` 股按不复权价花掉
        ``count × p`` 元，折成「time-0 股数」是 ``count × p / (p × factor)``。
        于是「time-0 股数 × 后复权价(t)」= ``count × p`` 元在 t 日的
        分红再投总值，与现金扣减严格相等，净值不跳变。

        ``count`` 本身是**等效股数**（分红再投后的口径）：买 100 股就是按
        当日不复权价付 100 股的钱；除息后 f 抬升，等效股数自动跟着涨，
        与 ``eff × f(t)`` 的取数口径一致 —— 这样清仓时买卖两侧严格对冲。
        """
        j = self._code_pos[code]
        raw_price = float(self._raw_open[day_index, j])
        f = float(self._factor[day_index, j])
        f = f if f > 0 else 1.0

        fee = self.costs.trade_cost(shares=count, price=raw_price, side=side)
        amount = count * raw_price
        self._period_amount += amount

        if side == "buy":
            account.eff[code] = account.eff.get(code, 0.0) + count / f
            account.cash -= amount + fee
        else:
            account.eff[code] = account.eff.get(code, 0.0) - count / f
            account.cash += amount - fee
            if account.eff[code] <= 1e-9:
                account.eff.pop(code, None)

        trades.append(
            {
                "date": day.strftime("%Y-%m-%d"),
                "code": code,
                "side": side,
                "price": round(raw_price, 4),
                "shares": int(count),
                "amount": round(amount, 2),
                "fee": round(fee, 2),
                "reason": "rebalance",
            }
        )


def run_backtest(
    panels,
    score_wide: pd.DataFrame,
    params: StrategyParams,
    costs: Optional[CostModel] = None,
    universe: Optional[pd.DataFrame] = None,
    bench_close: Optional[pd.Series] = None,
) -> BacktestResult:
    """便捷入口。"""
    engine = BacktestEngine(
        panels, params, costs=costs, universe=universe,
        score_wide=score_wide, bench_close=bench_close,
    )
    return engine.run()
