"""个人资金计划与资产管理模块。

对应 15-portfolio-architecture.md。子模块：
- service：PortfolioService（计划 CRUD、交易账本、持仓/绩效查询）
- calculator：PortfolioCalculator（持仓、成本、收益、回撤计算）
- dca_simulator：DCASimulator（定投模拟引擎，6 种内置策略）
- rule_engine：RuleEngine（可配置加仓规则，与回测共用）
- snapshot：SnapshotService（每日组合净值快照）
"""

from app.portfolio.calculator import (
    Holdings,
    Performance,
    PortfolioCalculator,
    q_amount,
    q_btc,
    q_rate,
)
from app.portfolio.dca_simulator import (
    DCAConfig,
    DCASimulator,
    SimTransaction,
    SimulationResult,
)
from app.portfolio.rule_engine import (
    Action,
    ActionKind,
    CompositeCondition,
    Condition,
    ConditionResult,
    DistanceFromAthBelow,
    DrawdownExceeds,
    IndicatorPercentile,
    IndicatorValue,
    PriceAbove,
    PriceBelow,
    RiskLevelCondition,
    Rule,
    RuleAction,
    RuleContext,
    RuleEngine,
    build_rule,
    build_rules,
    invest_fixed,
    multiply_amount,
    notify_only,
    pause,
    skip,
)
from app.portfolio.service import (
    PlanNotFoundError,
    PlanStateError,
    PortfolioService,
)
from app.portfolio.snapshot import SnapshotService

__all__ = [
    # service
    "PortfolioService",
    "PlanNotFoundError",
    "PlanStateError",
    # calculator
    "PortfolioCalculator",
    "Holdings",
    "Performance",
    "q_amount",
    "q_btc",
    "q_rate",
    # dca_simulator
    "DCASimulator",
    "DCAConfig",
    "SimulationResult",
    "SimTransaction",
    # rule_engine
    "RuleEngine",
    "Rule",
    "RuleContext",
    "RuleAction",
    "Condition",
    "ConditionResult",
    "CompositeCondition",
    "PriceBelow",
    "PriceAbove",
    "DistanceFromAthBelow",
    "DrawdownExceeds",
    "IndicatorValue",
    "IndicatorPercentile",
    "RiskLevelCondition",
    "Action",
    "ActionKind",
    "multiply_amount",
    "invest_fixed",
    "skip",
    "pause",
    "notify_only",
    "build_rule",
    "build_rules",
    # snapshot
    "SnapshotService",
]
