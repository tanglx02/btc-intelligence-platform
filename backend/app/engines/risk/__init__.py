"""风险引擎（Risk Engine）。

导出：
- :class:`RiskEngine` — 主引擎（11 因子 → 7 维度 → overall + RiskLevel）
- 各风险因子计算函数、维度归属与权重
"""

from app.engines.risk.engine import (
    RiskEngine,
    get_risk_engine,
    score_to_risk_level,
    set_risk_engine,
)
from app.engines.risk.factors import (
    FACTOR_TO_DIMENSION,
    RISK_DIMENSION_WEIGHTS,
    calculate_crowding_risk,
    calculate_drawdown_risk,
    calculate_funding_risk,
    calculate_leverage_risk,
    calculate_liquidation_risk,
    calculate_liquidity_risk,
    calculate_macro_risk,
    calculate_oi_risk,
    calculate_onchain_risk,
    calculate_valuation_risk,
    calculate_volatility_risk,
)

__all__ = [
    "RiskEngine",
    "get_risk_engine",
    "set_risk_engine",
    "score_to_risk_level",
    "FACTOR_TO_DIMENSION",
    "RISK_DIMENSION_WEIGHTS",
    "calculate_valuation_risk",
    "calculate_volatility_risk",
    "calculate_leverage_risk",
    "calculate_funding_risk",
    "calculate_oi_risk",
    "calculate_liquidation_risk",
    "calculate_liquidity_risk",
    "calculate_macro_risk",
    "calculate_onchain_risk",
    "calculate_crowding_risk",
    "calculate_drawdown_risk",
]
