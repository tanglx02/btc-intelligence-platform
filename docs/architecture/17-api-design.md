# 17 - API 设计（API Design）

> BTC 全市场智能研究平台 · 后端 API 设计规范
>
> 技术栈：FastAPI (Python) + Pydantic v2 + PostgreSQL/TimescaleDB + Redis

---

## 概述

本文档定义平台全部 REST API 与 WebSocket 协议。设计目标：

1. **统一规范**：一致的响应结构、错误码、分页、时间格式；
2. **数据可溯源**：每个数据响应都携带 `meta.source` / `meta.quality_status` / 时间戳，支撑前端溯源 UI；
3. **降级友好**：数据源不可用时返回明确的降级状态（503 + STALE 数据），而非静默失败或假数据；
4. **权限清晰**：与 RBAC 三角色（Admin/Analyst/User）对齐，详见《18-权限设计》；
5. **长期演进**：版本化路径 `/api/v1/`，破坏性变更升级 `/api/v2/`，旧版本至少保留 6 个月。

---

## 目录

1. [API 规范](#1-api-规范)
2. [统一响应格式](#2-统一响应格式)
3. [API 端点分组设计](#3-api-端点分组设计)
4. [WebSocket 协议设计](#4-websocket-协议设计)
5. [错误码体系](#5-错误码体系)
6. [Rate Limiting](#6-rate-limiting)
7. [缓存与条件请求](#7-缓存与条件请求)
8. [版本管理](#8-版本管理)

---

## 1. API 规范

| 项目 | 约定 |
|------|------|
| 基础路径 | `/api/v1/`（WebSocket 为 `/ws`） |
| 协议 | HTTPS（生产强制），HTTP/1.1 + HTTP/2 |
| 认证 | `Authorization: Bearer <JWT>`；匿名可访问只读市场数据端点 |
| 内容类型 | 请求/响应均为 `application/json; charset=utf-8`；文件导出用 `text/csv` |
| 时间格式 | ISO 8601 UTC，如 `2026-01-01T00:00:00Z`；请求参数中可用 `2026-01-01` 简写（按 UTC 00:00 解析） |
| 命名风格 | URL 用 kebab-case（`/exchange-flow`），JSON 字段用 snake_case（`quality_status`） |
| 幂等性 | GET/PUT/DELETE 幂等；POST 非幂等（回测/模拟类任务支持 `Idempotency-Key` 请求头防重复提交） |
| 排序 | `?sort=field&order=asc|desc`，多字段 `?sort=field1,-field2`（`-` 前缀表示降序） |
| 过滤 | `?filter[field]=value`，范围过滤 `?filter[value_gte]=10&filter[value_lte]=20`，时间范围 `?start=...&end=...` |
| 字段裁剪 | `?fields=name,value,timestamp`（减少载荷） |
| 时区 | 服务端一律 UTC 存储与返回；展示时区由前端处理 |

### 1.1 分页

两种分页模式：

**Cursor-based（时序数据，必须使用）** —— K线、指标历史、交易流、日志等按时间排序的数据：

```text
GET /api/v1/market/ohlcv?symbol=BTC-USDT&interval=1d&limit=500&cursor=eyJ0IjoiMjAyNi0wMS0wMSJ9
```

cursor 为不透明 Base64 字符串（内部编码最后一条记录的时间戳+id），客户端不得解析。

**Offset-based（列表数据）** —— 用户计划、回测列表、Provider 列表等：

```text
GET /api/v1/backtest/runs?page=2&page_size=20
```

`page_size` 默认 20，最大 100；时序端点 `limit` 默认 100，最大 5000（K线）/ 1000（其他）。

### 1.2 通用查询参数（时序端点）

| 参数 | 类型 | 说明 |
|------|------|------|
| `start` / `end` | ISO 8601 | 时间范围（闭开区间 `[start, end)`） |
| `interval` | string | 粒度：`1m/5m/15m/1h/4h/1d/1w`（行情）或 `1h/1d`（指标类） |
| `limit` | int | 返回条数上限 |
| `cursor` | string | 游标 |
| `as_of` | ISO 8601 | **历史回放专用**：只返回该时刻已知的数据（防未来泄漏，宏观数据按 release_date 过滤） |
| `provider_id` | string | 指定数据源查询（Analyst/Admin） |
| `include_raw` | bool | 是否附带原始数据引用（专业模式，默认 false） |

---

## 2. 统一响应格式

### 2.1 成功响应

```json
{
  "success": true,
  "data": { },
  "meta": {
    "timestamp": "2026-01-01T00:00:00Z",
    "source": "binance",
    "quality_status": "VERIFIED",
    "cache_hit": false,
    "observation_time": "2026-01-01T00:00:00Z",
    "fetch_time": "2026-01-01T00:00:03Z",
    "failover": null,
    "confidence": 0.96
  },
  "pagination": {
    "cursor": "eyJ0IjoiMjAyNi0wMS0wMSJ9",
    "has_more": true,
    "total": 1000
  }
}
```

**meta 字段说明**（支撑前端溯源 UI 的核心契约）：

| 字段 | 说明 |
|------|------|
| `timestamp` | 响应生成时间 |
| `source` | 实际提供数据的 Provider ID（failover 后为备用源） |
| `quality_status` | `VERIFIED` / `ESTIMATED` / `STALE` / `CONFLICT` / `INVALID` |
| `cache_hit` | 是否命中 Redis 缓存 |
| `observation_time` | 数据本身的业务时间 |
| `fetch_time` | 数据抓取时间 |
| `failover` | 若发生切换：`{"from": "glassnode", "reason": "TIMEOUT", "at": "..."}`，否则 null |
| `confidence` | 数据/模型置信度 0~1（引擎输出必带） |

**STALE 降级约定**：数据源全部失败但存在最近可信数据时，返回 **HTTP 200** + `quality_status: "STALE"` + 最近可信数据 + `meta.degraded: true`，绝不返回假数据，也绝不返回空。

### 2.2 错误响应

```json
{
  "success": false,
  "error": {
    "code": 40401,
    "type": "RESOURCE_NOT_FOUND",
    "message": "指标 'mvrvv2' 不存在",
    "details": [
      {"field": "name", "issue": "unknown metric", "hint": "可用指标见 GET /indicators/list"}
    ],
    "request_id": "req_8f3a2b1c"
  }
}
```

所有错误响应携带 `request_id`（关联日志追踪，见《19-日志设计》）。

### 2.3 写操作响应

创建类返回 `201` + 完整资源；更新返回 `200` + 更新后资源；删除返回 `200` + `{"success": true, "data": {"deleted": true, "id": "..."}}`。

---

## 3. API 端点分组设计

> 权限列：`A`=Admin，`An`=Analyst，`U`=登录用户，`○`=匿名可访问。完整矩阵见《18-权限设计》。

### 3.1 Auth（认证）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| POST | `/auth/register` | 注册（username/email/password） | ○ |
| POST | `/auth/login` | 登录，返回 access_token + refresh_token | ○ |
| POST | `/auth/refresh` | 用 refresh_token 换新 access_token（旋转刷新） | ○ |
| POST | `/auth/logout` | 登出（吊销 refresh_token） | U |
| GET | `/auth/me` | 当前用户信息（id、角色、偏好设置如普通/专业模式） | U |
| PUT | `/auth/me` | 更新个人资料与偏好 | U |
| PUT | `/auth/me/password` | 修改密码 | U |

`POST /auth/login` 响应示例：

```json
{
  "success": true,
  "data": {
    "access_token": "eyJhbG...",
    "refresh_token": "dGhpcy...",
    "token_type": "Bearer",
    "expires_in": 1800,
    "user": {"id": "u_123", "username": "alice", "role": "user"}
  }
}
```

### 3.2 Market（市场行情）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/market/price` | 当前价格（含 24H/7D/30D 涨跌、多源交叉验证结果） | ○ |
| GET | `/market/ohlcv` | K线数据（symbol/interval/start/end/cursor 分页） | ○ |
| GET | `/market/volume` | 成交量历史 | ○ |
| GET | `/market/stats` | 市场统计：市值、ATH、距 ATH 回撤、波动率、ATR、VWAP | ○ |
| GET | `/market/orderbook` | 订单簿快照（depth 参数：20/100/500 档） | ○ |
| GET | `/market/cvd` | 累计成交量差（CVD） | ○ |
| GET | `/market/exchanges` | 多交易所价格对比（交叉验证明细：各源价格/偏差/中位数/VWAP） | ○ |

`GET /market/price` 响应 data 示例：

```json
{
  "symbol": "BTC-USDT",
  "price": 108240.5,
  "change_24h_pct": 2.31,
  "change_7d_pct": 5.1,
  "change_30d_pct": -1.2,
  "high_24h": 109100.0,
  "low_24h": 105800.0,
  "cross_validation": {
    "status": "VERIFIED",
    "sources": [
      {"provider": "binance", "price": 108240.5, "deviation_pct": 0.0},
      {"provider": "coinbase", "price": 108251.2, "deviation_pct": 0.01},
      {"provider": "kraken", "price": 108233.0, "deviation_pct": -0.01}
    ],
    "median": 108240.5,
    "vwap": 108241.6
  }
}
```

### 3.3 On-Chain（链上）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/onchain/metrics` | 链上指标列表（每个指标的最新值/分位/状态摘要） | ○ |
| GET | `/onchain/metrics/{name}` | 特定指标历史序列（mvrv/sopr/nupl/rhodl/…，支持 as_of） | ○ |
| GET | `/onchain/exchange-flow` | 交易所资金流（inflow/outflow/netflow，分交易所） | ○ |
| GET | `/onchain/supply` | 供应分布（LTH/STH Supply、HODL Waves、Supply Last Active） | ○ |
| GET | `/onchain/miners` | 矿工数据（Puell Multiple、Miner Flow） | ○ |
| GET | `/onchain/whale-activity` | 鲸鱼大额转账事件 | ○ |

### 3.4 ETF

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/etf/flows` | ETF 每日资金流（单只/全部，7D/30D/90D/累计） | ○ |
| GET | `/etf/holdings` | ETF 持仓（各 ETF 持有 BTC 数、占流通量比） | ○ |
| GET | `/etf/summary` | ETF 汇总（首页卡片数据：昨日净流、累计、总持仓） | ○ |

### 3.5 Derivatives（衍生品）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/derivatives/funding` | 资金费率（当前+历史，分交易所） | ○ |
| GET | `/derivatives/open-interest` | 未平仓合约（总量+分交易所） | ○ |
| GET | `/derivatives/liquidations` | 清算数据（多/空清算额，事件列表） | ○ |
| GET | `/derivatives/long-short` | 多空比 | ○ |
| GET | `/derivatives/basis` | 基差与溢价（永续-现货、季度年化） | ○ |
| GET | `/derivatives/taker-flow` | Taker Buy/Sell 与衍生品 CVD | ○ |

### 3.6 Options（期权）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/options/overview` | 期权概览（总 OI、成交量、名义价值、到期分布、Max Pain） | ○ |
| GET | `/options/iv` | 隐含波动率（DVOL、期限结构、Skew） | ○ |
| GET | `/options/put-call` | 看跌/看涨比（成交量比、OI 比） | ○ |

### 3.7 Macro（宏观）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/macro/indicators` | 宏观指标看板（DXY/Fed Rate/2Y/10Y/CPI/PCE/M2/…最新值与状态） | ○ |
| GET | `/macro/events` | 宏观事件日历（FOMC/CPI/NFP，历史公布值 vs 预期） | ○ |
| GET | `/macro/series/{name}` | 特定宏观序列（**区分 observation_date / release_date / revision_date**，`as_of` 按 release_date 过滤防泄漏） | ○ |

### 3.8 Sentiment（情绪）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/sentiment/fear-greed` | 恐惧贪婪指数（当前+历史+构成因子） | ○ |
| GET | `/sentiment/social` | 社交情绪（热度、极性） | ○ |
| GET | `/sentiment/news` | 新闻情绪（分数曲线+新闻列表） | ○ |

### 3.9 Indicators（技术指标）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/indicators/list` | 可用指标列表（名称、类别、描述、状态） | ○ |
| GET | `/indicators/{name}` | 指标数据序列（ma/ema/rsi/macd/adx/atr/boll/vol-profile/percentile） | ○ |
| GET | `/indicators/definitions/{name}` | 指标定义（公式、参数、数学定义、计算说明——专业模式用） | ○ |

### 3.10 Engines（引擎输出）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/engine/cycle` | 当前周期状态（阶段/置信度/支持与反向证据/相似历史阶段） | ○ |
| GET | `/engine/valuation` | 当前估值状态（五档/综合分/各指标分位/权重） | ○ |
| GET | `/engine/risk` | 当前风险评分（总分+六分项+因子明细） | ○ |
| GET | `/engine/regime` | 综合市场状态（Market Regime，首页状态条数据） | ○ |
| GET | `/engine/prediction` | 预测（上涨/横盘/下跌概率、区间、置信度、样本数、模型版本；证据不足时返回 `"insufficient_evidence": true`） | ○ |
| GET | `/engine/history` | 引擎历史输出（`?engine=cycle|risk|regime&date=...`，历史回放核心端点，支持 as_of） | ○ |
| GET | `/engine/explain` | 结论解释包（`?target=valuation&date=...`：支持/反对证据、计算过程、引用数据——「为什么？」按钮数据源） | ○ |

`GET /engine/regime` 响应 data 示例：

```json
{
  "regime": "UPTREND_OVEREXTENDED",
  "summary": "趋势上涨 · 估值偏高 · 风险中等",
  "confidence": 0.82,
  "states": {
    "trend": {"state": "UP", "confidence": 0.88},
    "valuation": {"state": "HIGH", "confidence": 0.78},
    "capital_flow": {"state": "INFLOW", "confidence": 0.85},
    "onchain": {"state": "ACCUMULATION", "confidence": 0.8},
    "derivative": {"state": "NEUTRAL_HIGH", "confidence": 0.7},
    "macro": {"state": "EASING_BIAS", "confidence": 0.65},
    "sentiment": {"state": "GREED", "confidence": 0.9},
    "risk": {"state": "MEDIUM", "confidence": 0.75},
    "cycle": {"state": "UPTREND", "confidence": 0.82}
  },
  "changed_at": "2026-09-20T00:00:00Z",
  "model_version": "regime-v1.4.2",
  "evidence_url": "/api/v1/engine/explain?target=regime"
}
```

### 3.11 Portfolio（个人）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/portfolio/plans` | 我的计划列表 | U |
| POST | `/portfolio/plans` | 创建计划（初始资金/投入/周期/加仓规则等，见需求第二十节字段） | U |
| GET | `/portfolio/plans/{id}` | 计划详情（含下次投入时间、建议金额与倍数命中说明） | U |
| PUT | `/portfolio/plans/{id}` | 更新计划 | U |
| DELETE | `/portfolio/plans/{id}` | 删除/归档计划 | U |
| GET | `/portfolio/transactions` | 交易记录列表（过滤：plan_id/时间范围） | U |
| POST | `/portfolio/transactions` | 手工记录一笔买入/卖出 | U |
| PUT | `/portfolio/transactions/{id}` | 修改记录 | U |
| DELETE | `/portfolio/transactions/{id}` | 删除记录 | U |
| GET | `/portfolio/holdings` | 当前持仓（BTC 数量、平均成本、市值、浮盈亏） | U |
| GET | `/portfolio/performance` | 绩效报告（资产曲线、收益率、最大回撤、Sharpe 等） | U |
| POST | `/portfolio/simulate` | 定投模拟（策略+参数 → 逐笔结果与统计；计算量大时返回 202 + 任务轮询） | U |
| GET | `/portfolio/export` | 导出个人数据（CSV，备份用） | U |

所有 `/portfolio/*` 端点强制 `user_id = current_user.id` 过滤，禁止跨用户访问（返回 40403）。

`POST /portfolio/simulate` 请求示例：

```json
{
  "strategy": "drawdown_dca",
  "start_date": "2020-01-01",
  "end_date": "2026-01-01",
  "frequency": "monthly",
  "base_amount": 5000,
  "currency": "CNY",
  "rules": [
    {"drawdown_from_ath_pct": -10, "multiplier": 1.0},
    {"drawdown_from_ath_pct": -20, "multiplier": 1.2},
    {"drawdown_from_ath_pct": -30, "multiplier": 1.5},
    {"drawdown_from_ath_pct": -40, "multiplier": 2.0}
  ],
  "fee_pct": 0.001,
  "slippage_pct": 0.0005
}
```

### 3.12 Backtest（回测）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| POST | `/backtest/run` | 创建回测（异步任务，返回 202 + run_id，进度走 WebSocket `job.progress`） | U |
| GET | `/backtest/runs` | 我的回测列表（Analyst/Admin 可见全部） | U |
| GET | `/backtest/runs/{id}` | 回测详情（结果统计、资产曲线、逐笔交易、防泄漏审计报告、模型/数据版本绑定） | U(本人) / An / A |
| DELETE | `/backtest/runs/{id}` | 删除回测 | U(本人) / A |
| POST | `/backtest/compare` | 策略对比（`{"run_ids": ["r1","r2"]}` → 指标对比表+曲线叠加数据） | U |
| GET | `/backtest/strategies` | 可用策略模板列表 | U |

### 3.13 Strategy Lab / Model Lab（策略与模型，Analyst+）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/strategies` | 策略版本列表 | An / A |
| POST | `/strategies` | 创建策略（新版本，旧版本不可覆盖） | An / A |
| GET | `/strategies/{id}` | 策略详情（开发区间/测试区间/参数/验证结果） | An / A |
| POST | `/strategies/{id}/validate` | 触发验证流水线（IS/OOS/Walk-Forward/敏感性/极端行情） | An / A |
| GET | `/models` | 模型版本列表 | An / A |
| GET | `/models/{id}` | 模型详情（架构/特征/权重/验证报告） | An / A |
| POST | `/models/{id}/activate` | 激活模型版本（写入审计日志） | A |
| GET | `/models/{id}/performance` | 模型历史表现追踪（预测 vs 实际） | An / A |

### 3.14 Provider（数据源管理）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/providers` | Provider 列表（含健康摘要、优先级、评分） | ○(摘要) / An / A(完整) |
| GET | `/providers/{id}` | Provider 详情 | An / A |
| GET | `/providers/{id}/health` | Provider 健康（状态/延迟/成功率/连续失败/限流/评分明细） | An / A |
| POST | `/providers/{id}/test` | 测试 Provider（真实请求一次，返回结果与耗时） | A |
| POST | `/providers/test-all` | 一键测试全部 Provider（异步任务） | A |
| PUT | `/providers/{id}/config` | 修改配置（优先级/超时/重试/失败阈值/恢复阈值/代理；API Key 写入后只回显掩码） | A |
| POST | `/providers` | 新增 Provider | A |
| DELETE | `/providers/{id}` | 删除 Provider（存在依赖时返回 42201） | A |
| PUT | `/providers/{id}/priority-lock` | 手动锁定/解锁优先级 | A |
| GET | `/providers/failover-events` | 故障切换事件（时间/原主源/新源/原因/恢复记录） | An / A |
| GET | `/providers/{id}/requests` | Provider 请求级日志（分页） | A |

### 3.15 System（系统）

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| GET | `/system/health` | 系统健康检查（DB/Redis/调度器/各模块降级状态；轻量，供探活） | ○ |
| GET | `/system/ready` | 就绪检查（依赖全部可用才返回 200） | ○ |
| GET | `/system/jobs` | 任务列表（状态/上次运行/checkpoint/下次计划） | A |
| GET | `/system/jobs/{id}` | 任务详情与执行历史 | A |
| POST | `/system/jobs/{id}/run` | 手动执行任务（可带参数） | A |
| POST | `/system/jobs/{id}/pause` | 暂停任务 | A |
| POST | `/system/jobs/{id}/resume` | 恢复任务 | A |
| POST | `/system/jobs/{id}/retry` | 重试失败任务 | A |
| POST | `/system/jobs/{id}/skip` | 跳过当前执行 | A |
| GET | `/system/data-quality` | 数据质量报告（各类别质量比例/缺失/冲突/覆盖率） | ○(摘要) / An / A(完整) |
| GET | `/system/data-quality/conflicts` | 冲突明细列表 | An / A |
| GET | `/system/stats` | 系统统计（存储量、表行数、请求数、运行时长） | A |
| GET | `/system/audit-logs` | 审计日志查询（用户/操作/对象/时间过滤） | A |
| POST | `/system/backup` | 触发备份 | A |
| GET | `/system/backups` | 备份列表 | A |
| POST | `/system/backups/{id}/restore` | 恢复备份（二次确认 token） | A |

`GET /system/health` 响应示例（降级运行状态透明化）：

```json
{
  "success": true,
  "data": {
    "status": "DEGRADED",
    "uptime_seconds": 8640000,
    "components": {
      "database": {"status": "OK", "latency_ms": 2},
      "redis": {"status": "OK", "latency_ms": 1},
      "scheduler": {"status": "OK", "active_jobs": 14}
    },
    "modules": {
      "market": {"status": "OK", "source": "binance"},
      "onchain": {"status": "DEGRADED", "source": "cryptoquant", "note": "glassnode failover 2h ago"},
      "etf": {"status": "OK", "source": "farside"},
      "macro": {"status": "UNAVAILABLE", "note": "所有 Provider 失败，展示最近可信数据 (STALE)"}
    }
  }
}
```

### 3.16 AI Assistant

| 方法 | 端点 | 说明 | 权限 |
|------|------|------|------|
| POST | `/ai/chat` | 提问（SSE 流式响应；每个数据引用带 source/quality 元数据） | U |
| GET | `/ai/conversations` | 历史会话列表 | U |
| GET | `/ai/conversations/{id}` | 会话详情 | U |
| DELETE | `/ai/conversations/{id}` | 删除会话 | U |

AI 响应中的引用结构（强制，防止编造）：

```json
{
  "citations": [
    {
      "ref_id": "c1",
      "metric": "mvrv",
      "value": 2.38,
      "as_of": "2026-09-27T00:00:00Z",
      "source": "glassnode",
      "quality_status": "VERIFIED",
      "api_ref": "/api/v1/onchain/metrics/mvrv"
    }
  ]
}
```

---

## 4. WebSocket 协议设计

### 4.1 连接

```text
wss://host/ws?token=<access_token>
```

- 匿名连接允许（仅可订阅公开频道）；
- 连接后服务端发送 `connection.ack`；
- 心跳：客户端每 30s 发送 `{"action":"ping"}`，服务端回 `{"action":"pong"}`；60s 无消息服务端主动断开；
- 断线重连：客户端指数退避（1s/2s/4s…最大 30s），重连后需重新订阅。

### 4.2 订阅管理

```json
// 订阅
{"action": "subscribe", "channels": ["market.price", "provider.status"]}
// 取消订阅
{"action": "unsubscribe", "channels": ["market.price"]}
// 服务端确认
{"action": "subscribed", "channels": ["market.price", "provider.status"], "limits": {"max": 10, "used": 2}}
```

每用户最多 **10 个** 订阅频道；超出返回 `{"action":"error","code":42902}`。

### 4.3 频道列表

| 频道 | 内容 | 推送频率 | 权限 |
|------|------|---------|------|
| `market.price` | BTC 实时价格（含涨跌、源、质量状态） | ≤ 1 次/秒（节流聚合） | ○ |
| `market.orderbook` | 订单簿增量 | ≤ 5 次/秒 | ○ |
| `engine.regime` | Regime/风险/周期状态变化事件 | 变化时推送 | ○ |
| `provider.status` | Provider 健康状态变化、failover 事件 | 变化时推送 | ○ |
| `job.progress` | 回测/模拟/批量测试任务进度 | 进度变化时 | U |
| `job.logs` | 指定任务实时日志（需带 `job_id` 订阅） | 实时 | A |
| `portfolio.alerts` | 个人计划提醒（下次投入、偏离告警） | 事件时 | U |

### 4.4 推送消息格式

```json
{
  "channel": "market.price",
  "data": {
    "symbol": "BTC-USDT",
    "price": 108240.5,
    "change_24h_pct": 2.31,
    "source": "binance",
    "quality_status": "VERIFIED"
  },
  "timestamp": "2026-09-27T06:12:33.482Z"
}
```

所有推送均携带 `channel` / `data` / `timestamp`；数据类频道推送必带 `source` 与 `quality_status`（与 REST meta 契约一致）。

---

## 5. 错误码体系

HTTP 状态码 + 6 位业务错误码（前 3 位 = HTTP 状态码）。

| 区间 | 类别 | 示例 |
|------|------|------|
| 400xxx | 请求参数错误 | `40001` 参数缺失；`40002` 参数格式非法；`40003` 时间范围非法（start > end）；`40004` interval 不支持；`40005` cursor 无效/过期 |
| 401xxx | 认证错误 | `40101` 未携带 Token；`40102` Token 过期；`40103` Token 无效；`40104` Refresh Token 已吊销；`40105` 用户名或密码错误 |
| 403xxx | 权限不足 | `40301` 角色权限不足；`40302` 访问他人资源；`40303` 账号被禁用；`40304` 操作需要二次确认 |
| 404xxx | 资源不存在 | `40401` 资源不存在；`40402` 指标不存在；`40403` Provider 不存在；`40404` 历史日期超出数据覆盖范围 |
| 422xxx | 数据验证失败 | `42201` 业务规则冲突（如删除仍被引用的 Provider）；`42202` 数值超出合理范围（拒绝写入可疑数据）；`42203` 状态机非法迁移（如激活未通过验证的模型）；`42204` 回测参数不可行（数据覆盖不足） |
| 429xxx | 请求频率限制 | `42901` HTTP 请求超限（响应带 `Retry-After` 头）；`42902` WebSocket 订阅超限；`42903` 任务并发超限 |
| 500xxx | 服务器内部错误 | `50001` 内部错误（携带 request_id）；`50002` 数据库错误；`50003` 计算引擎错误 |
| 503xxx | 数据源不可用（降级） | `50301` 该数据类别所有 Provider 失败且无本地数据；`50302` Provider 认证失败待管理员处理；`50303` 系统维护中；`50304` 任务队列已满 |

**503 降级语义**：

- 有最近可信数据 → 返回 **200** + `quality_status: STALE`（前端显示黄条）；
- 完全无数据（如新指标从未抓到）→ 返回 **503** + `50301` + `data: null`（前端显示「数据源未配置/暂不可用」）；
- 503 响应必须带 `meta.degradation`：`{"category": "onchain", "affected_providers": [...], "last_good_data_at": "...", "retry_at": "..."}`。

---

## 6. Rate Limiting

### 6.1 配额

| 主体 | HTTP 配额 | 说明 |
|------|-----------|------|
| 匿名用户 | 60 req/min | 按 IP 计数 |
| 登录用户（User） | 300 req/min | 按 user_id 计数 |
| Analyst | 600 req/min | 研究类操作较多 |
| Admin | 1000 req/min | 管理操作 |
| WebSocket | 10 subscriptions/user（匿名 3） | 频道数限制 |
| 重操作单独限流 | `POST /backtest/run`、`/portfolio/simulate`、`/ai/chat`：10 req/min/用户；并发运行任务 ≤ 3/用户 | 防止计算资源耗尽 |

### 6.2 实现

- Redis 滑动窗口计数器（`ratelimit:{scope}:{id}:{window}`）；
- 超限返回 `429` + 错误码 `42901` + 响应头：

```text
X-RateLimit-Limit: 300
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1790446800
Retry-After: 32
```

- 所有成功响应均携带 `X-RateLimit-*` 头；
- 内部调度器调用（本地回环 + 服务间 Token）不计入用户配额；
- 触发限流事件写入日志（WARNING 级，见《19-日志设计》）。

---

## 7. 缓存与条件请求

| 策略 | 说明 |
|------|------|
| Redis 缓存 | 实时价格 5s、首页聚合 30s、指标历史 5min、指标定义 1h；缓存命中在 `meta.cache_hit=true` 标识 |
| ETag | 低频变更资源（指标定义、Provider 配置）返回 ETag，支持 `If-None-Match` → 304 |
| HTTP 缓存头 | 历史数据（不可变）：`Cache-Control: public, max-age=3600`；实时数据：`no-cache` |
| 缓存原则 | 缓存只是加速层，**数据库是唯一长期数据源**（需求第三十七节）；缓存不可用时直接查库，不报错 |

---

## 8. 版本管理

- 当前版本 `v1`，路径前缀 `/api/v1`；
- 破坏性变更（字段删除/语义变化）→ 新版本 `v2`，`v1` 保留 ≥ 6 个月并在响应头加 `Deprecation` 与 `Sunset` 通知；
- 非破坏性变更（新增可选字段/新增端点）直接在 `v1` 发布，客户端必须容忍未知字段；
- 所有端点自动生成 OpenAPI 3.1 文档：`/docs`（Swagger UI）、`/redoc`；生产环境文档需 Admin 登录访问；
- 引擎输出、回测结果永久绑定 `model_version` 与数据版本（需求第四十三节），API 响应中如实返回，升级不覆盖历史。

---

## 附：端点与页面映射速查

| 页面 | 主要依赖端点 |
|------|-------------|
| 首页 | `/market/price`、`/engine/regime`、`/engine/explain`、`/portfolio/plans`、`/system/data-quality`、WS `market.price`+`provider.status` |
| BTC 行情 | `/market/*`、`/indicators/{name}`、WS `market.price`+`market.orderbook` |
| 市场周期/估值/风险 | `/engine/cycle|valuation|risk`、`/engine/history`、`/engine/explain` |
| 链上/资金流 | `/onchain/*` |
| ETF/衍生品/Options | `/etf/*`、`/derivatives/*`、`/options/*` |
| 宏观/情绪 | `/macro/*`、`/sentiment/*` |
| 历史回放 | `/engine/history?date=`、各数据端点 + `as_of` |
| 我的计划/资产/定投模拟 | `/portfolio/*` |
| 回测/策略/模型 | `/backtest/*`、`/strategies/*`、`/models/*`、WS `job.progress` |
| AI 助手 | `/ai/*`（SSE） |
| 数据源中心/质量/任务/后台 | `/providers/*`、`/system/*`、WS `job.logs` |

---

## 相关文档

| 文档 | 关联说明 |
|------|----------|
| [16-page-architecture.md](16-page-architecture.md) | 前端页面与组件设计（API 消费方） |
| [18-permission-design.md](18-permission-design.md) | 权限与角色定义（API 访问控制） |
| [10-data-quality.md](10-data-quality.md) | 数据质量状态枚举定义（meta.quality_status 字段值） |
| [04-provider-architecture.md](04-provider-architecture.md) | Provider 接口与状态枚举 |
| [09-core-tables.md](09-core-tables.md) | 数据库表结构（API 数据源） |
| [13-model-architecture.md](13-model-architecture.md) | 引擎输出结构（Engine API 响应字段来源） |
| [21-deployment.md](21-deployment.md) | 部署架构（Nginx 反向代理与 WebSocket 配置） |
