"""系统数据 ORM 模型。

包含：system_settings, data_quality, sync_checkpoints, system_jobs,
      audit_logs, market_events
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import (
    JobStatus,
    ProviderCategory,
    SyncStatus,
    job_status_enum,
    provider_category_enum,
    sync_status_enum,
)


class SystemSetting(Base, TimestampMixin):
    """系统设置键值对（点分命名空间，后台可改并热生效）。"""

    __tablename__ = "system_settings"
    __table_args__ = (
        {"comment": "系统设置键值对（点分命名空间）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True,
        comment="设置键（点分命名空间：alert.scan_interval_seconds / provider.health_check.interval 等）",
    )
    value: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{\"v\": null}'::jsonb"),
        comment="设置值（统一 {\"v\": ...} 包装，兼容 JSONB 任意标量/结构）",
    )
    description: Mapped[Optional[str]] = mapped_column(String(500), comment="设置说明")
    is_secret: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"),
        comment="是否敏感值（true 时加密存储、读取时掩码）",
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(100), comment="最后修改人")


class DataQuality(Base, CreatedAtMixin):
    """数据质量检查记录。"""

    __tablename__ = "data_quality"
    __table_args__ = (
        Index("idx_dq_category", "data_category", "check_time"),
        Index("idx_dq_status", "check_time", postgresql_where=text("status != 'OK'")),
        Index("idx_dq_severity", "severity", "check_time", postgresql_where=text("severity IN ('HIGH', 'CRITICAL')")),
        {"comment": "数据质量检查记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    check_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="检查时间",
    )
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    data_category: Mapped[ProviderCategory] = mapped_column(provider_category_enum, nullable=False, comment="数据类别")
    table_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="检查的表名")
    check_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="检查类型：COMPLETENESS / CONSISTENCY / ACCURACY / TIMELINESS / GAP_DETECTION / CROSS_VALIDATION",
    )
    # 结果
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'OK'"), comment="OK / WARNING / ERROR / CRITICAL")
    severity: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'INFO'"), comment="INFO / LOW / MEDIUM / HIGH / CRITICAL")
    records_checked: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="检查记录数")
    records_passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="通过记录数")
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="失败记录数")
    completeness_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 3), comment="完整性百分比")
    # 详情
    issues: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="发现的问题")
    gaps_found: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="发现的数据空洞")
    conflicts_found: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="发现的数据冲突")
    anomalies_found: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="发现的异常")
    # 修复
    auto_fixed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否自动修复")
    fix_actions: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="修复操作记录")
    requires_manual: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否需要人工处理")
    # 元数据
    time_range_start: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="检查时间范围开始")
    time_range_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="检查时间范围结束")


class SyncCheckpoint(Base, TimestampMixin):
    """数据同步断点续传状态。"""

    __tablename__ = "sync_checkpoints"
    __table_args__ = (
        UniqueConstraint("task_name", "provider_id", "symbol", name="uq_sync_checkpoints_task"),
        Index("idx_sync_status", "status", postgresql_where=text("status IN ('SYNCING', 'RETRY', 'FAILED')")),
        Index("idx_sync_category", "data_category", "status"),
        {"comment": "数据同步断点续传状态"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    task_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="任务名称：market_history / onchain_history / etf_history 等",
    )
    data_category: Mapped[ProviderCategory] = mapped_column(provider_category_enum, nullable=False, comment="数据类别")
    provider_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="Provider ID",
    )
    symbol: Mapped[Optional[str]] = mapped_column(String(20), server_default=text("'BTC'"), comment="资产符号")
    # 同步进度
    last_synced_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="最后成功同步到的时间点（断点位置）",
    )
    target_end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="目标结束时间")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="开始时间")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="完成时间")
    # 状态
    status: Mapped[SyncStatus] = mapped_column(
        sync_status_enum, nullable=False, server_default=text("'IDLE'"),
        comment="IDLE / SYNCING / PAUSED / COMPLETED / FAILED / RETRY",
    )
    progress_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="进度百分比")
    records_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="已同步记录数")
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="失败记录数")
    records_skipped: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="跳过记录数")
    # 配置
    sync_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="同步参数")
    batch_size: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1000"), comment="批次大小")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="重试次数")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"), comment="最大重试次数")
    # 错误
    last_error: Mapped[Optional[str]] = mapped_column(Text, comment="最后错误信息")
    last_error_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后错误时间")
    error_history: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="错误历史")


class SystemJob(Base, TimestampMixin):
    """系统调度任务注册表。"""

    __tablename__ = "system_jobs"
    __table_args__ = (
        Index("idx_jobs_enabled", "priority", "next_run_at", postgresql_where=text("is_enabled = true")),
        Index("idx_jobs_status", "status", postgresql_where=text("status = 'RUNNING'")),
        Index("idx_jobs_group", "job_group", "is_enabled"),
        {"comment": "系统调度任务注册表"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    job_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, comment="任务名称")
    job_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="任务类型：SCHEDULED / TRIGGERED / MANUAL / ONE_TIME",
    )
    job_group: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'GENERAL'"),
        comment="任务组：MARKET / ONCHAIN / ETF / DERIVATIVES / MACRO / SENTIMENT / ENGINE / SYSTEM",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="任务描述")
    # 调度
    schedule_cron: Mapped[Optional[str]] = mapped_column(String(100), comment="Cron 表达式")
    schedule_interval_seconds: Mapped[Optional[int]] = mapped_column(Integer, comment="调度间隔（秒）")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"), comment="优先级 1-100，数字越小越优先")
    # 状态
    status: Mapped[JobStatus] = mapped_column(
        job_status_enum, nullable=False, server_default=text("'PENDING'"), comment="任务状态",
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否启用")
    # 运行记录
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后运行时间")
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="下次运行时间")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="本次开始时间")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="本次完成时间")
    last_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="上次运行时长（毫秒）")
    avg_duration_ms: Mapped[Optional[int]] = mapped_column(Integer, comment="平均运行时长（毫秒）")
    # 重试
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="重试次数")
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"), comment="最大重试次数")
    last_error: Mapped[Optional[str]] = mapped_column(Text, comment="最后错误信息")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="连续失败次数")
    # 配置
    job_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="任务配置")
    result_summary: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="结果摘要")
    dependencies: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="依赖的其他任务 job_name 列表")


class AuditLog(Base, CreatedAtMixin):
    """系统审计日志（只追加，不可修改删除）。"""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_user", "user_id", "event_time"),
        Index("idx_audit_action", "action", "event_time"),
        Index("idx_audit_resource", "resource_type", "resource_id", "event_time"),
        Index("idx_audit_category", "action_category", "event_time"),
        {"comment": "系统审计日志（只追加，不可修改删除）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    event_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="事件时间",
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), comment="操作用户 ID",
    )
    # 操作
    action: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="操作：CREATE / UPDATE / DELETE / LOGIN / LOGOUT / EXPORT / CONFIG_CHANGE / PROVIDER_SWITCH",
    )
    action_category: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'GENERAL'"),
        comment="操作分类：AUTH / DATA / CONFIG / PROVIDER / BACKTEST / PORTFOLIO",
    )
    resource_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="资源类型：USER / PROVIDER / PLAN / STRATEGY / MODEL",
    )
    resource_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="资源 ID")
    resource_name: Mapped[Optional[str]] = mapped_column(String(200), comment="资源名称")
    # 上下文
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), comment="IP 地址")
    user_agent: Mapped[Optional[str]] = mapped_column(Text, comment="User Agent")
    request_id: Mapped[Optional[str]] = mapped_column(String(100), comment="请求 ID")
    # 变更
    old_values: Mapped[Optional[dict]] = mapped_column(JSONB, comment="变更前值")
    new_values: Mapped[Optional[dict]] = mapped_column(JSONB, comment="变更后值")
    context: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="上下文信息")
    # 结果
    is_success: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否成功")
    error_message: Mapped[Optional[str]] = mapped_column(Text, comment="错误信息")


class MarketEvent(Base, TimestampMixin):
    """市场事件时间线。"""

    __tablename__ = "market_events"
    __table_args__ = (
        Index("idx_market_events_time", "observation_time"),
        Index("idx_market_events_type", "event_type", "observation_time"),
        Index("idx_market_events_major", "observation_time", postgresql_where=text("is_major = true")),
        Index("idx_market_events_category", "event_category", "observation_time"),
        {"comment": "市场事件时间线"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="事件发生时间")
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时间",
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="HALVING / ATH / ATL / CRASH / RECOVERY / ETF_LAUNCH / FED_DECISION / REGULATION / HACK / LIQUIDATION_CASCADE",
    )
    event_category: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="CRYPTO / MACRO / REGULATORY / TECHNOLOGY / MARKET_STRUCTURE",
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="事件标题")
    title_cn: Mapped[Optional[str]] = mapped_column(String(200), comment="事件中文标题")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="英文描述")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    # 重要性
    significance: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0.5"), comment="重要性评分 0-1")
    is_major: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为重大事件")
    # 市场数据
    btc_price_at_event: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件发生时 BTC 价格")
    btc_price_before_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件前24h BTC 价格")
    btc_price_after_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件后24h BTC 价格")
    btc_price_after_7d: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件后7天 BTC 价格")
    btc_price_after_30d: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件后30天 BTC 价格")
    # 影响
    market_impact: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="市场影响")
    related_data: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="关联数据")
    tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="标签")
    # 来源
    source_url: Mapped[Optional[str]] = mapped_column(Text, comment="来源 URL")
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    is_auto_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否自动检测")
