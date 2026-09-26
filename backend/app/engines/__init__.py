"""分析引擎层。

引擎负责将多源数据融合为可解释的结论，彼此解耦、可独立测试：

- cycle      : 周期定位引擎（9 阶段识别、防抖状态机、历史模式匹配）
- valuation  : 估值引擎（5 维度 → 0-100 评分 → 5 级估值状态）
- risk       : 风险监测引擎（11 因子 → 7 维度 → 6 级风险等级）
- regime     : 综合市场状态引擎（九维融合 → 描述性 Regime 标签）
- quality    : 数据质量引擎（多源交叉校验、异常检测、置信度评分）

统一契约见 :mod:`app.engines.base`（EngineBase / EngineEvidence / EngineOutput）。
红线：所有引擎只输出描述性结论 + 证据链，永不输出 BUY/SELL。
"""

from app.engines.base import (
    EngineBase,
    EngineEvidence,
    EngineOutput,
    clamp,
    normalize_weights,
    percentile_rank,
    quality_coefficient,
    safe_div,
    weighted_average,
)
from app.engines.cycle import (
    CycleEngine,
    PatternMatcher,
    SimilarPeriod,
    get_cycle_engine,
    set_cycle_engine,
)
from app.engines.regime import (
    DimensionInput,
    FusionResult,
    MarketRegimeEngine,
    RegimeFusion,
    get_regime_engine,
    set_regime_engine,
)
from app.engines.risk import (
    RiskEngine,
    get_risk_engine,
    score_to_risk_level,
    set_risk_engine,
)
from app.engines.valuation import (
    ValuationEngine,
    get_valuation_engine,
    score_to_level,
    set_valuation_engine,
)

__all__ = [
    # base
    "EngineBase",
    "EngineEvidence",
    "EngineOutput",
    "clamp",
    "safe_div",
    "weighted_average",
    "percentile_rank",
    "normalize_weights",
    "quality_coefficient",
    # cycle
    "CycleEngine",
    "PatternMatcher",
    "SimilarPeriod",
    "get_cycle_engine",
    "set_cycle_engine",
    # valuation
    "ValuationEngine",
    "get_valuation_engine",
    "set_valuation_engine",
    "score_to_level",
    # risk
    "RiskEngine",
    "get_risk_engine",
    "set_risk_engine",
    "score_to_risk_level",
    # regime
    "MarketRegimeEngine",
    "RegimeFusion",
    "DimensionInput",
    "FusionResult",
    "get_regime_engine",
    "set_regime_engine",
]
