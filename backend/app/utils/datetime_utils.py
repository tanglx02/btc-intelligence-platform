"""datetime 工具函数。

项目统一的 UTC 时间获取入口：返回带时区的 aware datetime
（``datetime.now(timezone.utc)``），替代已弃用的 ``datetime.utcnow()``
（Python 3.12+ DeprecationWarning，且返回 naive datetime）。

PostgreSQL TIMESTAMPTZ 列与项目内部时间比较均应使用 aware datetime。
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """返回当前 UTC 时间的 aware datetime（tzinfo=UTC）。

    替代弃用的 ``datetime.utcnow()``。
    """
    return datetime.now(UTC)


def as_naive_utc(dt: datetime) -> datetime:
    """aware datetime 转 naive UTC（naive 原样返回，视为 UTC）。

    Provider 内部日频数据（ETF 日期 / 恐惧贪婪指数日期等）以 naive UTC
    表示；调用方（Service / 引擎）可能传入 aware datetime，比较前统一
    去掉 tzinfo，避免 naive/aware 混算抛出
    ``TypeError: can't compare offset-naive and offset-aware datetimes``。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


__all__ = ["as_naive_utc", "utcnow"]
