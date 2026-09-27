"""MacroService — 宏观经济业务服务。

对外提供宏观指标（DXY/利率/收益率/通胀/就业/GDP/M2/流动性）的统一读取入口：
1. 本地优先：先查 macro_series 表，且严格遵守 release_date（防未来数据泄漏）
2. Redis 缓存热数据
3. ProviderManager 多源自动 Failover（FRED 主 / World Bank 辅）
4. 统一 ServiceResult 返回

Look-ahead Bias 防护：
- 本地查询时，若给定 as_of 日期，仅返回 release_date <= as_of 的观测值，
  确保回测中不会使用“当时尚未发布”的数据。
"""

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.macro import MacroSeries
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult
from app.utils.datetime_utils import utcnow

# 宏观数据缓存 TTL（秒）—— 日/月/季频，缓存较久
_DAILY_CACHE_TTL = 3600
_MONTHLY_CACHE_TTL = 6 * 3600
# 本地日频数据可接受的最大陈旧度（10 天，覆盖节假日）
_LOCAL_MAX_AGE_DAILY = 10 * 86400

# 指标 -> macro_series.series_id（与 SyncService 落库口径一致）
_SERIES_ID = {
    "dxy": "DXY",
    "fed_rate": "FED_RATE",
    "treasury_2y": "US2Y",
    "treasury_5y": "US5Y",
    "treasury_10y": "US10Y",
    "treasury_30y": "US30Y",
    "real_yield": "REAL_YIELD",
    "cpi": "CPI",
    "pce": "PCE",
    "unemployment": "UNEMPLOYMENT",
    "nfp": "NFP",
    "gdp": "GDP",
    "m2": "M2",
    "fed_balance_sheet": "FED_BALANCE_SHEET",
}


class MacroService(CategoryServiceBase):
    """宏观经济业务服务。

    Usage:
        service = MacroService(provider_manager)
        result = await service.get_fed_rate()
        if result.success:
            print(result.data)
    """

    CATEGORY = "macro"
    CACHE_PREFIX = "macro:svc:"
    DEFAULT_CACHE_TTL = _DAILY_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 对外业务方法 ----

    async def get_dxy(self, date: datetime | None = None) -> ServiceResult:
        """美元指数（DXY）。"""
        return await self._get_indicator("get_dxy", "dxy", date, monthly=False)

    async def get_fed_rate(self, date: datetime | None = None) -> ServiceResult:
        """联邦基金利率。"""
        return await self._get_indicator("get_fed_rate", "fed_rate", date, monthly=True)

    async def get_treasury_yield(
        self, maturity: str = "10y", date: datetime | None = None
    ) -> ServiceResult:
        """国债收益率（2y/5y/10y/30y）。"""
        series_key = f"treasury_{maturity.lower()}"
        if series_key not in _SERIES_ID:
            return self._invalid(f"不支持的国债期限: {maturity}（可用 2y/5y/10y/30y）")

        async def _load_local() -> Any:
            return await self._load_latest(series_key, date)

        return await self.get_data(
            "get_treasury_yield",
            data_type=f"macro:treasury:{maturity}",
            cache_ttl=_DAILY_CACHE_TTL,
            local_loader=_load_local,
            maturity=maturity,
            date=date,
        )

    async def get_real_yield(self, date: datetime | None = None) -> ServiceResult:
        """10Y 实际收益率（TIPS）。"""
        return await self._get_indicator("get_real_yield", "real_yield", date, monthly=False)

    async def get_cpi(self, date: datetime | None = None) -> ServiceResult:
        """CPI 消费者物价指数。"""
        return await self._get_indicator("get_cpi", "cpi", date, monthly=True)

    async def get_pce(self, date: datetime | None = None) -> ServiceResult:
        """PCE 个人消费支出物价指数。"""
        return await self._get_indicator("get_pce", "pce", date, monthly=True)

    async def get_unemployment(self, date: datetime | None = None) -> ServiceResult:
        """失业率。"""
        return await self._get_indicator("get_unemployment", "unemployment", date, monthly=True)

    async def get_nfp(self, date: datetime | None = None) -> ServiceResult:
        """非农就业人数。"""
        return await self._get_indicator("get_nfp", "nfp", date, monthly=True)

    async def get_gdp(self, date: datetime | None = None) -> ServiceResult:
        """GDP 国内生产总值。"""
        return await self._get_indicator("get_gdp", "gdp", date, monthly=True)

    async def get_m2(self, date: datetime | None = None) -> ServiceResult:
        """M2 货币供应量。"""
        return await self._get_indicator("get_m2", "m2", date, monthly=True)

    async def get_fed_balance_sheet(self, date: datetime | None = None) -> ServiceResult:
        """美联储资产负债表。"""
        return await self._get_indicator(
            "get_fed_balance_sheet", "fed_balance_sheet", date, monthly=False
        )

    async def get_global_liquidity(self, date: datetime | None = None) -> ServiceResult:
        """全球流动性指数（FRED 无直接序列，可能返回 UNSUPPORTED）。"""
        return await self.get_data(
            "get_global_liquidity",
            data_type="macro:global_liquidity",
            cache_ttl=_MONTHLY_CACHE_TTL,
            date=date,
        )

    async def get_macro_series(
        self, indicator: str, start: datetime, end: datetime
    ) -> ServiceResult:
        """宏观指标历史序列（本地优先，缺失回源）。"""
        local = await self._load_range(indicator, start, end)
        if local:
            return ServiceResult(
                success=True,
                data=local,
                source="local_db",
                fetch_time=utcnow(),
                metadata={"local_first": True, "count": len(local)},
            )
        return await self.get_data(
            "get_macro_series",
            data_type=f"macro:series:{indicator}",
            cache_ttl=_MONTHLY_CACHE_TTL,
            indicator=indicator,
            start=start,
            end=end,
        )

    # ---- 内部：单指标读取 ----

    async def _get_indicator(
        self, method: str, series_key: str, date: datetime | None, monthly: bool
    ) -> ServiceResult:
        async def _load_local() -> Any:
            return await self._load_latest(series_key, date)

        return await self.get_data(
            method,
            data_type=f"macro:{series_key}",
            cache_ttl=_MONTHLY_CACHE_TTL if monthly else _DAILY_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    # ---- 内部：数据库查询（严格 release_date 过滤）----

    async def _load_latest(
        self, series_key: str, as_of: datetime | None
    ) -> dict[str, Any] | None:
        """查询本地 macro_series 最新观测值。

        防未来数据泄漏：as_of 给定时仅取 release_date <= as_of 的记录。
        """
        series_id = _SERIES_ID.get(series_key)
        if not series_id:
            return None
        try:
            async with get_db_session_ctx() as session:
                stmt = select(MacroSeries).where(MacroSeries.series_id == series_id)
                if as_of is not None:
                    stmt = stmt.where(MacroSeries.release_date <= as_of.date())
                stmt = stmt.order_by(MacroSeries.observation_date.desc()).limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                # 无 as_of 时做日频陈旧度检查（月频指标放宽，不检查）
                if as_of is None and series_key in ("dxy", "real_yield"):
                    age = datetime.now(tz=row.observation_time.tzinfo) - row.observation_time
                    if age > timedelta(seconds=_LOCAL_MAX_AGE_DAILY):
                        return None
                return self._row_to_dict(row)
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
            logger.warning(f"macro_series query failed: {e}")
            return None

    async def _load_range(
        self, series_key: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]] | None:
        """查询本地 macro_series 区间序列（release_date <= end，防泄漏）。"""
        series_id = _SERIES_ID.get(series_key)
        if not series_id:
            return None
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(MacroSeries)
                    .where(
                        MacroSeries.series_id == series_id,
                        MacroSeries.observation_date >= start.date(),
                        MacroSeries.observation_date <= end.date(),
                        MacroSeries.release_date <= end.date(),
                    )
                    .order_by(MacroSeries.observation_date)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [self._row_to_dict(r) for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"macro_series range query failed: {e}")
            return None

    # ---- 内部：ORM -> dict ----

    @staticmethod
    def _row_to_dict(row: MacroSeries) -> dict[str, Any]:
        return {
            "indicator": row.series_id,
            "series_name": row.series_name,
            "value": float(row.value) if row.value is not None else None,
            "unit": row.unit,
            "frequency": row.frequency,
            "observation_date": row.observation_date.isoformat(),
            "release_date": row.release_date.isoformat() if row.release_date else None,
            "revision_date": row.revision_date.isoformat() if row.revision_date else None,
            "is_revised": row.is_revised,
            "source": "local_db",
        }


__all__ = ["MacroService"]
