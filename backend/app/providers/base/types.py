"""Provider 层核心数据类型定义。

包含：ErrorType, FetchResult, ProviderMetadata, QualityStatus 等统一数据结构。
所有 Provider 方法的返回值统一使用 FetchResult 封装。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class ErrorType(str, Enum):
    """错误类型分类枚举。

    用于统一的错误分类和处理策略决策。
    """

    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    DNS_ERROR = "dns_error"
    TLS_ERROR = "tls_error"
    DATA_FORMAT = "data_format"
    DATA_QUALITY = "data_quality"
    SERVER_ERROR = "server_error"
    EMPTY_RESPONSE = "empty_response"
    STALE_DATA = "stale_data"
    CONNECTION_REFUSED = "connection_refused"
    UNKNOWN = "unknown"


class QualityStatus(str, Enum):
    """数据质量状态枚举。"""

    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


class ProviderLifecycleStatus(str, Enum):
    """Provider 生命周期状态枚举（运行时）。"""

    INITIALIZING = "initializing"
    HEALTH_CHECKING = "health_checking"
    READY = "ready"
    RUNNING = "running"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    RECOVERING = "recovering"
    DISABLED = "disabled"
    DESTROYED = "destroyed"


class HealthState(str, Enum):
    """Provider 健康状态枚举（对外暴露）。"""

    ONLINE = "online"
    DEGRADED = "degraded"
    SLOW = "slow"
    RATE_LIMITED = "rate_limited"
    AUTH_ERROR = "auth_error"
    NETWORK_ERROR = "network_error"
    DATA_ERROR = "data_error"
    OFFLINE = "offline"
    DISABLED = "disabled"


@dataclass
class FetchResult:
    """统一数据获取结果封装。

    所有 Provider 方法必须返回此类型，不抛出异常。

    Attributes:
        success: 请求是否成功
        data: 成功时返回的数据（标准化后的 Python 对象）
        error: 错误描述信息
        error_type: 错误分类（用于 Failover 策略决策）
        status_code: HTTP 响应状态码
        response_time_ms: 响应耗时（毫秒）
        provider_name: 数据来源 Provider 名称
        fetch_time: 数据抓取时间（UTC）
        observation_time: 数据观测时间（UTC）
        quality_status: 数据质量状态
        raw_response: 原始响应数据（用于 Raw Storage）
        metadata: 扩展元信息
        is_failover: 是否为 Failover 后获取的数据
        is_stale: 是否为过期缓存数据
    """

    success: bool
    data: Any = None
    error: str | None = None
    error_type: ErrorType | None = None
    status_code: int | None = None
    response_time_ms: float = 0.0
    provider_name: str = ""
    fetch_time: datetime = field(default_factory=datetime.utcnow)
    observation_time: datetime | None = None
    quality_status: QualityStatus = QualityStatus.VERIFIED
    raw_response: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    is_failover: bool = False
    is_stale: bool = False


@dataclass
class ProviderMetadata:
    """Provider 元信息描述。"""

    provider_id: UUID = field(default_factory=uuid4)
    name: str = ""
    category: str = ""
    description: str = ""
    supported_symbols: list[str] = field(default_factory=lambda: ["BTC/USDT"])
    supported_intervals: list[str] = field(default_factory=list)
    data_coverage_start: datetime | None = None
    documentation_url: str | None = None
    version: str = "1.0.0"


@dataclass
class DegradedResult:
    """全部 Provider 失败时的降级结果封装。"""

    success: bool = True
    data: Any = None
    quality_status: QualityStatus = QualityStatus.STALE
    is_degraded: bool = True
    last_updated: datetime | None = None
    stale_duration_seconds: float = 0.0
    message: str = "数据暂时无法更新，显示最近一次可信数据"
    all_providers_failed: bool = True
    retry_scheduled: bool = True
    next_retry_at: datetime | None = None


@dataclass
class FailoverEvent:
    """故障切换事件数据结构。"""

    event_id: UUID = field(default_factory=uuid4)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    data_type: str = ""
    original_provider: str = ""
    new_provider: str | None = None
    reason: str = ""
    error_detail: str = ""
    recovery_action: str = "auto_failover"
    retry_count: int = 0
    latency_ms: float = 0.0
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    is_all_failed: bool = False
    quality_status: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderMetrics:
    """Provider 运行时指标采集结构（用于评分计算）。"""

    provider_name: str = ""
    # 响应与延迟
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    # 成功率
    total_requests: int = 0
    total_failures: int = 0
    success_rate_1h: float = 1.0
    success_rate_24h: float = 1.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    today_failures: int = 0
    failure_count_7d: int = 0
    # 稳定性
    uptime_seconds: float = 0.0
    last_success_time: datetime | None = None
    last_failure_time: datetime | None = None
    # 数据质量
    data_completeness: float = 1.0
    data_accuracy: float = 1.0
    data_delay_seconds: float = 0.0
    # 网络
    network_reachable: bool = True
    reachability_score: float = 1.0
    # HTTP
    last_http_status: int | None = None
    is_rate_limited: bool = False
    rate_limit_remaining: int | None = None
    # 错误
    last_error_reason: str | None = None
    last_error_type: str | None = None


@dataclass
class ProviderHealthSnapshot:
    """Provider 健康快照 — 监控面板核心数据结构。"""

    provider_name: str = ""
    category: str = ""
    status: HealthState = HealthState.OFFLINE
    is_enabled: bool = True
    is_primary: bool = False
    current_priority: int = 100
    locked: bool = False
    # 评分
    health_score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    score_trend: str = "stable"
    # 响应
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    # 成功率
    success_rate_24h: float = 1.0
    consecutive_failures: int = 0
    today_failures: int = 0
    failure_count_7d: int = 0
    # 时间
    last_success_time: datetime | None = None
    last_failure_time: datetime | None = None
    last_health_check: datetime | None = None
    uptime_seconds: float = 0.0
    # 网络
    network_reachable: bool = True
    proxy_in_use: str | None = None
    last_error_reason: str | None = None
    updated_at: datetime = field(default_factory=datetime.utcnow)


__all__ = [
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
]
