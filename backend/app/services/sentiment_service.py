"""SentimentService — 市场情绪业务服务。

对外提供情绪指标（恐惧贪婪指数 / 社交情绪 / 社交量 / 新闻情绪）的统一读取入口：
1. 本地优先：先查 sentiment 表（恐惧贪婪指数为日频，适合本地优先）
2. Redis 缓存热数据
3. ProviderManager 多源自动 Failover（Alternative.me / LunarCrush）
4. 统一 ServiceResult 返回
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.sentiment import Sentiment
from app.providers.base.types import QualityStatus
from app.providers.manager import ProviderManager
from app.services.base_service import CategoryServiceBase
from app.services.provider_service import ServiceResult

_FEAR_GREED_CACHE_TTL = 1800
_SOCIAL_CACHE_TTL = 300
# 恐惧贪婪指数日频，本地陈旧度阈值 2 天
_LOCAL_MAX_AGE = 2 * 86400
_FEAR_GREED_METRIC = "fear_greed_index"


class SentimentService(CategoryServiceBase):
    """市场情绪业务服务。

    Usage:
        service = SentimentService(provider_manager)
        result = await service.get_fear_greed()
        if result.success:
            print(result.data["value"], result.data["label"])
    """

    CATEGORY = "sentiment"
    CACHE_PREFIX = "sentiment:svc:"
    DEFAULT_CACHE_TTL = _SOCIAL_CACHE_TTL

    def __init__(
        self,
        provider_manager: ProviderManager,
        data_store: Any = None,
        cache: Any = None,
    ):
        super().__init__(provider_manager, data_store, cache)

    # ---- 对外业务方法 ----

    async def get_fear_greed(self, date: datetime | None = None) -> ServiceResult:
        """恐惧贪婪指数（0-100，本地优先）。"""

        async def _load_local() -> Any:
            return await self._load_latest_fear_greed(date)

        return await self.get_data(
            "get_fear_greed",
            data_type="sentiment:fear_greed",
            cache_ttl=_FEAR_GREED_CACHE_TTL,
            local_loader=_load_local,
            date=date,
        )

    async def get_fear_greed_history(
        self, start: datetime, end: datetime
    ) -> ServiceResult:
        """恐惧贪婪指数历史序列（本地优先）。"""
        local = await self._load_fear_greed_range(start, end)
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
            "get_fear_greed_history",
            data_type="sentiment:fear_greed_history",
            cache_ttl=_FEAR_GREED_CACHE_TTL,
            limit=365,
        )

    async def get_social_sentiment(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> ServiceResult:
        """社交媒体情绪评分（LunarCrush）。"""
        return await self.get_data(
            "get_social_sentiment",
            data_type=f"sentiment:social:{symbol}",
            cache_ttl=_SOCIAL_CACHE_TTL,
            period=period,
            symbol=symbol,
        )

    async def get_social_volume(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> ServiceResult:
        """社交讨论量（LunarCrush）。"""
        return await self.get_data(
            "get_social_volume",
            data_type=f"sentiment:social_volume:{symbol}",
            cache_ttl=_SOCIAL_CACHE_TTL,
            period=period,
            symbol=symbol,
        )

    async def get_news_sentiment(self, period: str = "24h") -> ServiceResult:
        """新闻情绪评分（当前无 Provider 覆盖时返回 UNSUPPORTED）。"""
        return await self.get_data(
            "get_news_sentiment",
            data_type="sentiment:news",
            cache_ttl=_SOCIAL_CACHE_TTL,
            period=period,
        )

    async def get_google_trends(
        self, keyword: str = "bitcoin", period: str = "7d"
    ) -> ServiceResult:
        """Google Trends 搜索热度（当前无 Provider 覆盖时返回 UNSUPPORTED）。"""
        return await self.get_data(
            "get_google_trends",
            data_type=f"sentiment:trends:{keyword}",
            cache_ttl=_FEAR_GREED_CACHE_TTL,
            keyword=keyword,
            period=period,
        )

    # ---- 内部：数据库查询 ----

    async def _load_latest_fear_greed(
        self, date: datetime | None
    ) -> dict[str, Any] | None:
        """查询本地 sentiment 表最新恐惧贪婪指数。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(Sentiment).where(
                    Sentiment.metric_name == _FEAR_GREED_METRIC
                )
                if date is not None:
                    stmt = stmt.where(
                        Sentiment.observation_time <= self._as_utc(date)
                    )
                stmt = stmt.order_by(Sentiment.observation_time.desc()).limit(1)
                row = (await session.execute(stmt)).scalar_one_or_none()
                if row is None:
                    return None
                age = datetime.now(tz=row.observation_time.tzinfo) - row.observation_time
                if date is None and age > timedelta(seconds=_LOCAL_MAX_AGE):
                    return None
                return self._row_to_dict(row, "fear_greed")
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为纯 API
            logger.warning(f"sentiment query failed: {e}")
            return None

    async def _load_fear_greed_range(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]] | None:
        """查询本地 sentiment 表恐惧贪婪指数区间序列。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = (
                    select(Sentiment)
                    .where(
                        Sentiment.metric_name == _FEAR_GREED_METRIC,
                        Sentiment.observation_time >= self._as_utc(start),
                        Sentiment.observation_time <= self._as_utc(end),
                    )
                    .order_by(Sentiment.observation_time)
                )
                rows = (await session.execute(stmt)).scalars().all()
                return [self._row_to_dict(r, "fear_greed") for r in rows] or None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"sentiment range query failed: {e}")
            return None

    # ---- 内部：ORM -> dict ----

    @staticmethod
    def _row_to_dict(row: Sentiment, indicator: str) -> dict[str, Any]:
        def _f(v: Decimal | None) -> float | None:
            return float(v) if v is not None else None

        return {
            "indicator": indicator,
            "value": _f(row.value),
            "normalized_value": _f(row.normalized_value),
            "label": row.sentiment_label,
            "timestamp": row.observation_time.isoformat(),
            "source": "local_db",
            "metadata": row.metadata_ or {},
        }


__all__ = ["SentimentService"]
