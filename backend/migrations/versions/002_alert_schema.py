"""Alert schema - alert rules / events / deliveries / channel configs

Revision ID: 002
Revises: 001
Create Date: 2026-09-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---- ENUM 类型 ----
alert_severity = sa.Enum(
    "INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL",
    name="alert_severity", create_type=False,
)
alert_status = sa.Enum(
    "TRIGGERED", "ACKED", "RESOLVED", "SUPPRESSED",
    name="alert_status", create_type=False,
)
alert_rule_state = sa.Enum(
    "ACTIVE", "PAUSED", "ARCHIVED", "ERROR",
    name="alert_rule_state", create_type=False,
)


def upgrade() -> None:
    # ---- 创建 ENUM 类型（checkfirst，重复执行安全）----
    alert_severity.create(op.get_bind(), checkfirst=True)
    alert_status.create(op.get_bind(), checkfirst=True)
    alert_rule_state.create(op.get_bind(), checkfirst=True)

    # ---- 触发器函数（001 已创建则跳过）----
    op.execute(sa.text("""
        CREATE OR REPLACE FUNCTION trigger_set_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """))

    # ======== alert_rules ========
    op.create_table(
        "alert_rules",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("rule_name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("severity", alert_severity, nullable=False, server_default="MEDIUM"),
        sa.Column("category", sa.String(30), nullable=False,
                  server_default="MARKET",
                  comment="MARKET/INDICATOR/ENGINE/PROVIDER/PORTFOLIO/CUSTOM"),
        sa.Column("target_symbol", sa.String(20), nullable=False, server_default="BTC"),
        sa.Column("condition_tree", JSONB, nullable=False, comment="条件树 AST"),
        sa.Column("condition_text", sa.Text, comment="人类可读条件描述"),
        sa.Column("duration_seconds", sa.Integer, comment="条件需持续满足的秒数"),
        sa.Column("consecutive_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("cooldown_seconds", sa.Integer, nullable=False, server_default="86400"),
        sa.Column("channels", JSONB, nullable=False, server_default='["EMAIL"]'),
        sa.Column("channel_config", JSONB, nullable=False, server_default="{}"),
        sa.Column("min_data_quality", sa.String(20), nullable=False, server_default="ESTIMATED",
                  comment="低于此质量不触发"),
        sa.Column("require_multi_source", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("state", alert_rule_state, nullable=False, server_default="ACTIVE"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="50"),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True)),
        sa.Column("last_triggered_at", sa.DateTime(timezone=True)),
        sa.Column("trigger_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("consecutive_triggers", sa.Integer, nullable=False, server_default="0",
                  comment="未恢复期间的连续触发次数"),
        sa.Column("tags", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="预警规则定义",
    )
    op.create_index(
        "idx_alert_rules_enabled", "alert_rules", ["is_enabled", "state"],
        postgresql_where=sa.text("is_enabled = true"),
    )
    op.create_index(
        "idx_alert_rules_priority", "alert_rules", ["priority", "last_evaluated_at"],
        postgresql_where=sa.text("is_enabled = true"),
    )
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON alert_rules FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== alert_events ========
    op.create_table(
        "alert_events",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("rule_id", UUID(as_uuid=True), sa.ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True)),
        sa.Column("triggered_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("severity", alert_severity, nullable=False),
        sa.Column("status", alert_status, nullable=False, server_default="TRIGGERED"),
        sa.Column("trigger_value", sa.Numeric(30, 10), comment="触发时指标值"),
        sa.Column("trigger_threshold", sa.Numeric(30, 10), comment="触发阈值"),
        sa.Column("condition_result", JSONB, comment="各子条件求值详情"),
        sa.Column("evidence", JSONB, server_default="[]", comment="证据链"),
        sa.Column("market_context", JSONB, server_default="{}", comment="触发时市场快照"),
        sa.Column("data_quality", sa.String(20)),
        sa.Column("data_source", sa.String(50)),
        sa.Column("title", sa.String(200)),
        sa.Column("description_cn", sa.Text),
        sa.Column("is_suppressed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("suppress_reason", sa.String(50), comment="COOLDOWN/SILENCE/RATE_LIMIT"),
        sa.Column("acked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="预警事件",
    )
    op.create_index("idx_alert_events_rule", "alert_events", ["rule_id", sa.text("triggered_at DESC")])
    op.create_index("idx_alert_events_user", "alert_events", ["user_id", sa.text("triggered_at DESC")])
    op.create_index("idx_alert_events_status", "alert_events", ["status", sa.text("triggered_at DESC")])

    # ======== alert_deliveries ========
    op.create_table(
        "alert_deliveries",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("event_id", UUID(as_uuid=True), sa.ForeignKey("alert_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False, comment="EMAIL/WEBHOOK/TELEGRAM"),
        sa.Column("recipient", sa.String(200)),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING",
                  comment="PENDING/SENDING/SENT/FAILED"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column("response_detail", JSONB, server_default="{}"),
        sa.Column("template_used", sa.String(100)),
        sa.Column("payload_snapshot", JSONB, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="预警投递记录",
    )
    op.create_index("idx_alert_deliveries_event", "alert_deliveries", ["event_id"])
    op.create_index(
        "idx_alert_deliveries_retry", "alert_deliveries", ["status"],
        postgresql_where=sa.text("status IN ('PENDING', 'FAILED')"),
    )
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON alert_deliveries FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))

    # ======== alert_channel_configs ========
    op.create_table(
        "alert_channel_configs",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("channel_type", sa.String(20), nullable=False, comment="EMAIL/WEBHOOK/TELEGRAM"),
        sa.Column("channel_name", sa.String(100), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}", comment="SMTP 配置等"),
        sa.Column("is_primary", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="用户通知渠道配置",
    )
    op.create_index("idx_alert_channel_configs_user", "alert_channel_configs", ["user_id", "channel_type"])
    op.execute(sa.text("CREATE TRIGGER set_updated_at BEFORE UPDATE ON alert_channel_configs FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();"))


def downgrade() -> None:
    # 按依赖反序删除表
    tables = [
        "alert_deliveries", "alert_events", "alert_channel_configs", "alert_rules",
    ]
    for tbl in tables:
        op.drop_table(tbl)

    # 删除 ENUM 类型
    enums = [
        "alert_rule_state", "alert_status", "alert_severity",
    ]
    for enum_name in enums:
        op.execute(sa.text(f"DROP TYPE IF EXISTS {enum_name}"))
