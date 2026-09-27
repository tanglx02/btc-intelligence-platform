"""ProviderManager — Provider 管理器（优先级队列、请求路由）。

职责：
1. 按优先级队列管理同类 Provider
2. 核心方法 execute_with_failover：依次尝试调用，失败自动切换
3. 成功获取的数据写入缓存
4. 记录 Failover 事件
5. 全部失败时返回缓存数据（标记 stale）或错误
"""

from datetime import datetime
from typing import Any

from loguru import logger

from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    ErrorType,
    FailoverEvent,
    FetchResult,
    QualityStatus,
)
from app.utils.datetime_utils import utcnow


class PrioritizedProvider:
    """优先级队列中的 Provider 条目。

    排序键：(effective_priority, -health_score, name)
    - effective_priority: 数值越小优先级越高
    - health_score: 取负使得分数越高越靠前
    """

    __slots__ = ("provider", "effective_priority", "health_score", "locked", "enabled")

    def __init__(
        self,
        provider: BaseProvider,
        effective_priority: int,
        health_score: float = 0.0,
        locked: bool = False,
    ):
        self.provider = provider
        self.effective_priority = effective_priority
        self.health_score = health_score
        self.locked = locked
        self.enabled = provider.is_enabled

    def __lt__(self, other: "PrioritizedProvider") -> bool:
        """比较运算符（用于排序）。"""
        if self.effective_priority != other.effective_priority:
            return self.effective_priority < other.effective_priority
        if self.health_score != other.health_score:
            return self.health_score > other.health_score  # 高分优先
        return self.provider.name < other.provider.name

    @property
    def name(self) -> str:
        return self.provider.name


class ProviderManager:
    """Provider 管理器 — 核心请求路由与 Failover 引擎。

    职责：
    1. 维护每种数据类型的 Provider 优先级队列
    2. 执行带自动 Failover 的数据获取
    3. 记录 Failover 事件
    4. 管理降级策略（全部失败时返回缓存或错误）

    Usage:
        manager = ProviderManager(registry)
        result = await manager.execute_with_failover(
            category="market",
            method="get_current_price",
            symbol="BTC/USDT"
        )
    """

    def __init__(self, registry: ProviderRegistry):
        """初始化 Provider 管理器。

        Args:
            registry: Provider 注册中心实例
        """
        self._registry = registry
        self._priority_queues: dict[str, list[PrioritizedProvider]] = {}
        self._failover_events: list[FailoverEvent] = []
        self._cache: dict[str, tuple[Any, datetime]] = {}  # key -> (data, timestamp)
        self._cache_ttl: int = 300  # 默认缓存 TTL 5 分钟

    # ---- 优先级队列管理 ----

    def build_priority_queue(self, category: str) -> None:
        """构建/重建某数据类别的优先级队列。

        Args:
            category: 数据类别（如 "market", "onchain"）
        """
        providers = self._registry.get_providers(category=category, enabled_only=True)
        queue = []

        for provider in providers:
            entry = PrioritizedProvider(
                provider=provider,
                effective_priority=provider.priority,
                health_score=0.0,  # 由 HealthMonitor 更新
                locked=provider.config.locked,
            )
            queue.append(entry)

        queue.sort()
        self._priority_queues[category] = queue
        logger.debug(f"Priority queue built for '{category}': {[p.name for p in queue]}")

    def get_priority_queue(self, category: str) -> list[PrioritizedProvider]:
        """获取某类别的优先级队列。

        Args:
            category: 数据类别

        Returns:
            按优先级排序的 Provider 列表
        """
        if category not in self._priority_queues:
            self.build_priority_queue(category)
        return self._priority_queues.get(category, [])

    def update_health_score(self, provider_name: str, score: float) -> None:
        """更新 Provider 的健康评分并重新排序队列。

        Args:
            provider_name: Provider 名称
            score: 新的健康评分 (0-100)
        """
        for category, queue in self._priority_queues.items():
            for entry in queue:
                if entry.name == provider_name:
                    entry.health_score = score
                    # 重新排序（跳过 locked 的）
                    if not entry.locked:
                        queue.sort()
                    break

    # ---- 核心方法：带 Failover 的执行 ----

    async def execute_with_failover(
        self,
        category: str,
        method: str,
        data_type: str | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """带自动 Failover 的数据获取入口。

        执行流程：
        1. 获取该类 Provider 优先级队列
        2. 依次尝试调用
        3. 成功则返回结果 + 记录
        4. 失败则切换下一个 + 记录 failover event
        5. 全部失败则返回缓存数据（标记 stale）或错误

        Args:
            category: 数据类别
            method: 要调用的方法名（如 "get_current_price"）
            data_type: 数据类型标识（用于缓存 key，默认使用 category + method）
            **kwargs: 传递给方法的参数

        Returns:
            FetchResult 包含成功数据或降级结果
        """
        data_type = data_type or f"{category}:{method}"
        queue = self.get_priority_queue(category)

        if not queue:
            logger.error(f"No providers available for category '{category}'")
            return self._handle_all_failed(data_type, "No providers registered")

        last_error: str | None = None
        last_error_type: ErrorType | None = None
        attempts = 0

        for entry in queue:
            provider = entry.provider

            # 跳过不可用的 Provider
            if not provider.is_available:
                logger.debug(f"Skipping unavailable provider: {provider.name}")
                continue

            attempts += 1

            # 执行请求
            try:
                method_fn = getattr(provider, method, None)
                if not method_fn or not callable(method_fn):
                    logger.error(f"Provider '{provider.name}' has no method '{method}'")
                    continue

                result = await method_fn(**kwargs)

                if result.success:
                    # 成功：更新缓存并返回
                    self._update_cache(data_type, result.data)
                    result.is_failover = attempts > 1
                    logger.info(
                        f"Fetch success: {data_type} from {provider.name} "
                        f"(attempt {attempts}, {result.response_time_ms:.0f}ms)"
                    )
                    return result

                # 失败：记录错误并继续尝试下一个
                last_error = result.error
                last_error_type = result.error_type

                logger.warning(
                    f"Fetch failed: {data_type} from {provider.name} "
                    f"(error={result.error_type}, attempt {attempts})"
                )

                # 记录 Failover 事件
                self._record_failover_event(
                    data_type=data_type,
                    original_provider=provider.name,
                    reason=result.error_type.value if result.error_type else "unknown",
                    error_detail=result.error or "",
                    latency_ms=result.response_time_ms,
                    retry_count=attempts - 1,
                )

            except Exception as e:
                last_error = f"{type(e).__name__}: {str(e)}"
                last_error_type = ErrorType.UNKNOWN
                logger.exception(f"Unexpected error calling {provider.name}.{method}")

                self._record_failover_event(
                    data_type=data_type,
                    original_provider=provider.name,
                    reason="exception",
                    error_detail=last_error,
                    retry_count=attempts - 1,
                )

        # 所有 Provider 都失败了
        logger.error(
            f"All providers failed for {data_type} "
            f"(attempts={attempts}, last_error={last_error})"
        )
        return self._handle_all_failed(
            data_type,
            last_error or "All providers failed",
            error_type=last_error_type,
        )

    # ---- 缓存管理 ----

    def _update_cache(self, key: str, data: Any) -> None:
        """更新缓存。"""
        self._cache[key] = (data, utcnow())

    def _get_cached(
        self, key: str, max_age_seconds: int | None = None
    ) -> tuple[Any, datetime] | None:
        """获取缓存数据（检查 TTL）。

        Args:
            key: 缓存键
            max_age_seconds: 最大年龄（秒），None 使用默认 TTL

        Returns:
            (data, timestamp)，不存在或超过 max_age 时返回 None（视为缓存未命中，
            调用方应重新获取；降级回退旧缓存的语义由 _handle_all_failed 的
            all-failed 路径以更宽的 max_age 承担）
        """
        if key not in self._cache:
            return None

        data, timestamp = self._cache[key]
        max_age = max_age_seconds if max_age_seconds is not None else self._cache_ttl
        age = (utcnow() - timestamp).total_seconds()

        if age > max_age:
            logger.warning(
                f"Cache expired for {key} "
                f"(age={age:.0f}s > max_age={max_age}s), treating as cache miss"
            )
            return None

        return data, timestamp

    # ---- 降级处理 ----

    def _handle_all_failed(
        self,
        data_type: str,
        error: str,
        error_type: ErrorType | None = None,
    ) -> FetchResult:
        """处理所有 Provider 失败的情况。

        策略：
        1. 尝试返回缓存数据（标记为 STALE）
        2. 无缓存则返回错误（标记为 INVALID）
        3. 绝不生成假数据

        Args:
            data_type: 数据类型
            error: 最后的错误信息
            error_type: 最后的错误分类（用于 INVALID 结果）

        Returns:
            FetchResult 降级结果
        """
        cached = self._get_cached(data_type, max_age_seconds=86400)  # 允许 24h 内的缓存

        if cached:
            data, timestamp = cached
            stale_duration = (utcnow() - timestamp).total_seconds()

            logger.warning(
                f"Returning stale data for {data_type} "
                f"(age={stale_duration:.0f}s, last_updated={timestamp.isoformat()})"
            )

            return FetchResult(
                success=True,
                data=data,
                error=None,
                error_type=None,
                provider_name="cache",
                fetch_time=utcnow(),
                observation_time=timestamp,
                quality_status=QualityStatus.STALE,
                is_stale=True,
                is_failover=False,
                metadata={
                    "is_degraded": True,
                    "stale_duration_seconds": stale_duration,
                    "last_updated": timestamp.isoformat(),
                    "message": "数据暂时无法更新，显示最近一次可信数据",
                },
            )

        # 无缓存可用
        logger.error(f"No data available for {data_type}: {error}")

        return FetchResult(
            success=False,
            data=None,
            error=error,
            error_type=error_type or ErrorType.NETWORK_ERROR,
            provider_name="",
            fetch_time=utcnow(),
            quality_status=QualityStatus.INVALID,
            is_stale=False,
            is_failover=False,
            metadata={
                "is_degraded": True,
                "all_providers_failed": True,
                "message": "数据源未配置或全部不可用",
            },
        )

    # ---- Failover 事件记录 ----

    def _record_failover_event(
        self,
        data_type: str,
        original_provider: str,
        reason: str,
        error_detail: str,
        latency_ms: float = 0.0,
        retry_count: int = 0,
        new_provider: str | None = None,
    ) -> None:
        """记录 Failover 事件。

        Args:
            data_type: 数据类型
            original_provider: 原 Provider
            reason: 切换原因
            error_detail: 错误详情
            latency_ms: 失败请求耗时
            retry_count: 重试次数
            new_provider: 切换后的 Provider（可选）
        """
        event = FailoverEvent(
            timestamp=utcnow(),
            data_type=data_type,
            original_provider=original_provider,
            new_provider=new_provider,
            reason=reason,
            error_detail=error_detail,
            recovery_action="auto_failover",
            retry_count=retry_count,
            latency_ms=latency_ms,
            is_all_failed=new_provider is None,
        )

        self._failover_events.append(event)

        # 限制内存中保存的事件数量
        if len(self._failover_events) > 1000:
            self._failover_events = self._failover_events[-500:]

        logger.debug(
            f"Failover event recorded: {original_provider} -> {new_provider or 'NONE'} "
            f"(reason={reason})"
        )

    def get_failover_events(self, limit: int = 100) -> list[FailoverEvent]:
        """获取最近的 Failover 事件。

        Args:
            limit: 返回数量上限

        Returns:
            Failover 事件列表（按时间倒序）
        """
        return sorted(self._failover_events, key=lambda e: e.timestamp, reverse=True)[:limit]

    def clear_failover_events(self) -> None:
        """清空 Failover 事件记录（主要用于测试）。"""
        self._failover_events.clear()

    # ---- 生命周期 ----

    async def initialize(self) -> None:
        """初始化管理器：初始化所有 Provider 并构建优先级队列。"""
        logger.info("Initializing ProviderManager...")

        # 初始化所有 Provider
        await self._registry.initialize_all()

        # 为每个类别构建优先级队列
        for category in self._registry.get_categories():
            self.build_priority_queue(category)

        logger.info("ProviderManager initialized")

    async def shutdown(self) -> None:
        """关闭管理器：关闭所有 Provider。"""
        logger.info("Shutting down ProviderManager...")
        await self._registry.shutdown_all()
        logger.info("ProviderManager shutdown complete")


__all__ = ["PrioritizedProvider", "ProviderManager"]
