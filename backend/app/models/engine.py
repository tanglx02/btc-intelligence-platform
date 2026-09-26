"""引擎输出 ORM 模型。

包含：cycle_states, valuation_states, risk_scores, market_regimes, signals
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, CreatedAtMixin
from .enums import (
    CyclePhase,
    QualityStatus,
    RiskLevel,
    SignalDirection,
    ValuationLevel,
    cycle_phase_enum,
    quality_status_enum,
    risk_level_enum,
    signal_direction_enum,
    valuation_level_enum,
)


class CycleState(Base, CreatedAtMixin):
    """市场周期状态（Cycle Engine 输出）。"""

    __tablename__ = "cycle_states"
    __table_args__ = (
        Index("idx_cycle_phase", "phase", "observation_time"),
        Index("idx_cycle_time", "observation_time"),
        {"comment": "市场周期状态（Cycle Engine 输出）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="数据观测时间",
    )
    phase: Mapped[CyclePhase] = mapped_column(cycle_phase_enum, nullable=False, comment="当前周期阶段")
    previous_phase: Mapped[Optional[CyclePhase]] = mapped_column(cycle_phase_enum, comment="前一周期阶段")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0"), comment="判断置信度 0-1")
    phase_duration_days: Mapped[Optional[int]] = mapped_column(Integer, comment="当前阶段持续天数")
    evidence_for: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="支持证据")
    evidence_against: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="反对证据")
    dimension_scores: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="各维度评分")
    historical_similar: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="历史相似阶段")
    model_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="模型版本 ID")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="状态描述")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class ValuationState(Base, CreatedAtMixin):
    """估值状态（Valuation Engine 输出）。"""

    __tablename__ = "valuation_states"
    __table_args__ = (
        Index("idx_valuation_level", "valuation_level", "observation_time"),
        Index("idx_valuation_time", "observation_time"),
        {"comment": "估值状态（Valuation Engine 输出）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="数据观测时间",
    )
    valuation_level: Mapped[ValuationLevel] = mapped_column(valuation_level_enum, nullable=False, comment="估值等级")
    previous_level: Mapped[Optional[ValuationLevel]] = mapped_column(valuation_level_enum, comment="前一估值等级")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0"), comment="判断置信度 0-1")
    # 核心估值指标
    mvrv_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="MVRV 比率值")
    mvrv_percentile: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="MVRV 历史分位数")
    realized_cap: Mapped[Optional[Decimal]] = mapped_column(Numeric(24, 2), comment="已实现市值（USD）")
    realized_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="已实现价格")
    cost_basis: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="市场平均成本基础")
    nupl_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 6), comment="NUPL 值")
    nupl_percentile: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="NUPL 历史分位数")
    # 综合评分
    overall_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="综合估值评分")
    components: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="各估值因子详情")
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="证据列表")
    historical_context: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="历史上下文")
    model_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="模型版本 ID")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="状态描述")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class RiskScore(Base, CreatedAtMixin):
    """风险评分（Risk Engine 输出）。"""

    __tablename__ = "risk_scores"
    __table_args__ = (
        Index("idx_risk_level", "risk_level", "observation_time"),
        Index("idx_risk_time", "observation_time"),
        Index("idx_risk_high", "observation_time", postgresql_where=text("overall_risk >= 70")),
        {"comment": "风险评分（Risk Engine 输出）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="数据观测时间",
    )
    # 各维度风险评分 0-100
    overall_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="综合风险评分 0-100")
    trend_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="趋势风险")
    valuation_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="估值风险")
    leverage_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="杠杆风险")
    liquidity_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="流动性风险")
    macro_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="宏观风险")
    onchain_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="链上风险")
    volatility_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="波动率风险")
    crowding_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="拥挤度风险")
    drawdown_risk: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, server_default=text("0"), comment="回撤风险")
    # 综合
    risk_level: Mapped[RiskLevel] = mapped_column(
        risk_level_enum, nullable=False, server_default=text("'MODERATE'"), comment="风险等级",
    )
    previous_level: Mapped[Optional[RiskLevel]] = mapped_column(risk_level_enum, comment="前一风险等级")
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0"), comment="判断置信度 0-1")
    risk_factors: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="风险因子详情")
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="证据列表")
    warnings: Mapped[Optional[list]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"), comment="风险警告")
    model_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="模型版本 ID")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="状态描述")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class MarketRegime(Base, CreatedAtMixin):
    """综合市场状态（Multi-Factor Engine 输出）。"""

    __tablename__ = "market_regimes"
    __table_args__ = (
        Index("idx_regimes_overall", "overall_regime", "observation_time"),
        Index("idx_regimes_changed", "observation_time", postgresql_where=text("is_changed = true")),
        Index("idx_regimes_time", "observation_time"),
        {"comment": "综合市场状态（Multi-Factor Engine 输出）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    source_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("providers.id"), comment="数据来源 Provider ID",
    )
    observation_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, nullable=False, comment="数据观测时间",
    )
    # 各维度状态
    trend_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="趋势状态")
    valuation_state: Mapped[ValuationLevel] = mapped_column(
        valuation_level_enum, nullable=False, server_default=text("'FAIR'"), comment="估值状态",
    )
    capital_flow_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="资金流状态")
    onchain_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="链上状态")
    derivative_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="衍生品状态")
    macro_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="宏观状态")
    sentiment_state: Mapped[str] = mapped_column(String(30), nullable=False, server_default=text("'NEUTRAL'"), comment="情绪状态")
    risk_state: Mapped[RiskLevel] = mapped_column(
        risk_level_enum, nullable=False, server_default=text("'MODERATE'"), comment="风险状态",
    )
    cycle_state: Mapped[CyclePhase] = mapped_column(
        cycle_phase_enum, nullable=False, server_default=text("'UPTREND'"), comment="周期状态",
    )
    # 综合判断
    overall_regime: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'NEUTRAL'"),
        comment="综合市场状态：BULL_STRONG / BULL / NEUTRAL_BULL / NEUTRAL / NEUTRAL_BEAR / BEAR / BEAR_STRONG / CRISIS",
    )
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0"), comment="判断置信度 0-1")
    regime_score: Mapped[Optional[Decimal]] = mapped_column(Numeric(8, 4), comment="综合状态评分")
    # 变化追踪
    is_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), comment="是否发生了状态变化")
    previous_regime: Mapped[Optional[str]] = mapped_column(String(50), comment="前一状态")
    change_reason: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="状态变化原因")
    # 详情
    dimension_details: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), comment="各维度详情")
    evidence_summary: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="证据摘要")
    model_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="模型版本 ID")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="状态描述")
    quality_status: Mapped[QualityStatus] = mapped_column(
        quality_status_enum, nullable=False, server_default=text("'VERIFIED'"), comment="数据质量状态",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间",
    )


class Signal(Base, CreatedAtMixin):
    """信号事件记录（分析提醒用途，非自动交易）。"""

    __tablename__ = "signals"
    __table_args__ = (
        Index("idx_signals_type", "signal_type", "signal_time"),
        Index("idx_signals_direction", "signal_direction", "signal_time"),
        Index("idx_signals_active", "signal_time", postgresql_where=text("is_active = true")),
        Index("idx_signals_category", "category", "signal_time"),
        {"comment": "信号事件记录（分析提醒用途，非自动交易）"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4,
        server_default=func.gen_random_uuid(), comment="主键 UUID",
    )
    signal_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False, comment="信号时间",
    )
    signal_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="信号类型：TREND_CHANGE / VALUATION_ALERT / RISK_WARNING / FLOW_ANOMALY / CYCLE_SHIFT",
    )
    signal_direction: Mapped[SignalDirection] = mapped_column(signal_direction_enum, nullable=False, comment="信号方向")
    strength: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False, server_default=text("0"), comment="信号强度 0-1")
    category: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="信号分类：TREND / VALUATION / RISK / ONCHAIN / DERIVATIVE / MACRO / SENTIMENT",
    )
    engine_source: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="产生引擎：CYCLE_ENGINE / VALUATION_ENGINE / RISK_ENGINE / REGIME_ENGINE",
    )
    # 触发条件
    trigger_conditions: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="触发条件")
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"), comment="证据列表")
    # 上下文
    market_context: Mapped[Optional[dict]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"), comment="市场上下文")
    btc_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(20, 8), comment="信号时 BTC 价格")
    regime_at_signal: Mapped[Optional[str]] = mapped_column(String(50), comment="信号时市场状态")
    # 描述
    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="信号标题")
    description: Mapped[Optional[str]] = mapped_column(Text, comment="英文描述")
    description_cn: Mapped[Optional[str]] = mapped_column(Text, comment="中文描述")
    # 元数据
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), comment="是否有效")
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), comment="过期时间")
    model_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), comment="模型版本 ID")
