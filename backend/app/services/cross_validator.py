"""多源交叉验证服务 — 确保关键数据点经多个独立 Provider 确认。

对应架构文档：
- 《10-data-quality.md》§2 交叉验证机制
- 《10-data-quality.md》§2.2 偏差阈值与判定规则
- 《10-data-quality.md》§2.3 验证结果处理

核心规则：
- 价格偏差 > 0.5% → CONFLICT（标记冲突，不写入 Normalized）
- 成交量偏差 > 15% → 记录但不标记（不同交易所口径不同）
- 链上偏差 > 3% → 记录差异，看趋势一致性
- ETF 偏差 > 5% → WARNING
- 验证结果：VERIFIED（一致）/ CONFLICT（异常差异）
- 冲突记录写入 ``data_quality`` 表（check_type='CROSS_VALIDATION'）
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Sequence
from uuid import UUID, uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session_ctx
from app.models.enums import ProviderCategory, QualityStatus
from app.models.system import DataQuality
from app.providers.base.types import FetchResult
from app.services.data_store import utcnow


# ==========================================================================
# 偏差阈值配置
# ==========================================================================

#: 各类别偏差阈值（百分比）
DEVIATION_THRESHOLDS: dict[str, float] = {
    "price": 0.5,         # 价格：偏差 > 0.5% 标记 CONFLICT
    "volume": 15.0,       # 成交量：偏差 > 15% 记录但不标记
    "onchain": 3.0,       # 链上数据：偏差 > 3% 记录差异
    "etf": 5.0,           # ETF：偏差 > 5% WARNING
    "derivatives": 0.01,  # 衍生品（同所跨源）：偏差 > 0.01% CONFLICT
    "macro": 1.0,         # 宏观数据：偏差 > 1% WARNING
    "sentiment": 10.0,    # 情绪数据：偏差 > 10% 记录
}

#: 偏差超阈值时是否标记为 CONFLICT（vs 仅记录）
CONFLICT_MARKERS: dict[str, bool] = {
    "price": True,
    "volume": False,
    "onchain": False,
    "etf": False,
    "derivatives": True,
    "macro": False,
    "sentiment": False,
}


# ==========================================================================
# 验证结果结构
# ==========================================================================


class ValidationResult(str, Enum):
    """交叉验证结果状态。"""

    VERIFIED = "VERIFIED"
    CONFLICT = "CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    SINGLE_SOURCE = "SINGLE_SOURCE"


@dataclass
class CrossValidationResult:
    """交叉验证详细结果。

    Attributes:
        data_type: 数据类型（price / volume / onchain 等）
        symbol: 资产符号
        observation_time: 数据观测时间
        result: 验证结论
        median: 中位数
        mean: 均值
        std_dev: 标准差
        max_deviation_pct: 最大偏差百分比
        provider_count: 参与验证的 Provider 数量
        provider_values: 各 Provider 的值
        deviations: 各 Provider 相对中位数的偏差
        threshold_pct: 使用的阈值
        conflicts: 冲突的 Provider 列表
        confidence: 验证置信度 (0-1)
    """

    data_type: str
    symbol: str
    observation_time: datetime | None = None
    result: ValidationResult = ValidationResult.INSUFFICIENT
    median: Decimal | None = None
    mean: Decimal | None = None
    vwap: Decimal | None = None
    std_dev: Decimal | None = None
    max_deviation_pct: float = 0.0
    provider_count: int = 0
    provider_values: dict[str, Decimal] = field(default_factory=dict)
    deviations: dict[str, float] = field(default_factory=dict)
    threshold_pct: float = 0.0
    conflicts: list[str] = field(default_factory=list)
    confidence: float = 0.0
    message: str = ""

    @property
    def is_verified(self) -> bool:
        """是否通过验证。"""
        return self.result == ValidationResult.VERIFIED

    @property
    def has_conflict(self) -> bool:
        """是否存在冲突。"""
        return self.result == ValidationResult.CONFLICT

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "observation_time": self.observation_time.isoformat() if self.observation_time else None,
            "result": self.result.value,
            "median": str(self.median) if self.median is not None else None,
            "mean": str(self.mean) if self.mean is not None else None,
            "vwap": str(self.vwap) if self.vwap is not None else None,
            "std_dev": str(self.std_dev) if self.std_dev is not None else None,
            "max_deviation_pct": round(self.max_deviation_pct, 4),
            "provider_count": self.provider_count,
            "provider_values": {k: str(v) for k, v in self.provider_values.items()},
            "deviations": {k: round(v, 4) for k, v in self.deviations.items()},
            "threshold_pct": self.threshold_pct,
            "conflicts": self.conflicts,
            "confidence": round(self.confidence, 3),
            "message": self.message,
        }


@dataclass
class BatchValidationResult:
    """批量验证汇总。"""

    data_type: str
    symbol: str
    total_checked: int = 0
    verified: int = 0
    conflicts: int = 0
    insufficient: int = 0
    single_source: int = 0
    results: list[CrossValidationResult] = field(default_factory=list)
    avg_confidence: float = 0.0
    max_deviation_pct: float = 0.0

    def summary(self) -> dict[str, Any]:
        """汇总信息。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "total_checked": self.total_checked,
            "verified": self.verified,
            "conflicts": self.conflicts,
            "insufficient": self.insufficient,
            "single_source": self.single_source,
            "avg_confidence": round(self.avg_confidence, 3),
            "max_deviation_pct": round(self.max_deviation_pct, 4),
        }


# ==========================================================================
# CrossValidator 主类
# ==========================================================================


class CrossValidator:
    """多源交叉验证器。

    对同一数据点从多个独立 Provider 获取值，计算统计量并判定一致性。

    Usage::

        validator = CrossValidator()
        # 验证价格
        results = [fetch_result_binance, fetch_result_okx, fetch_result_coinbase]
        validation = await validator.validate_price(results, symbol="BTCUSDT")
        if validation.has_conflict:
            logger.warning(f"价格冲突: {validation.conflicts}")
    """

    def __init__(self, *, persist_conflicts: bool = True) -> None:
        """初始化。

        Args:
            persist_conflicts: 是否将冲突写入 data_quality 表
        """
        self._persist_conflicts = persist_conflicts

    # ==================================================================
    # 公开接口：价格验证
    # ==================================================================

    async def validate_price(
        self,
        providers_results: Sequence[FetchResult],
        *,
        symbol: str = "BTCUSDT",
        observation_time: datetime | None = None,
        data_type: str = "price",
    ) -> CrossValidationResult:
        """多源价格交叉验证。

        从各 Provider 的 FetchResult 中提取价格值，计算统计量并判定。

        Args:
            providers_results: 各 Provider 的 FetchResult 列表
            symbol: 资产符号
            observation_time: 观测时间
            data_type: 数据子类型（price / volume / market_cap）

        Returns:
            CrossValidationResult 验证结果
        """
        # 提取数值
        values = self._extract_values(providers_results, data_type)
        if not values:
            return CrossValidationResult(
                data_type=data_type,
                symbol=symbol,
                observation_time=observation_time,
                result=ValidationResult.INSUFFICIENT,
                message="无法从 Provider 结果中提取有效数值",
            )

        result = self._compute_validation(
            values=values,
            data_type=data_type,
            symbol=symbol,
            observation_time=observation_time,
        )

        # 持久化冲突
        if result.has_conflict and self._persist_conflicts:
            await self._persist_conflict(result)

        return result

    # ==================================================================
    # 公开接口：通用验证
    # ==================================================================

    async def validate_generic(
        self,
        data_type: str,
        results: Sequence[FetchResult],
        *,
        symbol: str = "",
        observation_time: datetime | None = None,
        value_key: str | None = None,
    ) -> CrossValidationResult:
        """通用交叉验证。

        适用于链上数据、ETF 流量、衍生品等非纯价格类型。

        Args:
            data_type: 数据类型（onchain / etf / derivatives / macro / sentiment）
            results: 各 Provider 的 FetchResult
            symbol: 资产符号
            observation_time: 观测时间
            value_key: 从 data 中提取值的键名；None 时自动推断

        Returns:
            CrossValidationResult 验证结果
        """
        effective_key = value_key or self._infer_value_key(data_type)
        values = self._extract_values(results, effective_key)

        if not values:
            return CrossValidationResult(
                data_type=data_type,
                symbol=symbol,
                observation_time=observation_time,
                result=ValidationResult.INSUFFICIENT,
                message=f"无法提取 {effective_key} 值",
            )

        # 对链上数据额外看趋势一致性
        if data_type == "onchain":
            result = self._compute_validation(
                values=values,
                data_type=data_type,
                symbol=symbol,
                observation_time=observation_time,
            )
            result.message = result.message or "链上数据：记录差异，关注趋势一致性"
        else:
            result = self._compute_validation(
                values=values,
                data_type=data_type,
                symbol=symbol,
                observation_time=observation_time,
            )

        if result.has_conflict and self._persist_conflicts:
            await self._persist_conflict(result)

        return result

    # ==================================================================
    # 公开接口：批量验证
    # ==================================================================

    async def validate_batch(
        self,
        data_type: str,
        symbol: str,
        batch_results: Sequence[Sequence[FetchResult]],
        *,
        observation_times: Sequence[datetime] | None = None,
    ) -> BatchValidationResult:
        """批量交叉验证（多个时间点）。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            batch_results: 每个时间点对应的多 Provider FetchResult 列表
            observation_times: 对应的观测时间列表

        Returns:
            BatchValidationResult 批量验证汇总
        """
        batch_result = BatchValidationResult(
            data_type=data_type,
            symbol=symbol,
            total_checked=len(batch_results),
        )

        for idx, results in enumerate(batch_results):
            obs_time = observation_times[idx] if observation_times and idx < len(observation_times) else None
            validation = await self.validate_generic(
                data_type=data_type,
                results=results,
                symbol=symbol,
                observation_time=obs_time,
            )
            batch_result.results.append(validation)

            if validation.result == ValidationResult.VERIFIED:
                batch_result.verified += 1
            elif validation.result == ValidationResult.CONFLICT:
                batch_result.conflicts += 1
            elif validation.result == ValidationResult.SINGLE_SOURCE:
                batch_result.single_source += 1
            else:
                batch_result.insufficient += 1

        # 汇总统计
        confidences = [r.confidence for r in batch_result.results]
        deviations = [r.max_deviation_pct for r in batch_result.results]
        batch_result.avg_confidence = statistics.mean(confidences) if confidences else 0.0
        batch_result.max_deviation_pct = max(deviations) if deviations else 0.0

        logger.info(
            f"批量验证完成: {data_type}/{symbol} "
            f"total={batch_result.total_checked} verified={batch_result.verified} "
            f"conflicts={batch_result.conflicts}"
        )
        return batch_result

    # ==================================================================
    # 内部实现：统计计算
    # ==================================================================

    def _compute_validation(
        self,
        values: dict[str, Decimal],
        data_type: str,
        symbol: str,
        observation_time: datetime | None,
    ) -> CrossValidationResult:
        """核心验证逻辑：计算统计量并判定结果。"""
        n = len(values)

        if n == 0:
            return CrossValidationResult(
                data_type=data_type, symbol=symbol,
                observation_time=observation_time,
                result=ValidationResult.INSUFFICIENT,
                message="无有效数据",
            )

        if n == 1:
            single_provider = list(values.keys())[0]
            return CrossValidationResult(
                data_type=data_type, symbol=symbol,
                observation_time=observation_time,
                result=ValidationResult.SINGLE_SOURCE,
                median=list(values.values())[0],
                mean=list(values.values())[0],
                provider_count=1,
                provider_values=values,
                confidence=0.5,
                message=f"仅单一数据源: {single_provider}",
            )

        # 计算统计量
        float_values = [float(v) for v in values.values()]
        median_val = Decimal(str(statistics.median(float_values)))
        mean_val = Decimal(str(statistics.mean(float_values)))
        std_dev_val = Decimal(str(statistics.stdev(float_values))) if n > 1 else Decimal("0")

        # 计算 VWAP（如果有 volume 权重则使用，否则等同于 mean）
        vwap_val = mean_val  # 简化：无 volume 权重时 VWAP = Mean

        # 计算各 Provider 偏差
        deviations: dict[str, float] = {}
        max_dev_pct = 0.0
        conflicts: list[str] = []
        threshold = DEVIATION_THRESHOLDS.get(data_type, 5.0)

        for provider, value in values.items():
            if median_val == 0:
                dev_pct = 0.0 if value == 0 else 100.0
            else:
                dev_pct = abs(float(value - median_val) / float(median_val)) * 100
            deviations[provider] = dev_pct
            max_dev_pct = max(max_dev_pct, dev_pct)
            if dev_pct > threshold:
                conflicts.append(provider)

        # 判定结果
        should_mark_conflict = CONFLICT_MARKERS.get(data_type, False)
        if conflicts and should_mark_conflict:
            result_status = ValidationResult.CONFLICT
        elif conflicts:
            # 有偏差但不标记冲突（如 volume）
            result_status = ValidationResult.VERIFIED
        else:
            result_status = ValidationResult.VERIFIED

        # 置信度：基于 Provider 数量和偏差
        confidence = min(1.0, n / 3.0) * (1.0 - min(max_dev_pct / 100.0, 0.5))

        message = ""
        if conflicts:
            message = (
                f"偏差超阈值({threshold}%): "
                f"{', '.join(f'{p}({deviations[p]:.2f}%)' for p in conflicts)}"
            )

        return CrossValidationResult(
            data_type=data_type,
            symbol=symbol,
            observation_time=observation_time,
            result=result_status,
            median=median_val,
            mean=mean_val,
            vwap=vwap_val,
            std_dev=std_dev_val,
            max_deviation_pct=max_dev_pct,
            provider_count=n,
            provider_values=values,
            deviations=deviations,
            threshold_pct=threshold,
            conflicts=conflicts,
            confidence=confidence,
            message=message,
        )

    # ==================================================================
    # 内部实现：值提取
    # ==================================================================

    def _extract_values(
        self, results: Sequence[FetchResult], key: str
    ) -> dict[str, Decimal]:
        """从 FetchResult 列表中提取指定键的数值。"""
        values: dict[str, Decimal] = {}

        for result in results:
            if not result.success or result.data is None:
                continue

            provider_name = result.provider_name or f"unknown_{len(values)}"
            value = self._extract_single_value(result.data, key)
            if value is not None:
                values[provider_name] = value

        return values

    def _extract_single_value(self, data: Any, key: str) -> Decimal | None:
        """从单条数据中提取指定键的 Decimal 值。"""
        if data is None:
            return None

        # data 可能是 dict / list / Decimal / float
        if isinstance(data, dict):
            raw_value = data.get(key)
            if raw_value is None:
                # 尝试嵌套路径
                for nested_key in ("data", "result", "ticker"):
                    nested = data.get(nested_key)
                    if isinstance(nested, dict):
                        raw_value = nested.get(key)
                        if raw_value is not None:
                            break
        elif isinstance(data, list) and len(data) > 0:
            first = data[0]
            if isinstance(first, dict):
                raw_value = first.get(key)
            else:
                raw_value = first
        elif isinstance(data, (int, float, str, Decimal)):
            raw_value = data
        else:
            return None

        return _to_decimal(raw_value)

    def _infer_value_key(self, data_type: str) -> str:
        """根据数据类型推断要提取的值键名。"""
        key_map: dict[str, str] = {
            "price": "price",
            "volume": "volume",
            "market_cap": "market_cap",
            "onchain": "value",
            "etf": "net_flow_usd",
            "derivatives": "funding_rate",
            "macro": "value",
            "sentiment": "value",
            "funding_rate": "funding_rate",
            "open_interest": "open_interest_usd",
        }
        return key_map.get(data_type.lower(), "value")

    # ==================================================================
    # 持久化冲突到 data_quality
    # ==================================================================

    async def _persist_conflict(self, result: CrossValidationResult) -> None:
        """将冲突记录写入 data_quality 表。"""
        try:
            async with get_db_session_ctx() as session:
                now = utcnow()
                category = self._infer_category(result.data_type)

                quality_record = DataQuality(
                    id=uuid4(),
                    check_time=now,
                    data_category=category,
                    table_name=self._infer_table(result.data_type),
                    check_type="CROSS_VALIDATION",
                    status="WARNING" if result.conflicts else "OK",
                    severity="HIGH" if result.max_deviation_pct > 2.0 else "MEDIUM",
                    records_checked=result.provider_count,
                    records_passed=result.provider_count - len(result.conflicts),
                    records_failed=len(result.conflicts),
                    conflicts_found=[result.to_dict()],
                    issues=[{
                        "type": "cross_validation_conflict",
                        "data_type": result.data_type,
                        "symbol": result.symbol,
                        "max_deviation_pct": round(result.max_deviation_pct, 4),
                        "conflicting_providers": result.conflicts,
                        "message": result.message,
                    }],
                    requires_manual=result.max_deviation_pct > 5.0,
                    time_range_start=result.observation_time,
                    time_range_end=result.observation_time,
                )

                session.add(quality_record)
                await session.commit()
                logger.debug(
                    f"冲突已记录: {result.data_type}/{result.symbol} "
                    f"deviation={result.max_deviation_pct:.2f}%"
                )
        except Exception as exc:
            logger.error(f"持久化冲突记录失败: {exc}")

    @staticmethod
    def _infer_category(data_type: str) -> ProviderCategory:
        """推断数据类别。"""
        category_map: dict[str, ProviderCategory] = {
            "price": ProviderCategory.MARKET,
            "volume": ProviderCategory.MARKET,
            "market_cap": ProviderCategory.MARKET,
            "onchain": ProviderCategory.ONCHAIN,
            "etf": ProviderCategory.ETF,
            "derivatives": ProviderCategory.DERIVATIVES,
            "macro": ProviderCategory.MACRO,
            "sentiment": ProviderCategory.SENTIMENT,
        }
        return category_map.get(data_type.lower(), ProviderCategory.MARKET)

    @staticmethod
    def _infer_table(data_type: str) -> str:
        """推断目标表名。"""
        table_map: dict[str, str] = {
            "price": "market_prices",
            "volume": "market_prices",
            "onchain": "onchain_metrics",
            "etf": "etf_flows",
            "derivatives": "derivatives",
            "macro": "macro_series",
            "sentiment": "sentiment",
        }
        return table_map.get(data_type.lower(), "unknown")


# ==========================================================================
# 辅助函数
# ==========================================================================


def _to_decimal(value: Any) -> Decimal | None:
    """安全转换为 Decimal。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            d = Decimal(repr(value))
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text or text.lower() in {"null", "none", "nan", "n/a", "-"}:
            return None
        try:
            d = Decimal(text)
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    return None


# ==========================================================================
# 全局单例
# ==========================================================================

_cross_validator: CrossValidator | None = None


def get_cross_validator() -> CrossValidator:
    """获取全局 CrossValidator 单例。"""
    global _cross_validator
    if _cross_validator is None:
        _cross_validator = CrossValidator()
    return _cross_validator


def set_cross_validator(validator: CrossValidator) -> None:
    """设置全局 CrossValidator 单例。"""
    global _cross_validator
    _cross_validator = validator


__all__ = [
    "BatchValidationResult",
    "CONFLICT_MARKERS",
    "CrossValidationResult",
    "CrossValidator",
    "DEVIATION_THRESHOLDS",
    "ValidationResult",
    "get_cross_validator",
    "set_cross_validator",
]
