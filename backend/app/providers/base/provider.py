"""BaseProvider 抽象基类。

所有 Provider 的顶层基类，定义：
- 生命周期管理（initialize / shutdown / health_check）
- 统一请求封装（内置限流、重试、超时处理）
- 运行时指标采集（成功率、延迟、连续失败计数等）
- 状态管理（ProviderLifecycleStatus / HealthState 转换）
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

from loguru import logger

from app.providers.base.config import ProviderConfig
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    HealthState,
    ProviderHealthSnapshot,
    ProviderLifecycleStatus,
    ProviderMetadata,
    ProviderMetrics,
)
from app.providers.network import HTTPClient
from app.providers.rate_limiter import RateLimiterFactory, TokenBucketRateLimiter
from app.utils.datetime_utils import utcnow


class BaseProvider(ABC):
    """所有 Provider 的顶层抽象基类。

    设计约束：
    - 子类必须实现所有 @abstractmethod
    - 子类不得持有业务逻辑，仅负责数据获取与格式转换
    - 子类不得直接写入数据库，由 Service 层统一处理
    - 所有方法不抛出异常，通过 FetchResult.success 标识成功/失败

    生命周期：
        INITIALIZING -> HEALTH_CHECKING -> READY -> RUNNING
        -> DEGRADED/OFFLINE -> RECOVERING -> READY
    """

    def __init__(self, config: ProviderConfig):
        """初始化 Provider 基础状态。

        Args:
            config: Provider 配置实例
        """
        self._config = config
        self._status = ProviderLifecycleStatus.INITIALIZING
        self._health_state = HealthState.OFFLINE
        self._metadata: ProviderMetadata | None = None
        self._http_client: HTTPClient | None = None
        self._rate_limiter: TokenBucketRateLimiter | None = None

        # 运行时指标
        self._consecutive_failures: int = 0
        self._consecutive_successes: int = 0
        self._last_success_time: datetime | None = None
        self._last_failure_time: datetime | None = None
        self._total_requests: int = 0
        self._total_failures: int = 0
        self._today_requests: int = 0
        self._today_failures: int = 0
        self._today_date: str | None = None

        # 延迟统计
        self._latencies: list[float] = []
        self._max_latency_history: int = 1000  # 保留最近 1000 次延迟

        # 错误记录
        self._last_error: str | None = None
        self._last_error_type: ErrorType | None = None
        self._last_http_status: int | None = None

        # 启动时间
        self._started_at: datetime | None = None

    # ---- 属性 ----

    @property
    def name(self) -> str:
        """Provider 唯一名称。"""
        return self._config.name

    @property
    def category(self) -> str:
        """Provider 数据类别。"""
        return self._config.category

    @property
    def config(self) -> ProviderConfig:
        """Provider 配置。"""
        return self._config

    @property
    def status(self) -> ProviderLifecycleStatus:
        """当前生命周期状态。"""
        return self._status

    @property
    def health_state(self) -> HealthState:
        """当前健康状态。"""
        return self._health_state

    @property
    def priority(self) -> int:
        """当前优先级。"""
        return self._config.priority

    @property
    def is_enabled(self) -> bool:
        """是否启用。"""
        return self._config.enabled and self._status != ProviderLifecycleStatus.DISABLED

    @property
    def is_available(self) -> bool:
        """是否可接收请求。"""
        return self._status in (
            ProviderLifecycleStatus.READY,
            ProviderLifecycleStatus.RUNNING,
            ProviderLifecycleStatus.DEGRADED,
        )

    # ---- 生命周期方法（子类必须实现）----

    @abstractmethod
    async def health_check(self) -> bool:
        """执行健康检查，返回 True 表示 Provider 可用。

        子类应实现轻量级的连通性探测（如请求 /ping 或获取简单数据）。
        """
        ...

    @abstractmethod
    def get_metadata(self) -> ProviderMetadata:
        """返回 Provider 元信息。"""
        ...

    @abstractmethod
    def get_supported_data_types(self) -> list[str]:
        """返回该 Provider 支持的数据类型列表。"""
        ...

    async def initialize(self) -> None:
        """初始化 Provider：创建 HTTP Client、限流器、验证配置。

        子类可 override 以添加额外初始化逻辑（如验证 API Key）。
        """
        logger.info(f"[{self.name}] Initializing provider...")

        # 创建 HTTP 客户端
        self._http_client = HTTPClient(self._config)
        await self._http_client.start()

        # 创建限流器
        self._rate_limiter = RateLimiterFactory.create(
            rate=self._config.rate_limit.requests,
            window=self._config.rate_limit.period,
            algorithm="token_bucket",
            burst=self._config.rate_limit.burst,
        )

        # 更新状态
        self._status = ProviderLifecycleStatus.HEALTH_CHECKING
        self._started_at = utcnow()

        logger.info(f"[{self.name}] Provider initialized successfully")

    async def shutdown(self) -> None:
        """优雅关闭：释放连接池、清理资源。

        子类可 override 以添加额外清理逻辑。
        """
        logger.info(f"[{self.name}] Shutting down provider...")

        if self._http_client:
            await self._http_client.close()
            self._http_client = None

        self._status = ProviderLifecycleStatus.DESTROYED
        self._health_state = HealthState.DISABLED

        logger.info(f"[{self.name}] Provider shutdown complete")

    # ---- 状态管理 ----

    def set_status(self, status: ProviderLifecycleStatus) -> None:
        """设置生命周期状态。"""
        old_status = self._status
        self._status = status
        if old_status != status:
            logger.info(f"[{self.name}] Status: {old_status.value} -> {status.value}")

    def set_health_state(self, state: HealthState) -> None:
        """设置健康状态。"""
        old_state = self._health_state
        self._health_state = state
        if old_state != state:
            logger.info(f"[{self.name}] Health: {old_state.value} -> {state.value}")

    def enable(self) -> None:
        """启用 Provider。"""
        self._config.enabled = True
        if self._status == ProviderLifecycleStatus.DISABLED:
            self._status = ProviderLifecycleStatus.INITIALIZING
            logger.info(f"[{self.name}] Provider enabled")

    def disable(self) -> None:
        """禁用 Provider。"""
        self._config.enabled = False
        self._status = ProviderLifecycleStatus.DISABLED
        self._health_state = HealthState.DISABLED
        logger.info(f"[{self.name}] Provider disabled")

    # ---- 指标记录 ----

    def record_success(self, latency_ms: float) -> None:
        """记录一次成功请求。

        Args:
            latency_ms: 响应耗时（毫秒）
        """
        self._total_requests += 1
        self._today_requests += 1
        self._consecutive_successes += 1
        self._consecutive_failures = 0
        self._last_success_time = utcnow()

        # 记录延迟
        self._latencies.append(latency_ms)
        if len(self._latencies) > self._max_latency_history:
            self._latencies.pop(0)

        # 更新状态
        if self._status == ProviderLifecycleStatus.READY:
            self._status = ProviderLifecycleStatus.RUNNING

    def record_failure(self, error: str | None = None, error_type: ErrorType | None = None,
                       status_code: int | None = None) -> None:
        """记录一次失败请求。

        Args:
            error: 错误描述
            error_type: 错误类型
            status_code: HTTP 状态码
        """
        self._total_requests += 1
        self._total_failures += 1
        self._today_requests += 1
        self._today_failures += 1
        self._consecutive_failures += 1
        self._consecutive_successes = 0
        self._last_failure_time = utcnow()

        self._last_error = error
        self._last_error_type = error_type
        self._last_http_status = status_code

    def get_metrics(self) -> ProviderMetrics:
        """获取当前运行时指标快照。"""
        self._check_date_rollover()

        avg_latency = (
            sum(self._latencies) / len(self._latencies) if self._latencies else 0.0
        )
        p95_latency = 0.0
        if self._latencies:
            sorted_latencies = sorted(self._latencies)
            idx = int(len(sorted_latencies) * 0.95)
            p95_latency = sorted_latencies[min(idx, len(sorted_latencies) - 1)]

        success_rate = (
            (self._total_requests - self._total_failures) / self._total_requests
            if self._total_requests > 0
            else 1.0
        )

        uptime = 0.0
        if self._started_at:
            uptime = (utcnow() - self._started_at).total_seconds()

        return ProviderMetrics(
            provider_name=self.name,
            avg_latency_ms=avg_latency,
            p95_latency_ms=p95_latency,
            last_latency_ms=self._latencies[-1] if self._latencies else 0.0,
            total_requests=self._total_requests,
            total_failures=self._total_failures,
            success_rate_1h=success_rate,  # 简化：使用总体成功率
            success_rate_24h=success_rate,
            consecutive_failures=self._consecutive_failures,
            consecutive_successes=self._consecutive_successes,
            today_failures=self._today_failures,
            failure_count_7d=self._total_failures,  # 简化
            uptime_seconds=uptime,
            last_success_time=self._last_success_time,
            last_failure_time=self._last_failure_time,
            network_reachable=self._health_state != HealthState.NETWORK_ERROR,
            reachability_score=1.0 if self._health_state == HealthState.ONLINE else 0.5,
            last_http_status=self._last_http_status,
            is_rate_limited=self._health_state == HealthState.RATE_LIMITED,
            last_error_reason=self._last_error,
            last_error_type=self._last_error_type.value if self._last_error_type else None,
        )

    def get_health_snapshot(self) -> ProviderHealthSnapshot:
        """获取健康快照（供监控面板使用）。"""
        metrics = self.get_metrics()
        return ProviderHealthSnapshot(
            provider_name=self.name,
            category=self.category,
            status=self._health_state,
            is_enabled=self.is_enabled,
            current_priority=self.priority,
            locked=self._config.locked,
            health_score=0.0,  # 由 HealthMonitor 计算填充
            avg_latency_ms=metrics.avg_latency_ms,
            p95_latency_ms=metrics.p95_latency_ms,
            last_latency_ms=metrics.last_latency_ms,
            success_rate_24h=metrics.success_rate_24h,
            consecutive_failures=metrics.consecutive_failures,
            today_failures=metrics.today_failures,
            failure_count_7d=metrics.failure_count_7d,
            last_success_time=metrics.last_success_time,
            last_failure_time=metrics.last_failure_time,
            uptime_seconds=metrics.uptime_seconds,
            network_reachable=metrics.network_reachable,
            proxy_in_use=self._config.proxy,
            last_error_reason=metrics.last_error_reason,
            updated_at=utcnow(),
        )

    # ---- 受保护的请求方法 ----

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """底层 HTTP 请求封装（含限流、重试、超时处理）。

        子类通过此方法发起所有 HTTP 请求，框架自动处理：
        1. Rate Limiting — 等待令牌
        2. Retry — 指数退避重试
        3. Timeout — 超时控制
        4. Error Classification — 错误分类
        5. Metrics — 指标记录

        Args:
            method: HTTP 方法 (GET / POST / ...)
            path: 请求路径
            params: 查询参数
            headers: 额外请求头
            json: JSON Body
            **kwargs: 传递给 httpx 的其他参数

        Returns:
            FetchResult 封装的响应结果
        """
        # HEALTH_CHECKING 状态下放行：健康监测器先置状态再发起探测请求，
        # 若拦截则健康检查永远无法完成（所有 Provider 永远 OFFLINE）
        if (
            not self._http_client
            or (not self.is_available and self._status != ProviderLifecycleStatus.HEALTH_CHECKING)
        ):
            return FetchResult(
                success=False,
                error=f"Provider {self.name} is not available (status={self._status.value})",
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self.name,
            )

        # 限流等待
        if self._rate_limiter:
            await self._rate_limiter.acquire()

        # 发起请求
        if method.upper() == "GET":
            result = await self._http_client.get(path, params=params, headers=headers, **kwargs)
        elif method.upper() == "POST":
            result = await self._http_client.post(
                path, json=json, params=params, headers=headers, **kwargs
            )
        else:
            result = await self._http_client._request(
                method, path, params=params, headers=headers, json=json, **kwargs
            )

        # 记录指标
        if result.success:
            self.record_success(result.response_time_ms)
        else:
            self.record_failure(
                error=result.error,
                error_type=result.error_type,
                status_code=result.status_code,
            )
            # 429 触发自动降速
            if result.error_type == ErrorType.RATE_LIMIT and self._rate_limiter:
                self._rate_limiter.backoff(factor=0.5)
                self._health_state = HealthState.RATE_LIMITED

        return result

    # ---- 内部辅助 ----

    def _check_date_rollover(self) -> None:
        """检查日期翻转，重置每日计数器。"""
        today = utcnow().strftime("%Y-%m-%d")
        if self._today_date != today:
            self._today_date = today
            self._today_requests = 0
            self._today_failures = 0


__all__ = ["BaseProvider"]
