"""数据补洞检测与修复服务。

对应架构文档：
- 《10-data-quality.md》§3 数据补洞机制
- 《10-data-quality.md》§3.3 补洞优先级与流程
- 《10-data-quality.md》§3.4 补洞失败处理

核心原则：
- **绝不使用插值填充**缺失数据（硬性规则 #1）；
- 补洞只从 Provider 获取真实历史数据，Provider 无数据时标记 UNSUPPORTED；
- 优先级：P0(价格/K线) > P1(链上/衍生品) > P2(ETF/宏观) > P3(情绪)；
- 指数退避重试，最多 5 次后标记 FAILED；
- 检测结果持久化到 ``data_quality`` 表（check_type='GAP_DETECTION'）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Sequence
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db_session_ctx, get_session_factory
from app.models import Candle, MarketPrice, OnchainMetric
from app.models.system import DataQuality
from app.models.enums import CandleInterval, ProviderCategory, SyncStatus
from app.providers.base.types import ErrorType, FetchResult
from app.services.data_store import (
    NormalizedDataStore,
    RawDataStore,
    StoreResult,
    get_normalized_data_store,
    get_raw_data_store,
    provider_id_resolver,
    utcnow,
)
from app.services.data_normalizer import (
    DataNormalizer,
    get_data_normalizer,
    parse_timestamp,
)
from app.services.sync_service import (
    AsyncRateLimiter,
    SyncOutcome,
    SyncSpec,
    SYNC_SPECS,
    resolve_sync_spec,
    DEFAULT_BACKOFF_SEQUENCE,
)


# ==========================================================================
# 补洞优先级
# ==========================================================================


class GapPriority(int, Enum):
    """数据补洞优先级（数字越小越优先）。"""

    P0_PRICE = 0
    P1_ONCHAIN = 1
    P1_DERIVATIVES = 1
    P2_ETF = 2
    P2_MACRO = 2
    P3_SENTIMENT = 3


#: data_type → 优先级映射
PRIORITY_MAP: dict[str, int] = {
    "candles": GapPriority.P0_PRICE,
    "market": GapPriority.P0_PRICE,
    "price": GapPriority.P0_PRICE,
    "onchain": GapPriority.P1_ONCHAIN,
    "derivatives": GapPriority.P1_DERIVATIVES,
    "etf": GapPriority.P2_ETF,
    "macro": GapPriority.P2_MACRO,
    "sentiment": GapPriority.P3_SENTIMENT,
}

#: data_type → 目标 Normalized 表（用于查询已有数据）
_DATA_TYPE_TABLE_MAP: dict[str, type] = {
    "candles": Candle,
    "market": MarketPrice,
    "price": MarketPrice,
    "onchain": OnchainMetric,
    "derivatives": OnchainMetric,  # 暂用 OnchainMetric，实际由 SyncSpec 路由
    "etf": OnchainMetric,
    "macro": OnchainMetric,
    "sentiment": OnchainMetric,
}


# ==========================================================================
# 数据缺口结构
# ==========================================================================


class GapStatus(str, Enum):
    """缺口状态。"""

    PENDING = "PENDING"
    FILLING = "FILLING"
    FILLED = "FILLED"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class DataGap:
    """单个数据缺口描述。

    Attributes:
        gap_id: 唯一标识
        data_type: 数据类型（candles / onchain / etf 等）
        symbol: 资产符号
        gap_start: 缺口起始时间（含）
        gap_end: 缺口结束时间（含）
        expected_interval_seconds: 期望数据间隔（秒）
        expected_records: 预期记录数
        missing_records: 实际缺失记录数
        status: 缺口状态
        priority: 补洞优先级（0 最高）
        attempt_count: 已尝试补洞次数
        max_attempts: 最大尝试次数
        last_attempt_at: 最后尝试时间
        last_error: 最后错误信息
        filled_records: 已成功填充的记录数
        metric: 指标名（链上/宏观类）
        interval: K线周期（行情类）
    """

    data_type: str
    symbol: str
    gap_start: datetime
    gap_end: datetime
    expected_interval_seconds: int
    gap_id: str = field(default_factory=lambda: uuid4().hex[:12])
    expected_records: int = 0
    missing_records: int = 0
    status: GapStatus = GapStatus.PENDING
    priority: int = GapPriority.P3_SENTIMENT
    attempt_count: int = 0
    max_attempts: int = 5
    last_attempt_at: datetime | None = None
    last_error: str | None = None
    filled_records: int = 0
    metric: str | None = None
    interval: str | None = None

    def __post_init__(self) -> None:
        if self.expected_records == 0:
            self.expected_records = self._calc_expected()
        if self.missing_records == 0:
            self.missing_records = self.expected_records
        if self.priority == GapPriority.P3_SENTIMENT:
            self.priority = PRIORITY_MAP.get(self.data_type, 3)

    def _calc_expected(self) -> int:
        """计算时间区间内的期望记录数。"""
        if self.expected_interval_seconds <= 0:
            return 0
        delta = (self.gap_end - self.gap_start).total_seconds()
        return max(1, int(delta / self.expected_interval_seconds) + 1)

    @property
    def duration_hours(self) -> float:
        """缺口时间跨度（小时）。"""
        return (self.gap_end - self.gap_start).total_seconds() / 3600.0

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "gap_id": self.gap_id,
            "data_type": self.data_type,
            "symbol": self.symbol,
            "gap_start": self.gap_start.isoformat(),
            "gap_end": self.gap_end.isoformat(),
            "expected_interval_seconds": self.expected_interval_seconds,
            "expected_records": self.expected_records,
            "missing_records": self.missing_records,
            "status": self.status.value,
            "priority": self.priority,
            "attempt_count": self.attempt_count,
            "last_error": self.last_error,
            "filled_records": self.filled_records,
            "metric": self.metric,
            "interval": self.interval,
            "duration_hours": round(self.duration_hours, 2),
        }


@dataclass
class GapDetectionResult:
    """缺口检测结果汇总。"""

    data_type: str
    symbol: str
    time_range_start: datetime
    time_range_end: datetime
    expected_interval_seconds: int
    total_expected: int = 0
    total_found: int = 0
    gaps: list[DataGap] = field(default_factory=list)
    completeness_pct: float = 100.0
    detection_time_ms: float = 0.0

    @property
    def has_gaps(self) -> bool:
        """是否存在缺口。"""
        return len(self.gaps) > 0

    @property
    def gap_count(self) -> int:
        """缺口数量。"""
        return len(self.gaps)

    @property
    def total_missing(self) -> int:
        """总缺失记录数。"""
        return sum(g.missing_records for g in self.gaps)

    def summary(self) -> dict[str, Any]:
        """汇总信息。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "time_range": f"{self.time_range_start.isoformat()} ~ {self.time_range_end.isoformat()}",
            "expected_interval_seconds": self.expected_interval_seconds,
            "total_expected": self.total_expected,
            "total_found": self.total_found,
            "completeness_pct": round(self.completeness_pct, 2),
            "gap_count": self.gap_count,
            "total_missing": self.total_missing,
            "detection_time_ms": round(self.detection_time_ms, 1),
        }


@dataclass
class GapFillResult:
    """补洞执行结果。"""

    data_type: str
    symbol: str
    total_gaps: int = 0
    filled_gaps: int = 0
    failed_gaps: int = 0
    unsupported_gaps: int = 0
    skipped_gaps: int = 0
    total_records_filled: int = 0
    total_records_failed: int = 0
    duration_seconds: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """补洞成功率。"""
        if self.total_gaps == 0:
            return 1.0
        return self.filled_gaps / self.total_gaps

    def as_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "total_gaps": self.total_gaps,
            "filled_gaps": self.filled_gaps,
            "failed_gaps": self.failed_gaps,
            "unsupported_gaps": self.unsupported_gaps,
            "skipped_gaps": self.skipped_gaps,
            "total_records_filled": self.total_records_filled,
            "total_records_failed": self.total_records_failed,
            "duration_seconds": round(self.duration_seconds, 2),
            "success_rate": round(self.success_rate * 100, 1),
            "details": self.details[:20],  # 只保留前 20 条详情
        }


# ==========================================================================
# GapDetector 主类
# ==========================================================================

#: 退避序列（秒）：1min → 5min → 30min → 2h → 8h（对应文档 §3.4）
_GAP_BACKOFF_SEQUENCE: tuple[int, ...] = (60, 300, 1800, 7200, 28800)


class GapDetector:
    """数据补洞检测与修复服务。

    职责：
    - 根据期望间隔检测时间序列中的缺失数据点；
    - 按优先级调度补洞（P0 价格 > P1 链上 > P2 ETF > P3 情绪）；
    - 利用 Provider 获取真实历史数据填充缺口（**绝不插值**）；
    - 指数退避重试，超限标记 FAILED / UNSUPPORTED；
    - 结果持久化到 ``data_quality`` 表。

    Usage::

        detector = GapDetector()
        result = await detector.detect_gaps(
            data_type="candles", symbol="BTCUSDT",
            start_date=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end_date=datetime(2024, 6, 1, tzinfo=timezone.utc),
            expected_interval=3600,
        )
        if result.has_gaps:
            fill_result = await detector.auto_fill_gaps("candles", "BTCUSDT", result.gaps)
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        provider_service: Any = None,
        normalizer: DataNormalizer | None = None,
        raw_store: RawDataStore | None = None,
        normalized_store: NormalizedDataStore | None = None,
        *,
        max_concurrent_fills: int = 3,
        backoff_sequence: tuple[int, ...] = _GAP_BACKOFF_SEQUENCE,
    ) -> None:
        """初始化 GapDetector。

        Args:
            session_factory: 异步会话工厂
            provider_service: ProviderService 实例
            normalizer: 数据标准化器
            raw_store: Raw 数据存储
            normalized_store: Normalized 数据存储
            max_concurrent_fills: 并发补洞任务上限
            backoff_sequence: 重试退避序列（秒）
        """
        self._session_factory = session_factory or get_session_factory()
        self._provider_service = provider_service
        self._normalizer = normalizer or get_data_normalizer()
        self._raw_store = raw_store or get_raw_data_store()
        self._normalized_store = normalized_store or get_normalized_data_store()
        self._max_concurrent = max_concurrent_fills
        self._backoff_sequence = backoff_sequence
        self._semaphore = asyncio.Semaphore(max_concurrent_fills)
        self._limiters: dict[str, AsyncRateLimiter] = {}

    def set_provider_service(self, provider_service: Any) -> None:
        """注入 ProviderService（延迟初始化）。"""
        self._provider_service = provider_service

    # ==================================================================
    # 公开接口：检测缺口
    # ==================================================================

    async def detect_gaps(
        self,
        data_type: str,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        expected_interval: int,
        *,
        metric: str | None = None,
        interval: str | None = None,
        session: AsyncSession | None = None,
    ) -> GapDetectionResult:
        """检测指定时间区间内的数据缺口。

        根据 ``expected_interval`` 生成期望时间点序列，与数据库已有数据对比，
        识别出连续缺失的时间段。

        Args:
            data_type: 数据类型（candles / price / onchain / etf / derivatives / macro / sentiment）
            symbol: 资产符号
            start_date: 检测起始时间（含）
            end_date: 检测结束时间（含）
            expected_interval: 期望数据间隔（秒）
            metric: 指标名（链上/宏观类需要）
            interval: K线周期字符串（行情类需要）
            session: 外部数据库会话（可选）

        Returns:
            GapDetectionResult 检测结果
        """
        import time
        t0 = time.perf_counter()

        start = _ensure_utc(start_date)
        end = _ensure_utc(end_date)
        if expected_interval <= 0:
            raise ValueError(f"expected_interval 必须为正整数，当前: {expected_interval}")
        if end <= start:
            raise ValueError(f"end_date({end}) 必须晚于 start_date({start})")

        # 查询已有数据的 observation_time 集合
        existing_times = await self._fetch_existing_times(
            data_type=data_type,
            symbol=symbol,
            start=start,
            end=end,
            metric=metric,
            interval=interval,
            session=session,
        )

        # 生成期望时间点并对比
        expected_times = self._generate_expected_times(start, end, expected_interval)
        gaps = self._identify_gaps(
            expected_times=expected_times,
            existing_times=existing_times,
            data_type=data_type,
            symbol=symbol,
            expected_interval=expected_interval,
            metric=metric,
            interval=interval,
        )

        total_expected = len(expected_times)
        total_found = len(existing_times)
        completeness = (total_found / total_expected * 100) if total_expected > 0 else 100.0

        result = GapDetectionResult(
            data_type=data_type,
            symbol=symbol,
            time_range_start=start,
            time_range_end=end,
            expected_interval_seconds=expected_interval,
            total_expected=total_expected,
            total_found=total_found,
            gaps=gaps,
            completeness_pct=min(100.0, completeness),
            detection_time_ms=(time.perf_counter() - t0) * 1000,
        )

        logger.info(
            f"缺口检测完成: {data_type}/{symbol} "
            f"[{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}] "
            f"完整性={result.completeness_pct:.1f}% 缺口数={result.gap_count} "
            f"缺失记录={result.total_missing}"
        )
        return result

    # ==================================================================
    # 公开接口：自动补洞
    # ==================================================================

    async def auto_fill_gaps(
        self,
        data_type: str,
        symbol: str,
        gaps: Sequence[DataGap],
        *,
        provider: str | None = None,
        max_attempts: int = 5,
    ) -> GapFillResult:
        """自动补齐检测到的数据缺口。

        按优先级排序后逐个补洞：
        1. 选择可用 Provider；
        2. 请求缺失时间段的真实数据；
        3. 验证 + 写入（经过 Write Gate）；
        4. 失败时指数退避重试，超限标记 FAILED；
        5. Provider 明确无数据时标记 UNSUPPORTED。

        **绝不使用插值 / 猜测 / 默认值填充。**

        Args:
            data_type: 数据类型
            symbol: 资产符号
            gaps: 待补洞的缺口列表
            provider: 指定 Provider（None 时自动选择）
            max_attempts: 单个缺口最大尝试次数

        Returns:
            GapFillResult 补洞执行结果
        """
        import time
        t0 = time.perf_counter()

        if not gaps:
            return GapFillResult(data_type=data_type, symbol=symbol)

        # 按优先级排序（P0 先补）
        sorted_gaps = sorted(gaps, key=lambda g: (g.priority, g.gap_start))
        fill_result = GapFillResult(
            data_type=data_type,
            symbol=symbol,
            total_gaps=len(sorted_gaps),
        )

        logger.info(
            f"开始补洞: {data_type}/{symbol} 共 {len(sorted_gaps)} 个缺口, "
            f"P0={sum(1 for g in sorted_gaps if g.priority == 0)}, "
            f"P1={sum(1 for g in sorted_gaps if g.priority == 1)}, "
            f"P2={sum(1 for g in sorted_gaps if g.priority == 2)}, "
            f"P3={sum(1 for g in sorted_gaps if g.priority == 3)}"
        )

        for gap in sorted_gaps:
            gap.max_attempts = max_attempts
            async with self._semaphore:
                outcome = await self._fill_single_gap(gap, provider=provider)

            if outcome == "FILLED":
                fill_result.filled_gaps += 1
                fill_result.total_records_filled += gap.filled_records
            elif outcome == "UNSUPPORTED":
                fill_result.unsupported_gaps += 1
            elif outcome == "FAILED":
                fill_result.failed_gaps += 1
                fill_result.total_records_failed += gap.missing_records
            else:
                fill_result.skipped_gaps += 1

            fill_result.details.append(gap.to_dict())

        fill_result.duration_seconds = time.perf_counter() - t0

        logger.info(
            f"补洞完成: {data_type}/{symbol} "
            f"filled={fill_result.filled_gaps} failed={fill_result.failed_gaps} "
            f"unsupported={fill_result.unsupported_gaps} "
            f"records={fill_result.total_records_filled} "
            f"耗时={fill_result.duration_seconds:.1f}s"
        )

        # 持久化检测结果到 data_quality 表
        await self._persist_gap_result(fill_result, sorted_gaps)

        return fill_result

    # ==================================================================
    # 公开接口：检测 + 补洞一体化
    # ==================================================================

    async def detect_and_fill(
        self,
        data_type: str,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        expected_interval: int,
        *,
        metric: str | None = None,
        interval: str | None = None,
        provider: str | None = None,
    ) -> GapFillResult:
        """检测缺口并自动补洞（一体化流程）。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            start_date: 起始时间
            end_date: 结束时间
            expected_interval: 期望间隔（秒）
            metric: 指标名
            interval: K线周期
            provider: 指定 Provider

        Returns:
            GapFillResult 补洞结果
        """
        detection = await self.detect_gaps(
            data_type=data_type,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            expected_interval=expected_interval,
            metric=metric,
            interval=interval,
        )
        if not detection.has_gaps:
            logger.info(f"无缺口需要补洞: {data_type}/{symbol}")
            return GapFillResult(data_type=data_type, symbol=symbol)

        return await self.auto_fill_gaps(
            data_type=data_type,
            symbol=symbol,
            gaps=detection.gaps,
            provider=provider,
        )

    # ==================================================================
    # 内部实现：缺口检测
    # ==================================================================

    async def _fetch_existing_times(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        metric: str | None = None,
        interval: str | None = None,
        session: AsyncSession | None = None,
    ) -> set[datetime]:
        """从数据库查询指定范围内已有的 observation_time 集合。"""
        model = self._resolve_model(data_type)
        if model is None:
            logger.warning(f"无法解析 data_type={data_type} 对应的 ORM 模型，跳过检测")
            return set()

        async def _query(sess: AsyncSession) -> set[datetime]:
            stmt = select(model.observation_time).where(
                model.observation_time >= start,
                model.observation_time <= end,
            )
            # 按表添加过滤条件
            if hasattr(model, "symbol") and symbol:
                stmt = stmt.where(model.symbol == symbol)
            if hasattr(model, "metric_name") and metric:
                stmt = stmt.where(model.metric_name == metric)
            if hasattr(model, "interval") and interval:
                candle_interval = _parse_candle_interval(interval)
                if candle_interval is not None:
                    stmt = stmt.where(model.interval == candle_interval)

            result = await sess.execute(stmt)
            return {row[0] for row in result.all()}

        if session is not None:
            return await _query(session)
        async with get_db_session_ctx() as sess:
            return await _query(sess)

    def _generate_expected_times(
        self, start: datetime, end: datetime, interval_seconds: int
    ) -> list[datetime]:
        """生成 [start, end] 区间内的期望时间点列表。"""
        times: list[datetime] = []
        current = start
        delta = timedelta(seconds=interval_seconds)
        while current <= end:
            times.append(current)
            current += delta
        return times

    def _identify_gaps(
        self,
        expected_times: list[datetime],
        existing_times: set[datetime],
        data_type: str,
        symbol: str,
        expected_interval: int,
        metric: str | None = None,
        interval: str | None = None,
    ) -> list[DataGap]:
        """对比期望时间点与实际数据，识别连续缺失段。

        将连续的缺失时间点合并为单个 DataGap（避免逐条生成碎片缺口）。
        """
        if not expected_times:
            return []

        gaps: list[DataGap] = []
        gap_start: datetime | None = None
        gap_end: datetime | None = None
        missing_count = 0

        # 容差：允许 1 秒的时间偏移匹配
        tolerance = timedelta(seconds=min(1, expected_interval // 10 or 1))

        for expected in expected_times:
            found = self._time_in_set(expected, existing_times, tolerance)
            if not found:
                if gap_start is None:
                    gap_start = expected
                gap_end = expected
                missing_count += 1
            else:
                # 连续缺失段结束
                if gap_start is not None and gap_end is not None:
                    gaps.append(DataGap(
                        data_type=data_type,
                        symbol=symbol,
                        gap_start=gap_start,
                        gap_end=gap_end,
                        expected_interval_seconds=expected_interval,
                        missing_records=missing_count,
                        metric=metric,
                        interval=interval,
                    ))
                gap_start = None
                gap_end = None
                missing_count = 0

        # 尾部缺口
        if gap_start is not None and gap_end is not None:
            gaps.append(DataGap(
                data_type=data_type,
                symbol=symbol,
                gap_start=gap_start,
                gap_end=gap_end,
                expected_interval_seconds=expected_interval,
                missing_records=missing_count,
                metric=metric,
                interval=interval,
            ))

        return gaps

    @staticmethod
    def _time_in_set(
        target: datetime, time_set: set[datetime], tolerance: timedelta
    ) -> bool:
        """检查 target 是否在 time_set 中（带容差匹配）。"""
        if target in time_set:
            return True
        # 容差范围内查找
        for t in time_set:
            if abs((t - target).total_seconds()) <= tolerance.total_seconds():
                return True
        return False

    def _resolve_model(self, data_type: str) -> type | None:
        """根据 data_type 解析对应的 ORM 模型。"""
        spec = SYNC_SPECS.get(data_type.lower())
        if spec is None:
            return None
        # 通过 store_method 反推模型
        store_method = spec.store_method
        method_model_map: dict[str, type] = {
            "store_candles": Candle,
            "store_prices": MarketPrice,
            "store_onchain_metrics": OnchainMetric,
        }
        return method_model_map.get(store_method)

    # ==================================================================
    # 内部实现：补洞执行
    # ==================================================================

    async def _fill_single_gap(
        self, gap: DataGap, *, provider: str | None = None
    ) -> str:
        """尝试填充单个缺口，返回最终状态字符串。"""
        if gap.status in (GapStatus.FILLED, GapStatus.UNSUPPORTED, GapStatus.SKIPPED):
            return gap.status.value

        gap.status = GapStatus.FILLING
        gap.last_attempt_at = utcnow()

        try:
            spec = resolve_sync_spec(gap.data_type)
        except ValueError:
            gap.status = GapStatus.FAILED
            gap.last_error = f"未知 data_type: {gap.data_type}"
            return "FAILED"

        # 构造获取参数
        kwargs = self._build_fetch_kwargs(spec, gap)

        # 重试循环
        for attempt in range(gap.max_attempts):
            gap.attempt_count = attempt + 1
            result = await self._fetch_gap_data(spec, kwargs, provider)

            if result is None:
                # 无可用 Provider
                gap.status = GapStatus.FAILED
                gap.last_error = "无可用 Provider"
                return "FAILED"

            if result.success:
                # 标准化并写入
                records_count = await self._normalize_and_store(result, spec, gap)
                if records_count > 0:
                    gap.status = GapStatus.FILLED
                    gap.filled_records = records_count
                    logger.debug(
                        f"缺口已填充: {gap.data_type}/{gap.symbol} "
                        f"[{gap.gap_start.strftime('%m-%d %H:%M')} ~ "
                        f"{gap.gap_end.strftime('%m-%d %H:%M')}] "
                        f"records={records_count}"
                    )
                    return "FILLED"
                else:
                    # Provider 返回了数据但标准化后为空（可能格式不匹配）
                    gap.status = GapStatus.UNSUPPORTED
                    gap.last_error = "Provider 返回数据但标准化后无有效记录"
                    return "UNSUPPORTED"

            # 失败处理
            error_type = result.error_type
            if error_type == ErrorType.EMPTY_RESPONSE:
                # Provider 明确无此时间段数据 → UNSUPPORTED
                gap.status = GapStatus.UNSUPPORTED
                gap.last_error = f"Provider 无数据: {result.error}"
                logger.info(
                    f"标记 UNSUPPORTED: {gap.data_type}/{gap.symbol} "
                    f"[{gap.gap_start.strftime('%Y-%m-%d')} ~ {gap.gap_end.strftime('%Y-%m-%d')}] "
                    f"provider={result.provider_name}"
                )
                return "UNSUPPORTED"

            gap.last_error = result.error or f"error_type={error_type}"

            if error_type == ErrorType.RATE_LIMIT:
                # 触发限速，等待更长时间
                delay = self._get_backoff_delay(attempt) * 2
                logger.warning(
                    f"补洞触发限速，等待 {delay}s: {gap.data_type}/{gap.symbol}"
                )
                await asyncio.sleep(delay)
            elif attempt < gap.max_attempts - 1:
                delay = self._get_backoff_delay(attempt)
                logger.debug(
                    f"补洞失败 (attempt {attempt + 1}/{gap.max_attempts}), "
                    f"退避 {delay}s: {gap.last_error}"
                )
                await asyncio.sleep(delay)

        # 所有重试用尽
        gap.status = GapStatus.FAILED
        logger.warning(
            f"补洞最终失败: {gap.data_type}/{gap.symbol} "
            f"[{gap.gap_start.strftime('%Y-%m-%d')} ~ {gap.gap_end.strftime('%Y-%m-%d')}] "
            f"attempts={gap.attempt_count} error={gap.last_error}"
        )
        return "FAILED"

    def _build_fetch_kwargs(self, spec: SyncSpec, gap: DataGap) -> dict[str, Any]:
        """根据 SyncSpec 和 DataGap 构造 Provider 调用参数。"""
        kwargs: dict[str, Any] = {}
        category = spec.category

        if category == ProviderCategory.MARKET:
            kwargs["symbol"] = _to_provider_symbol(gap.symbol)
            if gap.interval:
                kwargs["interval"] = gap.interval
            elif spec.default_interval:
                kwargs["interval"] = spec.default_interval
            kwargs["start"] = gap.gap_start
            kwargs["end"] = gap.gap_end
            kwargs["limit"] = min(spec.default_batch_limit, gap.missing_records + 10)

        elif category == ProviderCategory.ONCHAIN:
            if gap.metric:
                kwargs["metric"] = gap.metric
            kwargs["start"] = gap.gap_start
            kwargs["end"] = gap.gap_end

        elif category == ProviderCategory.ETF:
            kwargs["start"] = gap.gap_start
            kwargs["end"] = gap.gap_end

        elif category == ProviderCategory.DERIVATIVES:
            kwargs["symbol"] = _to_provider_symbol(gap.symbol)
            kwargs["limit"] = min(spec.default_batch_limit, gap.missing_records + 10)

        elif category == ProviderCategory.MACRO:
            if gap.metric:
                kwargs["indicator"] = gap.metric
            kwargs["start"] = gap.gap_start
            kwargs["end"] = gap.gap_end

        elif category == ProviderCategory.SENTIMENT:
            kwargs["date"] = gap.gap_start

        return kwargs

    async def _fetch_gap_data(
        self, spec: SyncSpec, kwargs: dict[str, Any], provider: str | None
    ) -> FetchResult | None:
        """通过 ProviderService 获取缺口数据。"""
        if self._provider_service is None:
            logger.error("ProviderService 未注入，无法补洞")
            return None

        try:
            # 优先使用 manager.execute_with_failover（返回完整 FetchResult）
            manager = getattr(self._provider_service, "manager", None)
            if manager is not None:
                # 限速等待
                limiter = self._get_limiter(provider or "default")
                await limiter.acquire()

                result: FetchResult = await manager.execute_with_failover(
                    category=spec.category,
                    method=spec.method,
                    data_type=spec.data_type,
                    **kwargs,
                )
                return result

            # 退化到 get_data
            service_result = await self._provider_service.get_data(
                category=spec.category,
                method=spec.method,
                data_type=spec.data_type,
                use_cache=False,
                **kwargs,
            )
            # 将 ServiceResult 转为 FetchResult
            return FetchResult(
                success=service_result.success,
                data=service_result.data,
                error=service_result.error,
                error_type=ErrorType.UNKNOWN if not service_result.success else None,
                provider_name=service_result.provider_name or "",
                fetch_time=utcnow(),
            )
        except Exception as exc:
            logger.error(f"补洞数据获取异常: {exc}")
            return FetchResult(
                success=False,
                data=None,
                error=str(exc),
                error_type=ErrorType.UNKNOWN,
                provider_name=provider or "",
                fetch_time=utcnow(),
            )

    async def _normalize_and_store(
        self, result: FetchResult, spec: SyncSpec, gap: DataGap
    ) -> int:
        """标准化获取的数据并写入数据库。"""
        if result.data is None:
            return 0

        # 标准化
        normalizer = self._normalizer
        norm_result = None

        if spec.normalize_kind == "ohlcv":
            norm_result = normalizer.normalize_ohlcv(
                raw_response=result.data,
                provider_name=result.provider_name,
                symbol=gap.symbol,
                interval=gap.interval or spec.default_interval,
            )
        elif spec.normalize_kind == "price":
            norm_result = normalizer.normalize_market_data(
                raw_response=result.data,
                provider_name=result.provider_name,
                symbol=gap.symbol,
                data_kind="price",
            )
        elif spec.normalize_kind == "metric":
            norm_result = normalizer.normalize_onchain_data(
                raw_response=result.data,
                provider_name=result.provider_name,
                metric_name=gap.metric,
            )
        elif spec.normalize_kind == "flow":
            norm_result = normalizer.normalize_etf_data(
                raw_response=result.data,
                provider_name=result.provider_name,
            )
        elif spec.normalize_kind == "funding":
            norm_result = normalizer.normalize_derivatives_data(
                raw_response=result.data,
                provider_name=result.provider_name,
                symbol=gap.symbol,
            )
        elif spec.normalize_kind == "series":
            norm_result = normalizer.normalize_macro_data(
                raw_response=result.data,
                provider_name=result.provider_name,
            )
        elif spec.normalize_kind == "index":
            norm_result = normalizer.normalize_sentiment_data(
                raw_response=result.data,
                provider_name=result.provider_name,
            )

        if norm_result is None or norm_result.is_empty():
            return 0

        # 解析 source_id
        source_id = await provider_id_resolver.resolve(
            session=None, provider_name=result.provider_name
        )
        if source_id is None:
            # 尝试通过 session 解析
            async with get_db_session_ctx() as sess:
                source_id = await provider_id_resolver.resolve(
                    session=sess, provider_name=result.provider_name
                )
        if source_id is None:
            logger.warning(f"无法解析 Provider ID: {result.provider_name}，跳过写入")
            return 0

        # 应用 source_id
        from app.services.data_normalizer import apply_source_id
        records = norm_result.records
        apply_source_id(records, source_id)

        # 写入 Raw 表
        if result.raw_response is not None:
            try:
                identifiers = self._raw_identifiers(spec, gap)
                await self._raw_store.store_raw(
                    category=spec.category,
                    provider_name=result.provider_name,
                    endpoint=f"gap_fill:{spec.method}",
                    params={"gap_start": gap.gap_start.isoformat(), "gap_end": gap.gap_end.isoformat()},
                    response_body=result.raw_response,
                    status_code=result.status_code,
                    response_time_ms=result.response_time_ms,
                    **identifiers,
                )
            except Exception as exc:
                logger.warning(f"补洞 Raw 写入失败（不阻断）: {exc}")

        # 写入 Normalized 表
        try:
            store_method = getattr(self._normalized_store, spec.store_method, None)
            if store_method is None:
                logger.error(f"NormalizedDataStore 无方法: {spec.store_method}")
                return 0
            store_result: StoreResult = await store_method(records)
            return store_result.accepted
        except Exception as exc:
            logger.error(f"补洞 Normalized 写入失败: {exc}")
            return 0

    def _raw_identifiers(self, spec: SyncSpec, gap: DataGap) -> dict[str, Any]:
        """构造 Raw 表的业务标识字段。"""
        identifiers: dict[str, Any] = {}
        category = spec.category

        if category == ProviderCategory.MARKET:
            identifiers["symbol"] = gap.symbol
            identifiers["data_type"] = spec.data_type
        elif category == ProviderCategory.ONCHAIN:
            identifiers["metric_name"] = gap.metric or "unknown"
        elif category == ProviderCategory.ETF:
            identifiers["ticker"] = gap.symbol
            identifiers["data_type"] = "flow"
        elif category == ProviderCategory.DERIVATIVES:
            identifiers["symbol"] = gap.symbol
            identifiers["exchange"] = "multi"
            identifiers["data_type"] = spec.data_type
        elif category == ProviderCategory.MACRO:
            identifiers["series_id"] = gap.metric or "unknown"
        elif category == ProviderCategory.SENTIMENT:
            identifiers["source_type"] = "fear_greed"

        return identifiers

    def _get_backoff_delay(self, attempt: int) -> int:
        """计算指数退避延迟（秒）。"""
        idx = min(attempt, len(self._backoff_sequence) - 1)
        return self._backoff_sequence[idx]

    def _get_limiter(self, provider_name: str) -> AsyncRateLimiter:
        """获取或创建 Provider 对应的限速器。"""
        if provider_name not in self._limiters:
            self._limiters[provider_name] = AsyncRateLimiter(rate=30, per_seconds=60.0)
        return self._limiters[provider_name]

    # ==================================================================
    # 持久化到 data_quality 表
    # ==================================================================

    async def _persist_gap_result(
        self, fill_result: GapFillResult, gaps: Sequence[DataGap]
    ) -> None:
        """将补洞结果写入 data_quality 表。"""
        try:
            async with get_db_session_ctx() as session:
                now = utcnow()

                # 确定 data_category
                category = ProviderCategory.MARKET
                if gaps:
                    spec = SYNC_SPECS.get(gaps[0].data_type)
                    if spec:
                        category = spec.category

                # 确定状态
                if fill_result.failed_gaps > 0:
                    status = "ERROR"
                    severity = "HIGH"
                elif fill_result.unsupported_gaps > 0:
                    status = "WARNING"
                    severity = "MEDIUM"
                elif fill_result.filled_gaps > 0:
                    status = "OK"
                    severity = "INFO"
                else:
                    status = "OK"
                    severity = "INFO"

                quality_record = DataQuality(
                    id=uuid4(),
                    check_time=now,
                    data_category=category,
                    table_name=self._get_table_name(fill_result.data_type),
                    check_type="GAP_DETECTION",
                    status=status,
                    severity=severity,
                    records_checked=fill_result.total_gaps,
                    records_passed=fill_result.filled_gaps,
                    records_failed=fill_result.failed_gaps + fill_result.unsupported_gaps,
                    completeness_pct=(
                        round(fill_result.success_rate * 100, 2)
                        if fill_result.total_gaps > 0 else 100.0
                    ),
                    gaps_found=[g.to_dict() for g in gaps[:50]],  # 最多存 50 条详情
                    issues=[
                        {"type": "gap_fill_summary", **fill_result.as_dict()}
                    ],
                    auto_fixed=fill_result.filled_gaps > 0,
                    fix_actions=[
                        {
                            "action": "auto_fill",
                            "filled": fill_result.filled_gaps,
                            "records": fill_result.total_records_filled,
                        }
                    ] if fill_result.filled_gaps > 0 else [],
                    requires_manual=fill_result.failed_gaps > 0,
                    time_range_start=gaps[0].gap_start if gaps else None,
                    time_range_end=gaps[-1].gap_end if gaps else None,
                )

                session.add(quality_record)
                await session.commit()
                logger.debug(f"补洞结果已持久化到 data_quality: id={quality_record.id}")

        except Exception as exc:
            logger.error(f"持久化补洞结果失败: {exc}")

    @staticmethod
    def _get_table_name(data_type: str) -> str:
        """根据 data_type 获取目标表名。"""
        table_map: dict[str, str] = {
            "candles": "candles",
            "market": "market_prices",
            "price": "market_prices",
            "onchain": "onchain_metrics",
            "derivatives": "derivatives",
            "etf": "etf_flows",
            "macro": "macro_series",
            "sentiment": "sentiment",
        }
        return table_map.get(data_type.lower(), "unknown")


# ==========================================================================
# 辅助函数
# ==========================================================================


def _ensure_utc(value: datetime | None) -> datetime:
    """将 datetime 归一化为 UTC。"""
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_provider_symbol(symbol: str) -> str:
    """将内部 symbol（BTCUSDT）转为 Provider 格式（BTC/USDT）。"""
    symbol = symbol.upper().strip()
    if "/" in symbol:
        return symbol
    # 常见 quote 货币
    for quote in ("USDT", "USDC", "USD", "BUSD", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return f"{symbol[:-len(quote)]}/{quote}"
    return symbol


def _parse_candle_interval(interval: str | None) -> CandleInterval | None:
    """将字符串周期解析为 CandleInterval 枚举。"""
    if not interval:
        return None
    aliases: dict[str, CandleInterval] = {
        "1m": CandleInterval.M1, "5m": CandleInterval.M5,
        "15m": CandleInterval.M15, "30m": CandleInterval.M30,
        "1h": CandleInterval.H1, "4h": CandleInterval.H4,
        "1d": CandleInterval.D1, "1w": CandleInterval.W1,
    }
    return aliases.get(interval.lower())


# ==========================================================================
# 全局单例
# ==========================================================================

_gap_detector: GapDetector | None = None


def get_gap_detector() -> GapDetector:
    """获取全局 GapDetector 单例。"""
    global _gap_detector
    if _gap_detector is None:
        _gap_detector = GapDetector()
    return _gap_detector


def set_gap_detector(detector: GapDetector) -> None:
    """设置全局 GapDetector 单例。"""
    global _gap_detector
    _gap_detector = detector


__all__ = [
    "DataGap",
    "GapDetectionResult",
    "GapDetector",
    "GapFillResult",
    "GapPriority",
    "GapStatus",
    "PRIORITY_MAP",
    "get_gap_detector",
    "set_gap_detector",
]
