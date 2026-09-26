"""ORM 模型统一导出。

导入此包即可注册所有模型到 SQLAlchemy metadata，
供 Alembic 迁移自动检测及 FastAPI 依赖注入使用。
"""

# Base & Mixins
from .base import Base, CreatedAtMixin, DataSourceMixin, TimestampMixin, UUIDPrimaryKeyMixin

# Enums
from .enums import (
    AlertRuleState,
    AlertSeverity,
    AlertStatus,
    BacktestStatus,
    CandleInterval,
    CyclePhase,
    FailoverResolution,
    JobStatus,
    ModelStatus,
    PlanStatus,
    ProviderCategory,
    ProviderStatus,
    QualityStatus,
    RiskLevel,
    SignalDirection,
    SyncStatus,
    TradeSide,
    UserRole,
    ValuationLevel,
)

# Provider 管理
from .provider import (
    Provider,
    ProviderFailoverEvent,
    ProviderHealth,
    ProviderRequest,
    ProviderScore,
)

# 原始数据
from .raw_data import (
    RawDerivativesData,
    RawEtfData,
    RawMacroData,
    RawMarketData,
    RawOnchainData,
    RawSentimentData,
)

# 标准化市场数据
from .market import Asset, Candle, MarketPrice, Orderbook, Trade

# 链上数据
from .onchain import AddressMetric, ExchangeFlow, OnchainMetric, SupplyMetric

# ETF 数据
from .etf import EtfFlow, EtfHolding

# 衍生品数据
from .derivatives import Derivative, OptionsData

# 宏观数据
from .macro import MacroEvent, MacroSeries

# 情绪数据
from .sentiment import Sentiment

# 指标
from .indicator import IndicatorDefinition, IndicatorValue

# 引擎输出
from .engine import CycleState, MarketRegime, RiskScore, Signal, ValuationState

# 模型管理
from .model import ModelValidation, ModelVersion, ModelWeight

# 回测
from .backtest import BacktestResult, BacktestRun, BacktestTrade, Strategy, StrategyVersion

# 用户与投资组合
from .portfolio import PortfolioSnapshot, User, UserHolding, UserPlan, UserTransaction

# 系统数据
from .system import AuditLog, DataQuality, MarketEvent, SyncCheckpoint, SystemJob

# 预警
from .alert import AlertChannelConfig, AlertDelivery, AlertEvent, AlertRule

__all__ = [
    # Base
    "Base",
    "CreatedAtMixin",
    "DataSourceMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    # Enums
    "AlertRuleState",
    "AlertSeverity",
    "AlertStatus",
    "BacktestStatus",
    "CandleInterval",
    "CyclePhase",
    "FailoverResolution",
    "JobStatus",
    "ModelStatus",
    "PlanStatus",
    "ProviderCategory",
    "ProviderStatus",
    "QualityStatus",
    "RiskLevel",
    "SignalDirection",
    "SyncStatus",
    "TradeSide",
    "UserRole",
    "ValuationLevel",
    # Provider
    "Provider",
    "ProviderFailoverEvent",
    "ProviderHealth",
    "ProviderRequest",
    "ProviderScore",
    # Raw Data
    "RawDerivativesData",
    "RawEtfData",
    "RawMacroData",
    "RawMarketData",
    "RawOnchainData",
    "RawSentimentData",
    # Market
    "Asset",
    "Candle",
    "MarketPrice",
    "Orderbook",
    "Trade",
    # Onchain
    "AddressMetric",
    "ExchangeFlow",
    "OnchainMetric",
    "SupplyMetric",
    # ETF
    "EtfFlow",
    "EtfHolding",
    # Derivatives
    "Derivative",
    "OptionsData",
    # Macro
    "MacroEvent",
    "MacroSeries",
    # Sentiment
    "Sentiment",
    # Indicator
    "IndicatorDefinition",
    "IndicatorValue",
    # Engine
    "CycleState",
    "MarketRegime",
    "RiskScore",
    "Signal",
    "ValuationState",
    # Model
    "ModelValidation",
    "ModelVersion",
    "ModelWeight",
    # Backtest
    "BacktestResult",
    "BacktestRun",
    "BacktestTrade",
    "Strategy",
    "StrategyVersion",
    # Portfolio
    "PortfolioSnapshot",
    "User",
    "UserHolding",
    "UserPlan",
    "UserTransaction",
    # System
    "AuditLog",
    "DataQuality",
    "MarketEvent",
    "SyncCheckpoint",
    "SystemJob",
    # Alert
    "AlertChannelConfig",
    "AlertDelivery",
    "AlertEvent",
    "AlertRule",
]
