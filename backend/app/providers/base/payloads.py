"""Provider 层标准化数据载荷（payload）构造助手。

所有 Provider 方法的 FetchResult.data 统一使用本模块构造的 dict 结构，
字段命名与 docs/architecture/04-provider-architecture.md 中定义的数据类型对齐：

- onchain_payload       -> OnChainMetric
- exchange_flow_payload -> ExchangeFlowData
- etf_flow_payload      -> ETFFlowData
- etf_holdings_payload  -> ETFHoldingsData
- derivative_payload    -> FundingData / OpenInterestData / ...
- options_payload       -> OptionsOIData / IVData / ...
- macro_payload         -> MacroDataPoint
- sentiment_payload     -> SentimentData

使用 dict 而非 dataclass 的原因：
1. 可直接 JSON 序列化，Redis 缓存 / API 响应无需二次转换
2. Service 层可直接映射到 ORM 模型字段
3. 可选字段缺失时不强制填充 None，载荷更紧凑
"""

from datetime import UTC, datetime
from typing import Any

from app.providers.base.types import ErrorType, FetchResult, QualityStatus
from app.utils.datetime_utils import utcnow

# ---- 失败结果构造 ----


def unsupported_result(provider_name: str, metric: str) -> FetchResult:
    """构造“该数据源不支持此指标”的失败结果（不触发无意义的 Failover 重试日志）。"""
    return FetchResult(
        success=False,
        error=f"{provider_name} does not support metric '{metric}'",
        error_type=ErrorType.DATA_FORMAT,
        provider_name=provider_name,
        metadata={"unsupported": True},
    )


def auth_required_result(provider_name: str, detail: str = "") -> FetchResult:
    """构造“API Key 未配置或无权限”的失败结果。"""
    return FetchResult(
        success=False,
        error=detail or f"{provider_name} requires a valid API key / subscription tier",
        error_type=ErrorType.AUTH_ERROR,
        provider_name=provider_name,
        quality_status=QualityStatus.INVALID,
    )


# ---- 时间工具 ----


def utc_now() -> datetime:
    """当前 UTC 时间（aware，与项目其余部分保持一致）。"""
    return utcnow()


def from_unix(ts: int | float | None) -> datetime | None:
    """Unix 时间戳（秒）转 naive UTC datetime。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(float(ts), tz=UTC).replace(tzinfo=None)


def to_unix(dt: datetime) -> int:
    """naive UTC datetime 转 Unix 时间戳（秒）。"""
    return int(dt.replace(tzinfo=UTC).timestamp())


def to_iso(dt: datetime | None) -> str | None:
    """datetime 转 ISO 字符串（None 安全）。"""
    return dt.isoformat() if dt else None


# ---- 链上数据 ----


def onchain_payload(
    metric: str,
    value: float,
    observation_time: datetime | None,
    *,
    unit: str = "ratio",
    resolution: str = "1d",
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造链上指标标准载荷（对应 OnChainMetric）。"""
    payload: dict[str, Any] = {
        "metric_name": metric,
        "value": value,
        "observation_time": to_iso(observation_time),
        "unit": unit,
        "resolution": resolution,
        "source": source,
    }
    if extra:
        payload["metadata"] = extra
    return payload


def onchain_series_payload(
    metric: str,
    points: list[dict[str, Any]],
    *,
    unit: str = "ratio",
    resolution: str = "1d",
    source: str = "",
) -> dict[str, Any]:
    """构造链上指标历史序列载荷。"""
    return {
        "metric_name": metric,
        "unit": unit,
        "resolution": resolution,
        "source": source,
        "count": len(points),
        "points": points,
    }


def exchange_flow_payload(
    flow_type: str,
    amount_btc: float | None = None,
    amount_usd: float | None = None,
    *,
    exchange: str | None = None,
    observation_time: datetime | None = None,
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造交易所资金流标准载荷（对应 ExchangeFlowData）。

    flow_type: inflow / outflow / netflow / reserve / miner_flow / whale_activity
    """
    payload: dict[str, Any] = {
        "flow_type": flow_type,
        "exchange": exchange or "ALL",
        "amount_btc": amount_btc,
        "amount_usd": amount_usd,
        "observation_time": to_iso(observation_time),
        "source": source,
    }
    if extra:
        payload["metadata"] = extra
    return payload


# ---- ETF 数据 ----


def etf_flow_payload(
    ticker: str,
    flow_type: str,
    amount_usd: float,
    *,
    date: datetime | None = None,
    period: str | None = None,
    amount_btc: float | None = None,
    fund_name: str | None = None,
    cumulative_usd: float | None = None,
    source: str = "",
) -> dict[str, Any]:
    """构造 ETF 资金流标准载荷（对应 ETFFlowData）。

    flow_type: daily_inflow / daily_outflow / net / cumulative
    """
    payload: dict[str, Any] = {
        "ticker": ticker,
        "fund_name": fund_name,
        "flow_type": flow_type,
        "amount_usd": amount_usd,
        "amount_btc": amount_btc,
        "cumulative_flow_usd": cumulative_usd,
        "date": to_iso(date),
        "period": period,
        "source": source,
    }
    return payload


def etf_holdings_payload(
    ticker: str,
    *,
    holdings_btc: float | None = None,
    holdings_usd: float | None = None,
    aum_usd: float | None = None,
    nav: float | None = None,
    price: float | None = None,
    fund_name: str | None = None,
    date: datetime | None = None,
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 ETF 持仓标准载荷（对应 ETFHoldingsData）。"""
    payload: dict[str, Any] = {
        "ticker": ticker,
        "fund_name": fund_name,
        "holdings_btc": holdings_btc,
        "holdings_usd": holdings_usd,
        "total_aum_usd": aum_usd,
        "nav": nav,
        "price": price,
        "date": to_iso(date),
        "source": source,
    }
    if extra:
        payload["metadata"] = extra
    return payload


# ---- 衍生品数据 ----


def derivative_payload(
    data_type: str,
    symbol: str,
    *,
    timestamp: datetime | None = None,
    source: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """构造衍生品标准载荷（对应 FundingData / OpenInterestData / LiquidationData 等）。

    data_type: funding / open_interest / liquidation / long_short_ratio /
               basis / premium / taker / cvd
    其余字段以关键字参数传入（如 funding_rate=0.0001, open_interest=12345.6）。
    """
    payload: dict[str, Any] = {
        "data_type": data_type,
        "symbol": symbol,
        "timestamp": to_iso(timestamp),
        "source": source,
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


# ---- 期权数据 ----


def options_payload(
    data_type: str,
    symbol: str,
    *,
    timestamp: datetime | None = None,
    source: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """构造期权标准载荷（对应 OptionsOIData / IVData / PutCallData 等）。

    data_type: oi / volume / implied_volatility / put_call_ratio / skew /
               gamma_exposure / expiration
    """
    payload: dict[str, Any] = {
        "data_type": data_type,
        "symbol": symbol,
        "timestamp": to_iso(timestamp),
        "source": source,
    }
    payload.update({k: v for k, v in fields.items() if v is not None})
    return payload


# ---- 宏观数据 ----


def macro_payload(
    indicator: str,
    value: float,
    observation_date: datetime,
    *,
    release_date: datetime | None = None,
    revision_date: datetime | None = None,
    unit: str = "",
    frequency: str | None = None,
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造宏观数据点标准载荷（对应 MacroDataPoint）。

    必须区分 observation_date（数据所属期）与 release_date（首次发布日期），
    防止回测时发生 Look-ahead Bias。
    """
    payload: dict[str, Any] = {
        "indicator": indicator,
        "value": value,
        "observation_date": to_iso(observation_date),
        "release_date": to_iso(release_date),
        "revision_date": to_iso(revision_date),
        "unit": unit,
        "frequency": frequency,
        "source": source,
    }
    if extra:
        payload["metadata"] = extra
    return payload


# ---- 情绪数据 ----


def sentiment_payload(
    indicator: str,
    value: float,
    *,
    label: str | None = None,
    timestamp: datetime | None = None,
    source: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造情绪数据标准载荷（对应 SentimentData）。

    indicator: fear_greed / social_score / social_volume / news_score
    """
    payload: dict[str, Any] = {
        "indicator": indicator,
        "value": value,
        "label": label,
        "timestamp": to_iso(timestamp),
        "source": source,
    }
    if extra:
        payload["metadata"] = extra
    return payload


__all__ = [
    "auth_required_result",
    "derivative_payload",
    "etf_flow_payload",
    "etf_holdings_payload",
    "exchange_flow_payload",
    "from_unix",
    "macro_payload",
    "onchain_payload",
    "onchain_series_payload",
    "options_payload",
    "sentiment_payload",
    "to_iso",
    "to_unix",
    "unsupported_result",
    "utc_now",
]
