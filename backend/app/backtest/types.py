"""回测相关数据类型（对应 14-backtest-architecture.md）。

集中定义事件驱动/向量化回测共享的数据结构：Bar、Signal、Order、Fill、
Position、Trade、BacktestConfig、BacktestResult 等。全部金额用 Decimal，
保证可复现与精度。

事件流（§1.2）：BarEvent → IndicatorEvent → SignalEvent → OrderEvent → FillEvent。
本模块以 dataclass 承载各事件载荷。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

import polars as pl

# 精度约定（与 portfolio.calculator 对齐）
Q_BTC = Decimal("0.00000001")   # satoshi
Q_AMOUNT = Decimal("0.0001")    # 成交金额（4 位）
Q_PRICE = Decimal("0.00000001") # 价格
Q_FEE = Decimal("0.000001")     # 手续费（6 位）


class Side(str, Enum):
    """交易方向。"""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """订单类型（DCA 回测默认市价单，日级流动性充足全额成交）。"""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class FillMode(str, Enum):
    """成交价假设（§1.2）。

    NEXT_OPEN：次 Bar 开盘成交（默认，更保守，杜绝 look-ahead）；
    SAME_CLOSE：同 Bar 收盘成交（信号收盘产生并按收盘价成交，需报告标注）。
    """

    NEXT_OPEN = "NEXT_OPEN"
    SAME_CLOSE = "SAME_CLOSE"


class SlippageModel(str, Enum):
    """滑点模型（§3）。"""

    FIXED_BPS = "FIXED_BPS"   # 固定基点
    ATR = "ATR"               # ATR 比例（使用 PIT 的 ATR 值）
    NONE = "NONE"


class EngineType(str, Enum):
    """回测引擎类型（§1.4）。"""

    EVENT_DRIVEN = "EVENT_DRIVEN"
    VECTORIZED = "VECTORIZED"


class DataMode(str, Enum):
    """引擎输出数据模式（§2.3 Model Leakage 防范）。"""

    AS_RUN = "AS_RUN"          # 消费历史当日实际产生的引擎输出
    RECOMPUTED = "RECOMPUTED"  # 用指定 model_version 重算历史


# ---------------------------------------------------------------------------
# 事件载荷
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    """单根 K 线（BarEvent 载荷）。"""

    time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = Decimal("0")
    quote_volume: Decimal | None = None
    symbol: str = "BTCUSDT"
    interval: str = "1d"

    @property
    def typical_price(self) -> Decimal:
        """典型价 (H+L+C)/3。"""
        return (self.high + self.low + self.close) / Decimal(3)


@dataclass
class Signal:
    """策略信号（SignalEvent 载荷）。

    策略无状态副作用：同一输入必产生同一输出（§1.2）。
    """

    time: datetime
    direction: Side
    strength: Decimal = Decimal("1")       # 信号强度 0-1（用于缩放投入）
    target_amount: Decimal | None = None  # 目标投入金额（DCA）
    target_quantity: Decimal | None = None  # 目标数量
    multiplier: Decimal = Decimal("1")     # 规则引擎乘数
    reason: str = ""                       # 触发原因（进决策日志）
    triggered_rules: list[str] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class Order:
    """订单（OrderEvent 载荷，组合校验后生成）。"""

    id: UUID = field(default_factory=uuid4)
    time: datetime = field(default_factory=lambda: datetime.min)
    side: Side = Side.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal | None = None     # 指定数量（与 amount 二选一）
    amount: Decimal | None = None       # 指定金额（市价 DCA 常用）
    limit_price: Decimal | None = None
    reason: str = ""
    signal_time: datetime | None = None  # 关联信号时间（时序审计用，§2.5）


@dataclass
class Fill:
    """成交回报（FillEvent 载荷）。"""

    time: datetime
    side: Side
    price: Decimal                          # 实际成交价（含滑点）
    quantity: Decimal
    amount: Decimal                         # 成交额 = price × quantity
    fee: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")   # 相对信号价的滑点成本
    order_id: UUID | None = None
    reason: str = ""


@dataclass
class Position:
    """当前持仓状态。"""

    quantity: Decimal = Decimal("0")        # BTC 持仓量
    avg_cost: Decimal = Decimal("0")        # 移动加权平均成本
    total_cost: Decimal = Decimal("0")      # 持仓成本
    realized_pnl: Decimal = Decimal("0")    # 已实现盈亏

    def market_value(self, price: Decimal) -> Decimal:
        """按给定价格计算市值。"""
        return self.quantity * price


@dataclass
class PortfolioState:
    """回测组合状态（PortfolioManager 逐笔记账，§1.2）。

    现金账户 + BTC 持仓；校验资金充足性、最大单次投入、现金储备下限。
    """

    initial_capital: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")             # 可用现金
    position: Position = field(default_factory=Position)
    total_invested: Decimal = Decimal("0")   # 累计投入本金（不含费）
    total_fees: Decimal = Decimal("0")       # 累计手续费
    total_slippage: Decimal = Decimal("0")   # 累计滑点成本
    realized_pnl: Decimal = Decimal("0")     # 已实现盈亏
    trade_count: int = 0

    def total_value(self, price: Decimal) -> Decimal:
        """组合总市值 = 现金 + 持仓市值。"""
        return self.cash + self.position.market_value(price)

    @property
    def btc_holdings(self) -> Decimal:
        """当前 BTC 持仓量。"""
        return self.position.quantity


@dataclass
class Trade:
    """完整成交记录（写入 backtest_trades）。"""

    trade_number: int
    time: datetime
    side: Side
    price: Decimal
    quantity_btc: Decimal
    amount: Decimal
    fee: Decimal = Decimal("0")
    slippage: Decimal = Decimal("0")
    trigger_reason: str = ""
    trigger_details: dict[str, Any] = field(default_factory=dict)
    pnl: Decimal | None = None           # 卖出平仓盈亏（买入为 None）
    cumulative_btc: Decimal | None = None
    cumulative_invested: Decimal | None = None
    avg_cost_after: Decimal | None = None
    portfolio_after: Decimal | None = None


@dataclass
class EquityPoint:
    """资产曲线单点（写入 backtest_results）。"""

    time: datetime
    portfolio_value: Decimal
    cash_balance: Decimal
    btc_holdings: Decimal
    btc_price: Decimal | None = None
    avg_cost: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal = Decimal("0")
    cumulative_invested: Decimal | None = None
    drawdown: Decimal | None = None
    daily_return: Decimal | None = None
    cumulative_return: Decimal | None = None


# ---------------------------------------------------------------------------
# 回测配置与结果
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    """回测配置（用户输入，§3）。"""

    strategy_name: str
    strategy_params: dict[str, Any] = field(default_factory=dict)
    initial_capital: Decimal = Decimal("0")
    periodic_investment: Decimal | None = None   # 每期定投金额
    frequency: str = "MONTHLY"                       # DAILY/WEEKLY/BIWEEKLY/MONTHLY
    dca_day: int = 1
    start_date: date = field(default_factory=lambda: date(2015, 1, 1))
    end_date: date = field(default_factory=date.today)
    fee_rate: Decimal = Decimal("0.001")             # 手续费率（现货默认 0.1%）
    slippage: Decimal = Decimal("0.0005")            # 滑点（固定 bps，默认 5bps）
    slippage_model: SlippageModel = SlippageModel.FIXED_BPS
    symbol: str = "BTCUSDT"
    interval: str = "1d"
    quote_currency: str = "CNY"
    fill_mode: FillMode = FillMode.NEXT_OPEN         # 默认次 Bar 开盘成交
    max_single_buy: Decimal | None = None         # 单次投入上限
    cash_reserve: Decimal = Decimal("0")             # 现金储备下限
    engine: EngineType = EngineType.EVENT_DRIVEN
    data_mode: DataMode = DataMode.AS_RUN
    model_version_id: UUID | None = None
    strategy_version_id: UUID | None = None
    user_id: UUID | None = None
    risk_free_rate: float = 0.02

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("end_date 不得早于 start_date")
        if self.initial_capital < 0:
            raise ValueError("initial_capital 不得为负")
        if not (Decimal("0") <= self.fee_rate <= Decimal("0.005")):
            raise ValueError("fee_rate 应在 0 ~ 0.5% 之间")


@dataclass
class BacktestResult:
    """回测结果（§4，写入 backtest_runs / backtest_results）。"""

    config: BacktestConfig
    status: str = "COMPLETED"                        # COMPLETED / FAILED
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: int | None = None
    # 绩效指标（§4.1）
    metrics: dict[str, Any] = field(default_factory=dict)
    # 时序数据
    equity_curve: pl.DataFrame | None = None      # date, value, invested, cash, btc_amount, ...
    equity_points: list[EquityPoint] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    # 资金结果
    initial_capital: Decimal = Decimal("0")
    total_invested: Decimal = Decimal("0")
    final_value: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    btc_accumulated: Decimal = Decimal("0")
    avg_cost_basis: Decimal = Decimal("0")
    # 元数据（§4.3 强制附注）
    engine: EngineType = EngineType.EVENT_DRIVEN
    data_mode: DataMode = DataMode.AS_RUN
    approximate: bool = False                        # 向量化引擎标注 true
    data_availability: dict[str, Any] = field(default_factory=dict)
    leakage_validated: bool = False                  # 是否通过前视检测（§2.5）
    benchmark_return: Decimal | None = None       # BTC 一次性买入基准
    run_id: UUID | None = None

    def summary(self) -> dict[str, Any]:
        """结果摘要（用于 API 返回与日志）。"""
        return {
            "status": self.status,
            "engine": self.engine.value,
            "approximate": self.approximate,
            "total_return": self.metrics.get("total_return"),
            "annualized_return": self.metrics.get("annualized_return"),
            "max_drawdown": self.metrics.get("max_drawdown"),
            "sharpe_ratio": self.metrics.get("sharpe_ratio"),
            "final_value": str(self.final_value),
            "btc_accumulated": str(self.btc_accumulated),
            "total_trades": len(self.trades),
            "leakage_validated": self.leakage_validated,
        }


# ---------------------------------------------------------------------------
# 回放相关（§5）
# ---------------------------------------------------------------------------

@dataclass
class MarketStateAtDate:
    """指定日期的市场状态（回放当前视角，§5.1）。"""

    date: datetime
    price: Decimal | None = None
    candle: Bar | None = None
    indicators: dict[str, float | None] = field(default_factory=dict)
    indicator_percentiles: dict[str, float | None] = field(default_factory=dict)
    cycle_phase: str | None = None
    valuation_level: str | None = None
    risk_level: str | None = None
    risk_score: float | None = None
    regime: str | None = None
    macro: dict[str, float | None] = field(default_factory=dict)
    sentiment: dict[str, Any] | None = None
    recomputed: bool = False                          # True = 重算值非当时实时判断
    data_availability: dict[str, Any] = field(default_factory=dict)


@dataclass
class FutureOutcome:
    """事后视角的未来走势（§5.2 view=after）。"""

    base_date: datetime
    horizons: dict[int, dict[str, Any]] = field(default_factory=dict)
    # horizons[days] = {future_date, price, return_pct, max_drawdown, min_price, max_price}


@dataclass
class ReplaySnapshot:
    """历史回放快照（§5）。"""

    date: datetime
    perspective: str = "current"                     # current=当时视角 / hindsight=事后视角
    market_state: MarketStateAtDate | None = None
    future_outcome: FutureOutcome | None = None


# SimTransaction 别名：回测内的成交记录以 Trade 为准，
# 保留 SimTransaction 名称以对齐任务契约（指向 Trade）。
SimTransaction = Trade
