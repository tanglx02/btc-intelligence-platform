"""模型管理 ORM 模型。

包含：model_versions, model_weights, model_validations
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, CreatedAtMixin, TimestampMixin
from .enums import ModelStatus, model_status_enum


class ModelVersion(Base, TimestampMixin):
    """模型版本注册表（不可覆盖，只能新建）。"""

    __tablename__ = "model_versions"
    __table_args__ = (
        UniqueConstraint("model_name", "version", name="uq_model_versions_name_version"),
        Index("idx_model_versions_name", "model_name", "status"),
        Index("idx_model_versions_production", "model_name", postgresql_where=text("is_production = true")),
        {"comment": "模型版本注册表（不可覆盖，只能新建）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, comment="模型名称")
    version: Mapped[str] = mapped_column(String(50), nullable=False, comment="版本号")
    model_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="模型类型：CYCLE / VALUATION / RISK / REGIME / PREDICTION / ENSEMBLE",
    )
    description: Mapped[Optional[str]] = mapped_column(Text, comment="英文描述")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    # 架构信息
    architecture: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="模型架构")
    hyperparameters: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="超参数")
    training_config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="训练配置")
    features_used: Mapped[Optional[list]] = mapped_column(ARRAY(Text), server_default=text("'{}'"), comment="使用的特征列表")
    # 训练/测试区间
    train_start_date: Mapped[date] = mapped_column(Date, nullable=False, comment="训练开始日期")
    train_end_date: Mapped[date] = mapped_column(Date, nullable=False, comment="训练结束日期")
    test_start_date: Mapped[Optional[date]] = mapped_column(Date, comment="测试开始日期")
    test_end_date: Mapped[Optional[date]] = mapped_column(Date, comment="测试结束日期")
    validation_type: Mapped[Optional[str]] = mapped_column(
        String(30), server_default=text("'WALK_FORWARD'"),
        comment="验证方式：IN_SAMPLE / OUT_OF_SAMPLE / WALK_FORWARD / CROSS_VALIDATION",
    )
    # 状态
    status: Mapped[ModelStatus] = mapped_column(
        model_status_enum, nullable=False, server_default=text("'DRAFT'"), comment="模型状态",
    )
    is_production: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否为生产版本")
    # 性能指标
    performance_metrics: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="性能指标 JSON")
    known_limitations: Mapped[Optional[str]] = mapped_column(Text, comment="已知局限性")
    effective_conditions: Mapped[Optional[str]] = mapped_column(Text, comment="模型有效条件描述")
    # 元数据
    parent_version_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id"), comment="父版本（版本谱系追踪）",
    )
    created_by: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id"), comment="创建者用户 ID",
    )
    changelog: Mapped[Optional[str]] = mapped_column(Text, comment="变更日志")

    # Relationships
    weights: Mapped[list["ModelWeight"]] = relationship(back_populates="model_version", lazy="selectin")
    validations: Mapped[list["ModelValidation"]] = relationship(back_populates="model_version", lazy="selectin")
    parent_version: Mapped[Optional["ModelVersion"]] = relationship(remote_side=[id], lazy="joined")


class ModelWeight(Base, CreatedAtMixin):
    """模型参数/权重存储。"""

    __tablename__ = "model_weights"
    __table_args__ = (
        UniqueConstraint("model_version_id", "weight_name", name="uq_model_weights_version_name"),
        Index("idx_model_weights_version", "model_version_id"),
        {"comment": "模型参数/权重存储"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    model_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="CASCADE"),
        nullable=False, comment="模型版本 ID",
    )
    weight_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="权重名称")
    weight_type: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default=text("'PARAMETER'"),
        comment="权重类型：PARAMETER / NEURAL_WEIGHT / COEFFICIENT / THRESHOLD",
    )
    # 存储（二选一）
    weight_blob: Mapped[Optional[bytes]] = mapped_column(LargeBinary, comment="小权重直接存储（<10MB）")
    storage_path: Mapped[Optional[str]] = mapped_column(String(500), comment="大权重文件存储路径")
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer, comment="文件大小（字节）")
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64), comment="SHA256 校验和")
    # 元数据
    weight_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="权重元数据")
    shape: Mapped[Optional[list]] = mapped_column(ARRAY(Integer), server_default=text("'{}'"), comment="张量形状")
    dtype: Mapped[Optional[str]] = mapped_column(String(20), server_default=text("'float64'"), comment="数据类型")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="描述")

    # Relationships
    model_version: Mapped["ModelVersion"] = relationship(back_populates="weights")


class ModelValidation(Base, CreatedAtMixin):
    """模型验证结果记录。"""

    __tablename__ = "model_validations"
    __table_args__ = (
        Index("idx_model_valid_version", "model_version_id", "validated_at"),
        Index("idx_model_valid_type", "validation_type", "result_status"),
        {"comment": "模型验证结果记录"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    model_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_versions.id", ondelete="CASCADE"),
        nullable=False, comment="模型版本 ID",
    )
    validation_type: Mapped[str] = mapped_column(
        String(30), nullable=False,
        comment="IN_SAMPLE / OUT_OF_SAMPLE / WALK_FORWARD / STRESS_TEST / EXTREME_MARKET",
    )
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="验证时间",
    )
    # 测试区间
    period_start: Mapped[date] = mapped_column(Date, nullable=False, comment="验证区间开始")
    period_end: Mapped[date] = mapped_column(Date, nullable=False, comment="验证区间结束")
    period_description: Mapped[Optional[str]] = mapped_column(String(200), comment="区间描述")
    # 结果指标
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="完整指标 JSON")
    sharpe_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="夏普比率")
    sortino_ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="索提诺比率")
    max_drawdown: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="最大回撤")
    win_rate: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="胜率")
    profit_factor: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="盈亏比")
    accuracy: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="准确率")
    precision_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="精确率")
    recall_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="召回率")
    f1_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="F1 分数")
    # 状态
    result_status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'PENDING'"), comment="PENDING / PASSED / FAILED / INCONCLUSIVE",
    )
    passed: Mapped[Optional[bool]] = mapped_column(Boolean, comment="是否通过")
    notes: Mapped[Optional[str]] = mapped_column(Text, comment="备注")
    # 数据快照
    data_snapshot_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), comment="验证时使用的数据快照时间（防止未来数据泄漏）",
    )
    data_version: Mapped[Optional[str]] = mapped_column(String(50), comment="数据版本")

    # Relationships
    model_version: Mapped["ModelVersion"] = relationship(back_populates="validations")
