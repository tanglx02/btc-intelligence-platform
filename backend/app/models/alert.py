"""预警系统 ORM 模型。

包含：alert_rules, alert_events, alert_deliveries, alert_channel_configs

设计要点：
1. AlertRule 为普通表（非 hypertable）：规则数量少、需频繁更新计数器；
2. AlertEvent / AlertDelivery 事件流水表，支持后续按时间分区；
3. 条件树以 JSONB AST 持久化（app.alerts.schemas.ConditionNode 格式），
   由 AlertEvaluator 递归求值，数据缺失/过期求值为 UNKNOWN（不触发）。
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import (
    AlertRuleState,
    AlertSeverity,
    AlertStatus,
    alert_rule_state_enum,
    alert_severity_enum,
    alert_status_enum,
)


class AlertRule(Base, TimestampMixin):
    """预警规则定义（条件树 + 触发策略 + 通知渠道）。"""

    __tablename__ = "alert_rules"
    __table_args__ = (
        Index(
            "idx_alert_rules_enabled", "is_enabled", "state",
            postgresql_where=text("is_enabled = true"),
        ),
        Index(
            "idx_alert_rules_priority", "priority", "last_evaluated_at",
            postgresql_where=text("is_enabled = true"),
        ),
        {"comment": "预警规则定义"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        comment="所属用户（NULL 为系统级规则）",
    )
    rule_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="规则名称",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="规则描述")
    severity: Mapped[AlertSeverity] = mapped_column(
        alert_severity_enum, nullable=False, server_default=text("'MEDIUM'"),
        comment="严重程度：INFO / LOW / MEDIUM / HIGH / CRITICAL",
    )
    category: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="规则分类：MARKET / INDICATOR / ENGINE / PROVIDER / PORTFOLIO / CUSTOM",
    )
    target_symbol: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'BTC'"), comment="目标资产符号",
    )
    condition_tree: Mapped[dict] = mapped_column(
        JSONB, nullable=False, comment="条件树 AST（ConditionNode JSON 格式）",
    )
    condition_text: Mapped[Optional[str]] = mapped_column(
        Text, comment="人类可读条件描述",
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(
        Integer, comment="条件需持续满足的秒数（NULL 不启用持续时间条件）",
    )
    consecutive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1"),
        comment="需连续满足的求值次数",
    )
    cooldown_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("86400"),
        comment="触发后冷却秒数（默认 24h）",
    )
    channels: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"EMAIL\"]'::jsonb"),
        comment="通知渠道列表：EMAIL / WEBHOOK / TELEGRAM",
    )
    channel_config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
        comment="渠道级配置（收件人等）",
    )
    min_data_quality: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'ESTIMATED'"),
        comment="最低数据质量要求（低于此质量不触发）",
    )
    require_multi_source: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="是否要求多源交叉验证通过才触发",
    )
    state: Mapped[AlertRuleState] = mapped_column(
        alert_rule_state_enum, nullable=False, server_default=text("'ACTIVE'"),
        comment="规则状态：ACTIVE / PAUSED / ARCHIVED / ERROR",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用",
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("50"), comment="优先级（数值小先求值）",
    )
    last_evaluated_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="最近求值时间",
    )
    last_triggered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="最近触发时间",
    )
    trigger_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="累计触发次数",
    )
    consecutive_triggers: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"),
        comment="未恢复期间的连续触发次数（恢复时归零）",
    )
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="标签",
    )


class AlertEvent(Base, CreatedAtMixin):
    """预警事件（规则条件满足时产生）。"""

    __tablename__ = "alert_events"
    __table_args__ = (
        Index("idx_alert_events_rule", "rule_id", text("triggered_at DESC")),
        Index("idx_alert_events_user", "user_id", text("triggered_at DESC")),
        Index("idx_alert_events_status", "status", text("triggered_at DESC")),
        {"comment": "预警事件"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    rule_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("alert_rules.id", ondelete="CASCADE"),
        nullable=False, comment="规则 ID",
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), comment="用户 ID（冗余自规则）",
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="触发时间",
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="恢复（解决）时间",
    )
    severity: Mapped[AlertSeverity] = mapped_column(
        alert_severity_enum, nullable=False, comment="严重程度（冗余自规则）",
    )
    status: Mapped[AlertStatus] = mapped_column(
        alert_status_enum, nullable=False, server_default=text("'TRIGGERED'"),
        comment="事件状态：TRIGGERED / ACKED / RESOLVED / SUPPRESSED",
    )
    trigger_value: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(30, 10), comment="触发时指标值",
    )
    trigger_threshold: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(30, 10), comment="触发阈值",
    )
    condition_result: Mapped[Optional[dict]] = mapped_column(
        JSONB, comment="各子条件求值详情",
    )
    evidence: Mapped[Optional[list]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb"), comment="证据链",
    )
    market_context: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"),
        comment="触发时市场快照（price / mvrv / risk / cycle 等）",
    )
    data_quality: Mapped[Optional[str]] = mapped_column(
        String(20), comment="触发时数据质量：VERIFIED / ESTIMATED / STALE / CONFLICT",
    )
    data_source: Mapped[Optional[str]] = mapped_column(
        String(50), comment="触发时主数据来源",
    )
    title: Mapped[Optional[str]] = mapped_column(String(200), comment="事件标题")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    is_suppressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="是否被抑制（仅记录不投递）",
    )
    suppress_reason: Mapped[Optional[str]] = mapped_column(
        String(50), comment="抑制原因：COOLDOWN / SILENCE / RATE_LIMIT",
    )
    acked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="确认时间",
    )


class AlertDelivery(Base, TimestampMixin):
    """预警投递记录（每渠道一条，供重试与审计）。"""

    __tablename__ = "alert_deliveries"
    __table_args__ = (
        Index("idx_alert_deliveries_event", "event_id"),
        Index(
            "idx_alert_deliveries_retry", "status",
            postgresql_where=text("status IN ('PENDING', 'FAILED')"),
        ),
        {"comment": "预警投递记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    event_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("alert_events.id", ondelete="CASCADE"),
        nullable=False, comment="预警事件 ID",
    )
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="投递渠道：EMAIL / WEBHOOK / TELEGRAM",
    )
    recipient: Mapped[Optional[str]] = mapped_column(
        String(200), comment="接收方（邮箱地址 / webhook URL / chat id）",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'"),
        comment="投递状态：PENDING / SENDING / SENT / FAILED",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0"), comment="已尝试次数",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("3"), comment="最大尝试次数",
    )
    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="成功投递时间",
    )
    failed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="最终失败时间",
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, comment="最近错误信息")
    response_detail: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="渠道响应详情",
    )
    template_used: Mapped[Optional[str]] = mapped_column(
        String(100), comment="使用的模板名称",
    )
    payload_snapshot: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="投递内容快照",
    )


class AlertChannelConfig(Base, TimestampMixin):
    """用户通知渠道配置（SMTP / Webhook / Telegram 等）。"""

    __tablename__ = "alert_channel_configs"
    __table_args__ = (
        Index("idx_alert_channel_configs_user", "user_id", "channel_type"),
        {"comment": "用户通知渠道配置"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        comment="所属用户（NULL 为系统级配置）",
    )
    channel_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="渠道类型：EMAIL / WEBHOOK / TELEGRAM",
    )
    channel_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="渠道显示名称",
    )
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"),
        comment="渠道配置（SMTP 服务器、URL、凭据引用等）",
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否主渠道",
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), comment="是否已验证",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), comment="是否启用",
    )
