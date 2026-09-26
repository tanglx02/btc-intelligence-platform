"""BaseOnChainProvider — 链上数据 Provider 抽象基类。

定义所有链上数据源的统一接口，包括：
- MVRV, Realized Cap, SOPR, NUPL, Puell Multiple
- RHODL, Reserve Risk, Active Addresses, HODL Waves
- 交易所资金流（Netflow, Inflow, Outflow）
"""

from abc import abstractmethod
from datetime import datetime

from app.providers.base.provider import BaseProvider
from app.providers.base.types import FetchResult


class BaseOnChainProvider(BaseProvider):
    """链上数据 Provider 基类。

    所有链上数据源（Glassnode、CryptoQuant、IntoTheBlock 等）必须继承此类。

    数据覆盖：
    - 估值指标：MVRV, Realized Cap, SOPR, NUPL, Puell Multiple
    - 持有者行为：RHODL, HODL Waves, LTH/STH Supply, Coin Days Destroyed
    - 网络活动：Active Addresses, New Addresses, Transaction Count
    - 风险指标：Reserve Risk, Realized Profit/Loss
    """

    @abstractmethod
    async def get_mvrv(self, date: datetime | None = None) -> FetchResult:
        """MVRV Ratio（市值/已实现市值比）。"""
        ...

    @abstractmethod
    async def get_realized_cap(self, date: datetime | None = None) -> FetchResult:
        """已实现市值。"""
        ...

    @abstractmethod
    async def get_sopr(self, date: datetime | None = None) -> FetchResult:
        """SOPR（花费产出利润率）。"""
        ...

    @abstractmethod
    async def get_asopr(self, date: datetime | None = None) -> FetchResult:
        """调整后 SOPR。"""
        ...

    @abstractmethod
    async def get_lth_sopr(self, date: datetime | None = None) -> FetchResult:
        """长期持有者 SOPR。"""
        ...

    @abstractmethod
    async def get_nupl(self, date: datetime | None = None) -> FetchResult:
        """NUPL（未实现净利润率）。"""
        ...

    @abstractmethod
    async def get_puell_multiple(self, date: datetime | None = None) -> FetchResult:
        """Puell Multiple（矿工收入倍数）。"""
        ...

    @abstractmethod
    async def get_rhodl(self, date: datetime | None = None) -> FetchResult:
        """RHODL Ratio。"""
        ...

    @abstractmethod
    async def get_reserve_risk(self, date: datetime | None = None) -> FetchResult:
        """Reserve Risk（储备风险）。"""
        ...

    @abstractmethod
    async def get_realized_profit_loss(self, date: datetime | None = None) -> FetchResult:
        """已实现盈亏。"""
        ...

    @abstractmethod
    async def get_supply_by_holder(self, date: datetime | None = None) -> FetchResult:
        """LTH/STH Supply 分布。"""
        ...

    @abstractmethod
    async def get_active_addresses(self, date: datetime | None = None) -> FetchResult:
        """活跃地址数。"""
        ...

    @abstractmethod
    async def get_new_addresses(self, date: datetime | None = None) -> FetchResult:
        """新增地址数。"""
        ...

    @abstractmethod
    async def get_transaction_count(self, date: datetime | None = None) -> FetchResult:
        """链上交易笔数。"""
        ...

    @abstractmethod
    async def get_hodl_waves(self, date: datetime | None = None) -> FetchResult:
        """HODL Waves 持币时间分布。"""
        ...

    @abstractmethod
    async def get_coin_days_destroyed(self, date: datetime | None = None) -> FetchResult:
        """币天销毁 (CDD)。"""
        ...

    @abstractmethod
    async def get_dormancy(self, date: datetime | None = None) -> FetchResult:
        """休眠度。"""
        ...

    @abstractmethod
    async def get_exchange_reserve(self, date: datetime | None = None) -> FetchResult:
        """交易所 BTC 储备量。"""
        ...

    @abstractmethod
    async def get_netflow(self, date: datetime | None = None) -> FetchResult:
        """交易所净流入/流出。"""
        ...

    @abstractmethod
    async def get_inflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流入量。"""
        ...

    @abstractmethod
    async def get_outflow(self, date: datetime | None = None) -> FetchResult:
        """交易所流出量。"""
        ...

    @abstractmethod
    async def get_metric_history(
        self, metric: str, start: datetime, end: datetime
    ) -> FetchResult:
        """批量获取某链上指标的历史序列。

        Args:
            metric: 指标名称（如 "mvrv", "sopr"）
            start: 起始时间
            end: 结束时间

        Returns:
            FetchResult[data=list[OnChainMetric]]
        """
        ...


__all__ = ["BaseOnChainProvider"]
