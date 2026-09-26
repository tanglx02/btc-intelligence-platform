"""BaseETFProvider — ETF 数据 Provider 抽象基类。

定义所有 ETF 数据源的统一接口，包括：
- 每日流入/流出、净流量
- 周期流量（7D/30D/90D）
- 累计流量、ETF 持仓量
"""

from abc import abstractmethod
from datetime import datetime

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseETFProvider(BaseProvider):
    """ETF 数据 Provider 基类。

    所有 ETF 数据源（Farside、SoSoValue 等）必须继承此类。

    数据覆盖：
    - 每日 ETF 净流入/流出（分基金明细）
    - 周期汇总流量（7D / 30D / 90D）
    - 累计流量序列
    - ETF BTC 持仓量
    """

    @abstractmethod
    async def get_daily_flow(self, date: datetime | None = None) -> FetchResult:
        """获取指定日期 ETF 每日净流入/流出。

        Args:
            date: 查询日期（默认今日）

        Returns:
            FetchResult[data=list[ETFFlowData]]
        """
        ...

    @abstractmethod
    async def get_net_flow(
        self, period: str = "7d", end_date: datetime | None = None
    ) -> FetchResult:
        """获取指定周期净流量。

        Args:
            period: 周期 (1d / 7d / 30d / 90d)
            end_date: 截止日期

        Returns:
            FetchResult[data=ETFFlowData]
        """
        ...

    @abstractmethod
    async def get_cumulative_flow(
        self, start: datetime, end: datetime
    ) -> FetchResult:
        """获取累计流量序列。

        Args:
            start: 起始日期
            end: 结束日期

        Returns:
            FetchResult[data=list[ETFFlowData]] 按日期排序的累计流量
        """
        ...

    @abstractmethod
    async def get_holdings(self, date: datetime | None = None) -> FetchResult:
        """获取 ETF 持仓量 (BTC)。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=ETFHoldingsData]
        """
        ...

    @abstractmethod
    async def get_all_funds_flow(self, date: datetime | None = None) -> FetchResult:
        """获取所有 ETF 基金的分基金流量明细。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=dict[str, ETFFlowData]] key 为基金代码（IBIT, FBTC 等）
        """
        ...


__all__ = ["BaseETFProvider"]
