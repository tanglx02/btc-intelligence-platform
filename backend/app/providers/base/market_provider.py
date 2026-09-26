"""BaseMarketProvider — 市场行情 Provider 抽象基类。

定义所有市场行情数据源的统一接口，包括：
- 当前价格、OHLCV K线、成交量、市值
- 订单簿、价差、CVD、历史最高价
- 批量历史数据获取
"""

from abc import abstractmethod
from datetime import datetime

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseMarketProvider(BaseProvider):
    """市场行情 Provider 基类。

    所有市场行情数据源（Binance、Coinbase、OKX 等）必须继承此类
    并实现全部抽象方法。

    数据覆盖：
    - BTC 现货价格（实时/历史）
    - OHLCV K线（1m / 5m / 15m / 1h / 4h / 1d / 1w）
    - 24h 成交量与市值
    - 订单簿深度与买卖价差
    - 累计成交量差（CVD）
    - 历史最高价（ATH）
    """

    @abstractmethod
    async def get_current_price(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取当前价格。

        Args:
            symbol: 交易对，如 "BTC/USDT"

        Returns:
            FetchResult[data=PriceData] 包含 symbol, price, bid, ask, timestamp, source
        """
        ...

    @abstractmethod
    async def get_ohlcv(
        self,
        symbol: str = "BTC/USDT",
        interval: str = "1h",
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
    ) -> FetchResult:
        """获取 K 线数据。

        Args:
            symbol: 交易对
            interval: K线间隔 (1m/5m/15m/1h/4h/1d/1w)
            start: 起始时间（UTC）
            end: 结束时间（UTC）
            limit: 最大返回条数

        Returns:
            FetchResult[data=list[Candle]] 每根K线包含 open/high/low/close/volume
        """
        ...

    @abstractmethod
    async def get_24h_stats(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取 24 小时统计数据。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=dict] 包含 price_change, volume, high, low 等
        """
        ...

    @abstractmethod
    async def get_volume(
        self, symbol: str = "BTC/USDT", interval: str = "24h"
    ) -> FetchResult:
        """获取成交量。

        Args:
            symbol: 交易对
            interval: 统计间隔

        Returns:
            FetchResult[data=VolumeData]
        """
        ...

    @abstractmethod
    async def get_market_cap(self, symbol: str = "BTC") -> FetchResult:
        """获取市值。

        Args:
            symbol: 资产符号

        Returns:
            FetchResult[data=MarketCapData]
        """
        ...

    @abstractmethod
    async def get_ath(self, symbol: str = "BTC") -> FetchResult:
        """获取历史最高价。

        Args:
            symbol: 资产符号

        Returns:
            FetchResult[data=ATHData] 包含 ath_price, ath_date, drawdown_percent
        """
        ...

    @abstractmethod
    async def get_order_book(
        self, symbol: str = "BTC/USDT", depth: int = 20
    ) -> FetchResult:
        """获取订单簿深度。

        Args:
            symbol: 交易对
            depth: 买卖各多少档

        Returns:
            FetchResult[data=OrderBookData] 包含 bids, asks, timestamp
        """
        ...

    @abstractmethod
    async def get_recent_trades(self, symbol: str = "BTC/USDT", limit: int = 50) -> FetchResult:
        """获取最近成交记录。

        Args:
            symbol: 交易对
            limit: 返回条数

        Returns:
            FetchResult[data=list[Trade]]
        """
        ...

    @abstractmethod
    async def get_spread(self, symbol: str = "BTC/USDT") -> FetchResult:
        """获取买卖价差。

        Args:
            symbol: 交易对

        Returns:
            FetchResult[data=SpreadData] 包含 bid, ask, spread, spread_percent
        """
        ...

    @abstractmethod
    async def get_cvd(
        self, symbol: str = "BTC/USDT", interval: str = "1h"
    ) -> FetchResult:
        """获取累计成交量差 (Cumulative Volume Delta)。

        Args:
            symbol: 交易对
            interval: 统计间隔

        Returns:
            FetchResult[data=CVDData]
        """
        ...

    @abstractmethod
    async def get_batch_ohlcv(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
    ) -> FetchResult:
        """批量获取历史 K 线数据（用于历史同步）。

        此方法可能需要内部分页请求以覆盖完整时间范围。

        Args:
            symbol: 交易对
            interval: K线间隔
            start: 起始时间
            end: 结束时间

        Returns:
            FetchResult[data=list[Candle]] 完整时间范围的 K 线数据
        """
        ...


__all__ = ["BaseMarketProvider"]
