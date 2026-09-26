"""综合市场状态引擎（Market Regime Engine）。

导出：
- :class:`MarketRegimeEngine` — 主引擎（九维融合 → 描述性 Regime 标签）
- :class:`RegimeFusion` / :class:`DimensionInput` / :class:`FusionResult` — 融合器
"""

from app.engines.regime.engine import (
    MarketRegimeEngine,
    get_regime_engine,
    set_regime_engine,
)
from app.engines.regime.fusion import (
    REGIME_DIMENSION_WEIGHTS,
    DimensionInput,
    FusionResult,
    RegimeFusion,
    score_to_regime_label,
)

__all__ = [
    "MarketRegimeEngine",
    "get_regime_engine",
    "set_regime_engine",
    "RegimeFusion",
    "DimensionInput",
    "FusionResult",
    "REGIME_DIMENSION_WEIGHTS",
    "score_to_regime_label",
]
