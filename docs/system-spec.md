# DeltaFStation 系统规格（v1.0 · 待评审）

> 本文是后续实现的唯一依据。评审通过后按 §10 的分阶段计划开工。
> 前序文档：`docs/gap-analysis-and-roadmap.md`（改造评估）、`docs/scoring-engine-design.md`（因子体系细节）。

---

## 1. 决策记录（本轮已确认）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 股票范围 | **沪深300 全成分，含创业板/科创板**；不再叠加主板过滤（HS300 已足够收窄范围） |
| D2 | 财务时点 | **按公告日 `NOTICE_DATE` 对齐**，不做 90 天滞后（已验证东财接口有该字段） |
| D3 | 综合分 | **横截面百分位**为主，绝对分保留作展示 |
| D4 | 落地节奏 | 先技术四维跑通（v1），再接估值/质量（v2） |
| D5 | 前端 | 4 页：首页 / 数据管理 / 每日打分 / 回测中心 |
| D6 | 移除模块 | 模拟交易、miniQMT 实盘、策略运行、AI Agent、MCP 侧车 |
| D7 | 数据库 | 不引入数据库服务器；Parquet + DuckDB + SQLite（元数据） |
| D8 | 回测引擎 | 自研，不引入 vectorbt/numba（与根项目 pandas 2.x 冲突） |
| D9 | 复权 | 落盘不复权 + 事件表；回测用后复权；展示用前复权 |
| D10 | 依赖 | 移除 deltafq，改为自有数据适配层与策略接口 |

---

## 2. 系统目标与边界

### 2.1 要做的四件事

1. **本地数据仓**：沪深300 全成分（约 300 只）日线，2008-01-01 起，含复权因子、估值、财务、行业、交易日历。
2. **每日更新**：每个交易日收盘后自动增量更新 → 校验 → 打分 → 快照，幂等可重跑。
3. **策略回测**：基于打分构建组合，支持参数化策略与自定义策略，输出完整绩效与 IS/OOS。
4. **每日打分**：全池六维（v1 四维）打分，落盘可回溯，带版本与因子评价。

### 2.2 明确不做

- 不做实盘/模拟交易、不做下单接口、不做 AI 对话、不做 MCP。
- 不做分钟级/tick 数据、不做期货/期权/港股/美股。
- 不做多用户与权限体系（单机单用户）。

---

## 3. 总体架构

```
┌──────────────────────────────────────────────────────────────┐
│ 前端（Jinja2 + 原生 JS + ECharts）                             │
│ 首页 │ 数据管理 │ 每日打分 │ 回测中心                          │
└───────────────────────┬──────────────────────────────────────┘
                        │ REST /api/*
┌───────────────────────▼──────────────────────────────────────┐
│ 服务层 Flask                                                  │
│ data_api │ score_api │ backtest_api │ job_api                 │
└───────────────────────┬──────────────────────────────────────┘
┌───────────────────────▼──────────────────────────────────────┐
│ 核心层                                                        │
│ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │
│ │ 数据管道  │ │ 打分引擎  │ │ 回测引擎  │ │ 任务调度          │  │
│ │ providers│ │ factors  │ │ portfolio│ │ scheduler/jobs   │  │
│ │ lake     │ │ scoring  │ │ metrics  │ │                  │  │
│ │ universe │ │ evaluation│ │ optimizer│ │                  │  │
│ │ validator│ │          │ │          │ │                  │  │
│ └──────────┘ └──────────┘ └──────────┘ └──────────────────┘  │
└───────────────────────┬──────────────────────────────────────┘
┌───────────────────────▼──────────────────────────────────────┐
│ 存储层                                                        │
│ Parquet（行情/估值/财务）│ SQLite（任务/版本/实验）│ JSON（回测结果）│
└──────────────────────────────────────────────────────────────┘
```

数据流：`数据源 → 校验 → Parquet → 指标面板 → 因子 → 打分快照 → 回测/展示`

---

## 4. 数据层设计

### 4.1 数据源分工（均已实测验证）

| 数据 | 主源 | 接口 | 实测结论 |
|---|---|---|---|
| 日线 OHLCV | baostock | `query_history_k_data_plus`，`adjustflag=3`（不复权） | 字段含 `preclose, turn, tradestatus, pctChg, isST`，可直接判停牌与 ST |
| 复权因子 | baostock | `query_adjust_factor` | 返回 `dividOperateDate / foreAdjustFactor / backAdjustFactor` |
| 交易日历 | baostock | `query_trade_dates` | 已验证可用 |
| HS300 成分 | baostock | `query_hs300_stocks` | 返回 `updateDate, code, code_name`，**只有当前成分**（见 §4.7） |
| 标的元信息 | baostock | `query_stock_basic` | `ipoDate, outDate, type, status` |
| 估值（当日/历史） | 东财 | `RPT_VALUEANALYSIS_DET` | 可按 `TRADE_DATE` 过滤；2020-01-02 返回 3556 只；含 `BOARD_NAME` 行业 |
| 财务（含公告日） | 东财 | `RPT_LICO_FN_CPD` | **含 `NOTICE_DATE`**（茅台 2026H1 报告 → 08-15 公告） |

> 说明：参考项目声称用 akshare，实际未 import。本项目以 baostock + 东财直连为主，akshare 仅作为行业分类的兜底（可选依赖）。

**代码格式统一**：内部一律 `sh.600000` / `sz.000001`（baostock 原生），东财的 `600000` 由 `to_em_code()/to_bs_code()` 互转。

### 4.2 存储布局

```
data/
├── lake/
│   ├── universe.parquet                 # 标的元信息（单文件，全量覆盖）
│   ├── calendar.parquet                 # 交易日历
│   ├── adj_factor.parquet               # 复权事件表（全量，几十万行）
│   ├── index_members/
│   │   └── hs300_20261012.parquet       # 成分股快照（按 updateDate 存）
│   ├── bars/year=2024/part.parquet      # 不复权日线
│   ├── valuation/year=2024/part.parquet # 估值（PE/PB/PS/PEG/市值/行业）
│   └── fundamental.parquet              # 财务（季频，含 notice_date）
├── snapshots/
│   └── score/version=v1.0.0/trade_date=2026-10-16/part.parquet
├── results/
│   └── backtests/bt_20261018_1030_ab12.json
└── meta.db                              # SQLite：job_runs / score_versions / experiments
```

### 4.3 表结构

**universe**（1 行/标的）
| 字段 | 类型 | 说明 |
|---|---|---|
| code | str | `sh.600000` |
| name | str | 名称 |
| board | str | main / gem(创业板) / star(科创板) |
| industry_l1 | str | 东财行业（如"银行Ⅱ"） |
| list_date | date | 上市日 |
| delist_date | date | 退市日（空=在市） |
| is_st | bool | 当前 ST 标记 |
| in_hs300 | bool | 是否当前成分 |
| updated_at | datetime | |

**bars**（分区 year）
| date | code | open | high | low | close | preclose | volume | amount | turn | tradestatus | is_st |
|---|---|---|---|---|---|---|---|---|---|---|---|

- 全部**不复权**；`tradestatus=0` 表示停牌；`volume` 单位股，`amount` 元。
- 主键 `(date, code)`。

**adj_factor**
| code | ex_date | fore_factor | back_factor |
|---|---|---|---|

**valuation**（分区 year）
| date | code | close | pe_ttm | pb_mrq | ps_ttm | peg | total_mv | float_mv | industry |
|---|---|---|---|---|---|---|---|---|---|

**fundamental**
| code | report_date | **notice_date** | roe_wa | eps | bps | rev | rev_yoy | net_profit | profit_yoy | gross_margin | ocf_ps | div_yield |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

- 主键 `(code, report_date)`；`notice_date` 缺失时回退为「报告期 + 法定披露截止日」（Q1/Q3 +30 天、H1 +60 天、年报 +90 天），并打 `notice_estimated=true`。

**score_daily**（分区 version + trade_date）
| trade_date | code | version | score_pct | score_abs | grade | dim_trend | dim_momentum | dim_volume | dim_risk | dim_valuation | dim_quality | data_quality |
|---|---|---|---|---|---|---|---|---|---|---|---|---|

- `score_pct` 0~100 横截面百分位；`dim_*` 为各维度 0~100；`data_quality` 记录缺失因子占比。

**index_members**
| snapshot_date | index_code | code |
|---|---|---|

### 4.4 复权方案

- 落盘：不复权 OHLCV + `adj_factor`。
- 计算：`后复权价 = 不复权价 × back_factor(截至当日)`，`factor(d) = 最近一条 ex_date ≤ d 的 back_factor`，无事件为 1.0。
- 前复权：`后复权 / 最新 back_factor`，**仅用于图表**。
- 涨跌停、停牌判定**必须用不复权价 + `preclose`**，不能用复权价。
- 因子表按快照版本存档（`adj_factor_YYYYMMDD.parquet`），因为 baostock 会重算历史因子。

### 4.5 每日更新流程（15:35 触发，可配置）

| 步 | 任务 | 幂等键 | 失败策略 |
|---|---|---|---|
| 1 | 判断交易日（`calendar`） | trade_date | 非交易日直接退出 |
| 2 | 更新 HS300 成分（仅当 `updateDate` 变化） | snapshot_date | 失败保留旧快照并告警 |
| 3 | 更新 universe（月度） | month | 失败沿用旧值 |
| 4 | 增量拉日线（每只从 `max(date)+1`） | (trade_date, code) | 单只重试 3 次，失败记入 `failed[]`，继续 |
| 5 | 更新复权事件（有新事件才写） | (trade_date, code) | 同上 |
| 6 | 拉当日全市场估值横截面 | trade_date | 失败重试；仍失败则打分降级（估值维 NaN） |
| 7 | 更新财务（仅财报季，按公告日） | (code, report_date) | 失败保留旧值 |
| 8 | 数据校验（L1/L2） | trade_date | 产出 `quality_report.json` |
| 9 | 计算打分并落快照 | (trade_date, version) | 失败中止并告警 |
| 10 | 写 `job_runs` + 生成日报 | job_id | — |

**时序说明**：baostock 当日日线通常在收盘后 1~2 小时可用。默认 18:00 执行；若检测到当日数据缺失，每 30 分钟重试一次，至 22:00 放弃并标记 `pending`。

**幂等**：所有写入按主键 upsert（先读分区 → 合并去重 → 原子替换文件）。重跑同一天不产生重复行，也不覆盖历史。

### 4.6 数据校验（沿用根项目规则，接入调度）

- L1 结构：列名、日期可解析、无重复、严格递增、无 NaN。
- L2 自洽：`High ≥ max(O,C)`、`Low ≤ min(O,C)`、价格为正、成交量非负、停牌填充行识别、涨跌幅用后复权价判定（硬阈值 35%、板块阈值 WARN）、日期间隔 > 5 天告警、因子单调性告警。
- 输出：`data/quality/report_YYYYMMDD.json`，含每只标的的 `ok/error_count/warn_count`。
- **门禁**：ERROR 比例 > 2% 时，打分任务标记 `degraded` 并在前端提示。

### 4.7 已知数据限制（必须在 UI 与报告中披露）

1. **HS300 历史成分不可免费获取**。baostock 只返回当前成分。用当前成分回测 2008 年起的区间存在**幸存者偏差**（只包含活到今天的赢家）。
   - 缓解：从上线日起每月存成分快照；回测报告显式标注 `survivorship_bias=true`；P4 再考虑用「每日市值前 300」近似重构历史池。
2. **基准是价格指数**（沪深300 不含分红），而策略用后复权（含分红再投），策略天然有约 1.5%/年的口径优势。报告中注明；若后续拿到全收益指数（H00300）则切换。
3. 东财估值在 2008~2010 覆盖不全，早期估值维缺失较多，`data_quality` 会体现。
4. baostock 会重算历史复权因子，跨版本回测结果可能有小幅差异 → 结果绑定因子快照版本。

---

## 5. 打分引擎

### 5.1 因子规范

```python
def factor_ma_bias_20(panel: Panels) -> pd.DataFrame:
    """收盘价相对 20 日均线的偏离。index=date, columns=code"""
    return panel.close / panel.close.rolling(20).mean() - 1
```

元数据（`factors/registry.py` 注册）：

```python
Factor(
    name="ma_bias_20", category="trend", direction=1, lookback=20,
    needs=["bars"], neutralize=["industry"], description="20 日均线乖离率",
    source="参考项目 analyst_brain.score_trend", version=1,
)
```

- `direction=1` 越大越好，`-1` 越小越好（标准化时取反）。
- 因子只读 `Panels`（已含后复权价、估值、财务、行业），不允许 IO。
- 输出 float32 面板，缺失为 NaN。

### 5.2 v1 因子清单（技术四维，14 个）

| 维度 | 因子 | 公式 | 方向 |
|---|---|---|---|
| 趋势 | `ma_bias_20/60/120` | `close / SMA(n) - 1` | 1 |
| 趋势 | `macd_hist_norm` | `MACD(12,26,9).hist / close` | 1 |
| 趋势 | `adx_dir` | `sign(PDI-MDI) × min(ADX/40,1)` | 1 |
| 趋势 | `slope_60` | `LINEARREG_SLOPE(ln(close),60) × 250` | 1 |
| 动量 | `roc_20/60/120` | `close / close.shift(n) - 1` | 1 |
| 动量 | `rs_60` | `Σ(r_i - r_bench)` 近 60 日 | 1 |
| 动量 | `rsi_health` | RSI14 分段映射（45~62 最优，>78 扣分） | 1 |
| 量能 | `vol_ratio` | `SMA(vol,5) / SMA(vol,60)` | 1 |
| 量能 | `obv_z` | `(OBV - MA60(OBV)) / STD60(OBV)` | 1 |
| 量能 | `mfi_14` | MFI(14) | 1 |
| 量能 | `turn_rank` | 换手率的 250 日滚动分位 | 1 |
| 风险 | `atr_pct_rank` | `ATR14/close` 的 250 日滚动分位 | -1 |
| 风险 | `hv_60` | 60 日年化波动率 | -1 |
| 风险 | `downside_vol` | 60 日下行波动率 | -1 |
| 风险 | `dd_1y` | `close / rolling_max(250) - 1` | 1（越接近 0 越好） |

维度权重（沿用参考项目 `W_TECH`）：趋势 0.35 / 动量 0.28 / 量能 0.22 / 风险 0.15。

### 5.3 v2 因子清单（估值 / 质量）

| 维度 | 因子 | 公式 | 方向 |
|---|---|---|---|
| 估值 | `ep` | `1 / PE_TTM` | 1 |
| 估值 | `bp` | `1 / PB_MRQ` | 1 |
| 估值 | `sp` | `1 / PS_TTM` | 1 |
| 估值 | `div_yield` | 股息率 | 1 |
| 估值 | `peg` | PEG | -1 |
| 质量 | `roe_ttm` | 加权 ROE（TTM） | 1 |
| 质量 | `roe_slope` | 近 6 期 ROE 线性斜率 | 1 |
| 质量 | `rev_yoy` / `profit_yoy` | 营收/净利同比 | 1 |
| 质量 | `gross_margin` | 毛利率 | 1 |
| 质量 | `ocf_to_profit` | 经营现金流 / 净利润 | 1 |

- **时点**：财务因子按 `notice_date` 展开为日频（`asof` 对齐），当天只能看到已公告的最新一期。
- 权重（沿用 `W_FULL`）：趋势 0.24 / 动量 0.18 / 量能 0.14 / 风险 0.10 / 估值 0.18 / 质量 0.16。

### 5.4 标准化

对每个因子、每个交易日做横截面处理：

```
① 去极值    median ± 5×MAD 截断
② 行业中性  行业内 rank → pct；行业内样本 < 5 时退回全市场分位
③ 方向统一  direction=-1 时取 1-pct
④ 维度分   维度内因子 pct 等权平均 × 100
⑤ 综合分   维度分加权求和 → 再在横截面上做一次 rank → score_pct(0~100)
```

- 市值中性：v1 不做（历史股本数据不稳），v2 作为可选开关。
- `score_abs` = 维度分加权和（0~100 绝对刻度，仅展示）。

### 5.5 分档

按 `score_pct` 固定比例分档（保证每天分布稳定）：

| 百分位 | 评级 |
|---|---|
| ≥ 90 | 强烈看多 |
| 70 ~ 90 | 看多 |
| 30 ~ 70 | 中性 |
| 10 ~ 30 | 看空 |
| < 10 | 强烈看空 |

### 5.6 版本与快照

- 版本号 `v{major}.{minor}.{patch}`，配置存 `config/scoring/v1.0.0.yaml`（权重 + 因子清单）。
- 每次打分写入 `score_daily`，同时 `score_versions` 记录 `config_yaml / factor_set / data_snapshot / created_at / note`。
- 历史分可随时用旧版本重算（因子代码不变时结果可复现）。

### 5.7 评价体系

| 指标 | 定义 | 通过线 |
|---|---|---|
| RankIC | 因子值与未来 20 日收益的 Spearman，逐日算 | \|均值\| ≥ 0.03 |
| ICIR | mean / std | ≥ 0.3 |
| IC 胜率 | RankIC > 0 占比 | ≥ 55% |
| 分层单调性 | 5 分组年化与组序 Spearman | ≥ 0.6 |
| 多空收益 | Q5 − Q1 年化 | > 8% |
| 换手率 | 每 20 日成分变动 | < 60% |
| 相关性 | 因子间 RankIC 相关 | 与现有因子 < 0.7 |

- 输出 `data/reports/factor_eval_{version}.csv`（每因子一行）+ 相关性矩阵。
- **门禁**：未过线的因子不进入合成（可在实验区单独查看）。

### 5.8 性能目标

| 场景 | 目标 |
|---|---|
| HS300 全历史（300×4400）单版本打分 | < 30 秒 |
| 单日增量打分 | < 3 秒 |
| 主板全量（3200 只）全历史 | < 3 分钟 |
| 内存峰值 | < 2 GB |

实现约束：全向量化、float32、按维度分组计算后即丢弃因子矩阵、只落盘最终快照。

---

## 6. 回测引擎

### 6.1 策略模型

**类型 A：`score_portfolio`（内置，参数化，v1 实现）**

| 参数 | 默认 | 说明 |
|---|---|---|
| score_version | 最新 | 使用哪版打分 |
| top_n | 20 | 持仓只数 |
| rebal_days | 20 | 调仓间隔（交易日） |
| weight_method | equal | equal / score / hrp(v2) |
| regime | true | 是否启用大盘择时 |
| min_score_pct | 0 | 只买百分位 ≥ 该值的股票 |
| max_weight | 0.10 | 单票上限 |
| exclude_st | true | 排除 ST |
| initial_cash | 1,000,000 | |
| commission / stamp / slippage | 见 6.2 | |

**类型 B：`custom`（自定义，v2 可选）**

用户实现 `generate_target_weights(panels, date) -> dict[code, weight]`，引擎只负责执行与结算。

### 6.2 执行假设（A 股制度，保守口径）

| 项 | 规则 |
|---|---|
| 信号时点 | T 日收盘后计算目标权重 |
| 成交时点 | **T+1 开盘价** |
| 停牌 | `tradestatus=0` 或 `volume=0` → 不可买不可卖 |
| 涨停 | T+1 开盘价 ≥ `preclose × (1+limit) - 0.002` → 放弃买入，资金留存 |
| 跌停 | T+1 开盘价 ≤ `preclose × (1-limit) + 0.002` → 卖不出，持仓顺延 |
| 涨跌停幅度 | 主板 10%；创业板 2020-08-24 前 10%、之后 20%；科创板 20% |
| 最小交易单位 | 100 股（可关闭） |
| 佣金 | 双边 0.025%，最低 5 元 |
| 印花税 | 卖出 0.05% |
| 滑点 | 双边 0.05% |
| 单轮成本 | ≈ 0.20%（保守） |
| 现金 | 未成交资金留存，不计息 |

### 6.3 组合与权重

- 目标权重按 `weight_method` 计算；
- 卖出先于买入结算（先释放现金再买）；
- 权重按 T+1 开盘价折算股数并取整到 100 股；
- 若总资金不足，按权重比例等比缩减；
- 单票权重上限 `max_weight` 生效后，超出部分按比例分给其余标的。

### 6.4 绩效指标

总收益、年化收益、年化波动、Sharpe、Sortino、最大回撤、Calmar、月度胜率、超额收益、信息比率、换手率、交易笔数、被阻挡笔数（涨停/跌停/停牌）、平均持仓数。

### 6.5 IS / OOS

- 按交易日索引滚动切分 70/30（沿用参考项目口径），切分点随数据更新前移。
- 参数网格在 IS 上按 Sharpe 寻优，最优参数汇报 OOS。
- 自洽校验：`(1+IS累计)×(1+OOS累计)-1 == 全区间累计`，偏差 > 0.01pp 报错。

### 6.6 参数网格

`top_n ∈ {5,10,20,30}` × `rebal_days ∈ {5,10,20,60}` × `regime ∈ {0,1}` = 32 组。

### 6.7 输出结构（JSON）

```json
{
  "id": "bt_20261018_1030_ab12",
  "created_at": "...",
  "params": {...},
  "period": {"is_start":"...","is_end":"...","oos_start":"...","oos_end":"..."},
  "equity": [{"date":"...","nav":1.0,"bench":1.0,"drawdown":0.0}],
  "metrics": {"is":{...},"oos":{...},"full":{...}},
  "trades": [{"date":"...","code":"...","side":"buy","price":0,"shares":0,"amount":0,"fee":0,"reason":"rebalance"}],
  "holdings": [{"date":"...","code":"...","weight":0.05}],
  "blocked": {"buy":0,"sell":0,"suspended":0},
  "grid": [{"top_n":20,"rebal_days":20,"regime":true,"is_sharpe":1.2,"oos_sharpe":0.8}],
  "warnings": ["survivorship_bias=true"]
}
```

净值曲线按周降采样（约 1000 点上限），交易明细全量。

---

## 7. API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 + 数据最新日期 |
| GET | `/api/universe` | 标的列表（含行业/板块/是否成分） |
| GET | `/api/data/status` | 每标的覆盖情况（首末日、行数、缺失、校验状态） |
| POST | `/api/jobs/update` | 触发增量更新，返回 `job_id` |
| GET | `/api/jobs/{id}` | 任务状态/进度/日志尾部 |
| GET | `/api/jobs` | 最近任务列表 |
| GET | `/api/scores?date=&version=&industry=&grade=&limit=` | 当日榜单（按 score_pct 降序） |
| GET | `/api/scores/{code}?date=` | 个股六维明细 + 近 250 日分数曲线 |
| GET | `/api/scores/industries?date=` | 行业均值/中位数/家数/看多占比 |
| GET | `/api/score-versions` | 版本列表与元数据 |
| GET | `/api/factors?version=` | 因子清单 + 最新 IC 指标 |
| POST | `/api/backtests` | 运行回测（同步，超 60s 转异步） |
| GET | `/api/backtests` | 回测列表 |
| GET | `/api/backtests/{id}` | 回测详情 |
| DELETE | `/api/backtests/{id}` | 删除 |

统一响应：`{"ok":true,"data":...}` / `{"ok":false,"error":{"code":"","message":""}}`。

---

## 8. 前端设计（4 页）

### 8.1 首页
- 数据状态卡：覆盖标的数、最新交易日、最后更新时间、数据质量（ERROR/WARN 数）。
- 今日概览：评级分布柱状图、行业平均分热力条、Top10 / Bottom10 榜。
- 快捷入口：更新数据、查看打分、跑回测。

### 8.2 数据管理
- 标的表：代码/名称/板块/行业/上市日/数据区间/行数/状态（可搜索、按行业筛）。
- 覆盖矩阵：年份 × 是否有数据（热力图）。
- 操作：触发增量更新（带进度条与日志流）、单只补拉、导出校验报告。

### 8.3 每日打分
- 顶部：日期选择、版本选择、行业筛选、评级筛选、搜索。
- 主表：排名 / 代码 / 名称 / 行业 / 综合分 / 百分位 / 评级 / 六维迷你条形图。
- 点击行 → 右侧抽屉：六维雷达图、近 250 日分数曲线、K 线（前复权）、关键指标（PE/PB/ROE/ATR%）。
- 底部：行业热力（行业 × 平均分/看多占比）。

### 8.4 回测中心
- 左栏参数表单（版本/topN/调仓/择时/权重/费用/区间），运行按钮 + 进度。
- 右栏结果：
  - 指标卡（年化/夏普/回撤/超额）
  - 净值曲线（策略 vs 沪深300）+ 回撤阴影
  - 年度收益柱状图
  - IS/OOS 对比表
  - 参数网格热力图（topN × rebal，颜色=OOS Sharpe）
  - 持仓历史（按调仓日）
  - 交易明细表
- 历史回测列表（可对比两条净值曲线）。

---

## 9. 目录结构与依赖

```
backend/
├── app.py
├── api/            data_api.py  score_api.py  backtest_api.py  job_api.py
├── core/
│   ├── config.py            路径与常量
│   ├── providers/           baostock_provider.py  eastmoney_provider.py
│   ├── lake.py              Parquet 读写 + DuckDB 查询
│   ├── universe.py          标的元信息与成分股
│   ├── calendar.py          交易日历
│   ├── adjustment.py        复权因子展开
│   ├── validator.py         数据校验
│   ├── panels.py            面板装载（后复权价/估值/财务 asof）
│   ├── indicators.py        向量化指标
│   ├── factors/             registry.py  trend.py  momentum.py  volume.py  risk.py  value.py  quality.py
│   ├── scoring.py           标准化/合成/快照
│   ├── evaluation.py        IC/分层/相关性
│   ├── backtest/
│   │   ├── engine.py        组合回测
│   │   ├── metrics.py       绩效指标
│   │   └── optimizer.py     equal/score/hrp
│   ├── scheduler.py         任务编排
│   └── jobs.py              任务定义与状态
└── strategies/     base.py  score_portfolio.py
config/
├── app.yaml                  路径/调度/费用
└── scoring/v1.0.0.yaml       权重与因子清单
tests/
├── unit/                     复权/涨跌停/指标/标准化/IC
├── integration/              端到端（合成数据）
└── fixtures/                 小样本数据
```

**requirements.txt（新增/移除后）**

```
Flask, Flask-CORS, pandas>=2.2,<3, numpy>=2,<3, pyarrow, duckdb,
baostock, TA-Lib, PyYAML, requests, scipy, pyportfolioopt
```

移除：`deltafq`、`yfinance`、`openai`、`mcp`、`matplotlib`、`seaborn`、`scikit-learn`。
`akshare` 列为可选（仅行业兜底），不强制安装。

---

## 10. 实施计划

| 阶段 | 内容 | 交付 | 验收标准 | 人日 | 状态 |
|---|---|---|---|---|---|
| **P0 骨架** | 清理交易/实盘/AI/MCP；新目录；配置；4 页空壳 | 可启动的 Web | `python run.py` 打开 4 页无报错 | 2 | ✅ |
| **P1 数据地基** | providers、lake、universe、calendar、adjustment、validator、回填脚本、增量更新 | 完整本地数据仓 | 一键回填 HS300 全历史；重复运行不破坏历史；覆盖报告正确 | 5~6 | ⬜ |
| **P2 打分 v1** | panels、indicators、14 个技术因子、标准化、合成、快照 | 每日打分 | 全历史重算 < 30s；同日两次运行结果完全一致 | 6~7 | ⬜ |
| **P3 回测** | 组合回测引擎、指标、IS/OOS、网格 | 回测中心后端 | 合成数据手工核对净值一致；3 个调仓日人工复核成交 | 5~6 | ⬜ |
| **P4 前端** | 4 页全部交互 | 可用界面 | 页面可触发更新/看榜/跑回测，无控制台错误 | 6~7 | ⬜ |
| **P5 六维 + 评价** | 估值/质量因子（公告日对齐）、IC/分层/相关性 | 完整打分 + 评价 | 六维可回溯；因子 IC 表产出；无前视（抽查 3 期财报） | 5~6 | ⬜ |
| **P6 调度与打磨** | 每日任务链、重试、日报、文档 | 生产可用 | 连续 3 个交易日自动运行成功 | 2~3 | ⬜ |
| | **合计** | | | **31~37** | |

**执行方式**：按 P0→P6 顺序推进，每个阶段结束跑测试并输出一份阶段报告，确认后再进下一阶段。P1 的回填耗时较长（约 20~40 分钟网络请求），放在后台任务执行。

### P0 完成记录（2026-10-18）

| 项 | 结果 |
|---|---|
| 删除 | 交易/实盘/模拟/AI Agent/MCP 相关 22 个 py、6 个旧 API、7 篇过时文档、旧策略与全部旧前端 |
| 新增核心 | `config.py`（YAML+深合并+点号取值）、`lake.py`（Parquet+DuckDB 幂等 upsert）、`meta_db.py`（SQLite 三表）、`snapshots.py`（按日快照）、`validator.py`（由 data_validator 改名） |
| 新增 API | `/api/universe`、`/api/data/status`、`/api/data/quality`、`/api/scores`、`/api/scores/industries`、`/api/scores/<code>`、`/api/score-versions`、`/api/backtests`、`/api/jobs`、`/api/health` |
| 新增前端 | `app.css` 设计系统、`base.html` 骨架、4 页模板 + 5 个 JS 模块 |
| 测试 | 121 个单元测试通过；4 页冒烟（空态/有数据两态）；DOM id 静态一致性；jsdom 运行时冒烟含抽屉交互 |
| 辅助脚本 | `smoke_test.py`、`check_dom_ids.py`、`frontend_smoke.cjs`、`preview_with_sample.py` |

**P0 验收命令**

```bash
.venv/Scripts/python.exe -m pytest -q                  # 121 passed
.venv/Scripts/python.exe scripts/smoke_test.py         # 4 页 + 16 接口
.venv/Scripts/python.exe scripts/check_dom_ids.py      # 模板/JS id 对齐
# 前端运行时（需先起服务）
DELTAF_PORT=5173 .venv/Scripts/python.exe run.py &
NODE_PATH=<node-workspace>/node_modules node scripts/frontend_smoke.cjs http://127.0.0.1:5173
# 带数据断言（合成数据，不写真实 data/）
.venv/Scripts/python.exe scripts/preview_with_sample.py --port 5174 &
NODE_PATH=<node-workspace>/node_modules node scripts/frontend_smoke.cjs http://127.0.0.1:5174 --data
```

---

## 11. 测试策略

| 层级 | 内容 |
|---|---|
| 单元 | 复权因子展开（含除权日当天切换）、涨跌停判定（主板/创业板 2020 前后/科创板）、指标与 talib 对齐、标准化去极值、IC 计算、股数取整 |
| 数据 | 抽 3 只股票与 baostock 官网逐行对账；除权日前后收益率连续（无假跳空）；停牌行被正确识别 |
| 回测 | 构造 5 只股票的合成价格，手工计算 2 个调仓周期的净值，断言误差 < 1e-6；验证 T+1、涨停拒单、跌停顺延、费用 |
| 一致性 | 用参考项目 24 只池、同参数跑一遍，年化/回撤量级差异可解释（差异来自复权口径与股票池） |
| 回归 | 固定数据快照 + 固定版本，产出 baseline 指标，改动后对比，偏差 > 0.5% 需说明 |
| 端到端 | 空库 → 回填 → 打分 → 回测 → 页面展示，全链路脚本化 |

---

## 12. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| HS300 历史成分不可得 | 回测存在幸存者偏差 | 报告显式标注；每月存快照；P4 用市值前 300 近似 |
| baostock 当日数据延迟 | 每日任务取不到当日数据 | 18:00 起每 30 分钟重试至 22:00，标记 pending |
| 东财接口变更/限频 | 估值/财务断供 | provider 隔离 + 重试 + 本地缓存 + 可切 akshare |
| baostock 重算历史因子 | 跨版本回测不可比 | 因子表按快照存档，结果绑定版本 |
| 基准为价格指数 | 超额被高估约 1.5%/年 | 报告注明；后续切全收益指数 |
| 早期估值覆盖不足 | 2010 前估值维大量缺失 | `data_quality` 标注，权重自动再归一 |
| TA-Lib 安装失败 | 指标不可用 | 已确认当前环境可用；文档写明 Windows 安装方式 |

---

## 13. 仍需你确认的 3 个小点

1. **每日任务执行时间**：默认 18:00（baostock 当日数据通常 17:30 后可取），可改。
2. **回测基准**：暂用沪深300 价格指数（不含分红），报告中标注口径差异；是否接受？
3. **P4 之前的幸存者偏差**：初期用当前 HS300 成分回测 2008 年起区间，报告标注 `survivorship_bias=true`；是否接受，还是先只回测近 5 年？
