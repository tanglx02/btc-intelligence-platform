"""用户与投资组合 ORM 模型。

包含：users, user_plans, user_transactions, user_holdings, portfolio_snapshots
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
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TimestampMixin
from .enums import PlanStatus, TradeSide, UserRole, plan_status_enum, trade_side_enum, user_role_enum


class User(Base, TimestampMixin):
    """用户账户表。"""

    __tablename__ = "users"
    __table_args__ = (
        Index("idx_users_email", "email"),
        Index("idx_users_role", "role", postgresql_where=text("is_active = true")),
        {"comment": "用户账户表"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    username: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, comment="用户名")
    email: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, comment="邮箱")
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False, comment="密码哈希")
    display_name: Mapped[Optional[str]] = mapped_column(String(100), comment="显示名称")
    role: Mapped[UserRole] = mapped_column(
        user_role_enum, nullable=False, server_default=text("'USER'"), comment="角色：ADMIN / ANALYST / USER",
    )
    preferences: Mapped[dict] = mapped_column(
        JSONB, nullable=False,
        server_default=text("""'{"language":"zh-CN","theme":"dark","mode":"simple"}'::jsonb"""),
        comment="用户偏好设置 JSON",
    )
    notification_config: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="通知配置 JSON")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否激活")
    is_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否已验证")
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后登录时间")
    login_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"), comment="登录次数")

    # Relationships
    plans: Mapped[list["UserPlan"]] = relationship(back_populates="user", lazy="selectin")


class UserPlan(Base, TimestampMixin):
    """用户资金计划配置。"""

    __tablename__ = "user_plans"
    __table_args__ = (
        Index("idx_user_plans_user", "user_id", "status"),
        Index("idx_user_plans_next", "next_investment_date", postgresql_where=text("status = 'ACTIVE'")),
        {"comment": "用户资金计划配置"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID",
    )
    plan_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="计划名称")
    plan_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'DCA'"), comment="计划类型：DCA / VALUE_AVERAGING / SIGNAL_BASED / CUSTOM",
    )
    # 资金配置
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="初始资金")
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'CNY'"), comment="货币单位")
    periodic_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="每期定投金额")
    periodic_frequency: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'MONTHLY'"), comment="定投频率：WEEKLY / BIWEEKLY / MONTHLY",
    )
    monthly_income: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="月收入")
    # 时间配置
    start_date: Mapped[date] = mapped_column(Date, nullable=False, comment="开始日期")
    end_date: Mapped[Optional[date]] = mapped_column(Date, comment="结束日期")
    investment_horizon_years: Mapped[Optional[int]] = mapped_column(Integer, comment="投资年限")
    # 资产配置
    target_asset: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTC'"), comment="目标资产")
    cash_reserve: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), server_default=text("0"), comment="现金储备")
    cash_reserve_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), server_default=text("10"), comment="现金储备比例（%）")
    max_single_investment: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="单次最大投资额")
    # 定投规则
    dca_rules: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("""'{"type":"fixed","multiplier_rules":[]}'::jsonb"""),
        comment="定投规则 JSON",
    )
    # 风险参数
    risk_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="风险参数 JSON")
    max_drawdown_tolerance: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), server_default=text("30"), comment="最大回撤容忍度（%）")
    stop_loss_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否启用止损")
    stop_loss_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), comment="止损百分比")
    # 加仓规则
    drawdown_buy_rules: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="回撤加仓规则")
    valuation_buy_rules: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="估值加仓规则")
    custom_rules: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="自定义规则")
    # 状态
    status: Mapped[PlanStatus] = mapped_column(
        plan_status_enum, nullable=False, server_default=text("'ACTIVE'"), comment="计划状态",
    )
    current_phase: Mapped[Optional[str]] = mapped_column(String(50), comment="当前阶段")
    next_investment_date: Mapped[Optional[date]] = mapped_column(Date, comment="下次投资日期")
    # 统计
    total_invested: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="累计投入")
    total_btc: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="累计 BTC")
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="平均成本")
    last_investment_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="最后投资时间")

    # Relationships
    user: Mapped["User"] = relationship(back_populates="plans")


class UserTransaction(Base, TimestampMixin):
    """用户交易记录。"""

    __tablename__ = "user_transactions"
    __table_args__ = (
        Index("idx_user_tx_user", "user_id", "transaction_time"),
        Index("idx_user_tx_plan", "plan_id", "transaction_time"),
        Index("idx_user_tx_type", "transaction_type", "transaction_time"),
        {"comment": "用户交易记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, comment="用户 ID",
    )
    plan_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("user_plans.id", ondelete="SET NULL"), comment="关联计划 ID",
    )
    transaction_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="交易时间",
    )
    # 交易信息
    transaction_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="交易类型：DCA_INVEST / MANUAL_BUY / MANUAL_SELL / PLAN_TRIGGER / IMPORT",
    )
    side: Mapped[TradeSide] = mapped_column(trade_side_enum, nullable=False, comment="交易方向")
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 4), nullable=False, comment="交易金额（以 currency 计价）")
    currency: Mapped[str] = mapped_column(String(10), nullable=False, server_default=text("'CNY'"), comment="货币单位")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交价格")
    quantity_btc: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交数量（BTC）")
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, server_default=text("0"), comment="手续费")
    fee_currency: Mapped[Optional[str]] = mapped_column(String(10), server_default=text("'CNY'"), comment="手续费货币")
    # 累计
    cumulative_invested: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="累计投入")
    cumulative_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="累计 BTC")
    avg_cost_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="交易后平均成本")
    # 来源
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'MANUAL'"), comment="来源：MANUAL / PLAN_AUTO / EXCHANGE_IMPORT / API",
    )
    exchange_name: Mapped[Optional[str]] = mapped_column(String(50), comment="交易所名称")
    order_id: Mapped[Optional[str]] = mapped_column(String(100), comment="订单 ID")
    # 备注
    notes: Mapped[Optional[str]] = mapped_column(Text, comment="备注")
    tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="标签")


class UserHolding(Base):
    """用户持仓快照（每日生成）。"""

    __tablename__ = "user_holdings"
    __table_args__ = (
        Index("idx_user_holdings_user", "user_id", "observation_time"),
        {"comment": "用户持仓快照（每日生成）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="用户 ID",
    )
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="快照时间",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时间",
    )
    # 持仓
    asset: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'BTC'"), comment="资产符号")
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="持仓数量")
    avg_cost: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="平均持仓成本")
    total_cost: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="总成本")
    # 市值
    current_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="当前价格")
    market_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="市值（原币）")
    market_value_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="市值（CNY）")
    # 收益
    unrealized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="浮动盈亏")
    unrealized_pnl_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="浮动盈亏百分比")
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), server_default=text("0"), comment="已实现盈亏")
    total_return_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 4), comment="总收益率（%）")
    # 元数据
    metadata_: Mapped[Optional[dict]] = mapped_column("metadata", JSONB, server_default=text("'{}'::jsonb"), comment="附加元数据")


class PortfolioSnapshot(Base):
    """用户投资组合净值快照（每日生成）。"""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        Index("idx_portfolio_user", "user_id", "observation_time"),
        {"comment": "用户投资组合净值快照（每日生成）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="用户 ID",
    )
    snapshot_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="快照时间",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时间",
    )
    # 总值
    total_value: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, comment="总价值")
    total_value_cny: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="总价值（CNY）")
    total_invested: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="累计投入")
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="现金余额")
    btc_value: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, server_default=text("0"), comment="BTC 价值")
    btc_holdings: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="BTC 持仓量")
    # 收益
    unrealized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="浮动盈亏")
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), server_default=text("0"), comment="已实现盈亏")
    daily_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="日收益率")
    cumulative_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="累计收益率")
    annualized_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="年化收益率")
    # 风险
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="最大回撤")
    current_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="当前回撤")
    volatility_30d: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="30日波动率")
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="夏普比率")
    # 分配
    allocation: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="资产分配")
    metrics: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="扩展指标 JSON")
