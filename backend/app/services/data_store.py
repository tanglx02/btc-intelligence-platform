"""数据存储层 — Raw / Normalized 双存储写入服务。

对应架构文档：
- 《11-historical-data.md》§2 双存储架构（Raw + Normalized）
- 《10-data-quality.md》§1.2 Write Gate（写入前校验闸门）、§1.5 双存储原则

本模块提供两个核心类：

``RawDataStore``
    原始 API 响应的 **append-only** 存储。写入 ``raw_*`` 系列表，
    完整保留响应体（JSONB）、请求参数、抓取时间，并计算 ``response_hash``
    用于幂等去重。**绝不修改已落库的原始数据**。

``NormalizedDataStore``
    标准化数据的批量写入。统一执行：

    1. **有效性校验**（Schema / 物理范围）→ 不合格数据拒绝进入 Normalized 表；
    2. **去重**（相同 ``(source_id, observation_time[, 业务键])`` 不重复写入）；
    3. **Write Gate**（新数据质量低于已有数据时不覆盖；剧变且单源时保留旧值并标记冲突）；
    4. **批量写入**（默认 1000 条/批，``executemany``，不逐条 INSERT）。

设计约束：
- 绝不生成假数据：本模块只做写入与校验，不做任何插值/兜底填充；
- 所有方法均支持传入外部 ``session``，以便与 checkpoint 更新在 **同一事务** 内原子提交
  （《11-historical-data.md》§3.2 关键实现规则 1）。
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, ClassVar, Sequence
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy import Numeric, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_session_factory
from app.models import (
    Candle,
    Derivative,
    EtfFlow,
    MacroSeries,
    MarketPrice,
    OnchainMetric,
    Provider,
    RawDerivativesData,
    RawEtfData,
    RawMacroData,
    RawMarketData,
    RawOnchainData,
    RawSentimentData,
    Sentiment,
)
from app.models.enums import CandleInterval, ProviderCategory, QualityStatus

# ---- 批量写入与精度常量 ----

#: 默认批量写入大小（《11-historical-data.md》§4.5：默认 1000 条/批）
DEFAULT_BATCH_SIZE = 1000

#: BTC 数量精度（satoshi 级，8 位小数）
PRECISION_BTC = Decimal("0.00000001")
#: USD 金额精度（2 位小数）
PRECISION_USD = Decimal("0.01")
#: 成交量精度（4 位小数）
PRECISION_VOLUME = Decimal("0.0001")
#: 比率精度（10 位小数）
PRECISION_RATIO = Decimal("0.0000000001")

#: 质量状态优先级（Write Gate 判定：低质量不得覆盖高质量）
QUALITY_RANK: dict[QualityStatus, int] = {
    QualityStatus.INVALID: 0,
    QualityStatus.STALE: 1,
    QualityStatus.CONFLICT: 2,
    QualityStatus.ESTIMATED: 3,
    QualityStatus.VERIFIED: 4,
}

#: Raw 表中 response_hash 的保留元数据键
_RESPONSE_HASH_KEY = "__response_hash"


def utcnow() -> datetime:
    """返回带时区的当前 UTC 时间。"""
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime | None:
    """将 naive datetime 视为 UTC 并补齐时区信息。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_decimal(value: Any) -> Decimal | None:
    """将任意数值安全转换为 Decimal（禁止 float 直接入库造成累计误差）。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def compute_response_hash(response_body: Any) -> str:
    """计算响应体的 SHA-256 哈希，用于幂等去重与完整性校验。

    Args:
        response_body: 原始响应体（dict / list / str / bytes）

    Returns:
        64 位十六进制哈希字符串
    """
    if isinstance(response_body, bytes | bytearray):
        payload = bytes(response_body)
    elif isinstance(response_body, str):
        payload = response_body.encode("utf-8")
    else:
        try:
            payload = json.dumps(
                response_body, sort_keys=True, ensure_ascii=False, default=str
            ).encode("utf-8")
        except (TypeError, ValueError):
            payload = repr(response_body).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _BoundedHashCache:
    """有界 LRU 哈希集合 — Raw 响应进程内幂等去重。"""

    def __init__(self, maxlen: int = 50_000) -> None:
        self._maxlen = max(1, maxlen)
        self._data: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        if key in self._data:
            self._data.move_to_end(key)
            return True
        return False

    def __len__(self) -> int:
        return len(self._data)

    def add(self, key: str) -> None:
        """加入哈希，超出容量时淘汰最旧条目。"""
        self._data[key] = None
        self._data.move_to_end(key)
        while len(self._data) > self._maxlen:
            self._data.popitem(last=False)

    def clear(self) -> None:
        """清空缓存。"""
        self._data.clear()


# ==========================================================================
# 范围校验规则（Write Gate 第 2 步：物理范围校验）
# ==========================================================================


@dataclass(frozen=True, slots=True)
class RangeRule:
    """单列数值范围规则。

    Attributes:
        min_value: 下界（None 表示不限）
        max_value: 上界（None 表示不限）
        min_inclusive: 下界是否可取等
        max_inclusive: 上界是否可取等
    """

    min_value: Decimal | None = None
    max_value: Decimal | None = None
    min_inclusive: bool = False
    max_inclusive: bool = True

    def violates(self, value: Decimal) -> bool:
        """判断给定值是否越界。"""
        if self.min_value is not None:
            if value < self.min_value or (value == self.min_value and not self.min_inclusive):
                return True
        if self.max_value is not None:
            if value > self.max_value or (value == self.max_value and not self.max_inclusive):
                return True
        return False


#: 必须严格为正的列（价格类）
_POSITIVE = RangeRule(min_value=Decimal("0"), min_inclusive=False)
#: 必须非负的列（数量 / 金额类）
_NON_NEGATIVE = RangeRule(min_value=Decimal("0"), min_inclusive=True)
#: 百分比列（-100 ~ 100）
_PERCENT = RangeRule(min_value=Decimal("-100"), max_value=Decimal("100"), min_inclusive=True)
#: 0-100 情绪指数
_INDEX_0_100 = RangeRule(min_value=Decimal("0"), max_value=Decimal("100"), min_inclusive=True)
#: 0-1 置信度 / 比率
_RATIO_0_1 = RangeRule(min_value=Decimal("0"), max_value=Decimal("1"), min_inclusive=True)

#: 列名 → 范围规则映射（跨表复用，按列名匹配）
RANGE_RULES: dict[str, RangeRule] = {
    # 价格
    "price": _POSITIVE,
    "open": _POSITIVE,
    "high": _POSITIVE,
    "low": _POSITIVE,
    "close": _POSITIVE,
    "bid": _POSITIVE,
    "ask": _POSITIVE,
    "mid_price": _POSITIVE,
    "spread": _NON_NEGATIVE,
    "high_24h": _POSITIVE,
    "low_24h": _POSITIVE,
    "vwap": _POSITIVE,
    "nav": _POSITIVE,
    "nav_per_share": _POSITIVE,
    "market_price": _POSITIVE,
    "last_price": _POSITIVE,
    "strike_price": _POSITIVE,
    "max_pain": _POSITIVE,
    # 数量 / 金额
    "volume": _NON_NEGATIVE,
    "volume_24h": _NON_NEGATIVE,
    "quote_volume": _NON_NEGATIVE,
    "quote_volume_24h": _NON_NEGATIVE,
    "daily_volume_usd": _NON_NEGATIVE,
    "market_cap": _NON_NEGATIVE,
    "total_aum_usd": _NON_NEGATIVE,
    "open_interest": _NON_NEGATIVE,
    "open_interest_usd": _NON_NEGATIVE,
    "quantity": _NON_NEGATIVE,
    "quote_quantity": _NON_NEGATIVE,
    "inflow_btc": _NON_NEGATIVE,
    "outflow_btc": _NON_NEGATIVE,
    "inflow_usd": _NON_NEGATIVE,
    "outflow_usd": _NON_NEGATIVE,
    "holdings_btc": _NON_NEGATIVE,
    "holdings_usd": _NON_NEGATIVE,
    "total_holdings_btc": _NON_NEGATIVE,
    "shares_outstanding": _NON_NEGATIVE,
    "exchange_balance": _NON_NEGATIVE,
    "liquidation_long_usd": _NON_NEGATIVE,
    "liquidation_short_usd": _NON_NEGATIVE,
    "liquidation_total_usd": _NON_NEGATIVE,
    "taker_buy_volume": _NON_NEGATIVE,
    "taker_sell_volume": _NON_NEGATIVE,
    "total_supply": _NON_NEGATIVE,
    "bid_depth": _NON_NEGATIVE,
    "ask_depth": _NON_NEGATIVE,
    # 百分比 / 比率
    "price_change_pct_24h": _PERCENT,
    "pct_of_supply": _PERCENT,
    "premium_discount": _PERCENT,
    "confidence": _RATIO_0_1,
    "long_short_ratio": _NON_NEGATIVE,
    "long_account_ratio": _NON_NEGATIVE,
    "put_call_ratio": _NON_NEGATIVE,
    "implied_volatility": _NON_NEGATIVE,
}

#: 情绪指标专用范围（按 metric_name 前缀匹配）
SENTIMENT_RANGE_RULES: dict[str, RangeRule] = {
    "fear_greed": _INDEX_0_100,
}


def check_row_validity(row: dict[str, Any]) -> list[str]:
    """对单行数据执行物理范围校验（Write Gate 第 2 步）。

    Args:
        row: 列名 → 值 的字典

    Returns:
        违规描述列表，空列表表示通过
    """
    problems: list[str] = []
    for column, value in row.items():
        rule = RANGE_RULES.get(column)
        if rule is None or value is None:
            continue
        decimal_value = _to_decimal(value)
        if decimal_value is None:
            problems.append(f"{column}: 非数值类型 ({value!r})")
            continue
        if rule.violates(decimal_value):
            problems.append(
                f"{column}: 值 {decimal_value} 超出允许范围 "
                f"[{rule.min_value}, {rule.max_value}]"
            )

    # OHLC 内在一致性：high >= max(open, close) >= min(open, close) >= low
    ohlc = {k: _to_decimal(row.get(k)) for k in ("open", "high", "low", "close")}
    if all(v is not None for v in ohlc.values()):
        high, low = ohlc["high"], ohlc["low"]
        if high < low:
            problems.append(f"OHLC 矛盾: high({high}) < low({low})")
        else:
            for name in ("open", "close"):
                value = ohlc[name]
                if value > high or value < low:
                    problems.append(f"OHLC 矛盾: {name}({value}) 不在 [low, high] 区间内")

    # 买卖价一致性
    bid, ask = _to_decimal(row.get("bid")), _to_decimal(row.get("ask"))
    if bid is not None and ask is not None and bid > ask:
        problems.append(f"盘口矛盾: bid({bid}) > ask({ask})")

    return problems


# ==========================================================================
# 存储结果结构
# ==========================================================================


@dataclass
class RawStoreResult:
    """Raw 数据存储结果。

    Attributes:
        success: 是否成功落库
        stored: 实际写入条数
        skipped_duplicate: 因 response_hash 重复而跳过的条数
        skipped_no_provider: 因 Provider 未注册（无法解析 source_id）而跳过的条数
        response_hash: 响应体哈希（单条写入时返回）
        error: 错误描述
    """

    success: bool = True
    stored: int = 0
    skipped_duplicate: int = 0
    skipped_no_provider: int = 0
    response_hash: str | None = None
    error: str | None = None


@dataclass
class StoreResult:
    """标准化数据批量写入结果。

    Attributes:
        total: 提交记录总数
        inserted: 新插入条数
        updated: 通过 Write Gate 后更新的条数
        skipped_duplicate: 完全重复（键相同且值一致）跳过条数
        rejected_invalid: 范围/有效性校验失败被拒绝的条数
        rejected_write_gate: 被 Write Gate 拦截（不允许覆盖）的条数
        conflicts: 冲突明细（旧值 / 新值 / 偏差 / 来源）
        invalid_details: 校验失败明细
        batch_count: 实际执行批次数
        duration_ms: 总耗时（毫秒）
    """

    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_duplicate: int = 0
    rejected_invalid: int = 0
    rejected_write_gate: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    invalid_details: list[dict[str, Any]] = field(default_factory=list)
    batch_count: int = 0
    duration_ms: float = 0.0

    @property
    def accepted(self) -> int:
        """被接受写入（插入 + 更新）的记录数。"""
        return self.inserted + self.updated

    def merge(self, other: StoreResult) -> None:
        """将另一个结果合并进当前结果（用于分片写入汇总）。"""
        self.total += other.total
        self.inserted += other.inserted
        self.updated += other.updated
        self.skipped_duplicate += other.skipped_duplicate
        self.rejected_invalid += other.rejected_invalid
        self.rejected_write_gate += other.rejected_write_gate
        self.conflicts.extend(other.conflicts)
        self.invalid_details.extend(other.invalid_details)
        self.batch_count += other.batch_count
        self.duration_ms += other.duration_ms

    def as_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的字典（用于日志 / 进度上报）。"""
        return {
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "accepted": self.accepted,
            "skipped_duplicate": self.skipped_duplicate,
            "rejected_invalid": self.rejected_invalid,
            "rejected_write_gate": self.rejected_write_gate,
            "conflict_count": len(self.conflicts),
            "batch_count": self.batch_count,
            "duration_ms": round(self.duration_ms, 2),
        }


# ==========================================================================
# 标准化记录数据结构
# ==========================================================================


@dataclass(kw_only=True)
class NormalizedRecord:
    """标准化数据记录基类。

    所有 ``NormalizedDataStore.store_*`` 方法接收的记录类型均继承自此类。
    子类通过 ``ClassVar`` 声明目标 ORM 模型、去重键与剧变守卫字段。

    Attributes:
        source_id: 数据来源 Provider ID（溯源五元组之一）
        observation_time: 数据观测时间（UTC）
        fetch_time: 数据抓取时间（UTC）
        quality_status: 数据质量状态
        rule_version: 标准化规则版本（写入 metadata JSONB，用于回测复现）
        metadata: 附加元数据
    """

    #: 目标 ORM 模型
    MODEL: ClassVar[type[Any] | None] = None
    #: 去重 / UPSERT 判定键列
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = ("source_id", "observation_time")
    #: 数据库唯一约束列（用于 ON CONFLICT DO NOTHING 竞态保护），None 表示无约束
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = None
    #: 剧变守卫字段：新旧值偏差超过 ``GUARD_MAX_JUMP_PCT`` 且为单源时拒绝覆盖
    GUARD_COLUMNS: ClassVar[tuple[str, ...]] = ()
    #: 剧变阈值（百分比）。BTC 价格 1 分钟内变动 > 20% 视为可疑（文档 10 §1.2）
    GUARD_MAX_JUMP_PCT: ClassVar[Decimal] = Decimal("20")
    #: 业务名称（日志用）
    RECORD_NAME: ClassVar[str] = "normalized"

    source_id: UUID
    observation_time: datetime
    fetch_time: datetime = field(default_factory=utcnow)
    quality_status: QualityStatus = QualityStatus.ESTIMATED
    rule_version: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """统一时间时区与质量状态类型。"""
        self.observation_time = _ensure_utc(self.observation_time)  # type: ignore[assignment]
        self.fetch_time = _ensure_utc(self.fetch_time) or utcnow()
        if isinstance(self.quality_status, str):
            self.quality_status = QualityStatus(self.quality_status)

    # ---- 元编程辅助 ----

    @classmethod
    def model_columns(cls) -> frozenset[str]:
        """返回目标 ORM 模型的全部列 key（带缓存）。"""
        cached = cls.__dict__.get("_model_columns_cache")
        if cached is None:
            if cls.MODEL is None:
                raise ValueError(f"{cls.__name__} 未声明 MODEL")
            cached = frozenset(c.key for c in cls.MODEL.__table__.columns)
            setattr(cls, "_model_columns_cache", cached)
        return cached  # type: ignore[return-value]

    def key(self) -> tuple[Any, ...]:
        """返回本记录的去重键元组。"""
        return tuple(getattr(self, col) for col in self.KEY_COLUMNS)

    def to_row(self) -> dict[str, Any]:
        """转换为 ORM 列名 → 值 的字典（自动过滤 None、转换 Decimal、补齐主键）。"""
        columns = self.model_columns()
        row: dict[str, Any] = {}
        for f in fields(self):
            if f.name not in columns:
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            column = self.MODEL.__table__.columns[f.name]  # type: ignore[union-attr]
            if isinstance(column.type, Numeric):
                value = _to_decimal(value)
                if value is None:
                    continue
            row[f.name] = value

        # metadata JSONB：合并 rule_version（溯源五元组之一）
        if "metadata_" in columns:
            merged: dict[str, Any] = dict(self.metadata or {})
            if self.rule_version:
                merged.setdefault("rule_version", self.rule_version)
            row["metadata_"] = merged

        row.setdefault("id", uuid4())
        if "observation_time" in columns:
            row["observation_time"] = self.observation_time
        if "fetch_time" in columns:
            row["fetch_time"] = self.fetch_time
        return row

    def guard_values(self) -> dict[str, Decimal | None]:
        """提取剧变守卫字段的数值（用于 Write Gate 偏差判定）。"""
        return {col: _to_decimal(getattr(self, col, None)) for col in self.GUARD_COLUMNS}


@dataclass(kw_only=True)
class PriceRecord(NormalizedRecord):
    """标准化价格记录 → ``market_prices`` 表。"""

    MODEL: ClassVar[type[Any] | None] = MarketPrice
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = ("source_id", "observation_time", "symbol")
    GUARD_COLUMNS: ClassVar[tuple[str, ...]] = ("price",)
    RECORD_NAME: ClassVar[str] = "price"

    symbol: str = "BTCUSDT"
    price: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread: Decimal | None = None
    volume_24h: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    market_cap: Decimal | None = None
    high_24h: Decimal | None = None
    low_24h: Decimal | None = None
    price_change_pct_24h: Decimal | None = None
    cross_validated: bool = False
    validation_sources: int = 1
    deviation_pct: Decimal | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.price = _to_decimal(self.price)  # type: ignore[arg-type]
        if self.spread is None and self.bid is not None and self.ask is not None:
            bid, ask = _to_decimal(self.bid), _to_decimal(self.ask)
            if bid is not None and ask is not None:
                self.spread = (ask - bid).quantize(PRECISION_BTC)


@dataclass(kw_only=True)
class CandleRecord(NormalizedRecord):
    """标准化 K 线记录 → ``candles`` 表。"""

    MODEL: ClassVar[type[Any] | None] = Candle
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = ("symbol", "interval", "observation_time", "source_id")
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = ("symbol", "interval", "observation_time")
    GUARD_COLUMNS: ClassVar[tuple[str, ...]] = ("close",)
    RECORD_NAME: ClassVar[str] = "candle"

    symbol: str = "BTCUSDT"
    interval: CandleInterval = CandleInterval.H1
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: Decimal | None = None
    quote_volume: Decimal | None = None
    trades: int | None = None
    taker_buy_volume: Decimal | None = None
    taker_sell_volume: Decimal | None = None
    vwap: Decimal | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.interval, str):
            self.interval = CandleInterval(self.interval)
        if self.vwap is None and self.volume and self.quote_volume:
            volume = _to_decimal(self.volume)
            quote = _to_decimal(self.quote_volume)
            if volume and quote and volume > 0:
                self.vwap = (quote / volume).quantize(PRECISION_BTC)


@dataclass(kw_only=True)
class OnChainRecord(NormalizedRecord):
    """标准化链上指标记录 → ``onchain_metrics`` 表。"""

    MODEL: ClassVar[type[Any] | None] = OnchainMetric
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = ("metric_name", "observation_time", "source_id")
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = (
        "metric_name",
        "observation_time",
        "source_id",
    )
    RECORD_NAME: ClassVar[str] = "onchain"

    metric_name: str = ""
    value: Decimal | None = None
    unit: str = "ratio"


@dataclass(kw_only=True)
class ETFRecord(NormalizedRecord):
    """标准化 ETF 资金流记录 → ``etf_flows`` 表。"""

    MODEL: ClassVar[type[Any] | None] = EtfFlow
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = ("ticker", "observation_time", "source_id")
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = ("ticker", "observation_time")
    RECORD_NAME: ClassVar[str] = "etf"

    ticker: str = ""
    fund_name: str | None = None
    daily_inflow_usd: Decimal | None = None
    daily_outflow_usd: Decimal | None = None
    net_flow_usd: Decimal | None = None
    cumulative_flow_usd: Decimal | None = None
    total_holdings_btc: Decimal | None = None
    total_aum_usd: Decimal | None = None
    daily_volume_usd: Decimal | None = None
    price: Decimal | None = None
    nav: Decimal | None = None
    premium_discount: Decimal | None = None


@dataclass(kw_only=True)
class DerivativeRecord(NormalizedRecord):
    """标准化衍生品记录 → ``derivatives`` 表。"""

    MODEL: ClassVar[type[Any] | None] = Derivative
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = (
        "source_id",
        "observation_time",
        "exchange",
        "data_type",
        "symbol",
    )
    RECORD_NAME: ClassVar[str] = "derivative"

    symbol: str = "BTCUSDT"
    exchange: str = ""
    data_type: str = ""
    funding_rate: Decimal | None = None
    funding_rate_next: Decimal | None = None
    funding_time: datetime | None = None
    open_interest: Decimal | None = None
    open_interest_usd: Decimal | None = None
    open_interest_change: Decimal | None = None
    long_short_ratio: Decimal | None = None
    long_account_ratio: Decimal | None = None
    liquidation_long_usd: Decimal | None = None
    liquidation_short_usd: Decimal | None = None
    liquidation_total_usd: Decimal | None = None
    liquidation_count: int | None = None
    basis: Decimal | None = None
    basis_pct: Decimal | None = None
    premium_index: Decimal | None = None
    cvd: Decimal | None = None
    taker_buy_volume: Decimal | None = None
    taker_sell_volume: Decimal | None = None


@dataclass(kw_only=True)
class MacroRecord(NormalizedRecord):
    """标准化宏观数据记录 → ``macro_series`` 表。

    严格区分 observation_date / release_date / revision_date，
    防止回测中的未来数据泄漏（《10-data-quality.md》§5.1 宏观条目）。
    """

    MODEL: ClassVar[type[Any] | None] = MacroSeries
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = (
        "series_id",
        "observation_date",
        "revision_number",
        "source_id",
    )
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = (
        "series_id",
        "observation_date",
        "revision_number",
    )
    RECORD_NAME: ClassVar[str] = "macro"

    series_id: str = ""
    series_name: str = ""
    value: Decimal | None = None
    unit: str = "index"
    frequency: str = "MONTHLY"
    observation_date: date | None = None
    release_date: date | None = None
    revision_date: date | None = None
    previous_value: Decimal | None = None
    revised_value: Decimal | None = None
    is_revised: bool = False
    revision_number: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        # observation_date / release_date 为 NOT NULL，缺省时退化为观测时间的日期部分
        day = self.observation_time.date()
        if self.observation_date is None:
            self.observation_date = day
        if self.release_date is None:
            self.release_date = self.revision_date or day
        if not self.series_name:
            self.series_name = self.series_id


@dataclass(kw_only=True)
class SentimentRecord(NormalizedRecord):
    """标准化情绪数据记录 → ``sentiment`` 表。"""

    MODEL: ClassVar[type[Any] | None] = Sentiment
    KEY_COLUMNS: ClassVar[tuple[str, ...]] = (
        "source_type",
        "metric_name",
        "observation_time",
        "source_id",
    )
    CONFLICT_INDEX: ClassVar[tuple[str, ...] | None] = (
        "source_type",
        "metric_name",
        "observation_time",
    )
    RECORD_NAME: ClassVar[str] = "sentiment"

    source_type: str = ""
    metric_name: str = ""
    value: Decimal | None = None
    normalized_value: Decimal | None = None
    sentiment_label: str | None = None
    sample_size: int | None = None
    confidence: Decimal | None = None
    previous_value: Decimal | None = None
    change_pct: Decimal | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        # 情绪指数范围校验（Fear & Greed ∈ [0, 100]）
        rule = next(
            (r for prefix, r in SENTIMENT_RANGE_RULES.items() if self.metric_name.startswith(prefix)),
            None,
        )
        if rule is not None:
            decimal_value = _to_decimal(self.value)
            if decimal_value is not None and rule.violates(decimal_value):
                self.quality_status = QualityStatus.INVALID
                self.metadata = {
                    **self.metadata,
                    "invalid_reason": f"{self.metric_name}={decimal_value} 超出 [0,100]",
                }


# ==========================================================================
# Provider 名称 → UUID 解析器
# ==========================================================================


class ProviderIdResolver:
    """Provider 名称到数据库 UUID 的解析器（带进程内缓存）。

    ``raw_*`` 与 Normalized 表均以外键 ``source_id`` 引用 ``providers.id``，
    而 Provider 抽象层对外暴露的是名称字符串，因此需要一次名称 → ID 的解析。
    """

    def __init__(self) -> None:
        self._cache: dict[str, UUID | None] = {}

    async def resolve(self, session: AsyncSession, provider_name: str) -> UUID | None:
        """解析 Provider 名称为 UUID。

        Args:
            session: 数据库会话
            provider_name: Provider 唯一名称

        Returns:
            Provider UUID；未注册时返回 None（缓存 negative 结果避免重复查询）
        """
        if not provider_name:
            return None
        if provider_name in self._cache:
            return self._cache[provider_name]

        try:
            result = await session.execute(
                select(Provider.id).where(Provider.name == provider_name).limit(1)
            )
            provider_id = result.scalar_one_or_none()
        except Exception as exc:  # noqa: BLE001 - 解析失败降级为 None
            logger.warning(f"解析 Provider ID 失败 name={provider_name}: {exc}")
            return None

        self._cache[provider_name] = provider_id
        if provider_id is None:
            logger.warning(
                f"Provider '{provider_name}' 未在 providers 表注册，"
                "相关数据无法写入（外键约束）。请先执行 Provider 注册。"
            )
        return provider_id

    def prime(self, provider_name: str, provider_id: UUID | None) -> None:
        """预置缓存（供测试或启动时批量加载使用）。"""
        self._cache[provider_name] = provider_id

    def invalidate(self, provider_name: str | None = None) -> None:
        """失效缓存；provider_name 为 None 时清空全部。"""
        if provider_name is None:
            self._cache.clear()
        else:
            self._cache.pop(provider_name, None)


#: 全局共享的 Provider ID 解析器
provider_id_resolver = ProviderIdResolver()


# ==========================================================================
# 存储层基类
# ==========================================================================


class _StoreBase:
    """存储层公共基类：会话管理 + 批量分片。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        """初始化。

        Args:
            session_factory: 异步会话工厂；None 时使用全局工厂
            batch_size: 单批写入条数上限
        """
        self._session_factory = session_factory
        self._batch_size = max(1, batch_size)

    @property
    def batch_size(self) -> int:
        """单批写入条数上限。"""
        return self._batch_size

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    @asynccontextmanager
    async def _session_scope(self, session: AsyncSession | None = None):
        """获取会话上下文。

        传入 ``session`` 时由调用方掌控事务（用于与 checkpoint 原子提交）；
        否则自建会话并在正常退出时提交、异常时回滚。
        """
        if session is not None:
            yield session
            return
        factory = self._factory()
        async with factory() as new_session:
            try:
                yield new_session
                await new_session.commit()
            except Exception:
                await new_session.rollback()
                raise

    @staticmethod
    def _chunked(items: Sequence[Any], size: int):
        """将序列切分为固定大小的批次。"""
        for start in range(0, len(items), size):
            yield items[start : start + size]


# ==========================================================================
# RawDataStore
# ==========================================================================

#: 数据类别 → Raw 表模型映射
RAW_TABLE_MAP: dict[ProviderCategory, type[Any]] = {
    ProviderCategory.MARKET: RawMarketData,
    ProviderCategory.ONCHAIN: RawOnchainData,
    ProviderCategory.EXCHANGE_FLOW: RawOnchainData,
    ProviderCategory.ETF: RawEtfData,
    ProviderCategory.DERIVATIVES: RawDerivativesData,
    ProviderCategory.OPTIONS: RawDerivativesData,
    ProviderCategory.MACRO: RawMacroData,
    ProviderCategory.SENTIMENT: RawSentimentData,
    ProviderCategory.NEWS: RawSentimentData,
}

#: 各类别 Raw 表的业务标识字段及其默认值（None 表示必填）
RAW_IDENTIFIER_FIELDS: dict[ProviderCategory, dict[str, str | None]] = {
    ProviderCategory.MARKET: {"symbol": "BTC", "data_type": "PRICE"},
    ProviderCategory.ONCHAIN: {"metric_name": None},
    ProviderCategory.EXCHANGE_FLOW: {"metric_name": None},
    ProviderCategory.ETF: {"ticker": None, "data_type": "FLOW"},
    ProviderCategory.DERIVATIVES: {"exchange": None, "data_type": None, "symbol": "BTCUSDT"},
    ProviderCategory.OPTIONS: {"exchange": None, "data_type": "OPTIONS", "symbol": "BTCUSDT"},
    ProviderCategory.MACRO: {"series_id": None},
    ProviderCategory.SENTIMENT: {"source_type": None},
    ProviderCategory.NEWS: {"source_type": "NEWS"},
}


def _coerce_category(category: ProviderCategory | str) -> ProviderCategory:
    """将字符串类别转为 ProviderCategory 枚举。"""
    if isinstance(category, ProviderCategory):
        return category
    try:
        return ProviderCategory(str(category).upper())
    except ValueError as exc:
        raise ValueError(f"未知数据类别: {category!r}") from exc


class RawDataStore(_StoreBase):
    """原始 API 响应存储（append-only）。

    核心保证：
    - **完整保留**响应体，一个字段都不改；
    - **只追加**：永不 UPDATE、永不因标准化出错而修改；
    - **幂等**：通过 ``response_hash`` 跳过重复响应；
    - **凭据不入库**：请求头需由调用方脱敏后传入。

    Usage::

        store = RawDataStore()
        result = await store.store_raw(
            category="market",
            provider_name="binance",
            endpoint="/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            response_body={"symbol": "BTCUSDT", "price": "108000.00"},
            status_code=200,
            response_time_ms=42.5,
            symbol="BTCUSDT",
            data_type="PRICE",
        )
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        dedup_cache_size: int = 50_000,
        enable_db_dedup: bool = False,
    ) -> None:
        """初始化。

        Args:
            session_factory: 异步会话工厂
            batch_size: 批量写入分片大小
            dedup_cache_size: 进程内 response_hash LRU 去重缓存容量
            enable_db_dedup: 是否额外查询数据库做去重（更严格，但每次写入多一次查询）
        """
        super().__init__(session_factory=session_factory, batch_size=batch_size)
        self._dedup_cache = _BoundedHashCache(dedup_cache_size)
        self._enable_db_dedup = enable_db_dedup

    # ---- 公共接口 ----

    async def store_raw(
        self,
        category: ProviderCategory | str,
        provider_name: str,
        endpoint: str,
        params: dict[str, Any] | None,
        response_body: Any,
        status_code: int | None = None,
        response_time_ms: float | None = None,
        *,
        observation_time: datetime | None = None,
        response_headers: dict[str, Any] | None = None,
        quality_status: QualityStatus = QualityStatus.VERIFIED,
        session: AsyncSession | None = None,
        **identifiers: Any,
    ) -> RawStoreResult:
        """保存单条原始 API 响应。

        Args:
            category: 数据类别（market / onchain / etf / derivatives / macro / sentiment ...）
            provider_name: Provider 名称（用于解析 source_id 外键）
            endpoint: 请求端点路径
            params: 请求参数
            response_body: 完整原始响应体（不做任何修改）
            status_code: HTTP 状态码
            response_time_ms: 响应耗时（毫秒），写入元数据用于延迟审计
            observation_time: 数据观测时间（None 时使用当前时间）
            response_headers: 脱敏后的响应头
            quality_status: 落库时的质量状态标记
            session: 外部会话（用于与业务写入同事务提交）
            **identifiers: 各类别所需的业务标识字段
                （symbol / data_type / metric_name / ticker / exchange / series_id / source_type）

        Returns:
            RawStoreResult 写入结果
        """
        try:
            rows, result = await self._build_rows(
                category=category,
                provider_name=provider_name,
                endpoint=endpoint,
                params=params,
                response_body=response_body,
                status_code=status_code,
                response_time_ms=response_time_ms,
                observation_time=observation_time,
                response_headers=response_headers,
                quality_status=quality_status,
                session=session,
                identifiers=identifiers,
            )
        except ValueError as exc:
            logger.error(f"Raw 数据构造失败: {exc}")
            return RawStoreResult(success=False, error=str(exc))

        if not rows:
            return result

        try:
            async with self._session_scope(session) as db:
                model = RAW_TABLE_MAP[_coerce_category(category)]
                await db.execute(pg_insert(model), rows)
                if session is None:
                    await db.flush()
        except Exception as exc:  # noqa: BLE001 - 存储层不向上抛异常，返回失败结果
            logger.exception(f"Raw 数据写入失败 category={category} provider={provider_name}: {exc}")
            result.success = False
            result.stored = 0
            result.error = str(exc)
            return result

        result.stored = len(rows)
        logger.debug(
            f"Raw 数据已落库 category={category} provider={provider_name} "
            f"endpoint={endpoint} rows={len(rows)} hash={result.response_hash[:12] if result.response_hash else '-'}"
        )
        return result

    async def store_raw_batch(
        self,
        category: ProviderCategory | str,
        provider_name: str,
        endpoint: str,
        responses: Sequence[dict[str, Any]],
        *,
        session: AsyncSession | None = None,
    ) -> RawStoreResult:
        """批量保存原始响应（历史同步场景）。

        Args:
            category: 数据类别
            provider_name: Provider 名称
            endpoint: 请求端点
            responses: 响应描述列表，每项为 ``store_raw`` 的关键字参数字典，
                至少包含 ``response_body``，可选 ``params`` / ``observation_time`` /
                ``status_code`` / ``response_time_ms`` 及业务标识字段
            session: 外部会话

        Returns:
            汇总后的 RawStoreResult
        """
        aggregate = RawStoreResult(success=True, stored=0)
        if not responses:
            return aggregate

        enum_category = _coerce_category(category)
        model = RAW_TABLE_MAP[enum_category]
        all_rows: list[dict[str, Any]] = []

        async with self._session_scope(session) as db:
            source_id = await provider_id_resolver.resolve(db, provider_name)
            if source_id is None:
                aggregate.success = False
                aggregate.skipped_no_provider = len(responses)
                aggregate.error = f"Provider '{provider_name}' 未注册，无法写入 Raw 数据"
                return aggregate

            for item in responses:
                payload = dict(item)
                body = payload.pop("response_body", None)
                if body is None:
                    aggregate.skipped_duplicate += 0
                    continue
                identifiers = {
                    k: payload.pop(k)
                    for k in list(RAW_IDENTIFIER_FIELDS[enum_category])
                    if k in payload
                }
                row = self._make_row(
                    category=enum_category,
                    source_id=source_id,
                    endpoint=str(payload.get("endpoint", endpoint)),
                    params=payload.get("params"),
                    response_body=body,
                    status_code=payload.get("status_code"),
                    response_time_ms=payload.get("response_time_ms"),
                    observation_time=payload.get("observation_time"),
                    response_headers=payload.get("response_headers"),
                    quality_status=payload.get("quality_status", QualityStatus.VERIFIED),
                    identifiers=identifiers,
                )
                if row is None:
                    aggregate.skipped_duplicate += 1
                    continue
                all_rows.append(row)

            for batch in self._chunked(all_rows, self._batch_size):
                try:
                    await db.execute(pg_insert(model), list(batch))
                    aggregate.stored += len(batch)
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"Raw 批量写入失败 category={category}: {exc}")
                    aggregate.success = False
                    aggregate.error = str(exc)
                    return aggregate

        logger.info(
            f"Raw 批量落库完成 category={category} provider={provider_name} "
            f"stored={aggregate.stored} skipped_dup={aggregate.skipped_duplicate}"
        )
        return aggregate

    def clear_dedup_cache(self) -> None:
        """清空进程内去重缓存。"""
        self._dedup_cache.clear()

    # ---- 内部实现 ----

    async def _build_rows(
        self,
        *,
        category: ProviderCategory | str,
        provider_name: str,
        endpoint: str,
        params: dict[str, Any] | None,
        response_body: Any,
        status_code: int | None,
        response_time_ms: float | None,
        observation_time: datetime | None,
        response_headers: dict[str, Any] | None,
        quality_status: QualityStatus,
        session: AsyncSession | None,
        identifiers: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], RawStoreResult]:
        """校验并构造 Raw 行数据。返回 (行列表, 预填结果)。"""
        enum_category = _coerce_category(category)
        result = RawStoreResult(success=True)

        async with self._session_scope(session) as db:
            source_id = await provider_id_resolver.resolve(db, provider_name)

        if source_id is None:
            result.success = False
            result.skipped_no_provider = 1
            result.error = f"Provider '{provider_name}' 未注册，无法写入 Raw 数据"
            return [], result

        row = self._make_row(
            category=enum_category,
            source_id=source_id,
            endpoint=endpoint,
            params=params,
            response_body=response_body,
            status_code=status_code,
            response_time_ms=response_time_ms,
            observation_time=observation_time,
            response_headers=response_headers,
            quality_status=quality_status,
            identifiers=identifiers,
        )
        if row is None:
            result.skipped_duplicate = 1
            result.response_hash = compute_response_hash(response_body)
            return [], result

        result.response_hash = row["request_params"].get(_RESPONSE_HASH_KEY)
        return [row], result

    def _make_row(
        self,
        *,
        category: ProviderCategory,
        source_id: UUID,
        endpoint: str,
        params: dict[str, Any] | None,
        response_body: Any,
        status_code: int | None,
        response_time_ms: float | None,
        observation_time: datetime | None,
        response_headers: dict[str, Any] | None,
        quality_status: QualityStatus,
        identifiers: dict[str, Any],
    ) -> dict[str, Any] | None:
        """构造单行 Raw 数据；命中去重缓存时返回 None。"""
        response_hash = compute_response_hash(response_body)
        if response_hash in self._dedup_cache:
            logger.debug(f"Raw 响应重复，跳过落库 hash={response_hash[:12]} endpoint={endpoint}")
            return None

        model = RAW_TABLE_MAP[category]
        columns = {c.key for c in model.__table__.columns}

        # 请求参数 + 保留元数据（哈希 / 状态码 / 耗时），原始响应体保持不动
        request_params: dict[str, Any] = dict(params or {})
        request_params[_RESPONSE_HASH_KEY] = response_hash
        if status_code is not None:
            request_params["__http_status"] = status_code
        if response_time_ms is not None:
            request_params["__response_time_ms"] = round(float(response_time_ms), 3)

        # 业务标识字段校验（必填项缺失即拒绝写入）
        ident_values: dict[str, Any] = {}
        for field_name, default in RAW_IDENTIFIER_FIELDS[category].items():
            if field_name not in columns:
                continue
            value = identifiers.get(field_name, default)
            if value is None:
                raise ValueError(
                    f"Raw {category.value} 数据缺少必填标识字段 '{field_name}'，"
                    f"endpoint={endpoint}"
                )
            ident_values[field_name] = value

        headers = dict(response_headers or {})
        body_bytes = (
            response_body
            if isinstance(response_body, bytes)
            else json.dumps(response_body, ensure_ascii=False, default=str).encode("utf-8")
        )

        row: dict[str, Any] = {
            "id": uuid4(),
            "source_id": source_id,
            "observation_time": _ensure_utc(observation_time) or utcnow(),
            "fetch_time": utcnow(),
            "raw_response": response_body if not isinstance(response_body, bytes) else response_body.decode("utf-8", "replace"),
            "endpoint": endpoint,
            "request_params": request_params,
            "quality_status": quality_status,
        }
        if "response_headers" in columns:
            row["response_headers"] = headers
        if "response_size_bytes" in columns:
            row["response_size_bytes"] = len(body_bytes)
        row.update(ident_values)

        self._dedup_cache.add(response_hash)
        return {k: v for k, v in row.items() if k in columns}

    async def is_duplicate(
        self,
        category: ProviderCategory | str,
        provider_name: str,
        endpoint: str,
        observation_time: datetime,
        *,
        session: AsyncSession | None = None,
    ) -> bool:
        """数据库级重复检测（可选的严格去重）。

        Args:
            category: 数据类别
            provider_name: Provider 名称
            endpoint: 请求端点
            observation_time: 观测时间
            session: 外部会话

        Returns:
            是否已存在同源、同端点、同观测时间的 Raw 记录
        """
        if not self._enable_db_dedup:
            return False
        enum_category = _coerce_category(category)
        model = RAW_TABLE_MAP[enum_category]
        async with self._session_scope(session) as db:
            source_id = await provider_id_resolver.resolve(db, provider_name)
            if source_id is None:
                return False
            stmt = (
                select(model.id)
                .where(
                    model.source_id == source_id,
                    model.endpoint == endpoint,
                    model.observation_time == _ensure_utc(observation_time),
                )
                .limit(1)
            )
            return (await db.execute(stmt)).scalar_one_or_none() is not None


# ==========================================================================
# NormalizedDataStore
# ==========================================================================


class NormalizedDataStore(_StoreBase):
    """标准化数据批量写入（含 Write Gate 质量闸门）。

    写入流程（对应《10-data-quality.md》§1.2）::

        新数据 → Schema/范围校验 → 与已有数据对比 → 质量等级判定 → 剧变守卫
              → 通过: INSERT / UPDATE
              → 拒绝: 保留旧值，记录冲突（原始数据仍在 Raw 表）

    Usage::

        store = NormalizedDataStore()
        result = await store.store_prices([
            PriceRecord(source_id=pid, observation_time=ts, symbol="BTCUSDT", price=Decimal("108000")),
        ])
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        enable_write_gate: bool = True,
    ) -> None:
        """初始化。

        Args:
            session_factory: 异步会话工厂
            batch_size: 单批写入条数上限（默认 1000）
            enable_write_gate: 是否启用 Write Gate（关闭时退化为纯 UPSERT）
        """
        super().__init__(session_factory=session_factory, batch_size=batch_size)
        self._enable_write_gate = enable_write_gate

    # ---- 分类写入接口 ----

    async def store_prices(
        self, data: Sequence[PriceRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化价格数据（``market_prices``）。"""
        return await self._store(data, session=session)

    async def store_candles(
        self, data: Sequence[CandleRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化 K 线数据（``candles``）。"""
        return await self._store(data, session=session)

    async def store_onchain_metrics(
        self, data: Sequence[OnChainRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化链上指标（``onchain_metrics``）。"""
        return await self._store(data, session=session)

    async def store_etf_flows(
        self, data: Sequence[ETFRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化 ETF 资金流（``etf_flows``）。"""
        return await self._store(data, session=session)

    async def store_derivatives(
        self, data: Sequence[DerivativeRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化衍生品数据（``derivatives``）。"""
        return await self._store(data, session=session)

    async def store_macro(
        self, data: Sequence[MacroRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化宏观数据（``macro_series``）。"""
        return await self._store(data, session=session)

    async def store_sentiment(
        self, data: Sequence[SentimentRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """批量写入标准化情绪数据（``sentiment``）。"""
        return await self._store(data, session=session)

    async def store(
        self, data: Sequence[NormalizedRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """通用写入入口：按记录类型自动分组后分派到对应表。

        Args:
            data: 混合类型的标准化记录列表
            session: 外部会话

        Returns:
            汇总后的写入结果
        """
        aggregate = StoreResult()
        if not data:
            return aggregate

        grouped: dict[type[NormalizedRecord], list[NormalizedRecord]] = {}
        for record in data:
            grouped.setdefault(type(record), []).append(record)

        for record_cls, records in grouped.items():
            result = await self._store(records, session=session)
            aggregate.merge(result)
            logger.debug(f"{record_cls.RECORD_NAME} 写入完成: {result.as_dict()}")
        return aggregate

    # ---- 核心写入实现 ----

    async def _store(
        self, data: Sequence[NormalizedRecord], *, session: AsyncSession | None = None
    ) -> StoreResult:
        """统一的批量写入实现（校验 → 分片 → 去重 → Write Gate → 落库）。"""
        started = utcnow()
        result = StoreResult(total=len(data))
        if not data:
            return result

        record_cls = type(data[0])
        if record_cls.MODEL is None:
            raise ValueError(f"{record_cls.__name__} 未声明目标 ORM 模型")

        # ---- 第 1 步：有效性校验（Schema + 物理范围），不合格数据拒绝入库 ----
        valid_rows: list[dict[str, Any]] = []
        for record in data:
            row = record.to_row()
            problems = check_row_validity(row)
            if record.quality_status is QualityStatus.INVALID:
                problems.append(record.metadata.get("invalid_reason", "记录被标记为 INVALID"))
            if problems:
                result.rejected_invalid += 1
                result.invalid_details.append(
                    {
                        "key": [str(v) for v in record.key()],
                        "problems": problems,
                        "provider_id": str(record.source_id),
                    }
                )
                continue
            valid_rows.append(row)

        if result.rejected_invalid:
            logger.warning(
                f"{record_cls.RECORD_NAME} 有效性校验拒绝 {result.rejected_invalid} 条记录 "
                f"(示例: {result.invalid_details[0]['problems']})"
            )

        if not valid_rows:
            result.duration_ms = (utcnow() - started).total_seconds() * 1000
            return result

        # ---- 第 2~4 步：分片写入 ----
        async with self._session_scope(session) as db:
            for batch in self._chunked(valid_rows, self._batch_size):
                batch_result = await self._write_batch(db, record_cls, list(batch))
                result.merge(batch_result)
                result.batch_count += 1

        result.duration_ms = (utcnow() - started).total_seconds() * 1000
        logger.info(
            f"{record_cls.RECORD_NAME} 批量写入完成: total={result.total} "
            f"inserted={result.inserted} updated={result.updated} "
            f"dup={result.skipped_duplicate} invalid={result.rejected_invalid} "
            f"gated={result.rejected_write_gate} batches={result.batch_count} "
            f"耗时={result.duration_ms:.1f}ms"
        )
        return result

    async def _write_batch(
        self,
        session: AsyncSession,
        record_cls: type[NormalizedRecord],
        rows: list[dict[str, Any]],
    ) -> StoreResult:
        """写入单个批次：查询已有数据 → 分流 insert / update → 执行。"""
        result = StoreResult(total=len(rows))
        model = record_cls.MODEL
        key_columns = record_cls.KEY_COLUMNS

        existing = await self._fetch_existing(session, model, key_columns, rows)

        to_insert: list[dict[str, Any]] = []
        to_update: list[tuple[dict[str, Any], dict[str, Any]]] = []

        for row in rows:
            key = tuple(row.get(col) for col in key_columns)
            old = existing.get(key)
            if old is None:
                to_insert.append(row)
                continue

            decision = self._evaluate_write_gate(record_cls, row, old)
            if decision["action"] == "skip":
                result.skipped_duplicate += 1
            elif decision["action"] == "update":
                to_update.append((row, old))
                result.updated += 1
            else:
                result.rejected_write_gate += 1
                result.conflicts.append(decision["conflict"])

        # ---- INSERT（带 ON CONFLICT DO NOTHING 防并发竞态）----
        if to_insert:
            stmt = pg_insert(model)
            if record_cls.CONFLICT_INDEX:
                stmt = stmt.on_conflict_do_nothing(index_elements=list(record_cls.CONFLICT_INDEX))
            else:
                stmt = stmt.on_conflict_do_nothing()
            try:
                await session.execute(stmt, to_insert)
                result.inserted += len(to_insert)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"{record_cls.RECORD_NAME} 批量 INSERT 失败: {exc}")
                await session.rollback()
                raise

        # ---- UPDATE（仅 Write Gate 放行的记录）----
        for row, old in to_update:
            values = {
                k: v
                for k, v in row.items()
                if k not in ("id", *key_columns) and v is not None
            }
            if not values:
                continue
            values["updated_at"] = utcnow()
            conditions = [getattr(model, col) == row[col] for col in key_columns]
            try:
                await session.execute(update(model).where(*conditions).values(**values))
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"{record_cls.RECORD_NAME} UPDATE 失败 key={row}: {exc}")
                await session.rollback()
                raise

        return result

    async def _fetch_existing(
        self,
        session: AsyncSession,
        model: type[Any],
        key_columns: tuple[str, ...],
        rows: list[dict[str, Any]],
    ) -> dict[tuple[Any, ...], dict[str, Any]]:
        """按批次键范围查询已存在记录，返回 key → 行字典 的映射。

        使用 ``source_id IN (...)`` + ``observation_time BETWEEN ...`` 的范围条件
        命中索引，避免为上千个键构造 OR 表达式。
        """
        if not rows:
            return {}

        select_columns = list(key_columns)
        guard_columns = [c for c in ("quality_status",) if hasattr(model, c)]
        time_column = getattr(model, "observation_time", None)
        times = [r.get("observation_time") for r in rows if r.get("observation_time") is not None]

        conditions = []
        if hasattr(model, "source_id"):
            source_ids = {r["source_id"] for r in rows if r.get("source_id") is not None}
            if source_ids:
                conditions.append(model.source_id.in_(source_ids))
        if time_column is not None and times:
            conditions.append(time_column.between(min(times), max(times)))

        # 无 source_id 列的表（如 candles 以 symbol/interval 为键）退化为时间范围过滤
        if not conditions and time_column is not None and times:
            conditions.append(time_column.between(min(times), max(times)))
        if not conditions:
            return {}

        # 需要参与 Write Gate 判定的列
        value_columns = {c.key for c in model.__table__.columns}
        fetch_columns = sorted(
            set(select_columns) | set(guard_columns) | (value_columns & set(_GUARD_FETCH_COLUMNS))
        )
        fetch_columns = [c for c in fetch_columns if hasattr(model, c)]

        stmt = select(*[getattr(model, c) for c in fetch_columns]).where(*conditions)
        try:
            rows_result = await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"查询已有 {model.__tablename__} 记录失败，退化为全量插入: {exc}")
            return {}

        existing: dict[tuple[Any, ...], dict[str, Any]] = {}
        for record in rows_result.all():
            mapping = dict(zip(fetch_columns, record, strict=False))
            key = tuple(mapping.get(col) for col in key_columns)
            existing[key] = mapping
        return existing

    def _evaluate_write_gate(
        self,
        record_cls: type[NormalizedRecord],
        new_row: dict[str, Any],
        old_row: dict[str, Any],
    ) -> dict[str, Any]:
        """Write Gate 判定：决定对已存在记录执行 skip / update / reject。

        规则（《10-data-quality.md》§1.2）：
        1. 新数据质量等级 **低于** 已有数据 → 拒绝覆盖（保留旧值）；
        2. 剧变守卫字段偏差超过阈值 **且** 新数据为单源（未交叉验证）→ 拒绝覆盖并记录冲突；
        3. 数值完全一致 → skip（幂等，不产生无意义 UPDATE）；
        4. 其余情况 → update。

        Args:
            record_cls: 记录类型（提供守卫字段与阈值）
            new_row: 新数据行
            old_row: 已有数据行

        Returns:
            ``{"action": "skip"|"update"|"reject", "conflict": dict|None}``
        """
        if not self._enable_write_gate:
            return {"action": "update", "conflict": None}

        # 规则 3：值完全一致 → 幂等跳过
        comparable = [
            k
            for k in new_row
            if k not in ("id", "created_at", "updated_at", "fetch_time", "metadata_")
        ]
        if all(_values_equal(new_row.get(k), old_row.get(k)) for k in comparable):
            return {"action": "skip", "conflict": None}

        new_status = _as_quality_status(new_row.get("quality_status"))
        old_status = _as_quality_status(old_row.get("quality_status"))

        # 规则 1：低质量不得覆盖高质量
        if QUALITY_RANK[new_status] < QUALITY_RANK[old_status]:
            return {
                "action": "reject",
                "conflict": {
                    "reason": "LOW_QUALITY_OVERWRITE_BLOCKED",
                    "table": record_cls.MODEL.__tablename__,
                    "key": {k: str(new_row.get(k)) for k in record_cls.KEY_COLUMNS},
                    "old_quality": old_status.value,
                    "new_quality": new_status.value,
                    "old_values": _snapshot(old_row, record_cls.GUARD_COLUMNS),
                    "new_values": _snapshot(new_row, record_cls.GUARD_COLUMNS),
                    "detected_at": utcnow().isoformat(),
                },
            }

        # 规则 2：剧变守卫（单源大幅偏离 → 保留旧值）
        is_multi_source = bool(new_row.get("cross_validated")) or int(
            new_row.get("validation_sources") or 1
        ) >= 2
        if record_cls.GUARD_COLUMNS and not is_multi_source:
            for column in record_cls.GUARD_COLUMNS:
                new_value = _to_decimal(new_row.get(column))
                old_value = _to_decimal(old_row.get(column))
                if new_value is None or old_value is None or old_value == 0:
                    continue
                deviation_pct = abs((new_value - old_value) / old_value) * Decimal("100")
                if deviation_pct > record_cls.GUARD_MAX_JUMP_PCT:
                    return {
                        "action": "reject",
                        "conflict": {
                            "reason": "EXTREME_DEVIATION_SINGLE_SOURCE",
                            "table": record_cls.MODEL.__tablename__,
                            "key": {k: str(new_row.get(k)) for k in record_cls.KEY_COLUMNS},
                            "column": column,
                            "old_value": str(old_value),
                            "new_value": str(new_value),
                            "deviation_pct": str(deviation_pct.quantize(Decimal("0.0001"))),
                            "threshold_pct": str(record_cls.GUARD_MAX_JUMP_PCT),
                            "old_quality": old_status.value,
                            "new_quality": new_status.value,
                            "detected_at": utcnow().isoformat(),
                            "note": "真实极端行情需 ≥2 个独立 Provider 确认方可放行",
                        },
                    }

        return {"action": "update", "conflict": None}


#: Write Gate 判定时需要从数据库额外取回的列
_GUARD_FETCH_COLUMNS = frozenset(
    {
        "quality_status",
        "cross_validated",
        "validation_sources",
        "price",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "value",
        "net_flow_usd",
        "funding_rate",
        "open_interest",
    }
)


def _as_quality_status(value: Any) -> QualityStatus:
    """将任意形式的质量状态转为枚举（无法识别时视为最低质量）。"""
    if isinstance(value, QualityStatus):
        return value
    if isinstance(value, str):
        try:
            return QualityStatus(value.upper())
        except ValueError:
            return QualityStatus.INVALID
    return QualityStatus.INVALID


def _values_equal(left: Any, right: Any) -> bool:
    """数值/时间感知的相等比较（Decimal 与 str、naive 与 aware datetime 兼容）。"""
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    if isinstance(left, Decimal) or isinstance(right, Decimal):
        left_dec, right_dec = _to_decimal(left), _to_decimal(right)
        if left_dec is not None and right_dec is not None:
            return left_dec == right_dec
    if isinstance(left, datetime) and isinstance(right, datetime):
        return _ensure_utc(left) == _ensure_utc(right)
    if isinstance(left, date) and isinstance(right, date):
        return left == right
    return left == right


def _snapshot(row: dict[str, Any], columns: tuple[str, ...]) -> dict[str, str]:
    """提取指定列的字符串快照（用于冲突记录）。"""
    return {col: str(row.get(col)) for col in columns if row.get(col) is not None}


# ---- 模块级便捷单例 ----

_raw_store: RawDataStore | None = None
_normalized_store: NormalizedDataStore | None = None


def get_raw_data_store() -> RawDataStore:
    """获取全局 RawDataStore 单例。"""
    global _raw_store
    if _raw_store is None:
        _raw_store = RawDataStore()
    return _raw_store


def get_normalized_data_store() -> NormalizedDataStore:
    """获取全局 NormalizedDataStore 单例。"""
    global _normalized_store
    if _normalized_store is None:
        _normalized_store = NormalizedDataStore()
    return _normalized_store


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "PRECISION_BTC",
    "PRECISION_RATIO",
    "PRECISION_USD",
    "PRECISION_VOLUME",
    "QUALITY_RANK",
    "CandleRecord",
    "DerivativeRecord",
    "ETFRecord",
    "MacroRecord",
    "NormalizedDataStore",
    "NormalizedRecord",
    "OnChainRecord",
    "PriceRecord",
    "ProviderIdResolver",
    "RawDataStore",
    "RawStoreResult",
    "SentimentRecord",
    "StoreResult",
    "check_row_validity",
    "compute_response_hash",
    "get_normalized_data_store",
    "get_raw_data_store",
    "provider_id_resolver",
    "utcnow",
]
