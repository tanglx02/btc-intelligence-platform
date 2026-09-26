"""BaseDerivativeProvider — 衍生品 Provider 抽象基类。

定义所有衍生品数据源的统一接口，包括：
- 资金费率、未平仓合约量、清算数据
- 多空比、基差、溢价率
- 主动买卖量、衍生品 CVD
"""

from abc import abstractmethod

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseDerivativeProvider(BaseProvider):
    """衍生品 Provider 基类。

    所有衍生品数据源（Binance Futures、Bybit、Coinglass 等）必须继承此类。

    数据覆盖：
    - 资金费率（Funding Rate）：当前/历史
    - 未平仓合约量（Open Interest）
    - 清算数据（Liquidation）
    - 多空比（Long/Short Ratio）
    - 基差（Basis）与溢价（Premium）
    - 主动买卖量（Taker Buy/Sell）
    - 衍生品 CVD
    """

    @abstractmethod
    async def get_funding_rate(
        self, symbol: str = "BTC/USDT", limit: int = 1
    ) -> FetchResult:
        """获取资金费率（当前/历史）。

        Args:
            symbol: 交易对
            limit: 返回条数（1=当前，>1=历史）

        Returns:
            FetchResult[data=FundingData | list[FundingData]]
        """
        ...

    @abstractmethod
    async def get_open_interest(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取未平仓合约量。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=OpenInterestData]
        """
        ...

    @abstractmethod
    async def get_liquidation(
        self, symbol: str = "BTC/USDT", period: str = "1h"
    ) -> FetchResult:
        """获取清算数据。

        Args:
            symbol: 交易对
            period: 统计周期

        Returns:
            FetchResult[data=LiquidationData]
        """
        ...

    @abstractmethod
    async def get_long_short_ratio(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取多空比。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=RatioData]
        """
        ...

    @abstractmethod
    async def get_basis(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取基差（期货价格 - 现货价格）。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=BasisData]
        """
        ...

    @abstractmethod
    async def get_premium(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取溢价率。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=PremiumData]
        """
        ...

    @abstractmethod
    async def get_taker_buy_sell(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """获取主动买卖量。

        Args:
            symbol: 交易对
            interval: 统计间隔

        Returns:
            FetchResult[data=TakerData]
        """
        ...

    @abstractmethod
    async def get_derivative_cvd(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """获取衍生品 CVD（累计成交量差）。

        Args:
            symbol: 交易对
            interval: 统计间隔

        Returns:
            FetchResult[data=CVDData]
        """
        ...


__all__ = ["BaseDerivativeProvider"]
