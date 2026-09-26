"""链上指标子包（On-chain Indicators）。

汇总 MVRV / SOPR 家族 / NUPL / Puell Multiple / RHODL / Reserve Risk 等实现。
"""

from .mvrv import MVRVIndicator, mvrv, resolve_market_cap, resolve_realized_cap
from .nupl import NUPLIndicator, nupl
from .puell import PuellMultipleIndicator, puell_multiple
from .reserve_risk import ReserveRiskIndicator, reserve_risk
from .rhodl import RHODLIndicator, rhodl
from .sopr import (
    AdjustedSOPRIndicator,
    LTHSOPRIndicator,
    SOPRBase,
    SOPRIndicator,
    asopr,
    lth_sopr,
    sopr,
)

#: 本子包内全部可注册指标类（供 registry 自动发现）
INDICATOR_CLASSES: list[type] = [
    MVRVIndicator,
    SOPRIndicator,
    AdjustedSOPRIndicator,
    LTHSOPRIndicator,
    NUPLIndicator,
    PuellMultipleIndicator,
    RHODLIndicator,
    ReserveRiskIndicator,
]

__all__ = [
    "AdjustedSOPRIndicator",
    "INDICATOR_CLASSES",
    "LTHSOPRIndicator",
    "MVRVIndicator",
    "NUPLIndicator",
    "PuellMultipleIndicator",
    "RHODLIndicator",
    "ReserveRiskIndicator",
    "SOPRBase",
    "SOPRIndicator",
    "asopr",
    "lth_sopr",
    "mvrv",
    "nupl",
    "puell_multiple",
    "reserve_risk",
    "resolve_market_cap",
    "resolve_realized_cap",
    "rhodl",
    "sopr",
]
