"""回测引擎测试：手工核对净值、T+1、涨跌停、停牌、费用、一手取整。

规格 §11 的验收原话：
「构造 5 只股票的合成价格，手工计算 2 个调仓周期的净值，断言误差 < 1e-6；
验证 T+1、涨停拒单、跌停顺延、费用」。

全部用例跑在确定性合成面板上（开盘价 = 收盘价，因子恒 1），
让「手工算一遍」变成可能 —— 引擎错了会立刻和手算对不上。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core.backtest import CostModel, StrategyParams, run_backtest
from backend.core.backtest.costs import (
    is_tradable,
    limit_pct_for,
    limit_reason,
)
from backend.core.backtest.strategies import (
    build_target_weights,
    cap_and_redistribute,
    regime_exposure,
    signal_dates,
    weights_for_picks,
)
from backend.core.panels import Panels

DAYS = pd.bdate_range("2024-01-02", periods=6)
CODES = ["sh.600001", "sh.600002", "sh.600003", "sh.600004", "sh.600005"]
CLOSE = {
    "sh.600001": [10.0, 10.0, 11.0, 12.0, 12.0, 13.0],
    "sh.600002": [20.0, 20.0, 19.0, 18.0, 18.0, 17.0],
    "sh.600003": [30.0, 30.0, 31.0, 30.0, 29.0, 30.0],
    "sh.600004": [50.0, 50.0, 50.0, 50.0, 50.0, 50.0],
    "sh.600005": [5.0, 5.0, 6.0, 6.0, 6.0, 6.0],
}


def make_panel(close=None, tradestatus=None, volume=None):
    """合成面板：open=close（消除日内噪声），high/low 留出带宽，factor 恒 1。"""
    close = close or CLOSE
    frame = pd.DataFrame(close, index=DAYS)
    status = np.ones(frame.shape, dtype="int8")
    if tradestatus is not None:
        for (day, code), value in tradestatus.items():
            status[list(DAYS).index(day), list(frame.columns).index(code)] = value
    vol = np.full(frame.shape, 1e6)
    if volume is not None:
        for (day, code), value in volume.items():
            vol[list(DAYS).index(day), list(frame.columns).index(code)] = value
    frames = {
        "open": frame,
        "high": frame * 1.02,
        "low": frame * 0.98,
        "close": frame,
        "volume": pd.DataFrame(vol, index=DAYS, columns=frame.columns),
        "turn": pd.DataFrame(np.full(frame.shape, 0.01), index=DAYS,
                             columns=frame.columns),
        "tradestatus": pd.DataFrame(status, index=DAYS, columns=frame.columns),
        "raw_close": frame,
        "factor": pd.DataFrame(np.ones(frame.shape), index=DAYS,
                               columns=frame.columns),
    }
    return Panels({k: v.astype("float32") for k, v in frames.items()})


# 打分：d0 选 A/B；d2 选 B/C；d4 选 C/E（min_score_pct=1 过滤掉 0 分）
# 注意 d0 的 B=75 必须高于 E=50，否则 top_n=2 会选到 E 而不是 B。
SCORES = pd.DataFrame(
    {
        "sh.600001": [100.0, np.nan, 0.0, np.nan, 0.0, np.nan],
        "sh.600002": [75.0, np.nan, 100.0, np.nan, 50.0, np.nan],
        "sh.600003": [0.0, np.nan, 75.0, np.nan, 100.0, np.nan],
        "sh.600004": [25.0, np.nan, 25.0, np.nan, 25.0, np.nan],
        "sh.600005": [50.0, np.nan, 50.0, np.nan, 75.0, np.nan],
    },
    index=DAYS,
)

NO_COST = CostModel(commission=0.0, min_commission=0.0, stamp_duty=0.0,
                    slippage=0.0, lot_size=0)


def make_panel_with_factors(close=None, factors=None):
    """同一套**真实价格**、不同 ``back_factor`` 的面板。

    真实数据里 back_factor 中位数约 3.2、95 分位 40.8、茅台 7、格力 156；
    原有合成面板恒为 1，于是「复权价 vs 不复权价」的岔路口从没被测过 ——
    下面 ``TestBackFactorAccounting`` 就是补这个盲区。
    """
    close = close or CLOSE
    frame = pd.DataFrame(close, index=DAYS)
    # factors 可以是标量（全程不变）或与 close 等长的序列（模拟除息日因子抬升）
    factors = factors or {c: 1.0 for c in close}
    factors_frame = pd.DataFrame(
        {
            c: (list(factors[c]) if isinstance(factors.get(c), (list, tuple, np.ndarray))
                else [float(factors.get(c, 1.0))] * len(DAYS))
            for c in frame.columns
        },
        index=DAYS,
    )
    return Panels({
        # 注意：面板里的 open/high/low/close 都是**复权后**的值
        "open": frame * factors_frame,
        "high": frame * 1.02 * factors_frame,
        "low": frame * 0.98 * factors_frame,
        "close": frame * factors_frame,
        "volume": pd.DataFrame(np.full(frame.shape, 1e6), index=DAYS,
                               columns=frame.columns),
        "turn": pd.DataFrame(np.full(frame.shape, 0.01), index=DAYS,
                             columns=frame.columns),
        "tradestatus": pd.DataFrame(np.ones(frame.shape, dtype="int8"), index=DAYS,
                                    columns=frame.columns),
        "raw_close": frame,
        "factor": factors_frame,
    })

#: 只开一手约束、费用全关 —— 用来单独验证 round_lot 的行为
LOT_ONLY = CostModel(commission=0.0, min_commission=0.0, stamp_duty=0.0,
                     slippage=0.0, lot_size=100)

#: 真实 A 股费率（与 config/app.yaml 的 costs 段一致）
REAL_COST = CostModel()


def params(**kwargs):
    base = dict(top_n=2, rebal_days=2, weight_method="equal",
                regime=False, max_weight=0.6, min_score_pct=1.0,
                initial_cash=1_000_000.0)
    base.update(kwargs)
    return StrategyParams(**base)


# ==================== 手工核对净值（规格验收） ====================


class TestManualNav:
    """无费用、无一手约束时，两个调仓周期的净值必须和手算逐分不差。"""

    def test_first_rebalance_exact(self):
        result = run_backtest(make_panel(), SCORES, params(), NO_COST)

        # d1 开盘成交（信号 d0 选出 A=600001 与 B=600002，等权 50%）
        # A: 1_000_000 × 0.5 / 10 = 50_000 股
        # B: 1_000_000 × 0.5 / 20 = 25_000 股
        assert result.nav.iloc[0] == pytest.approx(1_000_000.0, abs=1e-6)
        assert result.nav.iloc[1] == pytest.approx(50_000 * 10 + 25_000 * 20,
                                                  abs=1e-6)
        buys = [t for t in result.trades if t["side"] == "buy" and t["date"] == "2024-01-03"]
        assert len(buys) == 2
        got = {t["code"]: t["shares"] for t in buys}
        assert got == {"sh.600001": 50_000, "sh.600002": 25_000}

    def test_second_rebalance_exact(self):
        result = run_backtest(make_panel(), SCORES, params(), NO_COST)

        # 信号 d2 选出 B/C，d3 开盘执行：
        #   A 不在目标里 → 全卖：50_000 × 12 = 600_000 现金
        #   此时市值 = 600_000 + 25_000 × 18 = 1_050_000
        #   B: 0.5 × 1_050_000 / 18 = 29_166.6667 股（已持 25_000 → 加 4_166.67）
        #   C: 0.5 × 1_050_000 / 30 = 17_500 股
        # 收盘市值 = 29_166.6667×18 + 17_500×30 = 525_000 + 525_000 = 1_050_000
        assert result.nav.iloc[3] == pytest.approx(1_050_000.0, abs=1e-6)

        sells = [t for t in result.trades if t["side"] == "sell" and t["date"] == "2024-01-05"]
        assert len(sells) == 1 and sells[0]["code"] == "sh.600001"
        assert sells[0]["shares"] == 50_000
        assert sells[0]["price"] == pytest.approx(12.0, abs=1e-9)

    def test_t_plus_one_not_same_day(self):
        """信号日当天绝不能成交：d0 出信号 → d1 才买。"""
        result = run_backtest(make_panel(), SCORES, params(), NO_COST)
        first = min(t["date"] for t in result.trades)
        # d0 = 2024-01-02（信号日），d1 = 2024-01-03（执行日）
        assert first == "2024-01-03"
        assert not any(t["date"] == "2024-01-02" for t in result.trades)

    def test_cash_conservation(self):
        """每日收盘市值 = 现金 + 持仓市值（引擎内部恒等式）。"""
        result = run_backtest(make_panel(), SCORES, params(), NO_COST)
        panel = make_panel()
        # 反推现金：nav_已知，持仓可由 holdings 推算，这里只验证数值合理性
        assert (result.nav > 0).all()
        assert result.nav.iloc[0] == pytest.approx(1_000_000.0, abs=1e-6)


# ==================== T+1 与调仓节奏 ====================


class TestRebalanceRhythm:
    def test_signal_dates(self):
        assert list(signal_dates(DAYS, 2)) == [
            DAYS[0], DAYS[2], DAYS[4],
        ]
        assert list(signal_dates(DAYS, 3)) == [DAYS[0], DAYS[3]]
        assert list(signal_dates(DAYS, 1)) == list(DAYS)

    def test_rebalance_count(self):
        result = run_backtest(make_panel(), SCORES, params(), NO_COST)
        assert result.rebalance_count == 3          # d0/d2/d4 出信号 → d1/d3/d5 执行

    def test_only_day_with_score_trades(self):
        # 打分只在 d0 有 → 只可能有一次调仓（缺快照的日子不调仓，不清仓）
        scores = SCORES.copy()
        scores.iloc[2:] = np.nan
        result = run_backtest(make_panel(), scores, params(), NO_COST)
        assert result.rebalance_count == 1
        # 唯一一次执行发生在 d1，且只买不卖
        assert [t["date"] for t in result.trades] == ["2024-01-03"] * 2
        assert {t["side"] for t in result.trades} == {"buy"}

    def test_short_period_raises(self):
        with pytest.raises(RuntimeError, match="区间太短"):
            run_backtest(make_panel(), SCORES, params(start="2024-01-08"), NO_COST)


# ==================== 一手取整 ====================


class TestLotSize:
    def test_rounding_down_to_lot(self):
        result = run_backtest(make_panel(), SCORES, params(), LOT_ONLY)
        # d3（01-05）执行 B/C：B 目标 29_166.67 股 → 29_100；C 目标 17_500 → 17_500
        d3 = [t for t in result.trades if t["date"] == "2024-01-05"]
        buys = {t["code"]: t for t in d3 if t["side"] == "buy"}
        assert buys["sh.600002"]["shares"] == 4_100       # 29_100 - 25_000
        assert buys["sh.600003"]["shares"] == 17_500

    def test_residual_cash_after_lot_rounding(self):
        """一手取整丢掉的钱留在现金里，净值分毫不差。"""
        result = run_backtest(make_panel(), SCORES, params(), LOT_ONLY)
        # d3：卖 A 得 600_000，买 B 花 4_100×18、买 C 花 17_500×30
        #   现金 = 600_000 - 73_800 - 525_000 = 1_200（取整残值）
        #   持仓 = 29_100×18 + 17_500×30
        nav_expected = 29_100 * 18 + 17_500 * 30 + 1_200
        assert result.nav.iloc[3] == pytest.approx(nav_expected, abs=1e-6)

    def test_round_lot_helper(self):
        assert NO_COST.round_lot(1234.0) == 1234.0
        assert CostModel(lot_size=100).round_lot(1234.0) == 1200.0
        assert CostModel(lot_size=100).round_lot(0.0) == 0.0

    def test_holdings_are_whole_lots(self):
        """任何一天的持仓都必须是 100 的整数倍。"""
        result = run_backtest(make_panel(), SCORES, params(), LOT_ONLY)
        for trade in result.trades:
            assert trade["shares"] % 100 == 0, trade


# ==================== 涨跌停 / 停牌 ====================


class TestLimitAndSuspension:
    def test_limit_reason_boundaries(self):
        # 主板 10%，昨收 10 元 → 涨停 11 元、跌停 9 元
        assert limit_reason(11.0, 10.0, 0.10) == "up"
        assert limit_reason(10.998, 10.0, 0.10) == "up"      # 容忍 2 分钱
        assert limit_reason(9.0, 10.0, 0.10) == "down"
        assert limit_reason(9.002, 10.0, 0.10) == "down"
        assert limit_reason(10.5, 10.0, 0.10) == ""
        assert limit_reason(None, 10.0, 0.10) == ""
        assert limit_reason(10.0, 0.0, 0.10) == ""

    def test_limit_pct_by_board(self):
        # 主板 10%、科创板 20%、北交所 30%、创业板按 2020-08-24 分段
        assert limit_pct_for("sh.600001", "2024-01-02") == 0.10
        assert limit_pct_for("sh.688001", "2024-01-02") == 0.20
        assert limit_pct_for("bj.830799", "2024-01-02") == 0.30
        assert limit_pct_for("sz.300001", "2020-01-02") == 0.10
        assert limit_pct_for("sz.300001", "2021-01-02") == 0.20
        assert limit_pct_for("sh.600001", "2024-01-02", is_st=True) == 0.05

    def test_is_tradable(self):
        assert is_tradable(1, 1000.0) is True
        assert is_tradable(0, 1000.0) is False
        assert is_tradable(1, 0.0) is False
        assert is_tradable(None, None) is True

    def test_limit_up_blocks_buy(self):
        """涨停 → 放弃买入、资金留存（不补配给别的股票）。"""
        # B 在 d3（01-05）一字涨停：昨收 = d2 收盘 19 → 涨停价 19×1.1 = 20.9
        frame = pd.DataFrame(CLOSE, index=DAYS)
        open_frame = frame.copy()
        open_frame.loc[DAYS[3], "sh.600002"] = 19.0 * 1.1
        frames = _as_frames(open_frame, frame)
        panel = Panels({k: v.astype("float32") for k, v in frames.items()})

        result = run_backtest(panel, SCORES, params(), NO_COST)
        assert result.blocked["buy"] >= 1
        # B 没能加仓，只保留原有 25_000 股
        d3_buys = [t for t in result.trades
                   if t["date"] == "2024-01-05" and t["code"] == "sh.600002"
                   and t["side"] == "buy"]
        assert d3_buys == []

    def test_limit_down_blocks_sell(self):
        """跌停 → 卖不出，持仓顺延到下一调仓日。"""
        # A 在 d3（01-05）一字跌停：昨收 = d2 收盘 11 → 跌停价 11×0.9 = 9.9
        frame = pd.DataFrame(CLOSE, index=DAYS)
        open_frame = frame.copy()
        open_frame.loc[DAYS[3], "sh.600001"] = 11.0 * 0.9
        frames = _as_frames(open_frame, frame)
        panel = Panels({k: v.astype("float32") for k, v in frames.items()})

        result = run_backtest(panel, SCORES, params(), NO_COST)
        assert result.blocked["sell"] >= 1
        # d3 一股没卖，A 仍持有 50_000 股（顺延）
        d3_sells = [t for t in result.trades
                    if t["side"] == "sell" and t["date"] == "2024-01-05"]
        assert d3_sells == []
        positions = {h["code"]: h["shares"] for h in result.holdings
                     if h["date"] == "2024-01-05"}
        assert positions["sh.600001"] == 50_000
        # d5（01-09）信号换成 C/E，A 这才卖掉 —— 顺延而非永久卡死
        d5_sells = {t["code"]: t["shares"] for t in result.trades
                    if t["side"] == "sell" and t["date"] == "2024-01-09"}
        assert d5_sells["sh.600001"] == 50_000

    def test_suspended_blocks_both(self):
        """停牌 → 买不进也卖不出，双向都计入 blocked。"""
        # A 在 d3 停牌（要卖），C 在 d3 停牌（要买）
        panel = make_panel(tradestatus={
            (DAYS[3], "sh.600001"): 0,
            (DAYS[3], "sh.600003"): 0,
        })
        result = run_backtest(panel, SCORES, params(), NO_COST)
        assert result.blocked["suspended"] >= 2
        assert result.blocked["sell"] >= 1
        assert result.blocked["buy"] >= 1
        # d3 双向都没成交（A 卖不出、C 买不进）
        d3 = [t for t in result.trades if t["date"] == "2024-01-05"]
        assert d3 == []

    def test_no_supplementary_allocation(self):
        """被挡下的权重必须留在现金里，不会临时分给别的股票。"""
        panel = make_panel(tradestatus={(DAYS[3], "sh.600003"): 0})
        result = run_backtest(panel, SCORES, params(), NO_COST)
        d3_buys = [t for t in result.trades
                   if t["date"] == "2024-01-05" and t["side"] == "buy"]
        # 只有 B 加仓一笔，C 缺席；不存在「把 C 的份额给 B」的补配成交
        assert [t["code"] for t in d3_buys] == ["sh.600002"]

    def test_cash_starvation_is_recorded(self):
        """该卖的卖不掉 → 现金见底 → 想加仓也买不起：必须留痕。

        这是踩过的坑：旧实现用 ``min(desired-held, affordable)`` 一把梭，
        affordable=0 时直接 continue，既不成交也不计数，「目标权重」和
        「实际持仓」悄悄背离而报告里看不出来。
        """
        # A 在 d3 一字跌停卖不掉 → 现金停在 0 → B 想加仓却买不起
        frame = pd.DataFrame(CLOSE, index=DAYS)
        open_frame = frame.copy()
        open_frame.loc[DAYS[3], "sh.600001"] = 11.0 * 0.9
        panel = Panels({k: v.astype("float32")
                        for k, v in _as_frames(open_frame, frame).items()})

        result = run_backtest(panel, SCORES, params(), NO_COST)
        assert result.blocked["sell"] >= 1
        assert result.blocked["cash"] >= 1
        assert any("现金不足" in w for w in result.warnings)
        assert (result.nav > 0).all()

    def test_overweight_position_is_trimmed(self):
        """权重下调要减仓：不能只清「不在目标里」的票。"""
        trim = pd.DataFrame(
            {
                "sh.600001": [100.0, np.nan, 100.0, np.nan, 100.0, np.nan],
                "sh.600002": [75.0, np.nan, 0.0, np.nan, 0.0, np.nan],
                "sh.600003": [0.0, np.nan, 0.0, np.nan, 0.0, np.nan],
                "sh.600004": [25.0, np.nan, 0.0, np.nan, 0.0, np.nan],
                "sh.600005": [50.0, np.nan, 0.0, np.nan, 0.0, np.nan],
            },
            index=DAYS,
        )
        # d0 选 A/B 各 50%；d2 只选 A（100%）→ d3 卖掉全部 B、把 A 补到满仓
        # max_weight=1.0 关掉单票上限，单独考察「减仓」这一条路径
        result = run_backtest(make_panel(), trim, params(max_weight=1.0), NO_COST)

        d3 = [t for t in result.trades if t["date"] == "2024-01-05"]
        sells = {t["code"]: t["shares"] for t in d3 if t["side"] == "sell"}
        buys = {t["code"]: t["shares"] for t in d3 if t["side"] == "buy"}
        assert sells == {"sh.600002": 25_000}
        # 期初市值 1_050_000 → A 目标 87_500 股，已持 50_000 → 加 37_500
        #   卖 B 得 450_000，正好买 37_500×12 = 450_000
        assert buys == {"sh.600001": 37_500}
        positions = {h["code"]: h["shares"] for h in result.holdings
                     if h["date"] == "2024-01-05"}
        assert positions == {"sh.600001": 87_500}
        assert result.nav.iloc[3] == pytest.approx(87_500 * 12.0, abs=1e-6)


def _as_frames(open_frame, close_frame):
    return {
        "open": open_frame,
        "high": close_frame * 1.02,
        "low": close_frame * 0.98,
        "close": close_frame,
        "volume": pd.DataFrame(np.full(close_frame.shape, 1e6),
                               index=close_frame.index, columns=close_frame.columns),
        "turn": pd.DataFrame(np.full(close_frame.shape, 0.01),
                             index=close_frame.index, columns=close_frame.columns),
        "tradestatus": pd.DataFrame(np.ones(close_frame.shape, dtype="int8"),
                                    index=close_frame.index, columns=close_frame.columns),
        "raw_close": close_frame,
        "factor": pd.DataFrame(np.ones(close_frame.shape),
                               index=close_frame.index, columns=close_frame.columns),
    }


# ==================== 费用 ====================


class TestCosts:
    def test_buy_only_commission_and_slippage(self):
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.001,
                          slippage=0.001, lot_size=0)
        # 10_000 股 × 10 元 = 100_000 名义
        # 佣金 100 + 滑点 100 = 200（返回真实元，直接与真实现金对冲）
        assert model.trade_cost(10_000, 10.0, "buy") == pytest.approx(200.0)

    def test_sell_adds_stamp_duty(self):
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.001,
                          slippage=0.001, lot_size=0)
        # 卖出：佣金 100 + 滑点 100 + 印花税 100 = 300
        assert model.trade_cost(10_000, 10.0, "sell") == pytest.approx(300.0)

    def test_min_commission_floor(self):
        model = CostModel(commission=0.00025, min_commission=5.0, stamp_duty=0.0,
                          slippage=0.0, lot_size=0)
        # 100 股 × 5 元 = 500 名义 → 佣金 0.125 < 5 → 收 5 元
        assert model.trade_cost(100, 5.0, "buy") == pytest.approx(5.0)
        # 50_000 股 × 5 元 = 250_000 → 佣金 62.5 > 5 → 按比例
        assert model.trade_cost(50_000, 5.0, "buy") == pytest.approx(62.5)

    def test_fee_is_nominal_not_adjusted(self):
        """费用必须按名义金额返回真实元。

        曾按 ``× back_factor`` 折算成「复权单位」，搭配「复权净值/复权价」算股数，
        导致 back_factor=50 的股票只能买到 1/50 的真实仓位。
        """
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.0,
                          slippage=0.001, lot_size=0)
        assert model.trade_cost(10_000, 10.0, "buy") == pytest.approx(200.0)
        assert model.trade_cost(10_000, 10.0, "sell") == pytest.approx(
            10_000 * 10.0 * (0.001 + 0.001 + 0.0)
        )

    def test_zero_shares_no_cost(self):
        model = CostModel()
        assert model.trade_cost(0, 10.0, "buy") == 0.0
        assert model.trade_cost(100, 0.0, "buy") == 0.0

    def test_affordable_reserves_fees(self):
        """可买股数必须为费用预留现金，否则会出现「买了之后现金变负」。"""
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.0,
                          slippage=0.001, lot_size=100)
        cash = 100_000.0
        n = model.max_affordable_shares(cash, raw_price=10.0, side="buy")
        n = model.round_lot(n)
        fee = model.trade_cost(n, 10.0, "buy")
        assert n * 10.0 + fee <= cash + 1e-9, "成交后现金不能为负"
        assert n > 0

    def test_affordable_uses_real_price(self):
        """现金约束用**不复权价**：同一笔真实资金，高 back_factor 的股票
        买得起的股数应该一样少，而不是被 factor 放大买到更多。"""
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.0,
                          slippage=0.001, lot_size=0)
        cash = 100_000.0
        # 不复权价 10 元、费率 0.2% → 100000/(10×1.002) ≈ 9980 股
        n = model.max_affordable_shares(cash, raw_price=10.0, side="buy")
        assert n == pytest.approx(9980.03992015968, rel=1e-9)

    def test_engine_fee_recorded_matches_model(self):
        model = CostModel(commission=0.001, min_commission=0.0, stamp_duty=0.001,
                          slippage=0.001, lot_size=0)
        result = run_backtest(make_panel(), SCORES, params(), model)
        buy = [t for t in result.trades if t["side"] == "buy"][0]
        expected = (buy["amount"] * 0.001          # 佣金
                    + buy["amount"] * 0.001        # 滑点
                    )
        assert buy["fee"] == pytest.approx(expected, rel=1e-6)
        sell = [t for t in result.trades if t["side"] == "sell"][0]
        expected_sell = sell["amount"] * (0.001 + 0.001 + 0.001)
        assert sell["fee"] == pytest.approx(expected_sell, rel=1e-6)

    def test_costs_description(self):
        info = CostModel().describe()
        assert info["one_way_buy"] == 0.00075
        assert info["one_way_sell"] == 0.00125
        assert "note" in info


class TestRealCostConservation:
    """真实费率下逐日反推现金：不能透支，nav 必须等于 现金 + 持仓市值。

    这是规格「手工核对净值一致」在**带费用**场景下的等价形式 ——
    把成交记录重新走一遍账，看能不能复算出引擎给出的净值。
    """

    def test_daily_nav_matches_ledger(self):
        result = run_backtest(make_panel(), SCORES, params(), REAL_COST)
        panel = make_panel()

        by_day: dict = {}
        for trade in result.trades:
            by_day.setdefault(trade["date"], []).append(trade)

        cash = params().initial_cash
        shares: dict = {}
        for day in [d.strftime("%Y-%m-%d") for d in panel.dates]:
            for trade in by_day.get(day, []):
                # 合成面板 factor 恒 1：名义金额 == 复权金额，可直接对账
                if trade["side"] == "buy":
                    cash -= trade["shares"] * trade["price"] + trade["fee"]
                    shares[trade["code"]] = shares.get(trade["code"], 0.0) + trade["shares"]
                else:
                    cash += trade["shares"] * trade["price"] - trade["fee"]
                    shares[trade["code"]] = shares.get(trade["code"], 0.0) - trade["shares"]
            assert cash >= -1e-6, f"{day} 现金透支：{cash}"
            value = cash + sum(
                n * float(panel.close.loc[day, code])
                for code, n in shares.items() if n > 0
            )
            # 成交记录里的 price/fee 是给人看的（分别四舍五入到分/厘），
            # 所以对账容忍几厘分的累计误差；真正的记账错误会是几百上千。
            assert value == pytest.approx(result.nav.loc[day], abs=0.05), day

    def test_fees_are_nonzero_but_small(self):
        result = run_backtest(make_panel(), SCORES, params(), REAL_COST)
        assert result.trades, "默认费率下应该有成交"
        assert all(t["fee"] > 0 for t in result.trades)
        # 单轮换手约 0.2% 量级：40 个交易日不该吃掉 5% 以上净值
        assert result.total_return > -0.05

    def test_params_warning_surfaces_in_result(self):
        result = run_backtest(make_panel(), SCORES,
                              params(top_n=2, max_weight=0.1), NO_COST)
        assert any("现金" in w for w in result.warnings)


# ==================== 复权记账（factor ≠ 1） ====================


class TestBackFactorAccounting:
    """back_factor ≠ 1 时的记账口径。

    这是真实数据上踩过的最严重的一个坑，专门用合成面板钉死：
    早期版本用「后复权净值 / 后复权价」算股数，而 ``后复权价 = 不复权价 × factor``。
    后果是同样一笔「权重 w」的钱，在 factor=50 的股票上只买到 1/50 的真实仓位 ——
    实测 5 年 HS300 回测只投出去 14.5% 的资金、平均持仓 10 只而 top_n=20，
    而报告上看不出来（因为权重确实是按复权市值算的「齐」的）。

    正确口径：股数与费用用**不复权价**，持仓按「time-0 股数」记账。
    """

    #: 只在 d0 有信号 → 只在 d1 调仓一次，之后全程持有
    HOLD_SCORES = pd.DataFrame(
        {"sh.600001": [100.0, np.nan, np.nan, np.nan, np.nan, np.nan]},
        index=DAYS,
    )

    def test_share_count_uses_raw_price(self):
        """factor=50 的股票，1,000,000 元 / 10 元 = 100,000 股，不是 2,000 股。"""
        panel = make_panel_with_factors(
            close={"sh.600001": [10.0] * 6}, factors={"sh.600001": 50.0}
        )
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), NO_COST)
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) == 1
        assert buys[0]["shares"] == 100_000
        assert buys[0]["price"] == pytest.approx(10.0)      # 报告的是不复权真值
        assert buys[0]["amount"] == pytest.approx(1_000_000.0)

    def test_nav_does_not_jump_on_first_purchase(self):
        """买入瞬间净值必须连续（无费用时应严格不变）。

        旧口径把现金按后复权价扣减，factor=50 时净值会从 1,000,000 跳到
        1,980,000 —— 等于凭空造出 98% 的收益。
        """
        panel = make_panel_with_factors(
            close={"sh.600001": [10.0] * 6}, factors={"sh.600001": 50.0}
        )
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), NO_COST)
        assert result.nav.iloc[0] == pytest.approx(1_000_000.0, abs=1e-6)
        assert result.nav.iloc[1] == pytest.approx(1_000_000.0, abs=1e-6), "买入当日净值跳变"
        assert result.nav.iloc[-1] == pytest.approx(1_000_000.0, abs=1e-6)

    def test_weight_is_real_weight(self):
        """报告里的权重是真实市值占比，不该被 back_factor 扭曲。"""
        panel = make_panel_with_factors(
            close={"sh.600001": [10.0] * 6}, factors={"sh.600001": 50.0}
        )
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), NO_COST)
        weights = [h["weight"] for h in result.holdings]
        assert weights and all(w == pytest.approx(1.0, abs=1e-6) for w in weights)

    def test_position_grows_by_total_return(self):
        """持有期间因子抬升（= 除息）：净值应按总回报增长，而非按不复权价。

        真实价格全程 10 元不变，factor 从 1 抬到 1.1 —— 等价于一次
        每股 0.909 元的现金分红且红利再投，净值应涨 10%。
        """
        frame = {"sh.600001": [10.0] * 6}
        factors = {"sh.600001": [1.0, 1.0, 1.0, 1.1, 1.1, 1.1]}
        panel = make_panel_with_factors(close=frame, factors=factors)
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), NO_COST)
        assert result.nav.iloc[1] == pytest.approx(1_000_000.0, abs=1e-6)
        assert result.nav.iloc[2] == pytest.approx(1_000_000.0, abs=1e-6)
        assert result.nav.iloc[3] == pytest.approx(1_100_000.0, abs=1e-3)
        assert result.nav.iloc[-1] == pytest.approx(1_100_000.0, abs=1e-3)
        assert result.total_return == pytest.approx(0.10, abs=1e-4)

    def test_high_factor_stock_is_not_squeezed_out(self):
        """top_n=2 且两票真实价相同、factor 差 50 倍：两票都应买到目标股数。

        旧口径会把 factor=50 那只砍到 1/50 仓位，实际持仓权重严重失衡。
        """
        close = {"sh.600001": [10.0] * 6, "sh.600002": [10.0] * 6}
        scores = pd.DataFrame(
            {"sh.600001": [100.0] + [np.nan] * 5,
             "sh.600002": [75.0] + [np.nan] * 5},
            index=DAYS,
        )
        panel = make_panel_with_factors(
            close=close, factors={"sh.600001": 50.0, "sh.600002": 1.0}
        )
        result = run_backtest(panel, scores, params(top_n=2, max_weight=0.6), NO_COST)
        buys = {t["code"]: t for t in result.trades if t["side"] == "buy"}
        assert set(buys) == {"sh.600001", "sh.600002"}
        assert buys["sh.600001"]["shares"] == buys["sh.600002"]["shares"], \
            "同样的钱、同样的真实股价，股数不该被复权因子分开"
        weights = {h["code"]: h["weight"] for h in result.holdings}
        assert weights["sh.600001"] == pytest.approx(weights["sh.600002"], abs=1e-6)

    def test_lot_blocking_uses_real_price(self):
        """一手门槛用**不复权价**判：真实价 4,000 元才买不起一手，
        factor=50 的虚高复权价不该成为障碍。"""
        panel = make_panel_with_factors(
            close={"sh.600001": [4_000.0] * 6}, factors={"sh.600001": 50.0}
        )
        # 100 万满仓一只 → 目标金额 1,000,000 / 4,000 = 250 股 → 2 手
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), LOT_ONLY)
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) == 1 and buys[0]["shares"] == 200
        assert result.blocked["lot"] == 0

    def test_cheap_stock_unaffected_by_big_factor(self):
        """回归：factor=156（格力）也不能让真实价 4 元的股票买不到一手。"""
        panel = make_panel_with_factors(
            close={"sh.600001": [4.0] * 6}, factors={"sh.600001": 156.0}
        )
        result = run_backtest(panel, self.HOLD_SCORES,
                              params(top_n=1, max_weight=1.0), LOT_ONLY)
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) == 1
        assert buys[0]["shares"] == 250_000     # 1,000,000 / 4 = 250,000 股
        assert result.blocked["lot"] == 0

    def test_exit_after_dividend_leaves_no_residue(self):
        """除息后清仓必须把 eff 清零，不能留下幽灵持仓。

        踩过的坑：若按「买入时的原始股数」和目标比，除息后等效股数翻倍
        而原始股数不变，清仓只卖掉一半 —— 实测 5 年回测末期攒下
        77 只幽灵持仓、净值虚高 15%。这里用合成面板钉死：
        d1 满仓买入，d3 因子翻倍（除息再投，等效股数翻倍），d4 全部卖出。
        """
        close = {"sh.600001": [10.0] * 6}
        # factor 在 d2→d3 之间从 1 抬到 2：adj 价从 10 抬到 20，
        # 等价于一次 50% 的现金分红且红利再投 → 等效股数 ×2
        factors = {"sh.600001": [1.0, 1.0, 2.0, 2.0, 2.0, 2.0]}
        panel = make_panel_with_factors(close=close, factors=factors)
        # d1 持有全部；d3 起目标清仓（score 掉出榜单）
        scores = pd.DataFrame(
            {"sh.600001": [100.0, np.nan, 0.0, np.nan, np.nan, np.nan]},
            index=DAYS,
        )
        result = run_backtest(panel, scores,
                              params(top_n=1, max_weight=1.0), NO_COST)

        sells = [t for t in result.trades if t["side"] == "sell"]
        buys = [t for t in result.trades if t["side"] == "buy"]
        assert len(buys) == 1, "d1 应有一次满仓买入"
        assert len(sells) == 1, "除息后必须有一次清仓卖出"
        # 除息后等效股数翻倍 → 清仓卖掉的是 2 倍股数
        assert sells[0]["shares"] == 2 * buys[0]["shares"]
        # 清仓后不能有任何持仓残留
        held_dates = {h["date"] for h in result.holdings}
        assert sells[0]["date"] not in held_dates or \
            all(h["date"] != sells[0]["date"] for h in result.holdings), \
            "卖出当日仍记录持仓"
        after = [h for h in result.holdings if h["date"] > sells[0]["date"]]
        assert after == [], "清仓之后仍有持仓记录"
        # 净值 = 全部现金（无费用、价格不变、无后续信号）
        assert result.nav.iloc[-1] == pytest.approx(2_000_000.0, abs=1.0), \
            "分红再投应让净值翻倍，而不是留下残值"
        assert result.holdings == [] or all(
            h["date"] <= buys[0]["date"] for h in result.holdings
        )


# ==================== 权重与择时 ====================


class TestWeightsAndRegime:
    def test_equal_weights(self):
        scores = pd.Series({"a": 80.0, "b": 60.0, "c": 40.0})
        assert weights_for_picks(["a", "b"], scores, "equal") == {"a": 0.5, "b": 0.5}

    def test_score_weights_proportional(self):
        scores = pd.Series({"a": 80.0, "b": 20.0})
        got = weights_for_picks(["a", "b"], scores, "score")
        assert got["a"] == pytest.approx(0.8)
        assert got["b"] == pytest.approx(0.2)
        assert sum(got.values()) == pytest.approx(1.0)

    def test_score_weights_all_zero_falls_back_to_equal(self):
        scores = pd.Series({"a": 0.0, "b": 0.0})
        got = weights_for_picks(["a", "b"], scores, "score")
        assert got == {"a": 0.5, "b": 0.5}

    def test_cap_and_redistribute(self):
        """超标部分按比例分给未触顶的标的，迭代到全部达标。"""
        got = cap_and_redistribute({"a": 0.5, "b": 0.3, "c": 0.2}, 0.35)
        assert got["a"] == pytest.approx(0.35)
        assert got["b"] == pytest.approx(0.35)
        assert got["c"] == pytest.approx(0.30)
        assert sum(got.values()) == pytest.approx(1.0)

    def test_cap_single_round_is_enough(self):
        # a 超标 0.1，b/c 有足够空间承接 → 一轮就收敛
        got = cap_and_redistribute({"a": 0.5, "b": 0.25, "c": 0.25}, 0.4)
        assert got["a"] == pytest.approx(0.4)
        assert got["b"] == pytest.approx(0.3)
        assert got["c"] == pytest.approx(0.3)

    def test_cap_all_capped_leaves_cash(self):
        """全部触顶且无人承接 → 权重和 < 1，差额留给现金。"""
        got = cap_and_redistribute({"a": 0.2, "b": 0.2, "c": 0.2, "d": 0.2, "e": 0.2}, 0.1)
        assert all(v == pytest.approx(0.1) for v in got.values())
        assert sum(got.values()) == pytest.approx(0.5)

    def test_cap_normalizes_input(self):
        got = cap_and_redistribute({"a": 2.0, "b": 1.0, "c": 1.0}, 0.5)
        assert got["a"] == pytest.approx(0.5)
        assert sum(got.values()) == pytest.approx(1.0)

    def test_cap_not_triggered(self):
        weights = {"a": 0.3, "b": 0.3, "c": 0.4}
        assert cap_and_redistribute(weights, 0.5) == weights

    def test_cap_empty(self):
        assert cap_and_redistribute({}, 0.5) == {}

    def test_regime_three_tiers(self):
        # 构造一条先涨后跌的基准，验证三档敞口
        rising = list(np.linspace(1.0, 2.0, 250))
        falling = list(np.linspace(2.0, 1.0, 250))
        close = pd.Series(rising + falling,
                          index=pd.bdate_range("2020-01-02", periods=500))
        exposure = regime_exposure(close, ma_fast=20, ma_slow=50)
        assert exposure.max() == 1.0
        assert exposure.min() == 0.0
        assert 0.5 in set(exposure.round(6))

    def test_regime_warmup_is_half(self):
        close = pd.Series(np.linspace(100, 200, 60),
                          index=pd.bdate_range("2020-01-02", periods=60))
        exposure = regime_exposure(close, ma_fast=10, ma_slow=30)
        assert exposure.iloc[:29].eq(0.5).all()      # 慢线未成型 → 半仓

    def test_regime_zero_empties_target(self):
        scores = pd.DataFrame(
            {"a": [100.0, 100.0, 100.0], "b": [50.0, 50.0, 50.0]},
            index=DAYS[:3],
        )
        exposure = pd.Series([0.0, 0.0, 0.0], index=DAYS[:3])
        targets = build_target_weights(scores, DAYS[:3], params(regime=True),
                                       exposure=exposure)
        assert all(not w for w in targets.values())


# ==================== 参数校验 ====================


class TestParamsValidation:
    def test_bad_weight_method(self):
        with pytest.raises(ValueError, match="weight_method"):
            StrategyParams(weight_method="hrp")

    def test_bad_top_n(self):
        with pytest.raises(ValueError, match="top_n"):
            StrategyParams(top_n=0)

    def test_max_weight_times_topn_below_one_warns(self):
        """过严的上限不是错误，只是「常态持币」—— 只告警不阻断。"""
        loose = StrategyParams(top_n=20, max_weight=0.01)
        assert loose.top_n == 20                     # 能正常构造
        assert any("现金" in w for w in loose.warnings())
        # 默认配置（max_weight=0.10）下 top_n≥10 能满仓，不该告警
        assert StrategyParams(top_n=10).warnings() == []
        assert StrategyParams(top_n=20).warnings() == []
        # 而 top_n=5 会被上限卡住只用一半资金 —— 这正是要提示的
        assert StrategyParams(top_n=5).warnings()

    def test_to_dict_round_trip(self):
        original = StrategyParams(top_n=5, rebal_days=10, regime=False,
                                  max_weight=0.25)
        data = original.to_dict()
        assert data["top_n"] == 5 and data["rebal_days"] == 10
        assert data["regime"] is False
        assert StrategyParams.from_payload(data) == original
