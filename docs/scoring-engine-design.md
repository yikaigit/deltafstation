# 打分引擎设计（系统核心）

> 目标：把「每日打分」做成一条**可持续迭代的因子生产线**，而不是一个写死的六维公式。
> 本文定义因子规范、评价标准、版本管理和迭代流程。属于需求/架构层，不含实现代码。
>
> **口径以 `docs/system-spec.md` §5 为准**；本文是其展开说明与设计动机。若两处冲突，以规格为准。

---

## 1. 设计原则

1. **打分是产品，回测是质检**。回测的作用不是"证明策略能赚钱"，而是回答"这一版打分比上一版好还是坏"。
2. **一切可复现**。任何一天的分，都能用「因子集 + 权重 + 数据快照」重算出来。
3. **先横截面，后时序**。选股需要的是"同一天里谁比谁好"，不是"这只股票绝对值高不高"。
4. **因子可插拔**。加一个因子只写一个函数 + 一条元数据，不改流水线。
5. **评价先于合成**。没通过单因子检验的因子，不进合成。

---

## 2. 数据底座（打分的前置条件）

| 表 | 字段 | 粒度 | 来源 |
|---|---|---|---|
| `universe` | code, name, board, industry_l1, industry_l2, list_date, delist_date, is_st | 每标的 | akshare / 东财 |
| `bars` | date, code, open, high, low, close, volume, amount | 日 × 标的 | baostock（不复权） |
| `adj_factor` | code, ex_date, back_factor, fore_factor | 事件 | baostock |
| `valuation` | date, code, pe_ttm, pb_mrq, ps_ttm, peg, total_mv, float_mv, turnover | 日 × 标的 | 东财横截面 |
| `fundamental` | code, report_date, **notice_date**, roe, rev_yoy, profit_yoy, gross_margin, div_yield, ocf | 季 × 标的 | 东财 |
| `calendar` | date, is_trading_day | 日 | baostock |
| `benchmark` | date, code, close | 日 | baostock（sh.000300） |

三条硬约束：

- **价格统一后复权**（`close × back_factor`），打分与回测同源，绝不用前复权。
- **财务必须按 `notice_date` 对齐**，没有公告日就按「报告期 + 90 天（年报 +120 天）」保守滞后。
- **行业分类用东财 `BOARD_NAME`**（实测覆盖全 A，如"半导体""银行Ⅱ""保险Ⅱ"），一级行业用于中性化。

---

## 3. 因子规范

### 3.1 接口

```python
def factor_roe_ttm(bars, ctx) -> pd.DataFrame:
    """ROE(TTM)。返回 index=交易日, columns=股票代码 的矩阵。"""
    return ctx.fundamental["roe_ttm"]          # ctx 已按公告日展开为日频
```

约定：

- 输入是**面板**（date × code），输出同形状；
- 只允许使用 `ctx` 中「截至当日可见」的数据（`ctx` 内部已做时点截断）；
- 不允许在因子函数里读文件、发网络请求；
- 返回 float32，缺失保持 NaN（由标准化层处理）。

### 3.2 元数据

```yaml
- name: roe_ttm
  category: quality          # value/growth/quality/momentum/risk/liquidity/sentiment
  direction: 1               # 1 = 越大越好, -1 = 越小越好
  lookback: 250              # 需要的历史长度（交易日）
  needs: [fundamental]       # 依赖的数据表
  neutralize: [industry, size]   # 需要做的中性化
  description: 滚动 12 个月加权 ROE
  source: 中信《质量因子手册》2023
  version: 1
```

### 3.3 七大类因子清单

对齐中金/中信研报的常见分类，v1 只实现标 ★ 的（覆盖参考项目六维）。

| 类别 | 因子 | v1 |
|---|---|---|
| **价值** | EP(1/PE)、BP(1/PB)、SP(1/PS)、CFP、股息率、PEG | ★ EP/BP/股息率 |
| **成长** | 营收同比、净利同比、ROE 变化、单季环比加速、分析师预期上调 | ★ 营收/净利同比 |
| **质量** | ROE、ROIC、毛利率、经营现金流/净利、应计利润、资产周转 | ★ ROE/毛利率/现金流质量 |
| **动量** | 20/60/120/250 日收益、相对沪深300 强弱、行业动量、动量反转(1 月) | ★ 20/60/120 日 + 相对强弱 |
| **波动风险** | 60 日波动率、下行波动、ATR%、1 年最大回撤、beta、特质波动率 | ★ 全部 |
| **流动性** | 换手率、Amihud 非流动性、成交额分位、量比 | ★ 换手/量比 |
| **情绪资金** | OBV、MFI、北向持股变化、龙虎榜、股东户数变化、业绩预告类型 | ★ OBV/MFI（其余 P3） |

> 参考项目 `analyst_brain.py` 里的具体指标与阈值（SMA20/60/120/250、MACD、ADX、ROC20/60/120、RSI14、OBV、MFI14、ATR%、hv60、dd_1y 等）可以直接作为 v1 的因子实现来源。

---

## 4. 标准化与中性化

顺序固定，不可调换：

```
1. 去极值    median ± 5 × MAD 截断（比 3σ 稳健，A 股极端值多）
2. 缺失处理  行业内中位数填充；行业内有效样本 < 5 则填 0
3. 行业中性  行业内做 rank → pct → 映射到 [-1, 1]（等价于行业内排序，天然可比）
4. 市值中性  对 log(总市值) 做横截面回归，取残差（可选，成长类因子必做）
5. 标准化    z-score（供 IC 计算）或保留 pct（供合成）
```

**为什么行业中性是必须的**：银行 PE 永远低于半导体，不做行业中性，"估值低"会变成"买银行"。

---

## 5. 合成

### 5.1 三层合成

```
因子 → 维度分（维度内等权，或按滚动 IC_IR 加权）
     → 综合分（维度间权重，YAML 可配）
     → 横截面百分位（0~100）
     → 五档：强烈看多 / 看多 / 中性 / 看空 / 强烈看空
```

### 5.2 权重配置

`config/scoring/v1.0.0.yaml`（技术四维，先跑通）：

```yaml
version: v1.0.0
dimensions:
  trend:    {weight: 0.35, factors: [ma_bias_20, ma_bias_60, ma_bias_120, macd_hist_norm, adx_dir, slope_60]}
  momentum: {weight: 0.28, factors: [roc_20, roc_60, roc_120, rs_60, rsi_health]}
  volume:   {weight: 0.22, factors: [vol_ratio, obv_z, mfi_14, turn_rank]}
  risk:     {weight: 0.15, factors: [atr_pct_rank, hv_60, downside_vol, dd_1y]}
```

`config/scoring/v2.0.0.yaml`（六维完整版）：

```yaml
version: v2.0.0
dimensions:
  trend:     {weight: 0.24, factors: [ma_bias_20, ma_bias_60, ma_bias_120, macd_hist_norm, adx_dir, slope_60]}
  momentum:  {weight: 0.18, factors: [roc_20, roc_60, roc_120, rs_60, rsi_health]}
  volume:    {weight: 0.14, factors: [vol_ratio, obv_z, mfi_14, turn_rank]}
  risk:      {weight: 0.10, factors: [atr_pct_rank, hv_60, downside_vol, dd_1y]}
  valuation: {weight: 0.18, factors: [ep, bp, sp, div_yield, peg]}
  quality:   {weight: 0.16, factors: [roe_ttm, roe_slope, rev_yoy, profit_yoy, gross_margin, ocf_to_profit]}
```

> 权重取自参考项目的 `W_TECH` / `W_FULL`，改为 YAML 后每次调权重就是一次可记录的实验。

### 5.3 绝对分 vs 百分位

- **百分位**（`score_pct`）：当日全池排名，选股用这个 —— 普涨行情下也能区分优劣；
- **绝对分**（`score_abs`）：0~100 的刻度，展示用 —— 能表达"今天整个市场都不太行"。

两者都落盘，前端主看百分位。

---

## 6. 评价体系（决定能否迭代）

### 6.1 单因子检验

| 指标 | 定义 | 通过线（A 股经验值） |
|---|---|---|
| RankIC | 因子值与未来 20 日收益的 Spearman 相关，逐日计算 | \|均值\| ≥ 0.03 |
| ICIR | mean(RankIC) / std(RankIC) | ≥ 0.3 |
| IC 胜率 | RankIC > 0 的天数占比 | ≥ 55% |
| 分层单调性 | 5 分组年化收益与组序的 Spearman | ≥ 0.6 |
| 多空收益 | Q5 − Q1 年化 | > 8% |
| 换手率 | 每 20 日成分变动比例 | < 60%（否则成本吃光） |
| 衰减 | 持有 1/5/10/20/60 日的 IC 曲线 | 20 日内不衰减到 0 |

### 6.2 因子相关性

计算候选因子两两的 RankIC 相关矩阵，`|corr| > 0.7` 视为重复，保留 ICIR 更高的那个。合成前必须过这一步，否则等于给同一件事重复加权。

### 6.3 打分整体评价

- **分层回测**：按综合分五档等权持有 20 日，看单调性；
- **组合回测**：TopN + 定期调仓 + 大盘择时，看年化/夏普/回撤/超额；
- **样本外**：滚动 70/30 切分（沿用参考项目口径），报告 OOS 表现；
- **稳定性**：分年度、分行业看超额，避免"只在某一年的某一行业有效"。

### 6.4 判定标准（写死，防止自欺）

一个因子/一版打分要"上线"，必须同时满足：

1. 全区间 RankIC 均值 ≥ 0.03 且 ICIR ≥ 0.3；
2. 分层单调（Q1→Q5 年化递增）；
3. OOS 期超额为正；
4. 与现有因子的最大相关性 < 0.7。

不满足就留在"实验区"，不进生产。

---

## 7. 版本化与实验管理

三张表（SQLite 足够）：

```sql
-- 打分版本
score_versions(
  version TEXT PRIMARY KEY,      -- v1.0.0
  created_at TEXT,
  config_yaml TEXT,              -- 完整权重配置
  factor_set TEXT,               -- 因子清单 JSON
  data_snapshot TEXT,            -- 数据快照标识（如 2026-10-18）
  git_commit TEXT,
  note TEXT
);

-- 每日打分快照（核心表，永久保留）
score_daily(
  trade_date TEXT, code TEXT, version TEXT,
  score_pct REAL, score_abs REAL, grade TEXT,
  dims TEXT,                     -- {"trend":82.1,"momentum":...}
  PRIMARY KEY (trade_date, code, version)
);

-- 实验记录
experiments(
  id TEXT PRIMARY KEY, name TEXT, hypothesis TEXT,
  factor_set TEXT, weights TEXT,
  start_date TEXT, end_date TEXT,
  metrics TEXT,                  -- IC/ICIR/分层/回测指标
  created_at TEXT
);
```

**为什么必须版本化**：没有它，三个月后你无法回答"上一版为什么更好"、"这次改动是因子贡献还是行情贡献"。

---

## 8. 迭代 SOP

```
① 读研报/想法 → 写假设（一句话：什么信号能预测什么期限的收益）
② 实现因子（20~50 行，注册元数据）
③ 单因子检验（IC / 分层 / 换手 / 衰减）
④ 相关性去重（与现有因子比）
⑤ 进候选池 → 调权重 → 合成新版本
⑥ 组合回测（IS/OOS，含成本）
⑦ 通过判定标准 → 发布 score_version
⑧ 每日运行，跟踪 OOS 表现
⑨ 季度复盘：淘汰失效因子、加权有效因子
```

第 ⑨ 步是"不断优化"的真正来源。多数人做到 ⑦ 就停了。

---

## 9. 性能设计（必须提前想清楚）

规模：HS300 300 只 × 4400 天 = 132 万 cell；扩全量后 3200 只 = 1400 万 cell。

| 做法 | 后果 |
|---|---|
| 参考项目的逐行循环 | 300 只 × 4400 行 × 4 函数 ≈ 千万次调用，分钟级甚至更久 |
| 全因子矩阵常驻内存 | 50 因子 × 1400 万 cell × float64 ≈ 5.6 GB，扩全量必爆 |

**方案**：

1. **全部向量化**：指标用 `talib.XXX(panel)` 或 pandas rolling，一次算完一个面板；
2. **float32**：内存减半；
3. **按维度分组计算**，算完立刻聚合成分维度分，**不保留全量因子矩阵**；
4. **只落盘最终结果**：`score_daily`（date × code）才是产物，因子矩阵是中间态，按需重算；
5. 全量重算一次的目标耗时：**HS300 < 30 秒，主板全量 < 3 分钟**。

---

## 10. 分期落地

| 阶段 | 内容 | 验收 |
|---|---|---|
| **S1** | 因子框架 + 技术四维（趋势/动量/量能/风险）+ 每日快照 | 每日 15:35 出分，HS300 全历史可重算，< 30 秒 |
| **S2** | 评价体系（IC/分层/相关性）+ 实验记录 | 能出一张"每个因子的 IC/ICIR/换手"表 |
| **S3** | 估值/质量维（补公告日）+ 六维完整版 | 六维分可回溯，财务无前视 |
| **S4** | 因子实验室前端 + 版本对比 | 能对比 v1.0 与 v1.1 的 IC 与回测差异 |
| **S5** | 扩主板全量 + 2008 起回填 | 全量打分 < 3 分钟 |

---

## 11. 一句话提醒

研报因子的价值不在"因子本身有多神秘"，而在**评价体系能不能诚实地告诉你它没用**。中金/中信公开的因子大多是价量 + 财务的线性组合，网上能抄到几百个；真正拉开差距的是：行业中性做没做、公告日对齐没对齐、样本外有没有偷偷调参、失效因子敢不敢下架。
