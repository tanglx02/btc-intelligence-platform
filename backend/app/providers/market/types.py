"""市场行情标准化数据结构。

所有 Market Provider 在解析交易所原始响应后，必须转换为这里定义的
标准化 dataclass，再封装进 FetchResult.data 返回。字段命名与
``app.models.market`` ORM 模型保持对齐，便于 Service 层直接落库。

约定：
- 所有时间字段均为 UTC naive datetime
- 价格/数量统一使用 float（落库时由 Service 层转 Decimal）
- dataclass 均为 JSON 友好结构（asdict 后可直接序列化）
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class PriceData:
    """标准化价格快照。"""

    symbol: str                      # 标准化内部格式，如 "BTC/USDT"
    price: float                     # 最新成交价
    bid: float | None = None         # 买一价
    ask: float | None = None         # 卖一价
    timestamp: datetime | None = None  # 交易所观测时间（无则为抓取时间）
    source: str = ""                 # 数据来源 Provider 名称


@dataclass
class Candle:
    """标准化 K 线。"""

    timestamp: datetime              # K 线开始时间（UTC）
    open: float
    high: float
    low: float
    close: float
    volume: float                    # 基础资产成交量
    quote_volume: float | None = None  # 计价资产成交额
    trades: int | None = None        # 成交笔数
    taker_buy_volume: float | None = None
    taker_sell_volume: float | None = None
    vwap: float | None = None
    is_closed: bool = True           # 是否已收盘（False 表示进行中的 K 线）


@dataclass
class VolumeData:
    """标准化成交量数据。"""

    symbol: str
    base_volume: float               # 基础资产成交量（如 BTC 数量）
    quote_volume: float | None = None  # 计价资产成交额（如 USDT）
    interval: str = "24h"
    timestamp: datetime | None = None
    source: str = ""


@dataclass
class MarketCapData:
    """标准化市值数据。"""

    symbol: str                      # 资产符号，如 "BTC"
    price: float
    circulating_supply: float
    market_cap: float
    timestamp: datetime | None = None
    source: str = ""
    is_estimated: bool = True        # 是否为估算值（交易所无流通量接口时为 True）


@dataclass
class ATHData:
    """标准化历史最高价数据。"""

    symbol: str
    ath_price: float
    ath_date: datetime | None = None
    current_price: float | None = None
    drawdown_percent: float | None = None  # 距 ATH 回撤（负值，如 -35.2 表示 -35.2%）
    source: str = ""


@dataclass
class OrderBookData:
    """标准化订单簿快照。"""

    symbol: str
    bids: list[list[float]] = field(default_factory=list)  # [[price, qty], ...] 价格降序
    asks: list[list[float]] = field(default_factory=list)  # [[price, qty], ...] 价格升序
    timestamp: datetime | None = None
    source: str = ""
    last_update_id: str | None = None


@dataclass
class Trade:
    """标准化逐笔成交。"""

    trade_id: str
    price: float
    quantity: float
    timestamp: datetime | None = None
    side: str | None = None          # "buy" / "sell"（taker 方向，可能为 None）
    is_buyer_maker: bool | None = None
    quote_quantity: float | None = None


@dataclass
class SpreadData:
    """标准化买卖价差。"""

    symbol: str
    bid: float
    ask: float
    spread: float
    spread_percent: float            # 价差占中间价百分比（%）
    mid_price: float
    timestamp: datetime | None = None
    source: str = ""


@dataclass
class CVDData:
    """标准化累计成交量差（现货 CVD，基于逐笔成交聚合）。"""

    symbol: str
    cvd: float                       # 累计成交量差 = buy_volume - sell_volume
    buy_volume: float
    sell_volume: float
    trade_count: int = 0
    interval: str = "1h"
    start_time: datetime | None = None
    end_time: datetime | None = None
    source: str = ""


@dataclass
class FundingData:
    """标准化资金费率。"""

    symbol: str
    funding_rate: float
    mark_price: float | None = None
    index_price: float | None = None
    next_funding_time: datetime | None = None
    timestamp: datetime | None = None
    source: str = ""


@dataclass
class OpenInterestData:
    """标准化未平仓合约量。"""

    symbol: str
    open_interest: float             # 合约张数 / 币数量
    open_interest_value: float | None = None  # 名义价值（USD/USDT）
    timestamp: datetime | None = None
    source: str = ""


@dataclass
class LiquidationOrder:
    """标准化强平订单。"""

    symbol: str
    side: str                        # "buy" / "sell"
    price: float
    quantity: float
    timestamp: datetime | None = None


def to_dict(obj: Any) -> Any:
    """将标准化 dataclass 转为 dict（datetime 字段保持原样，序列化时由调用方处理）。"""
    return asdict(obj)


__all__ = [
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
    "to_dict",
]
