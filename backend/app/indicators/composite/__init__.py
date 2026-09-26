"""复合指标子包（Composite Indicators）。

汇总回撤（Drawdown / MaxDrawdown / DrawdownDuration）与
ATH（ATH / DistanceFromATH / ATHBreakout）相关指标实现。
"""

from .ath import (
    ATHBreakoutIndicator,
    ATHIndicator,
    DistanceFromATHIndicator,
    all_time_high,
    resolve_price,
)
from .drawdown import (
    DrawdownDurationIndicator,
    DrawdownIndicator,
    MaxDrawdownIndicator,
    drawdown_from_high,
)

#: 本子包内全部可注册指标类（供 registry 自动发现）
INDICATOR_CLASSES: list[type] = [
    ATHIndicator,
    DistanceFromATHIndicator,
    ATHBreakoutIndicator,
    DrawdownIndicator,
    MaxDrawdownIndicator,
    DrawdownDurationIndicator,
]

__all__ = [
    "ATHBreakoutIndicator",
    "ATHIndicator",
    "DistanceFromATHIndicator",
    "DrawdownDurationIndicator",
    "DrawdownIndicator",
    "INDICATOR_CLASSES",
    "MaxDrawdownIndicator",
    "all_time_high",
    "drawdown_from_high",
    "resolve_price",
]
