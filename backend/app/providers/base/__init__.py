"""Provider 抽象基类与核心数据类型。

本包定义 Provider 抽象层的全部基础设施：

- types       : 核心数据类型（FetchResult / ErrorType / QualityStatus 等）
- payloads    : 标准化数据载荷构造助手（onchain/etf/derivative/macro/sentiment payload）
- config      : ProviderConfig 配置数据类 + ConfigLoader（YAML 加载）
- provider    : BaseProvider 顶层抽象基类
- market_provider     : BaseMarketProvider（行情）
- onchain_provider    : BaseOnChainProvider（链上）
- etf_provider        : BaseETFProvider（ETF 资金流）
- derivative_provider : BaseDerivativeProvider（衍生品）
- options_provider    : BaseOptionsProvider（期权）
- macro_provider      : BaseMacroProvider（宏观）
- sentiment_provider  : BaseSentimentProvider（情绪）
- registry    : ProviderRegistry 单例注册中心
"""

from app.providers.base.config import (
    ConfigLoader,
    ProviderConfig,
    RateLimitConfig,
    RetryConfig,
    TimeoutConfig,
)
from app.providers.base.derivative_provider import BaseDerivativeProvider
from app.providers.base.etf_provider import BaseETFProvider
from app.providers.base.macro_provider import BaseMacroProvider
from app.providers.base.market_provider import BaseMarketProvider
from app.providers.base.onchain_provider import BaseOnChainProvider
from app.providers.base.options_provider import BaseOptionsProvider
from app.providers.base.payloads import (
    auth_required_result,
    derivative_payload,
    etf_flow_payload,
    etf_holdings_payload,
    exchange_flow_payload,
    macro_payload,
    onchain_payload,
    onchain_series_payload,
    options_payload,
    sentiment_payload,
    unsupported_result,
)
from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.base.sentiment_provider import BaseSentimentProvider
from app.providers.base.types import (
    DegradedResult,
    ErrorType,
    FailoverEvent,
    FetchResult,
    HealthState,
    ProviderHealthSnapshot,
    ProviderLifecycleStatus,
    ProviderMetadata,
    ProviderMetrics,
    QualityStatus,
)

__all__ = [
    # config
    "ConfigLoader",
    "ProviderConfig",
    "RateLimitConfig",
    "RetryConfig",
    "TimeoutConfig",
    # base providers
    "BaseProvider",
    "BaseMarketProvider",
    "BaseOnChainProvider",
    "BaseETFProvider",
    "BaseDerivativeProvider",
    "BaseOptionsProvider",
    "BaseMacroProvider",
    "BaseSentimentProvider",
    # registry
    "ProviderRegistry",
    # types
    "DegradedResult",
    "ErrorType",
    "FailoverEvent",
    "FetchResult",
    "HealthState",
    "ProviderHealthSnapshot",
    "ProviderLifecycleStatus",
    "ProviderMetadata",
    "ProviderMetrics",
    "QualityStatus",
    # payloads
    "auth_required_result",
    "derivative_payload",
    "etf_flow_payload",
    "etf_holdings_payload",
    "exchange_flow_payload",
    "macro_payload",
    "onchain_payload",
    "onchain_series_payload",
    "options_payload",
    "sentiment_payload",
    "unsupported_result",
]
