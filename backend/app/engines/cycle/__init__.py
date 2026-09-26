"""市场周期引擎（Cycle Engine）。

导出：
- :class:`CycleEngine` — 主引擎（9 阶段识别 + 防抖状态机）
- :class:`PatternMatcher` / :class:`SimilarPeriod` — 历史模式匹配
- 各因子计算函数与默认权重
"""

from app.engines.cycle.engine import (
    LEGAL_TRANSITIONS,
    CycleEngine,
    get_cycle_engine,
    score_to_phase,
    set_cycle_engine,
)
from app.engines.cycle.factors import (
    CYCLE_FACTOR_WEIGHTS,
    calculate_capital_flow_factor,
    calculate_derivative_factor,
    calculate_macro_factor,
    calculate_onchain_factor,
    calculate_price_trend_factor,
    calculate_sentiment_factor,
    calculate_valuation_anchor_factor,
)
from app.engines.cycle.patterns import PatternMatcher, SimilarPeriod

__all__ = [
    "CycleEngine",
    "get_cycle_engine",
    "set_cycle_engine",
    "score_to_phase",
    "LEGAL_TRANSITIONS",
    "PatternMatcher",
    "SimilarPeriod",
    "CYCLE_FACTOR_WEIGHTS",
    "calculate_price_trend_factor",
    "calculate_onchain_factor",
    "calculate_capital_flow_factor",
    "calculate_derivative_factor",
    "calculate_macro_factor",
    "calculate_sentiment_factor",
    "calculate_valuation_anchor_factor",
]
