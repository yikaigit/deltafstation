"""技术指标测试：talib 面板封装的三个坑 + 滚动统计口径。

重点回归
--------
1. ``apply_per_column`` 的 NaN 段隔离 —— talib 会把窗口里的 NaN 向后传播到
   整段输出，一只次新股足以让整列报废；
2. ``rolling_max`` 的 ``min_periods`` —— 默认取 ``max(20, window//4)``，
   否则 ``dd_1y`` 上市首日恒为 0、rank 后是毫无信息量的「假中性」；
3. ``downside_vol`` 的 ``pct_change(fill_method=None)`` —— 默认的 pad 填充会把
   未上市期算成 0 收益，且未来 pandas 会改默认值导致跨版本结果不一致；
4. ``downside_vol`` 的正收益置零必须 ``clip(upper=0.0)`` —— ``where(ret<0, 0.0)``
   会把 NaN 也改成 0.0，缺数据被当成「零风险」，经 direction=-1 反向标准化后
   反而拿最高分（未上市股票霸占 top-N）。
"""
from __future__ import annotations

import warnings

import numpy as np
import pandas as pd
import pytest

from backend.core import indicators as ind

DATES = pd.bdate_range("2024-01-02", periods=120)


def frame(**columns) -> pd.DataFrame:
    return pd.DataFrame(columns, index=DATES[: len(next(iter(columns.values())))])


# ---------------- require_talib ----------------


class TestRequireTalib:
    def test_missing_talib_raises(self, monkeypatch):
        monkeypatch.setattr(ind, "talib", None)
        with pytest.raises(RuntimeError, match="TA-Lib"):
            ind.require_talib()
        # 包装函数同样要报错，而不是 AttributeError
        with pytest.raises(RuntimeError, match="TA-Lib"):
            ind.sma(frame(a=[1.0, 2.0, 3.0]), 2)


# ---------------- apply_per_column ----------------


class TestApplyPerColumn:
    def test_nan_segment_is_not_propagated(self):
        """次新股回归：20 行 NaN + 30 行有效值。

        talib.SMA 遇到窗口内 NaN 会把 NaN 传播到整段输出，直接喂面板会让
        后 30 行也全变 NaN。
        """
        values = np.concatenate([np.full(20, np.nan), np.arange(1.0, 31.0)])
        f = pd.DataFrame({"a": values}, index=DATES[:50])
        out = ind.sma(f, 5)

        assert out["a"].iloc[:20].isna().all(), "未上市期必须保持 NaN"
        assert out["a"].iloc[24:].notna().all(), "窗口满 5 天后必须有值（NaN 未被传播）"
        # 第 25 行是有效段第 5 个值 → SMA(5) = mean(1..5)
        assert out["a"].iloc[24] == pytest.approx(3.0, rel=1e-5)

    def test_gap_in_middle_is_isolated(self):
        """中间停牌/缺口：缺口两侧的指标各自独立计算，互不污染。"""
        values = np.concatenate([np.arange(1.0, 11.0), np.full(5, np.nan),
                                 np.arange(101.0, 111.0)])
        f = pd.DataFrame({"a": values}, index=pd.bdate_range("2023-01-02", periods=25))
        out = ind.sma(f, 3)
        assert out["a"].iloc[:2].isna().all()            # 不足窗口
        assert out["a"].iloc[10:17].isna().all()         # 5 天缺口 + 缺口后 2 天
        assert out["a"].iloc[17:].notna().all()

        # 缺口不跨接：复牌后头两个 SMA 必须用「只有复牌后数据」的窗口
        assert out["a"].iloc[17] == pytest.approx(102.0, rel=1e-5)
        # 回归：曾经把缺口前后拼成一段，算出 mean(9,10,101)=40 这种幽灵值
        assert 40.0 not in out["a"].to_numpy()
        assert 71.0 not in out["a"].to_numpy()

    def test_short_column_skipped(self):
        f = frame(a=[1.0, np.nan])
        out = ind.sma(f, 5)
        assert out.isna().all().all()

    def test_failing_column_does_not_kill_batch(self):
        """单列算不出来只跳过该列，不拖垮整批。"""
        f = frame(a=np.arange(1.0, 30.0), b=np.arange(1.0, 30.0))
        state = {"calls": 0}

        def fn(segment, **kwargs):
            state["calls"] += 1
            if state["calls"] == 2:      # 第二列算不出来
                raise ValueError("boom")
            return segment

        out = ind.apply_per_column(f, fn)
        assert state["calls"] == 2, "两列都必须被尝试"
        assert out["a"].notna().all(), "第一列结果必须写回"
        assert out["b"].isna().all(), "失败的列整列留空，不拖垮整批"

    def test_returns_float32(self):
        f = frame(a=np.arange(1.0, 30.0))
        out = ind.sma(f, 5)
        assert out.dtypes.unique().tolist() == ["float32"]

    def test_index_and_columns_preserved(self):
        f = frame(a=np.arange(1.0, 30.0), b=np.arange(2.0, 31.0))
        out = ind.ema(f, 5)
        assert out.index.equals(f.index)
        assert list(out.columns) == list(f.columns)


class TestSmaEma:
    def test_sma_manual(self):
        values = np.arange(1.0, 11.0)
        out = ind.sma(frame(a=values), 3)
        assert out["a"].iloc[2] == pytest.approx((1 + 2 + 3) / 3, rel=1e-6)
        assert out["a"].iloc[9] == pytest.approx((8 + 9 + 10) / 3, rel=1e-6)

    def test_ema_length(self):
        out = ind.ema(frame(a=np.arange(1.0, 30.0)), 10)
        assert out["a"].notna().sum() >= 19


class TestMacd:
    def test_hist_has_sign(self):
        # 单边上涨 → DIF > DEA → hist > 0
        up = pd.DataFrame({"c": 100.0 * 1.01 ** np.arange(60)})
        hist = ind.macd_hist(up)
        assert hist["c"].dropna().gt(0).all()

    def test_hist_length_matches(self):
        f = frame(a=np.arange(1.0, 61.0))
        hist = ind.macd_hist(f)
        assert hist.shape == f.shape
        assert not hist.isna().all().all()


class TestAdx:
    def test_triple_alignment(self):
        """ADX/PDI/MDI 必须是**同一段有效数据**算出来的。

        分开算会因各列 NaN 位置不同而错位 —— 这里让 b 列晚 10 天上市，
        再拿「只含 b 有效段」的子帧重算，逐位对比：错位的话数值必然不同。
        """
        up = 100.0 * 1.005 ** np.arange(60)
        down = 100.0 * 0.995 ** np.arange(60)
        close = frame(a=up, b=down)
        close.loc[close.index[:10], "b"] = np.nan      # b 晚 10 天上市
        high, low = close * 1.002, close * 0.998

        adx, pdi, mdi = ind.adx(close, high, low)
        assert pdi.index.equals(adx.index) and list(pdi.columns) == list(adx.columns)

        sub_close = close.loc[close.index[10]:]
        sub_high = high.loc[high.index[10]:]
        sub_low = low.loc[low.index[10]:]
        sub_adx, sub_pdi, sub_mdi = ind.adx(sub_close, sub_high, sub_low)

        for full, part, name in ((adx, sub_adx, "adx"), (pdi, sub_pdi, "pdi"),
                                 (mdi, sub_mdi, "mdi")):
            merged = part["b"].reindex(full.index)
            assert np.allclose(full["b"].to_numpy(), merged.to_numpy(),
                               equal_nan=True), f"{name} 与子帧重算不一致（疑似错位）"

    def test_direction_sign(self):
        close = frame(up=100.0 * 1.01 ** np.arange(50), down=100.0 * 0.99 ** np.arange(50))
        high, low = close * 1.003, close * 0.997
        _, pdi, mdi = ind.adx(close, high, low)
        assert (pdi - mdi)["up"].iloc[-1] > 0
        assert (pdi - mdi)["down"].iloc[-1] < 0


class TestObv:
    def test_manual_accumulation(self):
        close = frame(a=[10.0, 11.0, 10.5, 12.0])
        volume = frame(a=[100.0, 200.0, 150.0, 300.0])
        out = ind.obv(close, volume)
        # 涨 +vol、涨 +vol、跌 -vol、涨 +vol
        assert out["a"].tolist() == [100.0, 300.0, 150.0, 450.0]

    def test_suspended_column_all_nan(self):
        close = frame(a=[np.nan] * 5 + [1.0] * 5)
        volume = frame(a=[np.nan] * 5 + [10.0] * 5)
        out = ind.obv(close, volume)
        assert out["a"].iloc[:5].isna().all()
        assert out["a"].iloc[5:].notna().all()


class TestAtrMfi:
    def test_atr_positive(self):
        close = frame(a=100.0 * (1 + 0.01 * np.sin(np.arange(60) / 3)))
        high, low = close * 1.01, close * 0.99
        out = ind.atr(high, low, close, 14)
        assert out["a"].dropna().gt(0).all()

    def test_mfi_bounded(self):
        rng = np.random.default_rng(1)
        base = 100.0 * np.cumprod(1 + rng.normal(0, 0.02, 60))
        close = frame(a=base)
        high = frame(a=base * 1.01)
        low = frame(a=base * 0.99)
        volume = frame(a=rng.uniform(1e5, 2e5, 60))
        out = ind.mfi(high, low, close, volume, 14)
        assert out["a"].dropna().between(0, 100).all()


# ---------------- 滚动统计 ----------------


class TestRollingMax:
    def test_default_min_periods_blocks_early_fake_neutral(self):
        """回归 ``dd_1y`` 上市首日问题。

        ``min_periods=1`` 时上市第一天当前价即最高价 → 回撤恒 0 → 横截面
        rank 后全变成 50 分位，一个没有任何信息量的「假中性」。
        """
        f = frame(a=np.arange(1.0, 31.0))
        out = ind.rolling_max(f, 250)
        assert out.isna().all().all(), "窗口不足时不该给值"
        explicit = ind.rolling_max(f, 250, min_periods=1)
        assert explicit["a"].notna().any()

    def test_value_is_window_maximum(self):
        f = frame(a=[1.0, 5.0, 3.0, 2.0, 4.0])
        out = ind.rolling_max(f, 3, min_periods=1)
        assert out["a"].tolist() == [1.0, 5.0, 5.0, 5.0, 4.0]


class TestDownsideVol:
    def test_no_future_warning(self):
        """直接调 ``pct_change(fill_method=None)``，绝不能触发 FutureWarning ——
        否则同一份数据在不同 pandas 版本上算出的结果不同，
        「同日两次运行结果完全一致」这条验收就没法保证。"""
        f = frame(a=100.0 * (1 + 0.001 * np.arange(60)))
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            ind.downside_vol(f, 20)

    def test_gap_does_not_create_spurious_downside(self):
        """未上市缺口后股价腰斩：复牌跳空不是「下跌」，不能计入下行波动。

        复牌首日收益横跨缺口，是 100→50 的 -50% 跳空还是「无数据」？
        口径取后者：跳空不是连续下跌，计入会凭空造出一次暴跌。
        受影响窗口随之作废（NaN），直到窗口整体越过缺口才有值。
        """
        values = [100.0] * 15 + [np.nan] * 5 + [50.0] * 25
        out = ind.downside_vol(frame(a=values), 20)
        assert pd.isna(out["a"].iloc[20]), "复牌首日收益横跨缺口，应当作废"
        # 复盘后一路走平 → 越过缺口后的下行波动是真实的 0
        assert out["a"].iloc[-1] == pytest.approx(0.0, abs=1e-6)

    def test_single_drop_day_matches_manual(self):
        values = [100.0] * 30
        values[25] = 90.0
        out = ind.downside_vol(frame(a=values), 20)
        expected = np.sqrt(0.01 / 20) * np.sqrt(250)
        assert out["a"].iloc[-1] == pytest.approx(expected, rel=1e-5)

    def test_flat_series_zero(self):
        out = ind.downside_vol(frame(a=[100.0] * 40), 20)
        assert out["a"].iloc[-1] == pytest.approx(0.0, abs=1e-6)

    def test_all_nan_column_stays_nan(self):
        """全 NaN 列（未上市/全程停牌）必须仍全 NaN，绝不能变成 0。

        这是踩过的真 bug：正收益置零用的是 ``where(ret < 0, 0.0)``，
        NaN 让条件为 False 于是取到替代值 0.0，整列下行方差恒为 0 ——
        而这个因子 ``direction=-1``，「零风险」经反向标准化后是最高分，
        一只还没上市的股票能以 99 分霸占 top-N。
        """
        f = frame(a=[np.nan] * 40)
        out = ind.downside_vol(f, 20)
        assert out["a"].isna().all(), "缺数据被当成了零风险"

    def test_nan_tail_does_not_backfill_zero(self):
        """上市前 30 天 NaN + 后 30 天正常：前 30 天仍须为 NaN。

        ``where`` 的写法会让前 30 天也是 0.0，等于凭空造出 30 天
        「零下行波动」的历史。有效收益率从 index 31 起（index 30 的
        分母是 NaN），min_periods=20，故前 50 天全 NaN、之后才有值。
        """
        values = [np.nan] * 30 + [100.0 * (1 + 0.003 * i) for i in range(30)]
        out = ind.downside_vol(frame(a=values), 20)
        assert out["a"].iloc[:50].isna().all()
        assert out["a"].iloc[50:].notna().all()

    def test_nan_burst_inside_real_data(self):
        """中段停牌 5 天：停牌窗口及随后的若干窗口作废，不是 0。"""
        values = [100.0 * (1 + 0.001 * i) for i in range(70)]
        for i in range(20, 25):
            values[i] = np.nan
        out = ind.downside_vol(frame(a=values), 20)
        # index 25 的分母是停牌价 → NaN；窗口整体越过缺口才有值
        assert out["a"].iloc[:45].isna().all()
        assert out["a"].iloc[45:].notna().all()


class TestRollingStats:
    def test_pct_rank_monotonic(self):
        f = frame(a=np.arange(1.0, 31.0))
        out = ind.rolling_pct_rank(f, 20)
        last = out["a"].iloc[-1]
        assert last > 0.9
        assert out["a"].dropna().between(0, 1).all()

    def test_pct_rank_constant_series(self):
        out = ind.rolling_pct_rank(frame(a=[7.0] * 30), 20)
        # 并列值取平均名次 → 约 0.5（不是 0）
        assert 0.45 <= out["a"].iloc[-1] <= 0.55

    def test_std_and_mean(self):
        f = frame(a=np.arange(1.0, 31.0))
        assert ind.rolling_std(f, 20)["a"].iloc[-1] > 0
        assert ind.rolling_mean(f, 20)["a"].iloc[-1] == pytest.approx(
            np.arange(11.0, 31.0).mean(), rel=1e-5
        )

    def teardown_method(self):
        pass


class TestLinearRegSlope:
    def test_uptrend_positive_slope(self):
        f = frame(a=100.0 * 1.002 ** np.arange(60))
        out = ind.linearreg_slope(f, 60)
        assert out["a"].dropna().gt(0).all()

    def test_log_price_comparable(self):
        """slope_60 的输入是对数价格 → 斜率不再随股价量纲变化。"""
        log_hi = np.log(1000.0 * 1.002 ** np.arange(60))
        log_lo = np.log(1.0 * 1.002 ** np.arange(60))
        f = pd.DataFrame({"hi": log_hi, "lo": log_lo})
        out = ind.linearreg_slope(f, 60)
        assert out["hi"].iloc[-1] == pytest.approx(out["lo"].iloc[-1], rel=1e-6)
