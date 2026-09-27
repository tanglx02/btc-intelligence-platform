"""历史数据同步服务 — 断点续传 + 批量请求 + 限速 + 进度上报。

对应架构文档：
- 《11-historical-data.md》§3 断点续传（Checkpoint）机制
- 《11-historical-data.md》§4 历史数据同步任务管理
- 《11-historical-data.md》§4.5 批量写入优化 / §4.6 Rate Limit 尊重

核心保证（文档 §3.2 关键实现规则）：

1. **原子推进**：数据写入与 checkpoint 更新在 **同一数据库事务** 内提交，
   崩溃时要么"这批数据 + 进度"都在，要么都不在；
2. **崩溃恢复**：进程重启后从 ``last_synced_time + 1 period`` 继续；
3. **幂等写入**：依赖 ``NormalizedDataStore`` 的 ``(source_id, observation_time)`` 去重；
4. **并发控制**：进程内任务注册表 + Redis 分布式锁，防止同一 checkpoint 被双写；
5. **连续错误熔断**：``retry_count`` 超过 ``max_retries`` 进入 FAILED，任何一次成功即清零；
6. **限速尊重**：主动令牌桶限速，宁可回填慢，不可触发封禁。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db_session_ctx, get_session_factory
from app.models import Provider, SyncCheckpoint
from app.models.enums import ProviderCategory, SyncStatus
from app.providers.base.types import FetchResult
from app.services.data_normalizer import (
    NIL_UUID,
    DataNormalizer,
    NormalizationResult,
    apply_source_id,
    get_data_normalizer,
)
from app.services.data_store import (
    NormalizedDataStore,
    RawDataStore,
    StoreResult,
    get_normalized_data_store,
    get_raw_data_store,
    provider_id_resolver,
    utcnow,
)

#: Redis 进度频道前缀
PROGRESS_CHANNEL_PREFIX = "sync:progress:"
#: 全局进度频道（前端订阅单一频道即可看到所有任务）
PROGRESS_CHANNEL_ALL = "sync:progress:all"
#: 分布式锁 key 前缀与默认 TTL（秒）
LOCK_KEY_PREFIX = "sync:lock:"
DEFAULT_LOCK_TTL = 3600

#: 同步失败后的指数退避序列（秒）
DEFAULT_BACKOFF_SEQUENCE: tuple[int, ...] = (5, 30, 120, 600, 1800)


# ==========================================================================
# 同步规格定义
# ==========================================================================


@dataclass(frozen=True, slots=True)
class SyncSpec:
    """某类数据的同步规格。

    Attributes:
        data_type: 对外暴露的数据类型标识
        category: Provider 数据类别
        method: Provider 方法名
        normalize_kind: 标准化数据形态
        interval_seconds: 预期数据间隔（秒），用于计算批次时间窗
        default_interval: 默认 K 线周期（仅行情类）
        default_batch_limit: 单批最大请求条数（Provider 支持的 limit 上限）
        time_window_seconds: 单次请求覆盖的时间跨度；None 表示按 limit 推进
        requires_metric: 是否需要额外的 metric / indicator / series_id 参数
        store_method: ``NormalizedDataStore`` 上对应的写入方法名
        priority: 任务优先级（数字越小越优先，文档 §4.2）
    """

    data_type: str
    category: ProviderCategory
    method: str
    normalize_kind: str
    interval_seconds: int
    default_interval: str | None = None
    default_batch_limit: int = 500
    time_window_seconds: int | None = None
    requires_metric: bool = False
    store_method: str = "store"
    priority: int = 50


#: 数据类型 → 同步规格
SYNC_SPECS: dict[str, SyncSpec] = {
    # ---- P0/P1 核心价格数据 ----
    "candles": SyncSpec(
        data_type="candles", category=ProviderCategory.MARKET, method="get_ohlcv",
        normalize_kind="ohlcv", interval_seconds=3600, default_interval="1h",
        default_batch_limit=1000, time_window_seconds=86400 * 30,
        store_method="store_candles", priority=10,
    ),
    "market": SyncSpec(
        data_type="market", category=ProviderCategory.MARKET, method="get_ohlcv",
        normalize_kind="ohlcv", interval_seconds=3600, default_interval="1h",
        default_batch_limit=1000, time_window_seconds=86400 * 30,
        store_method="store_candles", priority=10,
    ),
    "price": SyncSpec(
        data_type="price", category=ProviderCategory.MARKET, method="get_current_price",
        normalize_kind="price", interval_seconds=60, default_batch_limit=1,
        store_method="store_prices", priority=10,
    ),
    # ---- P1 链上数据 ----
    "onchain": SyncSpec(
        data_type="onchain", category=ProviderCategory.ONCHAIN, method="get_metric_history",
        normalize_kind="metric", interval_seconds=86400, default_batch_limit=500,
        time_window_seconds=86400 * 365, requires_metric=True,
        store_method="store_onchain_metrics", priority=20,
    ),
    # ---- P1 衍生品 ----
    "derivatives": SyncSpec(
        data_type="derivatives", category=ProviderCategory.DERIVATIVES,
        method="get_funding_rate", normalize_kind="funding", interval_seconds=8 * 3600,
        default_batch_limit=1000, store_method="store_derivatives", priority=40,
    ),
    # ---- P2 ETF ----
    "etf": SyncSpec(
        data_type="etf", category=ProviderCategory.ETF, method="get_cumulative_flow",
        normalize_kind="flow", interval_seconds=86400, default_batch_limit=500,
        time_window_seconds=86400 * 365, store_method="store_etf_flows", priority=30,
    ),
    # ---- P2 宏观 ----
    "macro": SyncSpec(
        data_type="macro", category=ProviderCategory.MACRO, method="get_macro_series",
        normalize_kind="series", interval_seconds=86400 * 30, default_batch_limit=500,
        time_window_seconds=86400 * 365 * 5, requires_metric=True,
        store_method="store_macro", priority=50,
    ),
    # ---- P3 情绪 ----
    "sentiment": SyncSpec(
        data_type="sentiment", category=ProviderCategory.SENTIMENT, method="get_fear_greed",
        normalize_kind="index", interval_seconds=86400, default_batch_limit=365,
        time_window_seconds=86400 * 365, store_method="store_sentiment", priority=60,
    ),
}


def resolve_sync_spec(data_type: str) -> SyncSpec:
    """解析数据类型对应的同步规格。

    Raises:
        ValueError: 未知数据类型
    """
    spec = SYNC_SPECS.get(str(data_type).lower())
    if spec is None:
        raise ValueError(
            f"未知同步数据类型: {data_type!r}，可选值: {sorted(SYNC_SPECS)}"
        )
    return spec


# ==========================================================================
# 限速器
# ==========================================================================


class AsyncRateLimiter:
    """异步令牌桶限速器（每个 Provider 独立实例）。

    主动限速而非依赖服务端 429 后被动退避（文档 §4.6）。
    """

    def __init__(self, rate: int = 60, per_seconds: float = 60.0) -> None:
        """初始化。

        Args:
            rate: 窗口内允许的最大请求数
            per_seconds: 窗口长度（秒）
        """
        self._rate = max(1, int(rate))
        self._per_seconds = max(0.001, float(per_seconds))
        self._interval = self._per_seconds / self._rate
        self._lock = asyncio.Lock()
        self._next_available = 0.0
        self._acquired = 0

    @property
    def acquired_count(self) -> int:
        """已获取的令牌总数。"""
        return self._acquired

    async def acquire(self, tokens: int = 1) -> float:
        """获取令牌，必要时等待。

        Args:
            tokens: 需要的令牌数

        Returns:
            实际等待的秒数
        """
        waited = 0.0
        async with self._lock:
            loop = asyncio.get_running_loop()
            for _ in range(max(1, tokens)):
                now = loop.time()
                if self._next_available <= now:
                    self._next_available = now + self._interval
                else:
                    delay = self._next_available - now
                    self._next_available += self._interval
                    await asyncio.sleep(delay)
                    waited += delay
            self._acquired += 1
        return waited

    def temporarily_throttle(self, factor: float = 2.0) -> None:
        """收到 429 后临时下调速率（拉长请求间隔）。

        Args:
            factor: 间隔放大倍数
        """
        self._interval *= max(1.0, factor)
        logger.warning(f"限速器临时降速，新请求间隔 {self._interval:.3f}s")


# ==========================================================================
# 运行时任务与结果结构
# ==========================================================================


@dataclass
class SyncProgress:
    """同步任务进度快照（用于 Redis Pub/Sub 上报与状态查询）。"""

    task_id: str
    task_name: str
    data_type: str
    symbol: str
    status: str
    progress_pct: float = 0.0
    records_synced: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    current_position: str | None = None
    target_end: str | None = None
    rate_per_second: float = 0.0
    eta_seconds: float | None = None
    provider: str | None = None
    message: str = ""

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        return json.dumps(self.__dict__, ensure_ascii=False, default=str)


@dataclass
class SyncOutcome:
    """同步任务最终结果。"""

    task_id: str
    task_name: str
    data_type: str
    symbol: str
    status: SyncStatus = SyncStatus.IDLE
    records_synced: int = 0
    records_failed: int = 0
    records_skipped: int = 0
    inserted: int = 0
    updated: int = 0
    rejected_invalid: int = 0
    rejected_write_gate: int = 0
    batches: int = 0
    progress_pct: float = 0.0
    last_synced_time: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float = 0.0
    error: str | None = None
    cancelled: bool = False
    paused: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success(self) -> bool:
        """任务是否成功完成。"""
        return self.status is SyncStatus.COMPLETED

    def as_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "data_type": self.data_type,
            "symbol": self.symbol,
            "status": self.status.value if isinstance(self.status, SyncStatus) else str(self.status),
            "records_synced": self.records_synced,
            "records_failed": self.records_failed,
            "records_skipped": self.records_skipped,
            "inserted": self.inserted,
            "updated": self.updated,
            "rejected_invalid": self.rejected_invalid,
            "rejected_write_gate": self.rejected_write_gate,
            "batches": self.batches,
            "progress_pct": round(self.progress_pct, 2),
            "last_synced_time": self.last_synced_time.isoformat() if self.last_synced_time else None,
            "duration_seconds": round(self.duration_seconds, 2),
            "error": self.error,
            "cancelled": self.cancelled,
            "paused": self.paused,
            "conflict_count": len(self.conflicts),
        }


@dataclass
class _RuntimeTask:
    """进程内运行时任务状态（承载暂停 / 恢复 / 取消信号）。"""

    task_id: str
    task_name: str
    data_type: str
    symbol: str
    provider: str | None
    pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress: SyncProgress | None = None
    started_at: datetime = field(default_factory=utcnow)
    lock_token: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        # 初始为"未暂停"状态
        self.pause_event.set()


# ==========================================================================
# SyncService
# ==========================================================================

#: 数据获取函数签名：(category, method, kwargs) → FetchResult
FetchFunc = Callable[..., Awaitable[FetchResult]]


class SyncService:
    """历史数据同步服务。

    职责：
    - 按 checkpoint 断点续传地拉取历史数据并落库（Raw + Normalized）；
    - 每批数据与 checkpoint 更新在同一事务内原子提交；
    - 尊重 Provider Rate Limit（令牌桶主动限速）；
    - 通过 Redis Pub/Sub 实时上报进度；
    - 支持暂停 / 恢复 / 取消。

    Usage::

        sync = SyncService(provider_service=get_provider_service())
        outcome = await sync.sync_historical(
            data_type="candles", symbol="BTCUSDT",
            start_date=datetime(2020, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2021, 1, 1, tzinfo=timezone.utc),
            interval="1d",
        )
        print(outcome.as_dict())
    """

    def __init__(
        self,
        provider_service: Any = None,
        fetch_func: FetchFunc | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        normalizer: DataNormalizer | None = None,
        raw_store: RawDataStore | None = None,
        normalized_store: NormalizedDataStore | None = None,
        redis_client: Any = None,
        *,
        default_rate_limit: int = 60,
        default_rate_window: int = 60,
        backoff_sequence: tuple[int, ...] = DEFAULT_BACKOFF_SEQUENCE,
        enable_distributed_lock: bool = True,
    ) -> None:
        """初始化同步服务。

        Args:
            provider_service: ``ProviderService`` 实例（提供带 Failover 的数据获取）
            fetch_func: 自定义数据获取函数（测试注入用），优先于 provider_service
            session_factory: 异步会话工厂
            normalizer: 数据标准化器
            raw_store: Raw 数据存储
            normalized_store: 标准化数据存储
            redis_client: Redis 异步客户端（进度上报与分布式锁）
            default_rate_limit: Provider 未配置限速时的默认每窗口请求数
            default_rate_window: 默认限速窗口（秒）
            backoff_sequence: 失败重试的指数退避序列（秒）
            enable_distributed_lock: 是否启用 Redis 分布式锁
        """
        self._provider_service = provider_service
        self._fetch_func = fetch_func
        self._session_factory = session_factory or get_session_factory()
        self._normalizer = normalizer or get_data_normalizer()
        self._raw_store = raw_store or get_raw_data_store()
        self._normalized_store = normalized_store or get_normalized_data_store()
        self._redis = redis_client
        self._default_rate_limit = default_rate_limit
        self._default_rate_window = default_rate_window
        self._backoff_sequence = backoff_sequence
        self._enable_lock = enable_distributed_lock

        self._limiters: dict[str, AsyncRateLimiter] = {}
        self._tasks: dict[str, _RuntimeTask] = {}
        self._task_by_name: dict[str, str] = {}

    # ---- 组件注入 ----

    def set_redis(self, redis_client: Any) -> None:
        """注入 Redis 客户端（延迟初始化场景）。"""
        self._redis = redis_client

    def set_provider_service(self, provider_service: Any) -> None:
        """注入 ProviderService。"""
        self._provider_service = provider_service

    # ---- 公共接口 ----

    async def sync_historical(
        self,
        data_type: str,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        provider: str | None = None,
        *,
        interval: str | None = None,
        metric: str | None = None,
        batch_limit: int | None = None,
        task_name: str | None = None,
        batch_size: int = 1000,
        max_retries: int = 5,
    ) -> SyncOutcome:
        """同步指定时间区间的历史数据（支持断点续传）。

        Args:
            data_type: 数据类型（candles / price / onchain / etf / derivatives / macro / sentiment）
            symbol: 资产符号（如 ``BTCUSDT``）
            start_date: 起始时间（含）
            end_date: 结束时间（含）
            provider: 指定 Provider 名称；None 时按优先级自动选择并 Failover
            interval: K 线周期（仅行情类有效）
            metric: 指标名（链上 / 宏观类必填，如 ``mvrv`` / ``cpi``）
            batch_limit: 单批请求条数上限；None 时使用规格默认值
            task_name: 任务名（checkpoint 唯一键的一部分）；None 时自动生成
            batch_size: 数据库批量写入大小
            max_retries: 连续失败最大重试次数

        Returns:
            SyncOutcome 同步结果

        Raises:
            ValueError: 数据类型未知或必填参数缺失
        """
        spec = resolve_sync_spec(data_type)
        if spec.requires_metric and not metric:
            raise ValueError(f"data_type={data_type} 必须提供 metric / indicator 参数")

        start = _ensure_utc(start_date)
        end = _ensure_utc(end_date)
        if end <= start:
            raise ValueError(f"end_date({end}) 必须晚于 start_date({start})")

        resolved_name = task_name or self._build_task_name(spec, symbol, metric, interval)
        return await self._run_sync(
            spec=spec,
            symbol=symbol,
            start=start,
            end=end,
            provider=provider,
            task_name=resolved_name,
            metric=metric,
            interval=interval or spec.default_interval,
            batch_limit=batch_limit or spec.default_batch_limit,
            batch_size=batch_size,
            max_retries=max_retries,
            incremental=False,
        )

    async def sync_incremental(
        self,
        data_type: str,
        symbol: str,
        *,
        provider: str | None = None,
        interval: str | None = None,
        metric: str | None = None,
        task_name: str | None = None,
        lookback_periods: int = 3,
        batch_limit: int | None = None,
        max_retries: int = 3,
    ) -> SyncOutcome:
        """增量同步：从 checkpoint 位置继续拉取到当前时刻。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            provider: 指定 Provider
            interval: K 线周期
            metric: 指标名（链上 / 宏观）
            task_name: 任务名；None 时按规则自动生成（与历史同步共享 checkpoint）
            lookback_periods: 回溯周期数（重叠拉取以修正未收盘数据，幂等写入不会产生重复）
            batch_limit: 单批请求条数
            max_retries: 最大重试次数

        Returns:
            SyncOutcome
        """
        spec = resolve_sync_spec(data_type)
        if spec.requires_metric and not metric:
            raise ValueError(f"data_type={data_type} 必须提供 metric / indicator 参数")

        resolved_name = task_name or self._build_task_name(spec, symbol, metric, interval)
        end = utcnow()

        # 从 checkpoint 读取起点
        start = await self._read_checkpoint_time(resolved_name, provider, symbol)
        if start is None:
            # 首次增量同步：仅回溯 lookback_periods，历史回填交给 sync_historical
            start = end - timedelta(seconds=spec.interval_seconds * lookback_periods)
        else:
            start = start - timedelta(seconds=spec.interval_seconds * lookback_periods)

        if start >= end:
            logger.info(f"增量同步无新数据区间 task={resolved_name} start={start} end={end}")
            return SyncOutcome(
                task_id=resolved_name, task_name=resolved_name, data_type=data_type,
                symbol=symbol, status=SyncStatus.COMPLETED, progress_pct=100.0,
                last_synced_time=start,
            )

        return await self._run_sync(
            spec=spec,
            symbol=symbol,
            start=start,
            end=end,
            provider=provider,
            task_name=resolved_name,
            metric=metric,
            interval=interval or spec.default_interval,
            batch_limit=batch_limit or spec.default_batch_limit,
            max_retries=max_retries,
            incremental=True,
        )

    async def pause_sync(self, task_id: str) -> bool:
        """暂停同步任务（保留进度，checkpoint 状态置为 PAUSED）。

        Args:
            task_id: 任务 ID 或任务名

        Returns:
            是否成功发出暂停指令
        """
        runtime = self._find_task(task_id)
        if runtime is None:
            logger.warning(f"暂停失败：任务不存在或已结束 task_id={task_id}")
            return await self._pause_by_db(task_id)

        runtime.pause_event.clear()
        await self._update_checkpoint_status(runtime.task_id, SyncStatus.PAUSED)
        logger.info(f"同步任务已暂停 task={runtime.task_name} id={runtime.task_id}")
        return True

    async def resume_sync(self, task_id: str) -> bool:
        """恢复被暂停的同步任务（从暂停点继续，retry_count 清零）。

        Args:
            task_id: 任务 ID 或任务名

        Returns:
            是否成功发出恢复指令
        """
        runtime = self._find_task(task_id)
        if runtime is None:
            logger.warning(f"恢复失败：任务不在运行中，请调用 sync_* 重新启动 task_id={task_id}")
            return False

        runtime.pause_event.set()
        await self._update_checkpoint_status(runtime.task_id, SyncStatus.SYNCING, reset_retry=True)
        logger.info(f"同步任务已恢复 task={runtime.task_name} id={runtime.task_id}")
        return True

    async def cancel_sync(self, task_id: str) -> bool:
        """取消同步任务（保留已写入进度，checkpoint 状态回到 IDLE）。

        Args:
            task_id: 任务 ID 或任务名

        Returns:
            是否成功发出取消指令
        """
        runtime = self._find_task(task_id)
        if runtime is None:
            return await self._pause_by_db(task_id, status=SyncStatus.IDLE)

        runtime.cancel_event.set()
        runtime.pause_event.set()  # 若处于暂停中，需先唤醒循环才能感知取消
        logger.info(f"同步任务取消指令已下发 task={runtime.task_name} id={runtime.task_id}")
        return True

    def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        """查询运行中任务的进度快照。"""
        runtime = self._find_task(task_id)
        if runtime is None or runtime.progress is None:
            return None
        return runtime.progress.__dict__.copy()

    def list_tasks(self) -> list[dict[str, Any]]:
        """列出当前进程内所有运行中/已完成任务的状态。"""
        return [
            t.progress.__dict__.copy()
            for t in self._tasks.values()
            if t.progress is not None
        ]

    async def list_checkpoints(
        self,
        data_category: ProviderCategory | str | None = None,
        status: SyncStatus | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """查询数据库中的同步 checkpoint 列表。

        Args:
            data_category: 按数据类别过滤
            status: 按状态过滤
            limit: 返回条数上限

        Returns:
            checkpoint 字典列表
        """
        async with get_db_session_ctx() as session:
            stmt = select(SyncCheckpoint).order_by(SyncCheckpoint.updated_at.desc()).limit(limit)
            if data_category is not None:
                category = (
                    data_category
                    if isinstance(data_category, ProviderCategory)
                    else ProviderCategory(str(data_category).upper())
                )
                stmt = stmt.where(SyncCheckpoint.data_category == category)
            if status is not None:
                status_enum = status if isinstance(status, SyncStatus) else SyncStatus(str(status).upper())
                stmt = stmt.where(SyncCheckpoint.status == status_enum)
            rows = (await session.execute(stmt)).scalars().all()

        return [
            {
                "id": str(row.id),
                "task_name": row.task_name,
                "data_category": row.data_category.value,
                "provider_id": str(row.provider_id) if row.provider_id else None,
                "symbol": row.symbol,
                "status": row.status.value,
                "progress_pct": float(row.progress_pct or 0),
                "last_synced_time": row.last_synced_time.isoformat() if row.last_synced_time else None,
                "target_end_time": row.target_end_time.isoformat() if row.target_end_time else None,
                "records_synced": row.records_synced,
                "records_failed": row.records_failed,
                "retry_count": row.retry_count,
                "last_error": row.last_error,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    # ---- 核心同步循环 ----

    async def _run_sync(
        self,
        *,
        spec: SyncSpec,
        symbol: str,
        start: datetime,
        end: datetime,
        provider: str | None,
        task_name: str,
        metric: str | None,
        interval: str | None,
        batch_limit: int,
        max_retries: int,
        incremental: bool,
        batch_size: int = 1000,
    ) -> SyncOutcome:
        """执行同步主循环。"""
        outcome = SyncOutcome(
            task_id="", task_name=task_name, data_type=spec.data_type, symbol=symbol,
            started_at=utcnow(),
        )

        # 1. 加载或创建 checkpoint（崩溃恢复入口）
        checkpoint = await self._load_or_create_checkpoint(
            task_name=task_name, spec=spec, symbol=symbol, provider=provider,
            start=start, end=end, batch_size=batch_size, max_retries=max_retries,
        )
        if checkpoint is None:
            outcome.status = SyncStatus.FAILED
            outcome.error = "无法创建或加载同步 checkpoint（数据库不可用）"
            logger.error(outcome.error)
            return outcome

        task_id = str(checkpoint.id)
        outcome.task_id = task_id
        outcome.status = SyncStatus.SYNCING

        # 2. 并发控制：同一 checkpoint 同时只允许一个执行实例
        if task_id in self._tasks:
            message = f"任务已在运行中，跳过重复执行 task={task_name} id={task_id}"
            logger.warning(message)
            outcome.status = SyncStatus.SYNCING
            outcome.error = message
            return outcome
        if not await self._acquire_lock(task_id):
            message = f"另一进程持有同步锁，跳过 task={task_name} id={task_id}"
            logger.warning(message)
            outcome.error = message
            outcome.status = SyncStatus.RETRY
            return outcome

        runtime = _RuntimeTask(
            task_id=task_id, task_name=task_name, data_type=spec.data_type,
            symbol=symbol, provider=provider,
        )
        runtime.progress = SyncProgress(
            task_id=task_id, task_name=task_name, data_type=spec.data_type,
            symbol=symbol, status=SyncStatus.SYNCING.value, provider=provider,
            target_end=end.isoformat(),
        )
        self._tasks[task_id] = runtime
        self._task_by_name[task_name] = task_id

        # 3. 恢复位置：checkpoint 优先于入参 start（文档 §3.2 崩溃恢复）
        cursor = checkpoint.last_synced_time or start
        if cursor < start:
            cursor = start
        cursor = cursor + timedelta(seconds=spec.interval_seconds) if checkpoint.last_synced_time else cursor
        if cursor > end:
            cursor = end

        limiter = await self._get_limiter(provider, spec)
        records_synced = checkpoint.records_synced or 0
        records_failed = checkpoint.records_failed or 0
        retry_count = checkpoint.retry_count or 0

        logger.info(
            f"同步任务启动 task={task_name} id={task_id} type={spec.data_type} "
            f"symbol={symbol} provider={provider or 'auto'} "
            f"区间=[{cursor.isoformat()} → {end.isoformat()}] "
            f"恢复自 checkpoint={checkpoint.last_synced_time} 模式={'增量' if incremental else '历史'}"
        )

        try:
            while cursor <= end:
                # ---- 取消检查 ----
                if runtime.cancel_event.is_set():
                    outcome.cancelled = True
                    outcome.status = SyncStatus.IDLE
                    outcome.error = "任务被取消"
                    await self._update_checkpoint_status(task_id, SyncStatus.IDLE)
                    logger.info(f"同步任务已取消 task={task_name} 进度={outcome.progress_pct:.1f}%")
                    break

                # ---- 暂停等待 ----
                if not runtime.pause_event.is_set():
                    logger.info(f"同步任务暂停中 task={task_name}，等待恢复指令...")
                    await self._publish_progress(runtime, "PAUSED")
                    await runtime.pause_event.wait()
                    if runtime.cancel_event.is_set():
                        continue
                    retry_count = 0
                    logger.info(f"同步任务恢复运行 task={task_name}")

                batch_end = self._next_batch_end(cursor, end, spec, batch_limit, interval)

                # ---- 限速 ----
                await limiter.acquire()

                # ---- 拉取一批数据 ----
                fetch_result = await self._fetch_batch(
                    spec=spec, symbol=symbol, metric=metric, interval=interval,
                    batch_start=cursor, batch_end=batch_end, batch_limit=batch_limit,
                    provider=provider,
                )

                if not fetch_result.success or fetch_result.is_stale:
                    retry_count += 1
                    records_failed += 1
                    if fetch_result.is_stale:
                        # 降级缓存回放的是历史快照（且未经标准化管道），
                        # 对断点续传回填无意义——视为失败，走统一退避等待 Provider 恢复
                        error_text = (
                            "STALE_CACHE: 所有 Provider 暂不可用，"
                            "拒绝使用降级缓存数据进行同步"
                        )
                    else:
                        error_text = f"{fetch_result.error_type or 'unknown'}: {fetch_result.error}"
                    logger.warning(
                        f"批次拉取失败 task={task_name} 区间=[{cursor} → {batch_end}] "
                        f"retry={retry_count}/{max_retries} err={error_text}"
                    )
                    if fetch_result.error_type is not None and fetch_result.error_type.value == "rate_limit":
                        limiter.temporarily_throttle(2.0)

                    if retry_count > max_retries:
                        outcome.status = SyncStatus.FAILED
                        outcome.error = f"连续失败超过 {max_retries} 次，最后错误: {error_text}"
                        await self._mark_failed(task_id, error_text, retry_count)
                        logger.error(f"同步任务熔断 task={task_name}: {outcome.error}")
                        break

                    await self._update_checkpoint(
                        task_id, status=SyncStatus.RETRY, retry_count=retry_count,
                        last_error=error_text, records_failed=records_failed,
                    )
                    delay = self._backoff_delay(retry_count)
                    await self._publish_progress(runtime, "RETRY", f"{delay}s 后重试")
                    await asyncio.sleep(delay)
                    continue

                # ---- 成功：标准化 + 原子落库 + 推进 checkpoint ----
                retry_count = 0
                source_id = await self._resolve_source_id(fetch_result.provider_name, provider)
                normalized = self._normalize_result(spec, fetch_result, source_id, symbol, interval, metric)

                apply_count = apply_source_id(normalized.records, source_id) if source_id else 0
                if normalized.records and source_id is None:
                    logger.warning(
                        f"Provider '{fetch_result.provider_name}' 未注册，"
                        f"{len(normalized.records)} 条记录无法写入（source_id 缺失）"
                    )

                store_result, new_position = await self._commit_batch(
                    task_id=task_id, spec=spec, normalized=normalized,
                    fetch_result=fetch_result, source_id=source_id, symbol=symbol,
                    metric=metric, interval=interval, cursor=cursor, batch_end=batch_end,
                    end=end, records_synced=records_synced, start=start,
                )

                if store_result is not None:
                    records_synced += store_result.accepted
                    outcome.inserted += store_result.inserted
                    outcome.updated += store_result.updated
                    outcome.rejected_invalid += store_result.rejected_invalid
                    outcome.rejected_write_gate += store_result.rejected_write_gate
                    outcome.conflicts.extend(store_result.conflicts)
                    outcome.batches += 1
                    if store_result.rejected_invalid or store_result.conflicts:
                        records_failed += store_result.rejected_invalid + len(store_result.conflicts)

                outcome.records_synced = records_synced
                outcome.records_failed = records_failed
                outcome.progress_pct = _progress_pct(cursor, start, end)
                outcome.last_synced_time = new_position or batch_end

                elapsed = (utcnow() - runtime.started_at).total_seconds()
                rate = records_synced / elapsed if elapsed > 0 else 0.0
                await self._publish_progress(
                    runtime, SyncStatus.SYNCING.value,
                    f"已同步至 {(new_position or batch_end).isoformat()}",
                    rate=rate, position=(new_position or batch_end), start=start, end=end,
                )

                if apply_count:
                    logger.debug(f"已为 {apply_count} 条记录补齐 source_id")

                # ---- 推进游标 ----
                next_cursor = (new_position + timedelta(seconds=spec.interval_seconds)) if new_position else batch_end
                if next_cursor <= cursor:
                    # 防御性推进，避免 Provider 返回空数据导致死循环
                    next_cursor = min(cursor + timedelta(seconds=spec.interval_seconds), batch_end + timedelta(seconds=spec.interval_seconds))
                cursor = next_cursor

                if batch_end >= end and (new_position is None or new_position >= end - timedelta(seconds=spec.interval_seconds)):
                    break
            else:
                pass

            if not outcome.cancelled and outcome.status is not SyncStatus.FAILED:
                outcome.status = SyncStatus.COMPLETED
                outcome.progress_pct = 100.0
                await self._mark_completed(task_id, records_synced, records_failed, cursor)
                logger.info(
                    f"同步任务完成 task={task_name} 记录数={records_synced} "
                    f"批次={outcome.batches} 位置={cursor.isoformat()}"
                )
                await self._publish_progress(runtime, SyncStatus.COMPLETED.value, "同步完成")

        except asyncio.CancelledError:
            outcome.cancelled = True
            outcome.status = SyncStatus.PAUSED
            await self._update_checkpoint_status(task_id, SyncStatus.PAUSED)
            logger.warning(f"同步任务被外部取消（asyncio）task={task_name}")
            raise
        except Exception as exc:  # noqa: BLE001 - 顶层兜底，保证 checkpoint 状态一致
            outcome.status = SyncStatus.FAILED
            outcome.error = str(exc)
            logger.exception(f"同步任务异常终止 task={task_name}: {exc}")
            await self._mark_failed(task_id, str(exc), retry_count)
        finally:
            outcome.completed_at = utcnow()
            outcome.duration_seconds = (
                outcome.completed_at - (outcome.started_at or outcome.completed_at)
            ).total_seconds()
            outcome.records_synced = records_synced
            outcome.records_failed = records_failed
            if runtime.progress is not None:
                runtime.progress.status = (
                    outcome.status.value if isinstance(outcome.status, SyncStatus) else str(outcome.status)
                )
                runtime.progress.records_synced = records_synced
            await self._release_lock(task_id, runtime.lock_token)
            self._tasks.pop(task_id, None)
            self._task_by_name.pop(task_name, None)

        return outcome

    # ---- 批次处理 ----

    @staticmethod
    def _next_batch_end(
        cursor: datetime,
        end: datetime,
        spec: SyncSpec,
        batch_limit: int,
        interval: str | None,
    ) -> datetime:
        """计算本批次的结束时间。

        优先按 ``time_window_seconds`` 切窗（时间驱动），否则按
        ``batch_limit × interval_seconds``（条数驱动），最终不超过 ``end``。
        """
        if spec.time_window_seconds:
            step = timedelta(seconds=spec.time_window_seconds)
        else:
            per_record = _interval_seconds(interval) or spec.interval_seconds
            step = timedelta(seconds=per_record * max(1, batch_limit))
        return min(cursor + step, end)

    async def _fetch_batch(
        self,
        *,
        spec: SyncSpec,
        symbol: str,
        metric: str | None,
        interval: str | None,
        batch_start: datetime,
        batch_end: datetime,
        batch_limit: int,
        provider: str | None,
    ) -> FetchResult:
        """按同步规格调用 Provider 拉取一批数据。"""
        kwargs: dict[str, Any] = {}
        method = spec.method

        if spec.category is ProviderCategory.MARKET:
            if method == "get_ohlcv":
                kwargs = {
                    "symbol": _to_provider_symbol(symbol),
                    "interval": interval or spec.default_interval or "1h",
                    "start": batch_start,
                    "end": batch_end,
                    "limit": batch_limit,
                }
            else:
                kwargs = {"symbol": _to_provider_symbol(symbol)}
        elif spec.category in (ProviderCategory.ONCHAIN, ProviderCategory.EXCHANGE_FLOW):
            kwargs = {"metric": metric, "start": batch_start, "end": batch_end}
        elif spec.category is ProviderCategory.MACRO:
            kwargs = {"indicator": metric, "start": batch_start, "end": batch_end}
        elif spec.category is ProviderCategory.ETF:
            kwargs = {"start": batch_start, "end": batch_end}
        elif spec.category in (ProviderCategory.DERIVATIVES, ProviderCategory.OPTIONS):
            kwargs = {"symbol": _to_provider_symbol(symbol), "limit": batch_limit}
        elif spec.category is ProviderCategory.SENTIMENT:
            kwargs = {"date": batch_end} if method == "get_fear_greed" else {"period": "30d"}
        else:  # pragma: no cover - 兜底
            kwargs = {"symbol": symbol, "start": batch_start, "end": batch_end}

        if provider:
            kwargs["provider_name"] = provider

        return await self._invoke_fetch(spec.category.value.lower(), method, kwargs)

    async def _invoke_fetch(
        self, category: str, method: str, kwargs: dict[str, Any]
    ) -> FetchResult:
        """统一的数据获取入口（自定义 fetch_func > ProviderManager > ProviderService）。"""
        if self._fetch_func is not None:
            return await self._fetch_func(category=category, method=method, **kwargs)

        if self._provider_service is not None:
            manager = getattr(self._provider_service, "manager", None)
            if manager is not None:
                return await manager.execute_with_failover(
                    category=category, method=method, data_type=f"{category}:{method}", **kwargs
                )
            service_result = await self._provider_service.get_data(
                category, method, use_cache=False, **kwargs
            )
            return FetchResult(
                success=service_result.success,
                data=service_result.data,
                error=service_result.error,
                error_type=service_result.error_type,
                response_time_ms=service_result.response_time_ms,
                provider_name=service_result.source,
                fetch_time=service_result.fetch_time,
                observation_time=service_result.observation_time,
                quality_status=service_result.quality_status,
                raw_response=None,
                metadata=service_result.metadata,
                is_failover=service_result.is_failover,
                is_stale=service_result.is_stale,
            )

        return FetchResult(
            success=False, error="SyncService 未注入 ProviderService 或 fetch_func",
            provider_name="",
        )

    def _normalize_result(
        self,
        spec: SyncSpec,
        fetch_result: FetchResult,
        source_id: UUID | None,
        symbol: str,
        interval: str | None,
        metric: str | None,
    ) -> NormalizationResult:
        """将 FetchResult 标准化为记录集合。"""
        payload = fetch_result.raw_response if fetch_result.raw_response is not None else fetch_result.data
        if payload is None:
            logger.debug(f"Provider 返回空 payload，跳过标准化 provider={fetch_result.provider_name}")
            return NormalizationResult(provider_name=fetch_result.provider_name)

        common: dict[str, Any] = {
            "source_id": source_id or NIL_UUID,
            "rule_version": self._normalizer.rule_version,
        }

        if spec.normalize_kind in {"ohlcv", "price"}:
            return self._normalizer.normalize_market_data(
                payload,
                fetch_result.provider_name,
                symbol=symbol,
                interval=interval or spec.default_interval,
                data_kind=spec.normalize_kind,
                **common,
            )
        if spec.normalize_kind == "metric":
            return self._normalizer.normalize_onchain_data(
                payload, fetch_result.provider_name, metric_name=metric, **common
            )
        if spec.normalize_kind == "flow":
            return self._normalizer.normalize_etf_data(
                payload, fetch_result.provider_name, **common
            )
        if spec.normalize_kind == "funding":
            return self._normalizer.normalize_derivatives_data(
                payload, fetch_result.provider_name, symbol=symbol, data_type="FUNDING", **common
            )
        if spec.normalize_kind == "series":
            return self._normalizer.normalize_macro_data(
                payload, fetch_result.provider_name, series_id=metric, **common
            )
        if spec.normalize_kind == "index":
            return self._normalizer.normalize_sentiment_data(
                payload, fetch_result.provider_name, **common
            )
        return self._normalizer.normalize(
            spec.category.value.lower(), payload, fetch_result.provider_name, **common
        )

    async def _commit_batch(
        self,
        *,
        task_id: str,
        spec: SyncSpec,
        normalized: NormalizationResult,
        fetch_result: FetchResult,
        source_id: UUID | None,
        symbol: str,
        metric: str | None,
        interval: str | None,
        cursor: datetime,
        batch_end: datetime,
        end: datetime,
        records_synced: int,
        start: datetime,
    ) -> tuple[StoreResult | None, datetime | None]:
        """在 **同一事务** 内写入 Raw + Normalized + 更新 checkpoint。

        Returns:
            ``(写入结果, 本批最新数据观测时间)``
        """
        records = normalized.records
        new_position = _latest_observation_time(records) or batch_end
        progress = _progress_pct(new_position, start, end)

        async with self._session_factory() as session:
            try:
                # 1. Raw 原始响应落库（append-only，失败不阻断标准化数据写入）
                if fetch_result.raw_response is not None and source_id is not None:
                    try:
                        await self._raw_store.store_raw(
                            category=spec.category,
                            provider_name=fetch_result.provider_name,
                            endpoint=spec.method,
                            params={
                                "symbol": symbol,
                                "metric": metric,
                                "interval": interval,
                                "start": cursor.isoformat(),
                                "end": batch_end.isoformat(),
                            },
                            response_body=fetch_result.raw_response,
                            status_code=fetch_result.status_code,
                            response_time_ms=fetch_result.response_time_ms,
                            observation_time=new_position,
                            session=session,
                            **_raw_identifiers(spec, symbol, metric, interval),
                        )
                    except Exception as exc:  # noqa: BLE001 - Raw 写入失败不影响主数据
                        logger.warning(f"Raw 数据落库失败（已忽略，标准化数据继续写入）: {exc}")

                # 2. Normalized 批量写入（含 Write Gate）
                store_result: StoreResult | None = None
                if records and source_id is not None:
                    store_method = getattr(self._normalized_store, spec.store_method, None)
                    if store_method is None:  # pragma: no cover - 配置错误保护
                        store_method = self._normalized_store.store
                    store_result = await store_method(records, session=session)

                # 3. 同事务更新 checkpoint（原子推进）
                await self._update_checkpoint(
                    task_id,
                    session=session,
                    status=SyncStatus.SYNCING,
                    last_synced_time=new_position,
                    target_end_time=end,
                    records_synced=records_synced + (store_result.accepted if store_result else 0),
                    records_failed=(len(normalized.errors) or 0),
                    retry_count=0,
                    last_error=None,
                    progress_pct=progress,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                logger.exception(f"批次事务提交失败 task_id={task_id}: {exc}")
                raise

        return store_result, new_position

    # ---- Checkpoint 管理 ----

    async def _load_or_create_checkpoint(
        self,
        *,
        task_name: str,
        spec: SyncSpec,
        symbol: str,
        provider: str | None,
        start: datetime,
        end: datetime,
        batch_size: int,
        max_retries: int,
    ) -> SyncCheckpoint | None:
        """加载已有 checkpoint，不存在则创建。"""
        provider_id = await self._resolve_source_id(provider, provider) if provider else None

        async with self._session_factory() as session:
            try:
                stmt = select(SyncCheckpoint).where(
                    SyncCheckpoint.task_name == task_name,
                    SyncCheckpoint.symbol == symbol,
                )
                if provider_id is not None:
                    stmt = stmt.where(SyncCheckpoint.provider_id == provider_id)
                else:
                    stmt = stmt.where(SyncCheckpoint.provider_id.is_(None))
                checkpoint = (await session.execute(stmt.limit(1))).scalar_one_or_none()

                if checkpoint is None:
                    checkpoint = SyncCheckpoint(
                        id=uuid4(),
                        task_name=task_name,
                        data_category=spec.category,
                        provider_id=provider_id,
                        symbol=symbol,
                        last_synced_time=None,
                        target_end_time=end,
                        started_at=utcnow(),
                        status=SyncStatus.IDLE,
                        progress_pct=0,
                        records_synced=0,
                        records_failed=0,
                        records_skipped=0,
                        sync_params={
                            "data_type": spec.data_type,
                            "metric": None,
                            "interval": spec.default_interval,
                            "start": start.isoformat(),
                            "end": end.isoformat(),
                            "provider": provider,
                            "priority": spec.priority,
                        },
                        batch_size=batch_size,
                        retry_count=0,
                        max_retries=max_retries,
                        error_history=[],
                    )
                    session.add(checkpoint)
                    logger.info(f"创建新同步 checkpoint task={task_name} symbol={symbol}")
                else:
                    checkpoint.target_end_time = end
                    checkpoint.started_at = checkpoint.started_at or utcnow()
                    if checkpoint.status in (SyncStatus.COMPLETED, SyncStatus.FAILED):
                        # 已完成/失败的任务重新触发 → 转为 RETRY 从断点继续
                        checkpoint.status = SyncStatus.RETRY
                        checkpoint.retry_count = 0
                    params = dict(checkpoint.sync_params or {})
                    params.update({"start": start.isoformat(), "end": end.isoformat(),
                                   "provider": provider, "data_type": spec.data_type})
                    checkpoint.sync_params = params
                    logger.info(
                        f"复用已有 checkpoint task={task_name} "
                        f"last_synced={checkpoint.last_synced_time} status={checkpoint.status.value}"
                    )

                checkpoint.status = SyncStatus.SYNCING
                await session.commit()
                await session.refresh(checkpoint)
                return checkpoint
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.exception(f"checkpoint 加载/创建失败 task={task_name}: {exc}")
                return None

    async def _read_checkpoint_time(
        self, task_name: str, provider: str | None, symbol: str
    ) -> datetime | None:
        """读取 checkpoint 的最后同步时间点。"""
        provider_id = await self._resolve_source_id(provider, provider) if provider else None
        async with self._session_factory() as session:
            try:
                stmt = select(SyncCheckpoint.last_synced_time).where(
                    SyncCheckpoint.task_name == task_name,
                    SyncCheckpoint.symbol == symbol,
                )
                stmt = (
                    stmt.where(SyncCheckpoint.provider_id == provider_id)
                    if provider_id is not None
                    else stmt.where(SyncCheckpoint.provider_id.is_(None))
                )
                return (await session.execute(stmt.limit(1))).scalar_one_or_none()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"读取 checkpoint 失败 task={task_name}: {exc}")
                return None

    async def _update_checkpoint(
        self,
        task_id: str,
        *,
        session: AsyncSession | None = None,
        **values: Any,
    ) -> None:
        """更新 checkpoint 字段（可复用外部事务会话）。"""
        clean = {k: v for k, v in values.items() if v is not None or k in {"last_error"}}
        if not clean:
            return
        clean["updated_at"] = utcnow()

        if session is not None:
            checkpoint = await session.get(SyncCheckpoint, UUID(task_id))
            if checkpoint is None:
                logger.warning(f"checkpoint 不存在，跳过更新 id={task_id}")
                return
            for key, value in clean.items():
                setattr(checkpoint, key, value)
            await session.flush()
            return

        async with self._session_factory() as new_session:
            try:
                checkpoint = await new_session.get(SyncCheckpoint, UUID(task_id))
                if checkpoint is None:
                    return
                for key, value in clean.items():
                    setattr(checkpoint, key, value)
                await new_session.commit()
            except Exception as exc:  # noqa: BLE001
                await new_session.rollback()
                logger.warning(f"checkpoint 更新失败 id={task_id}: {exc}")

    async def _update_checkpoint_status(
        self, task_id: str, status: SyncStatus, *, reset_retry: bool = False
    ) -> None:
        """仅更新 checkpoint 状态。"""
        values: dict[str, Any] = {"status": status}
        if reset_retry:
            values["retry_count"] = 0
            values["last_error"] = None
        await self._update_checkpoint(task_id, **values)

    async def _mark_completed(
        self, task_id: str, records_synced: int, records_failed: int, position: datetime
    ) -> None:
        """标记任务完成。"""
        await self._update_checkpoint(
            task_id, status=SyncStatus.COMPLETED, completed_at=utcnow(),
            records_synced=records_synced, records_failed=records_failed,
            last_synced_time=position, progress_pct=100,
        )

    async def _mark_failed(self, task_id: str, error: str, retry_count: int) -> None:
        """标记任务失败并追加错误历史。"""
        async with self._session_factory() as session:
            try:
                checkpoint = await session.get(SyncCheckpoint, UUID(task_id))
                if checkpoint is None:
                    return
                history = list(checkpoint.error_history or [])
                history.append({"time": utcnow().isoformat(), "error": error[:500],
                                "retry_count": retry_count})
                checkpoint.error_history = history[-50:]
                checkpoint.status = SyncStatus.FAILED
                checkpoint.last_error = error[:2000]
                checkpoint.last_error_time = utcnow()
                checkpoint.retry_count = retry_count
                checkpoint.updated_at = utcnow()
                await session.commit()
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.warning(f"标记 checkpoint 失败状态出错 id={task_id}: {exc}")

    # ---- Provider / 限速 ----

    async def _resolve_source_id(
        self, provider_name: str | None, fallback: str | None = None
    ) -> UUID | None:
        """解析 Provider 名称为数据库 UUID。"""
        name = provider_name or fallback
        if not name:
            return None
        try:
            async with self._session_factory() as session:
                return await provider_id_resolver.resolve(session, name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"解析 Provider source_id 失败 ({name}): {exc}")
            return None

    async def _get_limiter(self, provider: str | None, spec: SyncSpec) -> AsyncRateLimiter:
        """获取（或创建）Provider 级令牌桶限速器。"""
        key = provider or f"__auto__{spec.category.value}"
        if key in self._limiters:
            return self._limiters[key]

        rate, window = self._default_rate_limit, self._default_rate_window
        if provider:
            try:
                async with self._session_factory() as session:
                    row = (
                        await session.execute(
                            select(Provider.rate_limit, Provider.rate_limit_window).where(
                                Provider.name == provider
                            )
                        )
                    ).one_or_none()
                    if row and row[0]:
                        rate, window = int(row[0]), int(row[1] or 60)
            except Exception as exc:  # noqa: BLE001 - 读取失败退化为默认限速
                logger.debug(f"读取 Provider 限速配置失败，使用默认值 ({provider}): {exc}")

        # 历史回填最多使用 Provider 配额的 30%（文档 §4.5 / 10 §4.5）
        effective_rate = max(1, int(rate * 0.3))
        limiter = AsyncRateLimiter(rate=effective_rate, per_seconds=window)
        self._limiters[key] = limiter
        logger.debug(f"创建限速器 provider={key} rate={effective_rate}/{window}s（配额 30%）")
        return limiter

    def _backoff_delay(self, retry_count: int) -> int:
        """按重试次数返回指数退避延迟（秒）。"""
        if not self._backoff_sequence:
            return 0
        index = min(retry_count - 1, len(self._backoff_sequence) - 1)
        return self._backoff_sequence[max(0, index)]

    # ---- 分布式锁 ----

    async def _acquire_lock(self, task_id: str) -> bool:
        """获取 Redis 分布式锁；无 Redis 时退化为进程内独占。"""
        if not self._enable_lock or self._redis is None:
            return True
        try:
            token = uuid4().hex
            acquired = await self._redis.set(
                f"{LOCK_KEY_PREFIX}{task_id}", token, nx=True, ex=DEFAULT_LOCK_TTL
            )
            if acquired:
                runtime = self._tasks.get(task_id)
                if runtime is not None:
                    runtime.lock_token = token
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - Redis 故障时不阻断同步
            logger.warning(f"获取分布式锁失败，退化为进程内独占 task_id={task_id}: {exc}")
            return True

    async def _release_lock(self, task_id: str, token: str) -> None:
        """释放分布式锁（校验 token 避免误删他人锁）。"""
        if not self._enable_lock or self._redis is None:
            return
        key = f"{LOCK_KEY_PREFIX}{task_id}"
        try:
            current = await self._redis.get(key)
            if current in (token, None):
                await self._redis.delete(key)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"释放分布式锁失败 task_id={task_id}: {exc}")

    # ---- 进度上报 ----

    async def _publish_progress(
        self,
        runtime: _RuntimeTask,
        status: str,
        message: str = "",
        *,
        rate: float = 0.0,
        position: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> None:
        """通过 Redis Pub/Sub 发布进度（Redis 不可用时仅更新内存快照）。"""
        progress = runtime.progress
        if progress is None:
            return

        progress.status = status
        progress.message = message
        if rate:
            progress.rate_per_second = round(rate, 2)
        if position is not None:
            progress.current_position = position.isoformat()
            if start and end and end > start:
                progress.progress_pct = round(_progress_pct(position, start, end), 2)
                remaining = (end - position).total_seconds()
                progress.eta_seconds = round(remaining / rate, 1) if rate > 0 else None
        if runtime.progress is not None:
            progress.records_synced = runtime.progress.records_synced

        if self._redis is None:
            return
        payload = progress.to_json()
        for channel in (f"{PROGRESS_CHANNEL_PREFIX}{runtime.task_id}", PROGRESS_CHANNEL_ALL):
            try:
                await self._redis.publish(channel, payload)
            except Exception as exc:  # noqa: BLE001 - 进度上报失败不影响同步
                logger.debug(f"进度发布失败 channel={channel}: {exc}")
                break

    # ---- 任务注册表 ----

    def _find_task(self, task_id: str) -> _RuntimeTask | None:
        """按 task_id 或 task_name 查找运行时任务。"""
        if task_id in self._tasks:
            return self._tasks[task_id]
        resolved = self._task_by_name.get(task_id)
        if resolved and resolved in self._tasks:
            return self._tasks[resolved]
        return None

    async def _pause_by_db(self, task_id: str, status: SyncStatus = SyncStatus.PAUSED) -> bool:
        """任务不在本进程内时，直接改写数据库 checkpoint 状态（供调度器跨进程控制）。"""
        try:
            async with self._session_factory() as session:
                checkpoint = (
                    await session.execute(
                        select(SyncCheckpoint).where(SyncCheckpoint.task_name == task_id).limit(1)
                    )
                ).scalar_one_or_none()
                if checkpoint is None:
                    try:
                        checkpoint = await session.get(SyncCheckpoint, UUID(task_id))
                    except (ValueError, TypeError, AttributeError):
                        checkpoint = None
                if checkpoint is None:
                    return False
                checkpoint.status = status
                checkpoint.updated_at = utcnow()
                await session.commit()
                logger.info(f"已更新 checkpoint 状态（跨进程）task={task_id} → {status.value}")
                return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"跨进程更新 checkpoint 状态失败 task_id={task_id}: {exc}")
            return False

    @staticmethod
    def _build_task_name(
        spec: SyncSpec, symbol: str, metric: str | None, interval: str | None
    ) -> str:
        """生成稳定的任务名（checkpoint 唯一键的一部分）。"""
        parts = [spec.data_type]
        if interval:
            parts.append(interval)
        if metric:
            parts.append(metric.lower())
        parts.append(symbol.upper())
        return "_".join(parts)[:100]


# ==========================================================================
# 工具函数
# ==========================================================================


def _ensure_utc(value: datetime) -> datetime:
    """补齐 UTC 时区信息。"""
    if isinstance(value, str):  # pragma: no cover - 容错
        from app.services.data_normalizer import parse_timestamp

        parsed = parse_timestamp(value)
        if parsed is None:
            raise ValueError(f"无法解析时间字符串: {value!r}")
        return parsed
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _interval_seconds(interval: str | None) -> int | None:
    """将 K 线周期字符串转为秒数。"""
    if not interval:
        return None
    table = {
        "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
        "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
    }
    return table.get(str(interval).lower())


def _to_provider_symbol(symbol: str) -> str:
    """将 ``BTCUSDT`` 转为 Provider 层常用的 ``BTC/USDT`` 表示。"""
    text = str(symbol).strip().upper()
    if "/" in text or "-" in text:
        return text
    for quote in ("USDT", "USDC", "USD", "BUSD", "BTC", "EUR"):
        if text.endswith(quote) and len(text) > len(quote):
            return f"{text[: -len(quote)]}/{quote}"
    return text


def _progress_pct(position: datetime, start: datetime, end: datetime) -> float:
    """计算时间轴进度百分比。"""
    total = (end - start).total_seconds()
    if total <= 0:
        return 100.0
    done = (position - start).total_seconds()
    return max(0.0, min(100.0, done / total * 100))


def _latest_observation_time(records: list[Any]) -> datetime | None:
    """取记录集合中最大的观测时间。"""
    times = [r.observation_time for r in records if getattr(r, "observation_time", None)]
    return max(times) if times else None


def _raw_identifiers(
    spec: SyncSpec, symbol: str, metric: str | None, interval: str | None
) -> dict[str, Any]:
    """构造 Raw 表所需的业务标识字段。"""
    category = spec.category
    if category is ProviderCategory.MARKET:
        return {"symbol": symbol.upper(), "data_type": "OHLCV" if interval else "PRICE"}
    if category in (ProviderCategory.ONCHAIN, ProviderCategory.EXCHANGE_FLOW):
        return {"metric_name": (metric or "UNKNOWN").upper()}
    if category is ProviderCategory.ETF:
        return {"ticker": symbol.upper(), "data_type": "FLOW"}
    if category in (ProviderCategory.DERIVATIVES, ProviderCategory.OPTIONS):
        return {"symbol": symbol.upper(), "exchange": "AGGREGATE",
                "data_type": spec.normalize_kind.upper()}
    if category is ProviderCategory.MACRO:
        return {"series_id": (metric or "UNKNOWN").upper()}
    return {"source_type": "FEAR_GREED" if spec.data_type == "sentiment" else "NEWS"}


# ---- 全局单例 ----

_sync_service: SyncService | None = None


def get_sync_service() -> SyncService:
    """获取全局 SyncService 单例。"""
    global _sync_service
    if _sync_service is None:
        from app.services.provider_service import get_provider_service

        _sync_service = SyncService(provider_service=get_provider_service())
    return _sync_service


def set_sync_service(service: SyncService | None) -> None:
    """设置/重置全局 SyncService 单例（主要用于测试）。"""
    global _sync_service
    _sync_service = service


__all__ = [
    "DEFAULT_BACKOFF_SEQUENCE",
    "PROGRESS_CHANNEL_ALL",
    "PROGRESS_CHANNEL_PREFIX",
    "SYNC_SPECS",
    "AsyncRateLimiter",
    "SyncOutcome",
    "SyncProgress",
    "SyncService",
    "SyncSpec",
    "get_sync_service",
    "resolve_sync_spec",
    "set_sync_service",
]

