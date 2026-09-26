"""回测引擎模块。

对应 14-backtest-architecture.md。子模块：
- types：回测数据类型（Bar/Signal/Order/Fill/Trade/BacktestConfig/BacktestResult 等）
- metrics：绩效指标计算（§4）
- data_feed：Point-in-Time 数据供给器（§2，防未来数据泄漏，最高优先级约束）
- strategy：策略基类 + 内置策略（§6）
- engine：事件驱动回测引擎（§1.2，结果权威来源）
- vectorized：向量化快速回测（§1.3，approximate=true）
- replay：历史回放（§5，当时视角 / 事后视角）
"""

from app.backtest.data_feed import (
    DataAccessException,
    LookAheadViolationError,
    PointInTimeDataFeed,
)
from app.backtest.engine import (
    BacktestEngine,
    ExecutionSimulator,
    PortfolioManager,
)
from app.backtest.metrics import BacktestMetrics, calculate_all
from app.backtest.replay import HistoryReplay
from app.backtest.strategy import (
    DipBuyStrategy,
    DrawdownBuyStrategy,
    FixedDCAStrategy,
    MarketContext,
    RiskAdjustedDCAStrategy,
    RuleBasedStrategy,
    Strategy,
    ValuationDCAStrategy,
    create_strategy,
)
from app.backtest.types import (
    BacktestConfig,
    BacktestResult,
    Bar,
    DataMode,
    EngineType,
    EquityPoint,
    Fill,
    FillMode,
    FutureOutcome,
    MarketStateAtDate,
    Order,
    OrderType,
    PortfolioState,
    Position,
    ReplaySnapshot,
    Side,
    Signal,
    SimTransaction,
    SlippageModel,
    Trade,
)
from app.backtest.vectorized import VectorizedBacktest

__all__ = [
    # data_feed（防泄漏核心）
    "PointInTimeDataFeed",
    "LookAheadViolationError",
    "DataAccessException",
    # engine
    "BacktestEngine",
    "ExecutionSimulator",
    "PortfolioManager",
    # vectorized
    "VectorizedBacktest",
    # metrics
    "BacktestMetrics",
    "calculate_all",
    # strategy
    "Strategy",
    "MarketContext",
    "FixedDCAStrategy",
    "DipBuyStrategy",
    "DrawdownBuyStrategy",
    "ValuationDCAStrategy",
    "RiskAdjustedDCAStrategy",
    "RuleBasedStrategy",
    "create_strategy",
    # replay
    "HistoryReplay",
    # types
    "Bar",
    "Signal",
    "Order",
    "Fill",
    "Position",
    "PortfolioState",
    "Trade",
    "SimTransaction",
    "EquityPoint",
    "BacktestConfig",
    "BacktestResult",
    "MarketStateAtDate",
    "FutureOutcome",
    "ReplaySnapshot",
    "Side",
    "OrderType",
    "FillMode",
    "SlippageModel",
    "EngineType",
    "DataMode",
]
