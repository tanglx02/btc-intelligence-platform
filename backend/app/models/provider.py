"""Provider 管理相关 ORM 模型。

包含：providers, provider_health, provider_scores,
      provider_failover_events, provider_requests
"""

from datetime import datetime
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import (
    FailoverResolution,
    ProviderCategory,
    ProviderStatus,
    failover_resolution_enum,
    provider_category_enum,
    provider_status_enum,
)


class Provider(Base, TimestampMixin):
    """Provider 数据源注册表。"""

    __tablename__ = "providers"
    __table_args__ = (
        Index("idx_providers_category", "category", postgresql_where=text("is_enabled = true")),
        Index("idx_providers_priority", "category", "priority", postgresql_where=text("is_enabled = true")),
        Index("idx_providers_status", "status"),
        {"comment": "Provider 数据源注册表，所有外部数据源在此注册管理"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="Provider 唯一名称标识",
    )
    category: Mapped[ProviderCategory] = mapped_column(
        provider_category_enum, nullable=False, comment="数据类别",
    )
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="API 基础 URL")
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, comment="API Key（加密存储）")
    proxy_config: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("""'{"type": "direct"}'::jsonb"""), comment="代理配置",
    )
    timeout_config: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        server_default=text("""'{"connect": 5000, "read": 30000, "write": 10000}'::jsonb"""),
        comment="超时配置（毫秒）",
    )
    retry_config: Mapped[Optional[dict]] = mapped_column(
        JSONB,
        server_default=text("""'{"max_retries": 3, "backoff_factor": 2, "backoff_max": 60}'::jsonb"""),
        comment="重试配置",
    )
    rate_limit: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("60"), comment="速率限制：每窗口最大请求数")
    rate_limit_window: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("60"), comment="速率窗口（秒）")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("100"), comment="优先级，数字越小优先级越高")
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否启用")
    is_locked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否锁定优先级")
    status: Mapped[ProviderStatus] = mapped_column(
        provider_status_enum, nullable=False, server_default=text("'OFFLINE'"), comment="当前状态",
    )
    health_score: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), server_default=text("0"), comment="综合健康评分 0-100")
    recovery_threshold: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("3"), comment="恢复阈值：连续成功N次后恢复")
    failure_threshold: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("5"), comment="故障阈值：连续失败N次后标记故障")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="描述")
    supported_symbols: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY['BTC']"), comment="支持的资产符号",
    )
    supported_intervals: Mapped[Optional[list]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'"), comment="支持的K线间隔",
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后成功时间")
    last_failure_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后失败时间")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="连续失败次数")

    # Relationships
    health_records: Mapped[list["ProviderHealth"]] = relationship(back_populates="provider", lazy="selectin")
    scores: Mapped[list["ProviderScore"]] = relationship(back_populates="provider", lazy="selectin")
    failover_from: Mapped[list["ProviderFailoverEvent"]] = relationship(
        back_populates="from_provider", foreign_keys="ProviderFailoverEvent.from_provider_id", lazy="selectin",
    )


class ProviderHealth(Base):
    """Provider 健康状态快照（高频时序数据）。"""

    __tablename__ = "provider_health"
    __table_args__ = (
        Index("idx_provider_health_pid_time", "provider_id", "check_time"),
        {"comment": "Provider 健康状态快照（高频时序数据）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="Provider ID",
    )
    check_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(),
        nullable=False, comment="检查时间",
    )
    status: Mapped[ProviderStatus] = mapped_column(provider_status_enum, nullable=False, comment="当前状态")
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="响应时间（毫秒）")
    success_rate_1h: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), comment="过去1小时成功率（%）")
    success_rate_24h: Mapped[Optional[float]] = mapped_column(Numeric(5, 2), comment="过去24小时成功率（%）")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="连续失败次数")
    today_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="今日失败次数")
    today_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="今日请求次数")
    data_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="数据延迟（毫秒）")
    last_error: Mapped[Optional[str]] = mapped_column(Text, comment="最后错误信息")
    last_error_type: Mapped[Optional[str]] = mapped_column(String(50), comment="最后错误类型")
    http_status: Mapped[Optional[int]] = mapped_column(Integer, comment="HTTP 状态码")
    is_rate_limited: Mapped[Optional[bool]] = mapped_column(Boolean, server_default=text("false"), comment="是否被限速")
    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="扩展指标 JSONB")

    # Relationships
    provider: Mapped["Provider"] = relationship(back_populates="health_records")


class ProviderScore(Base):
    """Provider 综合评分历史。"""

    __tablename__ = "provider_scores"
    __table_args__ = (
        Index("idx_provider_scores_pid", "provider_id", "scored_at"),
        Index("idx_provider_scores_rank", "scored_at", "rank"),
        {"comment": "Provider 综合评分历史"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="Provider ID",
    )
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(),
        nullable=False, comment="评分时间",
    )
    accuracy_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="数据准确性评分 0-100")
    latency_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="响应速度评分 0-100")
    stability_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="稳定性评分 0-100")
    completeness_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="数据完整性评分 0-100")
    consistency_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="一致性评分 0-100")
    overall_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="综合评分 0-100")
    rank: Mapped[Optional[int]] = mapped_column(Integer, comment="同 category 内排名")
    score_details: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="评分详情")

    # Relationships
    provider: Mapped["Provider"] = relationship(back_populates="scores")


class ProviderFailoverEvent(Base):
    """Provider 故障切换事件记录。"""

    __tablename__ = "provider_failover_events"
    __table_args__ = (
        Index("idx_failover_from", "from_provider_id", "occurred_at"),
        Index("idx_failover_category", "data_category", "occurred_at"),
        {"comment": "Provider 故障切换事件记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    from_provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), primary_key=True,
        nullable=False, comment="源 Provider ID",
    )
    to_provider_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="目标 Provider ID",
    )
    data_category: Mapped[ProviderCategory] = mapped_column(
        provider_category_enum, nullable=False, comment="数据类别",
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(20), server_default=text("'BTC'"), comment="资产符号")
    trigger_reason: Mapped[str] = mapped_column(String(50), nullable=False, comment="触发原因")
    error_message: Mapped[Optional[str]] = mapped_column(Text, comment="错误信息")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(),
        nullable=False, comment="发生时间",
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="解决时间")
    resolution_type: Mapped[Optional[FailoverResolution]] = mapped_column(
        failover_resolution_enum, comment="解决方式",
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, comment="持续时间（秒）")
    requests_affected: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("0"), comment="受影响请求数")
    context: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="上下文信息")

    # Relationships
    from_provider: Mapped["Provider"] = relationship(
        back_populates="failover_from", foreign_keys=[from_provider_id],
    )


class ProviderRequest(Base):
    """Provider 请求日志（采样存储）。"""

    __tablename__ = "provider_requests"
    __table_args__ = (
        Index("idx_requests_provider", "provider_id", "request_time"),
        Index("idx_requests_failed", "provider_id", "request_time", postgresql_where=text("is_success = false")),
        {"comment": "Provider 请求日志（采样存储）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="Provider ID",
    )
    request_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(),
        nullable=False, comment="请求时间",
    )
    method: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'GET'"), comment="HTTP 方法")
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="请求 URL")
    endpoint: Mapped[Optional[str]] = mapped_column(String(200), comment="端点路径")
    status_code: Mapped[Optional[int]] = mapped_column(Integer, comment="HTTP 状态码")
    response_time_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="响应时间（毫秒）")
    error_type: Mapped[Optional[str]] = mapped_column(String(50), comment="错误类型")
    error_message: Mapped[Optional[str]] = mapped_column(Text, comment="错误信息")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    response_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, comment="响应大小（字节）")
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否成功")
    is_sampled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为采样记录")
    data_category: Mapped[Optional[ProviderCategory]] = mapped_column(provider_category_enum, comment="数据类别")
