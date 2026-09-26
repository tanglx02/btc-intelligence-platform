"""ETF 数据 ORM 模型。

包含：etf_flows, etf_holdings
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


class EtfFlow(Base, CreatedAtMixin):
    """BTC ETF 每日资金流数据。"""

    __tablename__ = "etf_flows"
    __table_args__ = (
        UniqueConstraint("ticker", "observation_time", name="idx_etf_flows_unique"),
        Index("idx_etf_flows_ticker", "ticker", "observation_time"),
        Index("idx_etf_flows_time", "observation_time"),
        {"comment": "BTC ETF 每日资金流数据"},
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
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, comment="ETF 代码：IBIT / FBTC / GBTC / ARKB / BITB 等")
    fund_name: Mapped[Optional[str]] = mapped_column(String(100), comment="基金名称")
    daily_inflow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="当日流入（USD）")
    daily_outflow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="当日流出（USD）")
    net_flow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="当日净流入（USD），正数为流入")
    cumulative_flow_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="累计净流入")
    total_holdings_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="总持仓 BTC 数量")
    total_aum_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="总管理资产（USD）")
    daily_volume_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="当日成交量（USD）")
    price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="ETF 价格")
    nav: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="净值")
    premium_discount: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="溢价/折价率")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class EtfHolding(Base, CreatedAtMixin):
    """BTC ETF 持仓快照。"""

    __tablename__ = "etf_holdings"
    __table_args__ = (
        UniqueConstraint("ticker", "observation_time", name="idx_etf_holdings_unique"),
        Index("idx_etf_holdings_ticker", "ticker", "observation_time"),
        {"comment": "BTC ETF 持仓快照"},
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
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, comment="ETF 代码")
    fund_name: Mapped[Optional[str]] = mapped_column(String(100), comment="基金名称")
    holdings_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="持有 BTC 数量")
    holdings_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="持有 BTC 价值（USD）")
    shares_outstanding: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="流通份额")
    nav_per_share: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="每份净值")
    market_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="市场价格")
    premium_discount: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="溢价/折价率")
    daily_change_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="当日持仓变化")
    custodian: Mapped[Optional[str]] = mapped_column(String(100), comment="托管方")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )
