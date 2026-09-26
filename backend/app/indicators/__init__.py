"""技术指标引擎（Indicator Engine）。

统一导出指标计算的公共 API：

- :class:`IndicatorBase` / :class:`IndicatorResult` / :class:`IndicatorCategory` — 计算契约；
- :data:`registry` 与 ``get_indicator`` / ``list_indicators`` /
  ``get_indicator_definition`` — 注册中心；
- :class:`IndicatorCalculator` / :data:`calculator` — 调度计算（缓存 + 持久化）。

各具体指标类可从子包导入，例如::

    from app.indicators.technical import RSIIndicator
    from app.indicators.onchain import MVRVIndicator

快速开始::

    from app.indicators import calculator
    res = calculator.calculate_indicator("tech.rsi", df, period=14)
    print(res.last())
"""

from __future__ import annotations

from .base import TIME_COLUMN, IndicatorBase, IndicatorCategory, IndicatorResult
from .calculator import IndicatorCalculator, calculator
from .registry import (
    INDICATOR_DEFINITIONS,
    IndicatorRegistry,
    get_indicator,
    get_indicator_definition,
    list_indicators,
    registry,
)

__all__ = [
    # 契约
    "IndicatorBase",
    "IndicatorResult",
    "IndicatorCategory",
    "TIME_COLUMN",
    # 注册中心
    "IndicatorRegistry",
    "registry",
    "INDICATOR_DEFINITIONS",
    "get_indicator",
    "list_indicators",
    "get_indicator_definition",
    # 调度计算
    "IndicatorCalculator",
    "calculator",
]
