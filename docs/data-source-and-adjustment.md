# 数据源锁定与复权方案设计

> 状态：**已实施**（2026-10-02）
> 日期：2026-10-02
> 适用范围：`backend/core/data_manager.py`、`backend/core/live_data_manager.py`、回测/策略/模拟引擎、前端数据源选择
>
> 决策记录见第 7 节，实施后的实测发现见第 9 节（含 3 个原方案未预料到的坑）。

---

## 0. 一句话结论

历史数据**只走 baostock**，保存**不复权价 + 除权事件因子表**，运行时按需计算**后复权价（点-in-time 安全）**作为回测主价格序列；落盘带 source 标记，禁止跨源混合写入；配套三层数据校验。

---

## 1. 现状盘点（已实测确认的事实）

| 项 | 事实 | 证据 |
|---|---|---|
| 默认数据源 | `yfinance`（三处硬编码） | `data_manager.py:156`、`backtest.js:648`、`gostrategy.js:70` |
| 数据源白名单 | `{yfinance, miniqmt, baostock}` | `data_manager.py:157` |
| yfinance 可用性 | **IP 级 429 封禁**，与代理无关 | 上一轮实测 curl / yfinance 直连均失败 |
| 跨源降级逻辑 | **不存在**，仅同源自愈 `using_local_on_error` | `data_manager.py:209-219` |
| akshare | 未接入（零引用），仅 deltafq 传递依赖 | 全仓 grep 零命中；`requirements.txt` 无 |
| baostock 网关 | **deltafq 已内置 `BaostockDataGateway`**，注册名 `baostock` | `gateway_registry.py:10` |
| baostock 指数 | `sh.000001` 可拉日线；**指数无复权因子**（返回 0 行） | 本次实测 |
| baostock 分钟线 | 支持 `frequency="5"`，字段 `date + time` | 本次实测，48 根/日 |
| baostock 交易日历 | `query_trade_dates` 可用，返回 `is_trading_day` | 本次实测 |
| baostock 退市股 | 历史可拉（sh.600485 拉到 2019-2020 共 487 行） | 本次实测 |
| **停牌日行为** | **返回填充行：OHLC 四值全等 + Volume=0** | 本次实测 sh.600485 2017 全年 244 行**全部** Volume=0 |
| `end_date` 语义 | **排他**（拉 9-30 需传 end=10-01） | `baostock_bars.py` 内部减一天 |

### 1.1 实测原始输出（关键三则）

```
指数 sh.000001 日线  fields=[date,code,open,high,low,close,volume,amount]  rows=4
['2026-09-30','sh.000001','3839.2527','3851.2169','3833.0863','3842.1946','41456024700',...]

指数复权因子 sh.000001  rows=0        ← 指数不需要复权
5分钟线 sh.600000      rows=48        ← 支持分钟线
sh.600485 2017 全年    rows=244，其中 volume==0 的行数 = 244   ← 停牌填充行
```

⚠️ **停牌填充行是本方案最需要额外处理的一条**：baostock 对停牌日不是"不返回"，而是返回一串**价格恒定、成交量为 0 的假 K 线**。这类行若不清洗，会让回测在停牌期产生假信号、假持仓，且不会报任何错。

---

## 2. 决策一：数据源锁定 baostock，移除 yfinance

### 2.1 原则

- **历史数据（回测/策略/数据页）**：白名单收敛为 `{baostock, miniqmt}`，默认 `baostock`
- **不做跨源降级**：一次拉取失败就是失败，不换源重试（避免同文件内口径混合）
- **akshare 不接入**（本期不做）

### 2.2 改动清单（精确到行）

**后端**

| 文件:行 | 现状 | 改为 |
|---|---|---|
| `data_manager.py:156` | `data_source or "yfinance"` | `or "baostock"` |
| `data_manager.py:157` | `{"yfinance","miniqmt","baostock"}` | `{"baostock","miniqmt"}` |
| `data_manager.py:159` | 映射表含 `yfinance` | 移除该项 |
| `data_api.py:241-247` | source 默认 `yfinance` + yfinance 字典 | 默认 `baostock` |
| `live_data_manager.py:86,95` | 默认 `yfinance` | 默认 `baostock` |
| `live_data_manager.py:97` | `DFS_LIVE_YF_INTERVAL` | `DFS_LIVE_BS_INTERVAL`（默认 60s） |
| `live_data_manager.py:421,425,432` | `SUPPORTED_SOURCES={"yfinance","miniqmt"}` | `{"baostock","miniqmt"}` |
| `simulation_engine.py:89` | `create_data_gateway("yfinance", interval=5.0)` | `("baostock", interval=60.0)` |
| `strategy_engine.py:144,147` | `data_gateway_name="yfinance"` | `"baostock"` |
| `agent/tool_registry.py:43,55,93,100` | 示例 `000001.SS / AAPL / GLD` | 改 A 股代码 `sh.600000` |
| `agent/skills/backtest/SKILL.md:14` | `pass symbol as yfinance ticker` | 改 baostock 代码规范 |

**前端**

| 文件:行 | 现状 | 改为 |
|---|---|---|
| `templates/backtest.html:274-276` | `<option yfinance selected>` | 默认 `baostock`，删除 yfinance 项 |
| `backtest.js:648,828` | `dataSource = 'yfinance'` | `'baostock'` |
| `backtest.js:924` | `\|\| 'yfinance'` | `\|\| 'baostock'` |
| **`backtest.js:929`** | `getBenchmarkNormalizedData('000001.SS', ...)` | `'sh.000001'` ← **易漏** |
| `gostrategy.js:70,86,141` | `? 'miniqmt' : 'yfinance'` | `: 'baostock'` |
| `trader.js:33,290` | `currentDataSource: 'yfinance'` | `'baostock'` |
| `templates/trader.html:72` | YF 按钮 | 改 BS 按钮 |

**数据/依赖**

- `data/symbols_dict_yfinance.json` → 归档（不删，留作对照）
- `data/raw/000001.SS.csv`、`000001.SZ.csv`、`BTC-USD.csv` → 移入 `data/raw/_legacy_yfinance/`（避免 `find_latest_file` 前缀误匹配）
- `requirements.txt`：yfinance 是 deltafq 的传递依赖**无法删除**，加注释说明"本项目不直接使用"

### 2.3 ⚠️ 移除 yfinance 的代价（必须知情）

`BaostockDataGateway` 是**轮询 5 分钟线的网关，不是实时行情**：

- 盘中 baostock 数据大概率不刷新（历史库性质，通常收盘后更新）
- `get_depths` 是**合成假盘口**（用最新 close 铺档 + 随机量）——这点与 yfinance gateway 一样是假的，不算退步
- `_warm_up` 会把**上一交易日**的 5 分钟线逐根回放成 tick

**结论**：paper 交易与模拟盘切换 baostock 网关后，**盘中基本是在回放上一交易日数据**。可以跑通流程，但**不能当作实时行情做决策依据**。UI 上必须显式标注。

---

## 3. 决策二：复权方案

### 3.1 核心认知（决定方案走向）

| 复权类型 | 计算 | 是否有未来函数 |
|---|---|---|
| **后复权** | `close(d) × back_factor(d)` | ✅ **天然安全**，只依赖截至 d 的历史事件 |
| **前复权** | `close(d) × back_factor(d) / back_factor(最新)` | ❌ **天然有未来函数**，分母用到未来事件 |

**所以：回测主价格序列用后复权价，不用前复权价。** 前复权只用于图表展示与跨期视觉对比（展示时用"截至回测结束日"的定点版本）。

### 3.2 因子定义（已实测逐位验证）

baostock `query_adjust_factor` 返回的是**除权日事件表**，不是每日序列：

```
fields = ['code','dividOperateDate','foreAdjustFactor','backAdjustFactor','adjustFactor']
```

展开为每日因子：

```
daily_factor(d) = 最后一条 dividOperateDate <= d 的 backAdjustFactor
                = 1.0   若 d 早于首个除权日
```

⚠️ **除权日当天即启用新因子**（不是次日）。已验证：浦发 2026-07-16

```
不复权 9.31 → 8.85      看似跌 4.9%
后复权 118.833 → 118.298   实际跌 0.45%
反推因子：118.833/9.31 = 12.7637（07-15）  118.298/8.85 = 13.3670（07-16）
13.367013 == 事件表中 2026-07-16 行的 backAdjustFactor  ✓
```

公式：

```
后复权价(d) = 不复权价(d) × daily_factor(d)
前复权价(d) = 不复权价(d) × daily_factor(d) / daily_factor(基准日)      # 仅用于展示
Volume 后复权(d) = Volume(d) / daily_factor(d)                        # Qlib 口径，可选
```

### 3.3 回测引擎用哪个价成交（**需拍板**）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A 后复权全程**（推荐起步） | 信号/成交/估值全部用后复权价 | 收益率正确；点-in-time 天然安全；无需拉分红数据 | 价格量级失真（茅台后复权上万）；涨跌停判断需另用不复权价 |
| B 真实价 + 分红事件 | 不复权价成交，除权日按 `query_dividend_data` 调持仓与现金 | 最精确，现金真实 | 需额外拉分红数据；实现复杂度高 |
| C 混合 | 信号用前复权、成交用真实价、估值用后复权 | 看似全面 | **现金与市值不守恒，记账会乱**，不推荐 |

**推荐 A**，并保留一套不复权价副本用于：涨跌停判断、真实价展示、成交额类指标（换手率、成交额）。后续若要精确建模分红再投资，再升级到 B。

### 3.4 存储设计（向后兼容）

```
data/raw/{symbol}.csv        Date,Open,High,Low,Close,Volume     ← 始终不复权，schema 不变
data/adjust/{symbol}.csv     dividOperateDate,foreAdjustFactor,backAdjustFactor,adjustFactor
                             ← 除权事件表原样存，运行时展开，不落每日序列
data/meta/{symbol}.json      {"source":"baostock","last_fetch":...,"rows":N,
                              "first_date":...,"last_date":...,"schema_version":1}
```

- OHLCV 文件**不加列**，现有代码与 CSV 全部向后兼容
- 因子存**事件表**而非每日序列：5000 只 × 20 年若展开成每日约 5000 行/只 = 2500 万行，存储与 IO 都不划算；事件表通常只有几十行
- `meta/{symbol}.json` 的 `source` 字段是**防止跨源混合写入的关键**

### 3.5 强制约束

`_merge_with_local` 合并本地历史前**必须校验 `source` 一致**：

- 一致 → 正常合并
- 不一致 → **拒绝合并**，日志报 ERROR，要求 `force_refresh=true` 全量重拉
- 无 meta 文件（旧数据）→ 视为 `unknown`，首次合并时补写并记录一条 WARN

---

## 4. 决策三：数据校验规则

分三层，成本与结论强度递增。**L1/L2 不联网可全量跑；L3 必须抽样（全量会打爆 baostock）。**

### L1 结构校验（每文件必跑，秒级）

| 编号 | 规则 | 级别 |
|---|---|---|
| R1.1 | 列名严格为 `Date,Open,High,Low,Close,Volume`（注意 O/H/L/C 顺序） | ERROR |
| R1.2 | `Date` 全部可解析为 ISO 日期 | ERROR |
| R1.3 | `Date` 严格单调递增 | ERROR |
| R1.4 | 无重复 `Date` | ERROR |
| R1.5 | 无 NaN / 空行 | ERROR |
| R1.6 | `Volume` 为整数（未被科学计数法污染） | WARN |

### L2 逻辑自洽（每文件必跑，秒级）

| 编号 | 规则 | 级别 |
|---|---|---|
| R2.1 | `High >= max(O,C)` 且 `Low <= min(O,C)` | ERROR |
| R2.2 | 严格版：`High == max(O,H,L,C)` 且 `Low == min(O,H,L,C)` | ERROR |
| R2.3 | 所有价格 > 0 | ERROR |
| R2.4 | `Volume >= 0` | ERROR |
| R2.5 | `Volume == 0` → 疑似停牌 | WARN |
| **R2.6** | **`O==H==L==C` 且 `Volume==0` → 确认停牌填充行** | **WARN（须清洗）** |
| R2.7 | 涨跌幅超限（**用后复权价算**） | 见 4.1 |
| R2.8 | 相邻日期间隔 > 5 个交易日 | WARN（查漏拉） |
| R2.9 | 复权因子 `backAdjustFactor` 单调不减 | WARN（原定 ERROR，实测后降级，见 9.2） |

**R2.6 是本方案新增的关键规则**，专门抓 baostock 的停牌填充行。

### 4.1 涨跌幅阈值（分两级，避免历史规则变化误报）

| 级别 | 阈值 | 说明 |
|---|---|---|
| **硬阈值** | `\|ret\| > 35%` → ERROR | 任何时期任何板块超过此值一定是数据错误 |
| **软阈值** | 按板块 → WARN，提示人工复核 | 主板 ±10%、创业板/科创板 ±20%、北交所 ±30%、ST ±5% |

软阈值**按日期分段**更准（创业板 2020-08 前是 ±10%、科创板 2019-07 才开市），但首期先用固定值 + WARN 级别，避免误报淹没有效信号。

⚠️ 涨跌幅**必须用后复权价算**。用不复权价算，除权日会出反向假信号（浦发案例：看似 -4.9%，实际 -0.45%）。

### 4.2 停牌行处置（**需拍板**）

| 选项 | 做法 |
|---|---|
| **剔除（推荐）** | 清洗阶段直接删除 `Volume==0` 且 OHLC 全等的行，回测不再看到停牌日 |
| 保留标记 | 保留行并加 `suspended` 列，回测时跳过信号但保留时间轴 |

推荐**剔除**：停牌日本身不产生可交易信号，保留会污染"交易日数""年化"等统计口径。

### L3 外部对账（分层抽样，分钟级）

**不要随机抽样**，按四层抽：

1. 每只股票**最近 30 个交易日**（最可能出问题的是增量部分）
2. **有除权日的股票跑全历史**（复权最容易错的地方）
3. 随机 **1%**
4. **涨跌停等极端行情日**

| 编号 | 规则 | 级别 |
|---|---|---|
| R3.1 | 本地日期集合 ⊆ `query_trade_dates` 的交易日集合 | ERROR |
| R3.2 | baostock 重拉最近 20 日与本地 diff，应逐位相同 | ERROR |
| R3.3 | 后复权价比值 == 对应两日 `backAdjustFactor` 比值 | ERROR |
| R3.4 | 跨源比**不复权** OHLCV（可选，需接入 akshare） | WARN |

⚠️ **跨源对账只能比不复权价**。复权价跨源必然不一致（基准日/因子口径不同），拿它比对会得到"数据错了"的错误结论。

### L4 回归测试（每次改动数据层必跑）

| 编号 | 规则 |
|---|---|
| R4.1 | "买入持有 X"的年化收益率落在预期区间 |
| R4.2 | 已知高分红标的的后复权收益 > 不复权收益（验证因子方向正确） |

L4 是唯一能抓出"数据自洽但语义错误"的手段——复权口径一旦搞错，长周期上会放大成巨大偏差。

### 4.3 基线指纹

首次全量拉完跑一遍 L1+L2，结果写入 `data/quality/baseline.json`：

```json
{"sh.600000": {"rows":181, "first":"2026-01-02", "last":"2026-09-30",
               "md5":"...", "errors":0, "warns":2, "checked_at":"..."}}
```

之后增量更新只 diff 新增段，5000 只也能秒级回归，无需每次全量重扫。

---

## 5. 新增文件

| 路径 | 职责 |
|---|---|
| `backend/core/adjustment.py` | 因子事件表加载、每日因子展开、后/前复权价计算、定点复权 |
| `backend/core/data_validator.py` | L1/L2 规则实现，输出结构化报告 |
| `scripts/quality_check.py` | CLI：`--all` 全量 L1L2、`--sample` 分层抽样 L3、`--baseline` 生成/比对指纹 |
| `scripts/fetch_all.py` | 批量全量拉取：串行 + 断点续传 + 失败重试 + 间隔控制 |
| `docs/data-source-and-adjustment.md` | 本文档定稿 |

---

## 6. 实施阶段

| 阶段 | 内容 | 验收 |
|---|---|---|
| **1. 复权内核** | `adjustment.py` + 单标的验证（浦发 07-16、茅台全历史） | 后复权价与 baostock `adjustflag=1` 逐位一致 |
| **2. 数据源切换** | 按 2.2 清单移除 yfinance，默认改 baostock | 全仓 grep `yfinance` 仅剩注释/历史说明 |
| **3. 校验模块** | `data_validator.py` + `quality_check.py` | 对现有样例数据跑通并输出报告 |
| **4. 批量拉取** | `fetch_all.py` 拉全 A 股 + 因子表 | 5000 只落盘，L1/L2 全量通过，生成基线指纹 |
| **5. 引擎接入** | 回测/策略引擎改用后复权价，涨跌停用不复权价 | L4 回归测试通过 |

---

## 7. 开放问题与最终决策

| # | 问题 | 决策 |
|---|---|---|
| **Q1** | 回测主价格序列用哪个？ | ✅ **后复权全程**（见 3.3）。已接入 `BacktestEngine.load_and_prepare_data(adjust='back')` |
| **Q2** | 停牌填充行剔除还是保留标记？ | ✅ **剔除**。实现 `data_validator.clean_suspensions()`；整年停牌清洗后为空时不写盘并告警 |
| **Q3** | paper/模拟的行情网关怎么办？ | ✅ **baostock 轮询 + 标注非实时**。`BaostockDataGateway` 已在 deltafq 内置，1:1 平替；轮询间隔 60s |
| **Q4** | yfinance 代码路径彻底删除还是保留关闭？ | ✅ **彻底删除**。白名单收敛为 `{baostock, miniqmt}` |
| **Q5** | 指数基准只留 `sh.000001`？ | ✅ 前端抽出 `BENCHMARK_SYMBOL` 常量，改常量即可换指数 |
| **Q6** | 全量拉取调度方式？ | ✅ `scripts/fetch_all.py` 一次性全量（断点续传）+ `--incremental N` 每日增量 |
| **Q7** | Volume 是否后复权？ | ✅ **不做**（默认 `adjust_volume=False`，需要时可开启） |

---

## 9. 实施后的实测发现（原方案未预料到）

### 9.1 复权事件表必须拉全历史 —— 否则后复权价会退化成不复权价

`backAdjustFactor` 是从上市日起的**累计**因子。按请求区间过滤会丢掉早于首个返回事件的因子。

反例（浦发 `sh.600000`）：只拉 2026-06-01 之后的事件 → 仅 1 条（2026-07-16），
导致 2026-06-01~07-15 的因子被当成 1.0：

```
后复权对撞 baostock: 最大相对差 = 0.92，超差 32 行   ← 后复权价从 118 掉到 9
```

修复：`AdjustmentStore.fetch_events()` **强制** `start=1990-01-01`、end=今天，
忽略调用方传入的区间。事件表通常只有几十行，全拉成本可忽略。

### 9.2 `backAdjustFactor` 会下降 —— 但这是数据源既定行为，不是算错

平安银行 `sz.000001` 在 2020-12-31 因子从 119.960317 降到 99.787353（-16%），
后复权价同期从 2303 跳到 1929。验证结果：

```
窗口 2020-12-20 ~ 2021-01-10，我方后复权价 vs baostock adjustflag=1
最大差 = 4.5e-13   → 逐位一致
```

即 baostock 自己的后复权价也是这么跳的。**规则 R2.9 因此从 ERROR 降级为 WARN**：
它是数据源的因子重算，不是我们算错，但该日前后的后复权价不可直接比较，
跨该日期的长周期回测收益率会有偏差，必须提示人工。

### 9.3 停牌日的成交量是**空字符串**，不是 0

中芯国际 `sh.688981` 2025-09-01~09-08 六天，OHLC 全等 114.76，`volume` 字段为空：

```
2025-09-01  114.76 114.76 114.76 114.76   NaN
```

直接落盘会变成 NaN 并污染下游。修复：`DataManager._standardize_dataframe()`
把空值填 0 并在全为整数时转 `int64`（顺带解决 `1.2e+08` 科学计数法精度丢失）。
停牌行本身由 R2.6 识别并在清洗阶段剔除。

### 9.4 端到端验证结果（6 个标的）

| 标的 | 说明 | L1/L2 | 关键告警 |
|---|---|---|---|
| `sh.600000` 浦发 | 含 2026-07-16 除权 | OK | — |
| `sh.600519` 茅台 | 高分红 | OK | — |
| `sh.000001` 上证指数 | 无复权因子 | OK | 因子表 0 行（预期） |
| `sz.000001` 平安 | 因子重算 | OK | R2.9 2020-12-31 因子下降 |
| `sh.688981` 中芯 | 科创板含停牌 | OK | R2.6 停牌 6 天；R2.7 2024-09-30/10-08 超 20% |
| `sh.600485` 信威 | 已退市长期停牌 | OK | **R2.6 停牌填充行 244 行**（全年） |

**L4 买入持有回归（2.7 年）** —— 复权方向全部正确：

```
sh.600000  不复权 14.11%  后复权 18.87%   差额 = 分红贡献
sh.600519  不复权 -10.09% 后复权 -6.90%
sz.000001  不复权  8.67%  后复权 16.75%
sh.688981  不复权 31.39%  后复权 31.39%   无分红 → 相同（符合预期）
sh.000001  不复权  9.94%  后复权  9.94%   指数无因子 → 相同（符合预期）
```

**回测链路**：`BOLLStrategy` on `sh.600000` 2024-01-01~2026-09-30 →
20 笔交易、总收益 57.54%、最大回撤 -16.76%、夏普 1.20。
不复权区间收益 43.64% vs 后复权 60.67%（前复权同为 60.67%，仅价格量级不同）。

---

## 8. 已知遗留风险

1. **baostock 无官方限流承诺**，它是免费无鉴权服务。全量拉取需自觉限速（建议单线程串行 + 每只间隔 0.2s，5000 只约 2-4 小时）
2. **baostock 是单连接库**（每次 `login/logout`），必须串行，已有 `_FETCH_LOCK` 保护
3. **baostock 盘中不刷新**，任何"实时"语义都不可信
4. **指数与个股的复权处理不同**（指数无因子），基准对比时不能用同一套复权逻辑
5. **历史规则变化**（创业板涨跌幅、ST 判定）会让软阈值误报，首期只当 WARN
