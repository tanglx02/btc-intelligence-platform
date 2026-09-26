"""指标定义与计算结果 ORM 模型。

包含：indicator_definitions, indicator_values
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import QualityStatus, quality_status_enum


class IndicatorDefinition(Base, TimestampMixin):
    """指标定义字典（技术指标、链上指标、衍生指标）。"""

    __tablename__ = "indicator_definitions"
    __table_args__ = (
        Index("idx_indicator_def_category", "category", "is_active"),
        Index("idx_indicator_def_primary", "sort_order", postgresql_where=text("is_primary = true")),
        {"comment": "指标定义字典（技术指标、链上指标、衍生指标）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, comment="指标代码：RSI / MACD / MVRV / SOPR 等")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="指标英文名称")
    name_cn: Mapped[str] = mapped_column(String(100), nullable=False, comment="指标中文名称")
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="分类：TECHNICAL / ONCHAIN / DERIVATIVES / MACRO / SENTIMENT / COMPOSITE",
    )
    sub_category: Mapped[Optional[str]] = mapped_column(String(50), comment="子分类")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="英文描述")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    formula: Mapped[Optional[str]] = mapped_column(Text, comment="计算公式文本描述")
    formula_latex: Mapped[Optional[str]] = mapped_column(Text, comment="LaTeX 公式")
    unit: Mapped[Optional[str]] = mapped_column(String(30), server_default=text("'value'"), comment="单位")
    value_range: Mapped[Optional[str]] = mapped_column(String(50), comment="值域范围描述")
    frequency: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'1d'"), comment="计算频率")
    params_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="参数 schema JSON Schema")
    default_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="默认参数")
    display_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="前端展示配置")
    interpretation: Mapped[Optional[dict]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb"), comment="解读说明 JSON",
    )
    data_dependencies: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="数据依赖列表")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否激活")
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为首页核心指标")
    sort_order: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("0"), comment="排序权重")

    # Relationships
    values: Mapped[list["IndicatorValue"]] = relationship(back_populates="definition", lazy="selectin")


class IndicatorValue(Base, CreatedAtMixin):
    """指标计算结果时序数据。"""

    __tablename__ = "indicator_values"
    __table_args__ = (
        UniqueConstraint("indicator_id", "symbol", "observation_time", name="idx_indicator_values_unique"),
        Index("idx_indicator_values_ind", "indicator_id", "observation_time"),
        Index("idx_indicator_values_symbol", "symbol", "observation_time"),
        {"comment": "指标计算结果时序数据"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    indicator_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("indicator_definitions.id"), nullable=False, comment="指标定义 ID",
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
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTC'"), comment="资产符号")
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, comment="指标原始值")
    normalized_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="标准化值（0-1 或 Z-score）")
    percentile: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="历史分位数（0-100）")
    signal: Mapped[Optional[str]] = mapped_column(
        String(20), comment="信号：BULLISH / BEARISH / NEUTRAL / OVERBOUGHT / OVERSOLD",
    )
    params_used: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="本次计算使用的参数")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )

    # Relationships
    definition: Mapped["IndicatorDefinition"] = relationship(back_populates="values")
