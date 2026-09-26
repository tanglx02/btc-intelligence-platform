"""BaseMacroProvider — 宏观经济 Provider 抽象基类。

定义所有宏观经济数据源的统一接口，包括：
- 美元指数 (DXY)、联邦基金利率
- 国债收益率、实际收益率
- CPI、PCE、失业率、非农就业、GDP
- M2 货币供应量、美联储资产负债表、全球流动性
"""

from abc import abstractmethod
from datetime import datetime

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseMacroProvider(BaseProvider):
    """宏观经济 Provider 基类。

    所有宏观数据源（FRED、Trading Economics 等）必须继承此类。

    特殊要求：
    - 必须区分 observation_date（数据所属期）、release_date（首次发布日期）、
      revision_date（修订日期），防止 Look-ahead Bias
    - 宏观数据更新频率低（日/月/季），但准确性要求极高

    数据覆盖：
    - 利率与收益率：Fed Rate, 2Y/10Y Treasury, Real Yield
    - 通胀指标：CPI, PCE
    - 就业：Unemployment, NFP
    - 产出：GDP
    - 货币：M2, Fed Balance Sheet, Global Liquidity
    - 汇率：DXY
    """

    @abstractmethod
    async def get_dxy(self, date: datetime | None = None) -> FetchResult:
        """美元指数 (DXY)。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_fed_rate(self, date: datetime | None = None) -> FetchResult:
        """联邦基金利率。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_treasury_yield(
        self, maturity: str = "10y", date: datetime | None = None
    ) -> FetchResult:
        """国债收益率。

        Args:
            maturity: 期限 (2y / 5y / 10y / 30y)
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_real_yield(self, date: datetime | None = None) -> FetchResult:
        """实际收益率（TIPS）。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_cpi(self, date: datetime | None = None) -> FetchResult:
        """CPI 消费者物价指数。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_pce(self, date: datetime | None = None) -> FetchResult:
        """PCE 个人消费支出。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_unemployment(self, date: datetime | None = None) -> FetchResult:
        """失业率。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_nfp(self, date: datetime | None = None) -> FetchResult:
        """非农就业人数。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_gdp(self, date: datetime | None = None) -> FetchResult:
        """GDP 国内生产总值。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_m2(self, date: datetime | None = None) -> FetchResult:
        """M2 货币供应量。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_fed_balance_sheet(self, date: datetime | None = None) -> FetchResult:
        """美联储资产负债表。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_global_liquidity(self, date: datetime | None = None) -> FetchResult:
        """全球流动性指数。

        Args:
            date: 查询日期

        Returns:
            FetchResult[data=MacroDataPoint]
        """
        ...

    @abstractmethod
    async def get_macro_series(
        self, indicator: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取宏观指标历史序列。

        Args:
            indicator: 指标名称（如 "cpi", "fed_rate", "dxy"）
            start: 起始时间
            end: 结束时间

        Returns:
            FetchResult[data=list[MacroDataPoint]]
        """
        ...


__all__ = ["BaseMacroProvider"]
