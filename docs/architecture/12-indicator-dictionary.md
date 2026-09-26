# 12. 指标字典（Indicator Dictionary）

> **文档定位**：本文档是全系统指标计算的唯一权威定义（Single Source of Truth）。所有引擎（Cycle / Valuation / Risk / Regime / Prediction / Backtest）、API 与前端展示必须引用本字典中的定义，禁止在代码中另行硬编码指标口径。
>
> **落地方式**：每个指标的元数据写入数据库表 `indicator_definitions`（对应需求文档第三十六节），代码中通过 `indicator_code` 引用；本文档与 `indicator_definitions` 表记录保持同步，任何口径变更必须同时更新两者并登记版本（见 §9）。
>
> **相关文档**：`13-model-architecture.md`（引擎如何消费指标）、`14-backtest-architecture.md`（Point-in-Time 约束）、数据库 ER 设计文档（表结构）。

---

## 1. 设计原则

1. **可追溯**：任何一个指标值都能沿「展示值 → 计算过程 → 标准化数据表 → 原始数据表（raw_*）→ Provider」的链路回溯到原始来源（需求文档原则 14：所有核心指标都必须知道自己的数据来源）。
2. **双模式解释**：每个指标必须同时提供「普通用户解释」（不含术语，一句话说清含义）与「专业模式详情」（公式、原始数值、历史分位、数据来源、计算方法）。对应需求文档第二十八节。
3. **分位数优先**：指标本身几乎不直接产生结论；所有引擎消费的是指标的**历史分位数（Percentile Rank）或标准化值**。原始阈值（如 MVRV>3.5）只作为展示层的「历史通常含义」参考，不得硬编码进业务判断。
4. **质量状态伴随**：每个指标值必须携带 `quality_status`（VERIFIED / ESTIMATED / STALE / CONFLICT / INVALID，见 10 号文档数据质量架构）与数据来源 `source_id`，数据降级时前端明确提示。
5. **Point-in-Time**：每条指标值记录必须同时保存 `observation_time`（数据所属时间）与 `fetch_time`（系统获取时间），回测与历史回放只允许使用 `fetch_time <= 模拟时刻` 的数据（详见 14 号文档 §2）。
6. **多源容错**：指标计算只依赖标准化数据表（如 `onchain_metrics`），不直接依赖任何 Provider；Provider 切换对指标层透明（需求文档第十节）。

## 2. 指标分类体系总览

| 分类 | 分类代码 | 指标条目数 | 主要更新频率 | 主要数据依赖表 |
|---|---|---|---|---|
| 技术指标 | `TECH` | 13 | 1m / 5m / 1h / 1d（随 K 线周期） | `candles`, `market_prices` |
| 链上指标 | `ONCHAIN` | 24 | 1h / 1d | `onchain_metrics`, `exchange_flows` |
| 衍生品指标 | `DERIV` | 10 | 5m / 1h / 8h | `derivatives` |
| 期权指标 | `OPT` | 5 | 1h / 1d | `options_data` |
| 宏观指标 | `MACRO` | 17 | 1d（按官方发布日历） | `macro_series` |
| 情绪指标 | `SENT` | 4 | 1h / 1d | `sentiment` |
| **合计** | — | **73** | — | — |

> 说明：MA 家族（SMA/EMA/WMA）与 RSI 家族（14/21）等按「一个计算口径 = 一个指标条目」定义，多周期/多参数通过参数字段区分，因此条目数少于指标实例数。指标实例（如 `EMA_99`）通过 `indicator_code + params` 唯一确定。

### 2.1 指标编码规范

```
indicator_code = {分类代码小写}.{指标名小写}[.{参数后缀}]
示例：
  tech.sma / tech.ema / tech.rsi / tech.macd
  onchain.mvrv / onchain.sopr / onchain.sopr.adjusted / onchain.sopr.lth
  deriv.funding_rate / opt.put_call_ratio / macro.fed_rate / sent.fear_greed
```

同一口径不同参数（如 SMA-200 与 SMA-50、RSI-14 与 RSI-21）共用一个 `indicator_code`，由 `params`（JSONB）区分实例，避免定义表膨胀。

## 3. 指标元数据统一格式

每个指标按以下字段定义（与 `indicator_definitions` 表字段一一对应）：

| 元数据字段 | 数据库字段 | 说明 |
|---|---|---|
| 指标名称 / 英文名 | `name_cn`, `name_en` | 展示名称 |
| 分类 | `category` | TECH / ONCHAIN / DERIV / OPT / MACRO / SENT |
| 指标代码 | `indicator_code` | 见 §2.1 编码规范 |
| 公式 | `formula` | 数学表达式，专业模式展示 |
| 参数 | `params` | JSONB，含默认值与可调范围 |
| 数据依赖 | `data_dependencies` | 依赖的标准化数据表与字段 |
| 计算周期 | `timeframe` | 1m/5m/1h/4h/1d 等 |
| 更新频率 | `update_frequency` | 调度器执行频率 |
| 值类型 / 单位 | `value_type`, `unit` | ratio / percent / usd / btc / count / index / z_score |
| 分位数方法 | `percentile_method` | 见 §4.1 |
| 普通用户解释 | `description_simple` | 面向非金融专业用户的一句话解释 |
| 专业模式详情 | `description_pro` | 公式、原始数值、历史分位、数据来源、计算方法 |
| 历史通常含义 | `historical_meaning` | 历史上高低值通常对应什么市场区域（仅参考，不做硬编码规则） |
| 注意事项 | `notes` | Provider 差异、口径陷阱、失效场景 |
| 消费方 | `consumed_by` | 哪些引擎/页面使用该指标 |
| 版本 | `version` | 口径变更时递增 |

## 4. 全局统一约定

### 4.1 分位数计算方法（percentile_method）

| 方法代码 | 名称 | 定义 | 适用指标 |
|---|---|---|---|
| `FULL_HISTORY` | 历史全量百分位 | 当前值在自数据可得以来全部历史值中的百分位（0~100%） | 慢变结构性指标：MVRV、NUPL、Realized Cap、HODL Waves 等 |
| `ROLLING_WINDOW` | 滚动窗口百分位 | 当前值在最近 N 天窗口内的百分位，N 默认 365，可配置 | 快变指标：Funding Rate、RSI、Liquidation、CVD、情绪类等 |
| `EXPANDING_FROM` | 指定起点扩展百分位 | 从指定日期（如 ETF 上市日 2024-01-11）起累积计算百分位 | 历史较短的新指标：ETF 流量、Gamma Exposure 等 |

统一约定：
- 百分位计算只使用 `quality_status IN ('VERIFIED','ESTIMATED')` 的数据；`STALE/CONFLICT` 数据不参与分位数累积，但当前值仍会带上其质量标记。
- 百分位值随每日任务重算并落库到 `indicator_values.percentile`，前端不实时计算。
- 历史全量百分位存在「早期低基数偏差」：BTC 早期数值普遍低，导致近年数值分位偏高。文档中对此类指标在「注意事项」中显式提示，并提供 `EXPANDING_FROM(2017-01-01)` 的辅助分位。

### 4.2 数值标准化

供引擎打分使用的标准化方式（写入 `indicator_values.normalized`）：
- `z_score`：(当前值 − 滚动均值) / 滚动标准差，窗口默认 365 天；
- `minmax`：(当前值 − 历史最小) / (历史最大 − 历史最小)，用于有界指标（RSI、Fear&Greed）；
- `percentile`：即 §4.1 分位数，多数引擎的默认输入。

### 4.3 缺失与降级处理

- 单点缺失：指标任务跳过该周期，不插值、不伪造（需求文档第十四节）；
- 连续缺失超过 3 个周期：指标标记 `STALE`，前端显示「最后更新时间」；
- 依赖表多源冲突：指标标记 `CONFLICT`，仍可计算（取中位数源），但引擎消费时自动降低该因子权重（见 13 号文档「数据置信度」）。

### 4.4 质量状态定义

| 状态 | 含义 | 引擎处理 |
|---|---|---|
| VERIFIED | 多源交叉验证一致 | 正常权重 |
| ESTIMATED | 单源数据或备用源数据，未交叉验证 | 正常权重，标记来源 |
| STALE | 数据过期（超过更新周期 N 倍未更新） | 权重减半，前端提示 |
| CONFLICT | 多源差异超阈值 | 权重减半，记录冲突，前端提示 |
| INVALID | 数据校验失败（超出合理范围、格式错误、无可用数据） | 不参与计算，前端明确提示 |

---

## 5. 技术指标（TECH）

> 数据来源：`candles`（多周期 K 线，由 MarketService 从多 Provider 聚合）、`market_prices`。
> 通用注意事项：不同交易所价格存在微小差异，系统使用**聚合参考价**（多源中位数，见交叉验证机制）计算技术指标；回测时必须使用与当时一致的聚合口径。

### 5.1 MA 家族（SMA / EMA / WMA）

**指标名称**：移动平均线（简单/指数/加权）
**英文名**：Moving Average (Simple / Exponential / Weighted)
**分类**：技术指标
**指标代码**：`tech.sma` / `tech.ema` / `tech.wma`
**公式**：
- SMA(N) = Σ(Close_i, i=1..N) / N
- EMA(N)：EMA_t = Close_t × k + EMA_{t−1} × (1 − k)，k = 2 / (N + 1)
- WMA(N) = Σ(Close_{N−i} × (N−i), i=0..N−1) / Σ(1..N)

**参数**：周期 N ∈ {7, 25, 50, 99, 100, 200, 365}（日线默认全集；小时线默认 {25, 99, 200}）
**数据依赖**：`candles(close)`，按 timeframe 分组
**计算周期**：1m / 5m / 1h / 4h / 1d
**更新频率**：随对应 K 线收盘更新（1d 指标每日 UTC 00:00 收盘后计算）
**值类型/单位**：价格 / USD
**分位数方法**：不适用（价格类指标，引擎消费的是「价格与均线的相对位置」`price/MA − 1` 的百分位，ROLLING_WINDOW 365）
**普通用户解释**：「过去 N 天市场的平均买入成本线。价格在均线上方说明近期买入的人整体赚钱，下方说明整体亏钱。200 日均线常被看作长期牛熊分界」
**专业模式详情**：显示三种 MA 公式、当前各周期数值、价格与各均线乖离率、金叉/死叉状态（短期均线上穿/下穿长期均线）、数据来源与更新时间
**历史通常含义**：日线价格站上 200DMA 多数时间对应牛市区间；200DMA 之下对应熊市区间。EMA 比 SMA 对近期价格更敏感，WMA 介于两者之间偏向线性加权
**注意事项**：365DMA 需要至少 1 年数据，2015 年前的展示受数据覆盖限制；均线在横盘期频繁金叉死叉，信号意义弱，ADX 低时应提示「趋势不明」
**消费方**：Cycle Engine（趋势因子）、Risk Engine（趋势风险）、行情页、首页趋势卡片

### 5.2 RSI

**指标名称**：相对强弱指数
**英文名**：Relative Strength Index
**分类**：技术指标
**指标代码**：`tech.rsi`
**公式**：RSI = 100 − 100 / (1 + RS)，RS = AvgGain(N) / AvgLoss(N)；AvgGain/AvgLoss 采用 Wilder 平滑（首个值为 N 期简单平均，其后 Avg_t = (Avg_{t−1} × (N−1) + X_t) / N）
**参数**：N ∈ {14, 21}
**数据依赖**：`candles(close)`
**计算周期**：1h / 4h / 1d
**更新频率**：随 K 线收盘
**值类型/单位**：指数 / 0–100
**分位数方法**：minmax 标准化（本身有界）；引擎另用 ROLLING_WINDOW(365) 百分位
**普通用户解释**：「衡量最近一段时间买方和卖方谁更有力气。数值越高说明买盘越猛、短期可能过热；越低说明卖盘越重、短期可能超卖」
**专业模式详情**：显示 Wilder 平滑公式、RSI-14 与 RSI-21 当前值、超买/超卖区域（70/30，仅展示参考线）、历史分位、背离检测状态（价格新高而 RSI 未新高）
**历史通常含义**：日线 RSI > 70 常出现在上涨加速段与局部顶部附近；< 30 常出现在恐慌抛售底部附近；极端行情中 RSI 可长期钝化在超买/超卖区
**注意事项**：RSI 钝化是常态（单边趋势中可连续数周 >70），禁止单独作为反转依据；不同周期 RSI 结论可能相反，展示时必须标注周期
**消费方**：Cycle Engine（动量因子）、Risk Engine（市场拥挤度）、情绪面板

### 5.3 MACD

**指标名称**：指数平滑异同移动平均线
**英文名**：Moving Average Convergence Divergence
**分类**：技术指标
**指标代码**：`tech.macd`
**公式**：DIF = EMA(12) − EMA(26)；DEA = EMA(DIF, 9)；Histogram = DIF − DEA（展示层可选 ×2）
**参数**：(fast=12, slow=26, signal=9)，日线默认；其他周期沿用同参数
**数据依赖**：`candles(close)`
**计算周期**：4h / 1d
**更新频率**：随 K 线收盘
**值类型/单位**：价格差 / USD
**分位数方法**：ROLLING_WINDOW(730)，用于判断当前动能强度的历史相对位置
**普通用户解释**：「观察短期成本和长期成本的差距是在拉大还是缩小，用来判断上涨/下跌的『劲儿』是在增强还是减弱」
**专业模式详情**：显示 DIF/DEA/Histogram 数值、金叉死叉事件时间、零轴上下状态、与价格的背离标记、历史分位
**历史通常含义**：日线 MACD 零轴上方金叉多对应上升趋势中继；零轴下方死叉多对应下跌延续；Histogram 收敛预示动能减弱
**注意事项**：滞后指标，转折点上必然晚于价格；横盘期频繁交叉产生噪音信号，应结合 ADX 过滤
**消费方**：Cycle Engine（趋势动能因子）、行情页

### 5.4 ADX

**指标名称**：平均趋向指数
**英文名**：Average Directional Index
**分类**：技术指标
**指标代码**：`tech.adx`
**公式**：+DM / −DM → 方向指标；TR 为真实波幅；ADX = Wilder 平滑( DX, 14 )，DX = |+DI − −DI| / (+DI + −DI) × 100
**参数**：N = 14
**数据依赖**：`candles(high, low, close)`
**计算周期**：4h / 1d
**更新频率**：随 K 线收盘
**值类型/单位**：指数 / 0–100
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「衡量市场当前是『有明确方向的趋势市』还是『来回震荡的盘整市』。数值越高趋势越强，但它不区分涨跌方向」
**专业模式详情**：显示 ADX、+DI、−DI 数值与交叉状态、趋势强度分级（<20 弱 / 20–40 中 / >40 强，仅展示参考）、历史分位
**历史通常含义**：ADX > 25 且 +DI > −DI 通常对应健康上升趋势；ADX < 20 通常对应震荡市，此时 MA/MACD 类信号可靠性显著下降
**注意事项**：ADX 本身滞后于趋势启动；只用于「趋势强度」判断，不用于方向判断；在 Cycle Engine 中作为其他趋势因子的置信度调节器
**消费方**：Cycle Engine（趋势置信度）、Risk Engine（趋势风险权重调节）

### 5.5 ATR

**指标名称**：平均真实波幅
**英文名**：Average True Range
**分类**：技术指标
**指标代码**：`tech.atr`
**公式**：TR = max(High−Low, |High−Close_prev|, |Low−Close_prev|)；ATR = Wilder 平滑(TR, 14)
**参数**：N = 14
**数据依赖**：`candles(high, low, close)`
**计算周期**：1h / 1d
**更新频率**：随 K 线收盘
**值类型/单位**：价格 / USD（同时派生 ATR% = ATR / Close × 100）
**分位数方法**：ROLLING_WINDOW(365)，对 ATR% 计算
**普通用户解释**：「最近两周市场平均每天上下波动多少钱，衡量市场『颠簸程度』。数值越大，短期风险越高」
**专业模式详情**：显示 ATR、ATR%、历史分位、与 Historical Volatility 的对照、数据来源
**历史通常含义**：ATR% 骤升通常对应恐慌或逼空行情；长期低 ATR% 后的放量突破往往伴随趋势启动
**注意事项**：ATR 只度量波动幅度不度量方向；不同价格量级下必须用 ATR% 做跨期比较
**消费方**：Risk Engine（波动风险）、Backtest（滑点模型校准）、定投模拟（风险调整）

### 5.6 Bollinger Bands

**指标名称**：布林带
**英文名**：Bollinger Bands
**分类**：技术指标
**指标代码**：`tech.bollinger`
**公式**：Mid = SMA(Close, 20)；Upper = Mid + 2 × σ(Close, 20)；Lower = Mid − 2 × σ(Close, 20)；%B = (Close − Lower) / (Upper − Lower)；Bandwidth = (Upper − Lower) / Mid
**参数**：(N=20, k=2)，σ 为样本标准差
**数据依赖**：`candles(close)`
**计算周期**：4h / 1d
**更新频率**：随 K 线收盘
**值类型/单位**：价格 / USD；%B 与 Bandwidth 为无量纲
**分位数方法**：对 %B 用 minmax 截断展示；对 Bandwidth 用 ROLLING_WINDOW(730) 百分位判断「挤压」程度
**普通用户解释**：「以 20 天平均价为中心画出正常波动范围。价格碰到上轨说明短期偏强，碰到下轨说明短期偏弱，带宽收窄说明市场正在憋一波大行情」
**专业模式详情**：显示上/中/下轨数值、%B、Bandwidth 及其历史分位、Squeeze 检测（Bandwidth 处于历史低位）状态
**历史通常含义**：Bandwidth 极度收窄（历史低分位）后常出现方向性突破，但方向不可预判；%B > 1 或 < 0 表示价格冲出带外，出现在强趋势中属正常现象
**注意事项**：「触轨即反转」是常见误用，强趋势中价格可沿上轨/下轨持续运行（walking the band）；只用于波动状态描述，不用于方向预测
**消费方**：Risk Engine（波动风险）、行情页

### 5.7 Volume Profile

**指标名称**：成交量分布
**英文名**：Volume Profile
**分类**：技术指标
**指标代码**：`tech.volume_profile`
**公式**：将统计区间按价格分为若干档位（tick 分组），累加每档成交量；POC = 成交量最大的价格档；VA = 覆盖区间总成交量 70% 的价格范围（VAH/VAL）
**参数**：区间（默认近 90 天）、档位数（默认 100）、VA 比例（70%）
**数据依赖**：`candles(high, low, close, volume)`（近似法：按 K 线高低价范围均摊成交量）；如接入 tick/逐笔数据可精确计算
**计算周期**：1d（每日重算区间分布）
**更新频率**：每日
**值类型/单位**：成交量 / BTC
**分位数方法**：不适用（结构类指标，输出 POC/VAH/VAL 价格水平）
**普通用户解释**：「统计一段时间里，大家在哪些价位买卖得最多。成交最密集的价位（POC）像『市场的重心』，价格离开重心太远后经常回来」
**专业模式详情**：显示完整分布直方图数据、POC/VAH/VAL 数值、当前价格相对 VA 位置、低成交量节点（LVN）列表
**历史通常含义**：价格在 VA 内运行属「平衡市」；突破 VAH 且回踩不破常被视为强势；POC 是高频 magnet 价位
**注意事项**：基于日 K 近似计算与逐笔精确计算存在偏差，展示时标注计算方法；区间参数对结果影响大，必须固定默认参数以保证历史可比性
**消费方**：行情页、Risk Engine（关键价位流动性参考）

### 5.8 VWAP

**指标名称**：成交量加权平均价
**英文名**：Volume Weighted Average Price
**分类**：技术指标
**指标代码**：`tech.vwap`
**公式**：VWAP = Σ(TypicalPrice_i × Volume_i) / Σ(Volume_i)，TypicalPrice = (H+L+C)/3；按锚定周期重置（日内 VWAP 每日重置；周/月 VWAP 分别按周/月重置）；另提供 Anchored VWAP（锚定事件日，如周期低点、ETF 上市日）
**参数**：锚定周期 ∈ {session(日), week, month, event}
**数据依赖**：`candles(high, low, close, volume)`（1m 或 5m 粒度聚合更精确）
**计算周期**：5m / 1h / 1d
**更新频率**：随 K 线更新
**值类型/单位**：价格 / USD
**分位数方法**：不适用（引擎消费「价格相对 VWAP 乖离率」的 ROLLING_WINDOW 百分位）
**普通用户解释**：「今天（或本周/本月）所有成交按成交量加权的平均成交价，代表市场参与者的『平均持仓成本』。价格在它上方说明当天买入的人整体浮盈」
**专业模式详情**：显示各锚定周期 VWAP 数值、乖离率、Anchored VWAP 的锚点事件说明、计算粒度（5m/1m）
**历史通常含义**：机构执行算法普遍以 VWAP 为基准，价格回踩周/月 VWAP 常出现承接；日线收盘持续低于日 VWAP 对应卖压主导
**注意事项**：跨日比较必须使用同一锚定口径；加密货币 7×24 交易，「日内」以 UTC 00:00 为界并需在界面注明
**消费方**：行情页、Backtest（成交假设）、Risk Engine（短期动能）

### 5.9 Historical Volatility

**指标名称**：历史波动率
**英文名**：Historical Volatility
**分类**：技术指标
**指标代码**：`tech.hv`
**公式**：r_t = ln(Close_t / Close_{t−1})；HV(N) = std(r, N) × √365 × 100%（加密货币全年交易，年化因子用 365）
**参数**：N ∈ {30, 60, 90}
**数据依赖**：`candles(close)`，1d
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %（年化）
**分位数方法**：FULL_HISTORY（波动率结构长期可比）
**普通用户解释**：「过去一个多月里价格起伏的剧烈程度，折算成年化百分比。数值越大，市场越不稳定，短期风险越高」
**专业模式详情**：显示 HV30/60/90 数值与曲线、期限结构（HV30 vs HV90 倒挂/正常）、与期权 IV 的对比（IV−HV 价差）、历史分位
**历史通常含义**：HV30 > 80% 通常对应恐慌或狂热阶段；HV 长期低位（<40%）常出现在筑底与趋势酝酿期；HV30 < HV90 表明波动正在收敛
**注意事项**：与期权 IV 对比时必须同口径年化（365）；对数收益率的极端值（单日 ±20%）会显著抬升短窗口 HV，展示时附窗口内最大单日波动
**消费方**：Risk Engine（波动风险核心输入）、Valuation Engine（辅助）、期权页

### 5.10 Percentile Rank

**指标名称**：历史百分位排名
**英文名**：Percentile Rank
**分类**：技术指标（通用派生工具）
**指标代码**：`tech.percentile_rank`
**公式**：PctRank(x_t, N) = count(x_i < x_t, i ∈ [t−N, t)) / N × 100%
**参数**：目标序列（任意指标）、窗口 N ∈ {90, 365, 730, FULL}
**数据依赖**：任意 `indicator_values` / 标准化数据表
**计算周期**：1d
**更新频率**：每日（随目标指标更新）
**值类型/单位**：百分比 / 0–100%
**分位数方法**：本指标即分位数工具，方法遵循 §4.1
**普通用户解释**：「告诉你现在的数值在历史上排第几。比如 90% 表示历史上 90% 的时间都比现在低，属于少见的高位」
**专业模式详情**：显示目标指标、窗口、样本数量、计算方法（strict less-than 计数）、当前百分位、样本起始日期
**历史通常含义**：>90% 或 <10% 表示极端状态；50% 附近表示中性
**注意事项**：窗口选择直接影响结论，所有展示必须标注窗口与样本数；样本数 < 365 时标记「样本不足，分位参考意义有限」；这是所有引擎打分的主要输入形式
**消费方**：全部引擎（Cycle / Valuation / Risk / Regime / Prediction）、所有指标详情页

### 5.11 Drawdown（回撤）

**指标名称**：价格回撤
**英文名**：Drawdown from ATH / Rolling High
**分类**：技术指标
**指标代码**：`tech.drawdown`
**公式**：DD_ATH = Close / ATH − 1（ATH 为截至 observation_time 的历史最高价，Point-in-Time 计算）；DD_RH(N) = Close / max(Close, N天) − 1
**参数**：N ∈ {30, 90, 365}（滚动高点窗口）；ATH 口径固定
**数据依赖**：`market_prices(close)`, `candles(high)`
**计算周期**：1d（另有 1h 高频版供风险页）
**更新频率**：每日
**值类型/单位**：百分比 / %（负值）
**分位数方法**：FULL_HISTORY（回撤幅度历史分布稳定）
**普通用户解释**：「当前价格距离历史最高点跌了百分之多少。历史上 BTC 从高点跌 30%~50% 很常见，跌得越深，越接近历史上的恐慌区域」
**专业模式详情**：显示 ATH 数值与日期（Point-in-Time）、DD_ATH、DD_RH30/90/365、当前回撤持续时间、历史回撤列表对照
**历史通常含义**：−20% 以内属正常波动；−30%~−50% 历史上多对应中期调整或熊市早段；−70% 以上历史上仅出现在深度熊市
**注意事项**：ATH 必须 Point-in-Time 计算——回测 2017 年时不得使用 2021 年的 ATH；这是未来数据泄漏的高发点（14 号文档 §2 将其列为强制检测项）
**消费方**：Valuation Engine（回撤维度）、Risk Engine（回撤风险）、Cycle Engine（底部因子）、定投模拟（回撤加仓）

### 5.12 Price vs 200W Cost Basis（长期成本线，可选扩展）

**指标名称**：价格相对长期均线位置
**英文名**：Price to Long-term MA Ratio
**分类**：技术指标
**指标代码**：`tech.price_to_ma`
**公式**：Ratio = Close / MA(N) − 1，N ∈ {200, 365}（日线）；同时输出 200 周均线（200W MA）比率
**参数**：N、MA 类型（默认 SMA）
**数据依赖**：`candles(close)`（1d / 1w）
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %
**分位数方法**：FULL_HISTORY
**普通用户解释**：「现在的价格比长期平均成本高多少或少多少。历史上价格大幅高于长期成本线时往往偏贵，跌破长期成本线时往往偏便宜」
**专业模式详情**：显示 Close、200DMA、200W MA、各乖离率及其历史分位、历史上跌破 200W MA 的时段列表
**历史通常含义**：日线收盘跌破 200W MA 历史上仅出现在大级别底部区域（2015、2018 末、2020-03、2022 末）；乖离率 >150% 历史上多对应过热阶段
**注意事项**：周线指标需要长历史数据，2015 年前数据覆盖不足时分位标注「样本受限」
**消费方**：Valuation Engine（长期趋势维度）、Cycle Engine（底部/顶部因子）

### 5.13 Spread & Order Book Depth（价差与盘口深度）

**指标名称**：买卖价差与盘口深度
**英文名**：Bid-Ask Spread & Order Book Depth
**分类**：技术指标（微观结构）
**指标代码**：`tech.spread` / `tech.depth`
**公式**：Spread = (Ask_1 − Bid_1) / Mid × 10000（bps）；Depth(±2%) = 中间价 ±2% 范围内买卖挂单量（BTC 与 USD 计）
**参数**：深度档位（默认 ±1%、±2%）
**数据依赖**：`orderbooks`（交易所盘口快照）
**计算周期**：实时快照（5s~1m）
**更新频率**：分钟级聚合入库
**值类型/单位**：Spread 为 bps；Depth 为 BTC / USD
**分位数方法**：ROLLING_WINDOW(90)
**普通用户解释**：「衡量市场买卖是否顺畅。价差越小、挂单越厚，说明市场越有流动性，大额买卖对价格的冲击越小」
**专业模式详情**：显示各交易所 Spread、聚合 Depth、买卖失衡比（Bid Depth / Ask Depth）、历史分位
**历史通常含义**：Spread 骤增 / Depth 骤减常出现在极端行情与流动性危机前夜；买卖失衡持续偏向一侧提示短期方向压力
**注意事项**：盘口数据量大，仅保留分钟级聚合结果长期存储，原始快照短期保留；不同交易所口径不可直接混合，按交易所分别展示
**消费方**：Risk Engine（流动性风险核心输入）、数据质量中心

---

## 6. 链上指标（ONCHAIN）

> 数据来源：`onchain_metrics`、`exchange_flows`（由 OnChainService / ExchangeFlowService 聚合多个链上数据 Provider）。
> **通用注意事项（适用于本节全部指标）**：
> 1. 不同 Provider 对「实体聚类」（把地址归并为交易所/鲸鱼/矿工）算法不同，同名指标数值可能有 5%~15% 差异——系统记录主源数值，冲突超阈值时标记 CONFLICT；
> 2. 链上数据存在**回填修正**（部分 Provider 会修正前几日数据），所有链上指标必须保存 `fetch_time`，回测按 fetch_time 过滤（14 号文档 §2）；
> 3. 「长期持有者 LTH」通用口径为持币 > 155 天，「短期持有者 STH」为 ≤ 155 天，全系统统一采用该口径。

### 6.1 MVRV

**指标名称**：市值与已实现市值比率
**英文名**：Market Value to Realized Value
**分类**：链上指标
**指标代码**：`onchain.mvrv`
**公式**：MVRV = Market Cap / Realized Cap（Market Cap = Close × Circulating Supply）
**参数**：无（派生 MVRV Z-Score 见 6.1.1 备注）
**数据依赖**：`market_prices(market_cap 或 close)`, `onchain_metrics(realized_cap, supply)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：倍数 / ratio
**分位数方法**：FULL_HISTORY（另提供 EXPANDING_FROM(2017-01-01) 辅助分位）
**普通用户解释**：「衡量当前市场价格相对于链上平均成本的倍数。>1 表示市场整体盈利，<1 表示整体亏损。倍数越高，市场越贵」
**专业模式详情**：显示公式、Market Cap、Realized Cap 原始数值、MVRV、历史分位、数据来源 Provider、计算方法、MVRV-Z 变体
**历史通常含义**：>3.5 通常对应周期顶部区域，<1 通常对应底部区域（历史上 MVRV<1 的时间占比很低）
**注意事项**：不同 Provider 的 Realized Cap 计算可能有差异（是否剔除交易所钱包、丢失币等）；早期（2013 前）分位参考意义弱
**消费方**：Valuation Engine（核心维度）、Cycle Engine、Risk Engine（估值风险）、定投模拟（估值加仓）

### 6.2 Realized Cap

**指标名称**：已实现市值
**英文名**：Realized Cap
**分类**：链上指标
**指标代码**：`onchain.realized_cap`
**公式**：Realized Cap = Σ(每枚 UTXO 数量 × 该 UTXO 最后一次链上移动时的价格)；可理解为「全网持仓的链上平均成本 × 流通量」
**参数**：无
**数据依赖**：`onchain_metrics(realized_cap)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：金额 / USD
**分位数方法**：不适用（单调增长的量级指标）；引擎消费其**变化率**：Realized Cap 30d/90d 增速的 ROLLING_WINDOW(730) 百分位
**普通用户解释**：「把每一枚币按它最后一次移动时的价格加起来，约等于全市场的『总成本』。它持续上涨说明有新资金以更高价格进场沉淀」
**专业模式详情**：显示 Realized Cap 数值与曲线、30d/90d 变化率、与 Market Cap 对照、Realized Price（= Realized Cap / Supply）、数据来源
**历史通常含义**：Realized Price 历史上是强支撑区域；Realized Cap 增速放缓常出现在周期后段
**注意事项**：交易所内部钱包整理会造成 UTXO 移动噪音，Provider 通常已过滤，但过滤口径不一；数值只增不减的特性使其不适合直接做分位
**消费方**：Valuation Engine（成本基础维度）、Cycle Engine（资金沉淀因子）

### 6.3 SOPR

**指标名称**：花费产出利润率
**英文名**：Spent Output Profit Ratio
**分类**：链上指标
**指标代码**：`onchain.sopr`
**公式**：SOPR = Σ(花费 UTXO 的产出价值) / Σ(花费 UTXO 的创造价值) = 已实现价格 / 创建价格（按当日全部花费输出加权）
**参数**：无（日度值）
**数据依赖**：`onchain_metrics(sopr)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：倍数 / ratio（围绕 1 波动）
**分位数方法**：ROLLING_WINDOW(730)（SOPR 是快变量，全历史分位意义弱）
**普通用户解释**：「今天卖币的人平均是赚钱还是亏钱。大于 1 说明当天卖出者平均获利了结，小于 1 说明割肉离场」
**专业模式详情**：显示 SOPR 数值、7 日移动平均、1 上方/下方持续天数、历史分位、与 aSOPR/LTH-SOPR 对照、数据来源
**历史通常含义**：牛市中 SOPR 回踩 1 获得支撑（获利盘换手完成）；熊市中 SOPR 反弹至 1 受阻（解套盘抛出）；持续 <1 对应投降式抛售
**注意事项**：SOPR 对交易所内部转账敏感（0 利润转移需过滤，各 Provider 过滤口径不同）；单日值噪音大，引擎消费其 7DMA
**消费方**：Cycle Engine（底部/顶部行为因子）、Valuation Engine（辅助）、Risk Engine（链上异常）

### 6.4 aSOPR

**指标名称**：调整花费产出利润率
**英文名**：Adjusted SOPR
**分类**：链上指标
**指标代码**：`onchain.sopr.adjusted`
**公式**：同 SOPR，但剔除寿命 < 1 小时的 UTXO（过滤链上噪音与交易所内部转账）
**参数**：寿命阈值 = 1h（固定口径）
**数据依赖**：`onchain_metrics(asopr)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：倍数 / ratio
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「和 SOPR 一样的含义，但剔除了机器人和交易所内部的频繁转账噪音，更能反映真实投资者的盈亏行为」
**专业模式详情**：显示 aSOPR 与原始 SOPR 对照、剔除规则说明、7DMA、历史分位、数据来源
**历史通常含义**：与 SOPR 相同；因噪音更少，1 附近的支撑/压力信号更可靠
**注意事项**：部分 Provider 不提供 aSOPR，需确认覆盖能力后再配置 Provider 优先级
**消费方**：同 SOPR（作为 SOPR 的优选替代，两者同时入库）

### 6.5 LTH-SOPR

**指标名称**：长期持有者花费产出利润率
**英文名**：Long-Term Holder SOPR
**分类**：链上指标
**指标代码**：`onchain.sopr.lth`
**公式**：仅统计币龄 > 155 天的花费 UTXO 的 SOPR
**参数**：币龄阈值 = 155 天（全系统统一 LTH 口径）
**数据依赖**：`onchain_metrics(lth_sopr)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：倍数 / ratio
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「只看『老币民』（拿币超过 5 个月的人）卖币时是赚是亏。老币民大量获利卖出常出现在市场高位，割肉卖出常出现在底部」
**专业模式详情**：显示 LTH-SOPR 数值、7DMA、持续 >1 / <1 天数、历史分位、与 STH-SOPR（如可得）对照
**历史通常含义**：LTH-SOPR 从高位回落至 1 附近并企稳，历史上对应牛市中的深度回调；持续 <1 数月对应熊市投降段
**注意事项**：155 天口径为行业惯例而非真理，需在专业模式注明；币龄计算依赖 Provider 的 UTXO 追踪完整性
**消费方**：Cycle Engine（分配阶段核心证据）、Valuation Engine、Risk Engine

### 6.6 NUPL

**指标名称**：净未实现盈亏
**英文名**：Net Unrealized Profit / Loss
**分类**：链上指标
**指标代码**：`onchain.nupl`
**公式**：NUPL = (Market Cap − Realized Cap) / Market Cap；等价于 1 − 1/MVRV；可按 LTH/STH 分解
**参数**：无
**数据依赖**：`market_prices(market_cap)`, `onchain_metrics(realized_cap)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：比例 / 0–1（可为负）
**分位数方法**：FULL_HISTORY
**普通用户解释**：「全市场账面上有多少比例的利润还没落袋。数值越高，说明浮盈越大、获利了结的压力越大；为负说明全网整体亏损」
**专业模式详情**：显示 NUPL 数值、传统分区（>0.75 极度贪婪 / 0.5~0.75 贪婪 / 0.25~0.5 乐观 / 0~0.25 希望-恐惧 / <0 投降，仅展示参考）、历史分位、LTH/STH 分解（如可得）
**历史通常含义**：NUPL > 0.75 历史上仅出现在周期顶部区域；NUPL < 0 历史上对应大底（全网整体亏损）
**注意事项**：与 MVRV 数学同源，两者在引擎中不可重复计入权重（13 号文档因子共线性处理）；Market Cap 口径需与 Realized Cap 的 supply 口径一致
**消费方**：Valuation Engine（核心）、Cycle Engine（顶部/底部证据）、定投模拟（估值加仓）

### 6.7 Puell Multiple

**指标名称**：普尔倍数
**英文名**：Puell Multiple
**分类**：链上指标
**指标代码**：`onchain.puell_multiple`
**公式**：Puell = 当日矿工收入(USD) / 365 日移动平均矿工收入(USD)；矿工收入 = 区块奖励 + 手续费（按产出当日价格计 USD）
**参数**：MA 窗口 = 365（固定口径）
**数据依赖**：`onchain_metrics(miner_revenue_usd)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：倍数 / ratio
**分位数方法**：FULL_HISTORY
**普通用户解释**：「矿工今天的收入是过去一年平均收入的几倍。矿工是市场里的刚性卖家，他们收入异常高时往往接近市场顶部，收入被压到很低时往往接近底部」
**专业模式详情**：显示 Puell 数值、当日矿工收入、365DMA、历史分位、减半日期标记（减半导致收入结构性下移，需对照解读）
**历史通常含义**：>4 历史上多次对应周期顶部附近；<0.5 历史上多次对应底部区域
**注意事项**：**比特币减半会使矿工收入骤降，Puell 在减半后 1 年内系统性偏低**——解读必须结合减半日历（事件时间线，需求文档第三十节）；这是该指标最大的口径陷阱
**消费方**：Cycle Engine（顶部/底部因子）、Valuation Engine（辅助）

### 6.8 RHODL Ratio

**指标名称**：RHODL 比率
**英文名**：RHODL Ratio
**分类**：链上指标
**指标代码**：`onchain.rhodl`
**公式**：RHODL = 短期持有者供应比例 / 长期持有者供应比例；其中 STH 供应 = 币龄 ≤ 48 周的供应量，LTH 供应 = 币龄 > 48 周的供应量（RHODL 原始口径用 48 周，与 §6 通用 155 天口径不同，专业模式必须注明）
**参数**：币龄分界 = 48 周（指标原始口径）
**数据依赖**：`onchain_metrics(rhodl_ratio 或 sth_supply_48w, lth_supply_48w)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %
**分位数方法**：FULL_HISTORY
**普通用户解释**：「衡量市场上是『新进场的人』多还是『拿了很久的老手』多。新人占比冲高说明市场热度极高、常接近顶部；新人占比极低说明无人问津、常接近底部」
**专业模式详情**：显示 RHODL 数值、STH/LTH 供应分解、RHODL 热力带（历史分位着色）、48 周口径说明、数据来源
**历史通常含义**：RHODL 冲至历史高分位（如 2017、2021 顶部）后均出现深度回调；跌至历史低分位对应熊市底部
**注意事项**：48 周口径与其他指标的 155 天口径并存，引擎侧作为独立因子处理，避免与 LTH Supply 重复计权
**消费方**：Cycle Engine（热度因子）、Risk Engine（市场拥挤度）

### 6.9 Reserve Risk

**指标名称**：储备风险
**英文名**：Reserve Risk
**分类**：链上指标
**指标代码**：`onchain.reserve_risk`
**公式**：Reserve Risk = Price / HODL Bank；HODL Bank = Σ(币龄带宽加权供应量)，常用带宽：1d、1-2d、2-3d、…、指数带宽至 10y+（按 Glassnode 口径的 coin-day 累积折现和）
**参数**：HODL Bank 带宽表（固定行业标准口径）
**数据依赖**：`onchain_metrics(reserve_risk 或 price + hodl_bank)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：比值（对数展示，log10）
**分位数方法**：FULL_HISTORY
**普通用户解释**：「价格相对于大家『囤币信心』的高低。老币民拿住不动时该值很低，意味着用较低的价格换取较高的长期信心，历史上是较好的长期买点区域」
**专业模式详情**：显示 Reserve Risk、HODL Bank 数值、log10 曲线、历史分位、计算方法与带宽表、数据来源
**历史通常含义**：历史低分位（如 <10%）多次对应大级别底部；历史高分位对应顶部区域
**注意事项**：数值跨越多个数量级，展示必须用对数轴；计算依赖 Provider 的完整 UTXO 币龄集，自建计算成本高，优先直接采集
**消费方**：Valuation Engine（长期维度）、Cycle Engine（底部因子）

### 6.10 Realized Profit

**指标名称**：已实现利润
**英文名**：Realized Profit
**分类**：链上指标
**指标代码**：`onchain.realized_profit`
**公式**：Realized Profit = Σ(当日花费 UTXO 中，产出价值 > 创造价值的部分：产出价值 − 创造价值)
**参数**：无（日度值）
**数据依赖**：`onchain_metrics(realized_profit_usd)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：金额 / USD
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天所有卖币赚钱的人一共赚走了多少钱。获利了结金额异常放大，通常说明市场进入兑现高潮」
**专业模式详情**：显示当日 Realized Profit、7DMA、历史分位、与 Realized Loss 对照、Realized P/L 比值
**历史通常含义**：Realized Profit 创阶段新高常出现在顶部派发期；持续低迷对应熊市
**注意事项**：金额随 BTC 价格量级增长，跨周期比较必须用分位或除以 Realized Cap 归一化；与 Realized Loss 来自同一 UTXO 口径，须使用同一 Provider 避免比值失真
**消费方**：Cycle Engine（分配证据）、Risk Engine（链上异常）

### 6.11 Realized Loss

**指标名称**：已实现亏损
**英文名**：Realized Loss
**分类**：链上指标
**指标代码**：`onchain.realized_loss`
**公式**：Realized Loss = Σ(当日花费 UTXO 中，产出价值 < 创造价值的部分：创造价值 − 产出价值)
**参数**：无（日度值）
**数据依赖**：`onchain_metrics(realized_loss_usd)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：金额 / USD
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天所有亏钱卖币的人一共亏了多少钱。恐慌割肉的金额冲到极端高位时，历史上往往离阶段性底部不远」
**专业模式详情**：显示当日 Realized Loss、7DMA、历史分位、Loss/Profit 比值、投降日标记（Loss 处于历史高分位且价格单日跌幅 >7%）
**历史通常含义**：Realized Loss 极端放大（投降式抛售）历史上多次对应局部或周期底部；持续低位对应麻木期
**注意事项**：同 6.10（量级归一化、同源 Provider）
**消费方**：Cycle Engine（底部证据）、Risk Engine（链上异常）

### 6.12 LTH Supply

**指标名称**：长期持有者供应量
**英文名**：Long-Term Holder Supply
**分类**：链上指标
**指标代码**：`onchain.lth_supply`
**公式**：LTH Supply = Σ(币龄 > 155 天的 UTXO 数量)
**参数**：币龄阈值 = 155 天（全系统统一口径）
**数据依赖**：`onchain_metrics(lth_supply)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / BTC
**分位数方法**：不适用；引擎消费其 **90 日净变化**（ΔLTH Supply）的方向与 ROLLING_WINDOW(730) 百分位
**普通用户解释**：「拿了超过 5 个月的老币民一共持有多少币。这个数持续上涨说明筹码在向坚定的长期持有者集中，通常是 accumulation（吸筹）；持续下降说明老币民在派发」
**专业模式详情**：显示 LTH Supply 数值与曲线、90d 净变化、变化速率的历史分位、与 STH Supply 对照、数据来源
**历史通常含义**：ΔLTH Supply(90d) 由负转正历史上对应熊市后段的筹码再积累；顶部区域通常伴随 LTH Supply 加速下降
**注意事项**：交易所地址归类错误会造成 LTH/STH 误分；Provider 间差异较大时标记 CONFLICT
**消费方**：Cycle Engine（吸筹/派发核心证据）、Valuation Engine（辅助）

### 6.13 STH Supply

**指标名称**：短期持有者供应量
**英文名**：Short-Term Holder Supply
**分类**：链上指标
**指标代码**：`onchain.sth_supply`
**公式**：STH Supply = Σ(币龄 ≤ 155 天的 UTXO 数量)；派生：STH 成本基础 = STH Realized Cap / STH Supply
**参数**：币龄阈值 = 155 天
**数据依赖**：`onchain_metrics(sth_supply, sth_realized_cap)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / BTC
**分位数方法**：ROLLING_WINDOW(730)（对 STH Supply 变化量）
**普通用户解释**：「最近 5 个月内买入的新持有人一共拿了多少币，以及他们的平均成本是多少。价格跌破新持有人的平均成本时，往往出现恐慌抛售」
**专业模式详情**：显示 STH Supply、STH 成本基础（STH-CB）、价格相对 STH-CB 的位置、90d 变化、数据来源
**历史通常含义**：价格下穿 STH-CB 历史上常触发投降段；牛市回调在 STH-CB 处获得支撑是常见形态
**注意事项**：与 LTH Supply 互补（两者之和 ≈ 流通量），引擎中只取其一作为独立因子避免共线性
**消费方**：Cycle Engine、Risk Engine（关键成本位跌破检测）、Valuation Engine（成本基础维度）

### 6.14 Active Addresses

**指标名称**：活跃地址数
**英文名**：Active Addresses
**分类**：链上指标
**指标代码**：`onchain.active_addresses`
**公式**：当日发生转账（发送或接收）的去重地址数量
**参数**：无
**数据依赖**：`onchain_metrics(active_addresses)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / 个
**分位数方法**：FULL_HISTORY（网络使用规模长期可比）
**普通用户解释**：「今天有多少个地址在用比特币网络转账，类似一个 App 的『日活跃用户数』。活跃度高说明网络使用热度高」
**专业模式详情**：显示当日活跃地址数、7DMA、30DMA、历史分位、与价格背离检测（价格新高而活跃地址未新高）
**历史通常含义**：活跃地址与价格长期正相关；「价格新高 + 活跃地址低迷」的背离历史上出现在周期顶部区域
**注意事项**：不同 Provider 的地址去重与过滤（灰尘交易、OP_RETURN）口径不同；L2/批量支付会低估真实使用量，解读需注明局限
**消费方**：Cycle Engine（网络活跃因子）、Risk Engine（链上异常/背离）

### 6.15 New Addresses

**指标名称**：新增地址数
**英文名**：New Addresses
**分类**：链上指标
**指标代码**：`onchain.new_addresses`
**公式**：当日首次出现在区块链上的地址数量
**参数**：无
**数据依赖**：`onchain_metrics(new_addresses)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / 个
**分位数方法**：FULL_HISTORY
**普通用户解释**：「今天有多少全新用户第一次使用比特币。新增用户暴增说明大量新人进场，历史上常出现在行情最热的阶段」
**专业模式详情**：显示当日新增地址、7DMA、历史分位、Active/New 比率（衡量新用户占网络活动比例）
**历史通常含义**：新增地址创阶段新高常对应散户 FOMO 顶部区域；持续低位对应无人问津的底部
**注意事项**：钱包服务商批量建址会造成尖峰噪音，7DMA 平滑后再消费；同 6.14 的口径差异问题
**消费方**：Cycle Engine（热度/拥挤度因子）、Risk Engine（市场拥挤度）

### 6.16 Transaction Count

**指标名称**：链上交易笔数
**英文名**：Transaction Count
**分类**：链上指标
**指标代码**：`onchain.transaction_count`
**公式**：当日被确认进区块的交易总数；派生：平均手续费 = 当日总手续费 / 交易笔数
**参数**：无
**数据依赖**：`onchain_metrics(tx_count, total_fees)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / 笔
**分位数方法**：FULL_HISTORY
**普通用户解释**：「今天比特币网络一共处理了多少笔转账，反映网络的使用繁忙程度」
**专业模式详情**：显示交易笔数、7DMA、总手续费、平均单笔手续费、历史分位、区块空间使用率（如可得）
**历史通常含义**：交易笔数与手续费同步冲高常出现在市场高潮期（转账需求挤压区块空间）
**注意事项**：闪电网络等 L2 交易不计入链上笔数；Ordinals/Runes 等新型负载改变了交易结构，跨年代对比需注明
**消费方**：Cycle Engine（辅助）、数据面板

### 6.17 Exchange Inflow

**指标名称**：交易所流入量
**英文名**：Exchange Inflow
**分类**：链上指标
**指标代码**：`onchain.exchange_inflow`
**公式**：当日从非交易所地址转入交易所地址的 BTC 总量（交易所地址集合由 Provider 实体聚类定义）
**参数**：无（按日聚合；可按交易所分组）
**数据依赖**：`exchange_flows(inflow)`
**计算周期**：1d（另有 1h 高频版）
**更新频率**：每日 / 每小时
**值类型/单位**：数量 / BTC
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天有多少币被转进了交易所。把币转进交易所通常是为了卖出，流入激增往往意味着卖压将要增加」
**专业模式详情**：显示当日 Inflow、7DMA、历史分位、按交易所分布（如 Provider 支持）、与 Outflow/Netflow 对照、Inflow/Supply 比率
**历史通常含义**：Inflow 尖峰（历史高分位）常先于或伴随大幅下跌；鲸鱼大额流入是重点监控事件
**注意事项**：交易所地址识别完全依赖 Provider 聚类，误差较大；内部钱包整理会造成虚假流入，优先采用已过滤口径
**消费方**：Risk Engine（卖压/链上异常）、Cycle Engine（资金因子）

### 6.18 Exchange Outflow

**指标名称**：交易所流出量
**英文名**：Exchange Outflow
**分类**：链上指标
**指标代码**：`onchain.exchange_outflow`
**公式**：当日从交易所地址转出到非交易所地址的 BTC 总量
**参数**：无
**数据依赖**：`exchange_flows(outflow)`
**计算周期**：1d / 1h
**更新频率**：每日 / 每小时
**值类型/单位**：数量 / BTC
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天有多少币被提出交易所。提币通常意味着转为长期持有，流出增加说明卖压潜在减少」
**专业模式详情**：显示当日 Outflow、7DMA、历史分位、按交易所分布、Outflow/Reserve 比率
**历史通常含义**：持续净流出历史上对应 accumulation 阶段与牛市中期；流出骤停对应需求转弱
**注意事项**：同 6.17（聚类误差、内部转账噪音）
**消费方**：Cycle Engine（吸筹证据）、Risk Engine

### 6.19 Exchange Netflow

**指标名称**：交易所净流量
**英文名**：Exchange Netflow
**分类**：链上指标
**指标代码**：`onchain.exchange_netflow`
**公式**：Netflow = Inflow − Outflow（正值 = 净流入 = 潜在卖压增加）；派生：Exchange Reserve（交易所总余额）及其变化
**参数**：无
**数据依赖**：`exchange_flows(netflow, reserve)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / BTC
**分位数方法**：ROLLING_WINDOW(730)（Netflow）；Exchange Reserve 用趋势斜率而非分位
**普通用户解释**：「交易所里的币今天净增加还是净减少。净增加说明想卖的人多了，净减少说明大家把币提走长期拿着」
**专业模式详情**：显示 Netflow、7D/30D 累计、Exchange Reserve 总量与曲线、Reserve 历史分位（FULL_HISTORY，衡量场内可售筹码的稀缺度）、数据来源
**历史通常含义**：Exchange Reserve 降至多年低位对应「供给紧缩」叙事；Netflow 连续多日为正对应抛压积聚
**注意事项**：Reserve 绝对值受 Provider 覆盖的交易所集合影响，**换 Provider 会造成 Reserve 跳变**——切换时必须记录基线偏移，禁止直接对比不同 Provider 的 Reserve 绝对值
**消费方**：Cycle Engine（资金因子）、Risk Engine（流动性/卖压）、ETF 对照分析

### 6.20 Miner Flow

**指标名称**：矿工资金流
**英文名**：Miner Flow / Miner Position Index
**分类**：链上指标
**指标代码**：`onchain.miner_flow`
**公式**：Miner Outflow = 矿工地址转出的 BTC 总量；Miner Netflow = 矿工转出 − 矿工新增收入（区块奖励+手续费）；MPI = 矿工转出量 / 1 年均值（Miner Position Index）
**参数**：MPI 窗口 = 365
**数据依赖**：`onchain_metrics(miner_flow)`, `exchange_flows(miner_to_exchange)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：数量 / BTC；MPI 为倍数
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「矿工（挖出比特币的人）是在囤币还是在卖币。矿工是市场里成本最透明的卖家，他们集中卖币常被视作短期见顶信号」
**专业模式详情**：显示 Miner Netflow、Miner→Exchange 流量、MPI 数值与历史分位、矿工余额（Miner Reserve）曲线、数据来源
**历史通常含义**：MPI > 2（转出量远超年均）历史上多次对应局部顶部；矿工持续净流入（囤币）对应牛市前中段
**注意事项**：矿工地址识别依赖聚类，2020 年后矿工地域迁移（中国→美国等）造成结构性变化，跨年代对比需谨慎
**消费方**：Cycle Engine（顶部证据）、Risk Engine（链上异常）

### 6.21 Whale Activity

**指标名称**：鲸鱼活动
**英文名**：Whale Activity / Whale Transaction Count
**分类**：链上指标
**指标代码**：`onchain.whale_activity`
**公式**：统计口径（全系统统一）：Whale Tx = 单笔转账价值 ≥ 100 BTC 且非交易所内部地址的交易；Whale Count = 当日 Whale Tx 笔数；Whale Ratio = Whale Tx 笔数 / 总交易笔数；另采集 Whale→Exchange 流量
**参数**：阈值 = 100 BTC（可配置，默认值写入 params）
**数据依赖**：`onchain_metrics(whale_tx_count)`, `exchange_flows(whale_flow)`
**计算周期**：1d / 1h
**更新频率**：每日 / 每小时
**值类型/单位**：数量 / 笔、比率
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「大额转账（巨鲸）今天的活跃程度。巨鲸集中把币转进交易所，可能预示大额卖压；巨鲸互相转币则常是市场酝酿变化的信号」
**专业模式详情**：显示 Whale Count、Whale Ratio、Whale→Exchange 量、当日 Top N 大额交易列表（脱敏地址）、历史分位、阈值口径说明
**历史通常含义**：鲸鱼交易所流入激增历史上先于多次急跌；鲸鱼活跃度长期低迷对应市场冷清期
**注意事项**：阈值口径必须固定并在展示中注明（不同媒体「鲸鱼」定义混乱）；交易所内部转账必须剔除，否则严重误判
**消费方**：Risk Engine（链上异常/大额卖压预警）、事件时间线（极端鲸鱼事件）

### 6.22 HODL Waves

**指标名称**：持币时间分布
**英文名**：HODL Waves
**分类**：链上指标
**指标代码**：`onchain.hodl_waves`
**公式**：按币龄带宽分组统计供应占比；标准带宽：<1d, 1-2d, 2-3d, 3-5d, 5-7d, 1-2w, 2-4w, 1-3m, 3-6m, 6-12m, 1-2y, 2-3y, 3-5y, 5-7y, 7-10y, >10y；各带宽供应量 / 流通量 = 占比%
**参数**：带宽表（固定行业标准口径）
**数据依赖**：`onchain_metrics(hodl_waves)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %（结构占比）
**分位数方法**：对关键聚合组用 FULL_HISTORY：短期组（<6m 占比）、长期组（>1y 占比）
**普通用户解释**：「把市场上的币按『被持有了多久』分堆。老币（拿了一年以上）占比越高，说明筹码越稳定；新币（几天内换手）占比冲高，说明投机热度高」
**专业模式详情**：显示完整 16 档带宽占比堆叠图数据、短期组/长期组占比及其历史分位、带宽口径说明、数据来源
**历史通常含义**：>1y 供应占比达到历史高位对应熊市底部（筹码完成向长期持有者转移）；<1w 占比冲高对应顶部换手狂热
**注意事项**：带宽口径各 Provider 略有差异，必须采用统一标准带宽并在专业模式注明实际口径；丢失币（Satoshi 时代 UTXO）天然推高 >7y 组占比，跨年代对比需意识到该结构漂移
**消费方**：Cycle Engine（筹码结构证据）、Valuation Engine（辅助）

### 6.23 Supply Last Active

**指标名称**：最后活跃时间供应分布
**英文名**：Supply Last Active
**分类**：链上指标
**指标代码**：`onchain.supply_last_active`
**公式**：对每个 UTXO 计算距今最后活跃天数（now − last_active），按时间桶（1d/1w/1m/3m/6m/1y/2y/5y+）统计供应量分布；派生：Dormant Supply Ratio = 最后活跃 > 2y 的供应占比
**参数**：时间桶表（固定口径）
**数据依赖**：`onchain_metrics(supply_last_active)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %
**分位数方法**：FULL_HISTORY（对 Dormant Supply Ratio）
**普通用户解释**：「有多少币已经很久没动过了。沉睡的币越多，说明持有人越不愿意卖；老币突然苏醒（被转走）往往是大行情的信号」
**专业模式详情**：显示各时间桶供应分布、Dormant Supply Ratio 及其历史分位、「沉睡币苏醒」事件流（>2y 未动的 UTXO 当日移动量）
**历史通常含义**：沉睡币大规模苏醒历史上多次出现在顶部派发前；沉睡占比持续上升对应熊市积累期
**注意事项**：与 HODL Waves（按币龄）口径不同——Supply Last Active 按「距今天数」，两者不可混用；Satoshi 时代丢失币永久沉睡，占比存在结构性底噪
**消费方**：Cycle Engine（苏醒预警）、Risk Engine（链上异常事件）

### 6.24 Coin Days Destroyed

**指标名称**：币天销毁
**英文名**：Coin Days Destroyed (CDD)
**分类**：链上指标
**指标代码**：`onchain.cdd`
**公式**：单笔 CDD = 转账 BTC 数量 × 该币休眠天数；日 CDD = Σ(当日全部花费 UTXO 的 CDD)；变体：Supply-Adjusted CDD = CDD / 流通量 × 1e6；LTH-CDD 仅统计币龄 > 155 天的 UTXO
**参数**：变体口径（raw / supply-adjusted / LTH）
**数据依赖**：`onchain_metrics(cdd, supply_adjusted_cdd, lth_cdd)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：币·天（raw）；无量纲（adjusted）
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天被花掉的『老币』有多老、有多少。老币被大量移动销毁，说明拿了很久的人开始行动了——历史上这常是重要行情的前兆」
**专业模式详情**：显示日 CDD、Supply-Adjusted CDD、LTH-CDD（30d 累计）及各自历史分位、当日 Top CDD 事件、变体口径说明
**历史通常含义**：LTH-CDD(30d) 冲至历史高分位对应长期持有者派发（顶部区域信号）；持续低分位对应筹码锁定
**注意事项**：单日原始 CDD 噪音极大（一笔沉睡 10 年的转账即可爆表），引擎只消费 30d 累计与 Supply-Adjusted 变体；交易所内部整理会造成假性 CDD 尖峰，需 Provider 过滤口径
**消费方**：Cycle Engine（派发证据）、Risk Engine（链上异常）

### 6.25 Dormancy

**指标名称**：币休眠度
**英文名**：Dormancy
**分类**：链上指标
**指标代码**：`onchain.dormancy`
**公式**：Dormancy = 当日总 CDD / 当日总销毁币数（= 当日被花费币的平均休眠天数）
**参数**：无
**数据依赖**：`onchain_metrics(dormancy)`（或由 CDD 与销毁量派生）
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：天数 / days
**分位数方法**：ROLLING_WINDOW(730)
**普通用户解释**：「今天卖币的人手里的币平均『睡了多久』。平均休眠天数突然变长，说明开始有老币民出手卖币了」
**专业模式详情**：显示当日 Dormancy、7DMA、历史分位、与 CDD 的区别说明（CDD 衡量总量，Dormancy 衡量平均币龄）
**历史通常含义**：Dormancy 飙升历史上对应长期持有者开始派发的早期阶段；低位平稳对应日常换手
**注意事项**：与 CDD 高度相关，引擎中二选一作为独立因子（默认 Dormancy，因其对单笔大额事件敏感度较低）
**消费方**：Cycle Engine（派发辅助证据）、Risk Engine（链上异常）

---

## 7. 衍生品指标（DERIV）

> 数据来源：`derivatives` 表（DerivativesService 聚合 Binance / OKX / Bybit / Deribit 等多交易所永续与交割合约数据）。
> **通用注意事项**：各交易所口径不同（Funding 结算周期、OI 计价币种、清算估算方法），系统按交易所分别存储，展示时提供「全市场聚合值（USD 计）+ 分交易所明细」；聚合口径为各交易所 USD 计值求和（OI/Liquidation/Volume）或 OI 加权均值（Funding）。

### 7.1 Funding Rate

**指标名称**：资金费率
**英文名**：Funding Rate
**分类**：衍生品指标
**指标代码**：`deriv.funding_rate`
**公式**：永续合约多空双方周期性互付的费率：Funding Rate = clamp(Premium Index + clamp(Interest Rate − Premium Index, ±0.05%), ±上限)（各交易所公式细节不同，直接采集交易所公布值）；系统另计算年化 Funding = Rate × 每日结算次数 × 365
**参数**：结算周期（Binance/OKX/Bybit 默认 8h，部分市场 4h/1h）；采集频率 5m
**数据依赖**：`derivatives(funding_rate, next_funding_time)`，按交易所分存
**计算周期**：5m 快照 / 1d 聚合（日均、结算值）
**更新频率**：每 5 分钟（预测值）+ 每结算周期（实际结算值）
**值类型/单位**：百分比 / %（单期）
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「合约市场里多头和空头谁更急。费率为正且很高，说明做多的人拥挤、愿意付费维持仓位，市场偏热；费率深度为负说明做空拥挤，有发生『逼空』反弹的风险」
**专业模式详情**：显示各交易所当前/预测 Funding、全市场加权均值、年化值、历史分位、连续为正/为负天数、极端 Funding 事件列表（历史高分位）
**历史通常含义**：年化 Funding 持续 >30% 对应多头拥挤（历史上常先于回调）；深度负 Funding（年化 <−20%）对应空头拥挤，历史上多次触发逼空反弹
**注意事项**：不同结算周期（8h vs 4h）的费率不可直接相加，年化后才能对比；Funding 是情绪温度计而非方向预言，极端负值可持续数周
**消费方**：Risk Engine（Funding 风险核心输入）、Cycle Engine（情绪因子）、Market Regime（Derivative State）

### 7.2 Open Interest

**指标名称**：未平仓合约量
**英文名**：Open Interest
**分类**：衍生品指标
**指标代码**：`deriv.open_interest`
**公式**：OI = 未平仓合约总数（多单边计）；USD 计 OI = OI(BTC) × 标记价格；派生：OI/Market Cap 比率（杠杆率代理）、OI 变化率
**参数**：按交易所×合约类型（永续/交割）分维度
**数据依赖**：`derivatives(open_interest, open_interest_usd)`
**计算周期**：5m 快照 / 1h / 1d 聚合
**更新频率**：每 5 分钟
**值类型/单位**：BTC / USD
**分位数方法**：OI 绝对值用 EXPANDING_FROM(2020-01-01)（市场结构增长，全历史分位失真）；OI/MCap 与 OI 30d 变化率用 ROLLING_WINDOW(365)
**普通用户解释**：「合约市场里还有多少仓位没有平掉，衡量杠杆资金的总规模。OI 快速上升说明杠杆资金涌入，一旦行情反向，连锁清算的风险也在累积」
**专业模式详情**：显示全市场 OI(USD)、分交易所 OI、OI/Market Cap、OI 24h/7d 变化率及历史分位、OI 与价格背离检测（价格新高 + OI 下降 = 动能衰减）
**历史通常含义**：OI/MCap 创阶段新高 + Funding 高位，历史上对应杠杆过热；OI 骤降（去杠杆）常伴随大幅波动
**注意事项**：交易所对 OI 的计算口径（单/双边）已统一为单边；OI 绝对值受市场规模增长影响，禁止直接用全历史分位判过热，必须用 OI/MCap
**消费方**：Risk Engine（OI 风险）、Cycle Engine（杠杆因子）、Market Regime

### 7.3 Liquidation

**指标名称**：清算量（多头/空头）
**英文名**：Liquidation (Long / Short)
**分类**：衍生品指标
**指标代码**：`deriv.liquidation`
**公式**：采集各交易所强平订单流；Long Liquidation = 多头被强平金额(USD)，Short Liquidation = 空头被强平金额(USD)；净清算 = Short Liq − Long Liq
**参数**：聚合粒度 1h / 1d；极端事件阈值：1h 全市场清算 > $100M（可配置）
**数据依赖**：`derivatives(liquidation_long, liquidation_short)`
**计算周期**：1h / 1d（另存 5m 细粒度供实时页）
**更新频率**：每 5 分钟增量
**值类型/单位**：金额 / USD
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「有多少加杠杆的人被强制平仓了。多头被大量清算说明杠杆多头崩了（行情下跌中被强卖）；空头被大量清算说明逼空行情正在发生」
**专业模式详情**：显示 1h/24h 多空清算量、净清算、分交易所明细、极端清算事件列表（历史高分位）、清算热力图数据（价格×清算量）
**历史通常含义**：单日全市场清算 > $500M 历史上对应恐慌底部或逼空高潮；连续小额清算对应阴跌去杠杆
**注意事项**：交易所披露口径不一（部分仅披露最近强平单），数据为**估算值**，质量状态默认 ESTIMATED，多源交叉后升 VERIFIED
**消费方**：Risk Engine（Liquidation 风险）、事件时间线（极端清算标记）、Cycle Engine（投降信号辅助）

### 7.4 Long/Short Ratio

**指标名称**：多空比
**英文名**：Long/Short Ratio
**分类**：衍生品指标
**指标代码**：`deriv.long_short_ratio`
**公式**：LSR_accounts = 多头账户数 / 空头账户数（ Binance 口径：全市场多空账户比）；LSR_positions = 多头持仓量 / 空头持仓量；另采集大户账户比与大户持仓比（Top Trader LSR，如 Provider 支持）
**参数**：采集粒度 5m；聚合 1h / 1d
**数据依赖**：`derivatives(long_short_ratio)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：倍数 / ratio
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「做多的账户和做空的账户谁多。散户一边倒做多时反而要警惕（反向指标）；大户与散户方向分歧时，历史上大户胜率更高」
**专业模式详情**：显示全市场账户比、大户账户比、大户持仓比、各自历史分位、背离标记（散户多 vs 大户空）
**历史通常含义**：散户 LSR 极端高位（历史高分位）常对应短期回调前夜；极端低位常伴随底部反弹（拥挤空头被清洗）
**注意事项**：账户比不等于仓位比（一个空头大户可能抵消万个散户）；各交易所披露字段不同，缺字段时对应维度标记不可用而非估算
**消费方**：Risk Engine（市场拥挤度）、Cycle Engine（情绪因子）

### 7.5 Basis / Premium

**指标名称**：基差 / 升水
**英文名**：Basis / Premium (Annualized)
**分类**：衍生品指标
**指标代码**：`deriv.basis`
**公式**：Basis = 交割合约价格 − 现货指数价格；年化基差 = Basis / 现货价 × (365 / 剩余到期天数) × 100%；永续溢价 = (永续价格 − 指数价格) / 指数价格
**参数**：按到期日（当周/次周/季度）分维度
**数据依赖**：`derivatives(futures_price, spot_index_price)`
**计算周期**：1h / 1d
**更新频率**：每小时（结算值每日）
**值类型/单位**：百分比 / %（年化）
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「期货价格比现货贵多少（换算成年化收益率）。这个『差价收益』反映了机构资金套利的热度：差价越高，说明机构越愿意买入期货对冲，市场情绪越乐观」
**专业模式详情**：显示各到期日合约的 Basis、年化基差曲线、历史分位、与 Funding 的一致性检验（两者同为多头情绪代理）
**历史通常含义**：季度合约年化基差 >20% 历史上对应牛市情绪高涨；基差转负（贴水）历史上对应极度悲观（如 2022 熊市）
**注意事项**：临近到期时年化基差数值失真（分母趋小），展示时过滤剩余天数 <2 的合约
**消费方**：Risk Engine（杠杆情绪）、Cycle Engine（机构情绪因子）、Market Regime（Derivative State）

### 7.6 Taker Buy/Sell Volume

**指标名称**：主动买卖量
**英文名**：Taker Buy / Taker Sell Volume
**分类**：衍生品指标
**指标代码**：`deriv.taker_volume`
**公式**：Taker Buy Volume = 以卖一价成交（主动买入）的量；Taker Sell Volume = 以买一价成交（主动卖出）的量；Taker Ratio = Buy / (Buy + Sell)；派生 Taker Imbalance = (Buy − Sell) / (Buy + Sell)
**参数**：采集粒度 1m（合约）；聚合 1h / 1d
**数据依赖**：`derivatives(taker_buy_volume, taker_sell_volume)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：BTC / USD；Taker Ratio 为 0–1
**分位数方法**：Taker Imbalance 用 ROLLING_WINDOW(90)（快变量）
**普通用户解释**：「主动砸盘卖出的量多，还是主动扫货买入的量多。连续多日主动买入占优，说明市场存在真实的现货/合约需求」
**专业模式详情**：显示 Buy/Sell 量、Taker Ratio、Imbalance 曲线与历史分位、与价格背离检测（价格涨 + Taker Sell 占优 = 上涨质量存疑）
**历史通常含义**：Taker Buy 持续占优对应需求驱动的健康上涨；价格新高而 Taker Imbalance 转负历史上常先于回调
**注意事项**：刷量/对敲会污染 Taker 数据，异常值（单日量超历史分位 99.9% 且价格无波动）应标记质量异常
**消费方**：Cycle Engine（资金因子）、Risk Engine（趋势质量）、Market Regime（Derivative State）

### 7.7 CVD

**指标名称**：累计成交量差
**英文名**：Cumulative Volume Delta
**分类**：衍生品指标
**指标代码**：`deriv.cvd`
**公式**：Volume Delta(t) = Taker Buy(t) − Taker Sell(t)；CVD = Σ Volume Delta（自锚定点累计，默认每日 UTC 00:00 重置，另提供周/月锚定）
**参数**：锚定 ∈ {day, week, month}；数据粒度 1m
**数据依赖**：`derivatives(taker_buy_volume, taker_sell_volume)`（1m）
**计算周期**：1m 累计 / 1h / 1d 快照
**更新频率**：每分钟
**值类型/单位**：BTC / USD
**分位数方法**：ROLLING_WINDOW(90)（对日内 CVD 斜率）
**普通用户解释**：「把主动买入减主动卖出的差额累加起来，看资金是在持续净流入还是净流出。CVD 与价格背离（价格涨但 CVD 跌）是重要的警示信号」
**专业模式详情**：显示日/周/月 CVD 曲线、CVD 斜率、与价格背离检测结果（滚动相关性）、分交易所 CVD（现货 vs 永续分开）
**历史通常含义**：价格上涨 + CVD 同步上升 = 需求驱动；价格新高 + CVD 背离历史上多次先于顶部
**注意事项**：CVD 依赖 Taker 方向判定，不同交易所规则微差；必须区分现货 CVD 与永续 CVD（两者背离本身是信号：现货弱/合约强 = 杠杆驱动）
**消费方**：Risk Engine（趋势质量）、Cycle Engine（资金因子）、行情页

### 7.8 Derivatives Volume

**指标名称**：衍生品成交量
**英文名**：Derivatives Volume (Perp / Futures)
**分类**：衍生品指标
**指标代码**：`deriv.volume`
**公式**：各交易所合约成交量（USD 计）求和；派生：成交量/OI 比率（换手强度）、衍生品/现货成交量比（杠杆主导度）
**参数**：聚合粒度 1h / 1d
**数据依赖**：`derivatives(volume_usd)`, `market_prices(volume)`（现货对照）
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：金额 / USD；比率为无量纲
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「合约市场今天的交易活跃度，以及合约交易量是现货的多少倍。倍数越高，说明市场越是杠杆资金主导，波动会更剧烈」
**专业模式详情**：显示分交易所成交量、全市场聚合、Deriv/Spot 比率及其历史分位、成交量/OI
**历史通常含义**：Deriv/Spot 比率创阶段新高对应杠杆主导的过热市场；比率低位对应现货主导的健康行情
**注意事项**：部分交易所存在刷量嫌疑，聚合时按 Provider 可信度加权，可疑量单独标记
**消费方**：Risk Engine（杠杆风险）、Market Regime（Derivative State）

### 7.9 Estimated Liquidation Heatmap

**指标名称**：预估清算热力图
**英文名**：Estimated Liquidation Levels
**分类**：衍生品指标
**指标代码**：`deriv.liq_heatmap`
**公式**：基于 OI 分布与常见杠杆档位（10x/25x/50x/100x）估算各价格区间的潜在清算量：EstLiq(P±Δ) ≈ OI_档位 × 强平距离；强平价格 ≈ 开仓价 × (1 ∓ 1/杠杆)
**参数**：杠杆档位表、价格步长（0.5%）、维护保证金率假设（按交易所公布值）
**数据依赖**：`derivatives(open_interest)`, 历史价格分布
**计算周期**：1h
**更新频率**：每小时
**值类型/单位**：金额 / USD（按价格档位）
**分位数方法**：不适用（结构分布数据）
**普通用户解释**：「估算在哪些价位堆积了大量杠杆仓位，价格碰到这些位置可能触发连锁强平，放大行情。类似『地图上标出地雷密集区』」
**专业模式详情**：显示完整估算方法、杠杆档位假设、OI 分布数据、上方/下方最近清算密集区价格与估算金额、与实际清算数据的历史吻合度
**历史通常含义**：价格向清算密集区方向突破时，行情常被放大（清算驱动的自我强化）
**注意事项**：**这是估算值而非真实数据**（交易所不公开完整仓位分布），quality_status 固定为 ESTIMATED，普通模式必须明确标注「估算」；禁止用于精确价位预测，只用于风险区域提示
**消费方**：Risk Engine（清算连锁风险）、行情页（专业模式）

---

## 8. 期权指标（OPT）

> 数据来源：`options_data` 表（OptionsProvider，主力数据源为 Deribit，占 BTC 期权市场绝大部分份额）。
> **通用注意事项**：BTC 期权市场自 2024 年起才有足够深度（美国上市期权、IBIT 期权等），历史分位样本较短，统一采用 EXPANDING_FROM(2022-01-01)；期权指标为日度快照（UTC 08:00 Deribit 结算后）+ 盘中小时级更新。

### 8.1 Options OI / Volume

**指标名称**：期权未平仓量与成交量
**英文名**：Options Open Interest & Volume
**分类**：期权指标
**指标代码**：`opt.open_interest` / `opt.volume`
**公式**：OI = 未平仓合约数（按 Call/Put、到期日、行权价分维度，BTC 张数计）；Max Pain = 使全部未平仓 Call+Put 内在价值总和最小的到期结算价；另输出 PCR_OI = Put OI / Call OI
**参数**：维度：expiry × strike × type
**数据依赖**：`options_data(open_interest, volume, strike, expiry, type)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：BTC 张数 / USD
**分位数方法**：EXPANDING_FROM(2022-01-01)
**普通用户解释**：「期权市场里押注的总规模有多大，以及到期日哪个价格会让最多的期权买家亏钱（Max Pain，到期日常被『钉』在这个价位附近）」
**专业模式详情**：显示 OI 按到期日/行权价分布（OI Wall）、Max Pain 数值与计算过程、Call/Put OI 与成交量、历史分位
**历史通常含义**：大型到期日（季度交割）前后波动常受 Max Pain 与 OI Wall 钉扎效应影响；OI 创新高对应机构对冲/投机需求旺盛
**注意事项**：Max Pain 是统计现象而非因果规律，展示时必须注明「历史参考，不构成必然」；美国上市期权（CME/IBIT）数据覆盖不全时分维度标记
**消费方**：Risk Engine（到期日风险）、Market Regime（Derivative State）、事件时间线（大型交割日）

### 8.2 Implied Volatility

**指标名称**：隐含波动率
**英文名**：Implied Volatility (IV / DVOL)
**分类**：期权指标
**指标代码**：`opt.iv`
**公式**：IV = 使 Black-Scholes（Deribit 对 BTC 用 Black-76）理论价 = 市场价的波动率参数；DVOL = Deribit BTC 波动率指数（类 VIX，30 天期，按 OI 加权一篮子期权 IV）；IV Term Structure = 不同到期日 ATM IV 曲线；IV−HV Spread = DVOL − HV30
**参数**：ATM 定义：|strike − forward| 最小；期限：7d/30d/60d/90d/180d
**数据依赖**：`options_data(iv, dvol)`, `candles(close)`（HV 对照）
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：百分比 / %（年化）
**分位数方法**：EXPANDING_FROM(2022-01-01)（DVOL 自 2022 有可靠连续序列）
**普通用户解释**：「期权价格里包含的『市场预期未来波动幅度』。IV 很高说明市场在为剧烈波动定价（通常伴随恐慌或重大事件）；IV 很低说明市场预期平静」
**专业模式详情**：显示 DVOL、期限结构曲线（contango/backwardation）、IV−HV Spread、各到期日 ATM IV、历史分位
**历史通常含义**：DVOL > 80% 历史上对应恐慌期（如 2020-03、2022-06）；DVOL < 40% 对应平静期，常是波动率买入机会（仅展示事实，不构成建议）；期限结构倒挂（短端 > 长端）对应即期恐慌
**注意事项**：重大事件（FOMC、ETF 审批、大选）前 IV 系统性抬升，解读必须对照事件日历；不同 Provider 的 DVOL 复制算法有微差
**消费方**：Risk Engine（波动风险）、Valuation Engine（辅助）、Cycle Engine（恐慌因子）

### 8.3 Put/Call Ratio

**指标名称**：看跌/看涨期权比
**英文名**：Put/Call Ratio
**分类**：期权指标
**指标代码**：`opt.put_call_ratio`
**公式**：PCR_volume = Put 成交量 / Call 成交量；PCR_oi = Put OI / Call OI
**参数**：聚合：全市场 / 按到期日
**数据依赖**：`options_data(volume, open_interest, type)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：倍数 / ratio
**分位数方法**：EXPANDING_FROM(2022-01-01)
**普通用户解释**：「买『下跌保险』（Put）的人和买『上涨彩票』（Call）的人的比例。比例异常高说明市场极度悲观（历史上反而常接近底部），异常低说明极度乐观」
**专业模式详情**：显示 PCR_volume、PCR_oi、各自历史分位、时间序列曲线、与价格背离标记
**历史通常含义**：PCR 历史高分位（>90%）对应恐慌情绪顶点，常为反向底部信号；历史低分位对应自满情绪
**注意事项**：PCR 受机构对冲策略结构性影响（牛市中也持续买 Put 保护），绝对值上升不等于悲观加剧，必须用分位而非绝对阈值
**消费方**：Risk Engine（情绪风险）、Cycle Engine（情绪因子）、Market Regime（Sentiment/Derivative State）

### 8.4 Skew

**指标名称**：波动率偏斜
**英文名**：Volatility Skew (25D Risk Reversal)
**分类**：期权指标
**指标代码**：`opt.skew`
**公式**：25D Risk Reversal = IV(25-Delta Call) − IV(25-Delta Put)（负值 = Put 更贵 = 下跌保护需求高）；另输出 10D RR 与全行权价 IV 微笑曲线
**参数**：Delta 档位 25D/10D；期限 30d 为主
**数据依赖**：`options_data(iv, delta, strike, expiry)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：百分比 / vol points
**分位数方法**：EXPANDING_FROM(2022-01-01)
**普通用户解释**：「市场更愿意为『暴跌保险』付钱，还是为『暴涨彩票』付钱。保险越贵，说明专业资金越担心下跌」
**专业模式详情**：显示 25D RR（30d）、10D RR、IV 微笑曲线、历史分位、Skew 突变事件
**历史通常含义**：RR 深度负值（Put 溢价极高）对应机构恐慌对冲高峰，历史上常接近阶段底；RR 转正（Call 溢价）对应 FOMO 顶部情绪
**注意事项**：BTC 期权 Skew 长期结构性偏负（崩盘保护需求），解读必须用分位而非绝对符号
**消费方**：Risk Engine（机构情绪）、Cycle Engine（顶部/底部辅助证据）

### 8.5 Gamma Exposure

**指标名称**：Gamma 暴露
**英文名**：Gamma Exposure (GEX)
**分类**：期权指标
**指标代码**：`opt.gamma_exposure`
**公式**：GEX(P) = Σ_k [Gamma_k × OI_k × ContractSize × Spot² × 0.01 × Side_k]，Side：假设做市商为 Call 空头/Put 多头（标准 SqueezeMetrics 口径，可配置）；派生：GEX 零点（Gamma Flip Level）、正/负 Gamma 区间
**参数**：做市商方向假设（默认 Call short / Put long，可在专业模式切换对照）；按到期日过滤（默认剔除 <2 天）
**数据依赖**：`options_data(gamma, open_interest, strike, expiry)`, `market_prices(close)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：USD / 1% 价格变动
**分位数方法**：EXPANDING_FROM(2024-01-01)（GEX 可靠历史较短）
**普通用户解释**：「估算期权做市商的对冲行为会『压住』还是『放大』价格波动。正 Gamma 环境下做市商高抛低吸、波动被抑制；负 Gamma 环境下做市商追涨杀跌、波动被放大」
**专业模式详情**：显示 GEX 总值、Gamma Flip 价位、按行权价的 GEX 分布（Call/Put Wall）、做市商方向假设说明、估算局限性声明
**历史通常含义**：价格处于负 Gamma 区间时，历史波动率倾向放大；大型 Call Wall 常构成短期阻力（钉扎效应）
**注意事项**：**GEX 是模型估算值**，做市商真实仓位方向不可观测，quality_status 固定为 ESTIMATED；普通模式只展示「波动抑制/放大环境」定性结论，不展示具体数值
**消费方**：Risk Engine（波动风险调节）、行情页（专业模式）

---

## 9. 宏观指标（MACRO）

> 数据来源：`macro_series` 表（MacroService 聚合 FRED / 官方统计局 / 第三方宏观数据 Provider）。
> **通用注意事项（本节全部指标强制遵守）**：
> 1. 宏观数据必须区分三个时间（需求文档第二十五节）：`observation_date`（数据所属期间，如 CPI 对应月份）、`release_date`（官方首次发布时间）、`revision_date`（修订时间）；**回测与历史回放只能使用 release_date <= 模拟时刻的首发值**，禁止使用修订后数据；
> 2. 发布日历：每个宏观指标元数据必须包含发布频率与典型发布日（如 CPI 为月度、次月中旬发布），供前端「即将发布」提醒与回测事件模拟；
> 3. 同比/环比：CPI、PCE 等同时存储 YoY 与 MoM，引擎默认消费 YoY。

### 9.1 DXY

**指标名称**：美元指数
**英文名**：US Dollar Index (DXY)
**分类**：宏观指标
**指标代码**：`macro.dxy`
**公式**：DXY = 50.14348112 × EURUSD^(−0.576) × USDJPY^(0.136) × GBPUSD^(0.119) × USDCAD^(0.091) × USDSEK^(0.042) × USDCHF^(0.036)（ICE 官方权重，直接采集发布值）
**参数**：无
**数据依赖**：`macro_series(series_code='DXY')`
**计算周期**：1d（另采集盘中小时价）
**更新频率**：每日
**值类型/单位**：指数 / 点
**分位数方法**：ROLLING_WINDOW(3650)（10 年滚动，避免全历史早期高基数失真）
**普通用户解释**：「美元相对一篮子主要货币的强弱。美元走强历史上往往对 BTC 等风险资产不利，美元走弱则相反」
**专业模式详情**：显示 DXY 当前值、趋势（50DMA/200DMA）、历史分位、与 BTC 的滚动相关系数（90d）、数据来源与 release 时间
**历史通常含义**：DXY 快速上行（如 2022 加息周期）历史上伴随 BTC 深跌；DXY 趋势性走弱阶段 BTC 表现通常较好；相关系数并不稳定，必须动态计算展示
**注意事项**：相关关系是历史统计而非因果定律，展示时必须附滚动相关系数而非断言；DXY 与 BTC 同涨的阶段性背离并不罕见
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）、宏观页

### 9.2 Fed Rate

**指标名称**：联邦基金目标利率
**英文名**：Fed Funds Target Rate
**分类**：宏观指标
**指标代码**：`macro.fed_rate`
**公式**：采集 FOMC 目标区间（上限/下限/中值）；派生：政策周期状态（加息/暂停/降息，由连续变动方向判定）、与上期变动幅度(bp)
**参数**：无
**数据依赖**：`macro_series(series_code='FEDFUNDS'/'DFEDTARU'/'DFEDTARL')`
**计算周期**：事件驱动（FOMC 决议日）
**更新频率**：每日检查 + FOMC 日历事件触发
**值类型/单位**：百分比 / %
**分位数方法**：不适用（事件型指标，引擎消费政策周期状态与变动方向）
**普通用户解释**：「美联储把美元的『基准利息』定在多少。加息意味着收紧资金，历史上对 BTC 不利；降息意味着放水，对风险资产友好」
**专业模式详情**：显示当前目标区间、历史利率路径图、FOMC 会议日历（含市场预期）、政策周期状态判定、数据来源与发布/修订时间
**历史通常含义**：加息周期中后段历史上对应 BTC 熊市；降息/QE 周期对应流动性宽松环境；但 2024 年后 BTC 与利率的单一相关性下降，必须结合 Global Liquidity 综合判断
**注意事项**：FOMC 决议属于**事件冲击**，事件时间线必须标记；市场预期（CME FedWatch 类数据如可得）与实际决议的差值才是冲击源
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）、Cycle Engine（宏观背景）、事件时间线

### 9.3 Treasury Yields (2Y / 10Y)

**指标名称**：美债收益率（2 年 / 10 年）
**英文名**：US Treasury Yields 2Y / 10Y
**分类**：宏观指标
**指标代码**：`macro.yield_2y` / `macro.yield_10y`
**公式**：直接采集日度收益率；派生：10Y−2Y 利差（Yield Curve Spread）、2s10s 倒挂状态、10Y 变化速率(bp/周)
**参数**：无
**数据依赖**：`macro_series(series_code='DGS2'/'DGS10')`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %；利差为 bp
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「把钱借给美国政府 2 年/10 年能拿到的无风险利息。这是全球资产定价的『锚』：无风险利息越高，资金越不愿意冒险进入 BTC 这类资产」
**专业模式详情**：显示 2Y/10Y 当前值、2s10s 利差曲线、倒挂状态与历史倒挂区间标记、与 BTC 滚动相关性
**历史通常含义**：2s10s 倒挂历史上是衰退先行信号（领先 6–24 个月）；10Y 快速上行阶段风险资产普遍承压
**注意事项**：倒挂→衰退的时滞极不稳定，禁止用于精确择时；只作为宏观背景风险因子（低权重）
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）

### 9.4 Real Yield

**指标名称**：实际收益率
**英文名**：Real Yield (10Y TIPS)
**分类**：宏观指标
**指标代码**：`macro.real_yield`
**公式**：Real Yield = 10Y 名义收益率 − 通胀预期（采集 10Y TIPS 收益率 DFII10，即市场实际利率）
**参数**：无
**数据依赖**：`macro_series(series_code='DFII10')`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：百分比 / %
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「扣除通胀后，持有无风险债券能获得的真实回报。真实回报越高，黄金、BTC 这类『无利息资产』的吸引力越低」
**专业模式详情**：显示 DFII10 当前值、曲线、历史分位、与 BTC 滚动相关性、数据来源与修订记录
**历史通常含义**：实际收益率快速上行（如 2022）历史上对应 BTC 深跌；实际收益率转负阶段对应 2020–2021 式流动性牛市环境
**注意事项**：与 BTC 的相关性在不同宏观机制下会失效（如 2024 后 BTC 受 ETF 资金流主导阶段），展示必须附滚动相关系数
**消费方**：Risk Engine（宏观风险）、Valuation Engine（宏观流动性背景，低权重）

### 9.5 CPI

**指标名称**：消费者价格指数
**英文名**：Consumer Price Index (CPI)
**分类**：宏观指标
**指标代码**：`macro.cpi`
**公式**：采集 CPI 指数值、CPI YoY%、Core CPI YoY%（剔除食品能源）；发布时记录首发值，后续修订另存 revision 记录
**参数**：YoY / MoM / Core 三口径全存
**数据依赖**：`macro_series(series_code='CPIAUCSL'/'CPILFESL')`
**计算周期**：月度（observation_date 为月初，release_date 为次月中旬）
**更新频率**：发布日事件触发 + 每日检查修订
**值类型/单位**：百分比 / %（YoY）
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「美国物价涨得有多快。通胀数据公布时市场常剧烈波动：通胀超预期→加息预期升温→对 BTC 短期利空，反之亦然」
**专业模式详情**：显示 CPI/Core CPI YoY 曲线、市场预期 vs 实际公布（如可得）、release_date/revision_date 完整记录、历史分位、下次发布日期
**历史通常含义**：CPI 峰值（2022-06 的 9.1%）历史上对应 BTC 阶段性恐慌底部区域；CPI 回落至目标区间对应宏观环境转友好
**注意事项**：**回测中必须用首发值（advance release）而非修订值**；CPI 公布日属于事件冲击，事件时间线必须标记，预测系统在发布窗口应主动降低置信度
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）、事件时间线

### 9.6 PCE

**指标名称**：个人消费支出价格指数
**英文名**：PCE Price Index
**分类**：宏观指标
**指标代码**：`macro.pce`
**公式**：采集 Headline PCE YoY% 与 Core PCE YoY%（美联储首选通胀指标）
**参数**：YoY / MoM / Core
**数据依赖**：`macro_series(series_code='PCEPI'/'PCEPILFE')`
**计算周期**：月度（release 为次月底）
**更新频率**：发布日事件触发
**值类型/单位**：百分比 / %
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「美联储最看重的通胀指标。它比 CPI 更影响加息/降息决策，因此对市场的中期影响更大」
**专业模式详情**：显示 PCE/Core PCE YoY 曲线、与 CPI 的剪刀差、release/revision 记录、历史分位
**历史通常含义**：Core PCE 趋势性回落阶段对应紧缩预期缓解；超预期反弹触发风险资产回调
**注意事项**：同 CPI（首发值/修订值区分、事件标记）
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）

### 9.7 Unemployment Rate

**指标名称**：失业率
**英文名**：Unemployment Rate (U3)
**分类**：宏观指标
**指标代码**：`macro.unemployment`
**公式**：采集 U3 失业率（%，季调）；派生：Sahm Rule 触发状态（失业率 3 个月均值较过去 12 个月低点上升 ≥ 0.5pp）
**参数**：无
**数据依赖**：`macro_series(series_code='UNRATE')`
**计算周期**：月度（随就业报告发布，通常次月首个周五）
**更新频率**：发布日事件触发
**值类型/单位**：百分比 / %
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「美国找不到工作的人的比例。失业率上升意味着经济变差，美联储更可能降息救市——历史上降息周期对 BTC 中期偏友好」
**专业模式详情**：显示 U3 曲线、Sahm Rule 状态与触发历史、与 NFP 联动展示、release/revision 记录
**历史通常含义**：失业率低位平稳对应经济软着陆；Sahm Rule 触发历史上均先于或伴随衰退
**注意事项**：失业率对 BTC 的影响方向取决于市场处于「坏消息是坏消息（衰退恐慌）」还是「坏消息是好消息（宽松预期）」机制，展示时不下单向结论
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）

### 9.8 NFP

**指标名称**：非农就业人数
**英文名**：Non-Farm Payrolls (NFP)
**分类**：宏观指标
**指标代码**：`macro.nfp`
**公式**：采集月度新增非农就业（千人，季调）；派生：3 个月均值、公布值 vs 预期差（如可得）
**参数**：无
**数据依赖**：`macro_series(series_code='PAYEMS')`
**计算周期**：月度
**更新频率**：发布日事件触发（次月首个周五）
**值类型/单位**：数量 / 千人
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「每月新增了多少工作岗位，是经济健康度的晴雨表。公布时市场波动剧烈，就业太强可能推迟降息（短期利空），太弱可能引发衰退担忧」
**专业模式详情**：显示当月值、3M 均值、修正记录（NFP 修订频繁，revision 必须完整保存）、历史分位
**历史通常含义**：NFP 趋势性走弱对应紧缩周期尾声；意外爆表引发「higher for longer」定价
**注意事项**：NFP 是**修订最频繁的宏观数据之一**，回测中首发值与终值差异可能很大，必须严格按 release_date 版本使用
**消费方**：Risk Engine（宏观风险）、事件时间线

### 9.9 GDP

**指标名称**：国内生产总值
**英文名**：GDP (Real, Annualized QoQ)
**分类**：宏观指标
**指标代码**：`macro.gdp`
**公式**：采集实际 GDP 环比折年率（%）与 GDP YoY；区分初估/修正/终值（advance/second/final，对应三次 release）
**参数**：口径：环比折年率为主
**数据依赖**：`macro_series(series_code='A191RL1Q225SBEA'/'GDPC1')`
**计算周期**：季度
**更新频率**：季度发布日事件触发（季后约 1 个月初估）
**值类型/单位**：百分比 / %
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「美国经济整体的增长速度。增长转负（衰退）历史上常伴随所有风险资产下跌，包括 BTC」
**专业模式详情**：显示 GDP 环比折年率、三次估算版本对比（advance/second/final）、历史分位、衰退判定辅助（连续两季负增长规则，仅展示参考）
**历史通常含义**：GDP 负增长季度历史上对应风险资产承压期；但 2020-03 式流动性危机中 BTC 与股市同步暴跌，宏观指标滞后于市场
**注意事项**：GDP 是**滞后指标**，对短期市场状态判断价值低，在 Risk Engine 中权重最低；三次估算版本必须全部存档
**消费方**：Risk Engine（宏观风险，低权重）、Market Regime（Macro State 背景）

### 9.10 M2 Money Supply

**指标名称**：M2 货币供应量
**英文名**：M2 Money Supply
**分类**：宏观指标
**指标代码**：`macro.m2`
**公式**：采集美国 M2 存量（万亿美元，季调）；派生：M2 YoY 增速、M2 环比变化
**参数**：无
**数据依赖**：`macro_series(series_code='M2SL')`
**计算周期**：月度（FRED 发布）
**更新频率**：每月
**值类型/单位**：金额 / 万亿 USD；增速 %
**分位数方法**：不适用（单调增长量级指标）；引擎消费 **M2 YoY 增速**的 ROLLING_WINDOW(3650) 百分位
**普通用户解释**：「市场上美元的总量。美元总量扩张（印钱）时，多余的钱历史上会流入 BTC 等资产；总量收缩时资金面偏紧」
**专业模式详情**：显示 M2 存量、YoY 增速曲线、历史分位、与 BTC 市值/价格的滚动相关性与领先滞后关系（展示统计事实，不声称因果）
**历史通常含义**：M2 YoY 触底回升历史上领先或同步于 BTC 牛市启动（2020、2023）；M2 收缩期（2022–2023 初）对应 BTC 熊市
**注意事项**：M2 发布滞后约 2–4 周，只能作为背景因子不能作为择时信号；全球美元体系下仅美国 M2 覆盖不全，与 Global Liquidity（9.12）配合使用
**消费方**：Valuation Engine（宏观流动性维度）、Market Regime（Macro State）、Cycle Engine（流动性背景）

### 9.11 Fed Balance Sheet

**指标名称**：美联储资产负债表
**英文名**：Fed Balance Sheet (WALCL)
**分类**：宏观指标
**指标代码**：`macro.fed_balance_sheet`
**公式**：采集美联储总资产（WALCL，百万美元，周度）；派生：周度变化、QT/QE 状态判定（连续 8 周方向）、扣除 TGA 与逆回购的「净流动性」：NetLiquidity = WALCL − TGA(WTREGEN) − RRP(ONTSFD)，如分项可得
**参数**：净流动性公式分项可配置（部分 Provider 无 TGA/RRP 时降级为 WALCL）
**数据依赖**：`macro_series(series_code='WALCL'/'WTREGEN'/'RRPONTSYD')`
**计算周期**：周度（每周四发布上周三数据）
**更新频率**：每周
**值类型/单位**：金额 / 十亿 USD
**分位数方法**：净流动性变化率用 ROLLING_WINDOW(1560)
**普通用户解释**：「美联储自己在『印钱』还是『收钱』。资产负债表扩张（QE）= 放水，历史上对 BTC 友好；缩表（QT）= 收水，资金面变紧」
**专业模式详情**：显示 WALCL 曲线、周变化、QE/QT 状态、净流动性曲线（WALCL−TGA−RRP）及其与 BTC 价格叠加图、数据来源与 release 滞后说明
**历史通常含义**：2020-03 紧急扩表后 BTC 开启大牛市；2022 缩表期对应熊市；净流动性比 WALCL 与 BTC 的历史相关性更强
**注意事项**：TGA/RRP 分项可能因 Provider 覆盖不全而缺失，缺失时降级使用并标记；数据发布滞后一周，只能作背景因子
**消费方**：Valuation Engine（宏观流动性）、Market Regime（Macro State）、Cycle Engine

### 9.12 Global Liquidity

**指标名称**：全球流动性指数
**英文名**：Global Liquidity Index
**分类**：宏观指标
**指标代码**：`macro.global_liquidity`
**公式**：GLI = 加权合成指数：主要央行（Fed/ECB/BOJ/PBOC）资产负债表规模（本币→USD 折算）+ 各国 M2 增速合成；归一化为指数（基期 2015-01-01 = 100）；具体权重配置化存储，变更记录版本
**参数**：成分权重表（JSONB 配置，默认：Fed BS 35% / 全球 M2 40% / ECB 10% / BOJ 5% / PBOC 10%）
**数据依赖**：`macro_series(多序列：WALCL, ECB 资产, BOJ 资产, PBOC 资产, 各国 M2)`
**计算周期**：周度（随最慢成分对齐）
**更新频率**：每周
**值类型/单位**：指数 / 点
**分位数方法**：ROLLING_WINDOW(1560)（对 GLI 变化率）
**普通用户解释**：「全世界『放水』的总体程度。全球资金面宽松时，历史上 BTC 这类资产容易上涨；全球收水时容易下跌」
**专业模式详情**：显示 GLI 曲线、成分明细与各自贡献度、权重配置与版本、与 BTC 价格叠加图、滚动相关性、成分数据可用性状态（某央行数据缺失时的降级说明）
**历史通常含义**：GLI 同比触底回升历史上领先 BTC 牛市 0–6 个月；GLI 收缩期对应 BTC 熊市阶段（统计关系，非必然）
**注意事项**：合成指数口径是**系统自定义**，成分缺失时自动降权并标记质量降级；汇率折算引入额外误差；该指标为低频背景因子，禁止用于短期择时
**消费方**：Valuation Engine（宏观流动性维度）、Cycle Engine（长周期背景）、Market Regime（Macro State）

### 9.13 ETF Net Flow

**指标名称**：BTC 现货 ETF 资金流
**英文名**：Spot BTC ETF Net Flow
**分类**：宏观指标（机构资金流）
**指标代码**：`macro.etf_net_flow`
**公式**：Net Flow(USD) = Σ_fund(份额变化 × 当日 NAV)；按基金（IBIT/FBTC/GBTC/ARK 等）分维度；派生：7D/30D/90D 累计净流入、ETF 总持仓（BTC 数量与市值）、持仓占流通量比例
**参数**：基金列表配置化（新基金上市自动纳入，历史从各自上市日起算）
**数据依赖**：`etf_flows(daily_flow_usd, shares, nav, holdings_btc)`
**计算周期**：1d（美股交易日，非交易日无数据）
**更新频率**：每美股交易日收盘后（北京时间次日凌晨）
**值类型/单位**：金额 / USD；持仓 / BTC
**分位数方法**：EXPANDING_FROM(2024-01-11)（现货 ETF 上市日）
**普通用户解释**：「华尔街的 BTC 基金（ETF）每天是被买入还是被赎回，代表机构资金的进出方向。持续净流入说明机构在买入，净流出说明机构在卖出」
**专业模式详情**：显示当日/7D/30D/90D 净流量、分基金明细、累计流量曲线、ETF 总持仓与占流通量比例、数据来源与更新时间、休市日标记
**历史通常含义**：2024 年现货 ETF 上市后，ETF 净流量成为 BTC 边际定价的重要力量；连续多日大额净流出历史上对应阶段性回调（如 2024-04、2025 初的流出潮）
**注意事项**：**仅美股交易日有数据**，周末/假日无值属正常（不得标记为缺失）；GBTC 折溢价转换期（2024 上半年）的流量需单独标注口径；ETF 数据 Provider 历史覆盖从 2024-01-11 开始，之前的回放中该因子标记不可用
**消费方**：Cycle Engine（机构资金核心因子）、Market Regime（Capital Flow State）、Risk Engine（流动性）、首页资金卡片、事件时间线（重大流量事件）

### 9.14 Stablecoin Supply & Flow

**指标名称**：稳定币供应与交易所稳定币流
**英文名**：Stablecoin Supply / Exchange Stablecoin Balance
**分类**：宏观指标（场内购买力代理）
**指标代码**：`macro.stablecoin_supply`
**公式**：USDT+USDC 总供应量（链上发行量）；交易所稳定币余额（场内待命购买力）；派生：稳定币/BTC 市值比、交易所稳定币余额 30d 变化
**参数**：币种集合 {USDT, USDC}（可配置扩展）
**数据依赖**：`onchain_metrics(stablecoin_supply)`, `exchange_flows(stablecoin_balance)`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：金额 / USD
**分位数方法**：供应总量用趋势斜率；稳定币/BTC 市值比用 FULL_HISTORY
**普通用户解释**：「交易所里有多少『还没买入的美元弹药』（稳定币）。弹药充足说明潜在买盘大，弹药枯竭说明追高意愿下降」
**专业模式详情**：显示 USDT/USDC 总供应、交易所稳定币余额、稳定币/BTC 市值比曲线与历史分位、30d 变化、数据来源
**历史通常含义**：交易所稳定币余额创阶段新高对应潜在买盘充裕（历史上常先于反弹）；余额持续下降对应资金撤离
**注意事项**：稳定币发行量受 DeFi/跨境支付等非交易需求影响，不能全部解读为「买 BTC 的弹药」；USDT 在部分链上的余额口径 Provider 间有差异
**消费方**：Cycle Engine（资金因子）、Market Regime（Capital Flow State）、Risk Engine（流动性）

### 9.15 Fear & Greed Index

**指标名称**：恐惧贪婪指数（加密）
**英文名**：Crypto Fear & Greed Index
**分类**：宏观指标（情绪合成，存于 `sentiment` 表，分类归入情绪更合理——本字典按需求文档将其列入情绪指标 §10.1，此处仅建立交叉引用避免重复定义）
**指标代码**：`sent.fear_greed`（定义见 §10.1）
**公式/参数/消费方**：见 §10.1

### 9.16 VIX

**指标名称**：美股波动率指数
**英文名**：CBOE Volatility Index (VIX)
**分类**：宏观指标
**指标代码**：`macro.vix`
**公式**：直接采集 CBOE VIX 收盘值（S&P500 期权 30 天隐含波动率指数）
**参数**：无
**数据依赖**：`macro_series(series_code='VIXCLS')`
**计算周期**：1d（美股交易日）
**更新频率**：每美股交易日
**值类型/单位**：指数 / 点
**分位数方法**：ROLLING_WINDOW(3650)
**普通用户解释**：「美国股市的恐慌程度。VIX 飙升说明传统市场恐慌，历史上这种恐慌会传导到 BTC（流动性危机时一起跌）」
**专业模式详情**：显示 VIX 当前值、曲线、历史分位、与 BTC 日收益率的滚动相关性、与 BTC 期权 DVOL 的对照
**历史通常含义**：VIX > 30 对应股市恐慌期，2020-03、2022 多次加息冲击期间 BTC 同步承压；VIX < 15 对应风险偏好环境
**注意事项**：非美股交易日无更新，周末 BTC 行情中 VIX 为静态值（标记 STALE 属正常，不触发告警）
**消费方**：Risk Engine（宏观风险）、Market Regime（Macro State）

### 9.17 Risk Appetite Proxies

**指标名称**：风险偏好代理（纳指/黄金对照）
**英文名**：Risk Appetite Proxies (NDX / Gold)
**分类**：宏观指标
**指标代码**：`macro.risk_proxies`
**公式**：采集 NDX（纳斯达克 100）与黄金（XAUUSD）日度收盘；派生：BTC-NDX 90d 滚动相关系数、BTC-Gold 90d 滚动相关系数（判断 BTC 当前交易机制：风险资产模式 vs 数字黄金模式）
**参数**：相关性窗口 90d（可配置）
**数据依赖**：`macro_series(series_code='NDX'/'GOLDAMGBD228NLBM')`
**计算周期**：1d
**更新频率**：每日
**值类型/单位**：价格 / USD；相关系数 −1~1
**分位数方法**：相关系数用 ROLLING_WINDOW(730)
**普通用户解释**：「看 BTC 现在更像『科技股』还是更像『黄金』。通过对比 BTC 与纳斯达克、黄金的联动程度，判断资金把 BTC 当作什么类型的资产在交易」
**专业模式详情**：显示 NDX/黄金曲线、BTC-NDX/BTC-Gold 滚动相关系数曲线、当前机制判定（相关性显著高者）、机制切换事件列表
**历史通常含义**：2020–2022 BTC 与纳指高度同步（风险资产模式）；阶段性切换至与黄金同涨（贬值对冲叙事）
**注意事项**：相关系数是滚动统计量，机制判定必须设置显著性阈值（|r|>0.4 才判定）；两个相关性都不显著时输出「独立行情」
**消费方**：Market Regime（Macro State）、首页机制提示、Risk Engine（联动风险）

---

## 10. 情绪指标（SENT）

> 数据来源：`sentiment` 表（SentimentProvider / NewsProvider / 社交数据 Provider 聚合）。
> **通用注意事项**：情绪类数据 Provider 变更频繁（API 停服风险最高的一类），所有情绪指标必须实现降级策略：单源失效时用剩余源重新归一化并标记质量降级；全部失效时情绪模块显示「数据暂时不可用」而不影响其他引擎（需求文档第四十四节降级运行）。

### 10.1 Fear & Greed Index

**指标名称**：加密恐惧贪婪指数
**英文名**：Crypto Fear & Greed Index
**分类**：情绪指标
**指标代码**：`sent.fear_greed`
**公式**：采集主源指数（0–100，合成因子：波动率 25% + 市场动量 25% + 社交媒体 15% + 问卷调查 15% + BTC 主导率 10% + Google Trends 10%，以主源公布口径为准）；系统同时自建平行口径（波动率+动量+Funding+社交）作为交叉验证源
**参数**：无（采集值）；自建口径权重配置化
**数据依赖**：`sentiment(fear_greed_index)`
**计算周期**：1d（另采集盘中小时值）
**更新频率**：每日
**值类型/单位**：指数 / 0–100
**分位数方法**：minmax（本身有界）+ ROLLING_WINDOW(365) 分位
**普通用户解释**：「市场情绪温度计：0 极度恐惧，100 极度贪婪。历史经验：极度恐惧时往往是长期机会区域，极度贪婪时要警惕风险——但这是统计规律，不是保证」
**专业模式详情**：显示指数值、情绪分区（Extreme Fear <25 / Fear 25–45 / Neutral 45–55 / Greed 55–75 / Extreme Greed >75，仅展示参考）、主源与自建平行口径对照、各合成因子明细（如可得）、历史分位、数据来源
**历史通常含义**：极度恐惧（<15）历史上多次对应阶段底部（2020-03、2022-06）；极度贪婪（>90）持续数周后历史上多次出现深度回调
**注意事项**：情绪指数可以长期钝化在极端区域，禁止单独作为买卖依据；主源 Provider 历史停服过，必须配置 ≥2 个源
**消费方**：Cycle Engine（情绪因子）、Market Regime（Sentiment State）、首页情绪卡片、Risk Engine（拥挤度辅助）

### 10.2 Google Trends

**指标名称**：谷歌搜索趋势
**英文名**：Google Trends (Search Interest)
**分类**：情绪指标
**指标代码**：`sent.google_trends`
**公式**：采集关键词（"bitcoin", "btc", "buy bitcoin", "bitcoin etf" 等，关键词表配置化）的搜索热度（0–100 归一化，主源相对值）；派生：热度 4 周变化率
**参数**：关键词表、地域（默认全球+美国分列）
**数据依赖**：`sentiment(google_trends)`
**计算周期**：周度（Google 发布粒度）/ 日度（如 Provider 支持）
**更新频率**：每周
**值类型/单位**：指数 / 0–100（相对值）
**分位数方法**：FULL_HISTORY（同关键词序列内可比）
**普通用户解释**：「普通大众对比特币的搜索关注度。搜索量冲上历史极值时，往往说明连平时不关心的人都在关注，历史上常对应行情过热阶段」
**专业模式详情**：显示各关键词热度曲线、历史分位、4 周变化率、历史峰值事件对照（2013/2017/2021/2024 顶部均伴随搜索峰值）
**历史通常含义**："bitcoin" 搜索热度创历史新高历史上均出现在周期顶部区域附近；热度长期低迷对应无人问津的底部积累期
**注意事项**：Google Trends 是**相对值**，不同时间拉取的同一周数据可能因归一化基准变化而不同——采集后必须以本地存储为准，禁止展示时重新拉取；周度数据滞后 2–3 天
**消费方**：Cycle Engine（散户热度因子）、Risk Engine（市场拥挤度）、Market Regime（Sentiment State）

### 10.3 News Sentiment Score

**指标名称**：新闻情绪分
**英文名**：News Sentiment Score
**分类**：情绪指标
**指标代码**：`sent.news_sentiment`
**公式**：采集新闻流（标题+摘要）→ NLP 情绪打分（−1 极负面 ~ +1 极正面，模型版本化记录）→ 日度加权均值（按媒体权重×文章热度）；另输出：新闻量（文章数）与情绪分的背离（量大情绪中性 = 观望）
**参数**：媒体权重表（配置化）、情绪模型版本（model_versions 登记）
**数据依赖**：`sentiment(news_sentiment, news_count)`；原始文章存 raw 层
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：分数 / −1 ~ +1；新闻量 / 篇
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「新闻媒体今天对比特币的报道整体是正面还是负面，以及报道量有多大。媒体集体唱多时要警惕，集体唱衰时历史上反而常是机会区域（反向参考）」
**专业模式详情**：显示情绪分曲线、新闻量、Top 正/负面文章列表（可追溯原文链接）、情绪模型版本与历史准确率、分媒体情绪明细
**历史通常含义**：情绪分创阶段新高对应媒体狂热顶部特征；创阶段新低对应恐慌底部特征（反向指标属性强于正向）
**注意事项**：NLP 情绪模型本身需要验证与版本管理（见 13 号文档 §6）；新闻源在中国大陆网络环境下可达性差，Provider 必须支持代理配置；媒体存在付费软文，异常单篇不改变日度均值
**消费方**：Market Regime（Sentiment State）、Cycle Engine（情绪因子）、AI 解释助手（引用新闻证据）

### 10.4 Social Sentiment Score

**指标名称**：社交媒体情绪分
**英文名**：Social Sentiment Score
**分类**：情绪指标
**指标代码**：`sent.social_sentiment`
**公式**：采集社交平台（X/Reddit 等，按 Provider 可得性）BTC 相关帖子量与情绪分 → 日度合成：Social Volume（帖子数，ROLLING 分位）+ Social Sentiment（−1~+1）；派生：散户狂热指数 = Volume 分位 × Sentiment 分位（量价齐升 = 狂热）
**参数**：平台权重表、关键词表（配置化）
**数据依赖**：`sentiment(social_volume, social_sentiment)`
**计算周期**：1h / 1d
**更新频率**：每小时
**值类型/单位**：帖子量 / 条；情绪分 / −1 ~ +1
**分位数方法**：ROLLING_WINDOW(365)
**普通用户解释**：「社交媒体上讨论比特币的热度和情绪。讨论量暴增+情绪极度乐观 = 散户狂热，历史上这种时候往往接近短期顶部；无人讨论 = 市场冷淡期」
**专业模式详情**：显示分平台 Volume/Sentiment 曲线、狂热指数、历史分位、采集覆盖率（各平台可用性状态）、原始热门帖子样本（可追溯）
**历史通常含义**：社交狂热指数创阶段新高历史上多次对应短期顶部；Volume 历史低位对应积累期
**注意事项**：社交 API 是**最容易失效的数据源**（平台封禁、API 涨价），必须至少 2 个 Provider 且支持快速替换；机器人刷量会污染 Volume，异常账号过滤规则配置化并记录版本
**消费方**：Cycle Engine（热度/拥挤因子）、Market Regime（Sentiment State）、Risk Engine（市场拥挤度）

---

## 11. 指标存储与消费规范

### 11.1 存储结构（与数据库 ER 设计文档一致）

```
indicator_definitions   ── 指标元数据（本字典 §3 字段，口径变更递增 version）
indicator_values        ── 指标计算结果（时序表，TimescaleDB hypertable）
  ├─ indicator_code      指标代码（引用 definitions）
  ├─ params              JSONB 参数实例（如 {"period": 200}）
  ├─ timeframe           计算周期
  ├─ observation_time    数据所属时间（回测过滤主键）
  ├─ fetch_time          系统获取时间（Point-in-Time 过滤键）
  ├─ value               原始值
  ├─ normalized          标准化值（§4.2）
  ├─ percentile          分位数（§4.1）
  ├─ source_id           数据来源 Provider
  ├─ quality_status      VERIFIED / ESTIMATED / STALE / CONFLICT / INVALID
  └─ definition_version  计算时使用的口径版本
```

### 11.2 计算调度

- 指标计算任务由 Scheduler 按 `update_frequency` 调度（任务命名：`indicator_{category}_{timeframe}`）；
- 依赖链：行情/链上/衍生品采集任务 → 标准化入库 → 指标计算 → 引擎消费；上游任务未完成时指标任务等待而非用旧数据计算；
- 指标重算：口径变更（definition_version 递增）时，必须全量重算历史并保留旧版本数据可查（回测结果永久对应版本，见 13/14 号文档）。

### 11.3 引擎消费接口

```
IndicatorService.get_value(code, params, timeframe, as_of=None)
  → as_of=None：返回最新值（实盘展示）
  → as_of=T：返回 observation_time <= T 且 fetch_time <= T 的最近值（回测/回放专用，
             Point-in-Time 语义由服务层强制，业务代码禁止直接查表）
IndicatorService.get_percentile(code, params, timeframe, as_of=None)
IndicatorService.get_history(code, params, timeframe, start, end, pit=True)
```

每个返回值附带元组：`(value, percentile, normalized, quality_status, source_id, observation_time, fetch_time, definition_version)`，引擎必须将 `quality_status` 与 `source_id` 透传到输出证据链（可解释性要求，需求文档第二十七/二十八节）。

### 11.4 口径变更流程（强制）

1. 提出变更（新 Provider 口径差异 / 行业口径演进 / Bug 修复）；
2. 更新本字典对应条目 + `indicator_definitions.version` 递增 + 变更记录（changelog 字段）；
3. 全量重算受影响历史区间，新旧版本并存；
4. 引擎权重如需同步调整 → 走模型版本管理（13 号文档 §6），**禁止静默改口径**；
5. 历史回测结果保持引用旧版本，不自动重跑。

---

## 12. 指标 ↔ 引擎消费矩阵（速查）

| 引擎 | 核心指标 | 辅助指标 |
|---|---|---|
| Cycle Engine | MA 家族、MVRV、NUPL、LTH-SOPR、ΔLTH Supply、ETF Net Flow、Funding、Puell | RSI、CVD、HODL Waves、Google Trends、Social Volume、Global Liquidity |
| Valuation Engine | MVRV、NUPL、Realized Cap/Price、Reserve Risk、Drawdown、200W MA 乖离 | STH 成本基础、M2/净流动性、HV |
| Risk Engine | Funding、OI/MCap、Liquidation、HV/ATR、DVOL、Exchange Reserve、Spread/Depth、DXY/VIX | LSR、CDD/Dormancy、Realized Loss、CPI/NFP 事件窗口 |
| Market Regime | 全部九维输入（见 13 号文档 §4） | — |
| Prediction System | 以上全部的特征化版本（分位数/变化率） | 历史相似状态检索特征集 |
| Backtest/定投模拟 | Price、Drawdown、MVRV、NUPL、Risk Score（均为 Point-in-Time 值） | Funding（成本模拟）、滑点用 ATR/Spread |

---

## 附：本文档维护说明

- 本文档为指标口径的**唯一权威来源**，代码中 `indicator_definitions` 种子数据由本文档生成；
- 新增指标：按 §3 元数据格式补全全部字段后方可入库，缺「普通用户解释」或「注意事项」的定义不得上线；
- 交叉引用：引擎如何使用这些指标打分与融合 → `13-model-architecture.md`；回测中的 Point-in-Time 强制约束 → `14-backtest-architecture.md` §2；定投规则可引用的指标条件集 → `15-portfolio-architecture.md` §4。
