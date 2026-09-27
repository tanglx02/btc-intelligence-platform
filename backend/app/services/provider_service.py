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
from sqlalchemy import select

from app.core.config import settings
from app.providers.base.config import ConfigLoader, ProviderConfig
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderLifecycleStatus,
    QualityStatus,
)
from app.providers.failover import FailoverEngine
from app.providers.health_monitor import HealthMonitor
from app.providers.manager import ProviderManager
from app.services.provider_config_service import (
    LIGHTWEIGHT_FIELDS,
    get_provider_config_service,
)
from app.services.settings_service import get_settings_service
from app.utils.datetime_utils import utcnow


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
    fetch_time: datetime = field(default_factory=utcnow)
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
        2. YAML -> DB 导入（providers 表空时播种；DB 不可用容错跳过）
        3. 以 DB 配置为基准注册全部 Provider（DB 不可用时回退 YAML 直载）
        4. 接入全局 failover / health_check 运行时配置（SettingsService）
        5. 初始化所有 Provider + 构建优先级队列
        6. 初始化 Redis 连接
        7. 启动健康监控 + 故障切换引擎
        """
        if self._started:
            logger.warning("ProviderService already started")
            return

        logger.info("Starting ProviderService...")

        # 1. 自动发现 Provider 类
        self._registry.auto_discover("app.providers")

        # 2. YAML -> DB 导入（首次启动播种；失败不阻断）
        try:
            imported = await get_provider_config_service().import_yaml_to_db()
            if imported:
                logger.info(f"Imported {imported} providers from YAML into DB")
        except Exception as e:  # noqa: BLE001 - DB 不可用时回退 YAML 加载
            logger.warning(f"Import providers.yaml to DB failed: {e}")

        # 3. 以 DB 配置注册（DB 不可用/无结果时回退 YAML 直载，保持旧行为）
        registered = await self.reload_all(initialize=False)
        if registered == 0:
            try:
                count = self._registry.load_from_config(self._config_path)
                logger.info(f"Registered {count} providers from {self._config_path}")
            except FileNotFoundError:
                logger.warning(f"Provider config not found: {self._config_path}, skip loading")

        # 4. 接入全局 failover / health_check 配置（无覆盖时保持默认，行为不变）
        await self._apply_runtime_config()

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

    # ---- 热重载与配置变更（配置后台化核心路径）----

    async def reload_all(self, *, initialize: bool = False) -> int:
        """以 DB 配置重建全部 Provider（启动时使用）。

        遍历 providers 表逐个注册/替换实例；DB 不可用返回 0（调用方回退
        YAML）。禁用（is_enabled=False）的 Provider 不注册运行时。

        Args:
            initialize: 是否立即 initialize 新实例（启动路径由
                ``Manager.initialize`` 统一执行，传 False 避免重复初始化）。

        Returns:
            成功注册的 Provider 数量。
        """
        from app.core.database import get_db_session_ctx
        from app.models.provider import Provider

        try:
            async with get_db_session_ctx() as session:
                names = (await session.execute(select(Provider.name))).scalars().all()
        except Exception as e:  # noqa: BLE001 - DB 不可用由调用方回退 YAML
            logger.warning(f"reload_all: query providers table failed: {e}")
            return 0

        registered = 0
        for name in names:
            try:
                if await self._register_from_config(name, initialize=initialize):
                    registered += 1
            except Exception as e:  # noqa: BLE001 - 单个失败不影响其余
                logger.error(f"reload_all: register '{name}' failed: {e}")
        logger.info(f"reload_all complete: {registered}/{len(names)} providers active")
        return registered

    async def reload_provider(self, name: str) -> bool:
        """热重载单个 Provider（配置后台化核心路径）。

        流程：先构建新配置（失败不动旧实例）→ 旧实例 shutdown（释放
        连接池）→ unregister → 新实例 register → initialize → 重建该
        类别优先级队列 → 清理健康评分状态。
        HealthMonitor / FailoverEngine 每轮从 registry 动态遍历，
        实例替换后自动跟随。

        Returns:
            是否重载成功（禁用实例被摘除返回 False，但运行时状态已同步）。
        """
        ok = await self._register_from_config(name, initialize=True)
        if ok:
            logger.info(f"Provider reloaded: {name}")
        return ok

    async def apply_config_change(self, name: str, updates: dict[str, Any]) -> dict[str, Any]:
        """应用 Provider 配置变更（轻量字段免重建，其余完整热重载）。

        轻量字段（is_enabled/priority/is_locked）直接改运行实例并重建
        优先级队列；其余字段（base_url/proxy/timeout/凭据等）触发
        :meth:`reload_provider` 重建实例（HTTPClient.start() 已把配置
        固化进 httpx.AsyncClient，改配置必须重建）。

        Returns:
            {"applied": [已应用字段], "reloaded": 是否完整重载}
        """
        applied: list[str] = []
        lightweight = {
            k: v for k, v in updates.items() if k in LIGHTWEIGHT_FIELDS and v is not None
        }
        if lightweight:
            provider = self._registry.get_provider(name)
            if provider is not None:
                if "is_enabled" in lightweight:
                    if lightweight["is_enabled"]:
                        self._registry.enable_provider(name)
                    else:
                        self._registry.disable_provider(name)
                    applied.append("is_enabled")
                if "priority" in lightweight:
                    self._registry.update_priority(name, int(lightweight["priority"]))
                    applied.append("priority")
                if "is_locked" in lightweight:
                    provider.config.locked = bool(lightweight["is_locked"])
                    applied.append("is_locked")
                self._manager.build_priority_queue(provider.category)
            else:
                logger.debug(
                    f"apply_config_change: '{name}' not in runtime, skip light apply"
                )

        heavy = [k for k in updates if k not in LIGHTWEIGHT_FIELDS]
        reloaded = False
        if heavy:
            reloaded = await self.reload_provider(name)
            applied.extend(heavy)

        return {"applied": applied, "reloaded": reloaded}

    async def _register_from_config(self, name: str, *, initialize: bool = True) -> bool:
        """按 DB 配置（YAML 兑底）注册/替换 Provider 实例。"""
        config = await get_provider_config_service().build_provider_config(name)
        if config is None:
            config = self._build_config_from_yaml(name)
        if config is None:
            logger.warning(f"reload: no config for '{name}' (DB & YAML), skipped")
            return False

        # 旧实例 shutdown（释放连接池）后注销
        old = self._registry.get_provider(name)
        if old is not None:
            try:
                await old.shutdown()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"reload: shutdown old '{name}' failed: {e}")
            self._registry.unregister(name)

        # 禁用实例：仅摘除（与 load_from_config 的 skip 语义一致）
        if not config.enabled:
            self._manager.build_priority_queue(config.category)
            self._health_monitor._score_states.pop(name, None)
            logger.info(f"reload: '{name}' is disabled, removed from runtime")
            return False

        provider_class = self._registry.get_provider_class(name)
        if provider_class is None:
            logger.error(f"reload: no provider class found for '{name}'")
            return False

        try:
            provider = provider_class(config)
            self._registry.register(provider)
        except Exception as e:  # noqa: BLE001
            logger.error(f"reload: register '{name}' failed: {e}")
            return False

        if initialize:
            try:
                await provider.initialize()
                provider.set_status(ProviderLifecycleStatus.READY)
            except Exception as e:  # noqa: BLE001
                logger.error(f"reload: initialize '{name}' failed: {e}")
                provider.set_status(ProviderLifecycleStatus.OFFLINE)

        # 重建队列 + 清理评分状态（实例替换后监控/故障切换自动跟随）
        self._manager.build_priority_queue(config.category)
        self._health_monitor._score_states.pop(name, None)
        return True

    def _build_config_from_yaml(self, name: str) -> ProviderConfig | None:
        """从 providers.yaml 解析单个 Provider 配置（DB 不可用时的兑底）。"""
        try:
            data = ConfigLoader.load(self._config_path)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"reload: load yaml config failed: {e}")
            return None
        for category, providers in (data.get("providers") or {}).items():
            if isinstance(providers, dict) and isinstance(providers.get(name), dict):
                return ConfigLoader.parse_provider_config(
                    name=name,
                    category=category,
                    raw=providers[name],
                    defaults=data.get("defaults") or {},
                )
        return None

    async def _apply_runtime_config(self) -> None:
        """将 SettingsService 中的全局 failover/health_check 配置接入组件。

        键空间（DB 无值时回退 env/默认值，不改变运行行为）：
        - provider.health_check.interval / .weights / .params
        - provider.failover.recovery（整段 dict）或逐键 provider.failover.*
        - provider.failover.anti_flapping（整段 dict）
        """
        svc = get_settings_service()

        interval = await svc.get("provider.health_check.interval", None)
        weights = await svc.get("provider.health_check.weights", None)
        params = await svc.get("provider.health_check.params", None)
        self._health_monitor.apply_runtime_config(
            check_interval=interval,
            weights=weights if isinstance(weights, dict) else None,
            params=params if isinstance(params, dict) else None,
        )

        recovery = await svc.get("provider.failover.recovery", None)
        if not isinstance(recovery, dict):
            recovery = {}
            for field_name in (
                "recovery_threshold",
                "probe_interval_initial",
                "probe_interval_max",
                "probe_backoff_multiplier",
                "min_observation_window",
                "trial_request_count",
                "trial_success_rate",
            ):
                value = await svc.get(f"provider.failover.{field_name}", None)
                if value is not None:
                    recovery[field_name] = value
        anti_flapping = await svc.get("provider.failover.anti_flapping", None)
        self._failover_engine.apply_runtime_config(
            recovery=recovery or None,
            anti_flapping=anti_flapping if isinstance(anti_flapping, dict) else None,
        )

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

    # ---- 底层组件访问 ----

    @property
    def manager(self) -> ProviderManager:
        """底层 ProviderManager。

        历史同步、补洞等场景需要 ``FetchResult.raw_response`` 以写入 Raw 表，
        而 ``ServiceResult`` 不携带原始响应，因此开放只读访问入口。
        """
        return self._manager

    @property
    def registry(self) -> ProviderRegistry:
        """底层 Provider 注册中心（只读）。"""
        return self._registry

    @property
    def health_monitor(self) -> HealthMonitor:
        """底层健康监控器（只读）。"""
        return self._health_monitor

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
        started = utcnow()
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
                "cached_at": utcnow().isoformat(),
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
        return (utcnow() - started).total_seconds() * 1000


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
