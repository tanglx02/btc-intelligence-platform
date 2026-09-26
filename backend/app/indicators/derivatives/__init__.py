"""衍生品指标子包（Derivatives Indicators）。

汇总 Funding Rate / Open Interest / CVD 等实现。
"""

from .cvd import CVDIndicator, cvd
from .funding import FundingRateAnalysis, funding_rate_analysis
from .open_interest import OpenInterestAnalysis, open_interest_analysis

#: 本子包内全部可注册指标类（供 registry 自动发现）
INDICATOR_CLASSES: list[type] = [
    FundingRateAnalysis,
    OpenInterestAnalysis,
    CVDIndicator,
]

__all__ = [
    "CVDIndicator",
    "FundingRateAnalysis",
    "INDICATOR_CLASSES",
    "OpenInterestAnalysis",
    "cvd",
    "funding_rate_analysis",
    "open_interest_analysis",
]
