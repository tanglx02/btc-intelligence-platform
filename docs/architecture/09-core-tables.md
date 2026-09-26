# 09 - 核心数据表 DDL

> 本文档定义 BTC 全市场智能研究平台所有核心数据表的完整 DDL。
> 数据库：PostgreSQL 16 + TimescaleDB
> 所有时序表使用 TimescaleDB hypertable，公共字段遵循统一规范。

---

## 公共定义

### ENUM 类型

```sql
-- Provider 状态
CREATE TYPE provider_status AS ENUM (
    'ONLINE', 'DEGRADED', 'SLOW', 'RATE_LIMITED',
    'AUTH_ERROR', 'NETWORK_ERROR', 'DATA_ERROR', 'OFFLINE', 'DISABLED'
);

-- 数据质量状态
CREATE TYPE quality_status_type AS ENUM (
    'VERIFIED', 'ESTIMATED', 'STALE', 'CONFLICT', 'INVALID'
);

-- Provider 类别
CREATE TYPE provider_category AS ENUM (
    'MARKET', 'ONCHAIN', 'EXCHANGE_FLOW', 'ETF',
    'DERIVATIVES', 'OPTIONS', 'MACRO', 'SENTIMENT', 'NEWS'
);

-- 故障切换解决方式
CREATE TYPE failover_resolution AS ENUM (
    'AUTO_RECOVERED', 'MANUAL_RESET', 'TIMEOUT', 'PERMANENTLY_DISABLED'
);

-- 任务状态
CREATE TYPE job_status AS ENUM (
    'PENDING', 'RUNNING', 'PAUSED', 'COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED'
);

-- 同步状态
CREATE TYPE sync_status AS ENUM (
    'IDLE', 'SYNCING', 'PAUSED', 'COMPLETED', 'FAILED', 'RETRY'
);

-- 用户角色
CREATE TYPE user_role AS ENUM ('ADMIN', 'ANALYST', 'USER');

-- 交易方向
CREATE TYPE trade_side AS ENUM ('BUY', 'SELL');

-- 计划状态
CREATE TYPE plan_status AS ENUM ('ACTIVE', 'PAUSED', 'COMPLETED', 'CANCELLED');

-- 市场周期阶段
CREATE TYPE cycle_phase AS ENUM (
    'DEEP_BEAR', 'BEAR', 'BOTTOM_BUILDING', 'RECOVERY',
    'UPTREND', 'ACCELERATION', 'DISTRIBUTION', 'TOP_RISK', 'DECLINE'
);

-- 估值等级
CREATE TYPE valuation_level AS ENUM (
    'DEEP_UNDERVALUED', 'UNDERVALUED', 'FAIR', 'OVERVALUED', 'EXTREME_OVERVALUED'
);

-- 风险等级
CREATE TYPE risk_level AS ENUM (
    'VERY_LOW', 'LOW', 'MODERATE', 'HIGH', 'VERY_HIGH', 'EXTREME'
);

-- 信号方向
CREATE TYPE signal_direction AS ENUM ('BULLISH', 'BEARISH', 'NEUTRAL');

-- 模型状态
CREATE TYPE model_status AS ENUM (
    'DRAFT', 'TRAINING', 'VALIDATED', 'DEPLOYED', 'DEPRECATED', 'REJECTED'
);

-- 回测状态
CREATE TYPE backtest_status AS ENUM (
    'PENDING', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED'
);

-- K线时间间隔
CREATE TYPE candle_interval AS ENUM ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w');
```

### 触发器函数

```sql
-- 自动更新 updated_at 字段
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 通用 updated_at 触发器模板（应用到每张需要 updated_at 的表）
-- CREATE TRIGGER set_updated_at BEFORE UPDATE ON table_name
--     FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

---

## Group 1：Provider 管理

### 1.1 assets — 资产定义字典

> 定义系统中支持的所有资产（BTC 为主资产，可扩展 ETH 等）

```sql
CREATE TABLE assets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol          VARCHAR(20) NOT NULL UNIQUE,
    name            VARCHAR(100) NOT NULL,
    asset_type      VARCHAR(30) NOT NULL DEFAULT 'CRYPTO',
    chain           VARCHAR(30) DEFAULT 'bitcoin',
    decimals        SMALLINT NOT NULL DEFAULT 8,
    is_active       BOOLEAN NOT NULL DEFAULT true,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE assets IS '资产定义字典，系统支持的所有资产元数据';
COMMENT ON COLUMN assets.symbol IS '资产符号，如 BTC、ETH';
COMMENT ON COLUMN assets.asset_type IS '资产类型：CRYPTO / FIAT / COMMODITY / TOKEN';
COMMENT ON COLUMN assets.chain IS '所属链：bitcoin / ethereum / solana 等';
COMMENT ON COLUMN assets.decimals IS '精度位数';

CREATE TRIGGER set_updated_at BEFORE UPDATE ON assets
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 初始数据
INSERT INTO assets (symbol, name, asset_type, chain, decimals) VALUES
    ('BTC', 'Bitcoin', 'CRYPTO', 'bitcoin', 8),
    ('USD', 'US Dollar', 'FIAT', NULL, 2),
    ('USDT', 'Tether', 'CRYPTO', 'ethereum', 6);
```

### 1.2 providers — Provider 注册信息

> 所有数据源的注册表，包含配置、状态、优先级等核心信息

```sql
CREATE TABLE providers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(100) NOT NULL UNIQUE,
    category            provider_category NOT NULL,
    base_url            VARCHAR(500) NOT NULL,
    api_key_encrypted   TEXT,
    proxy_config        JSONB DEFAULT '{"type": "direct"}',
    timeout_config      JSONB DEFAULT '{"connect": 5000, "read": 30000, "write": 10000}',
    retry_config        JSONB DEFAULT '{"max_retries": 3, "backoff_factor": 2, "backoff_max": 60}',
    rate_limit          INTEGER DEFAULT 60,
    rate_limit_window   INTEGER DEFAULT 60,
    priority            INTEGER NOT NULL DEFAULT 100,
    is_enabled          BOOLEAN NOT NULL DEFAULT true,
    is_locked           BOOLEAN NOT NULL DEFAULT false,
    status              provider_status NOT NULL DEFAULT 'OFFLINE',
    health_score        NUMERIC(5,2) DEFAULT 0,
    recovery_threshold  INTEGER DEFAULT 3,
    failure_threshold   INTEGER DEFAULT 5,
    description         TEXT,
    supported_symbols   TEXT[] DEFAULT ARRAY['BTC'],
    supported_intervals TEXT[] DEFAULT '{}',
    last_success_at     TIMESTAMPTZ,
    last_failure_at     TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE providers IS 'Provider 数据源注册表，所有外部数据源在此注册管理';
COMMENT ON COLUMN providers.name IS 'Provider 唯一名称标识';
COMMENT ON COLUMN providers.category IS '数据类别：MARKET / ONCHAIN / ETF / DERIVATIVES 等';
COMMENT ON COLUMN providers.api_key_encrypted IS 'API Key（加密存储）';
COMMENT ON COLUMN providers.proxy_config IS '代理配置：{"type":"http|socks5|direct","host":"...","port":...}';
COMMENT ON COLUMN providers.timeout_config IS '超时配置（毫秒）：connect / read / write';
COMMENT ON COLUMN providers.retry_config IS '重试配置：max_retries / backoff_factor / backoff_max';
COMMENT ON COLUMN providers.rate_limit IS '速率限制：每窗口最大请求数';
COMMENT ON COLUMN providers.priority IS '优先级，数字越小优先级越高';
COMMENT ON COLUMN providers.is_locked IS '是否锁定优先级（管理员手动锁定后自动评分不调整）';
COMMENT ON COLUMN providers.health_score IS '综合健康评分 0-100';
COMMENT ON COLUMN providers.recovery_threshold IS '恢复阈值：连续成功N次后才恢复为主数据源';
COMMENT ON COLUMN providers.failure_threshold IS '故障阈值：连续失败N次后标记为故障';

CREATE INDEX idx_providers_category ON providers (category) WHERE is_enabled = true;
CREATE INDEX idx_providers_priority ON providers (category, priority) WHERE is_enabled = true;
CREATE INDEX idx_providers_status ON providers (status);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON providers
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 1.3 provider_health — 健康状态快照

> 每分钟采集一次的 Provider 健康状态快照，用于趋势分析和故障检测

```sql
CREATE TABLE provider_health (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id         UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    check_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status              provider_status NOT NULL,
    response_time_ms    INTEGER,
    success_rate_1h     NUMERIC(5,2),
    success_rate_24h    NUMERIC(5,2),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    today_failures      INTEGER NOT NULL DEFAULT 0,
    today_requests      INTEGER NOT NULL DEFAULT 0,
    data_latency_ms     INTEGER,
    last_error          TEXT,
    last_error_type     VARCHAR(50),
    http_status         INTEGER,
    is_rate_limited     BOOLEAN DEFAULT false,
    metrics             JSONB DEFAULT '{}',
    PRIMARY KEY (id, check_time)
);

SELECT create_hypertable('provider_health', 'check_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE provider_health IS 'Provider 健康状态快照（高频时序数据）';
COMMENT ON COLUMN provider_health.response_time_ms IS '响应时间（毫秒）';
COMMENT ON COLUMN provider_health.success_rate_1h IS '过去1小时成功率（%）';
COMMENT ON COLUMN provider_health.data_latency_ms IS '数据延迟：当前时间 - 最新数据时间';
COMMENT ON COLUMN provider_health.metrics IS '扩展指标 JSONB';

CREATE INDEX idx_provider_health_pid_time ON provider_health (provider_id, check_time DESC);

ALTER TABLE provider_health SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'provider_id',
    timescaledb.compress_orderby = 'check_time DESC'
);
SELECT add_compression_policy('provider_health', INTERVAL '7 days');
SELECT add_retention_policy('provider_health', INTERVAL '2 years');
```

### 1.4 provider_scores — 评分历史

> 每小时计算一次的 Provider 综合评分，用于智能优先级调整

```sql
CREATE TABLE provider_scores (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id         UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    scored_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accuracy_score      NUMERIC(5,2) NOT NULL DEFAULT 0,
    latency_score       NUMERIC(5,2) NOT NULL DEFAULT 0,
    stability_score     NUMERIC(5,2) NOT NULL DEFAULT 0,
    completeness_score  NUMERIC(5,2) NOT NULL DEFAULT 0,
    consistency_score   NUMERIC(5,2) NOT NULL DEFAULT 0,
    overall_score       NUMERIC(5,2) NOT NULL DEFAULT 0,
    rank                INTEGER,
    score_details       JSONB DEFAULT '{}',
    PRIMARY KEY (id, scored_at)
);

SELECT create_hypertable('provider_scores', 'scored_at',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE provider_scores IS 'Provider 综合评分历史';
COMMENT ON COLUMN provider_scores.accuracy_score IS '数据准确性评分 0-100';
COMMENT ON COLUMN provider_scores.latency_score IS '响应速度评分 0-100';
COMMENT ON COLUMN provider_scores.stability_score IS '稳定性评分 0-100';
COMMENT ON COLUMN provider_scores.completeness_score IS '数据完整性评分 0-100';
COMMENT ON COLUMN provider_scores.consistency_score IS '与其他 Provider 一致性评分 0-100';
COMMENT ON COLUMN provider_scores.overall_score IS '综合评分 0-100（加权）';
COMMENT ON COLUMN provider_scores.rank IS '同 category 内排名';

CREATE INDEX idx_provider_scores_pid ON provider_scores (provider_id, scored_at DESC);
CREATE INDEX idx_provider_scores_rank ON provider_scores (scored_at DESC, rank);

ALTER TABLE provider_scores SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'provider_id',
    timescaledb.compress_orderby = 'scored_at DESC'
);
SELECT add_compression_policy('provider_scores', INTERVAL '30 days');
```

### 1.5 provider_failover_events — 故障切换事件

> 记录每次 Provider 故障切换的完整上下文

```sql
CREATE TABLE provider_failover_events (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    from_provider_id    UUID NOT NULL REFERENCES providers(id),
    to_provider_id      UUID REFERENCES providers(id),
    data_category       provider_category NOT NULL,
    symbol              VARCHAR(20) DEFAULT 'BTC',
    trigger_reason      VARCHAR(50) NOT NULL,
    error_message       TEXT,
    occurred_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at         TIMESTAMPTZ,
    resolution_type     failover_resolution,
    duration_seconds    INTEGER,
    requests_affected   INTEGER DEFAULT 0,
    context             JSONB DEFAULT '{}',
    PRIMARY KEY (id, occurred_at)
);

SELECT create_hypertable('provider_failover_events', 'occurred_at',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE provider_failover_events IS 'Provider 故障切换事件记录';
COMMENT ON COLUMN provider_failover_events.trigger_reason IS '触发原因：TIMEOUT / HTTP_429 / HTTP_500 / DNS_FAIL / AUTH_ERROR / DATA_ERROR';
COMMENT ON COLUMN provider_failover_events.resolution_type IS '解决方式：AUTO_RECOVERED / MANUAL_RESET / TIMEOUT / PERMANENTLY_DISABLED';

CREATE INDEX idx_failover_from ON provider_failover_events (from_provider_id, occurred_at DESC);
CREATE INDEX idx_failover_category ON provider_failover_events (data_category, occurred_at DESC);

ALTER TABLE provider_failover_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'data_category',
    timescaledb.compress_orderby = 'occurred_at DESC'
);
SELECT add_compression_policy('provider_failover_events', INTERVAL '90 days');
```

### 1.6 provider_requests — 请求日志（采样）

> Provider API 请求日志，采样存储（失败 100%，成功 10%）

```sql
CREATE TABLE provider_requests (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id         UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    request_time        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    method              VARCHAR(10) NOT NULL DEFAULT 'GET',
    url                 TEXT NOT NULL,
    endpoint            VARCHAR(200),
    status_code         INTEGER,
    response_time_ms    INTEGER,
    error_type          VARCHAR(50),
    error_message       TEXT,
    request_params      JSONB DEFAULT '{}',
    response_size_bytes INTEGER,
    is_success          BOOLEAN NOT NULL DEFAULT true,
    is_sampled          BOOLEAN NOT NULL DEFAULT false,
    data_category       provider_category,
    PRIMARY KEY (id, request_time)
);

SELECT create_hypertable('provider_requests', 'request_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE provider_requests IS 'Provider 请求日志（采样存储）';
COMMENT ON COLUMN provider_requests.is_sampled IS '是否为采样记录（成功请求仅保留部分）';
COMMENT ON COLUMN provider_requests.error_type IS '错误类型：TIMEOUT / HTTP_ERROR / AUTH / RATE_LIMIT / NETWORK / DATA';

CREATE INDEX idx_requests_provider ON provider_requests (provider_id, request_time DESC);
CREATE INDEX idx_requests_failed ON provider_requests (provider_id, request_time DESC) WHERE is_success = false;

ALTER TABLE provider_requests SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'provider_id',
    timescaledb.compress_orderby = 'request_time DESC'
);
SELECT add_compression_policy('provider_requests', INTERVAL '3 days');
SELECT add_retention_policy('provider_requests', INTERVAL '30 days');
```

---

## Group 2：原始数据

### 2.1 raw_market_data — 原始市场数据响应

> 保存市场行情 API 的完整原始响应，支持数据重放

```sql
CREATE TABLE raw_market_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTC',
    data_type           VARCHAR(50) NOT NULL,
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    response_headers    JSONB DEFAULT '{}',
    response_size_bytes INTEGER,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_market_data', 'fetch_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE raw_market_data IS '市场行情 API 原始响应存储';
COMMENT ON COLUMN raw_market_data.data_type IS '数据类型：PRICE / OHLCV / VOLUME / ORDERBOOK / TRADES';
COMMENT ON COLUMN raw_market_data.raw_response IS '完整 API 响应 JSON';

CREATE INDEX idx_raw_market_source ON raw_market_data (source_id, fetch_time DESC);
CREATE INDEX idx_raw_market_symbol ON raw_market_data (symbol, data_type, fetch_time DESC);
CREATE INDEX idx_raw_market_response ON raw_market_data USING GIN (raw_response jsonb_path_ops);

ALTER TABLE raw_market_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source_id,data_type',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_market_data', INTERVAL '7 days');
SELECT add_retention_policy('raw_market_data', INTERVAL '3 years');
```

### 2.2 raw_onchain_data — 原始链上数据响应

```sql
CREATE TABLE raw_onchain_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name         VARCHAR(100) NOT NULL,
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_onchain_data', 'fetch_time',
    chunk_time_interval => INTERVAL '7 days',
    create_default_indexes => false
);

COMMENT ON TABLE raw_onchain_data IS '链上数据 API 原始响应存储';
COMMENT ON COLUMN raw_onchain_data.metric_name IS '链上指标名称：MVRV / SOPR / NUPL 等';

CREATE INDEX idx_raw_onchain_source ON raw_onchain_data (source_id, fetch_time DESC);
CREATE INDEX idx_raw_onchain_metric ON raw_onchain_data (metric_name, fetch_time DESC);

ALTER TABLE raw_onchain_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_onchain_data', INTERVAL '30 days');
SELECT add_retention_policy('raw_onchain_data', INTERVAL '5 years');
```

### 2.3 raw_etf_data — 原始 ETF 数据响应

```sql
CREATE TABLE raw_etf_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker              VARCHAR(20) NOT NULL,
    data_type           VARCHAR(50) NOT NULL DEFAULT 'FLOW',
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_etf_data', 'fetch_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE raw_etf_data IS 'ETF 数据 API 原始响应存储';
COMMENT ON COLUMN raw_etf_data.ticker IS 'ETF 代码：IBIT / FBTC / GBTC 等';
COMMENT ON COLUMN raw_etf_data.data_type IS 'FLOW / HOLDINGS / NAV / PRICE';

CREATE INDEX idx_raw_etf_source ON raw_etf_data (source_id, fetch_time DESC);
CREATE INDEX idx_raw_etf_ticker ON raw_etf_data (ticker, fetch_time DESC);

ALTER TABLE raw_etf_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_etf_data', INTERVAL '30 days');
SELECT add_retention_policy('raw_etf_data', INTERVAL '5 years');
```

### 2.4 raw_derivatives_data — 原始衍生品数据响应

```sql
CREATE TABLE raw_derivatives_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(30) NOT NULL DEFAULT 'BTC',
    exchange            VARCHAR(50) NOT NULL,
    data_type           VARCHAR(50) NOT NULL,
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_derivatives_data', 'fetch_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE raw_derivatives_data IS '衍生品数据 API 原始响应存储';
COMMENT ON COLUMN raw_derivatives_data.data_type IS 'FUNDING / OPEN_INTEREST / LIQUIDATION / LONG_SHORT / BASIS';

CREATE INDEX idx_raw_deriv_source ON raw_derivatives_data (source_id, fetch_time DESC);
CREATE INDEX idx_raw_deriv_type ON raw_derivatives_data (exchange, data_type, fetch_time DESC);

ALTER TABLE raw_derivatives_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange,data_type',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_derivatives_data', INTERVAL '7 days');
SELECT add_retention_policy('raw_derivatives_data', INTERVAL '3 years');
```

### 2.5 raw_macro_data — 原始宏观数据响应

```sql
CREATE TABLE raw_macro_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    series_id           VARCHAR(100) NOT NULL,
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_macro_data', 'fetch_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE raw_macro_data IS '宏观数据 API 原始响应存储';
COMMENT ON COLUMN raw_macro_data.series_id IS '宏观数据系列 ID：DXY / FED_RATE / CPI 等';

CREATE INDEX idx_raw_macro_series ON raw_macro_data (series_id, fetch_time DESC);
CREATE INDEX idx_raw_macro_source ON raw_macro_data (source_id, fetch_time DESC);

ALTER TABLE raw_macro_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'series_id',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_macro_data', INTERVAL '30 days');
-- 宏观数据永久保留
```

### 2.6 raw_sentiment_data — 原始情绪数据响应

```sql
CREATE TABLE raw_sentiment_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type         VARCHAR(50) NOT NULL,
    raw_response        JSONB NOT NULL,
    endpoint            TEXT NOT NULL,
    request_params      JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);

SELECT create_hypertable('raw_sentiment_data', 'fetch_time',
    chunk_time_interval => INTERVAL '7 days',
    create_default_indexes => false
);

COMMENT ON TABLE raw_sentiment_data IS '情绪数据 API 原始响应存储';
COMMENT ON COLUMN raw_sentiment_data.source_type IS '来源类型：FEAR_GREED / GOOGLE_TRENDS / SOCIAL / NEWS';

CREATE INDEX idx_raw_sentiment_source ON raw_sentiment_data (source_id, fetch_time DESC);
CREATE INDEX idx_raw_sentiment_type ON raw_sentiment_data (source_type, fetch_time DESC);

ALTER TABLE raw_sentiment_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source_type',
    timescaledb.compress_orderby = 'fetch_time DESC'
);
SELECT add_compression_policy('raw_sentiment_data', INTERVAL '7 days');
SELECT add_retention_policy('raw_sentiment_data', INTERVAL '2 years');
```

---

## Group 3：标准化市场数据

### 3.1 market_prices — 标准化价格

> 经过交叉验证的标准化实时价格数据，系统最核心的数据表

```sql
CREATE TABLE market_prices (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT',
    price               NUMERIC(20,8) NOT NULL,
    bid                 NUMERIC(20,8),
    ask                 NUMERIC(20,8),
    spread              NUMERIC(20,8),
    volume_24h          NUMERIC(24,4),
    quote_volume_24h    NUMERIC(24,4),
    market_cap          NUMERIC(24,2),
    high_24h            NUMERIC(20,8),
    low_24h             NUMERIC(20,8),
    price_change_pct_24h NUMERIC(8,4),
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    cross_validated     BOOLEAN NOT NULL DEFAULT false,
    validation_sources  INTEGER DEFAULT 1,
    deviation_pct       NUMERIC(8,6),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('market_prices', 'observation_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE market_prices IS '标准化市场价格数据（核心表）';
COMMENT ON COLUMN market_prices.symbol IS '交易对：BTCUSDT / BTCUSD';
COMMENT ON COLUMN market_prices.price IS '最新成交价格（USD/USDT）';
COMMENT ON COLUMN market_prices.cross_validated IS '是否经过多源交叉验证';
COMMENT ON COLUMN market_prices.deviation_pct IS '多源价格偏差百分比';
COMMENT ON COLUMN market_prices.quality_status IS '数据质量：VERIFIED=多源一致 / CONFLICT=多源不一致 / STALE=超时未更新';

CREATE INDEX idx_prices_symbol_time ON market_prices (symbol, observation_time DESC);
CREATE INDEX idx_prices_source ON market_prices (source_id, observation_time DESC);
CREATE INDEX idx_prices_quality ON market_prices (observation_time DESC) WHERE quality_status != 'VERIFIED';

ALTER TABLE market_prices SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('market_prices', INTERVAL '7 days');
```

### 3.2 candles — OHLCV K线

> 标准化 K 线数据，按时间粒度存储，系统最频繁查询的表之一

```sql
CREATE TABLE candles (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT',
    interval            candle_interval NOT NULL,
    open                NUMERIC(20,8) NOT NULL,
    high                NUMERIC(20,8) NOT NULL,
    low                 NUMERIC(20,8) NOT NULL,
    close               NUMERIC(20,8) NOT NULL,
    volume              NUMERIC(24,4) NOT NULL DEFAULT 0,
    quote_volume        NUMERIC(24,4),
    trades              INTEGER,
    taker_buy_volume    NUMERIC(24,4),
    taker_sell_volume   NUMERIC(24,4),
    vwap                NUMERIC(20,8),
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('candles', 'observation_time',
    chunk_time_interval => INTERVAL '7 days',
    create_default_indexes => false
);

COMMENT ON TABLE candles IS 'OHLCV K线数据（多时间粒度）';
COMMENT ON COLUMN candles.interval IS 'K线时间间隔：1m / 5m / 15m / 30m / 1h / 4h / 1d / 1w';
COMMENT ON COLUMN candles.taker_buy_volume IS '主动买入成交量';
COMMENT ON COLUMN candles.taker_sell_volume IS '主动卖出成交量';
COMMENT ON COLUMN candles.vwap IS '成交量加权平均价格';

CREATE UNIQUE INDEX idx_candles_unique ON candles (symbol, interval, observation_time);
CREATE INDEX idx_candles_symbol_interval ON candles (symbol, interval, observation_time DESC);
CREATE INDEX idx_candles_source ON candles (source_id, observation_time DESC);

ALTER TABLE candles SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,interval',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('candles', INTERVAL '30 days');
```

### 3.3 orderbooks — 订单簿快照

> 订单簿深度快照，记录买卖盘口分布

```sql
CREATE TABLE orderbooks (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT',
    exchange            VARCHAR(50) NOT NULL,
    bids                JSONB NOT NULL DEFAULT '[]',
    asks                JSONB NOT NULL DEFAULT '[]',
    bid_depth           NUMERIC(24,4),
    ask_depth           NUMERIC(24,4),
    spread              NUMERIC(20,8),
    spread_pct          NUMERIC(10,6),
    mid_price           NUMERIC(20,8),
    imbalance           NUMERIC(10,6),
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('orderbooks', 'observation_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE orderbooks IS '订单簿深度快照（top 50 档位）';
COMMENT ON COLUMN orderbooks.bids IS '买盘 [[price, quantity], ...]';
COMMENT ON COLUMN orderbooks.asks IS '卖盘 [[price, quantity], ...]';
COMMENT ON COLUMN orderbooks.imbalance IS '买卖不平衡度 (bid_depth - ask_depth) / (bid_depth + ask_depth)';

CREATE INDEX idx_orderbooks_symbol ON orderbooks (symbol, exchange, observation_time DESC);

ALTER TABLE orderbooks SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,exchange',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('orderbooks', INTERVAL '3 days');
SELECT add_retention_policy('orderbooks', INTERVAL '90 days');
```

### 3.4 trades — 逐笔成交

> 逐笔成交数据，数据量极大，仅保留近期

```sql
CREATE TABLE trades (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT',
    exchange            VARCHAR(50) NOT NULL,
    trade_id            VARCHAR(50) NOT NULL,
    price               NUMERIC(20,8) NOT NULL,
    quantity            NUMERIC(20,8) NOT NULL,
    quote_quantity      NUMERIC(24,4),
    side                trade_side NOT NULL,
    is_buyer_maker      BOOLEAN NOT NULL DEFAULT false,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('trades', 'observation_time',
    chunk_time_interval => INTERVAL '1 day',
    create_default_indexes => false
);

COMMENT ON TABLE trades IS '逐笔成交数据（高容量，短期保留）';
COMMENT ON COLUMN trades.is_buyer_maker IS '买方是否为 Maker';

CREATE INDEX idx_trades_symbol ON trades (symbol, exchange, observation_time DESC);

ALTER TABLE trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol,exchange',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('trades', INTERVAL '3 days');
SELECT add_retention_policy('trades', INTERVAL '90 days');
```

---

## Group 4：链上数据

### 4.1 onchain_metrics — 链上指标时序

> 通用链上指标存储，支持 MVRV、SOPR、NUPL、Puell Multiple 等所有链上指标

```sql
CREATE TABLE onchain_metrics (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name         VARCHAR(100) NOT NULL,
    value               NUMERIC(30,10) NOT NULL,
    unit                VARCHAR(30) DEFAULT 'ratio',
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('onchain_metrics', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE onchain_metrics IS '链上指标时序数据（通用结构）';
COMMENT ON COLUMN onchain_metrics.metric_name IS '指标名称：MVRV / SOPR / aSOPR / NUPL / PUELL_MULTIPLE / RHODL / RESERVE_RISK 等';
COMMENT ON COLUMN onchain_metrics.value IS '指标值';
COMMENT ON COLUMN onchain_metrics.unit IS '单位：ratio / usd / btc / count / percent';
COMMENT ON COLUMN onchain_metrics.metadata IS '附加元数据，如计算参数、数据版本等';

CREATE UNIQUE INDEX idx_onchain_unique ON onchain_metrics (metric_name, observation_time, source_id);
CREATE INDEX idx_onchain_metric ON onchain_metrics (metric_name, observation_time DESC);
CREATE INDEX idx_onchain_source ON onchain_metrics (source_id, observation_time DESC);
CREATE INDEX idx_onchain_metadata ON onchain_metrics USING GIN (metadata jsonb_path_ops);

ALTER TABLE onchain_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('onchain_metrics', INTERVAL '30 days');
```

### 4.2 exchange_flows — 交易所资金流

> 交易所 BTC 资金流入流出数据

```sql
CREATE TABLE exchange_flows (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange_name       VARCHAR(100) NOT NULL,
    flow_type           VARCHAR(20) NOT NULL DEFAULT 'NET',
    inflow_btc          NUMERIC(20,8),
    outflow_btc         NUMERIC(20,8),
    net_flow_btc        NUMERIC(20,8),
    inflow_usd          NUMERIC(20,2),
    outflow_usd         NUMERIC(20,2),
    net_flow_usd        NUMERIC(20,2),
    exchange_balance    NUMERIC(20,8),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('exchange_flows', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE exchange_flows IS '交易所 BTC 资金流数据';
COMMENT ON COLUMN exchange_flows.flow_type IS '流量类型：INFLOW / OUTFLOW / NET / ALL';
COMMENT ON COLUMN exchange_flows.exchange_balance IS '交易所 BTC 余额';

CREATE INDEX idx_exchange_flows_name ON exchange_flows (exchange_name, observation_time DESC);
CREATE INDEX idx_exchange_flows_type ON exchange_flows (flow_type, observation_time DESC);

ALTER TABLE exchange_flows SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange_name',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('exchange_flows', INTERVAL '30 days');
```

### 4.3 address_metrics — 地址活跃指标

> 链上地址活跃度相关指标

```sql
CREATE TABLE address_metrics (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name         VARCHAR(100) NOT NULL,
    value               NUMERIC(30,10) NOT NULL,
    cohort              VARCHAR(50),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('address_metrics', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE address_metrics IS '地址活跃指标';
COMMENT ON COLUMN address_metrics.metric_name IS 'ACTIVE_ADDRESSES / NEW_ADDRESSES / TRANSACTION_COUNT / COIN_DAYS_DESTROYED';
COMMENT ON COLUMN address_metrics.cohort IS '地址群体：ALL / NEW / LTH / STH / WHALE';

CREATE UNIQUE INDEX idx_address_metrics_unique ON address_metrics (metric_name, cohort, observation_time);
CREATE INDEX idx_address_metrics_name ON address_metrics (metric_name, observation_time DESC);

ALTER TABLE address_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('address_metrics', INTERVAL '30 days');
```

### 4.4 supply_metrics — 供应分布指标

> BTC 供应在不同群体中的分布情况

```sql
CREATE TABLE supply_metrics (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name         VARCHAR(100) NOT NULL,
    value               NUMERIC(30,10) NOT NULL,
    supply_cohort       VARCHAR(50) NOT NULL DEFAULT 'ALL',
    total_supply        NUMERIC(20,8),
    pct_of_supply       NUMERIC(8,4),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('supply_metrics', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE supply_metrics IS 'BTC 供应分布指标（HODL Waves 等）';
COMMENT ON COLUMN supply_metrics.metric_name IS 'LTH_SUPPLY / STH_SUPPLY / HODL_WAVES / SUPPLY_LAST_ACTIVE / DORMANCY';
COMMENT ON COLUMN supply_metrics.supply_cohort IS '供应群体：LTH / STH / EXCHANGE / MINER / LOST / 1D_1W / 1W_1M / 1M_3M 等';
COMMENT ON COLUMN supply_metrics.pct_of_supply IS '占总供应百分比';

CREATE UNIQUE INDEX idx_supply_metrics_unique ON supply_metrics (metric_name, supply_cohort, observation_time);
CREATE INDEX idx_supply_metrics_name ON supply_metrics (metric_name, observation_time DESC);

ALTER TABLE supply_metrics SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric_name',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('supply_metrics', INTERVAL '30 days');
```

---

## Group 5：ETF 数据

### 5.1 etf_flows — ETF 资金流

> BTC ETF 每日资金流入流出数据

```sql
CREATE TABLE etf_flows (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker              VARCHAR(20) NOT NULL,
    fund_name           VARCHAR(100),
    daily_inflow_usd    NUMERIC(20,2),
    daily_outflow_usd   NUMERIC(20,2),
    net_flow_usd        NUMERIC(20,2),
    cumulative_flow_usd NUMERIC(20,2),
    total_holdings_btc  NUMERIC(20,8),
    total_aum_usd       NUMERIC(20,2),
    daily_volume_usd    NUMERIC(20,2),
    price               NUMERIC(12,4),
    nav                 NUMERIC(12,4),
    premium_discount    NUMERIC(8,4),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('etf_flows', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE etf_flows IS 'BTC ETF 每日资金流数据';
COMMENT ON COLUMN etf_flows.ticker IS 'ETF 代码：IBIT / FBTC / GBTC / ARKB / BITB 等';
COMMENT ON COLUMN etf_flows.net_flow_usd IS '当日净流入（USD），正数为流入';
COMMENT ON COLUMN etf_flows.cumulative_flow_usd IS '累计净流入';
COMMENT ON COLUMN etf_flows.premium_discount IS '溢价/折价率';

CREATE UNIQUE INDEX idx_etf_flows_unique ON etf_flows (ticker, observation_time);
CREATE INDEX idx_etf_flows_ticker ON etf_flows (ticker, observation_time DESC);
CREATE INDEX idx_etf_flows_time ON etf_flows (observation_time DESC);

ALTER TABLE etf_flows SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('etf_flows', INTERVAL '1 year');
```

### 5.2 etf_holdings — ETF 持仓

> BTC ETF 持仓快照数据

```sql
CREATE TABLE etf_holdings (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker              VARCHAR(20) NOT NULL,
    fund_name           VARCHAR(100),
    holdings_btc        NUMERIC(20,8),
    holdings_usd        NUMERIC(20,2),
    shares_outstanding  NUMERIC(20,2),
    nav_per_share       NUMERIC(12,4),
    market_price        NUMERIC(12,4),
    premium_discount    NUMERIC(8,4),
    daily_change_btc    NUMERIC(20,8),
    custodian           VARCHAR(100),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('etf_holdings', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE etf_holdings IS 'BTC ETF 持仓快照';
COMMENT ON COLUMN etf_holdings.holdings_btc IS '持有 BTC 数量';
COMMENT ON COLUMN etf_holdings.daily_change_btc IS '当日持仓变化';
COMMENT ON COLUMN etf_holdings.custodian IS '托管方';

CREATE UNIQUE INDEX idx_etf_holdings_unique ON etf_holdings (ticker, observation_time);
CREATE INDEX idx_etf_holdings_ticker ON etf_holdings (ticker, observation_time DESC);

ALTER TABLE etf_holdings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'ticker',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('etf_holdings', INTERVAL '1 year');
```

---

## Group 6：衍生品数据

### 6.1 derivatives — 资金费率/OI/清算

> 衍生品综合数据表，存储资金费率、未平仓合约、清算、多空比等

```sql
CREATE TABLE derivatives (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(30) NOT NULL DEFAULT 'BTCUSDT',
    exchange            VARCHAR(50) NOT NULL,
    data_type           VARCHAR(30) NOT NULL,
    -- 资金费率
    funding_rate        NUMERIC(12,10),
    funding_rate_next   NUMERIC(12,10),
    funding_time        TIMESTAMPTZ,
    -- 未平仓合约
    open_interest       NUMERIC(20,8),
    open_interest_usd   NUMERIC(24,2),
    open_interest_change NUMERIC(12,6),
    -- 多空比
    long_short_ratio    NUMERIC(10,6),
    long_account_ratio  NUMERIC(10,6),
    -- 清算数据
    liquidation_long_usd    NUMERIC(20,2),
    liquidation_short_usd   NUMERIC(20,2),
    liquidation_total_usd   NUMERIC(20,2),
    liquidation_count       INTEGER,
    -- 基差
    basis               NUMERIC(12,6),
    basis_pct           NUMERIC(10,6),
    premium_index       NUMERIC(12,8),
    -- CVD
    cvd                 NUMERIC(24,4),
    taker_buy_volume    NUMERIC(24,4),
    taker_sell_volume   NUMERIC(24,4),
    -- 通用
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('derivatives', 'observation_time',
    chunk_time_interval => INTERVAL '7 days',
    create_default_indexes => false
);

COMMENT ON TABLE derivatives IS '衍生品综合数据（资金费率/OI/清算/多空比/基差）';
COMMENT ON COLUMN derivatives.data_type IS '数据类型：FUNDING / OPEN_INTEREST / LIQUIDATION / LONG_SHORT / BASIS / CVD';
COMMENT ON COLUMN derivatives.funding_rate IS '当期资金费率';
COMMENT ON COLUMN derivatives.open_interest IS '未平仓合约量（BTC）';
COMMENT ON COLUMN derivatives.long_short_ratio IS '多空持仓比';
COMMENT ON COLUMN derivatives.liquidation_total_usd IS '总清算金额（USD）';
COMMENT ON COLUMN derivatives.basis IS '基差（期货价格 - 现货价格）';
COMMENT ON COLUMN derivatives.cvd IS '累计成交量差（Cumulative Volume Delta）';

CREATE INDEX idx_derivatives_type ON derivatives (data_type, symbol, observation_time DESC);
CREATE INDEX idx_derivatives_exchange ON derivatives (exchange, symbol, observation_time DESC);
CREATE INDEX idx_derivatives_symbol ON derivatives (symbol, observation_time DESC);

ALTER TABLE derivatives SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange,data_type',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('derivatives', INTERVAL '7 days');
```

### 6.2 options_data — 期权数据

> 期权链数据，包含隐含波动率、Greeks、持仓量等

```sql
CREATE TABLE options_data (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange            VARCHAR(50) NOT NULL,
    underlying          VARCHAR(20) NOT NULL DEFAULT 'BTC',
    expiry_date         DATE NOT NULL,
    strike_price        NUMERIC(12,2) NOT NULL,
    option_type         VARCHAR(4) NOT NULL CHECK (option_type IN ('CALL', 'PUT')),
    -- 量价
    open_interest       NUMERIC(20,4),
    volume              NUMERIC(20,4),
    last_price          NUMERIC(12,4),
    bid_price           NUMERIC(12,4),
    ask_price           NUMERIC(12,4),
    -- Greeks
    implied_volatility  NUMERIC(10,6),
    delta               NUMERIC(10,6),
    gamma               NUMERIC(12,8),
    theta               NUMERIC(12,8),
    vega                NUMERIC(12,6),
    rho                 NUMERIC(12,6),
    -- 汇总指标
    put_call_ratio      NUMERIC(10,6),
    iv_skew             NUMERIC(10,6),
    total_oi_usd        NUMERIC(20,2),
    max_pain            NUMERIC(12,2),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('options_data', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE options_data IS '期权链数据（Deribit 等交易所）';
COMMENT ON COLUMN options_data.option_type IS '期权类型：CALL / PUT';
COMMENT ON COLUMN options_data.implied_volatility IS '隐含波动率';
COMMENT ON COLUMN options_data.put_call_ratio IS 'Put/Call 比率';
COMMENT ON COLUMN options_data.max_pain IS '最大痛点价格';

CREATE UNIQUE INDEX idx_options_unique ON options_data (exchange, expiry_date, strike_price, option_type, observation_time);
CREATE INDEX idx_options_expiry ON options_data (expiry_date, observation_time DESC);
CREATE INDEX idx_options_exchange ON options_data (exchange, observation_time DESC);

ALTER TABLE options_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'exchange,option_type',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('options_data', INTERVAL '30 days');
SELECT add_retention_policy('options_data', INTERVAL '3 years');
```

---

## Group 7：宏观数据

### 7.1 macro_series — 宏观经济指标时序

> 宏观经济数据，严格区分观测日期、发布日期、修订日期以防止未来数据泄漏

```sql
CREATE TABLE macro_series (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    series_id           VARCHAR(100) NOT NULL,
    series_name         VARCHAR(200) NOT NULL,
    value               NUMERIC(30,10) NOT NULL,
    unit                VARCHAR(30) NOT NULL DEFAULT 'index',
    frequency           VARCHAR(20) NOT NULL DEFAULT 'MONTHLY',
    -- 三日期系统（防止未来数据泄漏）
    observation_date    DATE NOT NULL,
    release_date        DATE NOT NULL,
    revision_date       DATE,
    -- 修订追踪
    previous_value      NUMERIC(30,10),
    revised_value       NUMERIC(30,10),
    is_revised          BOOLEAN NOT NULL DEFAULT false,
    revision_number     INTEGER NOT NULL DEFAULT 0,
    -- 元数据
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('macro_series', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE macro_series IS '宏观经济指标时序数据';
COMMENT ON COLUMN macro_series.series_id IS '系列 ID：DXY / FED_RATE / CPI / PCE / NFP / M2 / US10Y 等';
COMMENT ON COLUMN macro_series.frequency IS '频率：DAILY / WEEKLY / MONTHLY / QUARTERLY';
COMMENT ON COLUMN macro_series.observation_date IS '数据所属期间日期（回测中使用此日期）';
COMMENT ON COLUMN macro_series.release_date IS '数据实际发布日期（回测中必须使用此日期避免未来泄漏）';
COMMENT ON COLUMN macro_series.revision_date IS '数据修订日期';
COMMENT ON COLUMN macro_series.is_revised IS '是否为修订数据';

CREATE UNIQUE INDEX idx_macro_unique ON macro_series (series_id, observation_date, revision_number);
CREATE INDEX idx_macro_series ON macro_series (series_id, observation_time DESC);
CREATE INDEX idx_macro_release ON macro_series (release_date, series_id);
CREATE INDEX idx_macro_revised ON macro_series (series_id, observation_time DESC) WHERE is_revised = true;

ALTER TABLE macro_series SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'series_id',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('macro_series', INTERVAL '1 year');
-- 宏观数据永久保留
```

### 7.2 macro_events — 宏观事件日历

> 宏观经济事件日历，如 FOMC 会议、CPI 发布、非农数据等

```sql
CREATE TABLE macro_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type          VARCHAR(50) NOT NULL,
    event_name          VARCHAR(200) NOT NULL,
    event_time          TIMESTAMPTZ NOT NULL,
    importance          VARCHAR(10) NOT NULL DEFAULT 'MEDIUM',
    actual_value        VARCHAR(50),
    forecast_value      VARCHAR(50),
    previous_value      VARCHAR(50),
    description         TEXT,
    market_impact       JSONB DEFAULT '{}',
    btc_price_at_event  NUMERIC(20,8),
    btc_price_1h_after  NUMERIC(20,8),
    btc_price_24h_after NUMERIC(20,8),
    is_recurring        BOOLEAN NOT NULL DEFAULT true,
    recurring_pattern   VARCHAR(100),
    source_url          TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE macro_events IS '宏观经济事件日历';
COMMENT ON COLUMN macro_events.event_type IS '事件类型：FOMC / CPI / NFP / GDP / PCE / RATE_DECISION';
COMMENT ON COLUMN macro_events.importance IS '重要性：LOW / MEDIUM / HIGH / CRITICAL';
COMMENT ON COLUMN macro_events.market_impact IS '市场影响 JSON：{"btc_change_pct": ..., "volume_spike": ...}';
COMMENT ON COLUMN macro_events.btc_price_at_event IS '事件发生时 BTC 价格（事后填充）';

CREATE INDEX idx_macro_events_time ON macro_events (event_time DESC);
CREATE INDEX idx_macro_events_type ON macro_events (event_type, event_time DESC);
CREATE INDEX idx_macro_events_importance ON macro_events (importance, event_time DESC);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON macro_events
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

---

## Group 8：情绪数据

### 8.1 sentiment — 情绪指标时序

> 市场情绪综合数据，支持 Fear & Greed、Google Trends、社交情绪、新闻情绪

```sql
CREATE TABLE sentiment (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type         VARCHAR(50) NOT NULL,
    metric_name         VARCHAR(100) NOT NULL,
    value               NUMERIC(12,6) NOT NULL,
    normalized_value    NUMERIC(8,4),
    sentiment_label     VARCHAR(30),
    sample_size         INTEGER,
    confidence          NUMERIC(5,4),
    previous_value      NUMERIC(12,6),
    change_pct          NUMERIC(10,6),
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('sentiment', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE sentiment IS '市场情绪指标时序数据';
COMMENT ON COLUMN sentiment.source_type IS '来源类型：FEAR_GREED / GOOGLE_TRENDS / SOCIAL_TWITTER / SOCIAL_REDDIT / NEWS';
COMMENT ON COLUMN sentiment.metric_name IS '指标名称：fear_greed_index / btc_search_volume / social_volume 等';
COMMENT ON COLUMN sentiment.value IS '原始值（各源量纲不同）';
COMMENT ON COLUMN sentiment.normalized_value IS '标准化值（0-100 统一量纲）';
COMMENT ON COLUMN sentiment.sentiment_label IS '情绪标签：EXTREME_FEAR / FEAR / NEUTRAL / GREED / EXTREME_GREED';
COMMENT ON COLUMN sentiment.sample_size IS '样本量（用于评估可信度）';

CREATE UNIQUE INDEX idx_sentiment_unique ON sentiment (source_type, metric_name, observation_time);
CREATE INDEX idx_sentiment_source ON sentiment (source_type, observation_time DESC);
CREATE INDEX idx_sentiment_metric ON sentiment (metric_name, observation_time DESC);
CREATE INDEX idx_sentiment_label ON sentiment (sentiment_label, observation_time DESC);

ALTER TABLE sentiment SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'source_type,metric_name',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('sentiment', INTERVAL '30 days');
```

---

## Group 9：指标与引擎输出

### 9.1 indicator_definitions — 指标定义字典

> 所有技术指标、链上指标的定义字典，包含公式、参数、展示配置

```sql
CREATE TABLE indicator_definitions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code                VARCHAR(50) NOT NULL UNIQUE,
    name                VARCHAR(100) NOT NULL,
    name_cn             VARCHAR(100) NOT NULL,
    category            VARCHAR(50) NOT NULL,
    sub_category        VARCHAR(50),
    description         TEXT,
    description_cn      TEXT,
    formula             TEXT,
    formula_latex       TEXT,
    unit                VARCHAR(30) DEFAULT 'value',
    value_range         VARCHAR(50),
    frequency           VARCHAR(20) NOT NULL DEFAULT '1d',
    params_schema       JSONB NOT NULL DEFAULT '{}',
    default_params      JSONB NOT NULL DEFAULT '{}',
    display_config      JSONB NOT NULL DEFAULT '{}',
    interpretation      JSONB DEFAULT '{}',
    data_dependencies   TEXT[] DEFAULT '{}',
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_primary          BOOLEAN NOT NULL DEFAULT false,
    sort_order          INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE indicator_definitions IS '指标定义字典（技术指标、链上指标、衍生指标）';
COMMENT ON COLUMN indicator_definitions.code IS '指标代码：RSI / MACD / MVRV / SOPR 等';
COMMENT ON COLUMN indicator_definitions.category IS '分类：TECHNICAL / ONCHAIN / DERIVATIVES / MACRO / SENTIMENT / COMPOSITE';
COMMENT ON COLUMN indicator_definitions.formula IS '计算公式文本描述';
COMMENT ON COLUMN indicator_definitions.params_schema IS '参数 schema JSON Schema';
COMMENT ON COLUMN indicator_definitions.display_config IS '前端展示配置：图表类型、颜色、区间等';
COMMENT ON COLUMN indicator_definitions.interpretation IS '解读说明：{"high": "...", "low": "...", "neutral": "..."}';
COMMENT ON COLUMN indicator_definitions.is_primary IS '是否为首页核心指标';

CREATE INDEX idx_indicator_def_category ON indicator_definitions (category, is_active);
CREATE INDEX idx_indicator_def_primary ON indicator_definitions (sort_order) WHERE is_primary = true;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON indicator_definitions
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 9.2 indicator_values — 指标计算结果时序

> 所有指标的计算结果存储

```sql
CREATE TABLE indicator_values (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    indicator_id        UUID NOT NULL REFERENCES indicator_definitions(id),
    source_id           UUID NOT NULL REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    fetch_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol              VARCHAR(20) NOT NULL DEFAULT 'BTC',
    value               NUMERIC(30,10) NOT NULL,
    normalized_value    NUMERIC(12,6),
    percentile          NUMERIC(8,4),
    signal              VARCHAR(20),
    params_used         JSONB NOT NULL DEFAULT '{}',
    metadata            JSONB DEFAULT '{}',
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('indicator_values', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE indicator_values IS '指标计算结果时序数据';
COMMENT ON COLUMN indicator_values.value IS '指标原始值';
COMMENT ON COLUMN indicator_values.normalized_value IS '标准化值（0-1 或 Z-score）';
COMMENT ON COLUMN indicator_values.percentile IS '历史分位数（0-100）';
COMMENT ON COLUMN indicator_values.signal IS '信号：BULLISH / BEARISH / NEUTRAL / OVERBOUGHT / OVERSOLD';
COMMENT ON COLUMN indicator_values.params_used IS '本次计算使用的参数';

CREATE UNIQUE INDEX idx_indicator_values_unique ON indicator_values (indicator_id, symbol, observation_time);
CREATE INDEX idx_indicator_values_ind ON indicator_values (indicator_id, observation_time DESC);
CREATE INDEX idx_indicator_values_symbol ON indicator_values (symbol, observation_time DESC);

ALTER TABLE indicator_values SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'indicator_id,symbol',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('indicator_values', INTERVAL '30 days');
```

### 9.3 cycle_states — 市场周期状态

> 市场周期引擎输出，记录每次周期阶段判断及其证据

```sql
CREATE TABLE cycle_states (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    phase               cycle_phase NOT NULL,
    previous_phase      cycle_phase,
    confidence          NUMERIC(5,4) NOT NULL DEFAULT 0,
    phase_duration_days INTEGER,
    evidence_for        JSONB NOT NULL DEFAULT '[]',
    evidence_against    JSONB NOT NULL DEFAULT '[]',
    dimension_scores    JSONB NOT NULL DEFAULT '{}',
    historical_similar  JSONB DEFAULT '[]',
    model_version_id    UUID,
    description         TEXT,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('cycle_states', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE cycle_states IS '市场周期状态（Cycle Engine 输出）';
COMMENT ON COLUMN cycle_states.phase IS '当前周期阶段';
COMMENT ON COLUMN cycle_states.confidence IS '判断置信度 0-1';
COMMENT ON COLUMN cycle_states.evidence_for IS '支持证据 [{"factor":"...","value":"...","weight":...}]';
COMMENT ON COLUMN cycle_states.evidence_against IS '反对证据（同上结构）';
COMMENT ON COLUMN cycle_states.dimension_scores IS '各维度评分 {"price":...,"onchain":...,"volume":...}';
COMMENT ON COLUMN cycle_states.historical_similar IS '历史相似阶段 [{"period":"2020-03","phase":"BOTTOM_BUILDING",...}]';

CREATE INDEX idx_cycle_phase ON cycle_states (phase, observation_time DESC);
CREATE INDEX idx_cycle_time ON cycle_states (observation_time DESC);

ALTER TABLE cycle_states SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('cycle_states', INTERVAL '1 year');
```

### 9.4 valuation_states — 估值状态

> 估值引擎输出，记录 BTC 当前估值水平及组成因素

```sql
CREATE TABLE valuation_states (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    valuation_level     valuation_level NOT NULL,
    previous_level      valuation_level,
    confidence          NUMERIC(5,4) NOT NULL DEFAULT 0,
    -- 核心估值指标
    mvrv_value          NUMERIC(12,6),
    mvrv_percentile     NUMERIC(8,4),
    realized_cap        NUMERIC(24,2),
    realized_price      NUMERIC(20,8),
    cost_basis          NUMERIC(20,8),
    nupl_value          NUMERIC(12,6),
    nupl_percentile     NUMERIC(8,4),
    -- 综合评分
    overall_score       NUMERIC(8,4),
    components          JSONB NOT NULL DEFAULT '{}',
    evidence            JSONB NOT NULL DEFAULT '[]',
    historical_context  JSONB DEFAULT '{}',
    model_version_id    UUID,
    description         TEXT,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('valuation_states', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE valuation_states IS '估值状态（Valuation Engine 输出）';
COMMENT ON COLUMN valuation_states.valuation_level IS '估值等级：DEEP_UNDERVALUED / UNDERVALUED / FAIR / OVERVALUED / EXTREME_OVERVALUED';
COMMENT ON COLUMN valuation_states.mvrv_value IS 'MVRV 比率值';
COMMENT ON COLUMN valuation_states.realized_cap IS '已实现市值（USD）';
COMMENT ON COLUMN valuation_states.cost_basis IS '市场平均成本基础';
COMMENT ON COLUMN valuation_states.components IS '各估值因子详情 JSONB';

CREATE INDEX idx_valuation_level ON valuation_states (valuation_level, observation_time DESC);
CREATE INDEX idx_valuation_time ON valuation_states (observation_time DESC);

ALTER TABLE valuation_states SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('valuation_states', INTERVAL '1 year');
```

### 9.5 risk_scores — 风险评分

> 风险引擎输出，多维度风险评估

```sql
CREATE TABLE risk_scores (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 各维度风险评分 0-100
    overall_risk        NUMERIC(5,2) NOT NULL DEFAULT 0,
    trend_risk          NUMERIC(5,2) NOT NULL DEFAULT 0,
    valuation_risk      NUMERIC(5,2) NOT NULL DEFAULT 0,
    leverage_risk       NUMERIC(5,2) NOT NULL DEFAULT 0,
    liquidity_risk      NUMERIC(5,2) NOT NULL DEFAULT 0,
    macro_risk          NUMERIC(5,2) NOT NULL DEFAULT 0,
    onchain_risk        NUMERIC(5,2) NOT NULL DEFAULT 0,
    volatility_risk     NUMERIC(5,2) NOT NULL DEFAULT 0,
    crowding_risk       NUMERIC(5,2) NOT NULL DEFAULT 0,
    drawdown_risk       NUMERIC(5,2) NOT NULL DEFAULT 0,
    -- 综合
    risk_level          risk_level NOT NULL DEFAULT 'MODERATE',
    previous_level      risk_level,
    confidence          NUMERIC(5,4) NOT NULL DEFAULT 0,
    risk_factors        JSONB NOT NULL DEFAULT '[]',
    evidence            JSONB NOT NULL DEFAULT '[]',
    warnings            JSONB DEFAULT '[]',
    model_version_id    UUID,
    description         TEXT,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('risk_scores', 'observation_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE risk_scores IS '风险评分（Risk Engine 输出）';
COMMENT ON COLUMN risk_scores.overall_risk IS '综合风险评分 0-100，越高越危险';
COMMENT ON COLUMN risk_scores.leverage_risk IS '杠杆风险（基于 Funding Rate + OI + 清算数据）';
COMMENT ON COLUMN risk_scores.risk_factors IS '风险因子详情 [{"factor":"...","score":...,"evidence":"..."}]';
COMMENT ON COLUMN risk_scores.warnings IS '风险警告 [{"type":"...","message":"...","severity":"..."}]';

CREATE INDEX idx_risk_level ON risk_scores (risk_level, observation_time DESC);
CREATE INDEX idx_risk_time ON risk_scores (observation_time DESC);
CREATE INDEX idx_risk_high ON risk_scores (observation_time DESC) WHERE overall_risk >= 70;

ALTER TABLE risk_scores SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('risk_scores', INTERVAL '90 days');
```

### 9.6 market_regimes — 综合市场状态

> Multi-Factor Engine 输出，记录所有维度的市场状态快照

```sql
CREATE TABLE market_regimes (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id           UUID REFERENCES providers(id),
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 各维度状态
    trend_state         VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    valuation_state     valuation_level NOT NULL DEFAULT 'FAIR',
    capital_flow_state  VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    onchain_state       VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    derivative_state    VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    macro_state         VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    sentiment_state     VARCHAR(30) NOT NULL DEFAULT 'NEUTRAL',
    risk_state          risk_level NOT NULL DEFAULT 'MODERATE',
    cycle_state         cycle_phase NOT NULL DEFAULT 'UPTREND',
    -- 综合判断
    overall_regime      VARCHAR(50) NOT NULL DEFAULT 'NEUTRAL',
    confidence          NUMERIC(5,4) NOT NULL DEFAULT 0,
    regime_score        NUMERIC(8,4),
    -- 变化追踪
    is_changed          BOOLEAN NOT NULL DEFAULT false,
    previous_regime     VARCHAR(50),
    change_reason       JSONB DEFAULT '{}',
    -- 详情
    dimension_details   JSONB NOT NULL DEFAULT '{}',
    evidence_summary    JSONB DEFAULT '{}',
    model_version_id    UUID,
    description         TEXT,
    quality_status      quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('market_regimes', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE market_regimes IS '综合市场状态（Multi-Factor Engine 输出）';
COMMENT ON COLUMN market_regimes.overall_regime IS '综合市场状态：BULL_STRONG / BULL / NEUTRAL_BULL / NEUTRAL / NEUTRAL_BEAR / BEAR / BEAR_STRONG / CRISIS';
COMMENT ON COLUMN market_regimes.is_changed IS '是否发生了状态变化（仅变化时记录新行）';
COMMENT ON COLUMN market_regimes.change_reason IS '状态变化原因 {"from":"...","to":"...","drivers":[...]}';
COMMENT ON COLUMN market_regimes.dimension_details IS '各维度详情 {"trend":{"state":"...","score":...,"indicators":[...]}}';
COMMENT ON COLUMN market_regimes.evidence_summary IS '证据摘要，用于前端解释「为什么」';

CREATE INDEX idx_regimes_overall ON market_regimes (overall_regime, observation_time DESC);
CREATE INDEX idx_regimes_changed ON market_regimes (observation_time DESC) WHERE is_changed = true;
CREATE INDEX idx_regimes_time ON market_regimes (observation_time DESC);

ALTER TABLE market_regimes SET (
    timescaledb.compress,
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('market_regimes', INTERVAL '1 year');
```

### 9.7 signals — 信号事件

> 各引擎产生的信号事件记录（不用于自动交易，仅分析和提醒）

```sql
CREATE TABLE signals (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    signal_time         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    signal_type         VARCHAR(50) NOT NULL,
    signal_direction    signal_direction NOT NULL,
    strength            NUMERIC(5,4) NOT NULL DEFAULT 0,
    category            VARCHAR(50) NOT NULL,
    engine_source       VARCHAR(50) NOT NULL,
    -- 触发条件
    trigger_conditions  JSONB NOT NULL DEFAULT '[]',
    evidence            JSONB NOT NULL DEFAULT '[]',
    -- 上下文
    market_context      JSONB DEFAULT '{}',
    btc_price           NUMERIC(20,8),
    regime_at_signal    VARCHAR(50),
    -- 描述
    title               VARCHAR(200) NOT NULL,
    description         TEXT,
    description_cn      TEXT,
    -- 元数据
    is_active           BOOLEAN NOT NULL DEFAULT true,
    expires_at          TIMESTAMPTZ,
    model_version_id    UUID,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, signal_time)
);

SELECT create_hypertable('signals', 'signal_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE signals IS '信号事件记录（分析提醒用途，非自动交易）';
COMMENT ON COLUMN signals.signal_type IS '信号类型：TREND_CHANGE / VALUATION_ALERT / RISK_WARNING / FLOW_ANOMALY / CYCLE_SHIFT';
COMMENT ON COLUMN signals.category IS '信号分类：TREND / VALUATION / RISK / ONCHAIN / DERIVATIVE / MACRO / SENTIMENT';
COMMENT ON COLUMN signals.engine_source IS '产生引擎：CYCLE_ENGINE / VALUATION_ENGINE / RISK_ENGINE / REGIME_ENGINE';
COMMENT ON COLUMN signals.strength IS '信号强度 0-1';

CREATE INDEX idx_signals_type ON signals (signal_type, signal_time DESC);
CREATE INDEX idx_signals_direction ON signals (signal_direction, signal_time DESC);
CREATE INDEX idx_signals_active ON signals (signal_time DESC) WHERE is_active = true;
CREATE INDEX idx_signals_category ON signals (category, signal_time DESC);

ALTER TABLE signals SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'category',
    timescaledb.compress_orderby = 'signal_time DESC'
);
SELECT add_compression_policy('signals', INTERVAL '90 days');
```

---

## Group 10：模型管理

### 10.1 model_versions — 模型版本

> 模型版本注册表，所有模型必须版本化管理，不可覆盖

```sql
CREATE TABLE model_versions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_name          VARCHAR(100) NOT NULL,
    version             VARCHAR(50) NOT NULL,
    model_type          VARCHAR(50) NOT NULL,
    description         TEXT,
    description_cn      TEXT,
    -- 架构信息
    architecture        JSONB NOT NULL DEFAULT '{}',
    hyperparameters     JSONB NOT NULL DEFAULT '{}',
    training_config     JSONB NOT NULL DEFAULT '{}',
    features_used       TEXT[] DEFAULT '{}',
    -- 训练/测试区间
    train_start_date    DATE NOT NULL,
    train_end_date      DATE NOT NULL,
    test_start_date     DATE,
    test_end_date       DATE,
    validation_type     VARCHAR(30) DEFAULT 'WALK_FORWARD',
    -- 状态
    status              model_status NOT NULL DEFAULT 'DRAFT',
    is_production       BOOLEAN NOT NULL DEFAULT false,
    -- 性能指标
    performance_metrics JSONB DEFAULT '{}',
    known_limitations   TEXT,
    effective_conditions TEXT,
    -- 元数据
    parent_version_id   UUID REFERENCES model_versions(id),
    created_by          UUID REFERENCES users(id),
    changelog           TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_name, version)
);

COMMENT ON TABLE model_versions IS '模型版本注册表（不可覆盖，只能新建）';
COMMENT ON COLUMN model_versions.model_type IS '模型类型：CYCLE / VALUATION / RISK / REGIME / PREDICTION / ENSEMBLE';
COMMENT ON COLUMN model_versions.validation_type IS '验证方式：IN_SAMPLE / OUT_OF_SAMPLE / WALK_FORWARD / CROSS_VALIDATION';
COMMENT ON COLUMN model_versions.performance_metrics IS '性能指标 JSON: {"sharpe":..., "max_dd":..., "accuracy":...}';
COMMENT ON COLUMN model_versions.effective_conditions IS '模型有效条件描述';
COMMENT ON COLUMN model_versions.parent_version_id IS '父版本（用于版本谱系追踪）';

CREATE INDEX idx_model_versions_name ON model_versions (model_name, status);
CREATE INDEX idx_model_versions_production ON model_versions (model_name) WHERE is_production = true;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON model_versions
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 10.2 model_weights — 模型参数/权重

> 模型权重存储，支持小权重存 DB、大权重存文件

```sql
CREATE TABLE model_weights (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id    UUID NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    weight_name         VARCHAR(200) NOT NULL,
    weight_type         VARCHAR(30) NOT NULL DEFAULT 'PARAMETER',
    -- 存储（二选一）
    weight_blob         BYTEA,
    storage_path        VARCHAR(500),
    file_size_bytes     BIGINT,
    checksum_sha256     VARCHAR(64),
    -- 元数据
    weight_metadata     JSONB DEFAULT '{}',
    shape               INTEGER[] DEFAULT '{}',
    dtype               VARCHAR(20) DEFAULT 'float64',
    description         TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_version_id, weight_name)
);

COMMENT ON TABLE model_weights IS '模型参数/权重存储';
COMMENT ON COLUMN model_weights.weight_type IS '权重类型：PARAMETER / NEURAL_WEIGHT / COEFFICIENT / THRESHOLD';
COMMENT ON COLUMN model_weights.weight_blob IS '小权重直接存储（<10MB）';
COMMENT ON COLUMN model_weights.storage_path IS '大权重文件存储路径';
COMMENT ON COLUMN model_weights.checksum_sha256 IS 'SHA256 校验和，确保完整性';

CREATE INDEX idx_model_weights_version ON model_weights (model_version_id);
```

### 10.3 model_validations — 模型验证结果

> 模型验证记录，包含 In-Sample、Out-of-Sample、Walk-Forward 结果

```sql
CREATE TABLE model_validations (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_version_id    UUID NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    validation_type     VARCHAR(30) NOT NULL,
    validated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 测试区间
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    period_description  VARCHAR(200),
    -- 结果指标
    metrics             JSONB NOT NULL DEFAULT '{}',
    sharpe_ratio        NUMERIC(8,4),
    sortino_ratio       NUMERIC(8,4),
    max_drawdown        NUMERIC(8,4),
    win_rate            NUMERIC(8,4),
    profit_factor       NUMERIC(8,4),
    accuracy            NUMERIC(8,4),
    precision_score     NUMERIC(8,4),
    recall_score        NUMERIC(8,4),
    f1_score            NUMERIC(8,4),
    -- 状态
    result_status       VARCHAR(20) NOT NULL DEFAULT 'PENDING',
    passed              BOOLEAN,
    notes               TEXT,
    -- 数据快照
    data_snapshot_time  TIMESTAMPTZ,
    data_version        VARCHAR(50),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE model_validations IS '模型验证结果记录';
COMMENT ON COLUMN model_validations.validation_type IS 'IN_SAMPLE / OUT_OF_SAMPLE / WALK_FORWARD / STRESS_TEST / EXTREME_MARKET';
COMMENT ON COLUMN model_validations.result_status IS 'PENDING / PASSED / FAILED / INCONCLUSIVE';
COMMENT ON COLUMN model_validations.data_snapshot_time IS '验证时使用的数据快照时间（防止未来数据泄漏）';

CREATE INDEX idx_model_valid_version ON model_validations (model_version_id, validated_at DESC);
CREATE INDEX idx_model_valid_type ON model_validations (validation_type, result_status);
```

---

## Group 11：回测

### 11.1 strategies — 策略定义

> 策略基础定义表

```sql
CREATE TABLE strategies (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                VARCHAR(100) NOT NULL,
    name_cn             VARCHAR(100),
    category            VARCHAR(50) NOT NULL DEFAULT 'DCA',
    description         TEXT,
    description_cn      TEXT,
    parameters_schema   JSONB NOT NULL DEFAULT '{}',
    rules_description   TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_system           BOOLEAN NOT NULL DEFAULT false,
    tags                TEXT[] DEFAULT '{}',
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE strategies IS '策略定义表';
COMMENT ON COLUMN strategies.category IS '策略分类：DCA / VALUE_AVERAGING / SIGNAL_BASED / RISK_ADJUSTED / CYCLE_BASED / CUSTOM';
COMMENT ON COLUMN strategies.is_system IS '是否为系统内置策略';

CREATE INDEX idx_strategies_category ON strategies (category) WHERE is_active = true;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON strategies
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 11.2 strategy_versions — 策略版本

> 策略版本管理，每次参数修改都创建新版本

```sql
CREATE TABLE strategy_versions (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id         UUID NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    version             VARCHAR(50) NOT NULL,
    parameters          JSONB NOT NULL DEFAULT '{}',
    rules_config        JSONB NOT NULL DEFAULT '{}',
    changelog           TEXT,
    is_current          BOOLEAN NOT NULL DEFAULT false,
    created_by          UUID REFERENCES users(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_id, version)
);

COMMENT ON TABLE strategy_versions IS '策略版本管理';
COMMENT ON COLUMN strategy_versions.parameters IS '策略参数 JSON: {"periodic_amount":5000,"frequency":"monthly",...}';
COMMENT ON COLUMN strategy_versions.rules_config IS '加仓/减仓规则配置 JSON';

CREATE INDEX idx_strategy_versions_sid ON strategy_versions (strategy_id, created_at DESC);
CREATE INDEX idx_strategy_versions_current ON strategy_versions (strategy_id) WHERE is_current = true;
```

### 11.3 backtest_runs — 回测运行记录

> 回测运行主表，关联策略版本、模型版本，记录完整参数和结果摘要

```sql
CREATE TABLE backtest_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    model_version_id    UUID REFERENCES model_versions(id),
    user_id             UUID REFERENCES users(id),
    -- 状态
    status              backtest_status NOT NULL DEFAULT 'PENDING',
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    duration_seconds    INTEGER,
    error_message       TEXT,
    -- 回测参数
    period_start        DATE NOT NULL,
    period_end          DATE NOT NULL,
    run_params          JSONB NOT NULL DEFAULT '{}',
    initial_conditions  JSONB NOT NULL DEFAULT '{}',
    -- 结果摘要
    total_return        NUMERIC(12,6),
    annual_return       NUMERIC(12,6),
    max_drawdown        NUMERIC(8,4),
    max_drawdown_duration_days INTEGER,
    sharpe_ratio        NUMERIC(8,4),
    sortino_ratio       NUMERIC(8,4),
    calmar_ratio        NUMERIC(8,4),
    win_rate            NUMERIC(8,4),
    profit_factor       NUMERIC(8,4),
    total_trades        INTEGER,
    avg_trade_pnl       NUMERIC(16,4),
    -- 资金结果
    initial_capital     NUMERIC(16,2) NOT NULL,
    total_invested      NUMERIC(16,2),
    final_value         NUMERIC(16,2),
    total_fees          NUMERIC(16,2),
    btc_accumulated     NUMERIC(20,8),
    avg_cost_basis      NUMERIC(20,8),
    -- 扩展
    summary_metrics     JSONB DEFAULT '{}',
    yearly_returns      JSONB DEFAULT '{}',
    benchmark_return    NUMERIC(12,6),
    alpha               NUMERIC(12,6),
    -- 数据快照
    data_snapshot_time  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE backtest_runs IS '回测运行记录（主表）';
COMMENT ON COLUMN backtest_runs.run_params IS '运行参数: {"fee_rate":0.001,"slippage":0.0005,"frequency":"monthly"}';
COMMENT ON COLUMN backtest_runs.initial_conditions IS '初始条件: {"capital":100000,"currency":"CNY","start_price":...}';
COMMENT ON COLUMN backtest_runs.summary_metrics IS '扩展指标 JSON';
COMMENT ON COLUMN backtest_runs.yearly_returns IS '年度收益率 {"2020":0.35,"2021":1.2,...}';
COMMENT ON COLUMN backtest_runs.data_snapshot_time IS '数据快照时间（确保回测可复现）';

CREATE INDEX idx_backtest_user ON backtest_runs (user_id, created_at DESC);
CREATE INDEX idx_backtest_strategy ON backtest_runs (strategy_version_id, created_at DESC);
CREATE INDEX idx_backtest_status ON backtest_runs (status) WHERE status IN ('PENDING', 'RUNNING');
CREATE INDEX idx_backtest_period ON backtest_runs (period_start, period_end);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON backtest_runs
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 11.4 backtest_results — 回测结果详情

> 回测每个时间点的组合净值快照

```sql
CREATE TABLE backtest_results (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    backtest_run_id     UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 组合状态
    portfolio_value     NUMERIC(20,4) NOT NULL,
    cash_balance        NUMERIC(20,4) NOT NULL DEFAULT 0,
    btc_holdings        NUMERIC(20,8) NOT NULL DEFAULT 0,
    btc_price           NUMERIC(20,8),
    -- 成本与收益
    avg_cost            NUMERIC(20,8),
    unrealized_pnl      NUMERIC(20,4),
    realized_pnl        NUMERIC(20,4) DEFAULT 0,
    cumulative_invested NUMERIC(20,4),
    -- 风险指标
    drawdown            NUMERIC(8,6),
    drawdown_duration   INTEGER,
    daily_return        NUMERIC(10,6),
    cumulative_return   NUMERIC(12,6),
    -- 快照
    metrics_snapshot    JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('backtest_results', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE backtest_results IS '回测结果详情（每个时间点的组合净值）';
COMMENT ON COLUMN backtest_results.portfolio_value IS '组合总市值（现金 + BTC）';
COMMENT ON COLUMN backtest_results.drawdown IS '当前回撤比例';

CREATE INDEX idx_backtest_results_run ON backtest_results (backtest_run_id, observation_time);

ALTER TABLE backtest_results SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'backtest_run_id',
    timescaledb.compress_orderby = 'observation_time'
);
SELECT add_compression_policy('backtest_results', INTERVAL '30 days');
```

### 11.5 backtest_trades — 回测交易明细

> 回测中每笔模拟交易的详细记录

```sql
CREATE TABLE backtest_trades (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    backtest_run_id     UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    trade_time          TIMESTAMPTZ NOT NULL,
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 交易信息
    trade_number        INTEGER NOT NULL,
    side                trade_side NOT NULL,
    price               NUMERIC(20,8) NOT NULL,
    quantity_btc        NUMERIC(20,8) NOT NULL,
    amount              NUMERIC(20,4) NOT NULL,
    fee                 NUMERIC(16,6) NOT NULL DEFAULT 0,
    slippage            NUMERIC(16,6) NOT NULL DEFAULT 0,
    -- 上下文
    trigger_reason      VARCHAR(100) NOT NULL,
    trigger_details     JSONB DEFAULT '{}',
    market_context      JSONB DEFAULT '{}',
    -- 结果
    pnl                 NUMERIC(16,4),
    cumulative_btc      NUMERIC(20,8),
    cumulative_invested NUMERIC(20,4),
    avg_cost_after      NUMERIC(20,8),
    portfolio_after     NUMERIC(20,4),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('backtest_trades', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE backtest_trades IS '回测交易明细';
COMMENT ON COLUMN backtest_trades.trigger_reason IS '触发原因：SCHEDULED_DCA / DRAWDOWN_BUY / VALUATION_BUY / SIGNAL_BUY / STOP_LOSS';
COMMENT ON COLUMN backtest_trades.trigger_details IS '触发详情 JSON';

CREATE INDEX idx_backtest_trades_run ON backtest_trades (backtest_run_id, observation_time);
CREATE INDEX idx_backtest_trades_reason ON backtest_trades (trigger_reason, observation_time DESC);

ALTER TABLE backtest_trades SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'backtest_run_id',
    timescaledb.compress_orderby = 'observation_time'
);
SELECT add_compression_policy('backtest_trades', INTERVAL '30 days');
```

---

## Group 12：用户数据

### 12.1 users — 用户

> 用户账户表

```sql
CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username            VARCHAR(50) NOT NULL UNIQUE,
    email               VARCHAR(200) NOT NULL UNIQUE,
    password_hash       VARCHAR(200) NOT NULL,
    display_name        VARCHAR(100),
    role                user_role NOT NULL DEFAULT 'USER',
    preferences         JSONB NOT NULL DEFAULT '{"language":"zh-CN","theme":"dark","mode":"simple"}',
    notification_config JSONB DEFAULT '{}',
    is_active           BOOLEAN NOT NULL DEFAULT true,
    is_verified         BOOLEAN NOT NULL DEFAULT false,
    last_login_at       TIMESTAMPTZ,
    login_count         INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE users IS '用户账户表';
COMMENT ON COLUMN users.role IS '角色：ADMIN / ANALYST / USER';
COMMENT ON COLUMN users.preferences IS '用户偏好设置 JSON';
COMMENT ON COLUMN users.notification_config IS '通知配置 JSON';

CREATE INDEX idx_users_email ON users (email);
CREATE INDEX idx_users_role ON users (role) WHERE is_active = true;

CREATE TRIGGER set_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 12.2 user_plans — 资金计划

> 完整的定投/投资计划配置

```sql
CREATE TABLE user_plans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_name           VARCHAR(100) NOT NULL,
    plan_type           VARCHAR(30) NOT NULL DEFAULT 'DCA',
    -- 资金配置
    initial_capital     NUMERIC(16,2) NOT NULL DEFAULT 0,
    currency            VARCHAR(10) NOT NULL DEFAULT 'CNY',
    periodic_amount     NUMERIC(16,2) NOT NULL DEFAULT 0,
    periodic_frequency  VARCHAR(20) NOT NULL DEFAULT 'MONTHLY',
    monthly_income      NUMERIC(16,2),
    -- 时间配置
    start_date          DATE NOT NULL,
    end_date            DATE,
    investment_horizon_years INTEGER,
    -- 资产配置
    target_asset        VARCHAR(20) NOT NULL DEFAULT 'BTC',
    cash_reserve        NUMERIC(16,2) DEFAULT 0,
    cash_reserve_pct    NUMERIC(5,2) DEFAULT 10,
    max_single_investment NUMERIC(16,2),
    -- 定投规则
    dca_rules           JSONB NOT NULL DEFAULT '{"type":"fixed","multiplier_rules":[]}',
    -- 风险参数
    risk_params         JSONB NOT NULL DEFAULT '{}',
    max_drawdown_tolerance NUMERIC(5,2) DEFAULT 30,
    stop_loss_enabled   BOOLEAN NOT NULL DEFAULT false,
    stop_loss_pct       NUMERIC(5,2),
    -- 加仓规则
    drawdown_buy_rules  JSONB DEFAULT '[]',
    valuation_buy_rules JSONB DEFAULT '[]',
    custom_rules        JSONB DEFAULT '[]',
    -- 状态
    status              plan_status NOT NULL DEFAULT 'ACTIVE',
    current_phase       VARCHAR(50),
    next_investment_date DATE,
    -- 统计
    total_invested      NUMERIC(16,2) NOT NULL DEFAULT 0,
    total_btc           NUMERIC(20,8) NOT NULL DEFAULT 0,
    avg_cost            NUMERIC(20,8) NOT NULL DEFAULT 0,
    last_investment_at  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE user_plans IS '用户资金计划配置';
COMMENT ON COLUMN user_plans.plan_type IS '计划类型：DCA / VALUE_AVERAGING / SIGNAL_BASED / CUSTOM';
COMMENT ON COLUMN user_plans.periodic_frequency IS '定投频率：WEEKLY / BIWEEKLY / MONTHLY';
COMMENT ON COLUMN user_plans.dca_rules IS '定投规则: {"type":"fixed|drawdown_adjusted|valuation_adjusted","multiplier_rules":[{"drawdown_pct":-20,"multiplier":1.2}]}';
COMMENT ON COLUMN user_plans.risk_params IS '风险参数: {"max_position_pct":80,"rebalance_threshold":10}';
COMMENT ON COLUMN user_plans.drawdown_buy_rules IS '回撤加仓规则: [{"drawdown_pct":-10,"amount_multiplier":1.0},{"drawdown_pct":-20,"amount_multiplier":1.2}]';
COMMENT ON COLUMN user_plans.max_drawdown_tolerance IS '最大回撤容忍度（%）';

CREATE INDEX idx_user_plans_user ON user_plans (user_id, status);
CREATE INDEX idx_user_plans_next ON user_plans (next_investment_date) WHERE status = 'ACTIVE';
CREATE INDEX idx_user_plans_dca ON user_plans USING GIN (dca_rules jsonb_path_ops);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_plans
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 12.3 user_transactions — 交易记录

> 用户买卖交易记录（手工录入 + 计划自动记录）

```sql
CREATE TABLE user_transactions (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id             UUID REFERENCES user_plans(id) ON DELETE SET NULL,
    transaction_time    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 交易信息
    transaction_type    VARCHAR(20) NOT NULL,
    side                trade_side NOT NULL,
    amount              NUMERIC(16,4) NOT NULL,
    currency            VARCHAR(10) NOT NULL DEFAULT 'CNY',
    price               NUMERIC(20,8) NOT NULL,
    quantity_btc        NUMERIC(20,8) NOT NULL,
    fee                 NUMERIC(12,6) NOT NULL DEFAULT 0,
    fee_currency        VARCHAR(10) DEFAULT 'CNY',
    -- 累计
    cumulative_invested NUMERIC(16,2),
    cumulative_btc      NUMERIC(20,8),
    avg_cost_after      NUMERIC(20,8),
    -- 来源
    source              VARCHAR(30) NOT NULL DEFAULT 'MANUAL',
    exchange_name       VARCHAR(50),
    order_id            VARCHAR(100),
    -- 备注
    notes               TEXT,
    tags                TEXT[] DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, transaction_time)
);

SELECT create_hypertable('user_transactions', 'transaction_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE user_transactions IS '用户交易记录';
COMMENT ON COLUMN user_transactions.transaction_type IS '交易类型：DCA_INVEST / MANUAL_BUY / MANUAL_SELL / PLAN_TRIGGER / IMPORT';
COMMENT ON COLUMN user_transactions.source IS '来源：MANUAL / PLAN_AUTO / EXCHANGE_IMPORT / API';
COMMENT ON COLUMN user_transactions.amount IS '交易金额（以 currency 计价）';

CREATE INDEX idx_user_tx_user ON user_transactions (user_id, transaction_time DESC);
CREATE INDEX idx_user_tx_plan ON user_transactions (plan_id, transaction_time DESC);
CREATE INDEX idx_user_tx_type ON user_transactions (transaction_type, transaction_time DESC);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_transactions
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 12.4 user_holdings — 持仓快照

> 用户持仓定期快照

```sql
CREATE TABLE user_holdings (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    snapshot_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 持仓
    asset               VARCHAR(20) NOT NULL DEFAULT 'BTC',
    quantity            NUMERIC(20,8) NOT NULL DEFAULT 0,
    avg_cost            NUMERIC(20,8) NOT NULL DEFAULT 0,
    total_cost          NUMERIC(16,2) NOT NULL DEFAULT 0,
    -- 市值
    current_price       NUMERIC(20,8),
    market_value        NUMERIC(16,2),
    market_value_cny    NUMERIC(16,2),
    -- 收益
    unrealized_pnl      NUMERIC(16,2),
    unrealized_pnl_pct  NUMERIC(8,4),
    realized_pnl        NUMERIC(16,2) DEFAULT 0,
    total_return_pct    NUMERIC(10,4),
    -- 元数据
    metadata            JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('user_holdings', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE user_holdings IS '用户持仓快照（每日生成）';
COMMENT ON COLUMN user_holdings.avg_cost IS '平均持仓成本';
COMMENT ON COLUMN user_holdings.unrealized_pnl IS '浮动盈亏';

CREATE INDEX idx_user_holdings_user ON user_holdings (user_id, observation_time DESC);

ALTER TABLE user_holdings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'user_id',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('user_holdings', INTERVAL '1 year');
```

### 12.5 portfolio_snapshots — 组合净值快照

> 用户整体投资组合的定期净值快照

```sql
CREATE TABLE portfolio_snapshots (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id             UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    snapshot_time       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observation_time    TIMESTAMPTZ NOT NULL,
    -- 总值
    total_value         NUMERIC(16,2) NOT NULL,
    total_value_cny     NUMERIC(16,2),
    total_invested      NUMERIC(16,2) NOT NULL DEFAULT 0,
    cash_balance        NUMERIC(16,2) NOT NULL DEFAULT 0,
    btc_value           NUMERIC(16,2) NOT NULL DEFAULT 0,
    btc_holdings        NUMERIC(20,8) NOT NULL DEFAULT 0,
    -- 收益
    unrealized_pnl      NUMERIC(16,2),
    realized_pnl        NUMERIC(16,2) DEFAULT 0,
    daily_return        NUMERIC(10,6),
    cumulative_return   NUMERIC(12,6),
    annualized_return   NUMERIC(12,6),
    -- 风险
    max_drawdown        NUMERIC(8,4),
    current_drawdown    NUMERIC(8,4),
    volatility_30d      NUMERIC(8,4),
    sharpe_ratio        NUMERIC(8,4),
    -- 分配
    allocation          JSONB DEFAULT '{}',
    metrics             JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('portfolio_snapshots', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE portfolio_snapshots IS '用户投资组合净值快照（每日生成）';
COMMENT ON COLUMN portfolio_snapshots.allocation IS '资产分配: {"btc_pct":85,"cash_pct":15}';
COMMENT ON COLUMN portfolio_snapshots.metrics IS '扩展指标 JSON';

CREATE INDEX idx_portfolio_user ON portfolio_snapshots (user_id, observation_time DESC);

ALTER TABLE portfolio_snapshots SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'user_id',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('portfolio_snapshots', INTERVAL '1 year');
```

---

## Group 13：系统数据

### 13.1 data_quality — 数据质量记录

> 数据质量检查结果记录

```sql
CREATE TABLE data_quality (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    check_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_id           UUID REFERENCES providers(id),
    data_category       provider_category NOT NULL,
    table_name          VARCHAR(100) NOT NULL,
    check_type          VARCHAR(50) NOT NULL,
    -- 结果
    status              VARCHAR(20) NOT NULL DEFAULT 'OK',
    severity            VARCHAR(10) NOT NULL DEFAULT 'INFO',
    records_checked     INTEGER NOT NULL DEFAULT 0,
    records_passed      INTEGER NOT NULL DEFAULT 0,
    records_failed      INTEGER NOT NULL DEFAULT 0,
    completeness_pct    NUMERIC(6,3),
    -- 详情
    issues              JSONB DEFAULT '[]',
    gaps_found          JSONB DEFAULT '[]',
    conflicts_found     JSONB DEFAULT '[]',
    anomalies_found     JSONB DEFAULT '[]',
    -- 修复
    auto_fixed          BOOLEAN NOT NULL DEFAULT false,
    fix_actions         JSONB DEFAULT '[]',
    requires_manual     BOOLEAN NOT NULL DEFAULT false,
    -- 元数据
    time_range_start    TIMESTAMPTZ,
    time_range_end      TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, check_time)
);

SELECT create_hypertable('data_quality', 'check_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE data_quality IS '数据质量检查记录';
COMMENT ON COLUMN data_quality.check_type IS '检查类型：COMPLETENESS / CONSISTENCY / ACCURACY / TIMELINESS / GAP_DETECTION / CROSS_VALIDATION';
COMMENT ON COLUMN data_quality.status IS 'OK / WARNING / ERROR / CRITICAL';
COMMENT ON COLUMN data_quality.severity IS 'INFO / LOW / MEDIUM / HIGH / CRITICAL';
COMMENT ON COLUMN data_quality.gaps_found IS '发现的数据空洞 [{"start":"...","end":"...","expected":N,"actual":M}]';
COMMENT ON COLUMN data_quality.conflicts_found IS '发现的数据冲突 [{"field":"price","sources":[...],"deviation_pct":...}]';

CREATE INDEX idx_dq_category ON data_quality (data_category, check_time DESC);
CREATE INDEX idx_dq_status ON data_quality (check_time DESC) WHERE status != 'OK';
CREATE INDEX idx_dq_severity ON data_quality (severity, check_time DESC) WHERE severity IN ('HIGH', 'CRITICAL');

ALTER TABLE data_quality SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'data_category',
    timescaledb.compress_orderby = 'check_time DESC'
);
SELECT add_compression_policy('data_quality', INTERVAL '30 days');
SELECT add_retention_policy('data_quality', INTERVAL '2 years');
```

### 13.2 sync_checkpoints — 同步断点

> 数据同步任务的断点续传状态，支持暂停/继续/重试

```sql
CREATE TABLE sync_checkpoints (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_name           VARCHAR(100) NOT NULL,
    data_category       provider_category NOT NULL,
    provider_id         UUID REFERENCES providers(id),
    symbol              VARCHAR(20) DEFAULT 'BTC',
    -- 同步进度
    last_synced_time    TIMESTAMPTZ,
    target_end_time     TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    -- 状态
    status              sync_status NOT NULL DEFAULT 'IDLE',
    progress_pct        NUMERIC(5,2) NOT NULL DEFAULT 0,
    records_synced      INTEGER NOT NULL DEFAULT 0,
    records_failed      INTEGER NOT NULL DEFAULT 0,
    records_skipped     INTEGER NOT NULL DEFAULT 0,
    -- 配置
    sync_params         JSONB NOT NULL DEFAULT '{}',
    batch_size          INTEGER NOT NULL DEFAULT 1000,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 5,
    -- 错误
    last_error          TEXT,
    last_error_time     TIMESTAMPTZ,
    error_history       JSONB DEFAULT '[]',
    -- 元数据
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (task_name, provider_id, symbol)
);

COMMENT ON TABLE sync_checkpoints IS '数据同步断点续传状态';
COMMENT ON COLUMN sync_checkpoints.task_name IS '任务名称：market_history / onchain_history / etf_history 等';
COMMENT ON COLUMN sync_checkpoints.last_synced_time IS '最后成功同步到的时间点（断点位置）';
COMMENT ON COLUMN sync_checkpoints.target_end_time IS '目标结束时间';
COMMENT ON COLUMN sync_checkpoints.status IS 'IDLE / SYNCING / PAUSED / COMPLETED / FAILED / RETRY';
COMMENT ON COLUMN sync_checkpoints.sync_params IS '同步参数: {"interval":"1d","start_date":"2015-01-01"}';

CREATE INDEX idx_sync_status ON sync_checkpoints (status) WHERE status IN ('SYNCING', 'RETRY', 'FAILED');
CREATE INDEX idx_sync_category ON sync_checkpoints (data_category, status);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON sync_checkpoints
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 13.3 system_jobs — 系统任务

> 系统调度任务注册表

```sql
CREATE TABLE system_jobs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_name            VARCHAR(100) NOT NULL UNIQUE,
    job_type            VARCHAR(50) NOT NULL,
    job_group           VARCHAR(50) NOT NULL DEFAULT 'GENERAL',
    description         TEXT,
    -- 调度
    schedule_cron       VARCHAR(100),
    schedule_interval_seconds INTEGER,
    priority            INTEGER NOT NULL DEFAULT 50,
    -- 状态
    status              job_status NOT NULL DEFAULT 'PENDING',
    is_enabled          BOOLEAN NOT NULL DEFAULT true,
    -- 运行记录
    last_run_at         TIMESTAMPTZ,
    next_run_at         TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    last_duration_ms    INTEGER,
    avg_duration_ms     INTEGER,
    -- 重试
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 3,
    last_error          TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    -- 配置
    job_config          JSONB NOT NULL DEFAULT '{}',
    result_summary      JSONB DEFAULT '{}',
    dependencies        TEXT[] DEFAULT '{}',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE system_jobs IS '系统调度任务注册表';
COMMENT ON COLUMN system_jobs.job_type IS '任务类型：SCHEDULED / TRIGGERED / MANUAL / ONE_TIME';
COMMENT ON COLUMN system_jobs.job_group IS '任务组：MARKET / ONCHAIN / ETF / DERIVATIVES / MACRO / SENTIMENT / ENGINE / SYSTEM';
COMMENT ON COLUMN system_jobs.priority IS '优先级 1-100，数字越小越优先';
COMMENT ON COLUMN system_jobs.dependencies IS '依赖的其他任务 job_name 列表';

CREATE INDEX idx_jobs_enabled ON system_jobs (priority, next_run_at) WHERE is_enabled = true;
CREATE INDEX idx_jobs_status ON system_jobs (status) WHERE status = 'RUNNING';
CREATE INDEX idx_jobs_group ON system_jobs (job_group, is_enabled);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON system_jobs
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
```

### 13.4 audit_logs — 审计日志

> 系统操作审计日志，不可修改

```sql
CREATE TABLE audit_logs (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    event_time          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id             UUID REFERENCES users(id) ON DELETE SET NULL,
    -- 操作
    action              VARCHAR(50) NOT NULL,
    action_category     VARCHAR(30) NOT NULL DEFAULT 'GENERAL',
    resource_type       VARCHAR(50) NOT NULL,
    resource_id         UUID,
    resource_name       VARCHAR(200),
    -- 上下文
    ip_address          INET,
    user_agent          TEXT,
    request_id          VARCHAR(100),
    -- 变更
    old_values          JSONB,
    new_values          JSONB,
    context             JSONB DEFAULT '{}',
    -- 结果
    is_success          BOOLEAN NOT NULL DEFAULT true,
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, event_time)
);

SELECT create_hypertable('audit_logs', 'event_time',
    chunk_time_interval => INTERVAL '30 days',
    create_default_indexes => false
);

COMMENT ON TABLE audit_logs IS '系统审计日志（只追加，不可修改删除）';
COMMENT ON COLUMN audit_logs.action IS '操作：CREATE / UPDATE / DELETE / LOGIN / LOGOUT / EXPORT / CONFIG_CHANGE / PROVIDER_SWITCH';
COMMENT ON COLUMN audit_logs.action_category IS '操作分类：AUTH / DATA / CONFIG / PROVIDER / BACKTEST / PORTFOLIO';
COMMENT ON COLUMN audit_logs.resource_type IS '资源类型：USER / PROVIDER / PLAN / STRATEGY / MODEL';

CREATE INDEX idx_audit_user ON audit_logs (user_id, event_time DESC);
CREATE INDEX idx_audit_action ON audit_logs (action, event_time DESC);
CREATE INDEX idx_audit_resource ON audit_logs (resource_type, resource_id, event_time DESC);
CREATE INDEX idx_audit_category ON audit_logs (action_category, event_time DESC);

ALTER TABLE audit_logs SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'action_category',
    timescaledb.compress_orderby = 'event_time DESC'
);
SELECT add_compression_policy('audit_logs', INTERVAL '30 days');
SELECT add_retention_policy('audit_logs', INTERVAL '5 years');
```

### 13.5 market_events — 市场事件时间线

> 重要市场事件记录，用于时间轴展示和历史回放

```sql
CREATE TABLE market_events (
    id                  UUID NOT NULL DEFAULT gen_random_uuid(),
    event_time          TIMESTAMPTZ NOT NULL,
    observation_time    TIMESTAMPTZ NOT NULL,
    event_type          VARCHAR(50) NOT NULL,
    event_category      VARCHAR(30) NOT NULL,
    title               VARCHAR(200) NOT NULL,
    title_cn            VARCHAR(200),
    description         TEXT,
    description_cn      TEXT,
    -- 重要性
    significance        NUMERIC(5,4) NOT NULL DEFAULT 0.5,
    is_major            BOOLEAN NOT NULL DEFAULT false,
    -- 市场数据
    btc_price_at_event  NUMERIC(20,8),
    btc_price_before_24h NUMERIC(20,8),
    btc_price_after_24h NUMERIC(20,8),
    btc_price_after_7d  NUMERIC(20,8),
    btc_price_after_30d NUMERIC(20,8),
    -- 影响
    market_impact       JSONB DEFAULT '{}',
    related_data        JSONB DEFAULT '{}',
    tags                TEXT[] DEFAULT '{}',
    -- 来源
    source_url          TEXT,
    source_id           UUID REFERENCES providers(id),
    is_auto_detected    BOOLEAN NOT NULL DEFAULT false,
    -- 元数据
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);

SELECT create_hypertable('market_events', 'observation_time',
    chunk_time_interval => INTERVAL '1 year',
    create_default_indexes => false
);

COMMENT ON TABLE market_events IS '市场事件时间线';
COMMENT ON COLUMN market_events.event_type IS 'HALVING / ATH / ATL / CRASH / RECOVERY / ETF_LAUNCH / FED_DECISION / REGULATION / HACK / LIQUIDATION_CASCADE';
COMMENT ON COLUMN market_events.event_category IS 'CRYPTO / MACRO / REGULATORY / TECHNOLOGY / MARKET_STRUCTURE';
COMMENT ON COLUMN market_events.significance IS '重要性评分 0-1';
COMMENT ON COLUMN market_events.market_impact IS '市场影响: {"price_change_pct":..., "volume_spike":..., "sentiment_shift":...}';

CREATE INDEX idx_market_events_time ON market_events (observation_time DESC);
CREATE INDEX idx_market_events_type ON market_events (event_type, observation_time DESC);
CREATE INDEX idx_market_events_major ON market_events (observation_time DESC) WHERE is_major = true;
CREATE INDEX idx_market_events_category ON market_events (event_category, observation_time DESC);

CREATE TRIGGER set_updated_at BEFORE UPDATE ON market_events
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

ALTER TABLE market_events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'event_category',
    timescaledb.compress_orderby = 'observation_time DESC'
);
SELECT add_compression_policy('market_events', INTERVAL '1 year');
```

---

## 附录：初始化数据

### 市场事件种子数据

```sql
INSERT INTO market_events (event_time, observation_time, event_type, event_category, title, title_cn, significance, is_major, btc_price_at_event) VALUES
    ('2016-07-09', '2016-07-09', 'HALVING', 'CRYPTO', 'Bitcoin 2nd Halving', '比特币第二次减半', 0.9, true, 650),
    ('2017-12-17', '2017-12-17', 'ATH', 'MARKET_STRUCTURE', 'BTC ATH $19,783', '比特币历史高点 $19,783', 1.0, true, 19783),
    ('2020-03-12', '2020-03-12', 'CRASH', 'MARKET_STRUCTURE', 'Black Thursday', '黑色星期四', 0.95, true, 3800),
    ('2020-05-11', '2020-05-11', 'HALVING', 'CRYPTO', 'Bitcoin 3rd Halving', '比特币第三次减半', 0.9, true, 8700),
    ('2021-04-14', '2021-04-14', 'ATH', 'MARKET_STRUCTURE', 'BTC ATH $64,863', '比特币历史高点 $64,863', 0.9, true, 64863),
    ('2021-11-10', '2021-11-10', 'ATH', 'MARKET_STRUCTURE', 'BTC ATH $68,789', '比特币历史高点 $68,789', 1.0, true, 68789),
    ('2022-05-09', '2022-05-09', 'CRASH', 'MARKET_STRUCTURE', 'Terra/LUNA Collapse', 'Terra/LUNA 崩盘', 0.85, true, 30000),
    ('2022-11-11', '2022-11-11', 'CRASH', 'MARKET_STRUCTURE', 'FTX Collapse', 'FTX 崩盘', 0.9, true, 16500),
    ('2024-01-10', '2024-01-10', 'ETF_LAUNCH', 'REGULATORY', 'US Spot BTC ETF Approved', '美国现货比特币ETF获批', 0.95, true, 46000),
    ('2024-04-20', '2024-04-20', 'HALVING', 'CRYPTO', 'Bitcoin 4th Halving', '比特币第四次减半', 0.9, true, 64000);
```

---

## 附录：数据库初始化脚本顺序

执行顺序如下：

1. 创建扩展：`CREATE EXTENSION IF NOT EXISTS timescaledb;`
2. 创建 ENUM 类型
3. 创建触发器函数
4. Group 13：`assets`（被其他表引用）
5. Group 12：`users`（被其他表引用）
6. Group 1：`providers`（被所有时序表引用）
7. Group 9：`indicator_definitions`（被 indicator_values 引用）
8. Group 10：`model_versions`（被回测引用）
9. Group 11：`strategies` → `strategy_versions`
10. Group 2：所有 `raw_*` 表
11. Group 3：`market_prices` → `candles` → `orderbooks` → `trades`
12. Group 4：`onchain_metrics` → `exchange_flows` → `address_metrics` → `supply_metrics`
13. Group 5：`etf_flows` → `etf_holdings`
14. Group 6：`derivatives` → `options_data`
15. Group 7：`macro_series` → `macro_events`
16. Group 8：`sentiment`
17. Group 9：`indicator_values` → `cycle_states` → `valuation_states` → `risk_scores` → `market_regimes` → `signals`
18. Group 10：`model_weights` → `model_validations`
19. Group 11：`backtest_runs` → `backtest_results` → `backtest_trades`
20. Group 12：`user_plans` → `user_transactions` → `user_holdings` → `portfolio_snapshots`
21. Group 13：`data_quality` → `sync_checkpoints` → `system_jobs` → `audit_logs` → `market_events`
22. 插入种子数据
23. 创建 Continuous Aggregates
24. 配置压缩和保留策略
