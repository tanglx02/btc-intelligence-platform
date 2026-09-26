# 19 - 日志设计（Logging Design）

> BTC 全市场智能研究平台 · 日志与可观测性设计
>
> 技术栈：loguru（Python 后端）+ PostgreSQL（持久化日志）+ Redis（实时缓冲）

---

## 概述

平台是长期运行（5-10 年以上）的数据系统，日志承担四个职责：

1. **故障诊断**：Provider 失败、failover 切换、数据质量问题的完整现场还原；
2. **数据溯源**：每一次外部 API 调用、每一条数据写入都可追踪（配合「所有数据都能够追溯」的产品原则）；
3. **安全审计**：用户与管理员行为记录（与《18-权限设计》审计日志互补）；
4. **运行观测**：健康检查、性能指标、任务执行状态。

核心约束：**任何日志不得包含敏感明文**（API Key、密码、Token）；**日志系统故障不得影响业务主流程**（异步写入 + 失败降级）。

---

## 目录

1. [日志框架与初始化](#1-日志框架与初始化)
2. [日志分级](#2-日志分级)
3. [结构化日志格式](#3-结构化日志格式json-lines)
4. [日志分类与存储](#4-日志分类与存储)
5. [日志轮转与保留](#5-日志轮转与保留)
6. [可观测性端点](#6-可观测性端点)
7. [敏感信息脱敏](#7-敏感信息脱敏)
8. [第三方库日志拦截](#8-第三方库日志拦截)
9. [request_id 全链路追踪](#9-request_id-全链路追踪)

---

## 1. 日志框架与初始化

使用 **loguru**，在 `app/core/logging.py::setup_logging()` 统一初始化（应用启动时调用一次）：

```python
def setup_logging() -> None:
    logger.remove()  # 移除默认 handler

    # ① 控制台：开发环境彩色可读格式；生产环境 JSON
    logger.add(sys.stderr, level=console_level,
               format=dev_format if settings.debug else json_format,
               colorize=settings.debug)

    # ② 应用主日志文件：JSON Lines，按大小+按天轮转
    logger.add("logs/app/app_{time:YYYY-MM-DD}.log", level="INFO",
               serialize=True, rotation="100 MB", retention="30 days",
               compression="gz", encoding="utf-8", enqueue=True)

    # ③ 错误专用文件：ERROR 及以上单独存放，便于告警扫描
    logger.add("logs/app/error_{time:YYYY-MM-DD}.log", level="ERROR",
               serialize=True, rotation="100 MB", retention="90 days",
               compression="gz", encoding="utf-8", enqueue=True)

    # ④ Provider 调用日志文件（量大，独立存放）
    logger.add("logs/provider/provider_{time:YYYY-MM-DD}.log", level="DEBUG",
               serialize=True, filter=lambda r: r["extra"].get("log_type") == "provider",
               rotation="100 MB", retention="30 days", compression="gz",
               encoding="utf-8", enqueue=True)

    # ⑤ PostgreSQL sink（关键类别入库，供后台页面查询，异步批量）
    logger.add(PGBatchSink(table="provider_request_logs",
               types=("provider",)), level="DEBUG", enqueue=True)
    logger.add(PGBatchSink(table="failover_event_logs",
               types=("failover",)), level="INFO", enqueue=True)
    # ... 其余入库类别同理
```

要点：

- `enqueue=True`：全部 sink 异步写入（独立线程/队列），日志慢不阻塞业务；
- `serialize=True`：文件输出为 JSON Lines（第 3 节格式）；
- `PGBatchSink`：自定义 sink，攒批（500 条或 2s）写入 TimescaleDB，写入失败时降级回退到本地文件并告警，**绝不抛出到业务层**；
- 日志目录结构：

```text
logs/
├── app/            # 应用主日志 + 错误日志
├── provider/       # Provider 调用日志
├── quality/        # 数据质量日志
├── jobs/           # 任务执行日志
└── audit/          # 审计日志文件副本（主存储在 DB）
```

---

## 2. 日志分级

| 级别 | 使用场景 | 本项目典型事件 |
|------|---------|---------------|
| DEBUG | 开发诊断细节，生产默认关闭（可按模块开启） | Provider 请求/响应原文（脱敏后）、指标计算中间值、缓存读写细节 |
| INFO | 正常运行的关键节点 | 应用启动/关闭、任务开始与成功、failover 切换与恢复、数据同步进度、模型版本加载 |
| WARNING | 异常但系统可继续/已降级 | 单 Provider 请求失败（已切换备用）、数据 STALE、数据冲突 CONFLICT、质量检查未通过、限流触发、缓存不可用回退 DB |
| ERROR | 功能失败但不影响整体 | 某数据类别全部 Provider 失败、任务执行失败、DB 写入失败、数据格式异常（Data Format Error） |
| CRITICAL | 威胁系统整体运行 | 数据库不可达、Redis+DB 同时故障、调度器崩溃、主密钥不可用、磁盘空间不足 |

级别控制：

- 环境变量 `LOG_LEVEL` 全局级别（生产默认 INFO）；
- 支持按模块覆盖：`LOG_LEVEL_PROVIDER=DEBUG`、`LOG_LEVEL_JOBS=INFO`（通过 loguru filter 实现），便于线上临时诊断单个 Provider；
- 生产环境 DEBUG 日志默认不落盘，仅当动态开启（管理端点）后限时 30 分钟自动关闭。

---

## 3. 结构化日志格式（JSON Lines）

每行一个 JSON 对象，统一 schema：

```json
{
  "timestamp": "2026-09-27T06:12:33.482Z",
  "level": "WARNING",
  "module": "app.providers.base.failover",
  "function": "fetch_with_failover",
  "line": 142,
  "message": "Provider glassnode 请求失败，切换至 cryptoquant",
  "log_type": "provider",
  "request_id": "req_8f3a2b1c",
  "job_id": null,
  "provider_id": "glassnode",
  "extra": {
    "error_type": "TIMEOUT",
    "elapsed_ms": 10021,
    "endpoint": "/v2/entities/bitcoin/mvrv",
    "next_provider": "cryptoquant",
    "consecutive_failures": 3
  }
}
```

**固定字段**（所有日志必含）：

| 字段 | 说明 |
|------|------|
| `timestamp` | UTC ISO 8601，毫秒精度 |
| `level` | 日志级别 |
| `module` / `function` / `line` | 代码位置 |
| `message` | 人类可读消息（中文，不含敏感数据） |
| `log_type` | 日志分类（见第 4 节：`system` / `provider` / `quality` / `failover` / `audit` / `job` / `access`） |
| `request_id` | HTTP/WS 请求链路 ID（API 触发时） |
| `job_id` | 调度任务 ID（任务触发时） |

**extra 字段**：按 `log_type` 各带扩展字段（provider_id、metric、duration、error_type、user_id 等），schema 在第 4 节各分类中定义。

约定：结构化数据一律进 `extra`，禁止把键值对拼进 message 字符串（保证可检索性）。

---

## 4. 日志分类与存储

六类业务日志 + 一类访问日志，各自定义字段与存储去向：

### 4.1 系统日志（log_type=system）

- **内容**：应用启动/关闭、配置加载（脱敏）、组件连接状态（DB/Redis/调度器）、未捕获异常、CRITICAL 事件；
- **存储**：文件 `logs/app/`；ERROR+ 同时入 `system_event_logs` 表（后台管理页展示）；
- **示例事件**：`app_started`、`scheduler_started`、`disk_space_low`、`unhandled_exception`。

### 4.2 Provider 日志（log_type=provider）

**每次外部 API 调用记录一条**（数据溯源的基础，配合需求「所有数据都能够追溯」）：

| extra 字段 | 说明 |
|-----------|------|
| `provider_id` / `category` | Provider 与数据类别（market/onchain/etf…） |
| `endpoint` / `method` / `params` | 请求详情（params 脱敏：Key 字段替换为 `<masked>`） |
| `http_status` | HTTP 状态码 |
| `elapsed_ms` | 耗时（用于健康评分的延迟因子） |
| `result` | SUCCESS / TIMEOUT / HTTP_429 / HTTP_403 / DNS_ERROR / TLS_ERROR / DATA_FORMAT_ERROR / EMPTY_DATA / QUALITY_REJECTED |
| `error_detail` | 错误摘要（截断 512 字符，脱敏） |
| `proxy` | 使用的代理线路标识（不含账号密码） |
| `raw_stored_id` | 原始响应入库 ID（raw_* 表关联，响应体不写日志文件，只写库） |

- **存储**：文件 `logs/provider/` + 入 `provider_request_logs` 表（TimescaleDB hypertable，量大，保留 30 天明细，之后仅保留按小时聚合统计 `provider_stats_hourly`，聚合永久保留）；
- **注意**：Provider 健康页的「最近成功/失败时间、连续失败次数、成功率」直接由该表实时聚合，日志即数据源。

### 4.3 数据质量日志（log_type=quality）

- **内容**：质量检查结果（完整性/新鲜度/交叉验证偏差）、CONFLICT 记录（各源数值与偏差百分比）、缺失发现与自动补洞尝试、异常数据拒写（防止错误数据覆盖正确数据）；
- **extra 字段**：`metric`、`check_type`（freshness/completeness/cross_validation/range）、`status`（PASS/WARN/FAIL）、`sources_compared`、`deviation_pct`、`rejected_value`；
- **存储**：文件 `logs/quality/` + 入 `data_quality_logs` 表；数据质量页直接查询该表。

### 4.4 Failover 事件日志（log_type=failover）

- **内容**：每次故障切换与恢复的完整现场（需求第五节）；
- **extra 字段**：`category`、`from_provider`、`to_provider`、`reason`（TIMEOUT/HTTP_429/AUTH_ERROR/DNS…）、`consecutive_failures`、`recovered`（是否恢复事件）、`recovery_success_count`（恢复阈值计数）；
- **级别**：切换 = WARNING，恢复 = INFO，全链失败 = ERROR；
- **存储**：文件 + 入 `provider_failover_events` 表（**永久保留**，量小且为关键审计数据）；数据源中心「Failover 事件流」直接读表。

### 4.5 用户操作日志（log_type=audit）

- **内容**：与《18-权限设计》第 5 节审计日志同一事件流——所有写操作与敏感读操作；
- **主存储**：`audit_logs` 表（同事务写入，保留 ≥ 3 年）；文件 `logs/audit/` 作为异地副本（只追加）；
- **额外记录**：`permission_denied` 事件（WARNING 级）用于安全扫描。

### 4.6 任务日志（log_type=job）

- **内容**：调度任务全生命周期——触发、开始、checkpoint 保存/恢复、进度、成功/失败/跳过/重试、耗时、写入行数；
- **extra 字段**：`job_name`（market_1m / onchain_1h / etf_daily…）、`run_id`、`trigger`（schedule/manual/retry）、`checkpoint`（断点位置，如 `2021-05-01`）、`rows_written`、`elapsed_ms`、`error`；
- **存储**：文件 `logs/jobs/` + 入 `system_job_runs` 表（执行历史）；系统任务页的「实时日志流」通过 Redis Pub/Sub（`job:log:{job_id}` 频道）推送给 WS `job.logs`；
- **级别**：开始/成功 = INFO，重试/跳过 = WARNING，失败 = ERROR。

### 4.7 访问日志（log_type=access）

- **内容**：HTTP/WS 请求摘要——方法、路径、状态码、耗时、user_id/匿名、IP、rate limit 命中；
- **存储**：文件（uvicorn access log 重定向到 loguru），不入库（量大低价值，聚合指标进 Prometheus，见第 6 节）；
- 健康检查端点（`/health`、`/ready`）的访问日志**静默**（避免刷屏）。

---

## 5. 日志轮转与保留

| 目标 | 轮转策略 | 保留 | 压缩 |
|------|---------|------|------|
| 应用/错误/质量/任务/审计文件 | **按大小 100MB + 按天**（先到先触发；loguru `rotation="100 MB"` 配合每日 rotation 规则） | **30 天**（error 与 audit 文件 90 天） | gz |
| Provider 文件日志 | 100MB / 天 | 30 天 | gz |
| `provider_request_logs` 表 | TimescaleDB 按天 chunk | 明细 30 天 → 自动聚合到 `provider_stats_hourly`（永久） | 列压缩 |
| `data_quality_logs` 表 | 按周 chunk | 180 天 | 列压缩 |
| `provider_failover_events` 表 | — | **永久** | — |
| `audit_logs` 表 | 按月 chunk | **≥ 3 年** | 列压缩 |
| `system_job_runs` 表 | 按周 chunk | 1 年 | 列压缩 |

- 轮转/清理由 loguru retention + TimescaleDB `add_retention_policy` 自动执行，纳入每日自检任务；
- 磁盘水位保护：日志盘使用率 > 85% 时 WARNING，> 95% 时自动删除最旧的压缩归档并 CRITICAL 告警；
- 备份：`logs/` 归档文件与关键日志表纳入《备份恢复设计》的备份范围（audit 与 failover 表必须备份）。

---

## 6. 可观测性端点

| 端点 | 用途 | 权限 | 说明 |
|------|------|------|------|
| `GET /health`（=`/api/v1/system/health`） | 存活+组件健康 | 匿名 | 返回 DB/Redis/调度器状态与各模块降级状态（见《17-API 设计》3.15 示例）；探活轻量查询，不做重检查 |
| `GET /ready` | 就绪检查 | 匿名 | 全部关键依赖可用才返回 200，否则 503 + 未就绪原因（供 systemd / Docker healthcheck / 部署编排使用） |
| `GET /metrics` | Prometheus 指标 | 生产内网/Admin | 见下方指标清单 |

### 6.1 Prometheus 指标清单

```text
# Provider
provider_requests_total{provider,category,result}        # 请求计数
provider_request_duration_seconds{provider,category}     # 耗时直方图
provider_consecutive_failures{provider}                  # 当前连续失败数
provider_failover_total{category,from,to,reason}         # 切换次数
provider_health_score{provider}                          # 健康评分

# 数据质量
data_quality_status{category,metric}                     # 0=INVALID 1=CONFLICT 2=STALE 3=ESTIMATED 4=VERIFIED
data_backfill_total{category,result}                     # 补洞次数

# 任务
job_runs_total{job,trigger,result}
job_duration_seconds{job}
job_last_success_timestamp{job}                          # 用于「数据太久没更新」告警

# API
http_requests_total{method,path,status}
http_request_duration_seconds{path}
ratelimit_hits_total{scope}
ws_connections_active / ws_subscriptions_active

# 系统
db_pool_usage / redis_latency_seconds / disk_usage_pct / app_uptime_seconds
```

### 6.2 告警规则（内置阈值，Admin 可调）

| 告警 | 条件 | 级别 |
|------|------|------|
| Provider 连续失败 | consecutive_failures ≥ 5 | WARNING |
| 类别全源失败 | 某 category 全部 Provider 失败 | ERROR（首页红色横幅） |
| 数据过期 | job_last_success 超过 2× 调度周期 | WARNING |
| 磁盘水位 | > 85% / 95% | WARNING / CRITICAL |
| 登录异常 | 同 IP 登录失败 ≥ 5 / token 重放 | WARNING（安全） |
| 任务失败 | 同任务连续失败 ≥ 3 | ERROR |

---

## 7. 敏感信息脱敏

**目标**：API Key、密码、Token、代理凭据在任何日志（文件/DB/控制台/异常堆栈）中都不出现明文。

### 7.1 机制

三层防线：

1. **入口脱敏（patcher）**：loguru 全局 patcher 对所有记录的 `extra` 与 message 执行正则/键名脱敏：

```python
SENSITIVE_KEYS = {"api_key", "apikey", "secret", "password", "token",
                  "access_token", "refresh_token", "authorization",
                  "fernet_key", "master_key", "proxy_url"}
SENSITIVE_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S+"), r"\1=<masked>"),
    (re.compile(r"(?i)(proxy://)[^@]+@"), r"\1<masked>@"),        # 代理账号密码
    (re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*"), "Bearer <masked>"),
]
# 键名匹配 → 值替换为 "<masked>"；保留尾4位提示时用 hint 字段单独传
```

2. **调用侧规范**：代码约定——Provider 请求日志只记录 params 白名单字段；凭据类配置对象实现 `__repr__` 返回 `<Provider credentials masked>`，防止异常堆栈带出；
3. **出口兜底**：`PGBatchSink` 与文件 sink 序列化前再做一次模式扫描，命中即替换并记一条 WARNING（`sensitive_data_blocked`，用于发现脱敏遗漏）。

### 7.2 脱敏对照

| 数据 | 日志中呈现 |
|------|-----------|
| Provider API Key | `<masked>`（需要辨识时用 `key_hint: "sk-****abcd"`） |
| 用户密码 | 任何级别都不记录（连 `<masked>` 上下文都不出现） |
| JWT | `Bearer <masked>` + `jti` 前 8 位（可关联吊销） |
| 代理 URL | `socks5://<masked>@1.2.3.4:1080`（保留主机端口便于诊断线路） |
| 原始 API 响应 | 入 raw_* 表（本身即业务数据），日志只存 `raw_stored_id` 引用 |
| 个人资产数值 | 审计日志 detail 中金额保留（属审计必要信息），但用户身份仅存 user_id 不存用户名以外 PII |

### 7.3 验证

- 单元测试：构造含敏感字段的日志调用，断言输出全部脱敏（纳入《测试设计》安全测试项）；
- CI 检查：grep 扫描日志样例文件，命中敏感模式即失败。

---

## 8. 第三方库日志拦截

标准库 logging → loguru 桥接（InterceptHandler），统一格式与脱敏：

| 库 | 处理 |
|----|------|
| uvicorn | access log 重定向为 `log_type=access`；error log 拦截为 loguru ERROR |
| httpx / aiohttp | 拦截为 DEBUG（含请求 URL，params 中 Key 已脱敏）；连接级错误由 Provider 层记录，不重复 |
| sqlalchemy | 生产 WARNING（回显 SQL 仅在 DEBUG 模块开关下，且参数值不输出） |
| apscheduler | INFO 拦截（任务触发事件以本平台 job 日志为准，去重） |
| asyncio | 未处理异常强制 ERROR + 堆栈 |

原则：第三方库日志级别整体上压一级（DEBUG→TRACE 丢弃），避免噪音；任何库的日志都经过第 7 节脱敏 patcher。

---

## 9. request_id 全链路追踪

```text
HTTP 请求进入
  → 中间件生成 request_id（或沿用客户端 X-Request-Id）
  → 写入 contextvars（request_id_var）
  → logger 全局 patcher 自动注入每条日志
  → 同步透传：调用 Provider / 写库 / 发任务 都携带
  → 错误响应 error.request_id 返回给客户端（《17-API 设计》2.2）
  → 审计日志 request_id 字段关联

调度任务进入
  → 生成 job_run_id，同样机制注入
```

效果：用户报告「页面报错 req_8f3a2b1c」→ 一条命令捞出该请求全部日志：

```bash
grep '"request_id": "req_8f3a2b1c"' logs/app/*.log logs/provider/*.log
```

数据库侧：`provider_request_logs` / `audit_logs` / `data_quality_logs` 均含 `request_id` 列并建索引，后台管理页支持按 request_id 聚合查询完整调用链。

---

## 附：与现有代码的衔接

- 现有骨架 `backend/app/core/logging.py` 已实现基础 console + 按天文件输出；本设计为其目标形态：补充 JSON serialize、100MB 轮转、分类 sink、PGBatchSink、脱敏 patcher、第三方拦截；
- 配置项归入 `app/core/config.py::Settings`：`log_level`、`log_dir`、`log_json`（生产 true）、`log_retention_days`、各模块级别覆盖；
- 实现顺序遵循开发路线：随 Provider 层（第三阶段）落地 provider/failover 日志，随任务系统（第三十五节需求）落地 job 日志，随后台（第二十九阶段）落地查询页面。

---

## 相关文档

| 文档 | 关联说明 |
|------|----------|
| [10-data-quality.md](10-data-quality.md) | 数据质量状态枚举定义（Prometheus 指标中的质量状态映射） |
| [04-provider-architecture.md](04-provider-architecture.md) | Provider 日志与健康监控 |
| [05-failover-architecture.md](05-failover-architecture.md) | Failover 事件日志 |
| [17-api-design.md](17-api-design.md) | API 访问日志与健康检查端点 |
| [21-deployment.md](21-deployment.md) | 部署环境日志配置与轮转 |
| [18-permission-design.md](18-permission-design.md) | 审计日志表结构 |
