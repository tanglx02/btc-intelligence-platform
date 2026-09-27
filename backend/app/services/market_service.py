"""MarketService — 市场行情业务服务。

职责（业务层入口，不直接依赖具体 Provider）：
1. 通过 ProviderManager.execute_with_failover 获取多源行情数据
2. Redis 业务缓存（价格 5s / 订单簿 10s / 衍生品 60s 等短 TTL）
3. K线数据本地优先：先查 candles 表，缺失区间再从 API 补充并落库
4. 多源交叉验证：并发调用多个 Provider 比对价格偏差，异常标记 CONFLICT
5. 统一返回 ServiceResult（data + source + quality_status + metadata）

设计约束：
- 绝不生成假数据：全部 Provider 失败时返回缓存（STALE）或明确错误（INVALID）
- 所有方法不抛出异常，通过 ServiceResult.success 标识成功/失败
- Provider 写入 candles 表时 source_id 通过 providers 表按名称解析，
  解析失败则跳过落库（不影响数据返回）
"""

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import get_db_session_ctx
from app.models.enums import CandleInterval
from app.models.enums import QualityStatus as DBQualityStatus
from app.models.market import Candle as CandleModel
from app.models.provider import Provider as ProviderModel
from app.providers.base.types import FetchResult, QualityStatus
from app.providers.manager import ProviderManager
from app.providers.market.symbol_mapper import normalize_symbol, to_compact
from app.providers.market.types import Candle
from app.services.provider_service import ServiceResult

# 价格交叉验证的最大允许偏差（%），超过则标记 CONFLICT
PRICE_DEVIATION_THRESHOLD_PCT = 0.5
# 多源验证的单 Provider 超时（秒）
_VALIDATION_TIMEOUT = 8.0


class MarketService:
    """市场行情业务服务。

    Usage:
        service = MarketService(provider_manager)
        result = await service.get_current_price("BTCUSDT")
        if result.success:
            print(result.data["price"], result.quality_status)
    """

    # 各数据类型的缓存 TTL（秒）
    PRICE_CACHE_TTL = 5
    OVERVIEW_CACHE_TTL = 30
    STATS_CACHE_TTL = 30
    ORDERBOOK_CACHE_TTL = 10
    FUNDING_CACHE_TTL = 60
    OPEN_INTEREST_CACHE_TTL = 60
    OHLCV_CACHE_TTL = 300

    CACHE_PREFIX = "market:svc:"

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        """初始化 MarketService。

        Args:
            provider_manager: Provider 管理器（execute_with_failover 入口）
            data_store: 标准化数据存储（可选，预留接口；None 时直接使用
                SQLAlchemy 会话读写 candles 表）
            cache: Redis 异步客户端（可选，None 时禁用业务缓存）
        """
        self._manager = provider_manager
        self._data_store = data_store
        self._cache = cache
        # provider 名称 -> DB providers.id 解析缓存
        self._source_id_cache: dict[str, Any] = {}

    # ---- 对外业务方法 ----

    async def get_current_price(
        self, symbol: str = "BTCUSDT", cross_validate: bool = True
    ) -> ServiceResult:
        """获取当前价格（带 Redis 缓存 + 多源交叉验证）。

        流程：
        1. 检查 Redis 缓存（TTL 5s），命中直接返回
        2. 缓存未命中 -> ProviderManager.execute_with_failover
        3. cross_validate=True 时并发调用其他 Provider 比对价格偏差
        4. 写入缓存并返回 ServiceResult
        """
        started = datetime.utcnow()
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        cache_key = self._cache_key("price", normalized)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return ServiceResult(
                success=True,
                data=cached["data"],
                quality_status=QualityStatus(cached.get("quality_status", "VERIFIED")),
                source=cached.get("source", "cache"),
                is_cached=True,
                fetch_time=started,
                metadata={**cached.get("metadata", {}), "cache_hit": True},
            )

        result = await self._manager.execute_with_failover(
            category="market",
            method="get_current_price",
            data_type=f"market:price:{normalized}",
            symbol=normalized,
        )
        if not result.success:
            return self._from_fetch(result, started)

        price_data = self._price_to_dict(result.data, normalized)
        metadata: dict[str, Any] = {}
        quality = result.quality_status

        # 多源交叉验证（仅在拿到新鲜数据时执行）
        if cross_validate and not result.is_stale:
            validation = await self._cross_validate_price(normalized, price_data["price"])
            metadata["cross_validation"] = validation
            if validation["sources"] >= 2 and validation["max_deviation_pct"] is not None:
                if validation["max_deviation_pct"] > PRICE_DEVIATION_THRESHOLD_PCT:
                    quality = QualityStatus.CONFLICT
                    logger.warning(
                        f"Price deviation across sources exceeds threshold: "
                        f"{validation['max_deviation_pct']:.4f}% ({normalized})"
                    )
                else:
                    quality = QualityStatus.VERIFIED

        service_result = ServiceResult(
            success=True,
            data=price_data,
            quality_status=quality,
            source=result.provider_name,
            is_failover=result.is_failover,
            is_stale=result.is_stale,
            observation_time=result.observation_time,
            fetch_time=started,
            response_time_ms=self._elapsed_ms(started),
            metadata={**result.metadata, **metadata},
        )

        if not result.is_stale:
            await self._cache_set(cache_key, service_result, self.PRICE_CACHE_TTL)
        return service_result

    async def get_24h_stats(self, symbol: str = "BTCUSDT") -> ServiceResult:
        """获取 24 小时统计数据（价格变化、成交量、高低价等）。"""
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        return await self._fetch_with_cache(
            data_type=f"market:stats_24h:{normalized}",
            cache_ns="stats",
            cache_ttl=self.STATS_CACHE_TTL,
            method="get_24h_stats",
            symbol=normalized,
        )

    async def get_ohlcv(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> ServiceResult:
        """获取 K 线数据（本地数据库优先，缺失区间从 API 补充并落库）。

        流程：
        1. 查询本地 candles 表覆盖范围
        2. 本地缺失（全部或尾部）-> ProviderManager 获取缺失区间
        3. 新数据写入数据库（source_id 解析失败时跳过落库）
        4. 合并去重后返回完整序列（升序，截断到 limit）
        """
        started = datetime.utcnow()
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        try:
            candle_interval = CandleInterval(interval.lower())
        except ValueError:
            return self._invalid(
                f"不支持的K线间隔: {interval}（可用: {[i.value for i in CandleInterval]}）"
            )

        compact_symbol = to_compact(normalized)
        end = end or datetime.utcnow()
        start = start or (end - self._interval_delta(interval) * limit)

        # 1. 本地数据库查询
        local_candles, db_available = await self._query_local_candles(
            compact_symbol, candle_interval, start, end
        )
        local_coverage_end = local_candles[-1].timestamp if local_candles else None

        # 2. 判断缺失区间
        need_fetch = True
        fetch_start = start
        if local_coverage_end is not None:
            remaining = (end - local_coverage_end).total_seconds()
            # 本地数据已覆盖到距 end 不足两根K线 -> 无需请求 API
            if remaining <= self._interval_seconds(interval) * 2:
                need_fetch = False
            else:
                fetch_start = local_coverage_end

        fetched: list[Candle] = []
        fetch_result: FetchResult | None = None
        if need_fetch:
            fetch_result = await self._manager.execute_with_failover(
                category="market",
                method="get_ohlcv",
                data_type=f"market:ohlcv:{normalized}:{interval}",
                symbol=normalized,
                interval=interval,
                start=fetch_start,
                end=end,
                limit=limit,
            )
            if fetch_result.success and isinstance(fetch_result.data, list):
                fetched = [c for c in fetch_result.data if isinstance(c, Candle)]

            # 3. 新数据落库（失败不影响返回）
            if fetched and db_available:
                stored = await self._persist_candles(
                    compact_symbol, candle_interval, fetched, fetch_result.provider_name
                )
                logger.info(
                    f"Persisted {stored} candles to DB "
                    f"({compact_symbol} {interval} from {fetch_result.provider_name})"
                )

        # 4. 合并去重（本地 + API），API 数据覆盖同时间戳的本地数据
        merged: dict[datetime, dict] = {}
        for c in local_candles:
            merged[c.timestamp] = self._candle_to_dict(c)
        for c in fetched:
            merged[c.timestamp] = self._candle_to_dict(c)

        if not merged:
            if fetch_result is not None and not fetch_result.success:
                return self._from_fetch(fetch_result, started)
            return ServiceResult(
                success=True,
                data=[],
                quality_status=QualityStatus.ESTIMATED,
                source=fetch_result.provider_name if fetch_result else "",
                fetch_time=started,
                response_time_ms=self._elapsed_ms(started),
                metadata={"count": 0, "db_available": db_available},
            )

        candles = [merged[k] for k in sorted(merged)]
        if len(candles) > limit:
            candles = candles[-limit:]

        last_ts = max(merged)
        quality = (
            fetch_result.quality_status
            if fetch_result is not None and fetch_result.success
            else QualityStatus.VERIFIED
        )
        return ServiceResult(
            success=True,
            data=candles,
            quality_status=quality,
            source=(fetch_result.provider_name if fetch_result else "local_db"),
            is_failover=bool(fetch_result and fetch_result.is_failover),
            is_stale=bool(fetch_result and fetch_result.is_stale),
            observation_time=last_ts,
            fetch_time=started,
            response_time_ms=self._elapsed_ms(started),
            metadata={
                "count": len(candles),
                "symbol": compact_symbol,
                "interval": interval,
                "local_count": len(local_candles),
                "fetched_count": len(fetched),
                "db_available": db_available,
            },
        )

    async def get_market_overview(self, symbol: str = "BTCUSDT") -> ServiceResult:
        """获取市场概览（价格、24h变化、市值、ATH、回撤）。

        并发聚合 4 个子请求，任一失败降级为对应字段 None（部分成功仍返回）。
        """
        started = datetime.utcnow()
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")
        asset = normalized.split("/")[0]

        cache_key = self._cache_key("overview", normalized)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return ServiceResult(
                success=True,
                data=cached["data"],
                quality_status=QualityStatus(cached.get("quality_status", "VERIFIED")),
                source=cached.get("source", "cache"),
                is_cached=True,
                fetch_time=started,
                metadata={**cached.get("metadata", {}), "cache_hit": True},
            )

        price_res, stats_res, cap_res, ath_res = await asyncio.gather(
            self.get_current_price(normalized),
            self._manager.execute_with_failover(
                category="market", method="get_24h_stats",
                data_type=f"market:stats_24h:{normalized}", symbol=normalized,
            ),
            self._manager.execute_with_failover(
                category="market", method="get_market_cap",
                data_type="market:market_cap", symbol=asset,
            ),
            self._manager.execute_with_failover(
                category="market", method="get_ath",
                data_type="market:ath", symbol=asset,
            ),
        )

        if not price_res.success:
            return price_res

        overview: dict[str, Any] = {
            "symbol": normalized,
            "price": price_res.data,
            "stats_24h": self._fetch_data_or_none(stats_res),
            "market_cap": self._fetch_data_or_none(cap_res),
            "ath": self._fetch_data_or_none(ath_res),
        }

        failed_parts = [
            name
            for name, r in (
                ("stats_24h", stats_res), ("market_cap", cap_res), ("ath", ath_res)
            )
            if not r.success
        ]
        if failed_parts:
            logger.warning(f"Market overview partial failure: {failed_parts}")

        service_result = ServiceResult(
            success=True,
            data=overview,
            quality_status=price_res.quality_status,
            source=price_res.source,
            is_failover=price_res.is_failover,
            is_stale=price_res.is_stale,
            observation_time=price_res.observation_time,
            fetch_time=started,
            response_time_ms=self._elapsed_ms(started),
            metadata={
                "failed_parts": failed_parts,
                "cross_validation": price_res.metadata.get("cross_validation"),
            },
        )
        if not service_result.is_stale:
            await self._cache_set(cache_key, service_result, self.OVERVIEW_CACHE_TTL)
        return service_result

    async def get_order_book(self, symbol: str = "BTCUSDT", limit: int = 20) -> ServiceResult:
        """获取订单簿深度（买卖各 limit 档）。"""
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        return await self._fetch_with_cache(
            data_type=f"market:order_book:{normalized}",
            cache_ns="orderbook",
            cache_ttl=self.ORDERBOOK_CACHE_TTL,
            method="get_order_book",
            symbol=normalized,
            depth=max(1, min(limit, 500)),
        )

    async def get_funding_rate(self, symbol: str = "BTCUSDT") -> ServiceResult:
        """获取永续资金费率（由支持衍生品的 market Provider 提供）。"""
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        return await self._fetch_with_cache(
            data_type=f"market:funding_rate:{normalized}",
            cache_ns="funding",
            cache_ttl=self.FUNDING_CACHE_TTL,
            method="get_funding_rate",
            symbol=normalized,
        )

    async def get_open_interest(self, symbol: str = "BTCUSDT") -> ServiceResult:
        """获取未平仓合约量（由支持衍生品的 market Provider 提供）。"""
        try:
            normalized = normalize_symbol(symbol)
        except ValueError as e:
            return self._invalid(f"无效的交易对: {e}")

        return await self._fetch_with_cache(
            data_type=f"market:open_interest:{normalized}",
            cache_ns="open_interest",
            cache_ttl=self.OPEN_INTEREST_CACHE_TTL,
            method="get_open_interest",
            symbol=normalized,
        )

    # ---- 多源交叉验证 ----

    async def _cross_validate_price(
        self, normalized_symbol: str, primary_price: float
    ) -> dict[str, Any]:
        """并发调用其他可用 Provider 获取价格，计算跨源偏差。

        Returns:
            {"sources": 参与比对的源数量, "prices": {provider: price},
             "max_deviation_pct": 最大偏差百分比或 None}
        """
        prices: dict[str, float] = {}
        try:
            queue = self._manager.get_priority_queue("market")
        except Exception:  # noqa: BLE001 - 队列不可用时跳过验证
            return {"sources": 0, "prices": {}, "max_deviation_pct": None}

        entries = [e for e in queue if e.provider.is_available][:4]
        if len(entries) < 2:
            return {"sources": 0, "prices": {}, "max_deviation_pct": None}

        async def fetch_one(entry: Any) -> None:
            try:
                r = await asyncio.wait_for(
                    entry.provider.get_current_price(symbol=normalized_symbol),
                    timeout=_VALIDATION_TIMEOUT,
                )
                if r.success and r.data is not None:
                    # 兼容 dict 与 PriceData dataclass 两种返回形态（与 _price_to_dict 一致）
                    if isinstance(r.data, dict):
                        price_val = r.data.get("price")
                    else:
                        price_val = getattr(r.data, "price", None)
                    if price_val:
                        prices[entry.name] = float(price_val)
            except Exception as e:  # noqa: BLE001 - 验证失败不阻断主流程
                logger.debug(f"Cross-validation skipped for {entry.name}: {e}")

        await asyncio.gather(*(fetch_one(e) for e in entries))

        if len(prices) < 2 or primary_price <= 0:
            return {
                "sources": len(prices),
                "prices": prices,
                "max_deviation_pct": None,
            }

        values = [*prices.values(), primary_price]
        mid = (max(values) + min(values)) / 2.0
        max_dev = (max(values) - min(values)) / mid * 100.0 if mid > 0 else 0.0
        return {
            "sources": len(prices),
            "prices": prices,
            "max_deviation_pct": round(max_dev, 6),
        }

    # ---- 本地数据库读写 ----

    async def _query_local_candles(
        self,
        compact_symbol: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> tuple[list[Candle], bool]:
        """查询本地 candles 表。

        Returns:
            (升序 Candle 列表, 数据库是否可用)
        """
        start_aware = self._as_utc(start)
        end_aware = self._as_utc(end)
        try:
            async with get_db_session_ctx() as session:
                rows = await session.execute(
                    select(CandleModel)
                    .where(
                        CandleModel.symbol == compact_symbol,
                        CandleModel.interval == interval,
                        CandleModel.observation_time >= start_aware,
                        CandleModel.observation_time <= end_aware,
                    )
                    .order_by(CandleModel.observation_time)
                )
                models = rows.scalars().all()
                candles = [
                    Candle(
                        timestamp=m.observation_time.replace(tzinfo=None),
                        open=float(m.open),
                        high=float(m.high),
                        low=float(m.low),
                        close=float(m.close),
                        volume=float(m.volume),
                        quote_volume=float(m.quote_volume) if m.quote_volume is not None else None,
                        trades=m.trades,
                        taker_buy_volume=(
                            float(m.taker_buy_volume)
                            if m.taker_buy_volume is not None else None
                        ),
                        taker_sell_volume=(
                            float(m.taker_sell_volume)
                            if m.taker_sell_volume is not None else None
                        ),
                        vwap=float(m.vwap) if m.vwap is not None else None,
                    )
                    for m in models
                ]
                return candles, True
        except Exception as e:  # noqa: BLE001 - DB 不可用时降级为纯 API 模式
            logger.warning(f"Local candles query failed, fallback to API only: {e}")
            return [], False

    async def _persist_candles(
        self,
        compact_symbol: str,
        interval: CandleInterval,
        candles: list[Candle],
        provider_name: str,
    ) -> int:
        """将 API 获取的 K 线 upsert 到 candles 表。

        Returns:
            实际写入条数（source_id 解析失败或 DB 异常时返回 0）
        """
        if not candles:
            return 0

        source_id = await self._resolve_source_id(provider_name)
        if source_id is None:
            logger.warning(
                f"Cannot persist candles: provider '{provider_name}' "
                f"not found in providers table"
            )
            return 0

        fetch_time = self._as_utc(datetime.utcnow())
        rows = []
        for c in candles:
            rows.append({
                "source_id": source_id,
                "observation_time": self._as_utc(c.timestamp),
                "fetch_time": fetch_time,
                "symbol": compact_symbol,
                "interval": interval,
                "open": Decimal(str(c.open)),
                "high": Decimal(str(c.high)),
                "low": Decimal(str(c.low)),
                "close": Decimal(str(c.close)),
                "volume": Decimal(str(c.volume)),
                "quote_volume": (
                    Decimal(str(c.quote_volume)) if c.quote_volume is not None else None
                ),
                "trades": c.trades,
                "taker_buy_volume": (
                    Decimal(str(c.taker_buy_volume))
                    if c.taker_buy_volume is not None else None
                ),
                "taker_sell_volume": (
                    Decimal(str(c.taker_sell_volume))
                    if c.taker_sell_volume is not None else None
                ),
                "vwap": Decimal(str(c.vwap)) if c.vwap is not None else None,
                "quality_status": DBQualityStatus.VERIFIED,
            })

        try:
            async with get_db_session_ctx() as session:
                stmt = pg_insert(CandleModel).values(rows)
                stmt = stmt.on_conflict_do_update(
                    constraint="idx_candles_unique",
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "volume": stmt.excluded.volume,
                        "quote_volume": stmt.excluded.quote_volume,
                        "trades": stmt.excluded.trades,
                        "taker_buy_volume": stmt.excluded.taker_buy_volume,
                        "taker_sell_volume": stmt.excluded.taker_sell_volume,
                        "vwap": stmt.excluded.vwap,
                        "fetch_time": stmt.excluded.fetch_time,
                        "source_id": stmt.excluded.source_id,
                        "quality_status": stmt.excluded.quality_status,
                        "updated_at": func.now(),
                    },
                )
                await session.execute(stmt)
            return len(rows)
        except Exception as e:  # noqa: BLE001 - 落库失败不影响数据返回
            logger.error(f"Failed to persist candles ({compact_symbol} {interval}): {e}")
            return 0

    async def _resolve_source_id(self, provider_name: str) -> Any:
        """按名称解析 providers 表主键（带内存缓存），失败返回 None。"""
        if not provider_name or provider_name == "cache":
            return None
        if provider_name in self._source_id_cache:
            return self._source_id_cache[provider_name]

        try:
            async with get_db_session_ctx() as session:
                row = await session.execute(
                    select(ProviderModel.id).where(ProviderModel.name == provider_name)
                )
                source_id = row.scalar_one_or_none()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to resolve source_id for '{provider_name}': {e}")
            return None

        if source_id is not None:
            self._source_id_cache[provider_name] = source_id
        return source_id

    # ---- 通用获取 + 缓存 ----

    async def _fetch_with_cache(
        self,
        *,
        data_type: str,
        cache_ns: str,
        cache_ttl: int,
        method: str,
        **kwargs: Any,
    ) -> ServiceResult:
        """execute_with_failover + Redis 缓存的通用封装。"""
        started = datetime.utcnow()
        cache_key = self._cache_key(cache_ns, data_type)

        cached = await self._cache_get(cache_key)
        if cached is not None:
            return ServiceResult(
                success=True,
                data=cached["data"],
                quality_status=QualityStatus(cached.get("quality_status", "VERIFIED")),
                source=cached.get("source", "cache"),
                is_cached=True,
                fetch_time=started,
                metadata={**cached.get("metadata", {}), "cache_hit": True},
            )

        result = await self._manager.execute_with_failover(
            category="market", method=method, data_type=data_type, **kwargs
        )
        service_result = self._from_fetch(result, started)

        if result.success and not result.is_stale:
            await self._cache_set(cache_key, service_result, cache_ttl)
        return service_result

    # ---- Redis 缓存内部方法 ----

    def _cache_key(self, namespace: str, identifier: str) -> str:
        """构建缓存 key。"""
        return f"{self.CACHE_PREFIX}{namespace}:{identifier}"

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
            logger.warning(f"Market cache read error for {key}: {e}")
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
            logger.warning(f"Market cache write error for {key}: {e}")

    # ---- 结果封装工具 ----

    @staticmethod
    def _from_fetch(result: FetchResult, started: datetime) -> ServiceResult:
        """FetchResult -> ServiceResult（带端到端耗时）。"""
        service_result = ServiceResult.from_fetch_result(result)
        service_result.response_time_ms = MarketService._elapsed_ms(started)
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
    def _price_to_dict(data: Any, normalized_symbol: str) -> dict[str, Any]:
        """将 PriceData dataclass（或缓存恢复的 dict）转为统一 dict。"""
        if isinstance(data, dict):
            return {"symbol": normalized_symbol, **data}
        return {
            "symbol": normalized_symbol,
            "price": getattr(data, "price", None),
            "bid": getattr(data, "bid", None),
            "ask": getattr(data, "ask", None),
            "timestamp": str(getattr(data, "timestamp", None)),
            "source": getattr(data, "source", ""),
        }

    @staticmethod
    def _candle_to_dict(c: Candle) -> dict[str, Any]:
        """Candle -> dict（JSON 友好）。"""
        return {
            "timestamp": c.timestamp,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "quote_volume": c.quote_volume,
            "trades": c.trades,
            "is_closed": c.is_closed,
        }

    @staticmethod
    def _fetch_data_or_none(result: FetchResult) -> Any:
        """成功时返回 data（dataclass 转 dict），失败返回 None。"""
        if not result.success or result.data is None:
            return None
        return asdict(result.data) if is_dataclass(result.data) else result.data

    @staticmethod
    def _interval_seconds(interval: str) -> int:
        """间隔字符串 -> 秒数（未知间隔按 1h 处理）。"""
        from app.providers.market._helpers import interval_seconds

        return interval_seconds(interval.lower()) or 3600

    def _interval_delta(self, interval: str) -> timedelta:
        """间隔字符串 -> timedelta。"""
        return timedelta(seconds=self._interval_seconds(interval))

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        """naive datetime 视为 UTC 并附加时区（DB 列为 timestamptz）。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    @staticmethod
    def _elapsed_ms(started: datetime) -> float:
        """计算自 started 起的耗时（毫秒）。"""
        return (datetime.utcnow() - started).total_seconds() * 1000


__all__ = ["MarketService"]
