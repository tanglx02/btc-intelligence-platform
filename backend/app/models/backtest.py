"""回测相关 ORM 模型。

包含：strategies, strategy_versions, backtest_runs, backtest_results, backtest_trades
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import BacktestStatus, TradeSide, backtest_status_enum, trade_side_enum


class Strategy(Base, TimestampMixin):
    """策略定义表。"""

    __tablename__ = "strategies"
    __table_args__ = (
        Index("idx_strategies_category", "category", postgresql_where=text("is_active = true")),
        {"comment": "策略定义表"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="策略名称")
    name_cn: Mapped[Optional[str]] = mapped_column(String(100), comment="策略中文名称")
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'DCA'"),
        comment="策略分类：DCA / VALUE_AVERAGING / SIGNAL_BASED / RISK_ADJUSTED / CYCLE_BASED / CUSTOM",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="英文描述")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    parameters_schema: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="参数 Schema")
    rules_description: Mapped[Optional[str]] = mapped_column(Text, comment="规则描述")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否激活")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为系统内置策略")
    tags: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="标签列表")
    created_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="创建者用户 ID",
    )

    # Relationships
    versions: Mapped[list["StrategyVersion"]] = relationship(back_populates="strategy", lazy="selectin")


class StrategyVersion(Base, CreatedAtMixin):
    """策略版本管理。"""

    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint("strategy_id", "version", name="uq_strategy_versions_sid_version"),
        Index("idx_strategy_versions_sid", "strategy_id", "created_at"),
        Index("idx_strategy_versions_current", "strategy_id", postgresql_where=text("is_current = true")),
        {"comment": "策略版本管理"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False, comment="策略 ID",
    )
    version: Mapped[str] = mapped_column(String(50), nullable=False, comment="版本号")
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="策略参数 JSON")
    rules_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="加仓/减仓规则配置")
    changelog: Mapped[Optional[str]] = mapped_column(Text, comment="变更日志")
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为当前版本")
    created_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="创建者用户 ID",
    )

    # Relationships
    strategy: Mapped["Strategy"] = relationship(back_populates="versions")


class BacktestRun(Base, TimestampMixin):
    """回测运行记录（主表）。"""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("idx_backtest_user", "user_id", "created_at"),
        Index("idx_backtest_strategy", "strategy_version_id", "created_at"),
        Index("idx_backtest_status", "status", postgresql_where=text("status IN ('PENDING', 'RUNNING')")),
        Index("idx_backtest_period", "period_start", "period_end"),
        {"comment": "回测运行记录（主表）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    strategy_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("strategy_versions.id"), nullable=False, comment="策略版本 ID",
    )
    model_version_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), comment="模型版本 ID",
    )
    user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="用户 ID",
    )
    # 状态
    status: Mapped[BacktestStatus] = mapped_column(
        backtest_status_enum, nullable=False, server_default=text("'PENDING'"), comment="回测状态",
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="开始时间")
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="完成时间")
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, comment="运行时长（秒）")
    error_message: Mapped[Optional[str]] = mapped_column(Text, comment="错误信息")
    # 回测参数
    period_start: Mapped[date] = mapped_column(Date, nullable=False, comment="回测区间开始")
    period_end: Mapped[date] = mapped_column(Date, nullable=False, comment="回测区间结束")
    run_params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="运行参数")
    initial_conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="初始条件")
    # 结果摘要
    total_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="总收益率")
    annual_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="年化收益率")
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="最大回撤")
    max_drawdown_duration_days: Mapped[Optional[int]] = mapped_column(Integer, comment="最大回撤持续天数")
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="夏普比率")
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="索提诺比率")
    calmar_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="卡尔玛比率")
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="胜率")
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="盈亏比")
    total_trades: Mapped[Optional[int]] = mapped_column(Integer, comment="总交易次数")
    avg_trade_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 4), comment="平均交易盈亏")
    # 资金结果
    initial_capital: Mapped[Decimal] = mapped_column(Numeric(16, 2), nullable=False, comment="初始资金")
    total_invested: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="总投入")
    final_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="最终价值")
    total_fees: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 2), comment="总手续费")
    btc_accumulated: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="累计 BTC")
    avg_cost_basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="平均成本")
    # 扩展
    summary_metrics: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="扩展指标 JSON")
    yearly_returns: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="年度收益率")
    benchmark_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="基准收益率")
    alpha: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="Alpha")
    # 数据快照
    data_snapshot_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="数据快照时间（确保回测可复现）",
    )

    # Relationships
    results: Mapped[list["BacktestResult"]] = relationship(back_populates="run", lazy="selectin")
    trades: Mapped[list["BacktestTrade"]] = relationship(back_populates="run", lazy="selectin")


class BacktestResult(Base):
    """回测结果详情（每个时间点的组合净值）。"""

    __tablename__ = "backtest_results"
    __table_args__ = (
        Index("idx_backtest_results_run", "backtest_run_id", "observation_time"),
        {"comment": "回测结果详情（每个时间点的组合净值）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    backtest_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="回测运行 ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时间",
    )
    # 组合状态
    portfolio_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, comment="组合总市值（现金 + BTC）")
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, server_default=text("0"), comment="现金余额")
    btc_holdings: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, server_default=text("0"), comment="BTC 持仓量")
    btc_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="BTC 价格")
    # 成本与收益
    avg_cost: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="平均成本")
    unrealized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="浮动盈亏")
    realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), server_default=text("0"), comment="已实现盈亏")
    cumulative_invested: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="累计投入")
    # 风险指标
    drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 6), comment="当前回撤比例")
    drawdown_duration: Mapped[Optional[int]] = mapped_column(Integer, comment="回撤持续天数")
    daily_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 6), comment="日收益率")
    cumulative_return: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="累计收益率")
    # 快照
    metrics_snapshot: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="指标快照")

    # Relationships
    run: Mapped["BacktestRun"] = relationship(back_populates="results")


class BacktestTrade(Base):
    """回测交易明细。"""

    __tablename__ = "backtest_trades"
    __table_args__ = (
        Index("idx_backtest_trades_run", "backtest_run_id", "observation_time"),
        Index("idx_backtest_trades_reason", "trigger_reason", "observation_time"),
        {"comment": "回测交易明细"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    backtest_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, comment="回测运行 ID",
    )
    trade_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, comment="交易时间")
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="观测时间",
    )
    # 交易信息
    trade_number: Mapped[int] = mapped_column(Integer, nullable=False, comment="交易序号")
    side: Mapped[TradeSide] = mapped_column(trade_side_enum, nullable=False, comment="交易方向")
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交价格")
    quantity_btc: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, comment="成交数量（BTC）")
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False, comment="成交金额")
    fee: Mapped[Decimal] = mapped_column(Numeric(16, 6), nullable=False, server_default=text("0"), comment="手续费")
    slippage: Mapped[Decimal] = mapped_column(Numeric(16, 6), nullable=False, server_default=text("0"), comment="滑点")
    # 上下文
    trigger_reason: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="触发原因：SCHEDULED_DCA / DRAWDOWN_BUY / VALUATION_BUY / SIGNAL_BUY / STOP_LOSS",
    )
    trigger_details: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="触发详情")
    market_context: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="市场上下文")
    # 结果
    pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(16, 4), comment="盈亏")
    cumulative_btc: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="累计 BTC")
    cumulative_invested: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="累计投入")
    avg_cost_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="交易后平均成本")
    portfolio_after: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 4), comment="交易后组合价值")

    # Relationships
    run: Mapped["BacktestRun"] = relationship(back_populates="trades")
