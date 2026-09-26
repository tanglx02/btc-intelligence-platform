"""AlternativeMeProvider — Alternative.me 恐惧贪婪指数（完全免费，无需 API Key）。

API: https://api.alternative.me/fng/
返回：value(0-100)、value_classification（Extreme Fear ... Extreme Greed）、timestamp

恐惧贪婪指数由波动率、市场动能、社交媒体、问卷、BTC 主导率、Google Trends
等多因子合成，是加密市场情绪的经典量化指标。

覆盖：
- get_fear_greed(date)  : 指定日期（或最新）的恐惧贪婪指数
- get_fear_greed_history: 历史序列（扩展方法，limit 控制天数）
其余情绪维度（Google Trends / 新闻 / 社交）Alternative.me 不单独提供，
标记为 UNSUPPORTED，由 Failover 切换到 LunarCrush。
"""

from datetime import datetime, timezone
from typing import Any

from app.providers.base.payloads import sentiment_payload, unsupported_result
from app.providers.base.sentiment_provider import BaseSentimentProvider
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    ProviderMetadata,
    QualityStatus,
)

# value_classification -> 标准情绪标签
_LABEL_MAP = {
    "Extreme Fear": "EXTREME_FEAR",
    "Fear": "FEAR",
    "Neutral": "NEUTRAL",
    "Greed": "GREED",
    "Extreme Greed": "EXTREME_GREED",
}


def _ts_to_dt(ts: Any) -> datetime | None:
    """Unix 秒时间戳 -> naive UTC datetime。"""
    try:
        num = float(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)


class AlternativeMeProvider(BaseSentimentProvider):
    """Alternative.me 恐惧贪婪指数 Provider（免费）。"""

    # ---- 生命周期 ----

    async def health_check(self) -> bool:
        """连通性探测：请求最新 1 天恐惧贪婪指数。"""
        result = await self._request("GET", "/fng/", params={"limit": 1, "format": "json"})
        return result.success

    def get_metadata(self) -> ProviderMetadata:
        if self._metadata is None:
            self._metadata = ProviderMetadata(
                name=self.name,
                category=self.category,
                description="Alternative.me 加密货币恐惧贪婪指数（0-100）",
                supported_symbols=["BTC"],
                data_coverage_start=datetime(2018, 2, 1),
                documentation_url="https://alternative.me/crypto/fear-and-greed-index/",
            )
        return self._metadata

    def get_supported_data_types(self) -> list[str]:
        return ["fear_greed"]

    # ---- 内部工具 ----

    async def _fetch_fng(self, limit: int = 30) -> FetchResult:
        """请求 /fng/ 并返回标准化后的观测点列表（升序）。"""
        result = await self._request(
            "GET", "/fng/", params={"limit": max(1, limit), "format": "json"}
        )
        if not result.success:
            return result

        body = result.data
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or not data:
            return FetchResult(
                success=False,
                error="Empty Fear & Greed response",
                error_type=ErrorType.EMPTY_RESPONSE,
                provider_name=self.name,
                status_code=result.status_code,
            )

        points: list[dict[str, Any]] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                value = float(row.get("value"))
            except (TypeError, ValueError):
                continue
            classification = row.get("value_classification") or ""
            points.append({
                "value": value,
                "label": _LABEL_MAP.get(classification, classification.upper() or None),
                "label_raw": classification or None,
                "timestamp": _ts_to_dt(row.get("timestamp")),
            })

        points.sort(key=lambda p: p["timestamp"] or datetime.min)
        result.data = points
        return result

    # ---- 基类接口实现 ----

    async def get_fear_greed(self, date: datetime | None = None) -> FetchResult:
        """恐惧贪婪指数（指定日期或最新）。

        date 为 None 时返回最新值；否则返回 <= date 的最近一个观测点。
        Alternative.me 按 UTC 每日更新（00:00 UTC 附近）。
        """
        # 拉取足够窗口以覆盖历史 date 查询
        limit = 1 if date is None else 2000
        result = await self._fetch_fng(limit=limit)
        if not result.success:
            return result

        points = result.data
        chosen = points[-1]
        if date is not None:
            eligible = [
                p for p in points
                if p["timestamp"] is not None and p["timestamp"] <= date
            ]
            if not eligible:
                return FetchResult(
                    success=False,
                    error=f"No Fear & Greed data before {date:%Y-%m-%d}",
                    error_type=ErrorType.EMPTY_RESPONSE,
                    provider_name=self.name,
                )
            chosen = eligible[-1]

        obs_time = chosen["timestamp"] or datetime.utcnow()
        return FetchResult(
            success=True,
            data=sentiment_payload(
                "fear_greed",
                chosen["value"],
                label=chosen["label"],
                timestamp=obs_time,
                source=self.name,
                extra={"label_raw": chosen["label_raw"], "range": "0-100"},
            ),
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=obs_time,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"range": "0-100"},
        )

    async def get_google_trends(
        self, keyword: str = "bitcoin", period: str = "7d"
    ) -> FetchResult:
        """Google Trends：Alternative.me 不单独提供（指数已内含该因子）。"""
        return unsupported_result(self.name, "google_trends")

    async def get_news_sentiment(self, period: str = "24h") -> FetchResult:
        """新闻情绪：Alternative.me 不提供。"""
        return unsupported_result(self.name, "news_sentiment")

    async def get_social_sentiment(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> FetchResult:
        """社交情绪：Alternative.me 不提供（由 LunarCrush 覆盖）。

        symbol 参数仅为与 LunarCrush 保持接口一致（忽略）。
        """
        return unsupported_result(self.name, "social_sentiment")

    # ---- 扩展方法（非基类接口）----

    async def get_social_volume(
        self, period: str = "24h", symbol: str = "BTC"
    ) -> FetchResult:
        """社交讨论量：Alternative.me 不提供（由 LunarCrush 覆盖）。"""
        return unsupported_result(self.name, "social_volume")

    async def get_fear_greed_history(self, limit: int = 30) -> FetchResult:
        """恐惧贪婪指数历史序列（最近 limit 天，升序）。"""
        result = await self._fetch_fng(limit=limit)
        if not result.success:
            return result

        points = result.data
        series = [
            sentiment_payload(
                "fear_greed",
                p["value"],
                label=p["label"],
                timestamp=p["timestamp"],
                source=self.name,
                extra={"label_raw": p["label_raw"]},
            )
            for p in points
        ]
        return FetchResult(
            success=True,
            data=series,
            provider_name=self.name,
            fetch_time=datetime.utcnow(),
            observation_time=points[-1]["timestamp"] if points else None,
            quality_status=QualityStatus.VERIFIED,
            raw_response=result.raw_response,
            status_code=result.status_code,
            response_time_ms=result.response_time_ms,
            metadata={"count": len(series), "range": "0-100"},
        )


__all__ = ["AlternativeMeProvider"]
