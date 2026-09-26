"""估值引擎（Valuation Engine）。

导出：
- :class:`ValuationEngine` — 主引擎（5 维度 → 0-100 综合评分 → 5 级状态）
- 各维度计算函数与默认权重
"""

from app.engines.valuation.engine import (
    ValuationEngine,
    get_valuation_engine,
    score_to_level,
    set_valuation_engine,
)
from app.engines.valuation.factors import (
    VALUATION_FACTOR_WEIGHTS,
    calculate_cost_basis_dimension,
    calculate_drawdown_dimension,
    calculate_mvrv_dimension,
    calculate_realized_cap_dimension,
    calculate_trend_dimension,
)

__all__ = [
    "ValuationEngine",
    "get_valuation_engine",
    "set_valuation_engine",
    "score_to_level",
    "VALUATION_FACTOR_WEIGHTS",
    "calculate_mvrv_dimension",
    "calculate_realized_cap_dimension",
    "calculate_cost_basis_dimension",
    "calculate_trend_dimension",
    "calculate_drawdown_dimension",
]
