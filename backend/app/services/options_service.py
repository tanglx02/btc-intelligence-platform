"""OptionsService — 期权业务服务。

对外提供 BTC 期权聚合指标（OI/成交量/IV/Put-Call/Skew/GEX/到期日）的统一读取入口：
1. Redis 缓存热数据（期权指标实时性中等）
2. ProviderManager 多源自动 Failover（Deribit 主 / Coinglass 聚合辅）
3. 本地优先：OI/成交量可由 options_data 表最新快照聚合得到
4. 统一 ServiceResult 返回

说明：IV/Skew/GEX/Put-Call 为 Deribit 实时计算的派生指标，不做本地持久化优先，
直接走缓存 + Failover。
"""

from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select

from app.core.database import get_db_session_ctx
from app.models.derivatives import OptionsData
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult

_OI_CACHE_TTL = 120
_IV_CACHE_TTL = 120
_RATIO_CACHE_TTL = 180
# 本地聚合快照可接受的最大陈旧度（10 分钟）
_LOCAL_MAX_AGE = 600


class OptionsService(CategoryServiceBase):
    """期权业务服务。

    Usage:
        service = OptionsService(provider_manager)
        result = await service.get_implied_volatility("BTC")
        if result.success:
            print(result.data)
    """

    CATEGORY = "options"
    CACHE_PREFIX = "options:svc:"
    DEFAULT_CACHE_TTL = _OI_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 对外业务方法 ----

    async def get_options_oi(self, symbol: str = "BTC") -> ServiceResult:
        """期权未平仓量（Call/Put 分解）。"""

        async def _load_local() -> Any:
            return await self._load_oi_snapshot(symbol)

        return await self.get_data(
            "get_options_oi",
            data_type=f"options:oi:{symbol}",
            cache_ttl=_OI_CACHE_TTL,
            local_loader=_load_local,
            symbol=symbol,
        )

    async def get_options_volume(self, symbol: str = "BTC") -> ServiceResult:
        """期权成交量（Call/Put 分解）。"""
        return await self.get_data(
            "get_options_volume",
            data_type=f"options:volume:{symbol}",
            cache_ttl=_OI_CACHE_TTL,
            symbol=symbol,
        )

    async def get_implied_volatility(self, symbol: str = "BTC") -> ServiceResult:
        """隐含波动率（ATM IV + DVOL 指数）。"""
        return await self.get_data(
            "get_implied_volatility",
            data_type=f"options:iv:{symbol}",
            cache_ttl=_IV_CACHE_TTL,
            symbol=symbol,
        )

    async def get_put_call_ratio(self, symbol: str = "BTC") -> ServiceResult:
        """Put/Call 比率（成交量与未平仓量两个口径）。"""
        return await self.get_data(
            "get_put_call_ratio",
            data_type=f"options:pcr:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
        )

    async def get_skew(self, symbol: str = "BTC") -> ServiceResult:
        """波动率偏斜（25-Delta 风险逆转）。"""
        return await self.get_data(
            "get_skew",
            data_type=f"options:skew:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
        )

    async def get_gamma_exposure(self, symbol: str = "BTC") -> ServiceResult:
        """Gamma 敞口（GEX，做市商对冲惯例估算）。"""
        return await self.get_data(
            "get_gamma_exposure",
            data_type=f"options:gex:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
        )

    async def get_expiration_data(self, symbol: str = "BTC") -> ServiceResult:
        """到期日数据（各到期日 OI / 成交量分布）。"""
        return await self.get_data(
            "get_expiration_data",
            data_type=f"options:expiration:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
        )

    # ---- 内部：数据库聚合 ----

    async def _load_oi_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """由 options_data 表最新快照聚合 OI（Call/Put 分解）。"""
        try:
            async with get_db_session_ctx() as session:
                ts_stmt = select(func.max(OptionsData.observation_time)).where(
                    OptionsData.underlying == symbol.upper()
                )
                latest = (await session.execute(ts_stmt)).scalar_one_or_none()
                if latest is None:
                    return None
                age = datetime.now(tz=latest.tzinfo) - latest
                if age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None

                stmt = select(OptionsData).where(
                    OptionsData.underlying == symbol.upper(),
                    OptionsData.observation_time == latest,
                )
                rows = (await session.execute(stmt)).scalars().all()
                if not rows:
                    return None

                call_oi = put_oi = 0.0
                for r in rows:
                    oi = float(r.open_interest) if r.open_interest is not None else 0.0
                    if r.option_type == "CALL":
                        call_oi += oi
                    else:
                        put_oi += oi
                return {
                    "data_type": "oi",
                    "symbol": symbol.upper(),
                    "open_interest": call_oi + put_oi,
                    "call_oi": call_oi,
                    "put_oi": put_oi,
                    "timestamp": latest.isoformat(),
                    "source": "local_db",
                }
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
            logger.warning(f"options_data aggregation failed: {e}")
            return None


__all__ = ["OptionsService"]
