"""CategoryServiceBase — 分类业务服务的公共基类。

统一封装六大类别（onchain / etf / derivatives / options / macro / sentiment）
Service 的公共读取流程，避免重复实现：

1. 本地优先（local-first）：先由子类提供的 local_loader 查询数据库
2. Redis 业务缓存：命中直接返回（is_cached=True）
3. ProviderManager.execute_with_failover：多源自动故障切换
4. 统一 ServiceResult 返回格式

设计约束（与 MarketService / ProviderService 一致）：
- 绝不生成假数据：全部 Provider 失败时返回缓存（STALE）或明确错误（INVALID）
- 所有方法不抛出异常，通过 ServiceResult.success 标识成功/失败
- 数据库不可用时自动降级为纯 API 模式（local_loader 返回 None）
"""

import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from app.providers.base.types import FetchResult, QualityStatus
from app.providers.manager import ProviderManager
from app.services.provider_service import ServiceResult

# 本地数据加载器类型：无参协程，命中返回 dict/list，未命中或 DB 不可用返回 None
LocalLoader = Callable[[], Awaitable[Any]]


class CategoryServiceBase:
    """分类业务服务公共基类。

    子类需设置 ``CATEGORY``（ProviderManager 路由类别）与 ``CACHE_PREFIX``
    （Redis key 命名空间），并按需覆写 ``DEFAULT_CACHE_TTL``。
    """

    #: ProviderManager.execute_with_failover 使用的数据类别
    CATEGORY: str = ""
    #: Redis 缓存 key 前缀
    CACHE_PREFIX: str = "svc:"
    #: 默认业务缓存 TTL（秒）
    DEFAULT_CACHE_TTL: int = 300

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        """初始化服务。

        Args:
            provider_manager: Provider 管理器（execute_with_failover 入口）
            data_store: 标准化数据存储（可选，预留接口）
            cache: Redis 异步客户端（可选，None 时禁用业务缓存）
        """
        self._manager = provider_manager
        self._data_store = data_store
        self._cache = cache

    # ---- 核心读取流程 ----

    async def get_data(
        self,
        method: str,
        *,
        data_type: str | None = None,
        cache_ttl: int | None = None,
        use_cache: bool = True,
        local_loader: Optional[LocalLoader] = None,
        **kwargs: Any,
    ) -> ServiceResult:
        """分类服务统一数据获取入口（本地优先 + 缓存 + Failover）。

        Args:
            method: Provider 方法名（如 "get_mvrv"）
            data_type: 数据类型标识（用于缓存 key，默认 CATEGORY:method）
            cache_ttl: 缓存 TTL（秒），None 使用 DEFAULT_CACHE_TTL
            use_cache: 是否使用 Redis 业务缓存
            local_loader: 本地 DB 加载器（可选，命中则优先返回）
            **kwargs: 传递给 Provider 方法的参数

        Returns:
            ServiceResult 统一业务结果
        """
        started = datetime.utcnow()
        data_type = data_type or f"{self.CATEGORY}:{method}"
        cache_key = self._cache_key(data_type, kwargs)

        # 1. Redis 业务缓存
        if use_cache:
            cached = await self._cache_get(cache_key)
            if cached is not None:
                return ServiceResult(
                    success=True,
                    data=cached.get("data"),
                    quality_status=QualityStatus(
                        cached.get("quality_status", QualityStatus.VERIFIED.value)
                    ),
                    source=cached.get("source", "cache"),
                    is_cached=True,
                    fetch_time=started,
                    response_time_ms=self._elapsed_ms(started),
                    metadata={**cached.get("metadata", {}), "cache_hit": True},
                )

        # 2. 本地优先（数据库）
        if local_loader is not None:
            try:
                local = await local_loader()
            except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
                logger.warning(f"Local loader failed for {data_type}: {e}")
                local = None
            if local is not None:
                service_result = ServiceResult(
                    success=True,
                    data=local,
                    quality_status=QualityStatus.VERIFIED,
                    source="local_db",
                    fetch_time=started,
                    response_time_ms=self._elapsed_ms(started),
                    metadata={"local_first": True},
                )
                await self._cache_set(
                    cache_key, service_result, cache_ttl or self.DEFAULT_CACHE_TTL
                )
                return service_result

        # 3. Provider Failover
        result = await self._manager.execute_with_failover(
            category=self.CATEGORY, method=method, data_type=data_type, **kwargs
        )
        service_result = self._from_fetch(result, started)

        if result.success and not result.is_stale and use_cache:
            await self._cache_set(
                cache_key, service_result, cache_ttl or self.DEFAULT_CACHE_TTL
            )
        return service_result

    # ---- Redis 缓存内部方法 ----

    def _cache_key(self, data_type: str, kwargs: dict[str, Any]) -> str:
        """构建缓存 key（参数序列化保证顺序稳定）。"""
        try:
            param_str = json.dumps(kwargs, sort_keys=True, default=str)
        except (TypeError, ValueError):
            param_str = str(sorted(kwargs.items()))
        return f"{self.CACHE_PREFIX}{data_type}:{param_str}"

    async def _cache_get(self, key: str) -> dict[str, Any] | None:
        """读取缓存（JSON 反序列化），故障时降级为未命中。"""
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            if not raw:
                return None
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001 - 缓存故障不影响主流程
            logger.warning(f"{self.CATEGORY} cache read error for {key}: {e}")
            return None

    async def _cache_set(self, key: str, result: ServiceResult, ttl: int) -> None:
        """写入缓存（ServiceResult 关键字段序列化）。"""
        if self._cache is None:
            return
        try:
            payload = {
                "data": result.data,
                "quality_status": result.quality_status.value,
                "source": result.source,
                "metadata": result.metadata,
                "cached_at": datetime.utcnow().isoformat(),
            }
            await self._cache.set(key, json.dumps(payload, default=str), ex=ttl)
        except Exception as e:  # noqa: BLE001 - 缓存写入失败不影响主流程
            logger.warning(f"{self.CATEGORY} cache write error for {key}: {e}")

    # ---- 结果封装工具 ----

    @staticmethod
    def _from_fetch(result: FetchResult, started: datetime) -> ServiceResult:
        """FetchResult -> ServiceResult（带端到端耗时）。"""
        service_result = ServiceResult.from_fetch_result(result)
        service_result.response_time_ms = CategoryServiceBase._elapsed_ms(started)
        return service_result

    @staticmethod
    def _invalid(error: str) -> ServiceResult:
        """构造参数非法的失败结果。"""
        return ServiceResult(
            success=False,
            error=error,
            quality_status=QualityStatus.INVALID,
            fetch_time=datetime.utcnow(),
        )

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        """naive datetime 视为 UTC 并附加时区（DB 列为 timestamptz）。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def _elapsed_ms(started: datetime) -> float:
        """计算自 started 起的耗时（毫秒）。"""
        return (datetime.utcnow() - started).total_seconds() * 1000


__all__ = ["CategoryServiceBase", "LocalLoader"]
