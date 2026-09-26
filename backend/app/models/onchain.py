"""链上数据 ORM 模型。

包含：onchain_metrics, exchange_flows, address_metrics, supply_metrics
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin
from .enums import QualityStatus, quality_status_enum


class OnchainMetric(Base, CreatedAtMixin):
    """链上指标时序数据（通用结构）。"""

    __tablename__ = "onchain_metrics"
    __table_args__ = (
        UniqueConstraint("metric_name", "observation_time", "source_id", name="idx_onchain_unique"),
        Index("idx_onchain_metric", "metric_name", "observation_time"),
        Index("idx_onchain_source", "source_id", "observation_time"),
        {"comment": "链上指标时序数据（通用结构）"},
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
    metric_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="指标名称：MVRV / SOPR / aSOPR / NUPL / PUELL_MULTIPLE / RHODL / RESERVE_RISK 等",
    )
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, comment="指标值")
    unit: Mapped[Optional[str]] = mapped_column(String(30), server_default=text("'ratio'"), comment="单位：ratio / usd / btc / count / percent")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class ExchangeFlow(Base, CreatedAtMixin):
    """交易所 BTC 资金流数据。"""

    __tablename__ = "exchange_flows"
    __table_args__ = (
        Index("idx_exchange_flows_name", "exchange_name", "observation_time"),
        Index("idx_exchange_flows_type", "flow_type", "observation_time"),
        {"comment": "交易所 BTC 资金流数据"},
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
    exchange_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="交易所名称")
    flow_type: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'NET'"), comment="流量类型：INFLOW / OUTFLOW / NET / ALL")
    inflow_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="流入 BTC 数量")
    outflow_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="流出 BTC 数量")
    net_flow_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="净流量 BTC")
    inflow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="流入 USD 金额")
    outflow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="流出 USD 金额")
    net_flow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="净流量 USD")
    exchange_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="交易所 BTC 余额")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class AddressMetric(Base, CreatedAtMixin):
    """地址活跃指标。"""

    __tablename__ = "address_metrics"
    __table_args__ = (
        UniqueConstraint("metric_name", "cohort", "observation_time", name="idx_address_metrics_unique"),
        Index("idx_address_metrics_name", "metric_name", "observation_time"),
        {"comment": "地址活跃指标"},
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
    metric_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="ACTIVE_ADDRESSES / NEW_ADDRESSES / TRANSACTION_COUNT / COIN_DAYS_DESTROYED",
    )
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, comment="指标值")
    cohort: Mapped[Optional[str]] = mapped_column(String(50), comment="地址群体：ALL / NEW / LTH / STH / WHALE")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class SupplyMetric(Base, CreatedAtMixin):
    """BTC 供应分布指标（HODL Waves 等）。"""

    __tablename__ = "supply_metrics"
    __table_args__ = (
        UniqueConstraint("metric_name", "supply_cohort", "observation_time", name="idx_supply_metrics_unique"),
        Index("idx_supply_metrics_name", "metric_name", "observation_time"),
        {"comment": "BTC 供应分布指标（HODL Waves 等）"},
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
    metric_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="LTH_SUPPLY / STH_SUPPLY / HODL_WAVES / SUPPLY_LAST_ACTIVE / DORMANCY",
    )
    value: Mapped[Decimal] = mapped_column(Numeric(30, 10), nullable=False, comment="指标值")
    supply_cohort: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'ALL'"),
        comment="供应群体：LTH / STH / EXCHANGE / MINER / LOST / 1D_1W / 1W_1M 等",
    )
    total_supply: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="总供应量")
    pct_of_supply: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="占总供应百分比")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )
