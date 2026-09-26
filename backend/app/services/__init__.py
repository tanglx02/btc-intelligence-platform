"""业务服务层。

封装 Provider 抽象层，向上层（API / Scheduler）提供统一的数据获取入口：

- ProviderService      : Provider 业务层统一入口（缓存 + 标准化 + Failover 封装）
- ServiceResult        : 业务层统一返回结构
- CategoryServiceBase  : 分类服务公共基类（本地优先 + 缓存 + Failover）
- MarketService        : 市场行情
- OnChainService       : 链上数据
- ETFService           : 现货 ETF 资金流
- DerivativesService   : 衍生品
- OptionsService       : 期权
- MacroService         : 宏观经济
- SentimentService     : 市场情绪
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
from app.services.base_service import CategoryServiceBase
from app.services.market_service import MarketService
from app.services.onchain_service import OnChainService
from app.services.etf_service import ETFService
from app.services.derivatives_service import DerivativesService
from app.services.options_service import OptionsService
from app.services.macro_service import MacroService
from app.services.sentiment_service import SentimentService
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
    "CategoryServiceBase",
    "CrossValidationResult",
    "CrossValidator",
    "DataGap",
    "DataNormalizer",
    "DerivativesService",
    "ETFService",
    "GapDetectionResult",
    "GapDetector",
    "GapFillResult",
    "MacroService",
    "MarketService",
    "NormalizationResult",
    "NormalizedDataStore",
    "OnChainService",
    "OptionsService",
    "ProviderService",
    "RawDataStore",
    "SentimentService",
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
