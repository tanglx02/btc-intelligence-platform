"""原始数据响应存储 ORM 模型。

包含：raw_market_data, raw_onchain_data, raw_etf_data,
      raw_derivatives_data, raw_macro_data, raw_sentiment_data
所有表均保存完整 API 原始响应 JSON，支持数据重放与审计。
"""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin
from .enums import QualityStatus, quality_status_enum


class RawMarketData(Base, CreatedAtMixin):
    """市场行情 API 原始响应存储。"""

    __tablename__ = "raw_market_data"
    __table_args__ = (
        Index("idx_raw_market_source", "source_id", "fetch_time"),
        Index("idx_raw_market_symbol", "symbol", "data_type", "fetch_time"),
        {"comment": "市场行情 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTC'"), comment="资产符号")
    data_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="数据类型：PRICE / OHLCV / VOLUME / ORDERBOOK / TRADES")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    response_headers: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="响应头")
    response_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, comment="响应大小（字节）")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class RawOnchainData(Base, CreatedAtMixin):
    """链上数据 API 原始响应存储。"""

    __tablename__ = "raw_onchain_data"
    __table_args__ = (
        Index("idx_raw_onchain_source", "source_id", "fetch_time"),
        Index("idx_raw_onchain_metric", "metric_name", "fetch_time"),
        {"comment": "链上数据 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="链上指标名称：MVRV / SOPR / NUPL 等")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class RawEtfData(Base, CreatedAtMixin):
    """ETF 数据 API 原始响应存储。"""

    __tablename__ = "raw_etf_data"
    __table_args__ = (
        Index("idx_raw_etf_source", "source_id", "fetch_time"),
        Index("idx_raw_etf_ticker", "ticker", "fetch_time"),
        {"comment": "ETF 数据 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False, comment="ETF 代码：IBIT / FBTC / GBTC 等")
    data_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default=text("'FLOW'"), comment="FLOW / HOLDINGS / NAV / PRICE")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class RawDerivativesData(Base, CreatedAtMixin):
    """衍生品数据 API 原始响应存储。"""

    __tablename__ = "raw_derivatives_data"
    __table_args__ = (
        Index("idx_raw_deriv_source", "source_id", "fetch_time"),
        Index("idx_raw_deriv_type", "exchange", "data_type", "fetch_time"),
        {"comment": "衍生品数据 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'BTC'"), comment="交易对符号")
    exchange: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所名称")
    data_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="FUNDING / OPEN_INTEREST / LIQUIDATION / LONG_SHORT / BASIS")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class RawMacroData(Base, CreatedAtMixin):
    """宏观数据 API 原始响应存储。"""

    __tablename__ = "raw_macro_data"
    __table_args__ = (
        Index("idx_raw_macro_series", "series_id", "fetch_time"),
        Index("idx_raw_macro_source", "source_id", "fetch_time"),
        {"comment": "宏观数据 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    series_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="宏观数据系列 ID：DXY / FED_RATE / CPI 等")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )


class RawSentimentData(Base, CreatedAtMixin):
    """情绪数据 API 原始响应存储。"""

    __tablename__ = "raw_sentiment_data"
    __table_args__ = (
        Index("idx_raw_sentiment_source", "source_id", "fetch_time"),
        Index("idx_raw_sentiment_type", "source_type", "fetch_time"),
        {"comment": "情绪数据 API 原始响应存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), nullable=False, comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="数据观测时间")
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="数据拉取时间",
    )
    source_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="来源类型：FEAR_GREED / GOOGLE_TRENDS / SOCIAL / NEWS")
    raw_response: Mapped[dict] = mapped_column(JSONB, nullable=False, comment="完整 API 响应 JSON")
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, comment="请求端点")
    request_params: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="请求参数")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
