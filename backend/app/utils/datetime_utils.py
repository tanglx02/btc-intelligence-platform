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


__all__ = ["utcnow"]
