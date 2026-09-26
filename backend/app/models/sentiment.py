"""情绪数据 ORM 模型。

包含：sentiment
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin
from .enums import QualityStatus, quality_status_enum


class Sentiment(Base, CreatedAtMixin):
    """市场情绪指标时序数据。"""

    __tablename__ = "sentiment"
    __table_args__ = (
        UniqueConstraint("source_type", "metric_name", "observation_time", name="idx_sentiment_unique"),
        Index("idx_sentiment_source", "source_type", "observation_time"),
        Index("idx_sentiment_metric", "metric_name", "observation_time"),
        Index("idx_sentiment_label", "sentiment_label", "observation_time"),
        {"comment": "市场情绪指标时序数据"},
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
    source_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="来源类型：FEAR_GREED / GOOGLE_TRENDS / SOCIAL_TWITTER / SOCIAL_REDDIT / NEWS",
    )
    metric_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="指标名称：fear_greed_index / btc_search_volume / social_volume 等",
    )
    value: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, comment="原始值（各源量纲不同）")
    normalized_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="标准化值（0-100 统一量纲）")
    sentiment_label: Mapped[Optional[str]] = mapped_column(
        String(30), comment="情绪标签：EXTREME_FEAR / FEAR / NEUTRAL / GREED / EXTREME_GREED",
    )
    sample_size: Mapped[Optional[int]] = mapped_column(Integer, comment="样本量（用于评估可信度）")
    confidence: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 4), comment="置信度 0-1")
    previous_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="前值")
    change_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="变化百分比")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )
