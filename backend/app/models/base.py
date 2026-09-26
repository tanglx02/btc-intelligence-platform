"""SQLAlchemy ORM 基础声明与公共 Mixin。

所有模型均继承自此处的 Base，公共字段通过 Mixin 混入。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全局 ORM 声明基类。"""

    pass


class UUIDPrimaryKeyMixin:
    """UUID 主键 Mixin（用于单主键表）。"""

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=func.gen_random_uuid(),
        comment="主键 UUID",
    )


class TimestampMixin:
    """公共时间戳字段：created_at / updated_at。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )


class CreatedAtMixin:
    """仅 created_at 字段（用于只追加、不更新的表）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )


class DataSourceMixin:
    """数据源追溯公共字段（所有时序数据表必须包含）。

    对应 DDL 中 source_id / observation_time / fetch_time / quality_status。
    """

    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=False,
        comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="数据观测时间（业务时间）",
    )
    fetch_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="数据拉取时间（系统时间）",
    )
    quality_status: Mapped[str] = mapped_column(
        String(20),
        server_default="VERIFIED",
        nullable=False,
        comment="数据质量状态：VERIFIED / ESTIMATED / STALE / CONFLICT / INVALID",
    )
