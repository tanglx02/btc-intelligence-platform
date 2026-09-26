"""BaseSentimentProvider — 市场情绪 Provider 抽象基类。

定义所有市场情绪数据源的统一接口，包括：
- 恐惧贪婪指数
- Google Trends 搜索热度
- 新闻情绪评分
- 社交媒体情绪评分
"""

from abc import abstractmethod
from datetime import datetime

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseSentimentProvider(BaseProvider):
    """市场情绪 Provider 基类。

    所有情绪数据源（Alternative.me、LunarCrush 等）必须继承此类。

    数据覆盖：
    - 恐惧贪婪指数 (Fear & Greed Index)：0-100
    - Google Trends 搜索热度
    - 新闻情绪评分
    - 社交媒体情绪评分
    """

    @abstractmethod
    async def get_fear_greed(self, date: datetime | None = None) -> FetchResult:
        """恐惧贪婪指数 (0-100)。

        Args:
            date: 查询日期（默认今日）

        Returns:
            FetchResult[data=SentimentData] 包含 value (0-100), label ("Extreme Greed" 等)
        """
        ...

    @abstractmethod
    async def get_google_trends(
        self, keyword: str = "bitcoin", period: str = "7d"
    ) -> FetchResult:
        """Google Trends 搜索热度。

        Args:
            keyword: 搜索关键词
            period: 时间范围 (7d / 30d / 90d)

        Returns:
            FetchResult[data=TrendsData]
        """
        ...

    @abstractmethod
    async def get_news_sentiment(self, period: str = "24h") -> FetchResult:
        """新闻情绪评分。

        Args:
            period: 统计周期

        Returns:
            FetchResult[data=NewsSentimentData]
        """
        ...

    @abstractmethod
    async def get_social_sentiment(self, period: str = "24h") -> FetchResult:
        """社交媒体情绪评分。

        Args:
            period: 统计周期

        Returns:
            FetchResult[data=SocialSentimentData]
        """
        ...


__all__ = ["BaseSentimentProvider"]
