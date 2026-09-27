"""Market Provider 内部通用辅助函数（包内私有模块，不对外导出）。

提供各交易所 Provider 共享的：
- 数值/时间解析工具（容错处理 None、空串、非法值）
- FetchResult 构造助手（成功/失败统一封装）
- K 线数据基础校验
"""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.providers.base.types import ErrorType, FetchResult, QualityStatus

# BTC 流通量估算值（交易所 API 不提供流通量，市值 = 价格 × 流通量）。
# 说明：这是可配置的近似常量，精确链上流通量由 onchain 类 Provider 提供，
# 基于该值计算的市值一律标记 quality_status=ESTIMATED。
BTC_CIRCULATING_SUPPLY = 19_900_000.0

# 标准 K 线间隔 -> 秒数（用于分页、CVD 窗口、批量拉取的游标计算）
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def safe_float(value: Any, default: float | None = None) -> float | None:
    """容错地将任意值转为 float。

    Args:
        value: 原始值（str / int / float / None）
        default: 转换失败时的默认值

    Returns:
        转换后的 float，失败返回 default
    """
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float("inf"), float("-inf")):  # NaN/Inf
        return default
    return result


def safe_int(value: Any, default: int | None = None) -> int | None:
    """容错地将任意值转为 int。"""
    f = safe_float(value)
    if f is None:
        return default
    return int(f)


def ms_to_datetime(ms: Any) -> datetime | None:
    """毫秒时间戳 -> UTC naive datetime。"""
    value = safe_float(ms)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def sec_to_datetime(sec: Any) -> datetime | None:
    """秒时间戳 -> UTC naive datetime。"""
    value = safe_float(sec)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def as_naive_utc(dt: datetime) -> datetime:
    """aware datetime 转 naive UTC（naive 原样返回，视为 UTC）。

    用于 Provider 内部与 API 解析产出的 naive candle timestamp 比较：
    调用方（Service/Sync）可能传入 aware datetime，比较前统一去掉
    tzinfo，避免 naive/aware 混算 TypeError。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def datetime_to_ms(dt: datetime) -> int:
    """UTC datetime -> 毫秒时间戳（naive datetime 视为 UTC）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def datetime_to_sec(dt: datetime) -> int:
    """UTC datetime -> 秒时间戳（naive datetime 视为 UTC）。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp())


def parse_iso_datetime(value: Any) -> datetime | None:
    """解析 ISO8601 字符串（兼容尾部 Z）-> UTC naive datetime。"""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def utcnow() -> datetime:
    """当前 UTC naive 时间。"""
    return datetime.now(UTC).replace(tzinfo=None)


def interval_seconds(interval: str) -> int | None:
    """标准间隔字符串 -> 秒数，未知间隔返回 None。"""
    return INTERVAL_SECONDS.get(interval.lower())


def align_to_interval(dt: datetime, interval: str) -> datetime:
    """将时间向下对齐到 K 线间隔边界。"""
    seconds = interval_seconds(interval)
    if not seconds:
        return dt
    epoch = int(dt.replace(tzinfo=UTC).timestamp())
    aligned = epoch - (epoch % seconds)
    return datetime.fromtimestamp(aligned, tz=UTC).replace(tzinfo=None)


def validate_candles(candles: list[Any]) -> str | None:
    """校验标准化 K 线列表的基本完整性。

    Args:
        candles: Candle 对象列表

    Returns:
        None 表示校验通过，否则返回错误描述
    """
    if not candles:
        return "K线数据为空"
    for i, c in enumerate(candles):
        if c.open is None or c.high is None or c.low is None or c.close is None:
            return f"第 {i} 根K线 OHLC 字段缺失"
        if c.high < c.low:
            return f"第 {i} 根K线 high({c.high}) < low({c.low})"
        if c.volume is None or c.volume < 0:
            return f"第 {i} 根K线 volume 非法: {c.volume}"
    return None


def parse_error_result(
    provider_name: str,
    detail: str,
    *,
    error_type: ErrorType = ErrorType.DATA_FORMAT,
    raw: Any = None,
    response_time_ms: float = 0.0,
) -> FetchResult:
    """构造响应解析失败的 FetchResult。"""
    return FetchResult(
        success=False,
        error=f"Response parse error: {detail}",
        error_type=error_type,
        provider_name=provider_name,
        fetch_time=utcnow(),
        quality_status=QualityStatus.INVALID,
        raw_response=raw,
        response_time_ms=response_time_ms,
    )


def data_error_result(
    provider_name: str,
    detail: str,
    *,
    raw: Any = None,
    response_time_ms: float = 0.0,
) -> FetchResult:
    """构造数据校验失败（非空/类型/完整性）的 FetchResult。"""
    return FetchResult(
        success=False,
        error=f"Data validation error: {detail}",
        error_type=ErrorType.DATA_QUALITY,
        provider_name=provider_name,
        fetch_time=utcnow(),
        quality_status=QualityStatus.INVALID,
        raw_response=raw,
        response_time_ms=response_time_ms,
    )


def empty_result(
    provider_name: str,
    detail: str = "Empty response",
    *,
    raw: Any = None,
    response_time_ms: float = 0.0,
) -> FetchResult:
    """构造空响应的 FetchResult。"""
    return FetchResult(
        success=False,
        error=detail,
        error_type=ErrorType.EMPTY_RESPONSE,
        provider_name=provider_name,
        fetch_time=utcnow(),
        quality_status=QualityStatus.INVALID,
        raw_response=raw,
        response_time_ms=response_time_ms,
    )


def success_result(
    raw_result: FetchResult,
    data: Any,
    *,
    observation_time: datetime | None = None,
    quality_status: QualityStatus = QualityStatus.VERIFIED,
    metadata: dict[str, Any] | None = None,
) -> FetchResult:
    """从底层 _request() 的 FetchResult 构造标准化成功结果。

    透传 status_code / response_time_ms / provider_name / raw_response，
    data 替换为标准化后的数据结构。

    Args:
        raw_result: 底层 HTTP 请求返回的 FetchResult（success=True）
        data: 标准化后的数据（types.py 中的 dataclass / list / dict）
        observation_time: 数据观测时间
        quality_status: 数据质量状态
        metadata: 扩展元信息
    """
    return FetchResult(
        success=True,
        data=data,
        status_code=raw_result.status_code,
        response_time_ms=raw_result.response_time_ms,
        provider_name=raw_result.provider_name,
        fetch_time=raw_result.fetch_time or utcnow(),
        observation_time=observation_time,
        quality_status=quality_status,
        raw_response=raw_result.raw_response,
        metadata=dict(metadata or {}),
    )


def window_start(interval: str) -> timedelta:
    """将间隔字符串转为时间窗口长度（用于 CVD 等聚合），未知间隔默认 1h。"""
    return timedelta(seconds=interval_seconds(interval) or 3600)


__all__ = [
    "BTC_CIRCULATING_SUPPLY",
    "INTERVAL_SECONDS",
    "align_to_interval",
    "as_naive_utc",
    "data_error_result",
    "datetime_to_ms",
    "datetime_to_sec",
    "empty_result",
    "interval_seconds",
    "ms_to_datetime",
    "parse_error_result",
    "parse_iso_datetime",
    "safe_float",
    "safe_int",
    "sec_to_datetime",
    "success_result",
    "utcnow",
    "validate_candles",
    "window_start",
]
