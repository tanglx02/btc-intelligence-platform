"""全部 ENUM 类型定义。

对应 docs/architecture/09-core-tables.md 中定义的 16 个 PostgreSQL ENUM 类型。
使用 Python enum + SQLAlchemy Enum 类型映射。
"""

import enum

from sqlalchemy import Enum as SAEnum


# ---- Provider 相关 ----

class ProviderCategory(str, enum.Enum):
    """Provider 数据类别。"""
    MARKET = "MARKET"
    ONCHAIN = "ONCHAIN"
    EXCHANGE_FLOW = "EXCHANGE_FLOW"
    ETF = "ETF"
    DERIVATIVES = "DERIVATIVES"
    OPTIONS = "OPTIONS"
    MACRO = "MACRO"
    SENTIMENT = "SENTIMENT"
    NEWS = "NEWS"


class ProviderStatus(str, enum.Enum):
    """Provider 运行状态。"""
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    SLOW = "SLOW"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_ERROR = "AUTH_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    DATA_ERROR = "DATA_ERROR"
    OFFLINE = "OFFLINE"
    DISABLED = "DISABLED"


class FailoverResolution(str, enum.Enum):
    """故障切换解决方式。"""
    AUTO_RECOVERED = "AUTO_RECOVERED"
    MANUAL_RESET = "MANUAL_RESET"
    TIMEOUT = "TIMEOUT"
    PERMANENTLY_DISABLED = "PERMANENTLY_DISABLED"


# ---- 数据质量 ----

class QualityStatus(str, enum.Enum):
    """数据质量状态。"""
    VERIFIED = "VERIFIED"
    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


# ---- K线 ----

class CandleInterval(str, enum.Enum):
    """K线时间间隔。"""
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


# ---- 引擎输出 ----

class CyclePhase(str, enum.Enum):
    """市场周期阶段。"""
    DEEP_BEAR = "DEEP_BEAR"
    BEAR = "BEAR"
    BOTTOM_BUILDING = "BOTTOM_BUILDING"
    RECOVERY = "RECOVERY"
    UPTREND = "UPTREND"
    ACCELERATION = "ACCELERATION"
    DISTRIBUTION = "DISTRIBUTION"
    TOP_RISK = "TOP_RISK"
    DECLINE = "DECLINE"


class ValuationLevel(str, enum.Enum):
    """估值等级。"""
    DEEP_UNDERVALUED = "DEEP_UNDERVALUED"
    UNDERVALUED = "UNDERVALUED"
    FAIR = "FAIR"
    OVERVALUED = "OVERVALUED"
    EXTREME_OVERVALUED = "EXTREME_OVERVALUED"


class RiskLevel(str, enum.Enum):
    """风险等级。"""
    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    MODERATE = "MODERATE"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"
    EXTREME = "EXTREME"


class SignalDirection(str, enum.Enum):
    """信号方向。"""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


# ---- 系统任务 ----

class JobStatus(str, enum.Enum):
    """系统任务状态。"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"


class SyncStatus(str, enum.Enum):
    """数据同步状态。"""
    IDLE = "IDLE"
    SYNCING = "SYNCING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY = "RETRY"


# ---- 模型管理 ----

class ModelStatus(str, enum.Enum):
    """模型版本状态。"""
    DRAFT = "DRAFT"
    TRAINING = "TRAINING"
    VALIDATED = "VALIDATED"
    DEPLOYED = "DEPLOYED"
    DEPRECATED = "DEPRECATED"
    REJECTED = "REJECTED"


# ---- 回测 ----

class BacktestStatus(str, enum.Enum):
    """回测运行状态。"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---- 用户/组合 ----

class UserRole(str, enum.Enum):
    """用户角色。"""
    ADMIN = "ADMIN"
    ANALYST = "ANALYST"
    USER = "USER"


class TradeSide(str, enum.Enum):
    """交易方向。"""
    BUY = "BUY"
    SELL = "SELL"


class PlanStatus(str, enum.Enum):
    """资金计划状态。"""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


# ---- SQLAlchemy ENUM 类型工厂（供 mapped_column 使用）----

provider_category_enum = SAEnum(
    ProviderCategory, name="provider_category", create_type=False
)
provider_status_enum = SAEnum(
    ProviderStatus, name="provider_status", create_type=False
)
failover_resolution_enum = SAEnum(
    FailoverResolution, name="failover_resolution", create_type=False
)
quality_status_enum = SAEnum(
    QualityStatus, name="quality_status_type", create_type=False
)
candle_interval_enum = SAEnum(
    CandleInterval,
    name="candle_interval",
    create_type=False,
    # 成员名（M1/D1）与存储值（1m/1d）不同，需按「值」读写以匹配迁移中定义的 ENUM
    values_callable=lambda e: [m.value for m in e],
)
cycle_phase_enum = SAEnum(
    CyclePhase, name="cycle_phase", create_type=False
)
valuation_level_enum = SAEnum(
    ValuationLevel, name="valuation_level", create_type=False
)
risk_level_enum = SAEnum(
    RiskLevel, name="risk_level", create_type=False
)
signal_direction_enum = SAEnum(
    SignalDirection, name="signal_direction", create_type=False
)
job_status_enum = SAEnum(
    JobStatus, name="job_status", create_type=False
)
sync_status_enum = SAEnum(
    SyncStatus, name="sync_status", create_type=False
)
model_status_enum = SAEnum(
    ModelStatus, name="model_status", create_type=False
)
backtest_status_enum = SAEnum(
    BacktestStatus, name="backtest_status", create_type=False
)
user_role_enum = SAEnum(
    UserRole, name="user_role", create_type=False
)
trade_side_enum = SAEnum(
    TradeSide, name="trade_side", create_type=False
)
plan_status_enum = SAEnum(
    PlanStatus, name="plan_status", create_type=False
)


# ---- 预警 ----

class AlertSeverity(str, enum.Enum):
    """预警严重程度。"""
    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, enum.Enum):
    """预警事件状态。"""
    TRIGGERED = "TRIGGERED"
    ACKED = "ACKED"
    RESOLVED = "RESOLVED"
    SUPPRESSED = "SUPPRESSED"


class AlertRuleState(str, enum.Enum):
    """预警规则状态。"""
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    ARCHIVED = "ARCHIVED"
    ERROR = "ERROR"


alert_severity_enum = SAEnum(
    AlertSeverity, name="alert_severity", create_type=False
)
alert_status_enum = SAEnum(
    AlertStatus, name="alert_status", create_type=False
)
alert_rule_state_enum = SAEnum(
    AlertRuleState, name="alert_rule_state", create_type=False
)
