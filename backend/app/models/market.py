"""标准化市场数据 ORM 模型。

包含：assets, market_prices, candles, orderbooks, trades
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import (
    CandleInterval,
    QualityStatus,
    TradeSide,
    candle_interval_enum,
    quality_status_enum,
    trade_side_enum,
)


class Asset(Base, TimestampMixin):
    """资产定义字典，系统支持的所有资产元数据。"""

    __tablename__ = "assets"
    __table_args__ = (
        {"comment": "资产定义字典，系统支持的所有资产元数据"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, comment="资产符号，如 BTC、ETH")
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="资产名称")
    asset_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'CRYPTO'"), comment="资产类型：CRYPTO / FIAT / COMMODITY / TOKEN")
    chain: Mapped[Optional[str]] = mapped_column(String(30), server_default=text("'bitcoin'"), comment="所属链：bitcoin / ethereum 等")
    decimals: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("8"), comment="精度位数")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否激活")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="扩展元数据")


class MarketPrice(Base, CreatedAtMixin):
    """标准化市场价格数据（核心表）。"""

    __tablename__ = "market_prices"
    __table_args__ = (
        Index("idx_prices_symbol_time", "symbol", "observation_time"),
        Index("idx_prices_source", "source_id", "observation_time"),
        Index("idx_prices_quality", "observation_time", postgresql_where=text("quality_status != 'VERIFIED'")),
        {"comment": "标准化市场价格数据（核心表）"},
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
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTCUSDT'"), comment="交易对：BTCUSDT / BTCUSD")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="最新成交价格（USD/USDT）")
    bid: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="买一价")
    ask: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="卖一价")
    spread: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="买卖价差")
    volume_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="24h 成交量")
    quote_volume_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="24h 成交额")
    market_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 2), comment="市值")
    high_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="24h 最高价")
    low_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="24h 最低价")
    price_change_pct_24h: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="24h 涨跌幅（%）")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"),
        comment="数据质量：VERIFIED / CONFLICT / STALE",
    )
    cross_validated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否经过多源交叉验证")
    validation_sources: Mapped[Optional[int]] = mapped_column(Integer, server_default=text("1"), comment="验证数据源数量")
    deviation_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), comment="多源价格偏差百分比")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class Candle(Base, CreatedAtMixin):
    """OHLCV K线数据（多时间粒度）。"""

    __tablename__ = "candles"
    __table_args__ = (
        UniqueConstraint("symbol", "interval", "observation_time", name="idx_candles_unique"),
        Index("idx_candles_symbol_interval", "symbol", "interval", "observation_time"),
        Index("idx_candles_source", "source_id", "observation_time"),
        {"comment": "OHLCV K线数据（多时间粒度）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="K线开始时间",
    )
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTCUSDT'"), comment="交易对")
    interval: Mapped[CandleInterval] = mapped_column(candle_interval_enum, nullable=False, comment="K线时间间隔")
    open: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="开盘价")
    high: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="最高价")
    low: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="最低价")
    close: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="收盘价")
    volume: Mapped[Decimal] = mapped_column(Numeric(24, 4), nullable=False, server_default=text("0"), comment="成交量")
    quote_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="成交额")
    trades: Mapped[Optional[int]] = mapped_column(Integer, comment="成交笔数")
    taker_buy_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="主动买入成交量")
    taker_sell_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="主动卖出成交量")
    vwap: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="成交量加权平均价格")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class Orderbook(Base, CreatedAtMixin):
    """订单簿深度快照（top 50 档位）。"""

    __tablename__ = "orderbooks"
    __table_args__ = (
        Index("idx_orderbooks_symbol", "symbol", "exchange", "observation_time"),
        {"comment": "订单簿深度快照（top 50 档位）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="快照时间",
    )
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTCUSDT'"), comment="交易对")
    exchange: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所名称")
    bids: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="买盘 [[price, quantity], ...]")
    asks: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="卖盘 [[price, quantity], ...]")
    bid_depth: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="买盘总深度")
    ask_depth: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="卖盘总深度")
    spread: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="买卖价差")
    spread_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="价差百分比")
    mid_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="中间价")
    imbalance: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="买卖不平衡度")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class Trade(Base, CreatedAtMixin):
    """逐笔成交数据（高容量，短期保留）。"""

    __tablename__ = "trades"
    __table_args__ = (
        Index("idx_trades_symbol", "symbol", "exchange", "observation_time"),
        {"comment": "逐笔成交数据（高容量，短期保留）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="成交时间",
    )
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTCUSDT'"), comment="交易对")
    exchange: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所名称")
    trade_id: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所成交 ID")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交价格")
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交数量")
    quote_quantity: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="成交金额")
    side: Mapped[TradeSide] = mapped_column(trade_side_enum, nullable=False, comment="买方/卖方方向")
    is_buyer_maker: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="买方是否为 Maker")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
