"""OnChainService — 链上数据业务服务。

对外提供链上指标（MVRV/SOPR/NUPL/...）与交易所资金流的统一读取入口：
1. 本地优先：先查 onchain_metrics / exchange_flows 表
2. Redis 缓存热数据
3. ProviderManager 多源自动 Failover（Glassnode / CryptoQuant / Blockchain.com）
4. 统一 ServiceResult 返回

持久化（历史回填/补洞）由 SyncService 负责，本服务专注读取路径。
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.onchain import ExchangeFlow, OnchainMetric
from app.providers.base.types import QualityStatus
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult

# 链上指标缓存 TTL（秒）—— 日频指标变化慢，缓存较久
_METRIC_CACHE_TTL = 3600
_FLOW_CACHE_TTL = 600
# 本地数据可接受的最大陈旧度（秒），超过则回源（日频指标 2 天）
_LOCAL_MAX_AGE = 2 * 86400


class OnChainService(CategoryServiceBase):
    """链上数据业务服务。

    Usage:
        service = OnChainService(provider_manager)
        result = await service.get_mvrv()
        if result.success:
            print(result.data["value"], result.quality_status)
    """

    CATEGORY = "onchain"
    CACHE_PREFIX = "onchain:svc:"
    DEFAULT_CACHE_TTL = _METRIC_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 估值/指标类（单点）----

    async def get_mvrv(self, date: datetime | None = None) -> ServiceResult:
        """MVRV 比率（市值/已实现市值）。"""
        return await self._get_metric("get_mvrv", "MVRV", date)

    async def get_sopr(self, date: datetime | None = None) -> ServiceResult:
        """SOPR 花费产出利润率。"""
        return await self._get_metric("get_sopr", "SOPR", date)

    async def get_nupl(self, date: datetime | None = None) -> ServiceResult:
        """NUPL 未实现净盈亏。"""
        return await self._get_metric("get_nupl", "NUPL", date)

    async def get_puell_multiple(self, date: datetime | None = None) -> ServiceResult:
        """Puell Multiple（矿工收入倍数）。"""
        return await self._get_metric("get_puell_multiple", "PUELL_MULTIPLE", date)

    async def get_reserve_risk(self, date: datetime | None = None) -> ServiceResult:
        """Reserve Risk（储备风险）。"""
        return await self._get_metric("get_reserve_risk", "RESERVE_RISK", date)

    async def get_realized_cap(self, date: datetime | None = None) -> ServiceResult:
        """已实现市值（Realized Cap）。"""
        return await self._get_metric("get_realized_cap", "REALIZED_CAP", date, unit="usd")

    async def get_rhodl(self, date: datetime | None = None) -> ServiceResult:
        """RHODL 比率。"""
        return await self._get_metric("get_rhodl", "RHODL", date)

    async def get_active_addresses(self, date: datetime | None = None) -> ServiceResult:
        """活跃地址数。"""
        return await self._get_metric("get_active_addresses", "ACTIVE_ADDRESSES", date, unit="count")

    async def get_transaction_count(self, date: datetime | None = None) -> ServiceResult:
        """链上交易笔数。"""
        return await self._get_metric("get_transaction_count", "TRANSACTION_COUNT", date, unit="count")

    async def get_hash_rate(self, date: datetime | None = None) -> ServiceResult:
        """全网算力（Blockchain.com 提供）。"""
        return await self._get_metric("get_hash_rate", "HASH_RATE", date, unit="gh/s")

    async def get_difficulty(self, date: datetime | None = None) -> ServiceResult:
        """挖矿难度（Blockchain.com 提供）。"""
        return await self._get_metric("get_difficulty", "DIFFICULTY", date, unit="difficulty")

    async def get_lth_supply(self, date: datetime | None = None) -> ServiceResult:
        """长期持有者供应（Glassnode 提供）。"""
        return await self._get_metric("get_lth_supply", "LTH_SUPPLY", date, unit="btc")

    async def get_sth_supply(self, date: datetime | None = None) -> ServiceResult:
        """短期持有者供应（Glassnode 提供）。"""
        return await self._get_metric("get_sth_supply", "STH_SUPPLY", date, unit="btc")

    # ---- 交易所资金流 ----

    async def get_exchange_reserve(self, date: datetime | None = None) -> ServiceResult:
        """交易所 BTC 储备量。"""
        return await self._get_flow_point("get_exchange_reserve", "reserve", date)

    async def get_exchange_netflow(self, date: datetime | None = None) -> ServiceResult:
        """交易所净流入/流出（正值=净流入交易所，通常看跌）。"""
        return await self._get_flow_point("get_netflow", "netflow", date)

    async def get_exchange_inflow(self, date: datetime | None = None) -> ServiceResult:
        """交易所流入量。"""
        return await self._get_flow_point("get_inflow", "inflow", date)

    async def get_exchange_outflow(self, date: datetime | None = None) -> ServiceResult:
        """交易所流出量。"""
        return await self._get_flow_point("get_outflow", "outflow", date)

    async def get_miner_flow(self, date: datetime | None = None) -> ServiceResult:
        """矿工流出（CryptoQuant 提供，非基类接口）。"""
        return await self._get_flow_point("get_miner_flow", "miner_flow", date)

    async def get_whale_activity(self, date: datetime | None = None) -> ServiceResult:
        """鲸鱼活动（CryptoQuant 提供，非基类接口）。"""
        return await self._get_flow_point("get_whale_activity", "whale_activity", date)

    async def get_exchange_flows(
        self, start: datetime, end: datetime, exchange: str | None = None
    ) -> ServiceResult:
        """交易所资金流历史序列（本地优先，缺失回源补洞）。

        Args:
            start: 起始时间
            end: 结束时间
            exchange: 交易所过滤（None 表示全部/聚合）

        Returns:
            ServiceResult[data=list[dict]]
        """
        async def _load_local() -> Any:
            return await self._load_flow_range(start, end, exchange)

        # 区间历史以本地库为准（Provider 单点接口不适合批量回填）
        local = await _load_local()
        if local:
            return ServiceResult(
                success=True,
                data=local,
                quality_status=QualityStatus.VERIFIED,
                source="local_db",
                fetch_time=datetime.utcnow(),
                metadata={"local_first": True, "count": len(local)},
            )

        # 本地无数据：回源取最新单点作为兜底
        return await self.get_data(
            "get_netflow",
            data_type="onchain:exchange_flows_range",
            cache_ttl=_FLOW_CACHE_TTL,
            date=end,
        )

    async def get_metric_history(
        self, metric: str, start: datetime, end: datetime
    ) -> ServiceResult:
        """链上指标历史序列（本地优先，缺失回源）。"""
        metric_upper = metric.upper()

        async def _load_local() -> Any:
            return await self._load_metric_range(metric_upper, start, end)

        local = await _load_local()
        if local:
            return ServiceResult(
                success=True,
                data=local,
                quality_status=QualityStatus.VERIFIED,
                source="local_db",
                fetch_time=datetime.utcnow(),
                metadata={"local_first": True, "count": len(local)},
            )

        return await self.get_data(
            "get_metric_history",
            data_type=f"onchain:history:{metric_upper}",
            cache_ttl=_FLOW_CACHE_TTL,
            metric=metric,
            start=start,
            end=end,
        )

    # ---- 内部：单指标读取（本地优先 + 缓存 + Failover）----

    async def _get_metric(
        self, method: str, metric_name: str, date: datetime | None, unit: str = "ratio"
    ) -> ServiceResult:
        async def _load_local() -> Any:
            return await self._load_latest_metric(metric_name, date)

        return await self.get_data(
            method,
            data_type=f"onchain:{metric_name.lower()}",
            cache_ttl=_METRIC_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    async def _get_flow_point(
        self, method: str, flow_type: str, date: datetime | None
    ) -> ServiceResult:
        async def _load_local() -> Any:
            return await self._load_latest_flow(flow_type, date)

        return await self.get_data(
            method,
            data_type=f"onchain:flow:{flow_type}",
            cache_ttl=_FLOW_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    # ---- 内部：数据库查询 ----

    async def _load_latest_metric(
        self, metric_name: str, date: datetime | None
    ) -> dict[str, Any] | None:
        """查询本地 onchain_metrics 表最新（<= date）指标值。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(OnchainMetric).where(
                    OnchainMetric.metric_name == metric_name
                )
                if date is not None:
                    stmt = stmt.where(
                        OnchainMetric.observation_time <= self._as_utc(date)
                    )
                stmt = stmt.order_by(OnchainMetric.observation_time.desc()).limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                # 陈旧度检查：过旧数据不作为 local-first 命中
                age = (datetime.now(tz=row.observation_time.tzinfo) - row.observation_time)
                if date is None and age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                return self._metric_row_to_dict(row)
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
            logger.warning(f"onchain_metrics query failed: {e}")
            return None

    async def _load_metric_range(
        self, metric_name: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]] | None:
        """查询本地 onchain_metrics 表区间序列。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(OnchainMetric)
                    .where(
                        OnchainMetric.metric_name == metric_name,
                        OnchainMetric.observation_time >= self._as_utc(start),
                        OnchainMetric.observation_time <= self._as_utc(end),
                    )
                    .order_by(OnchainMetric.observation_time)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [self._metric_row_to_dict(r) for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"onchain_metrics range query failed: {e}")
            return None

    async def _load_latest_flow(
        self, flow_type: str, date: datetime | None
    ) -> dict[str, Any] | None:
        """查询本地 exchange_flows 表最新（<= date）资金流。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(ExchangeFlow).where(
                    ExchangeFlow.flow_type == flow_type.upper()[:20]
                )
                if date is not None:
                    stmt = stmt.where(
                        ExchangeFlow.observation_time <= self._as_utc(date)
                    )
                stmt = stmt.order_by(ExchangeFlow.observation_time.desc()).limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                age = (datetime.now(tz=row.observation_time.tzinfo) - row.observation_time)
                if date is None and age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                return self._flow_row_to_dict(row)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"exchange_flows query failed: {e}")
            return None

    async def _load_flow_range(
        self, start: datetime, end: datetime, exchange: str | None
    ) -> list[dict[str, Any]] | None:
        """查询本地 exchange_flows 表区间序列。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(ExchangeFlow).where(
                    ExchangeFlow.observation_time >= self._as_utc(start),
                    ExchangeFlow.observation_time <= self._as_utc(end),
                )
                if exchange:
                    stmt = stmt.where(ExchangeFlow.exchange_name == exchange)
                stmt = stmt.order_by(ExchangeFlow.observation_time)
                rows = (await session.execute(stmt)).scalars().all()
                return [self._flow_row_to_dict(r) for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"exchange_flows range query failed: {e}")
            return None

    # ---- 内部：ORM -> dict ----

    @staticmethod
    def _metric_row_to_dict(row: OnchainMetric) -> dict[str, Any]:
        return {
            "metric_name": row.metric_name,
            "value": float(row.value) if row.value is not None else None,
            "observation_time": row.observation_time.isoformat(),
            "unit": row.unit,
            "source": "local_db",
            "metadata": row.metadata_ or {},
        }

    @staticmethod
    def _flow_row_to_dict(row: ExchangeFlow) -> dict[str, Any]:
        def _f(v: Decimal | None) -> float | None:
            return float(v) if v is not None else None

        return {
            "flow_type": row.flow_type,
            "exchange": row.exchange_name,
            "observation_time": row.observation_time.isoformat(),
            "inflow_btc": _f(row.inflow_btc),
            "outflow_btc": _f(row.outflow_btc),
            "net_flow_btc": _f(row.net_flow_btc),
            "inflow_usd": _f(row.inflow_usd),
            "outflow_usd": _f(row.outflow_usd),
            "net_flow_usd": _f(row.net_flow_usd),
            "exchange_balance": _f(row.exchange_balance),
            "source": "local_db",
            "metadata": row.metadata_ or {},
        }


__all__ = ["OnChainService"]
