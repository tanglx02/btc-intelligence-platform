"""ETFService — 现货 ETF 资金流业务服务。

对外提供 BTC 现货 ETF 资金流/持仓的统一读取入口：
1. 本地优先：先查 etf_flows / etf_holdings 表
2. Redis 缓存热数据
3. ProviderManager 多源自动 Failover（Farside / SoSoValue）
4. 统一 ServiceResult 返回

ETF 数据按美股交易日更新（含周末停更），缓存 TTL 相对较长。
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from app.core.database import get_db_session_ctx
from app.models.etf import EtfFlow, EtfHolding
from app.providers.base.types import QualityStatus
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult
from app.utils.datetime_utils import utcnow

_FLOW_CACHE_TTL = 1800  # ETF 日频，30 分钟足够
_HOLDINGS_CACHE_TTL = 1800
# 本地数据可接受的最大陈旧度（3 天，覆盖周末 + 假期）
_LOCAL_MAX_AGE = 3 * 86400


class ETFService(CategoryServiceBase):
    """现货 ETF 资金流业务服务。

    Usage:
        service = ETFService(provider_manager)
        result = await service.get_daily_flow()
        if result.success:
            print(result.data)
    """

    CATEGORY = "etf"
    CACHE_PREFIX = "etf:svc:"
    DEFAULT_CACHE_TTL = _FLOW_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 对外业务方法 ----

    async def get_daily_flow(self, date: datetime | None = None) -> ServiceResult:
        """全市场现货 ETF 单日净流入（USD）。"""

        async def _load_local() -> Any:
            return await self._load_total_flow(date)

        return await self.get_data(
            "get_daily_flow",
            data_type="etf:daily_flow",
            cache_ttl=_FLOW_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    async def get_net_flow(
        self, period: str = "7d", end_date: datetime | None = None
    ) -> ServiceResult:
        """指定周期（7d/30d）ETF 累计净流入。"""
        return await self.get_data(
            "get_net_flow",
            data_type="etf:net_flow",
            cache_ttl=_FLOW_CACHE_TTL,
            period=period,
            end_date=end_date,
        )

    async def get_cumulative_flow(
        self, start: datetime, end: datetime
    ) -> ServiceResult:
        """区间累计净流入（含每日明细）。"""
        return await self.get_data(
            "get_cumulative_flow",
            data_type="etf:cumulative_flow",
            cache_ttl=_FLOW_CACHE_TTL,
            start=start,
            end=end,
        )

    async def get_holdings(self, date: datetime | None = None) -> ServiceResult:
        """各 ETF 持仓量（BTC / USD / AUM）。"""

        async def _load_local() -> Any:
            return await self._load_holdings(date)

        return await self.get_data(
            "get_holdings",
            data_type="etf:holdings",
            cache_ttl=_HOLDINGS_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    async def get_all_funds_flow(self, date: datetime | None = None) -> ServiceResult:
        """全部 ETF 基金逐只资金流明细。"""
        return await self.get_data(
            "get_all_funds_flow",
            data_type="etf:all_funds_flow",
            cache_ttl=_FLOW_CACHE_TTL,
            date=date,
        )

    async def get_historical_flows(
        self, start: datetime, end: datetime, ticker: str | None = None
    ) -> ServiceResult:
        """历史每日资金流序列（本地优先）。

        Args:
            start: 起始日期
            end: 结束日期
            ticker: ETF 代码过滤（None 表示全市场聚合 TOTAL）
        """
        local = await self._load_flow_range(start, end, ticker)
        if local:
            return ServiceResult(
                success=True,
                data=local,
                quality_status=QualityStatus.VERIFIED,
                source="local_db",
                fetch_time=utcnow(),
                metadata={"local_first": True, "count": len(local)},
            )
        # 本地缺失：回源取区间累计
        return await self.get_cumulative_flow(start, end)

    # ---- 内部：数据库查询 ----

    async def _load_total_flow(self, date: datetime | None) -> dict[str, Any] | None:
        """查询本地全市场（ticker=TOTAL）单日净流入。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(EtfFlow).where(EtfFlow.ticker == "TOTAL")
                if date is not None:
                    stmt = stmt.where(EtfFlow.observation_time <= self._as_utc(date))
                stmt = stmt.order_by(EtfFlow.observation_time.desc()).limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                obs = row.observation_time
                if obs.tzinfo is None:
                    # DB 返回 naive（TIMESTAMP 列/驱动差异）时按 UTC 补时区
                    obs = obs.replace(tzinfo=UTC)
                age = datetime.now(tz=obs.tzinfo) - obs
                if date is None and age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                return self._flow_row_to_dict(row)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"etf_flows total query failed: {e}")
            return None

    async def _load_holdings(self, date: datetime | None) -> list[dict[str, Any]] | None:
        """查询本地 ETF 持仓快照（最近一个交易日全部标的）。"""
        try:
            async with get_db_session_ctx() as session:
                # 先定位最近观测时间
                ts_stmt = select(func.max(EtfHolding.observation_time))
                if date is not None:
                    ts_stmt = ts_stmt.where(EtfHolding.observation_time <= self._as_utc(date))
                latest = (await session.execute(ts_stmt)).scalar_one_or_none()
                if latest is None:
                    return None
                if date is None and (
                    datetime.now(tz=latest.tzinfo or UTC) - (latest if latest.tzinfo else latest.replace(tzinfo=UTC))
                ) > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                stmt = select(EtfHolding).where(EtfHolding.observation_time == latest)
                rows = (await session.execute(stmt)).scalars().all()
                return [self._holding_row_to_dict(r) for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"etf_holdings query failed: {e}")
            return None

    async def _load_flow_range(
        self, start: datetime, end: datetime, ticker: str | None
    ) -> list[dict[str, Any]] | None:
        """查询本地 ETF 资金流区间序列。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(EtfFlow).where(
                    EtfFlow.observation_time >= self._as_utc(start),
                    EtfFlow.observation_time <= self._as_utc(end),
                )
                if ticker:
                    stmt = stmt.where(EtfFlow.ticker == ticker.upper())
                stmt = stmt.order_by(EtfFlow.observation_time)
                rows = (await session.execute(stmt)).scalars().all()
                return [self._flow_row_to_dict(r) for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"etf_flows range query failed: {e}")
            return None

    # ---- 内部：ORM -> dict ----

    @staticmethod
    def _dec(v: Decimal | None) -> float | None:
        return float(v) if v is not None else None

    @classmethod
    def _flow_row_to_dict(cls, row: EtfFlow) -> dict[str, Any]:
        return {
            "ticker": row.ticker,
            "fund_name": row.fund_name,
            "date": row.observation_time.isoformat(),
            "daily_inflow_usd": cls._dec(row.daily_inflow_usd),
            "daily_outflow_usd": cls._dec(row.daily_outflow_usd),
            "net_flow_usd": cls._dec(row.net_flow_usd),
            "cumulative_flow_usd": cls._dec(row.cumulative_flow_usd),
            "total_holdings_btc": cls._dec(row.total_holdings_btc),
            "total_aum_usd": cls._dec(row.total_aum_usd),
            "source": "local_db",
        }

    @classmethod
    def _holding_row_to_dict(cls, row: EtfHolding) -> dict[str, Any]:
        return {
            "ticker": row.ticker,
            "fund_name": row.fund_name,
            "date": row.observation_time.isoformat(),
            "holdings_btc": cls._dec(row.holdings_btc),
            "holdings_usd": cls._dec(row.holdings_usd),
            "nav": cls._dec(row.nav_per_share),
            "price": cls._dec(row.market_price),
            "source": "local_db",
        }


__all__ = ["ETFService"]
