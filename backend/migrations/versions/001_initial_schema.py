"""Initial schema - all 52 tables

Revision ID: 001
Revises:
Create Date: 2026-09-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---- ENUM 类型 ----
provider_category = sa.Enum(
    "MARKET", "ONCHAIN", "EXCHANGE_FLOW", "ETF", "DERIVATIVES",
    "OPTIONS", "MACRO", "SENTIMENT", "NEWS",
    name="provider_category", create_type=True,
)
provider_status = sa.Enum(
    "ONLINE", "DEGRADED", "SLOW", "RATE_LIMITED", "AUTH_ERROR",
    "NETWORK_ERROR", "DATA_ERROR", "OFFLINE", "DISABLED",
    name="provider_status", create_type=True,
)
failover_resolution = sa.Enum(
    "AUTO_RECOVERED", "MANUAL_RESET", "TIMEOUT", "PERMANENTLY_DISABLED",
    name="failover_resolution", create_type=True,
)
quality_status_type = sa.Enum(
    "VERIFIED", "ESTIMATED", "STALE", "CONFLICT", "INVALID",
    name="quality_status_type", create_type=True,
)
candle_interval = sa.Enum(
    "1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w",
    name="candle_interval", create_type=True,
)
cycle_phase = sa.Enum(
    "DEEP_BEAR", "BEAR", "BOTTOM_BUILDING", "RECOVERY", "UPTREND",
    "ACCELERATION", "DISTRIBUTION", "TOP_RISK", "DECLINE",
    name="cycle_phase", create_type=True,
)
valuation_level = sa.Enum(
    "DEEP_UNDERVALUED", "UNDERVALUED", "FAIR", "OVERVALUED", "EXTREME_OVERVALUED",
    name="valuation_level", create_type=True,
)
risk_level = sa.Enum(
    "VERY_LOW", "LOW", "MODERATE", "HIGH", "VERY_HIGH", "EXTREME",
    name="risk_level", create_type=True,
)
signal_direction = sa.Enum("BULLISH", "BEARISH", "NEUTRAL", name="signal_direction", create_type=True)
job_status = sa.Enum(
    "PENDING", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED", "SKIPPED",
    name="job_status", create_type=True,
)
sync_status = sa.Enum(
    "IDLE", "SYNCING", "PAUSED", "COMPLETED", "FAILED", "RETRY",
    name="sync_status", create_type=True,
)
user_role = sa.Enum("ADMIN", "ANALYST", "USER", name="user_role", create_type=True)
trade_side = sa.Enum("BUY", "SELL", name="trade_side", create_type=True)
plan_status = sa.Enum("ACTIVE", "PAUSED", "COMPLETED", "CANCELLED", name="plan_status", create_type=True)
model_status = sa.Enum(
    "DRAFT", "TRAINING", "VALIDATED", "DEPLOYED", "DEPRECATED", "REJECTED",
    name="model_status", create_type=True,
)
backtest_status = sa.Enum(
    "PENDING", "RUNNING", "COMPLETED", "FAILED", "CANCELLED",
    name="backtest_status", create_type=True,
)


def upgrade() -> None:
    # ---- 创建 ENUM 类型 ----
    provider_category.create(op.get_bind(), checkfirst=True)
    provider_status.create(op.get_bind(), checkfirst=True)
    failover_resolution.create(op.get_bind(), checkfirst=True)
    quality_status_type.create(op.get_bind(), checkfirst=True)
    candle_interval.create(op.get_bind(), checkfirst=True)
    cycle_phase.create(op.get_bind(), checkfirst=True)
    valuation_level.create(op.get_bind(), checkfirst=True)
    risk_level.create(op.get_bind(), checkfirst=True)
    signal_direction.create(op.get_bind(), checkfirst=True)
    job_status.create(op.get_bind(), checkfirst=True)
    sync_status.create(op.get_bind(), checkfirst=True)
    user_role.create(op.get_bind(), checkfirst=True)
    trade_side.create(op.get_bind(), checkfirst=True)
    plan_status.create(op.get_bind(), checkfirst=True)
    model_status.create(op.get_bind(), checkfirst=True)
    backtest_status.create(op.get_bind(), checkfirst=True)

    # ---- 触发器函数 ----
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION trigger_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    # ======== Group 1: assets (被其他表引用) ========
    op.create_table(
        "assets",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("symbol", sa.String(20), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("asset_type", sa.String(30), nullable=False, server_default="CRYPTO"),
        sa.Column("chain", sa.String(30), server_default="bitcoin"),
        sa.Column("decimals", sa.SmallInteger, nullable=False, server_default="8"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="资产定义字典，系统支持的所有资产元数据",
    )
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON assets FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== Group 12: users (被其他表引用) ========
    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False, unique=True),
        sa.Column("email", sa.String(200), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(100)),
        sa.Column("role", user_role, nullable=False, server_default="USER"),
        sa.Column("preferences", JSONB, nullable=False, server_default='{"language":"zh-CN","theme":"dark","mode":"simple"}'),
        sa.Column("notification_config", JSONB, server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("login_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="用户账户表",
    )
    op.create_index("idx_users_email", "users", ["email"])
    op.create_index("idx_users_role", "users", ["role"], postgresql_where=sa.text("is_active = true"))
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== Group 1: providers ========
    op.create_table(
        "providers",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False, unique=True),
        sa.Column("category", provider_category, nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("api_key_encrypted", sa.Text),
        sa.Column("proxy_config", JSONB, server_default='{"type": "direct"}'),
        sa.Column("timeout_config", JSONB, server_default='{"connect": 5000, "read": 30000, "write": 10000}'),
        sa.Column("retry_config", JSONB, server_default='{"max_retries": 3, "backoff_factor": 2, "backoff_max": 60}'),
        sa.Column("rate_limit", sa.Integer, server_default="60"),
        sa.Column("rate_limit_window", sa.Integer, server_default="60"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_locked", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("status", provider_status, nullable=False, server_default="OFFLINE"),
        sa.Column("health_score", sa.Numeric(5, 2), server_default="0"),
        sa.Column("recovery_threshold", sa.Integer, server_default="3"),
        sa.Column("failure_threshold", sa.Integer, server_default="5"),
        sa.Column("description", sa.Text),
        sa.Column("supported_symbols", ARRAY(sa.Text), server_default="ARRAY['BTC']"),
        sa.Column("supported_intervals", ARRAY(sa.Text), server_default="{}"),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="Provider 数据源注册表",
    )
    op.create_index("idx_providers_category", "providers", ["category"], postgresql_where=sa.text("is_enabled = true"))
    op.create_index("idx_providers_priority", "providers", ["category", "priority"], postgresql_where=sa.text("is_enabled = true"))
    op.create_index("idx_providers_status", "providers", ["status"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON providers FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== provider_health ========
    op.create_table(
        "provider_health",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("check_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("status", provider_status, nullable=False),
        sa.Column("response_time_ms", sa.Integer),
        sa.Column("success_rate_1h", sa.Numeric(5, 2)),
        sa.Column("success_rate_24h", sa.Numeric(5, 2)),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("today_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("today_requests", sa.Integer, nullable=False, server_default="0"),
        sa.Column("data_latency_ms", sa.Integer),
        sa.Column("last_error", sa.Text),
        sa.Column("last_error_type", sa.String(50)),
        sa.Column("http_status", sa.Integer),
        sa.Column("is_rate_limited", sa.Boolean, server_default="false"),
        sa.Column("metrics", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "check_time"),
        comment="Provider 健康状态快照",
    )
    op.create_index("idx_provider_health_pid_time", "provider_health", ["provider_id", "check_time"])

    # ======== provider_scores ========
    op.create_table(
        "provider_scores",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("accuracy_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("latency_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("stability_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("completeness_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("consistency_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("overall_score", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("rank", sa.Integer),
        sa.Column("score_details", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "scored_at"),
        comment="Provider 综合评分历史",
    )
    op.create_index("idx_provider_scores_pid", "provider_scores", ["provider_id", "scored_at"])
    op.create_index("idx_provider_scores_rank", "provider_scores", ["scored_at", "rank"])

    # ======== provider_failover_events ========
    op.create_table(
        "provider_failover_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("from_provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("to_provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("data_category", provider_category, nullable=False),
        sa.Column("symbol", sa.String(20), server_default="BTC"),
        sa.Column("trigger_reason", sa.String(50), nullable=False),
        sa.Column("error_message", sa.Text),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_type", failover_resolution),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("requests_affected", sa.Integer, server_default="0"),
        sa.Column("context", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "occurred_at"),
        comment="Provider 故障切换事件记录",
    )
    op.create_index("idx_failover_from", "provider_failover_events", ["from_provider_id", "occurred_at"])
    op.create_index("idx_failover_category", "provider_failover_events", ["data_category", "occurred_at"])

    # ======== provider_requests ========
    op.create_table(
        "provider_requests",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("method", sa.String(10), nullable=False, server_default="GET"),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("endpoint", sa.String(200)),
        sa.Column("status_code", sa.Integer),
        sa.Column("response_time_ms", sa.Integer),
        sa.Column("error_type", sa.String(50)),
        sa.Column("error_message", sa.Text),
        sa.Column("request_params", JSONB, server_default="{}"),
        sa.Column("response_size_bytes", sa.Integer),
        sa.Column("is_success", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_sampled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("data_category", provider_category),
        sa.PrimaryKeyConstraint("id", "request_time"),
        comment="Provider 请求日志（采样存储）",
    )
    op.create_index("idx_requests_provider", "provider_requests", ["provider_id", "request_time"])
    op.create_index("idx_requests_failed", "provider_requests", ["provider_id", "request_time"], postgresql_where=sa.text("is_success = false"))

    # ======== Group 9: indicator_definitions (被 indicator_values 引用) ========
    op.create_table(
        "indicator_definitions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("code", sa.String(50), nullable=False, unique=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("name_cn", sa.String(100), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("sub_category", sa.String(50)),
        sa.Column("description", sa.Text),
        sa.Column("description_cn", sa.Text),
        sa.Column("formula", sa.Text),
        sa.Column("formula_latex", sa.Text),
        sa.Column("unit", sa.String(30), server_default="value"),
        sa.Column("value_range", sa.String(50)),
        sa.Column("frequency", sa.String(20), nullable=False, server_default="1d"),
        sa.Column("params_schema", JSONB, nullable=False, server_default="{}"),
        sa.Column("default_params", JSONB, nullable=False, server_default="{}"),
        sa.Column("display_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("interpretation", JSONB, server_default="{}"),
        sa.Column("data_dependencies", ARRAY(sa.Text), server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("sort_order", sa.Integer, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="指标定义字典",
    )
    op.create_index("idx_indicator_def_category", "indicator_definitions", ["category", "is_active"])
    op.create_index("idx_indicator_def_primary", "indicator_definitions", ["sort_order"], postgresql_where=sa.text("is_primary = true"))
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON indicator_definitions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== Group 10: model_versions ========
    op.create_table(
        "model_versions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("model_name", sa.String(100), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("model_type", sa.String(50), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("description_cn", sa.Text),
        sa.Column("architecture", JSONB, nullable=False, server_default="{}"),
        sa.Column("hyperparameters", JSONB, nullable=False, server_default="{}"),
        sa.Column("training_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("features_used", ARRAY(sa.Text), server_default="{}"),
        sa.Column("train_start_date", sa.Date, nullable=False),
        sa.Column("train_end_date", sa.Date, nullable=False),
        sa.Column("test_start_date", sa.Date),
        sa.Column("test_end_date", sa.Date),
        sa.Column("validation_type", sa.String(30), server_default="WALK_FORWARD"),
        sa.Column("status", model_status, nullable=False, server_default="DRAFT"),
        sa.Column("is_production", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("performance_metrics", JSONB, server_default="{}"),
        sa.Column("known_limitations", sa.Text),
        sa.Column("effective_conditions", sa.Text),
        sa.Column("parent_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("changelog", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("model_name", "version", name="uq_model_versions_name_version"),
        comment="模型版本注册表",
    )
    op.create_index("idx_model_versions_name", "model_versions", ["model_name", "status"])
    op.create_index("idx_model_versions_production", "model_versions", ["model_name"], postgresql_where=sa.text("is_production = true"))
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON model_versions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== Group 11: strategies → strategy_versions ========
    op.create_table(
        "strategies",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("name_cn", sa.String(100)),
        sa.Column("category", sa.String(50), nullable=False, server_default="DCA"),
        sa.Column("description", sa.Text),
        sa.Column("description_cn", sa.Text),
        sa.Column("parameters_schema", JSONB, nullable=False, server_default="{}"),
        sa.Column("rules_description", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("is_system", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("tags", ARRAY(sa.Text), server_default="{}"),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="策略定义表",
    )
    op.create_index("idx_strategies_category", "strategies", ["category"], postgresql_where=sa.text("is_active = true"))
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON strategies FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "strategy_versions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("strategy_id", UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("parameters", JSONB, nullable=False, server_default="{}"),
        sa.Column("rules_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("changelog", sa.Text),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_sid_version"),
        comment="策略版本管理",
    )
    op.create_index("idx_strategy_versions_sid", "strategy_versions", ["strategy_id", "created_at"])
    op.create_index("idx_strategy_versions_current", "strategy_versions", ["strategy_id"], postgresql_where=sa.text("is_current = true"))

    # ======== Group 2: raw_* 表 ========
    _raw_tables = [
        ("raw_market_data", [
            ("symbol", sa.String(20), {"nullable": False, "server_default": "BTC"}),
            ("data_type", sa.String(50), {"nullable": False}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
            ("response_headers", JSONB, {"server_default": "{}"}),
            ("response_size_bytes", sa.Integer, {}),
        ], "1 day"),
        ("raw_onchain_data", [
            ("metric_name", sa.String(100), {"nullable": False}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
        ], "7 days"),
        ("raw_etf_data", [
            ("ticker", sa.String(20), {"nullable": False}),
            ("data_type", sa.String(50), {"nullable": False, "server_default": "FLOW"}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
        ], "30 days"),
        ("raw_derivatives_data", [
            ("symbol", sa.String(30), {"nullable": False, "server_default": "BTC"}),
            ("exchange", sa.String(50), {"nullable": False}),
            ("data_type", sa.String(50), {"nullable": False}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
        ], "1 day"),
        ("raw_macro_data", [
            ("series_id", sa.String(100), {"nullable": False}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
        ], "30 days"),
        ("raw_sentiment_data", [
            ("source_type", sa.String(50), {"nullable": False}),
            ("raw_response", JSONB, {"nullable": False}),
            ("endpoint", sa.Text, {"nullable": False}),
            ("request_params", JSONB, {"server_default": "{}"}),
        ], "7 days"),
    ]
    for tbl_name, extra_cols, _chunk in _raw_tables:
        cols = [
            sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
            sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
            sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        ]
        for col_name, col_type, col_kw in extra_cols:
            cols.append(sa.Column(col_name, col_type, **col_kw))
        cols.extend([
            sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.PrimaryKeyConstraint("id", "fetch_time"),
        ])
        op.create_table(tbl_name, *cols, comment=f"{tbl_name} 原始数据存储")
        op.create_index(f"idx_{tbl_name}_source", tbl_name, ["source_id", "fetch_time"])

    # ======== Group 3: market_prices, candles, orderbooks, trades ========
    op.create_table(
        "market_prices",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTCUSDT"),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("bid", sa.Numeric(20, 8)),
        sa.Column("ask", sa.Numeric(20, 8)),
        sa.Column("spread", sa.Numeric(20, 8)),
        sa.Column("volume_24h", sa.Numeric(24, 4)),
        sa.Column("quote_volume_24h", sa.Numeric(24, 4)),
        sa.Column("market_cap", sa.Numeric(24, 2)),
        sa.Column("high_24h", sa.Numeric(20, 8)),
        sa.Column("low_24h", sa.Numeric(20, 8)),
        sa.Column("price_change_pct_24h", sa.Numeric(8, 4)),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("cross_validated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("validation_sources", sa.Integer, server_default="1"),
        sa.Column("deviation_pct", sa.Numeric(8, 6)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="标准化市场价格数据（核心表）",
    )
    op.create_index("idx_prices_symbol_time", "market_prices", ["symbol", "observation_time"])
    op.create_index("idx_prices_source", "market_prices", ["source_id", "observation_time"])
    op.create_index("idx_prices_quality", "market_prices", ["observation_time"], postgresql_where=sa.text("quality_status != 'VERIFIED'"))

    op.create_table(
        "candles",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTCUSDT"),
        sa.Column("interval", candle_interval, nullable=False),
        sa.Column("open", sa.Numeric(20, 8), nullable=False),
        sa.Column("high", sa.Numeric(20, 8), nullable=False),
        sa.Column("low", sa.Numeric(20, 8), nullable=False),
        sa.Column("close", sa.Numeric(20, 8), nullable=False),
        sa.Column("volume", sa.Numeric(24, 4), nullable=False, server_default="0"),
        sa.Column("quote_volume", sa.Numeric(24, 4)),
        sa.Column("trades", sa.Integer),
        sa.Column("taker_buy_volume", sa.Numeric(24, 4)),
        sa.Column("taker_sell_volume", sa.Numeric(24, 4)),
        sa.Column("vwap", sa.Numeric(20, 8)),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="OHLCV K线数据",
    )
    op.create_index("idx_candles_unique", "candles", ["symbol", "interval", "observation_time"], unique=True)
    op.create_index("idx_candles_symbol_interval", "candles", ["symbol", "interval", "observation_time"])
    op.create_index("idx_candles_source", "candles", ["source_id", "observation_time"])

    op.create_table(
        "orderbooks",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTCUSDT"),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("bids", JSONB, nullable=False, server_default="[]"),
        sa.Column("asks", JSONB, nullable=False, server_default="[]"),
        sa.Column("bid_depth", sa.Numeric(24, 4)),
        sa.Column("ask_depth", sa.Numeric(24, 4)),
        sa.Column("spread", sa.Numeric(20, 8)),
        sa.Column("spread_pct", sa.Numeric(10, 6)),
        sa.Column("mid_price", sa.Numeric(20, 8)),
        sa.Column("imbalance", sa.Numeric(10, 6)),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="订单簿深度快照",
    )
    op.create_index("idx_orderbooks_symbol", "orderbooks", ["symbol", "exchange", "observation_time"])

    op.create_table(
        "trades",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTCUSDT"),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("trade_id", sa.String(50), nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False),
        sa.Column("quote_quantity", sa.Numeric(24, 4)),
        sa.Column("side", trade_side, nullable=False),
        sa.Column("is_buyer_maker", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="逐笔成交数据",
    )
    op.create_index("idx_trades_symbol", "trades", ["symbol", "exchange", "observation_time"])

    # ======== Group 4: onchain_metrics, exchange_flows, address_metrics, supply_metrics ========
    op.create_table(
        "onchain_metrics",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("unit", sa.String(30), server_default="ratio"),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="链上指标时序数据",
    )
    op.create_index("idx_onchain_unique", "onchain_metrics", ["metric_name", "observation_time", "source_id"], unique=True)
    op.create_index("idx_onchain_metric", "onchain_metrics", ["metric_name", "observation_time"])
    op.create_index("idx_onchain_source", "onchain_metrics", ["source_id", "observation_time"])

    op.create_table(
        "exchange_flows",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("exchange_name", sa.String(100), nullable=False),
        sa.Column("flow_type", sa.String(20), nullable=False, server_default="NET"),
        sa.Column("inflow_btc", sa.Numeric(20, 8)),
        sa.Column("outflow_btc", sa.Numeric(20, 8)),
        sa.Column("net_flow_btc", sa.Numeric(20, 8)),
        sa.Column("inflow_usd", sa.Numeric(20, 2)),
        sa.Column("outflow_usd", sa.Numeric(20, 2)),
        sa.Column("net_flow_usd", sa.Numeric(20, 2)),
        sa.Column("exchange_balance", sa.Numeric(20, 8)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="交易所 BTC 资金流数据",
    )
    op.create_index("idx_exchange_flows_name", "exchange_flows", ["exchange_name", "observation_time"])
    op.create_index("idx_exchange_flows_type", "exchange_flows", ["flow_type", "observation_time"])

    op.create_table(
        "address_metrics",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("cohort", sa.String(50)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="地址活跃指标",
    )
    op.create_index("idx_address_metrics_unique", "address_metrics", ["metric_name", "cohort", "observation_time"], unique=True)
    op.create_index("idx_address_metrics_name", "address_metrics", ["metric_name", "observation_time"])

    op.create_table(
        "supply_metrics",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("supply_cohort", sa.String(50), nullable=False, server_default="ALL"),
        sa.Column("total_supply", sa.Numeric(20, 8)),
        sa.Column("pct_of_supply", sa.Numeric(8, 4)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="BTC 供应分布指标",
    )
    op.create_index("idx_supply_metrics_unique", "supply_metrics", ["metric_name", "supply_cohort", "observation_time"], unique=True)
    op.create_index("idx_supply_metrics_name", "supply_metrics", ["metric_name", "observation_time"])

    # ======== Group 5: etf_flows, etf_holdings ========
    op.create_table(
        "etf_flows",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("fund_name", sa.String(100)),
        sa.Column("daily_inflow_usd", sa.Numeric(20, 2)),
        sa.Column("daily_outflow_usd", sa.Numeric(20, 2)),
        sa.Column("net_flow_usd", sa.Numeric(20, 2)),
        sa.Column("cumulative_flow_usd", sa.Numeric(20, 2)),
        sa.Column("total_holdings_btc", sa.Numeric(20, 8)),
        sa.Column("total_aum_usd", sa.Numeric(20, 2)),
        sa.Column("daily_volume_usd", sa.Numeric(20, 2)),
        sa.Column("price", sa.Numeric(12, 4)),
        sa.Column("nav", sa.Numeric(12, 4)),
        sa.Column("premium_discount", sa.Numeric(8, 4)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="BTC ETF 每日资金流数据",
    )
    op.create_index("idx_etf_flows_unique", "etf_flows", ["ticker", "observation_time"], unique=True)
    op.create_index("idx_etf_flows_ticker", "etf_flows", ["ticker", "observation_time"])
    op.create_index("idx_etf_flows_time", "etf_flows", ["observation_time"])

    op.create_table(
        "etf_holdings",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("ticker", sa.String(20), nullable=False),
        sa.Column("fund_name", sa.String(100)),
        sa.Column("holdings_btc", sa.Numeric(20, 8)),
        sa.Column("holdings_usd", sa.Numeric(20, 2)),
        sa.Column("shares_outstanding", sa.Numeric(20, 2)),
        sa.Column("nav_per_share", sa.Numeric(12, 4)),
        sa.Column("market_price", sa.Numeric(12, 4)),
        sa.Column("premium_discount", sa.Numeric(8, 4)),
        sa.Column("daily_change_btc", sa.Numeric(20, 8)),
        sa.Column("custodian", sa.String(100)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="BTC ETF 持仓快照",
    )
    op.create_index("idx_etf_holdings_unique", "etf_holdings", ["ticker", "observation_time"], unique=True)
    op.create_index("idx_etf_holdings_ticker", "etf_holdings", ["ticker", "observation_time"])

    # ======== Group 6: derivatives, options_data ========
    op.create_table(
        "derivatives",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(30), nullable=False, server_default="BTCUSDT"),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("data_type", sa.String(30), nullable=False),
        sa.Column("funding_rate", sa.Numeric(12, 10)),
        sa.Column("funding_rate_next", sa.Numeric(12, 10)),
        sa.Column("funding_time", sa.DateTime(timezone=True)),
        sa.Column("open_interest", sa.Numeric(20, 8)),
        sa.Column("open_interest_usd", sa.Numeric(24, 2)),
        sa.Column("open_interest_change", sa.Numeric(12, 6)),
        sa.Column("long_short_ratio", sa.Numeric(10, 6)),
        sa.Column("long_account_ratio", sa.Numeric(10, 6)),
        sa.Column("liquidation_long_usd", sa.Numeric(20, 2)),
        sa.Column("liquidation_short_usd", sa.Numeric(20, 2)),
        sa.Column("liquidation_total_usd", sa.Numeric(20, 2)),
        sa.Column("liquidation_count", sa.Integer),
        sa.Column("basis", sa.Numeric(12, 6)),
        sa.Column("basis_pct", sa.Numeric(10, 6)),
        sa.Column("premium_index", sa.Numeric(12, 8)),
        sa.Column("cvd", sa.Numeric(24, 4)),
        sa.Column("taker_buy_volume", sa.Numeric(24, 4)),
        sa.Column("taker_sell_volume", sa.Numeric(24, 4)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="衍生品综合数据",
    )
    op.create_index("idx_derivatives_type", "derivatives", ["data_type", "symbol", "observation_time"])
    op.create_index("idx_derivatives_exchange", "derivatives", ["exchange", "symbol", "observation_time"])
    op.create_index("idx_derivatives_symbol", "derivatives", ["symbol", "observation_time"])

    op.create_table(
        "options_data",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("exchange", sa.String(50), nullable=False),
        sa.Column("underlying", sa.String(20), nullable=False, server_default="BTC"),
        sa.Column("expiry_date", sa.Date, nullable=False),
        sa.Column("strike_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("option_type", sa.String(4), nullable=False),
        sa.Column("open_interest", sa.Numeric(20, 4)),
        sa.Column("volume", sa.Numeric(20, 4)),
        sa.Column("last_price", sa.Numeric(12, 4)),
        sa.Column("bid_price", sa.Numeric(12, 4)),
        sa.Column("ask_price", sa.Numeric(12, 4)),
        sa.Column("implied_volatility", sa.Numeric(10, 6)),
        sa.Column("delta", sa.Numeric(10, 6)),
        sa.Column("gamma", sa.Numeric(12, 8)),
        sa.Column("theta", sa.Numeric(12, 8)),
        sa.Column("vega", sa.Numeric(12, 6)),
        sa.Column("rho", sa.Numeric(12, 6)),
        sa.Column("put_call_ratio", sa.Numeric(10, 6)),
        sa.Column("iv_skew", sa.Numeric(10, 6)),
        sa.Column("total_oi_usd", sa.Numeric(20, 2)),
        sa.Column("max_pain", sa.Numeric(12, 2)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        sa.CheckConstraint("option_type IN ('CALL', 'PUT')", name="ck_options_type"),
        comment="期权链数据",
    )
    op.create_index("idx_options_unique", "options_data", ["exchange", "expiry_date", "strike_price", "option_type", "observation_time"], unique=True)
    op.create_index("idx_options_expiry", "options_data", ["expiry_date", "observation_time"])
    op.create_index("idx_options_exchange", "options_data", ["exchange", "observation_time"])

    # ======== Group 7: macro_series, macro_events ========
    op.create_table(
        "macro_series",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("series_id", sa.String(100), nullable=False),
        sa.Column("series_name", sa.String(200), nullable=False),
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("unit", sa.String(30), nullable=False, server_default="index"),
        sa.Column("frequency", sa.String(20), nullable=False, server_default="MONTHLY"),
        sa.Column("observation_date", sa.Date, nullable=False),
        sa.Column("release_date", sa.Date, nullable=False),
        sa.Column("revision_date", sa.Date),
        sa.Column("previous_value", sa.Numeric(30, 10)),
        sa.Column("revised_value", sa.Numeric(30, 10)),
        sa.Column("is_revised", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("revision_number", sa.Integer, nullable=False, server_default="0"),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="宏观经济指标时序数据",
    )
    op.create_index("idx_macro_unique", "macro_series", ["series_id", "observation_date", "revision_number"], unique=True)
    op.create_index("idx_macro_series", "macro_series", ["series_id", "observation_time"])
    op.create_index("idx_macro_release", "macro_series", ["release_date", "series_id"])
    op.create_index("idx_macro_revised", "macro_series", ["series_id", "observation_time"], postgresql_where=sa.text("is_revised = true"))

    op.create_table(
        "macro_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("event_name", sa.String(200), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("importance", sa.String(10), nullable=False, server_default="MEDIUM"),
        sa.Column("actual_value", sa.String(50)),
        sa.Column("forecast_value", sa.String(50)),
        sa.Column("previous_value", sa.String(50)),
        sa.Column("description", sa.Text),
        sa.Column("market_impact", JSONB, server_default="{}"),
        sa.Column("btc_price_at_event", sa.Numeric(20, 8)),
        sa.Column("btc_price_1h_after", sa.Numeric(20, 8)),
        sa.Column("btc_price_24h_after", sa.Numeric(20, 8)),
        sa.Column("is_recurring", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("recurring_pattern", sa.String(100)),
        sa.Column("source_url", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="宏观经济事件日历",
    )
    op.create_index("idx_macro_events_time", "macro_events", ["event_time"])
    op.create_index("idx_macro_events_type", "macro_events", ["event_type", "event_time"])
    op.create_index("idx_macro_events_importance", "macro_events", ["importance", "event_time"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON macro_events FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== Group 8: sentiment ========
    op.create_table(
        "sentiment",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_type", sa.String(50), nullable=False),
        sa.Column("metric_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Numeric(12, 6), nullable=False),
        sa.Column("normalized_value", sa.Numeric(8, 4)),
        sa.Column("sentiment_label", sa.String(30)),
        sa.Column("sample_size", sa.Integer),
        sa.Column("confidence", sa.Numeric(5, 4)),
        sa.Column("previous_value", sa.Numeric(12, 6)),
        sa.Column("change_pct", sa.Numeric(10, 6)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="市场情绪指标时序数据",
    )
    op.create_index("idx_sentiment_unique", "sentiment", ["source_type", "metric_name", "observation_time"], unique=True)
    op.create_index("idx_sentiment_source", "sentiment", ["source_type", "observation_time"])
    op.create_index("idx_sentiment_metric", "sentiment", ["metric_name", "observation_time"])
    op.create_index("idx_sentiment_label", "sentiment", ["sentiment_label", "observation_time"])

    # ======== Group 9: indicator_values, cycle_states, valuation_states, risk_scores, market_regimes, signals ========
    op.create_table(
        "indicator_values",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("indicator_id", UUID(as_uuid=True), sa.ForeignKey("indicator_definitions.id"), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fetch_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("symbol", sa.String(20), nullable=False, server_default="BTC"),
        sa.Column("value", sa.Numeric(30, 10), nullable=False),
        sa.Column("normalized_value", sa.Numeric(12, 6)),
        sa.Column("percentile", sa.Numeric(8, 4)),
        sa.Column("signal", sa.String(20)),
        sa.Column("params_used", JSONB, nullable=False, server_default="{}"),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="指标计算结果时序数据",
    )
    op.create_index("idx_indicator_values_unique", "indicator_values", ["indicator_id", "symbol", "observation_time"], unique=True)
    op.create_index("idx_indicator_values_ind", "indicator_values", ["indicator_id", "observation_time"])
    op.create_index("idx_indicator_values_symbol", "indicator_values", ["symbol", "observation_time"])

    op.create_table(
        "cycle_states",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", cycle_phase, nullable=False),
        sa.Column("previous_phase", cycle_phase),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("phase_duration_days", sa.Integer),
        sa.Column("evidence_for", JSONB, nullable=False, server_default="[]"),
        sa.Column("evidence_against", JSONB, nullable=False, server_default="[]"),
        sa.Column("dimension_scores", JSONB, nullable=False, server_default="{}"),
        sa.Column("historical_similar", JSONB, server_default="[]"),
        sa.Column("model_version_id", UUID(as_uuid=True)),
        sa.Column("description", sa.Text),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="市场周期状态",
    )
    op.create_index("idx_cycle_phase", "cycle_states", ["phase", "observation_time"])
    op.create_index("idx_cycle_time", "cycle_states", ["observation_time"])

    op.create_table(
        "valuation_states",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valuation_level", valuation_level, nullable=False),
        sa.Column("previous_level", valuation_level),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("mvrv_value", sa.Numeric(12, 6)),
        sa.Column("mvrv_percentile", sa.Numeric(8, 4)),
        sa.Column("realized_cap", sa.Numeric(24, 2)),
        sa.Column("realized_price", sa.Numeric(20, 8)),
        sa.Column("cost_basis", sa.Numeric(20, 8)),
        sa.Column("nupl_value", sa.Numeric(12, 6)),
        sa.Column("nupl_percentile", sa.Numeric(8, 4)),
        sa.Column("overall_score", sa.Numeric(8, 4)),
        sa.Column("components", JSONB, nullable=False, server_default="{}"),
        sa.Column("evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("historical_context", JSONB, server_default="{}"),
        sa.Column("model_version_id", UUID(as_uuid=True)),
        sa.Column("description", sa.Text),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="估值状态",
    )
    op.create_index("idx_valuation_level", "valuation_states", ["valuation_level", "observation_time"])
    op.create_index("idx_valuation_time", "valuation_states", ["observation_time"])

    op.create_table(
        "risk_scores",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overall_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("trend_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("valuation_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("leverage_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("liquidity_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("macro_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("onchain_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("volatility_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("crowding_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("drawdown_risk", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("risk_level", risk_level, nullable=False, server_default="MODERATE"),
        sa.Column("previous_level", risk_level),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("risk_factors", JSONB, nullable=False, server_default="[]"),
        sa.Column("evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("warnings", JSONB, server_default="[]"),
        sa.Column("model_version_id", UUID(as_uuid=True)),
        sa.Column("description", sa.Text),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="风险评分",
    )
    op.create_index("idx_risk_level", "risk_scores", ["risk_level", "observation_time"])
    op.create_index("idx_risk_time", "risk_scores", ["observation_time"])
    op.create_index("idx_risk_high", "risk_scores", ["observation_time"], postgresql_where=sa.text("overall_risk >= 70"))

    op.create_table(
        "market_regimes",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trend_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("valuation_state", valuation_level, nullable=False, server_default="FAIR"),
        sa.Column("capital_flow_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("onchain_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("derivative_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("macro_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("sentiment_state", sa.String(30), nullable=False, server_default="NEUTRAL"),
        sa.Column("risk_state", risk_level, nullable=False, server_default="MODERATE"),
        sa.Column("cycle_state", cycle_phase, nullable=False, server_default="UPTREND"),
        sa.Column("overall_regime", sa.String(50), nullable=False, server_default="NEUTRAL"),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("regime_score", sa.Numeric(8, 4)),
        sa.Column("is_changed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("previous_regime", sa.String(50)),
        sa.Column("change_reason", JSONB, server_default="{}"),
        sa.Column("dimension_details", JSONB, nullable=False, server_default="{}"),
        sa.Column("evidence_summary", JSONB, server_default="{}"),
        sa.Column("model_version_id", UUID(as_uuid=True)),
        sa.Column("description", sa.Text),
        sa.Column("quality_status", quality_status_type, nullable=False, server_default="VERIFIED"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="综合市场状态",
    )
    op.create_index("idx_regimes_overall", "market_regimes", ["overall_regime", "observation_time"])
    op.create_index("idx_regimes_changed", "market_regimes", ["observation_time"], postgresql_where=sa.text("is_changed = true"))
    op.create_index("idx_regimes_time", "market_regimes", ["observation_time"])

    op.create_table(
        "signals",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("signal_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("signal_type", sa.String(50), nullable=False),
        sa.Column("signal_direction", signal_direction, nullable=False),
        sa.Column("strength", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("engine_source", sa.String(50), nullable=False),
        sa.Column("trigger_conditions", JSONB, nullable=False, server_default="[]"),
        sa.Column("evidence", JSONB, nullable=False, server_default="[]"),
        sa.Column("market_context", JSONB, server_default="{}"),
        sa.Column("btc_price", sa.Numeric(20, 8)),
        sa.Column("regime_at_signal", sa.String(50)),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("description_cn", sa.Text),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("model_version_id", UUID(as_uuid=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "signal_time"),
        comment="信号事件记录",
    )
    op.create_index("idx_signals_type", "signals", ["signal_type", "signal_time"])
    op.create_index("idx_signals_direction", "signals", ["signal_direction", "signal_time"])
    op.create_index("idx_signals_active", "signals", ["signal_time"], postgresql_where=sa.text("is_active = true"))
    op.create_index("idx_signals_category", "signals", ["category", "signal_time"])

    # ======== Group 10: model_weights, model_validations ========
    op.create_table(
        "model_weights",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("weight_name", sa.String(200), nullable=False),
        sa.Column("weight_type", sa.String(30), nullable=False, server_default="PARAMETER"),
        sa.Column("weight_blob", sa.LargeBinary),
        sa.Column("storage_path", sa.String(500)),
        sa.Column("file_size_bytes", sa.BigInteger),
        sa.Column("checksum_sha256", sa.String(64)),
        sa.Column("weight_metadata", JSONB, server_default="{}"),
        sa.Column("shape", ARRAY(sa.Integer), server_default="{}"),
        sa.Column("dtype", sa.String(20), server_default="float64"),
        sa.Column("description", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("model_version_id", "weight_name", name="uq_model_weights_version_name"),
        comment="模型参数/权重存储",
    )
    op.create_index("idx_model_weights_version", "model_weights", ["model_version_id"])

    op.create_table(
        "model_validations",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("validation_type", sa.String(30), nullable=False),
        sa.Column("validated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("period_description", sa.String(200)),
        sa.Column("metrics", JSONB, nullable=False, server_default="{}"),
        sa.Column("sharpe_ratio", sa.Numeric(8, 4)),
        sa.Column("sortino_ratio", sa.Numeric(8, 4)),
        sa.Column("max_drawdown", sa.Numeric(8, 4)),
        sa.Column("win_rate", sa.Numeric(8, 4)),
        sa.Column("profit_factor", sa.Numeric(8, 4)),
        sa.Column("accuracy", sa.Numeric(8, 4)),
        sa.Column("precision_score", sa.Numeric(8, 4)),
        sa.Column("recall_score", sa.Numeric(8, 4)),
        sa.Column("f1_score", sa.Numeric(8, 4)),
        sa.Column("result_status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("passed", sa.Boolean),
        sa.Column("notes", sa.Text),
        sa.Column("data_snapshot_time", sa.DateTime(timezone=True)),
        sa.Column("data_version", sa.String(50)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="模型验证结果记录",
    )
    op.create_index("idx_model_valid_version", "model_validations", ["model_version_id", "validated_at"])
    op.create_index("idx_model_valid_type", "model_validations", ["validation_type", "result_status"])

    # ======== Group 11: backtest_runs, backtest_results, backtest_trades ========
    op.create_table(
        "backtest_runs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("strategy_version_id", UUID(as_uuid=True), sa.ForeignKey("strategy_versions.id"), nullable=False),
        sa.Column("model_version_id", UUID(as_uuid=True), sa.ForeignKey("model_versions.id")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("status", backtest_status, nullable=False, server_default="PENDING"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_seconds", sa.Integer),
        sa.Column("error_message", sa.Text),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("period_end", sa.Date, nullable=False),
        sa.Column("run_params", JSONB, nullable=False, server_default="{}"),
        sa.Column("initial_conditions", JSONB, nullable=False, server_default="{}"),
        sa.Column("total_return", sa.Numeric(12, 6)),
        sa.Column("annual_return", sa.Numeric(12, 6)),
        sa.Column("max_drawdown", sa.Numeric(8, 4)),
        sa.Column("max_drawdown_duration_days", sa.Integer),
        sa.Column("sharpe_ratio", sa.Numeric(8, 4)),
        sa.Column("sortino_ratio", sa.Numeric(8, 4)),
        sa.Column("calmar_ratio", sa.Numeric(8, 4)),
        sa.Column("win_rate", sa.Numeric(8, 4)),
        sa.Column("profit_factor", sa.Numeric(8, 4)),
        sa.Column("total_trades", sa.Integer),
        sa.Column("avg_trade_pnl", sa.Numeric(16, 4)),
        sa.Column("initial_capital", sa.Numeric(16, 2), nullable=False),
        sa.Column("total_invested", sa.Numeric(16, 2)),
        sa.Column("final_value", sa.Numeric(16, 2)),
        sa.Column("total_fees", sa.Numeric(16, 2)),
        sa.Column("btc_accumulated", sa.Numeric(20, 8)),
        sa.Column("avg_cost_basis", sa.Numeric(20, 8)),
        sa.Column("summary_metrics", JSONB, server_default="{}"),
        sa.Column("yearly_returns", JSONB, server_default="{}"),
        sa.Column("benchmark_return", sa.Numeric(12, 6)),
        sa.Column("alpha", sa.Numeric(12, 6)),
        sa.Column("data_snapshot_time", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="回测运行记录",
    )
    op.create_index("idx_backtest_user", "backtest_runs", ["user_id", "created_at"])
    op.create_index("idx_backtest_strategy", "backtest_runs", ["strategy_version_id", "created_at"])
    op.create_index("idx_backtest_status", "backtest_runs", ["status"], postgresql_where=sa.text("status IN ('PENDING', 'RUNNING')"))
    op.create_index("idx_backtest_period", "backtest_runs", ["period_start", "period_end"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON backtest_runs FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "backtest_results",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("backtest_run_id", UUID(as_uuid=True), sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("portfolio_value", sa.Numeric(20, 4), nullable=False),
        sa.Column("cash_balance", sa.Numeric(20, 4), nullable=False, server_default="0"),
        sa.Column("btc_holdings", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("btc_price", sa.Numeric(20, 8)),
        sa.Column("avg_cost", sa.Numeric(20, 8)),
        sa.Column("unrealized_pnl", sa.Numeric(20, 4)),
        sa.Column("realized_pnl", sa.Numeric(20, 4), server_default="0"),
        sa.Column("cumulative_invested", sa.Numeric(20, 4)),
        sa.Column("drawdown", sa.Numeric(8, 6)),
        sa.Column("drawdown_duration", sa.Integer),
        sa.Column("daily_return", sa.Numeric(10, 6)),
        sa.Column("cumulative_return", sa.Numeric(12, 6)),
        sa.Column("metrics_snapshot", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="回测结果详情",
    )
    op.create_index("idx_backtest_results_run", "backtest_results", ["backtest_run_id", "observation_time"])

    op.create_table(
        "backtest_trades",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("backtest_run_id", UUID(as_uuid=True), sa.ForeignKey("backtest_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trade_number", sa.Integer, nullable=False),
        sa.Column("side", trade_side, nullable=False),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity_btc", sa.Numeric(20, 8), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("fee", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("slippage", sa.Numeric(16, 6), nullable=False, server_default="0"),
        sa.Column("trigger_reason", sa.String(100), nullable=False),
        sa.Column("trigger_details", JSONB, server_default="{}"),
        sa.Column("market_context", JSONB, server_default="{}"),
        sa.Column("pnl", sa.Numeric(16, 4)),
        sa.Column("cumulative_btc", sa.Numeric(20, 8)),
        sa.Column("cumulative_invested", sa.Numeric(20, 4)),
        sa.Column("avg_cost_after", sa.Numeric(20, 8)),
        sa.Column("portfolio_after", sa.Numeric(20, 4)),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="回测交易明细",
    )
    op.create_index("idx_backtest_trades_run", "backtest_trades", ["backtest_run_id", "observation_time"])
    op.create_index("idx_backtest_trades_reason", "backtest_trades", ["trigger_reason", "observation_time"])

    # ======== Group 12: user_plans, user_transactions, user_holdings, portfolio_snapshots ========
    op.create_table(
        "user_plans",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_name", sa.String(100), nullable=False),
        sa.Column("plan_type", sa.String(30), nullable=False, server_default="DCA"),
        sa.Column("initial_capital", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(10), nullable=False, server_default="CNY"),
        sa.Column("periodic_amount", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("periodic_frequency", sa.String(20), nullable=False, server_default="MONTHLY"),
        sa.Column("monthly_income", sa.Numeric(16, 2)),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date),
        sa.Column("investment_horizon_years", sa.Integer),
        sa.Column("target_asset", sa.String(20), nullable=False, server_default="BTC"),
        sa.Column("cash_reserve", sa.Numeric(16, 2), server_default="0"),
        sa.Column("cash_reserve_pct", sa.Numeric(5, 2), server_default="10"),
        sa.Column("max_single_investment", sa.Numeric(16, 2)),
        sa.Column("dca_rules", JSONB, nullable=False, server_default='{"type":"fixed","multiplier_rules":[]}'),
        sa.Column("risk_params", JSONB, nullable=False, server_default="{}"),
        sa.Column("max_drawdown_tolerance", sa.Numeric(5, 2), server_default="30"),
        sa.Column("stop_loss_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("stop_loss_pct", sa.Numeric(5, 2)),
        sa.Column("drawdown_buy_rules", JSONB, server_default="[]"),
        sa.Column("valuation_buy_rules", JSONB, server_default="[]"),
        sa.Column("custom_rules", JSONB, server_default="[]"),
        sa.Column("status", plan_status, nullable=False, server_default="ACTIVE"),
        sa.Column("current_phase", sa.String(50)),
        sa.Column("next_investment_date", sa.Date),
        sa.Column("total_invested", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("total_btc", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("avg_cost", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("last_investment_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="用户资金计划配置",
    )
    op.create_index("idx_user_plans_user", "user_plans", ["user_id", "status"])
    op.create_index("idx_user_plans_next", "user_plans", ["next_investment_date"], postgresql_where=sa.text("status = 'ACTIVE'"))
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_plans FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "user_transactions",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("plan_id", UUID(as_uuid=True), sa.ForeignKey("user_plans.id", ondelete="SET NULL")),
        sa.Column("transaction_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("transaction_type", sa.String(20), nullable=False),
        sa.Column("side", trade_side, nullable=False),
        sa.Column("amount", sa.Numeric(16, 4), nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="CNY"),
        sa.Column("price", sa.Numeric(20, 8), nullable=False),
        sa.Column("quantity_btc", sa.Numeric(20, 8), nullable=False),
        sa.Column("fee", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("fee_currency", sa.String(10), server_default="CNY"),
        sa.Column("cumulative_invested", sa.Numeric(16, 2)),
        sa.Column("cumulative_btc", sa.Numeric(20, 8)),
        sa.Column("avg_cost_after", sa.Numeric(20, 8)),
        sa.Column("source", sa.String(30), nullable=False, server_default="MANUAL"),
        sa.Column("exchange_name", sa.String(50)),
        sa.Column("order_id", sa.String(100)),
        sa.Column("notes", sa.Text),
        sa.Column("tags", ARRAY(sa.Text), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "transaction_time"),
        comment="用户交易记录",
    )
    op.create_index("idx_user_tx_user", "user_transactions", ["user_id", "transaction_time"])
    op.create_index("idx_user_tx_plan", "user_transactions", ["plan_id", "transaction_time"])
    op.create_index("idx_user_tx_type", "user_transactions", ["transaction_type", "transaction_time"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON user_transactions FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "user_holdings",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("asset", sa.String(20), nullable=False, server_default="BTC"),
        sa.Column("quantity", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("avg_cost", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("total_cost", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("current_price", sa.Numeric(20, 8)),
        sa.Column("market_value", sa.Numeric(16, 2)),
        sa.Column("market_value_cny", sa.Numeric(16, 2)),
        sa.Column("unrealized_pnl", sa.Numeric(16, 2)),
        sa.Column("unrealized_pnl_pct", sa.Numeric(8, 4)),
        sa.Column("realized_pnl", sa.Numeric(16, 2), server_default="0"),
        sa.Column("total_return_pct", sa.Numeric(10, 4)),
        sa.Column("metadata", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="用户持仓快照",
    )
    op.create_index("idx_user_holdings_user", "user_holdings", ["user_id", "observation_time"])

    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("snapshot_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_value", sa.Numeric(16, 2), nullable=False),
        sa.Column("total_value_cny", sa.Numeric(16, 2)),
        sa.Column("total_invested", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("cash_balance", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("btc_value", sa.Numeric(16, 2), nullable=False, server_default="0"),
        sa.Column("btc_holdings", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Numeric(16, 2)),
        sa.Column("realized_pnl", sa.Numeric(16, 2), server_default="0"),
        sa.Column("daily_return", sa.Numeric(10, 6)),
        sa.Column("cumulative_return", sa.Numeric(12, 6)),
        sa.Column("annualized_return", sa.Numeric(12, 6)),
        sa.Column("max_drawdown", sa.Numeric(8, 4)),
        sa.Column("current_drawdown", sa.Numeric(8, 4)),
        sa.Column("volatility_30d", sa.Numeric(8, 4)),
        sa.Column("sharpe_ratio", sa.Numeric(8, 4)),
        sa.Column("allocation", JSONB, server_default="{}"),
        sa.Column("metrics", JSONB, server_default="{}"),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="用户投资组合净值快照",
    )
    op.create_index("idx_portfolio_user", "portfolio_snapshots", ["user_id", "observation_time"])

    # ======== Group 13: data_quality, sync_checkpoints, system_jobs, audit_logs, market_events ========
    op.create_table(
        "data_quality",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("check_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("data_category", provider_category, nullable=False),
        sa.Column("table_name", sa.String(100), nullable=False),
        sa.Column("check_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="OK"),
        sa.Column("severity", sa.String(10), nullable=False, server_default="INFO"),
        sa.Column("records_checked", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_passed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("completeness_pct", sa.Numeric(6, 3)),
        sa.Column("issues", JSONB, server_default="[]"),
        sa.Column("gaps_found", JSONB, server_default="[]"),
        sa.Column("conflicts_found", JSONB, server_default="[]"),
        sa.Column("anomalies_found", JSONB, server_default="[]"),
        sa.Column("auto_fixed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("fix_actions", JSONB, server_default="[]"),
        sa.Column("requires_manual", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("time_range_start", sa.DateTime(timezone=True)),
        sa.Column("time_range_end", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "check_time"),
        comment="数据质量检查记录",
    )
    op.create_index("idx_dq_category", "data_quality", ["data_category", "check_time"])
    op.create_index("idx_dq_status", "data_quality", ["check_time"], postgresql_where=sa.text("status != 'OK'"))
    op.create_index("idx_dq_severity", "data_quality", ["severity", "check_time"], postgresql_where=sa.text("severity IN ('HIGH', 'CRITICAL')"))

    op.create_table(
        "sync_checkpoints",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("task_name", sa.String(100), nullable=False),
        sa.Column("data_category", provider_category, nullable=False),
        sa.Column("provider_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("symbol", sa.String(20), server_default="BTC"),
        sa.Column("last_synced_time", sa.DateTime(timezone=True)),
        sa.Column("target_end_time", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sync_status, nullable=False, server_default="IDLE"),
        sa.Column("progress_pct", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("records_synced", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("records_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sync_params", JSONB, nullable=False, server_default="{}"),
        sa.Column("batch_size", sa.Integer, nullable=False, server_default="1000"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="5"),
        sa.Column("last_error", sa.Text),
        sa.Column("last_error_time", sa.DateTime(timezone=True)),
        sa.Column("error_history", JSONB, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("task_name", "provider_id", "symbol", name="uq_sync_checkpoints_task"),
        comment="数据同步断点续传状态",
    )
    op.create_index("idx_sync_status", "sync_checkpoints", ["status"], postgresql_where=sa.text("status IN ('SYNCING', 'RETRY', 'FAILED')"))
    op.create_index("idx_sync_category", "sync_checkpoints", ["data_category", "status"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON sync_checkpoints FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "system_jobs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("job_name", sa.String(100), nullable=False, unique=True),
        sa.Column("job_type", sa.String(50), nullable=False),
        sa.Column("job_group", sa.String(50), nullable=False, server_default="GENERAL"),
        sa.Column("description", sa.Text),
        sa.Column("schedule_cron", sa.String(100)),
        sa.Column("schedule_interval_seconds", sa.Integer),
        sa.Column("priority", sa.Integer, nullable=False, server_default="50"),
        sa.Column("status", job_status, nullable=False, server_default="PENDING"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_run_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("last_duration_ms", sa.Integer),
        sa.Column("avg_duration_ms", sa.Integer),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("job_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("result_summary", JSONB, server_default="{}"),
        sa.Column("dependencies", ARRAY(sa.Text), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="系统调度任务注册表",
    )
    op.create_index("idx_jobs_enabled", "system_jobs", ["priority", "next_run_at"], postgresql_where=sa.text("is_enabled = true"))
    op.create_index("idx_jobs_status", "system_jobs", ["status"], postgresql_where=sa.text("status = 'RUNNING'"))
    op.create_index("idx_jobs_group", "system_jobs", ["job_group", "is_enabled"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON system_jobs FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    op.create_table(
        "audit_logs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_time", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("action_category", sa.String(30), nullable=False, server_default="GENERAL"),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True)),
        sa.Column("resource_name", sa.String(200)),
        sa.Column("ip_address", sa.String(45)),
        sa.Column("user_agent", sa.Text),
        sa.Column("request_id", sa.String(100)),
        sa.Column("old_values", JSONB),
        sa.Column("new_values", JSONB),
        sa.Column("context", JSONB, server_default="{}"),
        sa.Column("is_success", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("error_message", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "event_time"),
        comment="系统审计日志",
    )
    op.create_index("idx_audit_user", "audit_logs", ["user_id", "event_time"])
    op.create_index("idx_audit_action", "audit_logs", ["action", "event_time"])
    op.create_index("idx_audit_resource", "audit_logs", ["resource_type", "resource_id", "event_time"])
    op.create_index("idx_audit_category", "audit_logs", ["action_category", "event_time"])

    op.create_table(
        "market_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observation_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("event_category", sa.String(30), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("title_cn", sa.String(200)),
        sa.Column("description", sa.Text),
        sa.Column("description_cn", sa.Text),
        sa.Column("significance", sa.Numeric(5, 4), nullable=False, server_default="0.5"),
        sa.Column("is_major", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("btc_price_at_event", sa.Numeric(20, 8)),
        sa.Column("btc_price_before_24h", sa.Numeric(20, 8)),
        sa.Column("btc_price_after_24h", sa.Numeric(20, 8)),
        sa.Column("btc_price_after_7d", sa.Numeric(20, 8)),
        sa.Column("btc_price_after_30d", sa.Numeric(20, 8)),
        sa.Column("market_impact", JSONB, server_default="{}"),
        sa.Column("related_data", JSONB, server_default="{}"),
        sa.Column("tags", ARRAY(sa.Text), server_default="{}"),
        sa.Column("source_url", sa.Text),
        sa.Column("source_id", UUID(as_uuid=True), sa.ForeignKey("providers.id")),
        sa.Column("is_auto_detected", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", "observation_time"),
        comment="市场事件时间线",
    )
    op.create_index("idx_market_events_time", "market_events", ["observation_time"])
    op.create_index("idx_market_events_type", "market_events", ["event_type", "observation_time"])
    op.create_index("idx_market_events_major", "market_events", ["observation_time"], postgresql_where=sa.text("is_major = true"))
    op.create_index("idx_market_events_category", "market_events", ["event_category", "observation_time"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON market_events FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))


def downgrade() -> None:
    # 按依赖反序删除所有表
    tables = [
        "market_events", "audit_logs", "system_jobs", "sync_checkpoints", "data_quality",
        "portfolio_snapshots", "user_holdings", "user_transactions", "user_plans",
        "backtest_trades", "backtest_results", "backtest_runs",
        "model_validations", "model_weights",
        "signals", "market_regimes", "risk_scores", "valuation_states", "cycle_states",
        "indicator_values", "sentiment",
        "macro_events", "macro_series",
        "options_data", "derivatives",
        "etf_holdings", "etf_flows",
        "supply_metrics", "address_metrics", "exchange_flows", "onchain_metrics",
        "trades", "orderbooks", "candles", "market_prices",
        "raw_sentiment_data", "raw_macro_data", "raw_derivatives_data",
        "raw_etf_data", "raw_onchain_data", "raw_market_data",
        "strategy_versions", "strategies",
        "model_versions", "indicator_definitions",
        "provider_requests", "provider_failover_events", "provider_scores", "provider_health",
        "providers", "users", "assets",
    ]
    for tbl in tables:
        op.drop_table(tbl)

    # 删除 ENUM 类型
    enums = [
        "backtest_status", "model_status", "plan_status", "trade_side", "user_role",
        "sync_status", "job_status", "signal_direction", "risk_level", "valuation_level",
        "cycle_phase", "candle_interval", "quality_status_type", "failover_resolution",
        "provider_status", "provider_category",
    ]
    for enum_name in enums:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))

    op.execute(sa.text("DROP FUNCTION IF EXISTS trigger_set_updated_at()"))
