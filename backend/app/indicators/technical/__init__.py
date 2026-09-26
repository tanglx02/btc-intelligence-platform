"""技术指标子包（Technical Indicators）。

汇总 SMA / EMA / WMA / RSI / MACD / ADX / ATR / Bollinger / VWAP /
Historical Volatility / Percentile Rank 等指标实现。
"""

from .adx import ADXIndicator, adx
from .atr import ATRIndicator, atr, true_range
from .bollinger import BollingerBandsIndicator, bollinger_bands
from .macd import MACDIndicator, macd
from .moving_average import (
    COMMON_PERIODS,
    EMAIndicator,
    MovingAverageBase,
    SMAIndicator,
    WMAIndicator,
    ema,
    sma,
    wma,
)
from .percentile import PercentileRankIndicator, percentile_rank_series
from .rsi import RSIIndicator, rsi
from .volatility import HistoricalVolatilityIndicator, historical_volatility
from .vwap import VWAPIndicator, vwap

#: 本子包内全部可注册指标类（供 registry 自动发现）
INDICATOR_CLASSES: list[type] = [
    SMAIndicator,
    EMAIndicator,
    WMAIndicator,
    RSIIndicator,
    MACDIndicator,
    ADXIndicator,
    ATRIndicator,
    BollingerBandsIndicator,
    VWAPIndicator,
    HistoricalVolatilityIndicator,
    PercentileRankIndicator,
]

__all__ = [
    "ADXIndicator",
    "ATRIndicator",
    "BollingerBandsIndicator",
    "COMMON_PERIODS",
    "EMAIndicator",
    "HistoricalVolatilityIndicator",
    "INDICATOR_CLASSES",
    "MACDIndicator",
    "MovingAverageBase",
    "PercentileRankIndicator",
    "RSIIndicator",
    "SMAIndicator",
    "VWAPIndicator",
    "WMAIndicator",
    "adx",
    "atr",
    "bollinger_bands",
    "ema",
    "historical_volatility",
    "macd",
    "percentile_rank_series",
    "rsi",
    "sma",
    "true_range",
    "vwap",
    "wma",
]
