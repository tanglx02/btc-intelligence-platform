"""数据质量引擎。

提供数据质量的全面检查、评分、报告能力：

- DataChecker       : 数据质量检查器（范围/异常值/连续性/重复）
- QualityScorer     : 五维度加权评分
- QualityReporter   : 质量报告生成与持久化
- DataQualityEngine : 主引擎（整合以上三者）
"""

from app.engines.quality.checker import (
    CheckIssue,
    CheckResult,
    DataCheckReport,
    DataChecker,
)
from app.engines.quality.engine import (
    DataQualityEngine,
    get_quality_engine,
    set_quality_engine,
)
from app.engines.quality.reporter import (
    BatchQualityReport,
    QualityReport,
    QualityReporter,
    get_quality_reporter,
)
from app.engines.quality.scorer import (
    DimensionScore,
    QualityDimension,
    QualityLevel,
    QualityScore,
    QualityScorer,
    get_quality_scorer,
)

__all__ = [
    "BatchQualityReport",
    "CheckIssue",
    "CheckResult",
    "DataCheckReport",
    "DataChecker",
    "DataQualityEngine",
    "DimensionScore",
    "QualityDimension",
    "QualityLevel",
    "QualityReport",
    "QualityReporter",
    "QualityScore",
    "QualityScorer",
    "get_quality_engine",
    "get_quality_reporter",
    "get_quality_scorer",
    "set_quality_engine",
]