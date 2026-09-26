"""BaseOptionsProvider — 期权 Provider 抽象基类。

定义所有期权数据源的统一接口，包括：
- 期权未平仓量、成交量
- 隐含波动率、Put/Call Ratio
- 波动率偏斜、Gamma 敞口、到期日数据
"""

from abc import abstractmethod

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseOptionsProvider(BaseProvider):
    """期权 Provider 基类。

    所有期权数据源（Deribit、OKX Options 等）必须继承此类。

    数据覆盖：
    - 期权未平仓量 (OI)
    - 期权成交量
    - 隐含波动率 (IV)
    - Put/Call Ratio
    - 波动率偏斜 (Skew)
    - Gamma 敞口 (GEX)
    - 到期日数据
    """

    @abstractmethod
    async def get_options_oi(self, symbol: str = "BTC") -> FetchResult:
        """期权未平仓量。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=OptionsOIData]
        """
        ...

    @abstractmethod
    async def get_options_volume(self, symbol: str = "BTC") -> FetchResult:
        """期权成交量。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=OptionsVolumeData]
        """
        ...

    @abstractmethod
    async def get_implied_volatility(self, symbol: str = "BTC") -> FetchResult:
        """隐含波动率。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=IVData]
        """
        ...

    @abstractmethod
    async def get_put_call_ratio(self, symbol: str = "BTC") -> FetchResult:
        """Put/Call Ratio（看跌/看涨比率）。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=PutCallData]
        """
        ...

    @abstractmethod
    async def get_skew(self, symbol: str = "BTC") -> FetchResult:
        """波动率偏斜。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=SkewData]
        """
        ...

    @abstractmethod
    async def get_gamma_exposure(self, symbol: str = "BTC") -> FetchResult:
        """Gamma 敞口 (GEX)。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=GammaData]
        """
        ...

    @abstractmethod
    async def get_expiration_data(self, symbol: str = "BTC") -> FetchResult:
        """到期日数据。

        Args:
            symbol: 标的资产

        Returns:
            FetchResult[data=ExpirationData]
        """
        ...


__all__ = ["BaseOptionsProvider"]
