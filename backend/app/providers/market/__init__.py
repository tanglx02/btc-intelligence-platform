"""市场行情 Provider 实现（market 类别）。

按优先级排序的多交易所行情数据源，全部继承 BaseMarketProvider：

- BinanceProvider  (优先级 1) : 现货 + 合约公共接口（资金费率/未平仓/强平）
- OKXProvider      (优先级 2) : 现货 + 永续公共接口（资金费率/未平仓）
- CoinbaseProvider (优先级 3) : Exchange 现货（合规标杆，USD 计价）
- BybitProvider    (优先级 4) : V5 现货 + 线性合约（资金费率/未平仓/强平）
- KrakenProvider   (优先级 5) : 现货（XBT 计价符号体系）

辅助模块：
- symbol_mapper : 交易对格式统一映射（内部规范 "BTC/USDT"）
- types         : 标准化数据结构（PriceData / Candle / OrderBookData 等）

ProviderRegistry.auto_discover 会自动扫描本包并按类名注册
（BinanceProvider -> binance，与 config/providers.yaml 的键对应）。
"""

from app.providers.market.binance import BinanceProvider
from app.providers.market.bybit import BybitProvider
from app.providers.market.coinbase import CoinbaseProvider
from app.providers.market.kraken import KrakenProvider
from app.providers.market.okx import OKXProvider
from app.providers.market.symbol_mapper import (
    normalize_symbol,
    split_symbol,
    to_binance,
    to_bybit,
    to_coinbase,
    to_compact,
    to_kraken,
    to_okx,
)
from app.providers.market.types import (
    ATHData,
    Candle,
    CVDData,
    FundingData,
    LiquidationOrder,
    MarketCapData,
    OpenInterestData,
    OrderBookData,
    PriceData,
    SpreadData,
    Trade,
    VolumeData,
)

__all__ = [
    # providers
    "BinanceProvider",
    "OKXProvider",
    "CoinbaseProvider",
    "BybitProvider",
    "KrakenProvider",
    # symbol mapper
    "normalize_symbol",
    "split_symbol",
    "to_binance",
    "to_bybit",
    "to_coinbase",
    "to_compact",
    "to_kraken",
    "to_okx",
    # types
    "ATHData",
    "CVDData",
    "Candle",
    "FundingData",
    "LiquidationOrder",
    "MarketCapData",
    "OpenInterestData",
    "OrderBookData",
    "PriceData",
    "SpreadData",
    "Trade",
    "VolumeData",
]
