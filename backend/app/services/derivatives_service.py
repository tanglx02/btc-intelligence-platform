"""DerivativesService — 衍生品业务服务。

对外提供永续/期货衍生品指标（资金费率/OI/清算/多空比/基差）的统一读取入口：
1. 本地优先：先查 derivatives 表（按 data_type + symbol）
2. Redis 缓存热数据（短 TTL，衍生品实时性强）
3. ProviderManager 多源自动 Failover（Binance Futures / Coinglass）
4. 统一 ServiceResult 返回
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.derivatives import Derivative
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult

# 衍生品缓存 TTL（秒）—— 实时性强，短缓存
_FUNDING_CACHE_TTL = 60
_OI_CACHE_TTL = 60
_LIQ_CACHE_TTL = 120
_RATIO_CACHE_TTL = 120
# 本地数据可接受的最大陈旧度（5 分钟）
_LOCAL_MAX_AGE = 300


class DerivativesService(CategoryServiceBase):
    """衍生品业务服务。

    Usage:
        service = DerivativesService(provider_manager)
        result = await service.get_funding_rate("BTCUSDT")
        if result.success:
            print(result.data)
    """

    CATEGORY = "derivatives"
    CACHE_PREFIX = "deriv:svc:"
    DEFAULT_CACHE_TTL = _OI_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 对外业务方法 ----

    async def get_funding_rate(
        self, symbol: str = "BTC/USDT", limit: int = 1
    ) -> ServiceResult:
        """资金费率（当期 + 预测）。"""

        async def _load_local() -> Any:
            return await self._load_latest(symbol, "FUNDING")

        return await self.get_data(
            "get_funding_rate",
            data_type=f"deriv:funding:{symbol}",
            cache_ttl=_FUNDING_CACHE_TTL,
            local_loader=_load_local,
            symbol=symbol,
            limit=limit,
        )

    async def get_open_interest(self, symbol: str = "BTC/USDT") -> ServiceResult:
        """未平仓合约量（OI）。"""

        async def _load_local() -> Any:
            return await self._load_latest(symbol, "OPEN_INTEREST")

        return await self.get_data(
            "get_open_interest",
            data_type=f"deriv:oi:{symbol}",
            cache_ttl=_OI_CACHE_TTL,
            local_loader=_load_local,
            symbol=symbol,
        )

    async def get_liquidation(
        self, symbol: str = "BTC/USDT", period: str = "1h"
    ) -> ServiceResult:
        """清算数据（多头/空头清算额）。"""
        return await self.get_data(
            "get_liquidation",
            data_type=f"deriv:liquidation:{symbol}",
            cache_ttl=_LIQ_CACHE_TTL,
            symbol=symbol,
            period=period,
        )

    async def get_long_short_ratio(self, symbol: str = "BTC/USDT") -> ServiceResult:
        """多空持仓/账户比。"""
        return await self.get_data(
            "get_long_short_ratio",
            data_type=f"deriv:long_short:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
        )

    async def get_basis(self, symbol: str = "BTC/USDT") -> ServiceResult:
        """基差（期货价格 - 现货价格）。"""
        return await self.get_data(
            "get_basis",
            data_type=f"deriv:basis:{symbol}",
            cache_ttl=_OI_CACHE_TTL,
            symbol=symbol,
        )

    async def get_premium(self, symbol: str = "BTC/USDT") -> ServiceResult:
        """溢价指数。"""
        return await self.get_data(
            "get_premium",
            data_type=f"deriv:premium:{symbol}",
            cache_ttl=_OI_CACHE_TTL,
            symbol=symbol,
        )

    async def get_taker_buy_sell(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> ServiceResult:
        """主动买卖量（Taker Buy/Sell）。"""
        return await self.get_data(
            "get_taker_buy_sell",
            data_type=f"deriv:taker:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
            interval=interval,
        )

    async def get_derivative_cvd(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> ServiceResult:
        """衍生品累计成交量差（CVD）。"""
        return await self.get_data(
            "get_derivative_cvd",
            data_type=f"deriv:cvd:{symbol}",
            cache_ttl=_RATIO_CACHE_TTL,
            symbol=symbol,
            interval=interval,
        )

    # ---- 内部：数据库查询 ----

    async def _load_latest(self, symbol: str, data_type: str) -> dict[str, Any] | None:
        """查询本地 derivatives 表最新（5 分钟内）指定类型记录。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(Derivative)
                    .where(
                        Derivative.data_type == data_type,
                        Derivative.symbol == self._compact(symbol),
                    )
                    .order_by(Derivative.observation_time.desc())
                    .limit(1)
                )
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                age = datetime.now(tz=row.observation_time.tzinfo) - row.observation_time
                if age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                return self._row_to_dict(row)
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
            logger.warning(f"derivatives query failed: {e}")
            return None

    # ---- 内部：工具 ----

    @staticmethod
    def _compact(symbol: str) -> str:
        """BTC/USDT -> BTCUSDT（与 DB 存储口径一致）。"""
        return symbol.replace("/", "").replace("-", "").replace("_", "").upper()

    @staticmethod
    def _dec(v: Decimal | None) -> float | None:
        return float(v) if v is not None else None

    @classmethod
    def _row_to_dict(cls, row: Derivative) -> dict[str, Any]:
        return {
            "data_type": row.data_type,
            "symbol": row.symbol,
            "exchange": row.exchange,
            "timestamp": row.observation_time.isoformat(),
            "funding_rate": cls._dec(row.funding_rate),
            "funding_rate_next": cls._dec(row.funding_rate_next),
            "open_interest": cls._dec(row.open_interest),
            "open_interest_usd": cls._dec(row.open_interest_usd),
            "long_short_ratio": cls._dec(row.long_short_ratio),
            "liquidation_long_usd": cls._dec(row.liquidation_long_usd),
            "liquidation_short_usd": cls._dec(row.liquidation_short_usd),
            "liquidation_total_usd": cls._dec(row.liquidation_total_usd),
            "basis": cls._dec(row.basis),
            "premium_index": cls._dec(row.premium_index),
            "cvd": cls._dec(row.cvd),
            "source": "local_db",
        }


__all__ = ["DerivativesService"]
