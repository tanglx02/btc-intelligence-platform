"""业务服务层。

封装 Provider 抽象层，向上层（API / Scheduler）提供统一的数据获取入口：

- ProviderService      : Provider 业务层统一入口（缓存 + 标准化 + Failover 封装）
- ServiceResult        : 业务层统一返回结构
- RawDataStore         : 原始数据 append-only 存储
- NormalizedDataStore  : 标准化数据批量写入（Write Gate + UPSERT）
- DataNormalizer       : 数据标准化管道（字段/单位/时间/精度统一）
- SyncService          : 历史数据同步（断点续传 + 限速 + 进度上报）
- GapDetector          : 数据补洞检测与修复
- CrossValidator       : 多源交叉验证
"""

from app.services.provider_service import (
    ProviderService,
    ServiceResult,
    get_provider_service,
    set_provider_service,
)
from app.services.data_store import (
    NormalizedDataStore,
    RawDataStore,
    StoreResult,
    get_normalized_data_store,
    get_raw_data_store,
)
from app.services.data_normalizer import (
    DataNormalizer,
    NormalizationResult,
    get_data_normalizer,
)
from app.services.sync_service import (
    SyncOutcome,
    SyncService,
    get_sync_service,
)
from app.services.gap_detector import (
    DataGap,
    GapDetectionResult,
    GapDetector,
    GapFillResult,
    get_gap_detector,
)
from app.services.cross_validator import (
    CrossValidationResult,
    CrossValidator,
    ValidationResult,
    get_cross_validator,
)

__all__ = [
    "CrossValidationResult",
    "CrossValidator",
    "DataGap",
    "DataNormalizer",
    "GapDetectionResult",
    "GapDetector",
    "GapFillResult",
    "NormalizationResult",
    "NormalizedDataStore",
    "ProviderService",
    "RawDataStore",
    "ServiceResult",
    "StoreResult",
    "SyncOutcome",
    "SyncService",
    "ValidationResult",
    "get_cross_validator",
    "get_data_normalizer",
    "get_gap_detector",
    "get_normalized_data_store",
    "get_provider_service",
    "get_raw_data_store",
    "get_sync_service",
    "set_provider_service",
]
