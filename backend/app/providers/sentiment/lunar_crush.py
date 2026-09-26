"""LunarCrushProvider — LunarCrush 社交媒体情绪数据源（需 API Key）。

API: https://lunarcrush.com/api4/public/coins/{symbol}/v1
鉴权：Authorization: Bearer <LUNARCRUSH_API_KEY>（在 lunarcrush.com 申请）

覆盖：
- get_social_sentiment(symbol, period) : Galaxy Score / 社交情绪评分（0-100）
- get_social_volume(symbol, period)    : 社交讨论量（24h 互动/发帖数）
其余维度（Fear & Greed / Google Trends / 新闻）由其他 Provider 覆盖，
标记为 UNSUPPORTED。

说明：LunarCrush 免费层级速率受限，未配置 API Key 时返回 AUTH_ERROR，
由 Failover 切换到 Alternative.me（仅提供 Fear & Greed）。
"""

from datetime import datetime
from typing import Any

from app.providers.base.payloads import (
    auth_required_result,
    sentiment_payload,
    unsupported_result,
)
from app.providers.base.sentiment_provider import BaseSentimentProvider
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class LunarCrushProvider(BaseSentimentProvider):
    """LunarCrush 社交情绪 Provider（需 API Key）。"""

    # 注册名（与 providers.yaml 配置 key 对齐）
    provider_key = "lunarcrush"

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求 BTC 社交指标。"""
        result = await self._get_coin("BTC")
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="LunarCrush 社交媒体情绪与热度（Galaxy Score / 社交量）",
                supported_symbols=["BTC", "ETH"],
                documentation_url="https://lunarcrush.com/developers/api-v4",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return ["social_sentiment", "social_volume"]

    # ---- 内部工具 ----

    async def _get_coin(self, symbol: str = "BTC") -> FetchResult:
        """请求 /public/coins/{symbol}/v1 并解包 data 字段。"""
        if not self._config.api_key:
            return auth_required_result(
                self.name, "LUNARCRUSH_API_KEY not configured (apply at lunarcrush.com)"
            )

        headers = {"Authorization": f"Bearer {self._config.api_key}"}
        path = f"/public/coins/{symbol.upper()}/v1"
        result = await self._request("GET", path, headers=headers)
        if not result.success:
            if result.status_code in (401, 403):
                result.error_type = ErrorType.AUTH_ERROR
                result.error = f"LunarCrush auth failed: {result.error}"
            return result

        body = result.data
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return FetchResult(
                success=False,
                error=f"Unexpected LunarCrush response for '{symbol}'",
                error_type=ErrorType.DATA_FORMAT,
                provider_name=self.name,
                status_code=result.status_code,
            )
        result.data = data
        return result

    # ---- 基类接口实现 ----

    async def get_social_sentiment(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> FetchResult:
        """社交媒体情绪评分。

        使用 LunarCrush 的 social_score / galaxy_score（0-100，越高越积极/热门）。
        """
        result = await self._get_coin(symbol)
        if not result.success:
            return result

        data = result.data
        social_score = _to_float(data.get("social_score") or data.get("social_score_24h"))
        galaxy_score = _to_float(data.get("galaxy_score"))
        sentiment = _to_float(data.get("sentiment"))

        # 主值优先取 social_score，退化到 galaxy_score
        value = social_score if social_score is not None else galaxy_score
        if value is None:
            return FetchResult(
                success=False,
                error=f"No social sentiment fields for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        ts = datetime.utcnow()
        return FetchResult(
            success=True,
            data=sentiment_payload(
                "social_score",
                value,
                label=self._classify(value),
                timestamp=ts,
                source=self.name,
                extra={
                    "symbol": symbol.upper(),
                    "galaxy_score": galaxy_score,
                    "social_score": social_score,
                    "sentiment": sentiment,
                    "altrank": data.get("altrank"),
                    "period": period,
                },
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"symbol": symbol.upper(), "range": "0-100"},
        )

    async def get_social_volume(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> FetchResult:
        """社交讨论量（24h 发帖数 / 互动数）。"""
        result = await self._get_coin(symbol)
        if not result.success:
            return result

        data = result.data
        posts = _to_float(
            data.get("social_volume_24h") or data.get("social_volume")
        )
        interactions = _to_float(
            data.get("social_impact_score") or data.get("interactions_24h")
        )
        if posts is None and interactions is None:
            return FetchResult(
                success=False,
                error=f"No social volume fields for '{symbol}'",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        ts = datetime.utcnow()
        return FetchResult(
            success=True,
            data=sentiment_payload(
                "social_volume",
                posts if posts is not None else (interactions or 0.0),
                timestamp=ts,
                source=self.name,
                extra={
                    "symbol": symbol.upper(),
                    "social_volume_24h": posts,
                    "interactions_24h": interactions,
                    "period": period,
                },
            ),
            provider_name=self.name,
            fetch_time=ts,
            observation_time=ts,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"symbol": symbol.upper()},
        )

    async def get_fear_greed(self, date: datetime | None = None) -> FetchResult:
        """恐惧贪婪指数：LunarCrush 不提供（由 Alternative.me 覆盖）。"""
        return unsupported_result(self.name, "fear_greed")

    async def get_google_trends(
        self, keyword: str = "bitcoin", period: str = "7d"
    ) -> FetchResult:
        """Google Trends：LunarCrush 不提供。"""
        return unsupported_result(self.name, "google_trends")

    async def get_news_sentiment(self, period: str = "24h") -> FetchResult:
        """新闻情绪：LunarCrush v4 coins 端点不单独提供。"""
        return unsupported_result(self.name, "news_sentiment")

    # ---- 内部辅助 ----

    @staticmethod
    def _classify(value: float) -> str:
        """将 0-100 社交评分映射到情绪标签。"""
        if value >= 80:
            return "EXTREME_GREED"
        if value >= 60:
            return "GREED"
        if value >= 40:
            return "NEUTRAL"
        if value >= 20:
            return "FEAR"
        return "EXTREME_FEAR"


__all__ = ["LunarCrushProvider"]
