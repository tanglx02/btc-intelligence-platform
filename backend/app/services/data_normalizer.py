"""数据标准化管道 — 将异构 Provider 原始响应统一为标准化记录。

对应架构文档：
- 《11-historical-data.md》§2.3 Normalized Data 表设计（统一化四要素）
- 《11-historical-data.md》§6 数据版本管理（rule_version）

统一化四要素：

| 要素 | 本模块实现 |
|---|---|
| 统一字段名 | ``FIELD_ALIASES`` + Provider 级 ``NormalizationRule`` 覆盖映射 |
| 统一单位   | ``UnitTransform`` 换算（satoshi→BTC、wei→ETH、ratio↔percent、金额→USD） |
| 统一时间格式 | ``parse_timestamp`` → UTC ``datetime``（对应 TIMESTAMPTZ） |
| 统一精度   | ``quantize`` → ``Decimal``（BTC 8 位 / USD 2 位 / 比率 10 位），禁止 float 入库 |

设计要点：
- **规则配置化**：每个 Provider 拥有独立的字段映射规则（``NormalizationRule``），
  以声明式数据结构表达，可序列化为 JSON 快照持久化到 ``normalization_rule_versions``；
- **规则版本化**：每条产出记录携带 ``rule_version``，规则升级后可对 Raw 数据重新标准化；
- **绝不生成假数据**：解析失败的字段保持 ``None``，绝不用 0 / 默认值冒充真实观测值；
  整条记录无法解析时计入 ``NormalizationResult.errors`` 并跳过。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Iterable, Sequence
from uuid import UUID

from loguru import logger

from app.services.data_store import (
    PRECISION_BTC,
    PRECISION_RATIO,
    PRECISION_USD,
    PRECISION_VOLUME,
    CandleRecord,
    DerivativeRecord,
    ETFRecord,
    MacroRecord,
    NormalizedRecord,
    OnChainRecord,
    PriceRecord,
    SentimentRecord,
    provider_id_resolver,
)

#: 当前标准化规则版本（语义化版本，每次规则变更必须递增）
NORMALIZATION_RULE_VERSION = "v1.0.0"

#: 尚未解析 source_id 时使用的占位 UUID（全 0），写入前必须被替换
NIL_UUID = UUID("00000000-0000-0000-0000-000000000000")


# ==========================================================================
# 时间解析
# ==========================================================================

#: 毫秒级时间戳阈值：大于此值视为毫秒（2001-09-09 之后的秒级时间戳均小于它）
_EPOCH_MS_THRESHOLD = 1e11
#: 微秒级时间戳阈值
_EPOCH_US_THRESHOLD = 1e14

_ISO_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_timestamp(value: Any) -> datetime | None:
    """将任意时间表示统一解析为 UTC 带时区 ``datetime``。

    支持：秒/毫秒/微秒级 epoch 整数与浮点、ISO-8601 字符串、纯日期字符串、
    ``datetime`` / ``date`` 对象。

    Args:
        value: 原始时间值

    Returns:
        UTC 带时区 datetime；无法解析时返回 ``None``（绝不猜测、绝不兜底为当前时间）
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return _to_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)

    # 数值型 epoch
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _epoch_to_utc(Decimal(str(value)))

    if isinstance(value, Decimal):
        return _epoch_to_utc(value)

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # 纯数字字符串 → epoch
        if re.fullmatch(r"-?\d+(\.\d+)?", text):
            try:
                return _epoch_to_utc(Decimal(text))
            except (InvalidOperation, ValueError, OSError, OverflowError):
                return None
        # 纯日期
        if _ISO_DATE_ONLY.match(text):
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
            return parsed.replace(tzinfo=timezone.utc)
        # ISO-8601（兼容尾部 Z）
        iso_text = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        try:
            parsed = datetime.fromisoformat(iso_text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%d %b %Y", "%b %d, %Y"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                logger.debug(f"无法解析时间字符串: {text!r}")
                return None
        return _to_utc(parsed)

    return None


def _epoch_to_utc(epoch: Decimal) -> datetime | None:
    """将 epoch 数值（自动识别秒/毫秒/微秒）转为 UTC datetime。"""
    try:
        magnitude = float(epoch)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if magnitude == 0:
        return None
    try:
        if abs(magnitude) >= _EPOCH_US_THRESHOLD:
            seconds = magnitude / 1_000_000
        elif abs(magnitude) >= _EPOCH_MS_THRESHOLD:
            seconds = magnitude / 1_000
        else:
            seconds = magnitude
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        logger.debug(f"epoch 数值超出可表示范围: {epoch}")
        return None


def _to_utc(value: datetime) -> datetime:
    """将 datetime 归一化为 UTC；naive 值按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ==========================================================================
# 数值与单位转换
# ==========================================================================

#: 单位换算因子表
UNIT_FACTORS: dict[str, Decimal] = {
    "satoshi_to_btc": Decimal("0.00000001"),
    "wei_to_eth": Decimal("0.000000000000000001"),
    "milli_to_unit": Decimal("0.001"),
    "micro_to_unit": Decimal("0.000001"),
    "percent_to_ratio": Decimal("0.01"),
    "ratio_to_percent": Decimal("100"),
    "thousand_to_unit": Decimal("1000"),
    "million_to_unit": Decimal("1000000"),
    "billion_to_unit": Decimal("1000000000"),
    "usdt_to_usd": Decimal("1"),
}


def to_decimal(value: Any, *, precision: Decimal | None = None) -> Decimal | None:
    """将任意值安全转换为 Decimal 并按精度量化。

    Args:
        value: 原始值（str / int / float / Decimal）
        precision: 量化精度（如 ``PRECISION_BTC``）；None 表示不量化

    Returns:
        Decimal 值；无法转换时返回 ``None``
    """
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # float 必须先经 str 转换，避免二进制浮点误差进入 Decimal
        try:
            result = Decimal(repr(value))
        except (InvalidOperation, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip().replace(",", "").replace("%", "")
        if not text or text.lower() in {"null", "none", "nan", "n/a", "-", ""}:
            return None
        try:
            result = Decimal(text)
        except (InvalidOperation, ValueError):
            return None
    else:
        return None

    if not result.is_finite():
        return None
    return quantize(result, precision)


def quantize(value: Decimal, precision: Decimal | None) -> Decimal:
    """按指定精度量化 Decimal（ROUND_HALF_UP，与金融口径一致）。"""
    if precision is None:
        return value
    try:
        return value.quantize(precision, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return value


def apply_unit_factor(value: Decimal, transform: str | None) -> Decimal:
    """应用单位换算因子。

    Args:
        value: 原始数值
        transform: ``UNIT_FACTORS`` 中的换算名称；None / 未知名称时原样返回

    Returns:
        换算后的数值
    """
    if not transform:
        return value
    factor = UNIT_FACTORS.get(transform)
    if factor is None:
        logger.debug(f"未知单位换算规则: {transform}")
        return value
    return value * factor


def to_int(value: Any) -> int | None:
    """将任意值安全转换为 int。"""
    decimal_value = to_decimal(value)
    if decimal_value is None:
        return None
    try:
        return int(decimal_value.to_integral_value(rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return None


# ==========================================================================
# 字段别名与符号标准化
# ==========================================================================

#: 统一字段名 ← 各 Provider 常见字段别名（按优先级排序，首个命中生效）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # 价格
    "price": ("lastPrice", "last_price", "last", "price", "close", "c", "mark_price",
              "markPrice", "index_price", "indexPrice", "v", "value", "y", "usd", "px"),
    "bid": ("bidPrice", "bid_price", "bid", "bestBid", "best_bid", "b"),
    "ask": ("askPrice", "ask_price", "ask", "bestAsk", "best_ask", "a"),
    "high_24h": ("highPrice", "high_price", "high24h", "high_24h", "h", "high24Hour"),
    "low_24h": ("lowPrice", "low_price", "low24h", "low_24h", "l", "low24Hour"),
    "volume_24h": ("volume", "vol", "volume24h", "volume_24h", "v", "baseVolume", "qty"),
    "quote_volume_24h": ("quoteVolume", "quote_volume", "quoteVolume24h", "turnover",
                         "volumeUsd", "volume_usd", "amount"),
    "price_change_pct_24h": ("priceChangePercent", "price_change_percent", "change24h",
                             "changePercent24h", "change_pct_24h", "priceChangePct", "change24Hour"),
    "market_cap": ("marketCap", "market_cap", "marketCapUsd", "mc"),
    # K 线
    "open": ("open", "o", "openPrice", "open_price"),
    "high": ("high", "h", "highPrice", "high_price"),
    "low": ("low", "l", "lowPrice", "low_price"),
    "close": ("close", "c", "closePrice", "close_price"),
    "trades": ("trades", "n", "trade_count", "count", "num_trades"),
    "taker_buy_volume": ("takerBuyBaseAssetVolume", "taker_buy_volume", "takerBuyVolume",
                         "buyVol", "buy_vol"),
    "taker_sell_volume": ("takerSellBaseAssetVolume", "taker_sell_volume", "takerSellVolume",
                          "sellVol", "sell_vol"),
    # 通用标识
    "symbol": ("symbol", "pair", "instrument", "market", "contract", "instId", "product_id"),
    "timestamp": ("time", "timestamp", "ts", "t", "E", "openTime", "open_time", "datetime",
                  "date", "day", "created_at", "createdAt", "event_time", "fundingTime"),
    # 链上
    "onchain_value": ("v", "value", "val", "amount", "data", "y", "metric_value"),
    "metric_name": ("metric", "metric_name", "name", "indicator", "id", "series"),
    # ETF
    "ticker": ("ticker", "symbol", "fund", "etf", "code", "fundTicker"),
    "net_flow_usd": ("net_flow_usd", "netFlow", "net_flow", "daily_net_flow", "flow_usd",
                     "totalNetFlow", "netFlowUsd"),
    "total_holdings_btc": ("total_holdings_btc", "holdings_btc", "btcHoldings", "totalBtc",
                           "holdingsBtc"),
    "total_aum_usd": ("total_aum_usd", "aum", "totalAum", "assetsUsd", "nav"),
    # 衍生品
    "funding_rate": ("fundingRate", "funding_rate", "rate", "funding", "currentFundingRate"),
    "funding_rate_next": ("nextFundingRate", "funding_rate_next", "predictedFundingRate"),
    "open_interest": ("openInterest", "open_interest", "oi", "sumOpenInterest", "oiQty"),
    "open_interest_usd": ("openInterestUsd", "open_interest_usd", "oiUsd", "sumOpenInterestValue"),
    "long_short_ratio": ("longShortRatio", "long_short_ratio", "lsr", "longShortAccountRatio"),
    "liquidation_long_usd": ("longLiquidationUsd", "liquidation_long_usd", "buyVolUsd",
                             "longLiqUsd"),
    "liquidation_short_usd": ("shortLiquidationUsd", "liquidation_short_usd", "sellVolUsd",
                              "shortLiqUsd"),
    "exchange": ("exchange", "exchangeName", "exchange_name", "venue", "market"),
    # 宏观
    "series_id": ("series_id", "seriesId", "seriesid", "id", "symbol", "metric"),
    "release_date": ("release_date", "releaseDate", "published", "updated", "realtime_start"),
    # 情绪
    "sentiment_value": ("value", "score", "index", "v", "fear_greed", "value_score"),
    "sentiment_label": ("value_classification", "classification", "label", "sentiment"),
}

#: 类别 → 通用时间字段候选（Provider 规则可覆盖）
TIME_FIELD_CANDIDATES: tuple[str, ...] = FIELD_ALIASES["timestamp"]

#: 符号标准化分隔符
_SYMBOL_SEPARATORS = re.compile(r"[\s\-_/:.]")


def normalize_symbol(symbol: Any, default: str = "BTCUSDT") -> str:
    """统一交易对符号表示（``BTC/USDT`` / ``btc-usdt`` → ``BTCUSDT``）。

    Args:
        symbol: 原始符号
        default: 无法解析时的默认符号

    Returns:
        大写、无分隔符的符号字符串
    """
    if symbol is None:
        return default
    text = str(symbol).strip().upper()
    if not text:
        return default
    cleaned = _SYMBOL_SEPARATORS.sub("", text)
    return cleaned or default


def normalize_metric_name(name: Any, default: str = "UNKNOWN") -> str:
    """统一链上/情绪指标名称为大写下划线形式（``MVRV-Z`` → ``MVRV_Z``）。"""
    if name is None:
        return default
    text = str(name).strip()
    if not text:
        return default
    cleaned = re.sub(r"[\s\-/.]+", "_", text)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned.upper() or default


# ==========================================================================
# 规则声明结构
# ==========================================================================


@dataclass(slots=True)
class FieldRule:
    """单字段映射规则。

    Attributes:
        target: 目标统一字段名
        sources: 源字段候选路径（支持 ``a.b.c`` 嵌套与 ``a[0].b`` 下标）
        precision: 量化精度
        unit_transform: 单位换算名称（``UNIT_FACTORS`` 键）
        as_int: 是否转换为整数
        required: 是否为必需字段（缺失则整条记录作废）
    """

    target: str
    sources: tuple[str, ...] = ()
    precision: Decimal | None = None
    unit_transform: str | None = None
    as_int: bool = False
    required: bool = False


@dataclass(slots=True)
class NormalizationRule:
    """某 Provider 在某数据类别下的完整标准化规则（可序列化、可版本化）。

    Attributes:
        provider_name: Provider 名称，``"*"`` 表示通用兜底规则
        category: 数据类别（market / onchain / etf / derivatives / macro / sentiment）
        data_kind: 数据形态（price / ohlcv / metric / flow / funding / series / index）
        rule_version: 规则版本号
        description: 规则说明
        fields: 字段映射规则列表
        time_sources: 时间字段候选路径
        data_path: 数据点在响应中的路径（None 表示自动探测）
        symbol_default: 默认符号
        extra: 附加常量（写入记录 metadata）
    """

    provider_name: str = "*"
    category: str = "market"
    data_kind: str = "price"
    rule_version: str = NORMALIZATION_RULE_VERSION
    description: str = ""
    fields: tuple[FieldRule, ...] = ()
    time_sources: tuple[str, ...] = TIME_FIELD_CANDIDATES
    data_path: str | None = None
    symbol_default: str = "BTCUSDT"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_snapshot(self) -> dict[str, Any]:
        """序列化为 JSON 快照（供 ``normalization_rule_versions.rule_snapshot`` 落库）。"""
        return {
            "provider_name": self.provider_name,
            "category": self.category,
            "data_kind": self.data_kind,
            "rule_version": self.rule_version,
            "description": self.description,
            "time_sources": list(self.time_sources),
            "data_path": self.data_path,
            "symbol_default": self.symbol_default,
            "extra": self.extra,
            "fields": [
                {
                    "target": f.target,
                    "sources": list(f.sources),
                    "precision": str(f.precision) if f.precision is not None else None,
                    "unit_transform": f.unit_transform,
                    "as_int": f.as_int,
                    "required": f.required,
                }
                for f in self.fields
            ],
        }


# ==========================================================================
# 内置规则库
# ==========================================================================


def _price_fields() -> tuple[FieldRule, ...]:
    """构建通用价格字段规则。"""
    return (
        FieldRule("price", FIELD_ALIASES["price"], PRECISION_BTC, required=True),
        FieldRule("bid", FIELD_ALIASES["bid"], PRECISION_BTC),
        FieldRule("ask", FIELD_ALIASES["ask"], PRECISION_BTC),
        FieldRule("high_24h", FIELD_ALIASES["high_24h"], PRECISION_BTC),
        FieldRule("low_24h", FIELD_ALIASES["low_24h"], PRECISION_BTC),
        FieldRule("volume_24h", FIELD_ALIASES["volume_24h"], PRECISION_VOLUME),
        FieldRule("quote_volume_24h", FIELD_ALIASES["quote_volume_24h"], PRECISION_VOLUME),
        FieldRule("price_change_pct_24h", FIELD_ALIASES["price_change_pct_24h"], Decimal("0.0001")),
        FieldRule("market_cap", FIELD_ALIASES["market_cap"], PRECISION_USD),
        FieldRule("symbol", FIELD_ALIASES["symbol"]),
    )


def _ohlcv_fields() -> tuple[FieldRule, ...]:
    """构建通用 K 线字段规则。"""
    return (
        FieldRule("open", FIELD_ALIASES["open"], PRECISION_BTC, required=True),
        FieldRule("high", FIELD_ALIASES["high"], PRECISION_BTC, required=True),
        FieldRule("low", FIELD_ALIASES["low"], PRECISION_BTC, required=True),
        FieldRule("close", FIELD_ALIASES["close"], PRECISION_BTC, required=True),
        FieldRule("volume", FIELD_ALIASES["volume_24h"], PRECISION_VOLUME),
        FieldRule("quote_volume", FIELD_ALIASES["quote_volume_24h"], PRECISION_VOLUME),
        FieldRule("trades", FIELD_ALIASES["trades"], as_int=True),
        FieldRule("taker_buy_volume", FIELD_ALIASES["taker_buy_volume"], PRECISION_VOLUME),
        FieldRule("taker_sell_volume", FIELD_ALIASES["taker_sell_volume"], PRECISION_VOLUME),
        FieldRule("symbol", FIELD_ALIASES["symbol"]),
    )


#: 内置规则表：``(category, provider_name)`` → 规则列表（按 data_kind 区分）
_BUILTIN_RULES: dict[tuple[str, str], list[NormalizationRule]] = {
    # ---- 市场行情 ----
    ("market", "*"): [
        NormalizationRule(
            provider_name="*", category="market", data_kind="price",
            description="通用市场价格标准化规则（字段别名自动探测）",
            fields=_price_fields(),
        ),
        NormalizationRule(
            provider_name="*", category="market", data_kind="ohlcv",
            description="通用 K 线标准化规则（支持数组型与对象型 K 线）",
            fields=_ohlcv_fields(),
            time_sources=("openTime", "open_time", "time", "timestamp", "ts", "t", "datetime", "date"),
        ),
    ],
    ("market", "binance"): [
        NormalizationRule(
            provider_name="binance", category="market", data_kind="price",
            description="Binance 现货价格（lastPrice / bidPrice / askPrice）",
            fields=(
                FieldRule("price", ("lastPrice", "price", "last"), PRECISION_BTC, required=True),
                FieldRule("bid", ("bidPrice", "bid"), PRECISION_BTC),
                FieldRule("ask", ("askPrice", "ask"), PRECISION_BTC),
                FieldRule("high_24h", ("highPrice",), PRECISION_BTC),
                FieldRule("low_24h", ("lowPrice",), PRECISION_BTC),
                FieldRule("volume_24h", ("volume",), PRECISION_VOLUME),
                FieldRule("quote_volume_24h", ("quoteVolume",), PRECISION_VOLUME),
                FieldRule("price_change_pct_24h", ("priceChangePercent",), Decimal("0.0001")),
                FieldRule("symbol", ("symbol",)),
            ),
            time_sources=("closeTime", "E", "time", "timestamp"),
        ),
        NormalizationRule(
            provider_name="binance", category="market", data_kind="ohlcv",
            description="Binance K 线（数组格式：[openTime,o,h,l,c,vol,closeTime,quoteVol,n,takerBuy,takerSell]）",
            fields=_ohlcv_fields(),
            time_sources=("openTime",),
            extra={"array_layout": "binance_klines"},
        ),
    ],
    ("market", "okx"): [
        NormalizationRule(
            provider_name="okx", category="market", data_kind="price",
            description="OKX 行情（data[0].last / bidPx / askPx）",
            data_path="data.0",
            fields=(
                FieldRule("price", ("last",), PRECISION_BTC, required=True),
                FieldRule("bid", ("bidPx",), PRECISION_BTC),
                FieldRule("ask", ("askPx",), PRECISION_BTC),
                FieldRule("high_24h", ("high24h",), PRECISION_BTC),
                FieldRule("low_24h", ("low24h",), PRECISION_BTC),
                FieldRule("volume_24h", ("vol24h", "vol"), PRECISION_VOLUME),
                FieldRule("symbol", ("instId",)),
            ),
            time_sources=("ts", "uTime"),
        ),
        NormalizationRule(
            provider_name="okx", category="market", data_kind="ohlcv",
            description="OKX K 线（数组格式：[ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]）",
            fields=_ohlcv_fields(),
            data_path="data",
            extra={"array_layout": "okx_candles"},
        ),
    ],
    ("market", "coinbase"): [
        NormalizationRule(
            provider_name="coinbase", category="market", data_kind="price",
            description="Coinbase 价格（data.amount / price）",
            fields=(
                FieldRule("price", ("amount", "price", "last"), PRECISION_BTC, required=True),
                FieldRule("symbol", ("product_id", "pair"), ),
            ),
            data_path="data",
        ),
    ],
    ("market", "coingecko"): [
        NormalizationRule(
            provider_name="coingecko", category="market", data_kind="price",
            description="CoinGecko 价格（bitcoin.usd / market_data.current_price）",
            fields=(
                FieldRule("price", ("usd", "current_price"), PRECISION_BTC, required=True),
                FieldRule("high_24h", ("high_24h",), PRECISION_BTC),
                FieldRule("low_24h", ("low_24h",), PRECISION_BTC),
                FieldRule("volume_24h", ("total_volume",), PRECISION_VOLUME),
                FieldRule("market_cap", ("market_cap",), PRECISION_USD),
                FieldRule("price_change_pct_24h", ("price_change_percentage_24h",),
                          Decimal("0.0001")),
            ),
            data_path="market_data.current_price",
        ),
    ],
    # ---- 链上 ----
    ("onchain", "*"): [
        NormalizationRule(
            provider_name="*", category="onchain", data_kind="metric",
            description="通用链上指标标准化（[{t, v}] / [{date, value}] 自动探测）",
            fields=(
                FieldRule("value", FIELD_ALIASES["onchain_value"], PRECISION_RATIO, required=True),
                FieldRule("metric_name", FIELD_ALIASES["metric_name"]),
            ),
            time_sources=("t", "time", "timestamp", "date", "datetime", "d", "day", "ts"),
        ),
    ],
    ("onchain", "glassnode"): [
        NormalizationRule(
            provider_name="glassnode", category="onchain", data_kind="metric",
            description="Glassnode 指标时序（[{t: epoch_s, v: value}]）",
            fields=(FieldRule("value", ("v", "value"), PRECISION_RATIO, required=True),),
            time_sources=("t",),
        ),
    ],
    ("onchain", "cryptoquant"): [
        NormalizationRule(
            provider_name="cryptoquant", category="onchain", data_kind="metric",
            description="CryptoQuant 指标时序（data: [{date, value}]）",
            data_path="data",
            fields=(FieldRule("value", ("value", "v"), PRECISION_RATIO, required=True),),
            time_sources=("date", "datetime"),
        ),
    ],
    # ---- ETF ----
    ("etf", "*"): [
        NormalizationRule(
            provider_name="*", category="etf", data_kind="flow",
            description="通用 ETF 资金流标准化",
            fields=(
                FieldRule("ticker", FIELD_ALIASES["ticker"], required=True),
                FieldRule("net_flow_usd", FIELD_ALIASES["net_flow_usd"], PRECISION_USD),
                FieldRule("total_holdings_btc", FIELD_ALIASES["total_holdings_btc"], PRECISION_BTC),
                FieldRule("total_aum_usd", FIELD_ALIASES["total_aum_usd"], PRECISION_USD),
                FieldRule("daily_volume_usd", ("daily_volume_usd", "volume", "volumeUsd"),
                          PRECISION_USD),
                FieldRule("price", ("price", "close", "nav"), Decimal("0.0001")),
            ),
            time_sources=("date", "day", "time", "timestamp", "tradeDate"),
        ),
    ],
    # ---- 衍生品 ----
    ("derivatives", "*"): [
        NormalizationRule(
            provider_name="*", category="derivatives", data_kind="funding",
            description="通用衍生品数据标准化",
            fields=(
                FieldRule("exchange", FIELD_ALIASES["exchange"], required=True),
                FieldRule("symbol", FIELD_ALIASES["symbol"]),
                FieldRule("funding_rate", FIELD_ALIASES["funding_rate"], PRECISION_RATIO),
                FieldRule("funding_rate_next", FIELD_ALIASES["funding_rate_next"], PRECISION_RATIO),
                FieldRule("open_interest", FIELD_ALIASES["open_interest"], PRECISION_BTC),
                FieldRule("open_interest_usd", FIELD_ALIASES["open_interest_usd"], PRECISION_USD),
                FieldRule("long_short_ratio", FIELD_ALIASES["long_short_ratio"], Decimal("0.000001")),
                FieldRule("liquidation_long_usd", FIELD_ALIASES["liquidation_long_usd"],
                          PRECISION_USD),
                FieldRule("liquidation_short_usd", FIELD_ALIASES["liquidation_short_usd"],
                          PRECISION_USD),
            ),
        ),
    ],
    # ---- 宏观 ----
    ("macro", "*"): [
        NormalizationRule(
            provider_name="*", category="macro", data_kind="series",
            description="通用宏观时序标准化（严格保留 observation/release/revision 三日期）",
            fields=(
                FieldRule("series_id", FIELD_ALIASES["series_id"], required=True),
                FieldRule("value", ("value", "val", "y", "amount"), PRECISION_RATIO,
                          required=True),
            ),
            time_sources=("date", "observation_date", "period", "time", "timestamp"),
        ),
    ],
    # ---- 情绪 ----
    ("sentiment", "*"): [
        NormalizationRule(
            provider_name="*", category="sentiment", data_kind="index",
            description="通用情绪指标标准化",
            fields=(
                FieldRule("value", FIELD_ALIASES["sentiment_value"], Decimal("0.000001"),
                          required=True),
                FieldRule("sentiment_label", FIELD_ALIASES["sentiment_label"]),
            ),
            time_sources=("timestamp", "time", "date", "ts"),
        ),
    ],
}

#: 数组型 K 线布局：``layout`` → 下标到目标字段的映射
ARRAY_LAYOUTS: dict[str, dict[int, str]] = {
    # Binance: [openTime, open, high, low, close, volume, closeTime, quoteVol, trades, buy, sell, ignore]
    "binance_klines": {
        0: "observation_time", 1: "open", 2: "high", 3: "low", 4: "close",
        5: "volume", 7: "quote_volume", 8: "trades", 9: "taker_buy_volume",
        10: "taker_sell_volume",
    },
    # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
    "okx_candles": {
        0: "observation_time", 1: "open", 2: "high", 3: "low", 4: "close",
        5: "volume", 7: "quote_volume",
    },
    # CoinGecko market_chart: [ts, value]
    "pair_ts_value": {0: "observation_time", 1: "value"},
}


# ==========================================================================
# 标准化结果容器
# ==========================================================================


@dataclass
class NormalizationResult:
    """标准化产出容器。

    Attributes:
        prices: 价格记录
        candles: K 线记录
        onchain: 链上指标记录
        etf: ETF 记录
        derivatives: 衍生品记录
        macro: 宏观记录
        sentiment: 情绪记录
        errors: 解析失败明细（原因 + 原始点位摘要），失败绝不静默吞掉
        rule_version: 本次标准化使用的规则版本
        provider_name: 数据来源 Provider
        total_points: 输入数据点总数
    """

    prices: list[PriceRecord] = field(default_factory=list)
    candles: list[CandleRecord] = field(default_factory=list)
    onchain: list[OnChainRecord] = field(default_factory=list)
    etf: list[ETFRecord] = field(default_factory=list)
    derivatives: list[DerivativeRecord] = field(default_factory=list)
    macro: list[MacroRecord] = field(default_factory=list)
    sentiment: list[SentimentRecord] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    rule_version: str = NORMALIZATION_RULE_VERSION
    provider_name: str = ""
    total_points: int = 0

    @property
    def records(self) -> list[NormalizedRecord]:
        """返回全部标准化记录的扁平列表。"""
        return [
            *self.prices,
            *self.candles,
            *self.onchain,
            *self.etf,
            *self.derivatives,
            *self.macro,
            *self.sentiment,
        ]

    @property
    def record_count(self) -> int:
        """成功标准化的记录总数。"""
        return len(self.records)

    def is_empty(self) -> bool:
        """是否未产出任何记录。"""
        return self.record_count == 0

    def summary(self) -> dict[str, Any]:
        """生成摘要字典（用于日志与进度上报）。"""
        return {
            "provider": self.provider_name,
            "rule_version": self.rule_version,
            "total_points": self.total_points,
            "prices": len(self.prices),
            "candles": len(self.candles),
            "onchain": len(self.onchain),
            "etf": len(self.etf),
            "derivatives": len(self.derivatives),
            "macro": len(self.macro),
            "sentiment": len(self.sentiment),
            "errors": len(self.errors),
        }


# ==========================================================================
# DataNormalizer
# ==========================================================================


class DataNormalizer:
    """数据标准化管道。

    职责：把不同 Provider 的原始响应统一为 ``NormalizedRecord`` 子类实例，
    完成字段名、单位、时间格式、精度四项统一，并给每条记录打上 ``rule_version``。

    Usage::

        normalizer = DataNormalizer()
        result = normalizer.normalize_market_data(
            {"symbol": "BTCUSDT", "lastPrice": "108000.50"}, "binance", source_id=pid
        )
        await normalized_store.store_prices(result.prices)

        # 异步版本（自动解析 provider_name → source_id）
        result = await normalizer.anormalize_market_data(raw, "binance", session=db)
    """

    def __init__(
        self,
        extra_rules: Iterable[NormalizationRule] | None = None,
        rule_version: str = NORMALIZATION_RULE_VERSION,
    ) -> None:
        """初始化标准化器。

        Args:
            extra_rules: 追加的自定义规则（同键位覆盖内置规则）
            rule_version: 本次运行使用的规则版本号
        """
        self._rule_version = rule_version
        self._rules: dict[tuple[str, str, str], NormalizationRule] = {}
        for rules in _BUILTIN_RULES.values():
            for rule in rules:
                self._register(rule)
        for rule in extra_rules or ():
            self._register(rule)

    # ---- 规则管理 ----

    def _register(self, rule: NormalizationRule) -> None:
        self._rules[(rule.category, rule.provider_name, rule.data_kind)] = rule

    def register_rule(self, rule: NormalizationRule) -> None:
        """注册/覆盖一条标准化规则（配置化扩展入口）。"""
        self._register(rule)
        logger.info(
            f"注册标准化规则 category={rule.category} provider={rule.provider_name} "
            f"kind={rule.data_kind} version={rule.rule_version}"
        )

    def get_rule(self, category: str, provider_name: str, data_kind: str) -> NormalizationRule | None:
        """按 (类别, Provider, 数据形态) 获取规则，Provider 专属规则优先于通用规则。"""
        provider_key = (provider_name or "").lower()
        return (
            self._rules.get((category, provider_key, data_kind))
            or self._rules.get((category, "*", data_kind))
        )

    def rule_snapshot(self, category: str | None = None) -> dict[str, Any]:
        """导出规则快照（供 ``normalization_rule_versions`` 表落库、实现规则可复现）。"""
        snapshot: dict[str, Any] = {
            "rule_version": self._rule_version,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "categories": {},
        }
        for (rule_category, provider, kind), rule in sorted(self._rules.items()):
            if category and rule_category != category:
                continue
            bucket = snapshot["categories"].setdefault(rule_category, {})
            bucket.setdefault(provider, {})[kind] = rule.to_snapshot()
        return snapshot

    @property
    def rule_version(self) -> str:
        """当前规则版本号。"""
        return self._rule_version

    # ---- 市场数据 ----

    def normalize_market_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        symbol: str = "BTCUSDT",
        interval: str | None = None,
        data_kind: str | None = None,
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化市场数据（价格 / K 线自动识别）。

        Args:
            raw_response: Provider 原始响应体
            provider_name: Provider 名称（用于选择专属规则）
            source_id: 数据来源 UUID；None 时置为 ``NIL_UUID``，写入前需调用
                :func:`apply_source_id` 补齐
            symbol: 默认交易对符号（响应内若含符号字段则以响应为准）
            interval: K 线周期（``1m`` / ``1h`` / ``1d`` ...）
            data_kind: 强制指定数据形态（``price`` / ``ohlcv``）；None 时自动探测
            rule_version: 覆盖规则版本标记

        Returns:
            NormalizationResult
        """
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        if raw_response is None:
            return result

        kind = data_kind or self._detect_market_kind(raw_response)
        rule = self.get_rule("market", provider_name, kind)
        if rule is None:
            rule = NormalizationRule(category="market", data_kind=kind, rule_version=version)

        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        if kind == "ohlcv":
            for index, point in enumerate(points):
                record = self._build_candle(
                    point, rule, source_id, symbol, interval, version, index, result
                )
                if record is not None:
                    result.candles.append(record)
        else:
            for index, point in enumerate(points):
                record = self._build_price(
                    point, rule, source_id, symbol, version, index, result
                )
                if record is not None:
                    result.prices.append(record)

        self._log_summary("market", result)
        return result

    def normalize_ohlcv(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        symbol: str = "BTCUSDT",
        interval: str = "1h",
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化 K 线数据（显式指定 ohlcv 形态）。"""
        return self.normalize_market_data(
            raw_response,
            provider_name,
            source_id=source_id,
            symbol=symbol,
            interval=interval,
            data_kind="ohlcv",
            rule_version=rule_version,
        )

    # ---- 链上数据 ----

    def normalize_onchain_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        metric_name: str | None = None,
        unit: str = "ratio",
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化链上指标数据。

        Args:
            raw_response: 原始响应（支持 ``[{t, v}]`` / ``{"data": [...]}`` / 单点对象）
            provider_name: Provider 名称
            source_id: 数据来源 UUID
            metric_name: 指标名（响应内无指标名时使用此值）
            unit: 单位标注（ratio / usd / btc / count / percent）
            rule_version: 覆盖规则版本

        Returns:
            NormalizationResult
        """
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        rule = self.get_rule("onchain", provider_name, "metric") or NormalizationRule(
            category="onchain", data_kind="metric", rule_version=version
        )
        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        for index, point in enumerate(points):
            values = self._apply_field_rules(point, rule)
            observation_time = self._extract_time(point, rule)
            value = values.get("value")
            if observation_time is None:
                self._record_error(result, index, point, "无法解析观测时间")
                continue
            if value is None:
                self._record_error(result, index, point, "无法解析指标值")
                continue

            resolved_metric = normalize_metric_name(
                values.get("metric_name") or metric_name or self._infer_metric_name(point)
            )
            result.onchain.append(
                OnChainRecord(
                    source_id=source_id or NIL_UUID,
                    observation_time=observation_time,
                    metric_name=resolved_metric,
                    value=value,
                    unit=unit,
                    rule_version=version,
                    metadata={"provider": provider_name},
                )
            )

        self._log_summary("onchain", result)
        return result

    # ---- ETF / 衍生品 / 宏观 / 情绪 ----

    def normalize_etf_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        ticker: str | None = None,
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化 ETF 资金流数据。"""
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        rule = self.get_rule("etf", provider_name, "flow") or NormalizationRule(
            category="etf", data_kind="flow", rule_version=version
        )
        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        for index, point in enumerate(points):
            values = self._apply_field_rules(point, rule)
            observation_time = self._extract_time(point, rule)
            resolved_ticker = str(values.get("ticker") or ticker or "").strip().upper()
            if observation_time is None:
                self._record_error(result, index, point, "无法解析观测时间")
                continue
            if not resolved_ticker:
                self._record_error(result, index, point, "缺少 ETF ticker")
                continue

            result.etf.append(
                ETFRecord(
                    source_id=source_id or NIL_UUID,
                    observation_time=observation_time,
                    ticker=resolved_ticker,
                    fund_name=_as_str(values.get("fund_name")),
                    net_flow_usd=values.get("net_flow_usd"),
                    daily_inflow_usd=values.get("daily_inflow_usd"),
                    daily_outflow_usd=values.get("daily_outflow_usd"),
                    cumulative_flow_usd=values.get("cumulative_flow_usd"),
                    total_holdings_btc=values.get("total_holdings_btc"),
                    total_aum_usd=values.get("total_aum_usd"),
                    daily_volume_usd=values.get("daily_volume_usd"),
                    price=values.get("price"),
                    nav=values.get("nav"),
                    rule_version=version,
                    metadata={"provider": provider_name},
                )
            )

        self._log_summary("etf", result)
        return result

    def normalize_derivatives_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        symbol: str = "BTCUSDT",
        exchange: str | None = None,
        data_type: str = "FUNDING",
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化衍生品数据（资金费率 / OI / 清算 / 多空比）。"""
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        rule = self.get_rule("derivatives", provider_name, "funding") or NormalizationRule(
            category="derivatives", data_kind="funding", rule_version=version
        )
        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        for index, point in enumerate(points):
            values = self._apply_field_rules(point, rule)
            observation_time = self._extract_time(point, rule)
            resolved_exchange = str(values.get("exchange") or exchange or provider_name or "").strip()
            if observation_time is None:
                self._record_error(result, index, point, "无法解析观测时间")
                continue
            if not resolved_exchange:
                self._record_error(result, index, point, "缺少交易所标识")
                continue

            result.derivatives.append(
                DerivativeRecord(
                    source_id=source_id or NIL_UUID,
                    observation_time=observation_time,
                    symbol=normalize_symbol(values.get("symbol") or symbol, symbol),
                    exchange=resolved_exchange,
                    data_type=data_type,
                    funding_rate=values.get("funding_rate"),
                    funding_rate_next=values.get("funding_rate_next"),
                    funding_time=parse_timestamp(point_value(point, "nextFundingTime")),
                    open_interest=values.get("open_interest"),
                    open_interest_usd=values.get("open_interest_usd"),
                    long_short_ratio=values.get("long_short_ratio"),
                    long_account_ratio=values.get("long_account_ratio"),
                    liquidation_long_usd=values.get("liquidation_long_usd"),
                    liquidation_short_usd=values.get("liquidation_short_usd"),
                    liquidation_total_usd=_sum_optional(
                        values.get("liquidation_long_usd"),
                        values.get("liquidation_short_usd"),
                    ),
                    basis=values.get("basis"),
                    basis_pct=values.get("basis_pct"),
                    premium_index=values.get("premium_index"),
                    rule_version=version,
                    metadata={"provider": provider_name},
                )
            )

        self._log_summary("derivatives", result)
        return result

    def normalize_macro_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        series_id: str | None = None,
        series_name: str | None = None,
        unit: str = "index",
        frequency: str = "MONTHLY",
        release_date: date | None = None,
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化宏观数据（严格区分观测日期与发布日期，防止未来数据泄漏）。"""
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        rule = self.get_rule("macro", provider_name, "series") or NormalizationRule(
            category="macro", data_kind="series", rule_version=version
        )
        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        for index, point in enumerate(points):
            values = self._apply_field_rules(point, rule)
            observation_time = self._extract_time(point, rule)
            value = values.get("value")
            resolved_series = str(values.get("series_id") or series_id or "").strip().upper()
            if observation_time is None:
                self._record_error(result, index, point, "无法解析观测时间")
                continue
            if value is None:
                # 宏观数据允许缺失值（如当期未公布），跳过而非填 0
                self._record_error(result, index, point, "指标值为空（可能尚未公布）")
                continue
            if not resolved_series:
                self._record_error(result, index, point, "缺少 series_id")
                continue

            point_release = parse_timestamp(point_value(point, "release_date")) or parse_timestamp(
                point_value(point, "realtime_start")
            )
            result.macro.append(
                MacroRecord(
                    source_id=source_id or NIL_UUID,
                    observation_time=observation_time,
                    series_id=resolved_series,
                    series_name=series_name or resolved_series,
                    value=value,
                    unit=unit,
                    frequency=frequency,
                    observation_date=observation_time.date(),
                    release_date=(point_release or _as_datetime(release_date)).date()
                    if (point_release or release_date)
                    else None,
                    rule_version=version,
                    metadata={"provider": provider_name},
                )
            )

        self._log_summary("macro", result)
        return result

    def normalize_sentiment_data(
        self,
        raw_response: Any,
        provider_name: str,
        *,
        source_id: UUID | None = None,
        source_type: str = "FEAR_GREED",
        metric_name: str = "fear_greed_index",
        rule_version: str | None = None,
    ) -> NormalizationResult:
        """标准化情绪数据（单源为主，仅做范围检查，默认状态 ESTIMATED）。"""
        version = rule_version or self._rule_version
        result = NormalizationResult(rule_version=version, provider_name=provider_name)
        rule = self.get_rule("sentiment", provider_name, "index") or NormalizationRule(
            category="sentiment", data_kind="index", rule_version=version
        )
        points = self._extract_points(raw_response, rule)
        result.total_points = len(points)

        for index, point in enumerate(points):
            values = self._apply_field_rules(point, rule)
            observation_time = self._extract_time(point, rule)
            value = values.get("value")
            if observation_time is None:
                self._record_error(result, index, point, "无法解析观测时间")
                continue
            if value is None:
                self._record_error(result, index, point, "无法解析情绪指标值")
                continue

            label = _as_str(values.get("sentiment_label")) or _as_str(
                point_value(point, "value_classification")
            )
            result.sentiment.append(
                SentimentRecord(
                    source_id=source_id or NIL_UUID,
                    observation_time=observation_time,
                    source_type=source_type,
                    metric_name=metric_name,
                    value=value,
                    normalized_value=_normalize_sentiment_scale(value, metric_name),
                    sentiment_label=label.upper() if label else None,
                    sample_size=to_int(point_value(point, "sample_size")),
                    confidence=to_decimal(point_value(point, "confidence")),
                    rule_version=version,
                    metadata={"provider": provider_name},
                )
            )

        self._log_summary("sentiment", result)
        return result

    # ---- 通用分派 ----

    def normalize(
        self,
        category: str,
        raw_response: Any,
        provider_name: str,
        **kwargs: Any,
    ) -> NormalizationResult:
        """按数据类别分派到对应的标准化方法。

        Args:
            category: market / onchain / etf / derivatives / macro / sentiment
            raw_response: 原始响应体
            provider_name: Provider 名称
            **kwargs: 透传给具体标准化方法的关键字参数

        Returns:
            NormalizationResult

        Raises:
            ValueError: 未知数据类别
        """
        dispatch: dict[str, Callable[..., NormalizationResult]] = {
            "market": self.normalize_market_data,
            "onchain": self.normalize_onchain_data,
            "exchange_flow": self.normalize_onchain_data,
            "etf": self.normalize_etf_data,
            "derivatives": self.normalize_derivatives_data,
            "options": self.normalize_derivatives_data,
            "macro": self.normalize_macro_data,
            "sentiment": self.normalize_sentiment_data,
        }
        handler = dispatch.get(str(category).lower())
        if handler is None:
            raise ValueError(f"不支持标准化的数据类别: {category!r}")
        return handler(raw_response, provider_name, **kwargs)

    # ---- 异步包装（自动解析 source_id）----

    async def anormalize(
        self,
        category: str,
        raw_response: Any,
        provider_name: str,
        *,
        session: Any = None,
        **kwargs: Any,
    ) -> NormalizationResult:
        """异步标准化：自动把 ``provider_name`` 解析为数据库 ``source_id``。

        Args:
            category: 数据类别
            raw_response: 原始响应体
            provider_name: Provider 名称
            session: 数据库会话（None 时自建）
            **kwargs: 透传参数

        Returns:
            NormalizationResult（记录的 source_id 已填充；Provider 未注册时为 NIL_UUID）
        """
        source_id = kwargs.pop("source_id", None)
        if source_id is None:
            source_id = await self._resolve_source_id(provider_name, session)
        result = self.normalize(category, raw_response, provider_name, source_id=source_id, **kwargs)
        if source_id is None:
            logger.warning(
                f"Provider '{provider_name}' 未解析到 source_id，产出记录标记为 NIL_UUID，"
                "写入前必须补齐"
            )
        return result

    async def _resolve_source_id(self, provider_name: str, session: Any) -> UUID | None:
        """解析 Provider 名称为 UUID。"""
        if session is not None:
            return await provider_id_resolver.resolve(session, provider_name)
        from app.core.database import get_db_session_ctx

        try:
            async with get_db_session_ctx() as db:
                return await provider_id_resolver.resolve(db, provider_name)
        except Exception as exc:  # noqa: BLE001 - 数据库不可用时降级
            logger.warning(f"解析 Provider source_id 失败 ({provider_name}): {exc}")
            return None

    # ---- 内部实现 ----

    def _detect_market_kind(self, raw_response: Any) -> str:
        """自动探测市场数据形态（price / ohlcv）。"""
        points = _find_data_list(raw_response)
        if not points:
            return "price"
        first = points[0]
        if isinstance(first, list | tuple):
            return "ohlcv" if len(first) >= 5 else "price"
        if isinstance(first, dict):
            keys = {str(k).lower() for k in first}
            if {"open", "high", "low", "close"} & keys or {"o", "h", "l", "c"} <= keys:
                return "ohlcv"
        return "price"

    def _extract_points(self, raw_response: Any, rule: NormalizationRule) -> list[Any]:
        """从原始响应中抽取数据点列表。"""
        if rule.data_path:
            located = resolve_path(raw_response, rule.data_path)
            if located is not None:
                if isinstance(located, list):
                    return list(located)
                return [located]

        points = _find_data_list(raw_response)
        if points:
            return points

        # 单点对象（如 ticker 快照）
        if isinstance(raw_response, dict):
            return [raw_response]
        if raw_response is not None:
            return [raw_response]
        return []

    def _extract_time(self, point: Any, rule: NormalizationRule) -> datetime | None:
        """按规则候选路径解析观测时间。"""
        if isinstance(point, dict):
            for source in rule.time_sources:
                parsed = parse_timestamp(resolve_path(point, source))
                if parsed is not None:
                    return parsed
            return None
        if isinstance(point, list | tuple):
            layout = rule.extra.get("array_layout")
            mapping = ARRAY_LAYOUTS.get(layout or "", {})
            for index, target in mapping.items():
                if target == "observation_time" and index < len(point):
                    return parse_timestamp(point[index])
            # 无布局声明时，首元素若像时间戳则采用
            if point:
                return parse_timestamp(point[0])
        return None

    def _apply_field_rules(self, point: Any, rule: NormalizationRule) -> dict[str, Any]:
        """对单个数据点应用全部字段映射规则，返回统一字段名 → 值。"""
        values: dict[str, Any] = {}

        # 数组型布局：按下标直接映射
        if isinstance(point, list | tuple):
            layout = rule.extra.get("array_layout")
            mapping = ARRAY_LAYOUTS.get(layout or "")
            if not mapping:
                # 无声明布局时按通用 K 线顺序 [ts,o,h,l,c,vol] 推断
                mapping = {0: "observation_time", 1: "open", 2: "high", 3: "low",
                           4: "close", 5: "volume"}
            rule_by_target = {f.target: f for f in rule.fields}
            for index, target in mapping.items():
                if index >= len(point) or target == "observation_time":
                    continue
                field_rule = rule_by_target.get(target)
                raw_value = point[index]
                if field_rule is not None:
                    converted = self._convert(raw_value, field_rule)
                    if converted is not None:
                        values[target] = converted
                elif target == "trades":
                    values[target] = to_int(raw_value)
                else:
                    decimal_value = to_decimal(raw_value)
                    if decimal_value is not None:
                        values[target] = decimal_value
            return values

        # 字典型：按候选路径逐一探测
        for field_rule in rule.fields:
            sources = field_rule.sources or FIELD_ALIASES.get(field_rule.target, (field_rule.target,))
            for source in sources:
                if source not in point and "." not in source and "[" not in source:
                    continue
                raw_value = resolve_path(point, source)
                if raw_value is None or raw_value == "":
                    continue
                converted = self._convert(raw_value, field_rule)
                if converted is not None:
                    values[field_rule.target] = converted
                    break
        return values

    @staticmethod
    def _convert(raw_value: Any, field_rule: FieldRule) -> Any:
        """按字段规则转换单个原始值。"""
        if field_rule.as_int:
            return to_int(raw_value)
        if field_rule.target in {"symbol", "ticker", "exchange", "metric_name",
                                 "series_id", "sentiment_label", "fund_name"}:
            return _as_str(raw_value)
        decimal_value = to_decimal(raw_value)
        if decimal_value is None:
            return None
        decimal_value = apply_unit_factor(decimal_value, field_rule.unit_transform)
        return quantize(decimal_value, field_rule.precision)

    def _build_price(
        self,
        point: Any,
        rule: NormalizationRule,
        source_id: UUID | None,
        default_symbol: str,
        version: str,
        index: int,
        result: NormalizationResult,
    ) -> PriceRecord | None:
        """从数据点构造 PriceRecord。"""
        values = self._apply_field_rules(point, rule)
        price = values.get("price")
        if price is None:
            self._record_error(result, index, point, "无法解析价格字段")
            return None

        observation_time = self._extract_time(point, rule)
        if observation_time is None:
            # 实时快照没有观测时间时，以抓取时刻为准（这是真实抓取时间，非伪造）
            observation_time = datetime.now(timezone.utc)

        return PriceRecord(
            source_id=source_id or NIL_UUID,
            observation_time=observation_time,
            symbol=normalize_symbol(values.get("symbol"), default_symbol),
            price=price,
            bid=values.get("bid"),
            ask=values.get("ask"),
            high_24h=values.get("high_24h"),
            low_24h=values.get("low_24h"),
            volume_24h=values.get("volume_24h"),
            quote_volume_24h=values.get("quote_volume_24h"),
            price_change_pct_24h=values.get("price_change_pct_24h"),
            market_cap=values.get("market_cap"),
            rule_version=version,
            metadata={"provider": rule.provider_name},
        )

    def _build_candle(
        self,
        point: Any,
        rule: NormalizationRule,
        source_id: UUID | None,
        default_symbol: str,
        interval: str | None,
        version: str,
        index: int,
        result: NormalizationResult,
    ) -> CandleRecord | None:
        """从数据点构造 CandleRecord。"""
        values = self._apply_field_rules(point, rule)
        missing = [k for k in ("open", "high", "low", "close") if values.get(k) is None]
        if missing:
            self._record_error(result, index, point, f"K 线缺少必需字段: {missing}")
            return None

        observation_time = self._extract_time(point, rule)
        if observation_time is None:
            self._record_error(result, index, point, "K 线无法解析开盘时间")
            return None

        resolved_interval = _as_str(values.get("interval")) or interval or "1h"
        try:
            candle_interval = _to_candle_interval(resolved_interval)
        except ValueError as exc:
            self._record_error(result, index, point, str(exc))
            return None

        return CandleRecord(
            source_id=source_id or NIL_UUID,
            observation_time=observation_time,
            symbol=normalize_symbol(values.get("symbol"), default_symbol),
            interval=candle_interval,
            open=values["open"],
            high=values["high"],
            low=values["low"],
            close=values["close"],
            volume=values.get("volume") or Decimal("0"),
            quote_volume=values.get("quote_volume"),
            trades=values.get("trades"),
            taker_buy_volume=values.get("taker_buy_volume"),
            taker_sell_volume=values.get("taker_sell_volume"),
            rule_version=version,
            metadata={"provider": rule.provider_name},
        )

    @staticmethod
    def _record_error(
        result: NormalizationResult, index: int, point: Any, reason: str
    ) -> None:
        """记录解析失败明细（异常不允许静默吞掉）。"""
        result.errors.append({"index": index, "reason": reason, "point": _brief(point)})
        if len(result.errors) <= 5:
            logger.debug(f"标准化跳过数据点 #{index}: {reason}")

    @staticmethod
    def _log_summary(category: str, result: NormalizationResult) -> None:
        if result.errors:
            logger.warning(
                f"[{category}] 标准化完成 provider={result.provider_name} "
                f"成功={result.record_count}/{result.total_points} "
                f"失败={len(result.errors)} rule_version={result.rule_version} "
                f"首个错误={result.errors[0]['reason']}"
            )
        else:
            logger.debug(
                f"[{category}] 标准化完成 provider={result.provider_name} "
                f"records={result.record_count} rule_version={result.rule_version}"
            )

    @staticmethod
    def _infer_metric_name(point: Any) -> str:
        """从数据点推断指标名（用于响应内嵌指标名的场景）。"""
        if isinstance(point, dict):
            for key in ("metric", "name", "indicator", "id", "series"):
                value = point.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return "UNKNOWN"


# ==========================================================================
# 路径解析与工具函数
# ==========================================================================

_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_path(data: Any, path: str) -> Any:
    """按点/下标路径从嵌套结构中取值。

    支持 ``"data.0.last"``、``"market_data.current_price.usd"``、``"items[0].v"``。

    Args:
        data: 原始数据结构
        path: 路径表达式

    Returns:
        命中的值；路径不存在时返回 ``None``
    """
    if not path:
        return None
    current = data
    for match in _PATH_TOKEN.finditer(path):
        key, index = match.group(1), match.group(2)
        if index is not None:
            position = int(index)
            if isinstance(current, Sequence) and not isinstance(current, str | bytes):
                if position >= len(current):
                    return None
                current = current[position]
            else:
                return None
        else:
            if isinstance(current, dict):
                if key not in current:
                    # 大小写不敏感兜底匹配
                    lowered = {str(k).lower(): v for k, v in current.items()}
                    if key.lower() not in lowered:
                        return None
                    current = lowered[key.lower()]
                else:
                    current = current[key]
            elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
                try:
                    current = current[int(key)]
                except (ValueError, IndexError):
                    return None
            else:
                return None
    return current


def point_value(point: Any, *names: str) -> Any:
    """从数据点中按候选名取第一个非空值。"""
    if not isinstance(point, dict):
        return None
    for name in names:
        value = resolve_path(point, name)
        if value is not None and value != "":
            return value
    return None


#: 常见的数据点列表容器键（按优先级探测）
_DATA_LIST_KEYS: tuple[str, ...] = (
    "data", "result", "results", "candles", "values", "prices", "series",
    "observations", "items", "records", "history", "rates", "list", "rows",
    "market_chart", "total", "flows", "metrics",
)


def _find_data_list(raw_response: Any) -> list[Any]:
    """从各种响应封装形态中定位数据点列表。"""
    if isinstance(raw_response, list):
        return list(raw_response)
    if not isinstance(raw_response, dict):
        return []

    for key in _DATA_LIST_KEYS:
        value = raw_response.get(key)
        if isinstance(value, list) and value:
            return list(value)
        if isinstance(value, dict):
            # CoinGecko market_chart: {"prices": [[ts, v], ...]}
            nested = _find_data_list(value)
            if nested:
                return nested

    # 顶层即为单点数据（ticker 快照）
    return []


def _as_str(value: Any) -> str | None:
    """转为去空白字符串；空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool | int | float | Decimal):
        return str(value)
    return None


def _as_datetime(value: Any) -> datetime:
    """将任意值转为 datetime（None 时返回当前 UTC 时间）。"""
    parsed = parse_timestamp(value)
    return parsed or datetime.now(timezone.utc)


def _sum_optional(*values: Decimal | None) -> Decimal | None:
    """对可空 Decimal 求和；全部为 None 时返回 None（绝不返回 0 冒充真实值）。"""
    present = [v for v in values if v is not None]
    if not present:
        return None
    total = Decimal("0")
    for value in present:
        total += value
    return total


def _normalize_sentiment_scale(value: Decimal, metric_name: str) -> Decimal | None:
    """将情绪指标映射到 0-100 统一量纲。"""
    if value is None:
        return None
    name = metric_name.lower()
    if "fear" in name or "greed" in name:
        return quantize(value, Decimal("0.0001")) if Decimal("0") <= value <= Decimal("100") else None
    if value <= Decimal("1"):
        return quantize(value * Decimal("100"), Decimal("0.0001"))
    return quantize(value, Decimal("0.0001"))


_INTERVAL_ALIASES: dict[str, str] = {
    "1m": "1m", "1min": "1m", "m1": "1m", "60": "1m", "60s": "1m", "1minute": "1m",
    "5m": "5m", "5min": "5m", "m5": "5m", "300": "5m", "5minute": "5m",
    "15m": "15m", "15min": "15m", "m15": "15m", "900": "15m",
    "30m": "30m", "30min": "30m", "m30": "30m", "1800": "30m", "1h30m": "30m",
    "1h": "1h", "60m": "1h", "h1": "1h", "3600": "1h", "1hour": "1h", "60min": "1h",
    "4h": "4h", "240m": "4h", "h4": "4h", "14400": "4h", "4hour": "4h",
    "1d": "1d", "1D": "1d", "d1": "1d", "day": "1d", "daily": "1d", "86400": "1d",
    "24h": "1d", "1day": "1d",
    "1w": "1w", "1W": "1w", "w1": "1w", "week": "1w", "weekly": "1w", "7d": "1w",
    "1M": "1w",
}


def _to_candle_interval(value: Any) -> Any:
    """将各 Provider 的周期表示统一为 ``CandleInterval`` 枚举。

    Raises:
        ValueError: 不支持的周期
    """
    from app.models.enums import CandleInterval

    if isinstance(value, CandleInterval):
        return value
    text = str(value).strip()
    canonical = _INTERVAL_ALIASES.get(text) or _INTERVAL_ALIASES.get(text.lower())
    if canonical is None:
        raise ValueError(f"不支持的 K 线周期: {value!r}")
    try:
        return CandleInterval(canonical)
    except ValueError as exc:
        raise ValueError(f"不支持的 K 线周期: {value!r}") from exc


def _brief(point: Any, limit: int = 200) -> str:
    """生成数据点的简短摘要（用于错误日志，避免打印超长响应）。"""
    text = repr(point)
    return text if len(text) <= limit else text[:limit] + "..."


def apply_source_id(
    records: Iterable[NormalizedRecord], source_id: UUID
) -> int:
    """批量为记录填充 ``source_id``（标准化与落库解耦时使用）。

    Args:
        records: 记录集合
        source_id: 数据来源 Provider UUID

    Returns:
        实际更新的记录数
    """
    count = 0
    for record in records:
        if record.source_id != source_id:
            record.source_id = source_id
            count += 1
    return count


# ---- 模块级便捷单例 ----

_normalizer: DataNormalizer | None = None


def get_data_normalizer() -> DataNormalizer:
    """获取全局 DataNormalizer 单例。"""
    global _normalizer
    if _normalizer is None:
        _normalizer = DataNormalizer()
    return _normalizer


def set_data_normalizer(normalizer: DataNormalizer | None) -> None:
    """设置/重置全局 DataNormalizer 单例（主要用于测试与规则热更新）。"""
    global _normalizer
    _normalizer = normalizer


__all__ = [
    "FIELD_ALIASES",
    "NIL_UUID",
    "NORMALIZATION_RULE_VERSION",
    "UNIT_FACTORS",
    "DataNormalizer",
    "FieldRule",
    "NormalizationResult",
    "NormalizationRule",
    "apply_source_id",
    "get_data_normalizer",
    "normalize_metric_name",
    "normalize_symbol",
    "parse_timestamp",
    "quantize",
    "resolve_path",
    "set_data_normalizer",
    "to_decimal",
    "to_int",
]
