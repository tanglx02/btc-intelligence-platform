-- ============================================================
-- BTC 全市场智能研究平台 - 数据库完整初始化脚本
-- 由 docker-entrypoint-initdb.d 在首次启动时自动执行
-- 包含：扩展 → ENUM → 触发器 → 表 → 索引 → 超表 → 压缩 → 保留 → 种子数据
-- ============================================================

-- ==================== 1. 扩展 ====================
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ==================== 2. ENUM 类型 ====================
DO $$ BEGIN
    CREATE TYPE provider_category AS ENUM ('MARKET','ONCHAIN','EXCHANGE_FLOW','ETF','DERIVATIVES','OPTIONS','MACRO','SENTIMENT','NEWS');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE provider_status AS ENUM ('ONLINE','DEGRADED','SLOW','RATE_LIMITED','AUTH_ERROR','NETWORK_ERROR','DATA_ERROR','OFFLINE','DISABLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE failover_resolution AS ENUM ('AUTO_RECOVERED','MANUAL_RESET','TIMEOUT','PERMANENTLY_DISABLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE quality_status_type AS ENUM ('VERIFIED','ESTIMATED','STALE','CONFLICT','INVALID');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE candle_interval AS ENUM ('1m','5m','15m','30m','1h','4h','1d','1w');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE cycle_phase AS ENUM ('DEEP_BEAR','BEAR','BOTTOM_BUILDING','RECOVERY','UPTREND','ACCELERATION','DISTRIBUTION','TOP_RISK','DECLINE');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE valuation_level AS ENUM ('DEEP_UNDERVALUED','UNDERVALUED','FAIR','OVERVALUED','EXTREME_OVERVALUED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE risk_level AS ENUM ('VERY_LOW','LOW','MODERATE','HIGH','VERY_HIGH','EXTREME');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE signal_direction AS ENUM ('BULLISH','BEARISH','NEUTRAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE job_status AS ENUM ('PENDING','RUNNING','PAUSED','COMPLETED','FAILED','CANCELLED','SKIPPED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sync_status AS ENUM ('IDLE','SYNCING','PAUSED','COMPLETED','FAILED','RETRY');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE user_role AS ENUM ('ADMIN','ANALYST','USER');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE trade_side AS ENUM ('BUY','SELL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE plan_status AS ENUM ('ACTIVE','PAUSED','COMPLETED','CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE model_status AS ENUM ('DRAFT','TRAINING','VALIDATED','DEPLOYED','DEPRECATED','REJECTED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE backtest_status AS ENUM ('PENDING','RUNNING','COMPLETED','FAILED','CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ==================== 3. 触发器函数 ====================
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ==================== 4. 表结构 ====================

-- 4.1 assets（资产字典，被多表引用）
CREATE TABLE IF NOT EXISTS assets (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL UNIQUE, name VARCHAR(100) NOT NULL,
    asset_type VARCHAR(30) NOT NULL DEFAULT 'CRYPTO', chain VARCHAR(30) DEFAULT 'bitcoin',
    decimals SMALLINT NOT NULL DEFAULT 8, is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON assets FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.2 users
CREATE TABLE IF NOT EXISTS users (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE, email VARCHAR(200) NOT NULL UNIQUE,
    password_hash VARCHAR(200) NOT NULL, display_name VARCHAR(100),
    role user_role NOT NULL DEFAULT 'USER',
    preferences JSONB NOT NULL DEFAULT '{"language":"zh-CN","theme":"dark","mode":"simple"}',
    notification_config JSONB DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true, is_verified BOOLEAN NOT NULL DEFAULT false,
    last_login_at TIMESTAMPTZ, login_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role) WHERE is_active = true;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.3 providers
CREATE TABLE IF NOT EXISTS providers (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE, category provider_category NOT NULL,
    base_url VARCHAR(500) NOT NULL, api_key_encrypted TEXT,
    proxy_config JSONB DEFAULT '{"type":"direct"}',
    timeout_config JSONB DEFAULT '{"connect":5000,"read":30000,"write":10000}',
    retry_config JSONB DEFAULT '{"max_retries":3,"backoff_factor":2,"backoff_max":60}',
    rate_limit INTEGER DEFAULT 60, rate_limit_window INTEGER DEFAULT 60,
    priority INTEGER NOT NULL DEFAULT 100, is_enabled BOOLEAN NOT NULL DEFAULT true,
    is_locked BOOLEAN NOT NULL DEFAULT false, status provider_status NOT NULL DEFAULT 'OFFLINE',
    health_score NUMERIC(5,2) DEFAULT 0, recovery_threshold INTEGER DEFAULT 3,
    failure_threshold INTEGER DEFAULT 5, description TEXT,
    supported_symbols TEXT[] DEFAULT ARRAY['BTC'], supported_intervals TEXT[] DEFAULT '{}',
    last_success_at TIMESTAMPTZ, last_failure_at TIMESTAMPTZ,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_providers_category ON providers (category) WHERE is_enabled = true;
CREATE INDEX IF NOT EXISTS idx_providers_priority ON providers (category, priority) WHERE is_enabled = true;
CREATE INDEX IF NOT EXISTS idx_providers_status ON providers (status);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON providers FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.4 provider_health
CREATE TABLE IF NOT EXISTS provider_health (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    check_time TIMESTAMPTZ NOT NULL DEFAULT NOW(), status provider_status NOT NULL,
    response_time_ms INTEGER, success_rate_1h NUMERIC(5,2), success_rate_24h NUMERIC(5,2),
    consecutive_failures INTEGER NOT NULL DEFAULT 0, today_failures INTEGER NOT NULL DEFAULT 0,
    today_requests INTEGER NOT NULL DEFAULT 0, data_latency_ms INTEGER,
    last_error TEXT, last_error_type VARCHAR(50), http_status INTEGER,
    is_rate_limited BOOLEAN DEFAULT false, metrics JSONB DEFAULT '{}',
    PRIMARY KEY (id, check_time)
);
CREATE INDEX IF NOT EXISTS idx_provider_health_pid_time ON provider_health (provider_id, check_time);

-- 4.5 provider_scores
CREATE TABLE IF NOT EXISTS provider_scores (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    accuracy_score NUMERIC(5,2) NOT NULL DEFAULT 0, latency_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    stability_score NUMERIC(5,2) NOT NULL DEFAULT 0, completeness_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    consistency_score NUMERIC(5,2) NOT NULL DEFAULT 0, overall_score NUMERIC(5,2) NOT NULL DEFAULT 0,
    rank INTEGER, score_details JSONB DEFAULT '{}',
    PRIMARY KEY (id, scored_at)
);
CREATE INDEX IF NOT EXISTS idx_provider_scores_pid ON provider_scores (provider_id, scored_at);
CREATE INDEX IF NOT EXISTS idx_provider_scores_rank ON provider_scores (scored_at, rank);

-- 4.6 provider_failover_events
CREATE TABLE IF NOT EXISTS provider_failover_events (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    from_provider_id UUID NOT NULL REFERENCES providers(id),
    to_provider_id UUID REFERENCES providers(id),
    data_category provider_category NOT NULL, symbol VARCHAR(20) DEFAULT 'BTC',
    trigger_reason VARCHAR(50) NOT NULL, error_message TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), resolved_at TIMESTAMPTZ,
    resolution_type failover_resolution, duration_seconds INTEGER,
    requests_affected INTEGER DEFAULT 0, context JSONB DEFAULT '{}',
    PRIMARY KEY (id, occurred_at)
);
CREATE INDEX IF NOT EXISTS idx_failover_from ON provider_failover_events (from_provider_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_failover_category ON provider_failover_events (data_category, occurred_at);

-- 4.7 provider_requests
CREATE TABLE IF NOT EXISTS provider_requests (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_id UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    request_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    method VARCHAR(10) NOT NULL DEFAULT 'GET', url TEXT NOT NULL, endpoint VARCHAR(200),
    status_code INTEGER, response_time_ms INTEGER, error_type VARCHAR(50), error_message TEXT,
    request_params JSONB DEFAULT '{}', response_size_bytes INTEGER,
    is_success BOOLEAN NOT NULL DEFAULT true, is_sampled BOOLEAN NOT NULL DEFAULT false,
    data_category provider_category,
    PRIMARY KEY (id, request_time)
);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON provider_requests (provider_id, request_time);
CREATE INDEX IF NOT EXISTS idx_requests_failed ON provider_requests (provider_id, request_time) WHERE is_success = false;

-- 4.8 indicator_definitions
CREATE TABLE IF NOT EXISTS indicator_definitions (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    code VARCHAR(50) NOT NULL UNIQUE, name VARCHAR(100) NOT NULL, name_cn VARCHAR(100) NOT NULL,
    category VARCHAR(50) NOT NULL, sub_category VARCHAR(50),
    description TEXT, description_cn TEXT, formula TEXT, formula_latex TEXT,
    unit VARCHAR(30) DEFAULT 'value', value_range VARCHAR(50),
    frequency VARCHAR(20) NOT NULL DEFAULT '1d',
    params_schema JSONB NOT NULL DEFAULT '{}', default_params JSONB NOT NULL DEFAULT '{}',
    display_config JSONB NOT NULL DEFAULT '{}', interpretation JSONB DEFAULT '{}',
    data_dependencies TEXT[] DEFAULT '{}',
    is_active BOOLEAN NOT NULL DEFAULT true, is_primary BOOLEAN NOT NULL DEFAULT false,
    sort_order INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_indicator_def_category ON indicator_definitions (category, is_active);
CREATE INDEX IF NOT EXISTS idx_indicator_def_primary ON indicator_definitions (sort_order) WHERE is_primary = true;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON indicator_definitions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.9 model_versions
CREATE TABLE IF NOT EXISTS model_versions (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    model_name VARCHAR(100) NOT NULL, version VARCHAR(50) NOT NULL, model_type VARCHAR(50) NOT NULL,
    description TEXT, description_cn TEXT,
    architecture JSONB NOT NULL DEFAULT '{}', hyperparameters JSONB NOT NULL DEFAULT '{}',
    training_config JSONB NOT NULL DEFAULT '{}', features_used TEXT[] DEFAULT '{}',
    train_start_date DATE NOT NULL, train_end_date DATE NOT NULL,
    test_start_date DATE, test_end_date DATE,
    validation_type VARCHAR(30) DEFAULT 'WALK_FORWARD',
    status model_status NOT NULL DEFAULT 'DRAFT', is_production BOOLEAN NOT NULL DEFAULT false,
    performance_metrics JSONB DEFAULT '{}', known_limitations TEXT, effective_conditions TEXT,
    parent_version_id UUID REFERENCES model_versions(id), created_by UUID REFERENCES users(id),
    changelog TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (model_name, version)
);
CREATE INDEX IF NOT EXISTS idx_model_versions_name ON model_versions (model_name, status);
CREATE INDEX IF NOT EXISTS idx_model_versions_production ON model_versions (model_name) WHERE is_production = true;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON model_versions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.10 strategies
CREATE TABLE IF NOT EXISTS strategies (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(100) NOT NULL, name_cn VARCHAR(100),
    category VARCHAR(50) NOT NULL DEFAULT 'DCA',
    description TEXT, description_cn TEXT,
    parameters_schema JSONB NOT NULL DEFAULT '{}', rules_description TEXT,
    is_active BOOLEAN NOT NULL DEFAULT true, is_system BOOLEAN NOT NULL DEFAULT false,
    tags TEXT[] DEFAULT '{}', created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_strategies_category ON strategies (category) WHERE is_active = true;
CREATE TRIGGER set_updated_at BEFORE UPDATE ON strategies FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.11 strategy_versions
CREATE TABLE IF NOT EXISTS strategy_versions (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    strategy_id UUID NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
    version VARCHAR(50) NOT NULL, parameters JSONB NOT NULL DEFAULT '{}',
    rules_config JSONB NOT NULL DEFAULT '{}', changelog TEXT,
    is_current BOOLEAN NOT NULL DEFAULT false, created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (strategy_id, version)
);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_sid ON strategy_versions (strategy_id, created_at);
CREATE INDEX IF NOT EXISTS idx_strategy_versions_current ON strategy_versions (strategy_id) WHERE is_current = true;

-- 4.12 raw_market_data
CREATE TABLE IF NOT EXISTS raw_market_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTC', data_type VARCHAR(50) NOT NULL,
    raw_response JSONB NOT NULL, endpoint TEXT NOT NULL,
    request_params JSONB DEFAULT '{}', response_headers JSONB DEFAULT '{}',
    response_size_bytes INTEGER,
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_market_source ON raw_market_data (source_id, fetch_time DESC);
CREATE INDEX IF NOT EXISTS idx_raw_market_symbol ON raw_market_data (symbol, data_type, fetch_time DESC);
CREATE INDEX IF NOT EXISTS idx_raw_market_response ON raw_market_data USING GIN (raw_response jsonb_path_ops);

-- 4.13 raw_onchain_data
CREATE TABLE IF NOT EXISTS raw_onchain_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name VARCHAR(100) NOT NULL, raw_response JSONB NOT NULL,
    endpoint TEXT NOT NULL, request_params JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_onchain_source ON raw_onchain_data (source_id, fetch_time DESC);

-- 4.14 raw_etf_data
CREATE TABLE IF NOT EXISTS raw_etf_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker VARCHAR(20) NOT NULL, data_type VARCHAR(50) NOT NULL DEFAULT 'FLOW',
    raw_response JSONB NOT NULL, endpoint TEXT NOT NULL, request_params JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_etf_source ON raw_etf_data (source_id, fetch_time DESC);

-- 4.15 raw_derivatives_data
CREATE TABLE IF NOT EXISTS raw_derivatives_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(30) NOT NULL DEFAULT 'BTC', exchange VARCHAR(50) NOT NULL,
    data_type VARCHAR(50) NOT NULL, raw_response JSONB NOT NULL,
    endpoint TEXT NOT NULL, request_params JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_deriv_source ON raw_derivatives_data (source_id, fetch_time DESC);

-- 4.16 raw_macro_data
CREATE TABLE IF NOT EXISTS raw_macro_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    series_id VARCHAR(100) NOT NULL, raw_response JSONB NOT NULL,
    endpoint TEXT NOT NULL, request_params JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_macro_source ON raw_macro_data (source_id, fetch_time DESC);

-- 4.17 raw_sentiment_data
CREATE TABLE IF NOT EXISTS raw_sentiment_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL, raw_response JSONB NOT NULL,
    endpoint TEXT NOT NULL, request_params JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, fetch_time)
);
CREATE INDEX IF NOT EXISTS idx_raw_sentiment_source ON raw_sentiment_data (source_id, fetch_time DESC);

-- 4.18 market_prices
CREATE TABLE IF NOT EXISTS market_prices (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT',
    price NUMERIC(20,8) NOT NULL, bid NUMERIC(20,8), ask NUMERIC(20,8), spread NUMERIC(20,8),
    volume_24h NUMERIC(24,4), quote_volume_24h NUMERIC(24,4), market_cap NUMERIC(24,2),
    high_24h NUMERIC(20,8), low_24h NUMERIC(20,8), price_change_pct_24h NUMERIC(8,4),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    cross_validated BOOLEAN NOT NULL DEFAULT false, validation_sources INTEGER DEFAULT 1,
    deviation_pct NUMERIC(8,6),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol_time ON market_prices (symbol, observation_time);
CREATE INDEX IF NOT EXISTS idx_prices_source ON market_prices (source_id, observation_time);
CREATE INDEX IF NOT EXISTS idx_prices_quality ON market_prices (observation_time) WHERE quality_status != 'VERIFIED';

-- 4.19 candles
CREATE TABLE IF NOT EXISTS candles (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT', interval candle_interval NOT NULL,
    open NUMERIC(20,8) NOT NULL, high NUMERIC(20,8) NOT NULL,
    low NUMERIC(20,8) NOT NULL, close NUMERIC(20,8) NOT NULL,
    volume NUMERIC(24,4) NOT NULL DEFAULT 0, quote_volume NUMERIC(24,4),
    trades INTEGER, taker_buy_volume NUMERIC(24,4), taker_sell_volume NUMERIC(24,4),
    vwap NUMERIC(20,8),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_candles_unique ON candles (symbol, interval, observation_time);
CREATE INDEX IF NOT EXISTS idx_candles_source ON candles (source_id, observation_time);

-- 4.20 orderbooks
CREATE TABLE IF NOT EXISTS orderbooks (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT', exchange VARCHAR(50) NOT NULL,
    bids JSONB NOT NULL DEFAULT '[]', asks JSONB NOT NULL DEFAULT '[]',
    bid_depth NUMERIC(24,4), ask_depth NUMERIC(24,4),
    spread NUMERIC(20,8), spread_pct NUMERIC(10,6), mid_price NUMERIC(20,8),
    imbalance NUMERIC(10,6),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_orderbooks_symbol ON orderbooks (symbol, exchange, observation_time);

-- 4.21 trades
CREATE TABLE IF NOT EXISTS trades (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTCUSDT', exchange VARCHAR(50) NOT NULL,
    trade_id VARCHAR(100), side trade_side NOT NULL,
    price NUMERIC(20,8) NOT NULL, quantity NUMERIC(20,8) NOT NULL,
    quote_quantity NUMERIC(24,4), is_buyer_maker BOOLEAN,
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades (symbol, exchange, observation_time);

-- 4.22 onchain_metrics
CREATE TABLE IF NOT EXISTS onchain_metrics (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name VARCHAR(100) NOT NULL, metric_value NUMERIC(30,2) NOT NULL,
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTC',
    change_24h NUMERIC(20,4), change_7d NUMERIC(20,4), change_30d NUMERIC(20,4),
    unit VARCHAR(30), extra_data JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_onchain_metric ON onchain_metrics (metric_name, observation_time);
CREATE INDEX IF NOT EXISTS idx_onchain_source ON onchain_metrics (source_id, observation_time);

-- 4.23 exchange_flows
CREATE TABLE IF NOT EXISTS exchange_flows (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange_name VARCHAR(100) NOT NULL, flow_type VARCHAR(30) NOT NULL,
    amount NUMERIC(20,8) NOT NULL, amount_usd NUMERIC(24,2),
    net_flow NUMERIC(20,8), balance NUMERIC(20,8), balance_change_pct NUMERIC(10,4),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_exchange_flows_name ON exchange_flows (exchange_name, observation_time);

-- 4.24 address_metrics
CREATE TABLE IF NOT EXISTS address_metrics (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name VARCHAR(100) NOT NULL, metric_value NUMERIC(30,2) NOT NULL,
    change_1d NUMERIC(20,4), change_7d NUMERIC(20,4), change_30d NUMERIC(20,4),
    extra_data JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_address_metric ON address_metrics (metric_name, observation_time);

-- 4.25 supply_metrics
CREATE TABLE IF NOT EXISTS supply_metrics (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metric_name VARCHAR(100) NOT NULL, metric_value NUMERIC(30,2) NOT NULL,
    supply_type VARCHAR(50), percentage_of_total NUMERIC(10,4),
    change_1d NUMERIC(20,4), change_7d NUMERIC(20,4), change_30d NUMERIC(20,4),
    extra_data JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_supply_metric ON supply_metrics (metric_name, observation_time);

-- 4.26 etf_flows
CREATE TABLE IF NOT EXISTS etf_flows (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker VARCHAR(20) NOT NULL, fund_name VARCHAR(200) NOT NULL,
    issuer VARCHAR(100), daily_flow NUMERIC(20,2) NOT NULL DEFAULT 0,
    cumulative_flow NUMERIC(20,2), daily_flow_btc NUMERIC(20,8),
    nav NUMERIC(20,2), shares_outstanding NUMERIC(20,2),
    volume NUMERIC(20,2), avg_daily_volume_30d NUMERIC(20,2),
    premium_discount_pct NUMERIC(10,4), holdings_btc NUMERIC(20,8),
    market_share_pct NUMERIC(10,4),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_etf_flows_ticker ON etf_flows (ticker, observation_time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_etf_flows_unique ON etf_flows (ticker, observation_time);

-- 4.27 etf_holdings
CREATE TABLE IF NOT EXISTS etf_holdings (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ticker VARCHAR(20) NOT NULL, fund_name VARCHAR(200) NOT NULL,
    total_btc NUMERIC(20,8) NOT NULL DEFAULT 0, total_value_usd NUMERIC(20,2),
    btc_change_1d NUMERIC(20,8), btc_change_7d NUMERIC(20,8),
    pct_of_total_etf NUMERIC(10,4), pct_of_btc_supply NUMERIC(10,6),
    custody_provider VARCHAR(100),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_etf_holdings_ticker ON etf_holdings (ticker, observation_time);

-- 4.28 derivatives
CREATE TABLE IF NOT EXISTS derivatives (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange VARCHAR(50) NOT NULL, symbol VARCHAR(30) NOT NULL DEFAULT 'BTC',
    data_type VARCHAR(50) NOT NULL,
    funding_rate NUMERIC(12,8), next_funding_time TIMESTAMPTZ,
    open_interest NUMERIC(24,4), open_interest_usd NUMERIC(24,2),
    long_short_ratio NUMERIC(10,4), top_trader_long_short NUMERIC(10,4),
    liquidations_long NUMERIC(20,2), liquidations_short NUMERIC(20,2),
    volume_24h NUMERIC(24,4), basis NUMERIC(20,8), basis_pct NUMERIC(10,6),
    mark_price NUMERIC(20,8), index_price NUMERIC(20,8),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_derivatives_exchange ON derivatives (exchange, data_type, observation_time);
CREATE INDEX IF NOT EXISTS idx_derivatives_funding ON derivatives (symbol, observation_time) WHERE data_type = 'FUNDING';

-- 4.29 options_data
CREATE TABLE IF NOT EXISTS options_data (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    exchange VARCHAR(50) NOT NULL, option_type VARCHAR(10) NOT NULL,
    strike_price NUMERIC(20,8) NOT NULL, expiry_date DATE NOT NULL,
    implied_volatility NUMERIC(10,6), delta NUMERIC(10,6), gamma NUMERIC(10,6),
    theta NUMERIC(10,6), vega NUMERIC(10,6), rho NUMERIC(10,6),
    volume NUMERIC(20,4), open_interest NUMERIC(20,4),
    mark_price NUMERIC(20,8), underlying_price NUMERIC(20,8),
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_options_exchange ON options_data (exchange, option_type, observation_time);
CREATE INDEX IF NOT EXISTS idx_options_expiry ON options_data (expiry_date, strike_price);

-- 4.30 macro_series
CREATE TABLE IF NOT EXISTS macro_series (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    series_id VARCHAR(100) NOT NULL, series_name VARCHAR(200) NOT NULL,
    value NUMERIC(20,4) NOT NULL,
    reference_date DATE NOT NULL, release_date DATE, revision_date DATE,
    period VARCHAR(20), unit VARCHAR(30),
    change_from_prior NUMERIC(20,4), yoy_change NUMERIC(10,4),
    is_preliminary BOOLEAN DEFAULT true,
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_macro_series_id ON macro_series (series_id, reference_date DESC);
CREATE INDEX IF NOT EXISTS idx_macro_release ON macro_series (release_date DESC) WHERE release_date IS NOT NULL;

-- 4.31 macro_events
CREATE TABLE IF NOT EXISTS macro_events (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    event_name VARCHAR(200) NOT NULL, event_category VARCHAR(50) NOT NULL,
    event_time TIMESTAMPTZ NOT NULL, importance VARCHAR(20) NOT NULL DEFAULT 'MEDIUM',
    actual_value VARCHAR(50), forecast_value VARCHAR(50), previous_value VARCHAR(50),
    impact_description TEXT, market_reaction JSONB DEFAULT '{}',
    is_recurring BOOLEAN DEFAULT false, recurrence_pattern VARCHAR(100),
    source VARCHAR(100), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_macro_events_time ON macro_events (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_macro_events_category ON macro_events (event_category, event_time DESC);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON macro_events FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.32 sentiment
CREATE TABLE IF NOT EXISTS sentiment (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES providers(id),
    observation_time TIMESTAMPTZ NOT NULL, fetch_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_type VARCHAR(50) NOT NULL, metric_name VARCHAR(100) NOT NULL,
    value NUMERIC(10,4) NOT NULL, normalized_value NUMERIC(10,4),
    label VARCHAR(50), confidence NUMERIC(5,4),
    sample_size INTEGER, extra_data JSONB DEFAULT '{}',
    quality_status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_sentiment_source ON sentiment (source_type, metric_name, observation_time);
CREATE INDEX IF NOT EXISTS idx_sentiment_time ON sentiment (observation_time DESC);

-- 4.33 indicator_values
CREATE TABLE IF NOT EXISTS indicator_values (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    indicator_id UUID NOT NULL REFERENCES indicator_definitions(id),
    observation_time TIMESTAMPTZ NOT NULL,
    symbol VARCHAR(20) NOT NULL DEFAULT 'BTC',
    value NUMERIC(30,10) NOT NULL,
    normalized_value NUMERIC(10,6), z_score NUMERIC(10,6),
    percentile NUMERIC(10,4), signal VARCHAR(20),
    params_used JSONB DEFAULT '{}', computation_time_ms INTEGER,
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_indicator_val ON indicator_values (indicator_id, symbol, observation_time DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_indicator_val_unique ON indicator_values (indicator_id, symbol, observation_time);

-- 4.34 cycle_states
CREATE TABLE IF NOT EXISTS cycle_states (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    observation_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version_id UUID REFERENCES model_versions(id),
    phase cycle_phase NOT NULL, confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    phase_duration_days INTEGER, days_since_transition INTEGER,
    transition_probability JSONB DEFAULT '{}',
    next_likely_phases JSONB DEFAULT '[]',
    historical_similar JSONB DEFAULT '[]',
    feature_contributions JSONB DEFAULT '{}',
    explanation TEXT, explanation_cn TEXT,
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_cycle_phase ON cycle_states (phase, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_cycle_time ON cycle_states (observation_time DESC);

-- 4.35 valuation_states
CREATE TABLE IF NOT EXISTS valuation_states (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    observation_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version_id UUID REFERENCES model_versions(id),
    level valuation_level NOT NULL, composite_score NUMERIC(10,4) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    fair_value_estimate NUMERIC(20,2), deviation_from_fair_pct NUMERIC(10,4),
    component_scores JSONB DEFAULT '{}',
    historical_percentile NUMERIC(10,4),
    support_level NUMERIC(20,2), resistance_level NUMERIC(20,2),
    explanation TEXT, explanation_cn TEXT,
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_valuation_level ON valuation_states (level, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_valuation_time ON valuation_states (observation_time DESC);

-- 4.36 risk_scores
CREATE TABLE IF NOT EXISTS risk_scores (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    observation_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version_id UUID REFERENCES model_versions(id),
    level risk_level NOT NULL, composite_score NUMERIC(10,4) NOT NULL,
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    risk_factors JSONB DEFAULT '{}',
    volatility_risk NUMERIC(10,4), liquidity_risk NUMERIC(10,4),
    systemic_risk NUMERIC(10,4), drawdown_risk NUMERIC(10,4),
    max_drawdown_estimate NUMERIC(10,4),
    var_95 NUMERIC(20,2), cvar_95 NUMERIC(20,2),
    explanation TEXT, explanation_cn TEXT,
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_risk_level ON risk_scores (level, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_risk_time ON risk_scores (observation_time DESC);

-- 4.37 market_regimes
CREATE TABLE IF NOT EXISTS market_regimes (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    observation_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    model_version_id UUID REFERENCES model_versions(id),
    regime VARCHAR(50) NOT NULL, sub_regime VARCHAR(50),
    confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    volatility_regime VARCHAR(30), trend_regime VARCHAR(30),
    liquidity_regime VARCHAR(30), sentiment_regime VARCHAR(30),
    regime_duration_days INTEGER, transition_indicators JSONB DEFAULT '{}',
    recommended_actions JSONB DEFAULT '[]',
    explanation TEXT, explanation_cn TEXT,
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_regime ON market_regimes (regime, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_regime_time ON market_regimes (observation_time DESC);

-- 4.38 signals
CREATE TABLE IF NOT EXISTS signals (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    signal_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    category VARCHAR(50) NOT NULL, signal_type VARCHAR(100) NOT NULL,
    direction signal_direction NOT NULL,
    strength NUMERIC(5,4) NOT NULL DEFAULT 0, confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
    source_model_id UUID REFERENCES model_versions(id),
    related_indicators TEXT[] DEFAULT '{}',
    trigger_conditions JSONB DEFAULT '{}',
    suggested_action VARCHAR(50), position_size_pct NUMERIC(5,2),
    stop_loss_pct NUMERIC(5,2), take_profit_pct NUMERIC(5,2),
    expires_at TIMESTAMPTZ, is_active BOOLEAN NOT NULL DEFAULT true,
    explanation TEXT, explanation_cn TEXT,
    PRIMARY KEY (id, signal_time)
);
CREATE INDEX IF NOT EXISTS idx_signals_category ON signals (category, direction, signal_time DESC);
CREATE INDEX IF NOT EXISTS idx_signals_active ON signals (signal_time DESC) WHERE is_active = true;

-- 4.39 model_weights
CREATE TABLE IF NOT EXISTS model_weights (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    model_version_id UUID NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    weight_name VARCHAR(200) NOT NULL,
    weight_data BYTEA NOT NULL, shape INTEGER[] NOT NULL,
    dtype VARCHAR(20) NOT NULL DEFAULT 'float32',
    layer_index INTEGER, is_trainable BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_model_weights_mv ON model_weights (model_version_id);

-- 4.40 model_validations
CREATE TABLE IF NOT EXISTS model_validations (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    model_version_id UUID NOT NULL REFERENCES model_versions(id) ON DELETE CASCADE,
    validation_type VARCHAR(50) NOT NULL, validated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dataset_description TEXT, train_metrics JSONB DEFAULT '{}',
    test_metrics JSONB DEFAULT '{}', validation_metrics JSONB DEFAULT '{}',
    is_passed BOOLEAN NOT NULL DEFAULT false, pass_criteria JSONB DEFAULT '{}',
    failure_reasons TEXT[], notes TEXT, validated_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_model_validations_mv ON model_validations (model_version_id, validated_at DESC);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON model_validations FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.41 backtest_runs
CREATE TABLE IF NOT EXISTS backtest_runs (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    strategy_version_id UUID NOT NULL REFERENCES strategy_versions(id),
    model_version_id UUID REFERENCES model_versions(id),
    status backtest_status NOT NULL DEFAULT 'PENDING',
    start_date DATE NOT NULL, end_date DATE NOT NULL,
    initial_capital NUMERIC(20,2) NOT NULL DEFAULT 10000,
    currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    commission_pct NUMERIC(8,4) DEFAULT 0.001,
    slippage_pct NUMERIC(8,4) DEFAULT 0.0005,
    parameters JSONB NOT NULL DEFAULT '{}',
    results_summary JSONB DEFAULT '{}',
    error_message TEXT, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
    duration_seconds INTEGER, created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs (strategy_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_status ON backtest_runs (status) WHERE status IN ('PENDING','RUNNING');
CREATE TRIGGER set_updated_at BEFORE UPDATE ON backtest_runs FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.42 backtest_results
CREATE TABLE IF NOT EXISTS backtest_results (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    backtest_run_id UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    observation_time TIMESTAMPTZ NOT NULL,
    equity NUMERIC(20,2) NOT NULL, cash NUMERIC(20,2) NOT NULL,
    position_value NUMERIC(20,2), drawdown_pct NUMERIC(10,4),
    daily_return_pct NUMERIC(10,4), cumulative_return_pct NUMERIC(10,4),
    position_size NUMERIC(20,8), signal_value NUMERIC(10,4),
    metadata JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_backtest_results_run ON backtest_results (backtest_run_id, observation_time);

-- 4.43 backtest_trades
CREATE TABLE IF NOT EXISTS backtest_trades (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    backtest_run_id UUID NOT NULL REFERENCES backtest_runs(id) ON DELETE CASCADE,
    observation_time TIMESTAMPTZ NOT NULL,
    side trade_side NOT NULL, price NUMERIC(20,8) NOT NULL,
    quantity NUMERIC(20,8) NOT NULL, commission NUMERIC(20,8) DEFAULT 0,
    slippage NUMERIC(20,8) DEFAULT 0, pnl NUMERIC(20,2),
    pnl_pct NUMERIC(10,4), reason VARCHAR(100),
    signal_strength NUMERIC(5,4), equity_after NUMERIC(20,2),
    metadata JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades (backtest_run_id, observation_time);

-- 4.44 user_plans
CREATE TABLE IF NOT EXISTS user_plans (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    strategy_version_id UUID REFERENCES strategy_versions(id),
    name VARCHAR(100) NOT NULL, description TEXT,
    plan_type VARCHAR(30) NOT NULL DEFAULT 'DCA',
    status plan_status NOT NULL DEFAULT 'ACTIVE',
    amount_per_period NUMERIC(16,2) NOT NULL, currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    period VARCHAR(20) NOT NULL DEFAULT 'WEEKLY',
    day_of_period INTEGER, hour_of_day INTEGER DEFAULT 0,
    total_invested NUMERIC(16,2) DEFAULT 0, total_periods_completed INTEGER DEFAULT 0,
    total_periods_planned INTEGER, start_date DATE NOT NULL, end_date DATE,
    next_execution_at TIMESTAMPTZ, last_execution_at TIMESTAMPTZ,
    parameters JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_user_plans_user ON user_plans (user_id, status);
CREATE INDEX IF NOT EXISTS idx_user_plans_next ON user_plans (next_execution_at) WHERE status = 'ACTIVE';
CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_plans FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.45 user_transactions
CREATE TABLE IF NOT EXISTS user_transactions (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    plan_id UUID REFERENCES user_plans(id) ON DELETE SET NULL,
    transaction_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    transaction_type VARCHAR(30) NOT NULL, side trade_side NOT NULL,
    asset VARCHAR(20) NOT NULL DEFAULT 'BTC',
    price NUMERIC(20,8) NOT NULL, quantity NUMERIC(20,8) NOT NULL,
    amount NUMERIC(16,2) NOT NULL, currency VARCHAR(10) NOT NULL DEFAULT 'USD',
    fee NUMERIC(16,4) DEFAULT 0, fee_currency VARCHAR(10) DEFAULT 'USD',
    source VARCHAR(30) NOT NULL DEFAULT 'MANUAL',
    exchange VARCHAR(50), notes TEXT, metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, transaction_time)
);
CREATE INDEX IF NOT EXISTS idx_user_tx_user ON user_transactions (user_id, transaction_time DESC);
CREATE INDEX IF NOT EXISTS idx_user_tx_plan ON user_transactions (plan_id, transaction_time DESC);
CREATE INDEX IF NOT EXISTS idx_user_tx_type ON user_transactions (transaction_type, transaction_time DESC);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_transactions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.46 user_holdings
CREATE TABLE IF NOT EXISTS user_holdings (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    snapshot_time TIMESTAMPTZ NOT NULL DEFAULT NOW(), observation_time TIMESTAMPTZ NOT NULL,
    asset VARCHAR(20) NOT NULL DEFAULT 'BTC',
    quantity NUMERIC(20,8) NOT NULL DEFAULT 0, avg_cost NUMERIC(20,8) NOT NULL DEFAULT 0,
    total_cost NUMERIC(16,2) NOT NULL DEFAULT 0,
    current_price NUMERIC(20,8), market_value NUMERIC(16,2), market_value_cny NUMERIC(16,2),
    unrealized_pnl NUMERIC(16,2), unrealized_pnl_pct NUMERIC(8,4),
    realized_pnl NUMERIC(16,2) DEFAULT 0, total_return_pct NUMERIC(10,4),
    metadata JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_user_holdings_user ON user_holdings (user_id, observation_time DESC);

-- 4.47 portfolio_snapshots
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    observation_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    total_value_usd NUMERIC(16,2) NOT NULL, total_value_cny NUMERIC(16,2),
    total_cost NUMERIC(16,2) NOT NULL,
    unrealized_pnl NUMERIC(16,2), unrealized_pnl_pct NUMERIC(10,4),
    realized_pnl NUMERIC(16,2) DEFAULT 0,
    daily_pnl NUMERIC(16,2), daily_pnl_pct NUMERIC(10,4),
    allocation JSONB DEFAULT '{}', metadata JSONB DEFAULT '{}',
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user ON portfolio_snapshots (user_id, observation_time DESC);

-- 4.48 data_quality
CREATE TABLE IF NOT EXISTS data_quality (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    check_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data_category VARCHAR(50) NOT NULL, table_name VARCHAR(100) NOT NULL,
    source_id UUID REFERENCES providers(id),
    status quality_status_type NOT NULL DEFAULT 'VERIFIED',
    records_checked INTEGER NOT NULL DEFAULT 0, records_passed INTEGER NOT NULL DEFAULT 0,
    records_failed INTEGER NOT NULL DEFAULT 0,
    completeness_pct NUMERIC(5,2), accuracy_pct NUMERIC(5,2),
    timeliness_seconds INTEGER, issues JSONB DEFAULT '[]',
    check_duration_ms INTEGER, details JSONB DEFAULT '{}',
    PRIMARY KEY (id, check_time)
);
CREATE INDEX IF NOT EXISTS idx_data_quality_cat ON data_quality (data_category, check_time DESC);
CREATE INDEX IF NOT EXISTS idx_data_quality_issues ON data_quality (check_time DESC) WHERE status != 'VERIFIED';

-- 4.49 sync_checkpoints
CREATE TABLE IF NOT EXISTS sync_checkpoints (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    provider_id UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    data_category VARCHAR(50) NOT NULL, symbol VARCHAR(20) DEFAULT 'BTC',
    status sync_status NOT NULL DEFAULT 'IDLE',
    last_sync_time TIMESTAMPTZ, last_success_time TIMESTAMPTZ,
    next_sync_time TIMESTAMPTZ, checkpoint_data JSONB DEFAULT '{}',
    records_synced INTEGER DEFAULT 0, records_failed INTEGER DEFAULT 0,
    error_message TEXT, retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_cp_unique ON sync_checkpoints (provider_id, data_category, symbol);
CREATE INDEX IF NOT EXISTS idx_sync_cp_next ON sync_checkpoints (next_sync_time) WHERE status IN ('IDLE','RETRY');
CREATE TRIGGER set_updated_at BEFORE UPDATE ON sync_checkpoints FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.50 system_jobs
CREATE TABLE IF NOT EXISTS system_jobs (
    id UUID NOT NULL DEFAULT gen_random_uuid() PRIMARY KEY,
    job_name VARCHAR(100) NOT NULL, job_category VARCHAR(50) NOT NULL,
    status job_status NOT NULL DEFAULT 'PENDING',
    priority INTEGER NOT NULL DEFAULT 50,
    scheduled_at TIMESTAMPTZ, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
    progress_pct NUMERIC(5,2) DEFAULT 0,
    input_params JSONB DEFAULT '{}', output_result JSONB DEFAULT '{}',
    error_message TEXT, retry_count INTEGER DEFAULT 0, max_retries INTEGER DEFAULT 3,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_system_jobs_status ON system_jobs (status, priority DESC) WHERE status IN ('PENDING','RUNNING');
CREATE INDEX IF NOT EXISTS idx_system_jobs_category ON system_jobs (job_category, scheduled_at);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON system_jobs FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- 4.51 audit_logs
CREATE TABLE IF NOT EXISTS audit_logs (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    event_time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    action_category VARCHAR(50) NOT NULL, action_type VARCHAR(100) NOT NULL,
    resource_type VARCHAR(50), resource_id UUID,
    ip_address INET, user_agent TEXT,
    request_body JSONB, response_status INTEGER,
    old_values JSONB, new_values JSONB,
    duration_ms INTEGER, is_success BOOLEAN NOT NULL DEFAULT true,
    error_message TEXT,
    PRIMARY KEY (id, event_time)
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs (user_id, event_time DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs (action_category, action_type, event_time DESC);

-- 4.52 market_events
CREATE TABLE IF NOT EXISTS market_events (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    event_time TIMESTAMPTZ NOT NULL, observation_time TIMESTAMPTZ NOT NULL,
    event_type VARCHAR(50) NOT NULL, event_category VARCHAR(30) NOT NULL,
    title VARCHAR(200) NOT NULL, title_cn VARCHAR(200),
    description TEXT, description_cn TEXT,
    significance NUMERIC(5,4) NOT NULL DEFAULT 0.5, is_major BOOLEAN NOT NULL DEFAULT false,
    btc_price_at_event NUMERIC(20,8), btc_price_before_24h NUMERIC(20,8),
    btc_price_after_24h NUMERIC(20,8), btc_price_after_7d NUMERIC(20,8), btc_price_after_30d NUMERIC(20,8),
    market_impact JSONB DEFAULT '{}', related_data JSONB DEFAULT '{}',
    tags TEXT[] DEFAULT '{}',
    source_url TEXT, source_id UUID REFERENCES providers(id),
    is_auto_detected BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, observation_time)
);
CREATE INDEX IF NOT EXISTS idx_market_events_time ON market_events (observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_type ON market_events (event_type, observation_time DESC);
CREATE INDEX IF NOT EXISTS idx_market_events_major ON market_events (observation_time DESC) WHERE is_major = true;
CREATE INDEX IF NOT EXISTS idx_market_events_category ON market_events (event_category, observation_time DESC);
CREATE TRIGGER set_updated_at BEFORE UPDATE ON market_events FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ==================== 5. TimescaleDB 超表转换 ====================

-- Provider 时序表
SELECT create_hypertable('provider_health', 'check_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('provider_scores', 'scored_at', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('provider_failover_events', 'occurred_at', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('provider_requests', 'request_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);

-- 原始数据表
SELECT create_hypertable('raw_market_data', 'fetch_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('raw_onchain_data', 'fetch_time', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('raw_etf_data', 'fetch_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('raw_derivatives_data', 'fetch_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('raw_macro_data', 'fetch_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('raw_sentiment_data', 'fetch_time', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false, if_not_exists => TRUE);

-- 市场数据表
SELECT create_hypertable('market_prices', 'observation_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('candles', 'observation_time', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('orderbooks', 'observation_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('trades', 'observation_time', chunk_time_interval => INTERVAL '1 day', create_default_indexes => false, if_not_exists => TRUE);

-- 链上数据表
SELECT create_hypertable('onchain_metrics', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('exchange_flows', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('address_metrics', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('supply_metrics', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);

-- ETF 表
SELECT create_hypertable('etf_flows', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('etf_holdings', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);

-- 衍生品表
SELECT create_hypertable('derivatives', 'observation_time', chunk_time_interval => INTERVAL '7 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('options_data', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);

-- 宏观 + 情绪
SELECT create_hypertable('macro_series', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('sentiment', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);

-- 指标 + 引擎输出
SELECT create_hypertable('indicator_values', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('cycle_states', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('valuation_states', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('risk_scores', 'observation_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('market_regimes', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('signals', 'signal_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);

-- 回测
SELECT create_hypertable('backtest_results', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('backtest_trades', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);

-- 用户数据
SELECT create_hypertable('user_transactions', 'transaction_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('user_holdings', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('portfolio_snapshots', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);

-- 系统表
SELECT create_hypertable('data_quality', 'check_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('audit_logs', 'event_time', chunk_time_interval => INTERVAL '30 days', create_default_indexes => false, if_not_exists => TRUE);
SELECT create_hypertable('market_events', 'observation_time', chunk_time_interval => INTERVAL '1 year', create_default_indexes => false, if_not_exists => TRUE);

-- ==================== 6. 压缩策略 ====================

ALTER TABLE provider_health SET (timescaledb.compress, timescaledb.compress_segmentby = 'provider_id', timescaledb.compress_orderby = 'check_time DESC');
SELECT add_compression_policy('provider_health', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('provider_health', INTERVAL '2 years', if_not_exists => TRUE);

ALTER TABLE provider_scores SET (timescaledb.compress, timescaledb.compress_segmentby = 'provider_id', timescaledb.compress_orderby = 'scored_at DESC');
SELECT add_compression_policy('provider_scores', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE provider_failover_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'data_category', timescaledb.compress_orderby = 'occurred_at DESC');
SELECT add_compression_policy('provider_failover_events', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE provider_requests SET (timescaledb.compress, timescaledb.compress_segmentby = 'provider_id', timescaledb.compress_orderby = 'request_time DESC');
SELECT add_compression_policy('provider_requests', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy('provider_requests', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE raw_market_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'source_id,data_type', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_market_data', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('raw_market_data', INTERVAL '3 years', if_not_exists => TRUE);

ALTER TABLE raw_onchain_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'metric_name', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_onchain_data', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('raw_onchain_data', INTERVAL '5 years', if_not_exists => TRUE);

ALTER TABLE raw_etf_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'ticker', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_etf_data', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('raw_etf_data', INTERVAL '5 years', if_not_exists => TRUE);

ALTER TABLE raw_derivatives_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange,data_type', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_derivatives_data', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('raw_derivatives_data', INTERVAL '3 years', if_not_exists => TRUE);

ALTER TABLE raw_macro_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'series_id', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_macro_data', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE raw_sentiment_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'source_type', timescaledb.compress_orderby = 'fetch_time DESC');
SELECT add_compression_policy('raw_sentiment_data', INTERVAL '7 days', if_not_exists => TRUE);
SELECT add_retention_policy('raw_sentiment_data', INTERVAL '2 years', if_not_exists => TRUE);

ALTER TABLE market_prices SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('market_prices', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE candles SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol,interval', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('candles', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE orderbooks SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol,exchange', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('orderbooks', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy('orderbooks', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE trades SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol,exchange', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('trades', INTERVAL '3 days', if_not_exists => TRUE);
SELECT add_retention_policy('trades', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE onchain_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'metric_name', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('onchain_metrics', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE exchange_flows SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange_name', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('exchange_flows', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE address_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'metric_name', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('address_metrics', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE supply_metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'metric_name', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('supply_metrics', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE etf_flows SET (timescaledb.compress, timescaledb.compress_segmentby = 'ticker', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('etf_flows', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE etf_holdings SET (timescaledb.compress, timescaledb.compress_segmentby = 'ticker', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('etf_holdings', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE derivatives SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange,data_type', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('derivatives', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE options_data SET (timescaledb.compress, timescaledb.compress_segmentby = 'exchange,option_type', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('options_data', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('options_data', INTERVAL '3 years', if_not_exists => TRUE);

ALTER TABLE macro_series SET (timescaledb.compress, timescaledb.compress_segmentby = 'series_id', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('macro_series', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE sentiment SET (timescaledb.compress, timescaledb.compress_segmentby = 'source_type,metric_name', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('sentiment', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE indicator_values SET (timescaledb.compress, timescaledb.compress_segmentby = 'indicator_id,symbol', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('indicator_values', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE cycle_states SET (timescaledb.compress, timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('cycle_states', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE valuation_states SET (timescaledb.compress, timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('valuation_states', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE risk_scores SET (timescaledb.compress, timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('risk_scores', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE market_regimes SET (timescaledb.compress, timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('market_regimes', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE signals SET (timescaledb.compress, timescaledb.compress_segmentby = 'category', timescaledb.compress_orderby = 'signal_time DESC');
SELECT add_compression_policy('signals', INTERVAL '90 days', if_not_exists => TRUE);

ALTER TABLE backtest_results SET (timescaledb.compress, timescaledb.compress_segmentby = 'backtest_run_id', timescaledb.compress_orderby = 'observation_time');
SELECT add_compression_policy('backtest_results', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE backtest_trades SET (timescaledb.compress, timescaledb.compress_segmentby = 'backtest_run_id', timescaledb.compress_orderby = 'observation_time');
SELECT add_compression_policy('backtest_trades', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE user_holdings SET (timescaledb.compress, timescaledb.compress_segmentby = 'user_id', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('user_holdings', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE portfolio_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'user_id', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('portfolio_snapshots', INTERVAL '1 year', if_not_exists => TRUE);

ALTER TABLE data_quality SET (timescaledb.compress, timescaledb.compress_segmentby = 'data_category', timescaledb.compress_orderby = 'check_time DESC');
SELECT add_compression_policy('data_quality', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('data_quality', INTERVAL '2 years', if_not_exists => TRUE);

ALTER TABLE audit_logs SET (timescaledb.compress, timescaledb.compress_segmentby = 'action_category', timescaledb.compress_orderby = 'event_time DESC');
SELECT add_compression_policy('audit_logs', INTERVAL '30 days', if_not_exists => TRUE);
SELECT add_retention_policy('audit_logs', INTERVAL '5 years', if_not_exists => TRUE);

ALTER TABLE market_events SET (timescaledb.compress, timescaledb.compress_segmentby = 'event_category', timescaledb.compress_orderby = 'observation_time DESC');
SELECT add_compression_policy('market_events', INTERVAL '1 year', if_not_exists => TRUE);

-- ==================== 7. 种子数据 ====================

INSERT INTO assets (symbol, name, asset_type, chain, decimals) VALUES
    ('BTC', 'Bitcoin', 'CRYPTO', 'bitcoin', 8),
    ('USD', 'US Dollar', 'FIAT', NULL, 2),
    ('USDT', 'Tether', 'CRYPTO', 'ethereum', 6)
ON CONFLICT (symbol) DO NOTHING;
