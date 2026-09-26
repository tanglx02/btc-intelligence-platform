"""个人资金计划与资产账本 Pydantic 模式。

对应 15-portfolio-architecture.md：计划（Plan）、交易账本（Ledger）、
持仓聚合（Holdings）与绩效（Performance）的 API 输入/输出契约。

约定：
- 全部金额字段使用 Decimal（禁止 float 承载金额）；
- 输入模式（Create*/Update*）做严格校验，输出模式（*Response）与 ORM 对齐；
- 收益率/回撤等比率字段统一为小数口径（0.35 = 35%），由前端负责百分号展示。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 支持的计划状态流转与取值域
PLAN_TYPES = {"DCA", "VALUE_AVERAGING", "SIGNAL_BASED", "CUSTOM"}
FREQUENCIES = {"DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"}
TRANSACTION_TYPES = {"DCA_INVEST", "MANUAL_BUY", "MANUAL_SELL", "PLAN_TRIGGER", "IMPORT"}
CURRENCIES = {"CNY", "USD"}


# ---------------------------------------------------------------------------
# 计划
# ---------------------------------------------------------------------------

class CreatePlanSchema(BaseModel):
    """创建资金计划输入。"""

    plan_name: str = Field(min_length=1, max_length=100, description="计划名称")
    plan_type: str = Field(default="DCA", description="计划类型")
    initial_capital: Decimal = Field(default=Decimal("0"), ge=0, description="初始资金（0=纯定投）")
    currency: str = Field(default="CNY", description="计价货币 CNY/USD")
    periodic_amount: Decimal = Field(default=Decimal("0"), ge=0, description="每期定投金额")
    periodic_frequency: str = Field(default="MONTHLY", description="定投频率")
    monthly_income: Decimal | None = Field(default=None, ge=0, description="月收入（仅提示用）")
    start_date: date = Field(description="开始日期")
    end_date: date | None = Field(default=None, description="结束日期（空=无限期）")
    investment_horizon_years: int | None = Field(default=None, ge=1, le=100)
    target_asset: str = Field(default="BTC", max_length=20)
    cash_reserve: Decimal | None = Field(default=Decimal("0"), ge=0, description="现金储备下限")
    cash_reserve_pct: Decimal | None = Field(default=Decimal("10"), ge=0, le=100)
    max_single_investment: Decimal | None = Field(
        default=None, gt=0, description="单次最大投入上限"
    )
    dca_rules: dict[str, Any] = Field(
        default_factory=lambda: {"type": "fixed", "multiplier_rules": []},
        description="定投规则 JSON（dca_simulator 策略配置）",
    )
    risk_params: dict[str, Any] = Field(default_factory=dict)
    max_drawdown_tolerance: Decimal | None = Field(default=Decimal("30"), ge=0, le=100)
    stop_loss_enabled: bool = False
    stop_loss_pct: Decimal | None = Field(default=None, ge=0, le=100)
    drawdown_buy_rules: list[dict[str, Any]] | None = Field(default_factory=list)
    valuation_buy_rules: list[dict[str, Any]] | None = Field(default_factory=list)
    custom_rules: list[dict[str, Any]] | None = Field(default_factory=list)

    @field_validator("plan_type")
    @classmethod
    def _check_plan_type(cls, v: str) -> str:
        if v not in PLAN_TYPES:
            raise ValueError(f"plan_type 必须为 {sorted(PLAN_TYPES)} 之一")
        return v

    @field_validator("periodic_frequency")
    @classmethod
    def _check_frequency(cls, v: str) -> str:
        v = v.upper()
        if v not in FREQUENCIES:
            raise ValueError(f"periodic_frequency 必须为 {sorted(FREQUENCIES)} 之一")
        return v

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in CURRENCIES:
            raise ValueError(f"currency 必须为 {sorted(CURRENCIES)} 之一")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不得早于 start_date")
        if self.initial_capital == 0 and self.periodic_amount == 0:
            raise ValueError("initial_capital 与 periodic_amount 不得同时为 0")


class UpdatePlanSchema(BaseModel):
    """更新资金计划输入（全部字段可选，仅更新提供的字段）。"""

    plan_name: str | None = Field(default=None, min_length=1, max_length=100)
    plan_type: str | None = None
    initial_capital: Decimal | None = Field(default=None, ge=0)
    currency: str | None = None
    periodic_amount: Decimal | None = Field(default=None, ge=0)
    periodic_frequency: str | None = None
    monthly_income: Decimal | None = Field(default=None, ge=0)
    start_date: date | None = None
    end_date: date | None = None
    investment_horizon_years: int | None = Field(default=None, ge=1, le=100)
    target_asset: str | None = None
    cash_reserve: Decimal | None = Field(default=None, ge=0)
    cash_reserve_pct: Decimal | None = Field(default=None, ge=0, le=100)
    max_single_investment: Decimal | None = Field(default=None, gt=0)
    dca_rules: dict[str, Any] | None = None
    risk_params: dict[str, Any] | None = None
    max_drawdown_tolerance: Decimal | None = Field(default=None, ge=0, le=100)
    stop_loss_enabled: bool | None = None
    stop_loss_pct: Decimal | None = Field(default=None, ge=0, le=100)
    drawdown_buy_rules: list[dict[str, Any]] | None = None
    valuation_buy_rules: list[dict[str, Any]] | None = None
    custom_rules: list[dict[str, Any]] | None = None


class PlanResponse(BaseModel):
    """资金计划输出。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    plan_name: str
    plan_type: str
    initial_capital: Decimal
    currency: str
    periodic_amount: Decimal
    periodic_frequency: str
    monthly_income: Decimal | None = None
    start_date: date
    end_date: date | None = None
    investment_horizon_years: int | None = None
    target_asset: str
    cash_reserve: Decimal | None = None
    cash_reserve_pct: Decimal | None = None
    max_single_investment: Decimal | None = None
    dca_rules: dict[str, Any]
    risk_params: dict[str, Any]
    max_drawdown_tolerance: Decimal | None = None
    stop_loss_enabled: bool
    stop_loss_pct: Decimal | None = None
    drawdown_buy_rules: list[Any] | None = None
    valuation_buy_rules: list[Any] | None = None
    custom_rules: list[Any] | None = None
    status: str
    current_phase: str | None = None
    next_investment_date: date | None = None
    total_invested: Decimal
    total_btc: Decimal
    avg_cost: Decimal
    last_investment_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# 交易账本
# ---------------------------------------------------------------------------

class CreateTransactionSchema(BaseModel):
    """录入交易记录输入（手工记账 / 计划触发 / 导入）。"""

    plan_id: UUID | None = Field(default=None, description="关联计划（手工记账也须归属计划）")
    transaction_time: datetime | None = Field(default=None, description="成交时间（默认当前）")
    transaction_type: str = Field(default="MANUAL_BUY", description="交易类型")
    side: str = Field(description="BUY / SELL")
    amount: Decimal | None = Field(
        default=None, gt=0, description="法币金额（与 quantity_btc 二选一必填）"
    )
    price: Decimal | None = Field(
        default=None, gt=0, description="成交价（缺省时须传 quantity_btc）"
    )
    quantity_btc: Decimal | None = Field(default=None, gt=0, description="BTC 数量")
    currency: str = Field(default="CNY")
    fee: Decimal = Field(default=Decimal("0"), ge=0)
    fee_currency: str | None = "CNY"
    source: str = Field(default="MANUAL", description="MANUAL / PLAN_AUTO / EXCHANGE_IMPORT / API")
    exchange_name: str | None = None
    order_id: str | None = None
    notes: str | None = None
    tags: list[str] | None = None

    @field_validator("transaction_type")
    @classmethod
    def _check_type(cls, v: str) -> str:
        if v not in TRANSACTION_TYPES:
            raise ValueError(f"transaction_type 必须为 {sorted(TRANSACTION_TYPES)} 之一")
        return v

    @field_validator("side")
    @classmethod
    def _check_side(cls, v: str) -> str:
        v = v.upper()
        if v not in {"BUY", "SELL"}:
            raise ValueError("side 必须为 BUY 或 SELL")
        return v

    def model_post_init(self, __context: Any) -> None:
        if self.amount is None and self.quantity_btc is None:
            raise ValueError("amount 与 quantity_btc 至少提供一个")
        if self.amount is not None and self.quantity_btc is None and self.price is None:
            raise ValueError("仅提供 amount 时必须同时提供 price 以推算 BTC 数量")
        if self.quantity_btc is not None and self.amount is None and self.price is None:
            raise ValueError("仅提供 quantity_btc 时必须同时提供 price 以推算金额")


class TransactionResponse(BaseModel):
    """交易记录输出。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    plan_id: UUID | None = None
    transaction_time: datetime
    transaction_type: str
    side: str
    amount: Decimal
    currency: str
    price: Decimal
    quantity_btc: Decimal
    fee: Decimal
    fee_currency: str | None = None
    cumulative_invested: Decimal | None = None
    cumulative_btc: Decimal | None = None
    avg_cost_after: Decimal | None = None
    source: str
    exchange_name: str | None = None
    order_id: str | None = None
    notes: str | None = None
    tags: list[str] | None = None
    created_at: datetime


# ---------------------------------------------------------------------------
# 持仓与绩效
# ---------------------------------------------------------------------------

class PortfolioHoldings(BaseModel):
    """用户持仓聚合（我的资产页核心卡片）。"""

    total_btc: Decimal = Field(description="当前 BTC 持仓数量")
    avg_cost: Decimal = Field(description="DCA 平均成本（移动加权，卖出不改变）")
    total_invested: Decimal = Field(description="累计净投入（含费）")
    total_cost: Decimal = Field(description="当前持仓成本 = total_btc × avg_cost")
    current_price: Decimal | None = Field(default=None, description="估值用价格")
    market_value: Decimal | None = Field(default=None, description="当前市值")
    unrealized_pnl: Decimal | None = Field(default=None, description="浮动盈亏")
    unrealized_pnl_pct: Decimal | None = Field(default=None, description="浮动盈亏率")
    realized_pnl: Decimal = Field(default=Decimal("0"), description="已实现盈亏（卖出）")
    total_buy_amount: Decimal = Field(default=Decimal("0"), description="累计买入金额")
    total_sell_amount: Decimal = Field(default=Decimal("0"), description="累计卖出金额")
    total_fees: Decimal = Field(default=Decimal("0"), description="累计手续费")
    transaction_count: int = 0


class PlanPerformance(BaseModel):
    """单计划绩效。"""

    plan_id: UUID
    total_invested: Decimal
    total_btc: Decimal
    avg_cost: Decimal
    realized_pnl: Decimal = Decimal("0")
    current_price: Decimal | None = None
    market_value: Decimal | None = None
    total_pnl: Decimal | None = Field(default=None, description="浮动 + 已实现")
    total_return_pct: Decimal | None = Field(default=None, description="收益率（小数口径）")
    max_drawdown: Decimal | None = Field(default=None, description="基于快照的最大回撤（小数口径）")
    current_drawdown: Decimal | None = Field(default=None, description="当前回撤（小数口径）")


class SnapshotResponse(BaseModel):
    """组合快照输出（资产曲线数据点）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    observation_time: datetime
    total_value: Decimal
    total_invested: Decimal
    cash_balance: Decimal
    btc_value: Decimal
    btc_holdings: Decimal
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    daily_return: Decimal | None = None
    cumulative_return: Decimal | None = None
    annualized_return: Decimal | None = None
    max_drawdown: Decimal | None = None
    current_drawdown: Decimal | None = None
    volatility_30d: Decimal | None = None
    sharpe_ratio: Decimal | None = None
    allocation: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
