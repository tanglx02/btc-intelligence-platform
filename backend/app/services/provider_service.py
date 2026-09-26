"""ProviderService — 业务层统一数据入口。

职责：
1. 封装 ProviderManager / FailoverEngine 调用，业务层不直接依赖具体 Provider
2. 添加业务级缓存（Redis），降低外部 API 压力
3. 数据标准化（统一 symbol、时间戳、字段命名）
4. 返回统一的 ServiceResult（data + metadata + quality_status）
5. 生命周期编排（初始化 Registry / Manager / HealthMonitor / FailoverEngine）

设计约束：
- 绝不生成假数据：全部 Provider 失败时返回缓存（标记 STALE）或明确错误（INVALID）
- 所有方法不抛出异常，通过 ServiceResult.success 标识成功/失败
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from app.core.config import settings
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    QualityStatus,
)
from app.providers.failover import FailoverEngine
from app.providers.health_monitor import HealthMonitor
from app.providers.manager import ProviderManager


@dataclass
class ServiceResult:
    """业务层统一返回结构。

    Attributes:
        success: 是否成功获取到可用数据
        data: 标准化后的业务数据
        quality_status: 数据质量状态（VERIFIED / ESTIMATED / STALE / CONFLICT / INVALID）
        source: 数据来源 Provider 名称
        is_failover: 是否经过故障切换
        is_stale: 是否为过期缓存数据
        is_cached: 是否命中业务缓存
        observation_time: 数据观测时间
        fetch_time: 数据抓取时间
        response_time_ms: 端到端耗时（毫秒）
        error: 错误描述
        error_type: 错误分类
        metadata: 扩展元信息
    """

    success: bool
    data: Any = None
    quality_status: QualityStatus = QualityStatus.ESTIMATED
    source: str = ""
    is_failover: bool = False
    is_stale: bool = False
    is_cached: bool = False
    observation_time: datetime | None = None
    fetch_time: datetime = field(default_factory=datetime.utcnow)
    response_time_ms: float = 0.0
    error: str | None = None
    error_type: ErrorType | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_fetch_result(cls, result: FetchResult, *, is_cached: bool = False) -> "ServiceResult":
        """从 FetchResult 构造 ServiceResult。

        Args:
            result: Provider 层返回的结果
            is_cached: 是否命中业务缓存

        Returns:
            ServiceResult 实例
        """
        return cls(
            success=result.success,
            data=result.data,
            quality_status=result.quality_status,
            source=result.provider_name,
            is_failover=result.is_failover,
            is_stale=result.is_stale,
            is_cached=is_cached,
            observation_time=result.observation_time,
            fetch_time=result.fetch_time,
            response_time_ms=result.response_time_ms,
            error=result.error,
            error_type=result.error_type,
            metadata=dict(result.metadata),
        )


class ProviderService:
    """Provider 业务层统一入口。

    编排 Provider 子系统的完整生命周期，并为业务层（API / Scheduler）提供
    统一的数据获取接口。业务层只需调用 ``get_data`` 或分类便捷方法，
    无需关心 Provider 选择、Failover、缓存等细节。

    Usage:
        service = ProviderService()
        await service.startup()

        result = await service.get_data("market", "get_current_price", symbol="BTC/USDT")
        if result.success:
            print(result.data, result.quality_status)

        await service.shutdown()
    """

    # 业务缓存 key 前缀与默认 TTL
    CACHE_PREFIX = "provider:svc:"
    DEFAULT_CACHE_TTL = 60  # 秒

    def __init__(
        self,
        registry: ProviderRegistry | None = None,
        redis_client: Any = None,
        config_path: str = "config/providers.yaml",
        enable_cache: bool = True,
    ):
        """初始化 ProviderService。

        Args:
            registry: Provider 注册中心（默认使用全局单例）
            redis_client: Redis 异步客户端（可选，None 时按配置惰性创建）
            config_path: Provider YAML 配置文件路径
            enable_cache: 是否启用业务级缓存
        """
        self._registry = registry or ProviderRegistry()
        self._config_path = config_path
        self._enable_cache = enable_cache

        # 核心组件（startup 时构建）
        self._manager = ProviderManager(self._registry)
        self._health_monitor = HealthMonitor(self._registry, manager=self._manager)
        self._failover_engine = FailoverEngine(self._registry, health_monitor=self._health_monitor)

        # Redis 缓存客户端
        self._redis: Any = redis_client
        self._redis_owned = redis_client is None  # 是否由本服务创建（负责关闭）
        self._started = False

    # ---- 生命周期 ----

    async def startup(self) -> None:
        """启动 Provider 子系统。

        流程：
        1. 自动发现 Provider 类
        2. 从 YAML 配置加载并注册 Provider
        3. 初始化所有 Provider + 构建优先级队列
        4. 初始化 Redis 连接
        5. 启动健康监控 + 故障切换引擎
        """
        if self._started:
            logger.warning("ProviderService already started")
            return

        logger.info("Starting ProviderService...")

        # 1. 自动发现 Provider 类
        self._registry.auto_discover("app.providers")

        # 2. 从配置加载并注册
        try:
            count = self._registry.load_from_config(self._config_path)
            logger.info(f"Registered {count} providers from {self._config_path}")
        except FileNotFoundError:
            logger.warning(f"Provider config not found: {self._config_path}, skip loading")

        # 3. 初始化 Manager（含所有 Provider initialize + 优先级队列）
        await self._manager.initialize()

        # 4. Redis 连接
        if self._enable_cache:
            await self._init_redis()

        # 5. 启动监控与故障切换引擎
        await self._health_monitor.start()
        await self._failover_engine.start()

        self._started = True
        logger.info("ProviderService started")

    async def shutdown(self) -> None:
        """关闭 Provider 子系统，释放所有资源。"""
        if not self._started:
            return

        logger.info("Shutting down ProviderService...")

        await self._failover_engine.stop()
        await self._health_monitor.stop()
        await self._manager.shutdown()

        if self._redis is not None and self._redis_owned:
            try:
                await self._redis.aclose()
            except Exception as e:  # noqa: BLE001 - 关闭阶段容错
                logger.warning(f"Error closing Redis connection: {e}")
            self._redis = None

        self._started = False
        logger.info("ProviderService shutdown complete")

    async def _init_redis(self) -> None:
        """惰性初始化 Redis 异步客户端。"""
        if self._redis is not None:
            return
        try:
            from redis.asyncio import Redis

            self._redis = Redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            await self._redis.ping()
            logger.info("ProviderService Redis cache connected")
        except Exception as e:  # noqa: BLE001 - 缓存不可用不影响主流程
            logger.warning(f"Redis unavailable, business cache disabled: {e}")
            self._redis = None

    # ---- 核心数据获取 ----

    async def get_data(
        self,
        category: str,
        method: str,
        *,
        data_type: str | None = None,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        **kwargs: Any,
    ) -> ServiceResult:
        """业务层统一数据获取入口。

        执行流程：
        1. 命中业务缓存则直接返回（标记 is_cached）
        2. 通过 ProviderManager 执行带 Failover 的获取
        3. 成功则写入业务缓存
        4. 统一封装为 ServiceResult 返回

        Args:
            category: 数据类别（market / onchain / etf / ...）
            method: Provider 方法名（如 "get_current_price"）
            data_type: 数据类型标识（用于缓存 key）
            use_cache: 是否使用业务缓存
            cache_ttl: 缓存 TTL（秒），None 使用默认值
            **kwargs: 传递给 Provider 方法的参数

        Returns:
            ServiceResult 统一业务结果
        """
        started = datetime.utcnow()
        data_type = data_type or f"{category}:{method}"
        cache_key = self._build_cache_key(data_type, kwargs)

        # 1. 尝试命中缓存
        if use_cache and self._enable_cache:
            cached = await self._get_cache(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit: {cache_key}")
                result = ServiceResult.from_fetch_result(cached["result"], is_cached=True)
                result.response_time_ms = self._elapsed_ms(started)
                return result

        # 2. 执行带 Failover 的获取
        fetch_result = await self._manager.execute_with_failover(
            category=category,
            method=method,
            data_type=data_type,
            **kwargs,
        )

        # 3. 成功则写缓存
        if fetch_result.success and not fetch_result.is_stale and use_cache and self._enable_cache:
            ttl = cache_ttl or self.DEFAULT_CACHE_TTL
            await self._set_cache(cache_key, fetch_result, ttl)

        # 4. 封装结果
        service_result = ServiceResult.from_fetch_result(fetch_result)
        service_result.response_time_ms = self._elapsed_ms(started)
        return service_result

    # ---- 分类便捷方法 ----

    async def get_current_price(self, symbol: str = "BTC/USDT", **kwargs: Any) -> ServiceResult:
        """获取当前价格（市场行情）。"""
        return await self.get_data(
            "market", "get_current_price", symbol=symbol, cache_ttl=15, **kwargs
        )

    async def get_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
        **kwargs: Any,
    ) -> ServiceResult:
        """获取 K 线数据（市场行情）。"""
        return await self.get_data(
            "market",
            "get_ohlcv",
            symbol=symbol,
            interval=interval,
            start=start,
            end=end,
            limit=limit,
            cache_ttl=300,
            **kwargs,
        )

    async def get_24h_stats(self, symbol: str = "BTC/USDT", **kwargs: Any) -> ServiceResult:
        """获取 24 小时统计数据（市场行情）。"""
        return await self.get_data(
            "market", "get_24h_stats", symbol=symbol, cache_ttl=60, **kwargs
        )

    # ---- 运维/监控接口 ----

    def list_providers(self) -> list[dict[str, Any]]:
        """列出所有已注册 Provider 及状态（供监控面板使用）。"""
        return self._registry.list_all()

    def get_failover_events(self, limit: int = 100) -> list[dict[str, Any]]:
        """获取最近的故障切换事件（供监控面板使用）。"""
        events = self._manager.get_failover_events(limit=limit)
        return [
            {
                "event_id": str(e.event_id),
                "timestamp": e.timestamp.isoformat(),
                "data_type": e.data_type,
                "original_provider": e.original_provider,
                "new_provider": e.new_provider,
                "reason": e.reason,
                "error_detail": e.error_detail,
                "retry_count": e.retry_count,
                "latency_ms": e.latency_ms,
                "is_all_failed": e.is_all_failed,
            }
            for e in events
        ]

    async def check_provider_health(self, provider_name: str) -> dict[str, Any] | None:
        """立即对指定 Provider 执行一次健康检查。

        Args:
            provider_name: Provider 名称

        Returns:
            健康快照字典，Provider 不存在返回 None
        """
        score = await self._health_monitor.check_provider_now(provider_name)
        if score is None:
            return None

        provider = self._registry.get_provider(provider_name)
        snapshot = provider.get_health_snapshot() if provider else None
        return {
            "name": provider_name,
            "category": snapshot.category if snapshot else "",
            "status": snapshot.status.value if snapshot else "unknown",
            "health_score": score,
            "avg_latency_ms": snapshot.avg_latency_ms if snapshot else 0.0,
            "success_rate_24h": snapshot.success_rate_24h if snapshot else 1.0,
        }

    # ---- 缓存内部方法 ----

    def _build_cache_key(self, data_type: str, kwargs: dict[str, Any]) -> str:
        """构建业务缓存 key。

        Args:
            data_type: 数据类型标识
            kwargs: 方法参数

        Returns:
            缓存 key 字符串
        """
        # 参数序列化（保证顺序稳定）
        try:
            param_str = json.dumps(kwargs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            param_str = str(sorted(kwargs.items()))
        return f"{self.CACHE_PREFIX}{data_type}:{param_str}"

    async def _get_cache(self, key: str) -> dict[str, Any] | None:
        """读取业务缓存并反序列化为 FetchResult。"""
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if not raw:
                return None
            payload = json.loads(raw)
            quality_raw = payload.get("quality_status", QualityStatus.ESTIMATED.value)
            result = FetchResult(
                success=payload.get("success", True),
                data=payload.get("data"),
                provider_name=payload.get("provider_name", "cache"),
                quality_status=QualityStatus(quality_raw),
                is_failover=payload.get("is_failover", False),
                metadata=payload.get("metadata", {}),
            )
            return {"result": result, "raw": payload}
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.warning(f"Cache deserialize failed for {key}: {e}")
            return None
        except Exception as e:  # noqa: BLE001 - 缓存故障降级为未命中
            logger.warning(f"Cache read error for {key}: {e}")
            return None

    async def _set_cache(self, key: str, result: FetchResult, ttl: int) -> None:
        """写入业务缓存。"""
        if self._redis is None:
            return
        try:
            payload = {
                "success": result.success,
                "data": result.data,
                "provider_name": result.provider_name,
                "quality_status": result.quality_status.value,
                "is_failover": result.is_failover,
                "metadata": result.metadata,
                "cached_at": datetime.utcnow().isoformat(),
            }
            await self._redis.set(key, json.dumps(payload, default=str), ex=ttl)
        except (TypeError, ValueError) as e:
            logger.warning(f"Cache serialize failed for {key}: {e}")
        except Exception as e:  # noqa: BLE001 - 缓存写入失败不影响主流程
            logger.warning(f"Cache write error for {key}: {e}")

    async def invalidate_cache(self, data_type: str | None = None) -> int:
        """失效业务缓存。

        Args:
            data_type: 指定数据类型（None 表示清空全部 provider 业务缓存）

        Returns:
            删除的 key 数量
        """
        if self._redis is None:
            return 0
        pattern = f"{self.CACHE_PREFIX}{data_type}*" if data_type else f"{self.CACHE_PREFIX}*"
        deleted = 0
        try:
            async for key in self._redis.scan_iter(match=pattern, count=100):
                await self._redis.delete(key)
                deleted += 1
        except Exception as e:  # noqa: BLE001 - 缓存清理容错
            logger.warning(f"Cache invalidate error: {e}")
        logger.info(f"Invalidated {deleted} cache keys (pattern={pattern})")
        return deleted

    # ---- 工具方法 ----

    @staticmethod
    def _elapsed_ms(started: datetime) -> float:
        """计算自 started 起的耗时（毫秒）。"""
        return (datetime.utcnow() - started).total_seconds() * 1000


# ---- 全局单例（依赖注入入口）----

_provider_service: ProviderService | None = None


def get_provider_service() -> ProviderService:
    """获取全局 ProviderService 单例（FastAPI 依赖注入入口）。

    Returns:
        ProviderService 实例
    """
    global _provider_service
    if _provider_service is None:
        _provider_service = ProviderService()
    return _provider_service


def set_provider_service(service: ProviderService | None) -> None:
    """设置/重置全局 ProviderService 单例（主要用于测试）。

    Args:
        service: ProviderService 实例，None 表示重置
    """
    global _provider_service
    _provider_service = service


__all__ = [
    "ProviderService",
    "ServiceResult",
    "get_provider_service",
    "set_provider_service",
]
