"""v1 技术因子测试：注册表契约、口径回归、全因子冒烟。

重点回归
--------
1. ``rs_60`` 用**累计收益比** ``(1+R_i)/(1+R_bench)-1``，不用初稿的
   ``Σ(r_i - r_bench)`` —— 算术累加会系统性高估高波动股
   （实测同一时点：算术累加中位 +7.88%，累计收益比中位 +6.55%）；
2. ``_pct_change`` 不用 ``pct_change``（默认 pad 填充会把未上市期算成 0 收益）；
3. ``rsi_health`` 是分段映射而非单调因子，断点必须落在文档写的区间上。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.core import factors as fl
from backend.core.factors import (
    CATEGORY_LABELS,
    MOMENTUM,
    QUALITY,
    RISK,
    TREND,
    VALUATION,
    VOLUME,
)
from backend.core.panels import Panels

DATES = pd.bdate_range("2023-01-02", periods=340)


def make_panel(seed=7, codes=("a", "b", "c"), n_days=340, drift=(0.0004, 0.0, -0.0003),
               vol=(0.018, 0.03, 0.012), with_bench=True, industry=True) -> Panels:
    """合成面板：三只走势不同的票 + 可选的行业映射与基准收益。"""
    rng = np.random.default_rng(seed)
    index = DATES[:n_days]
    close = {}
    for code, mu, sigma in zip(codes, drift, vol):
        steps = rng.normal(mu, sigma, n_days)
        steps[0] = 0.0
        close[code] = 100.0 * np.cumprod(1.0 + steps)
    close = pd.DataFrame(close, index=index)
    high = close * (1.0 + np.abs(rng.normal(0, 0.004, close.shape)))
    low = close * (1.0 - np.abs(rng.normal(0, 0.004, close.shape)))
    open_ = close * (1.0 + rng.normal(0, 0.003, close.shape))
    volume = pd.DataFrame(rng.uniform(8e5, 2e6, close.shape), index=index, columns=close.columns)
    amount = volume * close
    turn = pd.DataFrame(rng.uniform(0.002, 0.05, close.shape), index=index,
                        columns=close.columns)
    pct = close.pct_change(fill_method=None) * 100.0
    status = pd.DataFrame(np.ones(close.shape, dtype="int8"), index=index, columns=close.columns)

    frames = {
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "amount": amount, "turn": turn,
        "pct_chg": pct, "tradestatus": status,
        "raw_close": close,
    }
    frames = {k: v.astype("float32") for k, v in frames.items()}
    return Panels(
        frames,
        industry=pd.Series({"a": "银行Ⅱ", "b": "银行Ⅱ", "c": "计算机"}) if industry else pd.Series(dtype=object),
        bench_ret=pd.Series(
            np.concatenate([[np.nan], np.full(n_days - 1, 0.0008)]), index=index
        ) if with_bench else pd.Series(dtype="float64"),
    )


def _with_fundamental_frames(panel):
    """给合成面板补上估值/质量字段（P5 因子需要）。

    合成面板只造了行情，估值与财务字段是 P5 才引入的。这里用「贴近真实的数据」
    补上：PE 在 10~40 之间波动、ROE 为正、部分票 PEG 为负（覆盖亏损分支）。
    数值本身不重要，重要的是**形状与 dtype 与真实面板一致**，
    这样因子代码里的 NaN 语义、量纲处理才会被真正执行到。
    """
    index = panel.dates
    codes = panel.codes
    n = len(index)
    rng = np.random.default_rng(42)

    def frame(low, high):
        values = rng.uniform(low, high, size=(n, len(codes)))
        return pd.DataFrame(values, index=index, columns=codes).astype("float32")

    panel._frames["pe_ttm"] = frame(5.0, 60.0)
    panel._frames["pb_mrq"] = frame(0.5, 8.0)
    panel._frames["ps_ttm"] = frame(0.4, 12.0)
    panel._frames["peg"] = frame(-2.0, 4.0)          # 含负值，覆盖 peg_safe 的过滤分支
    panel._frames["total_mv"] = frame(1e10, 5e11)
    panel._frames["roe_wa"] = frame(-5.0, 30.0)
    panel._frames["eps"] = frame(-0.5, 3.0)
    panel._frames["bps"] = frame(1.0, 30.0)
    panel._frames["rev_yoy"] = frame(-30.0, 60.0)
    panel._frames["profit_yoy"] = frame(-50.0, 120.0)
    panel._frames["gross_margin"] = frame(5.0, 80.0)
    panel._frames["ocf_ps"] = frame(-0.5, 4.0)
    panel._frames["div_yield"] = frame(0.0, 6.0)
    return panel


class TestRegistry:
    def test_factor_count_and_categories(self):
        # P5 起 19 技术因子 + 5 估值 + 6 质量 = 30
        assert len(fl.FACTORS) == 30
        assert len(fl.names_by_category(TREND)) == 6
        assert len(fl.names_by_category(MOMENTUM)) == 5
        assert len(fl.names_by_category(VOLUME)) == 4
        assert len(fl.names_by_category(RISK)) == 4
        assert len(fl.names_by_category(VALUATION)) == 5
        assert len(fl.names_by_category(QUALITY)) == 6

    def test_category_labels_cover_all(self):
        for category in fl.categories():
            assert category in CATEGORY_LABELS

    def test_metadata_contract(self):
        for name, factor in fl.FACTORS.items():
            assert factor.name == name
            assert factor.direction in (1, -1), f"{name} 方向必须是 ±1"
            assert factor.lookback > 0, f"{name} lookback 必须为正"
            assert factor.description, f"{name} 必须有描述"
            assert callable(factor.compute)

    def test_risk_factor_directions(self):
        """风险类因子里 atr/hv/downside 是「越低越好」，dd_1y 是「越接近 0 越好」。"""
        assert fl.FACTORS["atr_pct_rank"].direction == -1
        assert fl.FACTORS["hv_60"].direction == -1
        assert fl.FACTORS["downside_vol"].direction == -1
        assert fl.FACTORS["dd_1y"].direction == 1

    def test_lookbacks_match_config(self):
        """每个版本配置引用的因子都必须已注册，且 lookback 覆盖最大滚动窗口。

        注意不能断言 `set(spec["factors"]) == set(fl.FACTORS)`：
        注册表是「所有可用因子」的超集，而具体版本只用其中一部分
        （v1.1.0 只有 19 个技术因子，v2.0.0 才用满 30 个）。
        正确的约束是「版本引用的 ⊆ 注册表」。
        """
        from backend.core.scorer import Scorer

        for version in ("v1.0.0", "v1.1.0", "v2.0.0"):
            spec = Scorer.load_version(version)
            assert set(spec["factors"]) <= set(fl.FACTORS), f"{version} 引用了未注册因子"
        # 全量注册表的最大 lookback 是技术因子的 260 天窗口
        assert max(f.lookback for f in fl.FACTORS.values()) == 260

    def test_categories_order(self):
        # 顺序即注册顺序：先技术四维，再估值、质量（P5 追加）
        assert fl.categories() == [TREND, MOMENTUM, VOLUME, RISK, VALUATION, QUALITY]


class TestComputeApi:
    def test_unknown_factor_raises(self):
        with pytest.raises(KeyError, match="未注册的因子"):
            fl.compute("no_such_factor", make_panel())

    def test_compute_many_isolates_failures(self, monkeypatch):
        panel = make_panel(n_days=60)
        real = fl.compute
        names = ["roc_20", "roc_60"]

        def fake(name, target):
            if name == "roc_60":
                raise RuntimeError("boom")
            return real(name, target)

        monkeypatch.setattr(fl, "compute", fake)
        frames = fl.compute_many(names, panel)
        assert list(frames) == ["roc_20"], "失败的因子被跳过，其余照常返回"

    def test_progress_callback_called(self):
        panel = make_panel(n_days=60)
        seen = []
        fl.compute_many(["roc_20", "roc_60"], panel, on_progress=lambda i, t, n: seen.append((i, t, n)))
        assert seen == [(1, 2, "roc_20"), (2, 2, "roc_60")]


class TestPctChange:
    def test_no_pad_across_gap(self):
        """未上市缺口不能被算成 0 收益混进分位统计。"""
        values = [100.0] * 5 + [np.nan] * 5 + [200.0] * 5
        frame = pd.DataFrame({"a": values}, index=DATES[:15])
        out = fl._pct_change(frame, 1)
        assert out["a"].iloc[5:10].isna().all()
        # 缺口后第一天的收益必须缺失；pad 填充会算出 200/100-1=1 的幽灵收益
        assert np.isnan(out["a"].iloc[10])
        assert out["a"].iloc[11] == pytest.approx(0.0, abs=1e-6)

    def test_shift_returns_nan(self):
        frame = pd.DataFrame({"a": np.arange(1.0, 11.0)}, index=DATES[:10])
        assert fl._pct_change(frame, 3)["a"].iloc[:3].isna().all()


class TestMissingDataIsNotZeroRisk:
    """未上市/全程停牌 → 因子必须 NaN，绝不能是「零风险」的高分。

    实测踩过的坑：``downside_vol`` 内部 ``ret.where(ret < 0, 0.0)`` 把 NaN
    也改成 0.0，而该因子 ``direction=-1``，「零风险」经反向标准化变成最高分 ——
    于是 2021-10-18 尚未上市的 sh.600938 以 ``score_pct=99`` 霸占 top-N。
    同一时点全 NaN 列若被误判成 0，回测会去买入一只买不到的股票。
    """

    def _panel_with_unlisted(self):
        index = DATES[:120]
        rng = np.random.default_rng(3)
        steps = rng.normal(0.0003, 0.015, len(index))
        steps[0] = 0.0
        listed = 100.0 * np.cumprod(1.0 + steps)
        close = pd.DataFrame({"listed": listed}, index=index)
        close["unlisted"] = np.nan
        frames = {k: v.astype("float32") for k, v in
                  {"close": close, "high": close * 1.01, "low": close * 0.99}.items()}
        return Panels(frames, bench_ret=pd.Series(
            np.concatenate([[np.nan], np.full(len(index) - 1, 0.0008)]), index=index))

    def test_downside_vol_factor_is_nan_for_unlisted(self):
        panel = self._panel_with_unlisted()
        out = fl.compute("downside_vol", panel)
        assert out["unlisted"].isna().all(), "未上市被算成了零风险"
        # 对照列必须有真实的下行波动（而不是被填成 0）
        assert out["listed"].dropna().gt(0).any()

    def test_risk_dimension_all_nan_for_unlisted(self):
        """四个风险因子对未上市列全 NaN —— 这样 composite 才会把
        该股票的 score_abs 置 NaN，行直接被快照剔除。"""
        panel = self._panel_with_unlisted()
        for name in fl.names_by_category(RISK):
            out = fl.compute(name, panel)
            assert out["unlisted"].isna().all(), f"{name} 给未上市列造出了值"

    def test_unlisted_excluded_from_snapshot(self):
        """端到端：整列全 NaN 的股票在打分后不进入结果。"""
        from backend.core import scoring as sc
        panel = self._panel_with_unlisted()
        frames = fl.compute_many(list(fl.FACTORS), panel)
        out = sc.score_panels(
            frames,
            factor_meta={n: {"direction": f.direction} for n, f in fl.FACTORS.items()},
            dimension_of={n: f.category for n, f in fl.FACTORS.items()},
            weights={"trend": 0.3, "momentum": 0.3, "volume": 0.2, "risk": 0.2},
        )
        # 所有维度都缺 → 权重归一后仍为 0 → score_abs 全 NaN
        assert out["score_abs"]["unlisted"].isna().all()
        assert out["data_quality"]["unlisted"].isna().all()
        assert out["score_abs"]["listed"].notna().any()


class TestRs60:
    def test_matches_geometric_manual(self):
        """手工核对 (1+R_i)/(1+R_bench)-1。

        若换成 ``Σ(r_i - r_bench)`` 会得到一个不同（且偏高）的数 ——
        这就是要盯的回归点。
        """
        n = 61
        index = DATES[:n]
        stock = 100.0 * 1.002 ** np.arange(n)
        close = pd.DataFrame({"a": stock}, index=index)
        frames = {k: v.astype("float32") for k, v in
                  {"close": close, "high": close, "low": close}.items()}
        bench = pd.Series(np.concatenate([[np.nan], np.full(n - 1, 0.001)]), index=index)
        panel = Panels(frames, bench_ret=bench)

        out = fl.compute("rs_60", panel)
        expected = (1.002 ** 60) / (1.001 ** 60) - 1.0
        assert out["a"].iloc[-1] == pytest.approx(expected, rel=1e-5)
        assert out["a"].iloc[:60].isna().all()

    def test_arithmetic_sum_would_differ(self):
        """文档化：算术累加口径与几何口径不同，且对高波动股偏高。"""
        rng = np.random.default_rng(11)
        n = 61
        index = DATES[:n]
        steps = rng.normal(0.0, 0.03, n - 1)
        close = pd.DataFrame({"a": 100.0 * np.cumprod(np.concatenate([[1.0], 1 + steps]))},
                             index=index)
        frames = {"close": close.astype("float32"), "high": close.astype("float32"),
                  "low": close.astype("float32")}
        bench_steps = rng.normal(0.0, 0.01, n - 1)
        bench = pd.Series(np.concatenate([[np.nan], bench_steps]), index=index)
        panel = Panels(frames, bench_ret=bench)

        geometric = float(fl.compute("rs_60", panel)["a"].iloc[-1])
        arithmetic = float(np.sum(steps - bench_steps))
        assert geometric != pytest.approx(arithmetic, abs=1e-4)

    def test_degrades_to_absolute_without_benchmark(self):
        panel = make_panel(with_bench=False, n_days=80)
        rs = fl.compute("rs_60", panel)
        roc = fl.compute("roc_60", panel)
        assert np.allclose(rs.to_numpy(), roc.to_numpy(), equal_nan=True)


class TestRsiHealth:
    def test_breakpoints(self):
        assert np.interp(0.0, fl._RSI_XP, fl._RSI_FP) == 0.0
        assert np.interp(50.0, fl._RSI_XP, fl._RSI_FP) == 1.0      # 45~62 最优
        assert np.interp(100.0, fl._RSI_XP, fl._RSI_FP) == 0.0     # 过热
        assert np.interp(20.0, fl._RSI_XP, fl._RSI_FP) < 0.5       # 极弱

    def test_output_bounded_and_finite(self):
        panel = make_panel(n_days=120)
        out = fl.compute("rsi_health", panel)
        values = out.to_numpy()
        finite = values[np.isfinite(values)]
        assert finite.min() >= 0.0 and finite.max() <= 1.0
        assert out.dtypes.unique().tolist() == ["float32"]

    def test_unlisted_stays_nan(self):
        panel = make_panel(n_days=120)
        frame = panel.close.copy()
        frame.iloc[:40] = np.nan
        panel = Panels({**panel._frames, "close": frame.astype("float32")})
        out = fl.compute("rsi_health", panel)
        assert out.iloc[:40].isna().all().all()


class TestDirectionAndRange:
    @pytest.mark.parametrize("name,expected_sign", [
        ("adx_dir", None),      # 单独测
        ("dd_1y", "non_positive"),
        ("turn_rank", "unit"),
        ("atr_pct_rank", "unit"),
    ])
    def test_value_ranges(self, name, expected_sign):
        panel = make_panel(n_days=300)
        out = fl.compute(name, panel)
        values = out.to_numpy()
        finite = values[np.isfinite(values)]
        if expected_sign == "non_positive":
            assert finite.max() <= 1e-6, "回撤不可能为正"
        elif expected_sign == "unit":
            assert finite.min() >= 0.0 and finite.max() <= 1.0
        assert out.shape == panel.shape

    def test_adx_dir_sign_follows_trend(self):
        up = pd.DataFrame({"u": 100.0 * 1.01 ** np.arange(80)})
        down = pd.DataFrame({"d": 100.0 * 0.99 ** np.arange(80)})
        close = pd.concat([up, down], axis=1)
        frames = {
            "close": close.astype("float32"),
            "high": (close * 1.003).astype("float32"),
            "low": (close * 0.997).astype("float32"),
        }
        panel = Panels(frames)
        out = fl.compute("adx_dir", panel).iloc[-1]
        assert out["u"] > 0, "单边上涨必须是正向"
        assert out["d"] < 0, "单边下跌必须是负向（否则会被误判成强趋势而给高分）"
        assert abs(out["u"]) <= 1.0 and abs(out["d"]) <= 1.0, "强度必须裁剪到 ±1"

    def test_slope_60_comparable_across_price_levels(self):
        """对数价格回归 → 斜率与股价量纲无关。"""
        hi = 5000.0 * 1.002 ** np.arange(80)
        lo = 2.0 * 1.002 ** np.arange(80)
        close = pd.concat([pd.Series(hi), pd.Series(lo)], axis=1).set_axis(["hi", "lo"], axis=1)
        frames = {"close": close.astype("float32")}
        panel = Panels(frames)
        out = fl.compute("slope_60", panel).iloc[-1]
        assert out["hi"] == pytest.approx(out["lo"], rel=1e-5)

    def test_dd_1y_zero_for_flat(self):
        close = pd.DataFrame({"a": [100.0] * 300})
        panel = Panels({"close": close.astype("float32")})
        out = fl.compute("dd_1y", panel)
        assert out["a"].dropna().abs().max() < 1e-6


class TestAllFactorsSmoke:
    def test_every_factor_runs_and_matches_shape(self):
        """全部因子在合成面板上都能算完，输出同形状 float32。

        面板必须带估值/质量字段 —— 否则估值因子会 KeyError，
        那这个「冒烟」就只覆盖了技术因子，P5 新增的 11 个等于没测。
        """
        panel = make_panel(n_days=320)
        panel = _with_fundamental_frames(panel)
        for name in fl.FACTORS:
            out = fl.compute(name, panel)
            assert out.shape == panel.shape, name
            assert out.dtypes.unique().tolist() == ["float32"], name
            # 最后一行的窗口已足够长，至少有一只票有值
            assert out.iloc[-1].notna().any(), name

    def test_unlisted_column_isolated(self):
        """次新股回归：c 列最后 40 天才上市，指标不能被 NaN 传播污染。"""
        panel = make_panel(n_days=320)
        close = panel.close.copy()
        close.iloc[:200] = np.nan          # 留 120 个有效日，够 roc_60 的窗口
        for field in ("high", "low", "open"):
            frame = panel._frames[field].copy()
            frame.iloc[:200] = np.nan
            panel._frames[field] = frame.astype("float32")
        panel._frames["close"] = close.astype("float32")

        for name in ("ma_bias_20", "macd_hist_norm", "adx_dir", "roc_60", "rsi_health"):
            out = fl.compute(name, panel)
            assert out.iloc[:200].isna().all().all(), f"{name} 未上市期必须全 NaN"
            assert out.iloc[300:].notna().any().any(), f"{name} 上市后必须有值"


class TestNamesAndLabels:
    def test_names_by_category(self):
        assert "ma_bias_20" in fl.names_by_category(TREND)
        assert "roc_20" in fl.names_by_category(MOMENTUM)
        assert "mfi_14" in fl.names_by_category(VOLUME)
        assert "hv_60" in fl.names_by_category(RISK)
        # P5 之前这里断言「估值维度为空」；现在两个维度都已实现
        assert "ep" in fl.names_by_category(VALUATION)
        assert "roe_ttm" in fl.names_by_category(QUALITY)
        assert fl.names_by_category("no_such_category") == []

    def test_factor_callable_directly(self):
        panel = make_panel(n_days=100)
        out = fl.FACTORS["roc_20"](panel)
        assert out.shape == panel.shape
