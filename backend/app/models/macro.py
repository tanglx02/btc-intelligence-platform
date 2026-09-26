"""宏观数据 ORM 模型。

包含：macro_series, macro_events
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
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
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import QualityStatus, quality_status_enum


class MacroSeries(Base, CreatedAtMixin):
    """宏观经济指标时序数据。

    严格区分观测日期、发布日期、修订日期以防止未来数据泄漏。
    """

    __tablename__ = "macro_series"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_date", "revision_number", name="idx_macro_unique"),
        Index("idx_macro_series", "series_id", "observation_time"),
        Index("idx_macro_release", "release_date", "series_id"),
        Index("idx_macro_revised", "series_id", "observation_time", postgresql_where=text("is_revised = true")),
        {"comment": "宏观经济指标时序数据"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="数据观测时间",
    )
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    series_id: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="系列 ID：DXY / FED_RATE / CPI / PCE / NFP / M2 / US10Y 等",
    )
    series_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="系列名称")
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, comment="指标值")
    unit: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'index'"), comment="单位")
    frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'MONTHLY'"), comment="频率：DAILY / WEEKLY / MONTHLY / QUARTERLY",
    )
    # 三日期系统（防止未来数据泄漏）
    observation_date: Mapped[date] = mapped_column(Date, nullable=False, comment="数据所属期间日期（回测中使用此日期）")
    release_date: Mapped[date] = mapped_column(Date, nullable=False, comment="数据实际发布日期（回测中必须使用此日期避免未来泄漏）")
    revision_date: Mapped[Optional[date]] = mapped_column(Date, comment="数据修订日期")
    # 修订追踪
    previous_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(30, 10), comment="修订前值")
    revised_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(30, 10), comment="修订后值")
    is_revised: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为修订数据")
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="修订版本号")
    # 元数据
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class MacroEvent(Base, TimestampMixin):
    """宏观经济事件日历。"""

    __tablename__ = "macro_events"
    __table_args__ = (
        Index("idx_macro_events_time", "event_time"),
        Index("idx_macro_events_type", "event_type", "event_time"),
        Index("idx_macro_events_importance", "importance", "event_time"),
        {"comment": "宏观经济事件日历"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    event_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="事件类型：FOMC / CPI / NFP / GDP / PCE / RATE_DECISION",
    )
    event_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="事件名称")
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="事件时间")
    importance: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default=text("'MEDIUM'"), comment="重要性：LOW / MEDIUM / HIGH / CRITICAL",
    )
    actual_value: Mapped[Optional[str]] = mapped_column(String(50), comment="实际值")
    forecast_value: Mapped[Optional[str]] = mapped_column(String(50), comment="预测值")
    previous_value: Mapped[Optional[str]] = mapped_column(String(50), comment="前值")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="事件描述")
    market_impact: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="市场影响 JSON",
    )
    btc_price_at_event: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件发生时 BTC 价格")
    btc_price_1h_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件后1小时 BTC 价格")
    btc_price_24h_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="事件后24小时 BTC 价格")
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否周期性事件")
    recurring_pattern: Mapped[Optional[str]] = mapped_column(String(100), comment="周期模式描述")
    source_url: Mapped[Optional[str]] = mapped_column(Text, comment="数据来源 URL")
