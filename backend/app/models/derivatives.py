"""衍生品数据 ORM 模型。

包含：derivatives, options_data
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin
from .enums import QualityStatus, quality_status_enum


class Derivative(Base, CreatedAtMixin):
    """衍生品综合数据（资金费率/OI/清算/多空比/基差）。"""

    __tablename__ = "derivatives"
    __table_args__ = (
        Index("idx_derivatives_type", "data_type", "symbol", "observation_time"),
        Index("idx_derivatives_exchange", "exchange", "symbol", "observation_time"),
        Index("idx_derivatives_symbol", "symbol", "observation_time"),
        {"comment": "衍生品综合数据（资金费率/OI/清算/多空比/基差）"},
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
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'BTCUSDT'"), comment="交易对符号")
    exchange: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所名称")
    data_type: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="数据类型：FUNDING / OPEN_INTEREST / LIQUIDATION / LONG_SHORT / BASIS / CVD",
    )
    # 资金费率
    funding_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 10), comment="当期资金费率")
    funding_rate_next: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 10), comment="下期预测资金费率")
    funding_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="下次结算时间")
    # 未平仓合约
    open_interest: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="未平仓合约量（BTC）")
    open_interest_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 2), comment="未平仓合约价值（USD）")
    open_interest_change: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="OI 变化率")
    # 多空比
    long_short_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="多空持仓比")
    long_account_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="多空账户比")
    # 清算数据
    liquidation_long_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="多头清算金额（USD）")
    liquidation_short_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="空头清算金额（USD）")
    liquidation_total_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="总清算金额（USD）")
    liquidation_count: Mapped[Optional[int]] = mapped_column(Integer, comment="清算笔数")
    # 基差
    basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="基差（期货价格 - 现货价格）")
    basis_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="基差百分比")
    premium_index: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 8), comment="溢价指数")
    # CVD
    cvd: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="累计成交量差（CVD）")
    taker_buy_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="主动买入量")
    taker_sell_volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 4), comment="主动卖出量")
    # 通用
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class OptionsData(Base, CreatedAtMixin):
    """期权链数据（Deribit 等交易所）。"""

    __tablename__ = "options_data"
    __table_args__ = (
        UniqueConstraint(
            "exchange", "expiry_date", "strike_price", "option_type", "observation_time",
            name="idx_options_unique",
        ),
        Index("idx_options_expiry", "expiry_date", "observation_time"),
        Index("idx_options_exchange", "exchange", "observation_time"),
        CheckConstraint("option_type IN ('CALL', 'PUT')", name="ck_options_type"),
        {"comment": "期权链数据（Deribit 等交易所）"},
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
    exchange: Mapped[str] = mapped_column(String(50), nullable=False, comment="交易所名称")
    underlying: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTC'"), comment="标的资产")
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, comment="到期日")
    strike_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, comment="行权价")
    option_type: Mapped[str] = mapped_column(String(4), nullable=False, comment="期权类型：CALL / PUT")
    # 量价
    open_interest: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="未平仓合约量")
    volume: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="成交量")
    last_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="最新价")
    bid_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="买价")
    ask_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 4), comment="卖价")
    # Greeks
    implied_volatility: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="隐含波动率")
    delta: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="Delta")
    gamma: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 8), comment="Gamma")
    theta: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 8), comment="Theta")
    vega: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="Vega")
    rho: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="Rho")
    # 汇总指标
    put_call_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="Put/Call 比率")
    iv_skew: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="IV 偏斜")
    total_oi_usd: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 2), comment="总 OI（USD）")
    max_pain: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), comment="最大痛点价格")
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )
