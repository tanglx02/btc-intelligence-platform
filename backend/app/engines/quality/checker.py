"""数据质量检查器 — 范围检查、异常值检测、连续性检查、重复数据检测。

对应架构文档：
- 《10-data-quality.md》§1.2 Write Gate 检查逻辑
- 《10-data-quality.md》§4 质量维度（准确性、完整性、有效性）

检查类型：
1. **范围检查**：价格 > 0、百分比 -100~100、volume >= 0 等物理约束
2. **异常值检测**：Z-score > 3 标记为异常；IQR 方法辅助判定
3. **连续性检查**：时间序列无跳跃（检测间隔超过 2× 期望间隔的点）
4. **重复数据检测**：相同 (source_id, observation_time, symbol) 的重复记录
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from loguru import logger


# ==========================================================================
# 检查结果结构
# ==========================================================================


@dataclass
class CheckIssue:
    """单条检查问题。"""

    check_type: str  # RANGE / OUTLIER / CONTINUITY / DUPLICATE / VALIDITY
    severity: str  # INFO / LOW / MEDIUM / HIGH / CRITICAL
    field_name: str = ""
    value: Any = None
    expected: str = ""
    message: str = ""
    timestamp: datetime | None = None
    record_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "check_type": self.check_type,
            "severity": self.severity,
            "field_name": self.field_name,
            "value": str(self.value) if self.value is not None else None,
            "expected": self.expected,
            "message": self.message,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "record_id": self.record_id,
        }


@dataclass
class CheckResult:
    """单项检查的结果汇总。"""

    check_type: str
    records_checked: int = 0
    records_passed: int = 0
    records_failed: int = 0
    issues: list[CheckIssue] = field(default_factory=list)
    score: float = 100.0  # 0-100，该项检查的得分
    message: str = ""

    @property
    def pass_rate(self) -> float:
        """通过率。"""
        if self.records_checked == 0:
            return 1.0
        return self.records_passed / self.records_checked

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "check_type": self.check_type,
            "records_checked": self.records_checked,
            "records_passed": self.records_passed,
            "records_failed": self.records_failed,
            "pass_rate": round(self.pass_rate * 100, 2),
            "score": round(self.score, 2),
            "issue_count": len(self.issues),
            "issues": [i.to_dict() for i in self.issues[:20]],
            "message": self.message,
        }


@dataclass
class DataCheckReport:
    """完整的数据质量检查报告。"""

    data_type: str
    symbol: str
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    total_records: int = 0
    checks: list[CheckResult] = field(default_factory=list)
    overall_score: float = 100.0
    overall_status: str = "OK"
    critical_issues: int = 0
    high_issues: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "time_range_start": self.time_range_start.isoformat() if self.time_range_start else None,
            "time_range_end": self.time_range_end.isoformat() if self.time_range_end else None,
            "total_records": self.total_records,
            "overall_score": round(self.overall_score, 2),
            "overall_status": self.overall_status,
            "critical_issues": self.critical_issues,
            "high_issues": self.high_issues,
            "checks": [c.to_dict() for c in self.checks],
        }


# ==========================================================================
# 范围检查规则
# ==========================================================================

#: 字段范围定义（最小值, 最大值, 是否可等于边界）
RANGE_DEFINITIONS: dict[str, tuple[Decimal | None, Decimal | None]] = {
    # 价格类：必须 > 0
    "price": (Decimal("0"), None),
    "open": (Decimal("0"), None),
    "high": (Decimal("0"), None),
    "low": (Decimal("0"), None),
    "close": (Decimal("0"), None),
    "bid": (Decimal("0"), None),
    "ask": (Decimal("0"), None),
    "vwap": (Decimal("0"), None),
    "mid_price": (Decimal("0"), None),
    # 数量/金额：>= 0
    "volume": (Decimal("0"), None),
    "volume_24h": (Decimal("0"), None),
    "quote_volume": (Decimal("0"), None),
    "quote_volume_24h": (Decimal("0"), None),
    "market_cap": (Decimal("0"), None),
    "open_interest": (Decimal("0"), None),
    "open_interest_usd": (Decimal("0"), None),
    "spread": (Decimal("0"), None),
    # 百分比：-100 ~ 100
    "price_change_pct_24h": (Decimal("-100"), Decimal("100")),
    "pct_of_supply": (Decimal("-100"), Decimal("100")),
    "premium_discount": (Decimal("-100"), Decimal("100")),
    # 比率：0 ~ 1
    "confidence": (Decimal("0"), Decimal("1")),
    # 指数：0 ~ 100
    "fear_greed": (Decimal("0"), Decimal("100")),
    # 非负
    "funding_rate": (None, None),  # 可正可负，不限
    "long_short_ratio": (Decimal("0"), None),
    "net_flow_usd": (None, None),  # 可正可负
    "net_flow_btc": (None, None),
}


# ==========================================================================
# DataChecker 主类
# ==========================================================================


class DataChecker:
    """数据质量检查器。

    提供四种检查能力：
    - ``check_range`` — 范围/有效性检查
    - ``check_outliers`` — 异常值检测（Z-score + IQR）
    - ``check_continuity`` — 时间连续性检查
    - ``check_duplicates`` — 重复数据检测

    Usage::

        checker = DataChecker()
        report = checker.run_all_checks(
            records=[{"price": 108000, "observation_time": dt, ...}],
            data_type="candles",
            symbol="BTCUSDT",
            expected_interval_seconds=3600,
        )
    """

    def __init__(
        self,
        *,
        zscore_threshold: float = 3.0,
        iqr_multiplier: float = 1.5,
        continuity_factor: float = 2.0,
    ) -> None:
        """初始化检查器。

        Args:
            zscore_threshold: Z-score 异常值阈值（默认 3.0）
            iqr_multiplier: IQR 倍数（默认 1.5）
            continuity_factor: 连续性检查倍数（间隔 > factor × expected 视为跳跃）
        """
        self._zscore_threshold = zscore_threshold
        self._iqr_multiplier = iqr_multiplier
        self._continuity_factor = continuity_factor

    # ==================================================================
    # 综合检查
    # ==================================================================

    def run_all_checks(
        self,
        records: Sequence[dict[str, Any]],
        data_type: str,
        symbol: str,
        *,
        expected_interval_seconds: int | None = None,
        time_field: str = "observation_time",
        time_range_start: datetime | None = None,
        time_range_end: datetime | None = None,
    ) -> DataCheckReport:
        """执行全部检查并生成综合报告。

        Args:
            records: 数据记录列表（字典格式）
            data_type: 数据类型
            symbol: 资产符号
            expected_interval_seconds: 期望时间间隔（秒），用于连续性检查
            time_field: 时间字段名
            time_range_start: 检查范围起始
            time_range_end: 检查范围结束

        Returns:
            DataCheckReport 综合报告
        """
        report = DataCheckReport(
            data_type=data_type,
            symbol=symbol,
            total_records=len(records),
            time_range_start=time_range_start,
            time_range_end=time_range_end,
        )

        if not records:
            report.overall_score = 0.0
            report.overall_status = "ERROR"
            report.checks.append(CheckResult(
                check_type="EMPTY",
                message="无数据可检查",
                score=0.0,
            ))
            return report

        # 1. 范围检查
        range_result = self.check_range(records)
        report.checks.append(range_result)

        # 2. 异常值检测
        outlier_result = self.check_outliers(records, data_type)
        report.checks.append(outlier_result)

        # 3. 连续性检查
        if expected_interval_seconds and expected_interval_seconds > 0:
            continuity_result = self.check_continuity(
                records, expected_interval_seconds, time_field=time_field
            )
            report.checks.append(continuity_result)

        # 4. 重复检查
        duplicate_result = self.check_duplicates(records, data_type, symbol)
        report.checks.append(duplicate_result)

        # 计算综合得分
        report.overall_score = self._compute_overall_score(report.checks)
        report.overall_status = self._determine_status(report.overall_score)
        report.critical_issues = sum(
            1 for c in report.checks for i in c.issues if i.severity == "CRITICAL"
        )
        report.high_issues = sum(
            1 for c in report.checks for i in c.issues if i.severity == "HIGH"
        )

        return report

    # ==================================================================
    # 范围检查
    # ==================================================================

    def check_range(self, records: Sequence[dict[str, Any]]) -> CheckResult:
        """检查数值字段是否在物理有效范围内。

        规则：
        - 价格 > 0
        - 百分比 -100 ~ 100
        - 数量 >= 0
        - OHLC 内在一致性（high >= low）
        """
        result = CheckResult(check_type="RANGE", records_checked=len(records))
        passed = 0

        for idx, record in enumerate(records):
            issues = self._check_single_range(record, idx)
            if issues:
                result.issues.extend(issues)
            else:
                passed += 1

        result.records_passed = passed
        result.records_failed = len(records) - passed
        result.score = (passed / len(records) * 100) if records else 100.0
        result.message = f"范围检查: {passed}/{len(records)} 通过"
        return result

    def _check_single_range(
        self, record: dict[str, Any], idx: int
    ) -> list[CheckIssue]:
        """检查单条记录的范围有效性。"""
        issues: list[CheckIssue] = []
        timestamp = record.get("observation_time")

        for field_name, value in record.items():
            if field_name not in RANGE_DEFINITIONS or value is None:
                continue

            decimal_value = _safe_decimal(value)
            if decimal_value is None:
                continue

            min_val, max_val = RANGE_DEFINITIONS[field_name]
            if min_val is not None and decimal_value < min_val:
                issues.append(CheckIssue(
                    check_type="RANGE",
                    severity="HIGH" if field_name == "price" else "MEDIUM",
                    field_name=field_name,
                    value=value,
                    expected=f">= {min_val}",
                    message=f"{field_name}={value} 低于最小值 {min_val}",
                    timestamp=timestamp,
                    record_id=str(idx),
                ))
            if max_val is not None and decimal_value > max_val:
                issues.append(CheckIssue(
                    check_type="RANGE",
                    severity="MEDIUM",
                    field_name=field_name,
                    value=value,
                    expected=f"<= {max_val}",
                    message=f"{field_name}={value} 超过最大值 {max_val}",
                    timestamp=timestamp,
                    record_id=str(idx),
                ))

        # OHLC 内在一致性
        ohlc_issues = self._check_ohlc_consistency(record, idx)
        issues.extend(ohlc_issues)

        return issues

    def _check_ohlc_consistency(
        self, record: dict[str, Any], idx: int
    ) -> list[CheckIssue]:
        """检查 OHLC 数据内在一致性。"""
        issues: list[CheckIssue] = []
        o = _safe_decimal(record.get("open"))
        h = _safe_decimal(record.get("high"))
        l = _safe_decimal(record.get("low"))
        c = _safe_decimal(record.get("close"))

        if h is not None and l is not None and h < l:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="CRITICAL",
                field_name="high/low",
                message=f"high({h}) < low({l})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))
        if h is not None and o is not None and h < o:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="HIGH",
                field_name="high/open",
                message=f"high({h}) < open({o})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))
        if h is not None and c is not None and h < c:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="HIGH",
                field_name="high/close",
                message=f"high({h}) < close({c})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))
        if l is not None and o is not None and l > o:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="HIGH",
                field_name="low/open",
                message=f"low({l}) > open({o})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))
        if l is not None and c is not None and l > c:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="HIGH",
                field_name="low/close",
                message=f"low({l}) > close({c})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))
        # bid <= ask
        bid = _safe_decimal(record.get("bid"))
        ask = _safe_decimal(record.get("ask"))
        if bid is not None and ask is not None and bid > ask:
            issues.append(CheckIssue(
                check_type="RANGE",
                severity="HIGH",
                field_name="bid/ask",
                message=f"bid({bid}) > ask({ask})",
                timestamp=record.get("observation_time"),
                record_id=str(idx),
            ))

        return issues

    # ==================================================================
    # 异常值检测
    # ==================================================================

    def check_outliers(
        self, records: Sequence[dict[str, Any]], data_type: str
    ) -> CheckResult:
        """检测异常值（Z-score > 3 或 IQR 方法）。

        对关键数值字段进行统计异常检测。
        """
        result = CheckResult(check_type="OUTLIER", records_checked=len(records))

        # 确定要检测的字段
        fields_to_check = self._get_numeric_fields(data_type)
        outlier_indices: set[int] = set()

        for field_name in fields_to_check:
            values = []
            indices = []
            for idx, record in enumerate(records):
                val = _safe_decimal(record.get(field_name))
                if val is not None:
                    values.append(float(val))
                    indices.append(idx)

            if len(values) < 5:
                continue  # 样本太少无法做统计检测

            # Z-score 方法
            zscore_outliers = self._detect_zscore_outliers(values, indices, field_name, records)
            # IQR 方法
            iqr_outliers = self._detect_iqr_outliers(values, indices, field_name, records)

            result.issues.extend(zscore_outliers)
            result.issues.extend(iqr_outliers)
            outlier_indices.update(i.record_id for i in zscore_outliers if i.record_id)

        passed = len(records) - len(outlier_indices)
        result.records_passed = max(0, passed)
        result.records_failed = len(outlier_indices)
        result.score = (passed / len(records) * 100) if records else 100.0
        result.message = f"异常值检测: {len(outlier_indices)} 个异常点"
        return result

    def _detect_zscore_outliers(
        self,
        values: list[float],
        indices: list[int],
        field_name: str,
        records: Sequence[dict[str, Any]],
    ) -> list[CheckIssue]:
        """Z-score 异常值检测。"""
        issues: list[CheckIssue] = []
        if len(values) < 3:
            return issues

        mean = statistics.mean(values)
        stdev = statistics.stdev(values)
        if stdev == 0:
            return issues

        for i, val in enumerate(values):
            zscore = abs(val - mean) / stdev
            if zscore > self._zscore_threshold:
                idx = indices[i]
                record = records[idx] if idx < len(records) else {}
                issues.append(CheckIssue(
                    check_type="OUTLIER",
                    severity="HIGH" if zscore > 5 else "MEDIUM",
                    field_name=field_name,
                    value=val,
                    expected=f"Z-score <= {self._zscore_threshold}",
                    message=f"{field_name}={val:.4f} Z-score={zscore:.2f} (mean={mean:.4f}, std={stdev:.4f})",
                    timestamp=record.get("observation_time"),
                    record_id=str(idx),
                ))

        return issues

    def _detect_iqr_outliers(
        self,
        values: list[float],
        indices: list[int],
        field_name: str,
        records: Sequence[dict[str, Any]],
    ) -> list[CheckIssue]:
        """IQR 异常值检测。"""
        issues: list[CheckIssue] = []
        if len(values) < 4:
            return issues

        sorted_vals = sorted(values)
        n = len(sorted_vals)
        q1 = sorted_vals[n // 4]
        q3 = sorted_vals[3 * n // 4]
        iqr = q3 - q1
        if iqr == 0:
            return issues

        lower_bound = q1 - self._iqr_multiplier * iqr
        upper_bound = q3 + self._iqr_multiplier * iqr

        for i, val in enumerate(values):
            if val < lower_bound or val > upper_bound:
                idx = indices[i]
                record = records[idx] if idx < len(records) else {}
                issues.append(CheckIssue(
                    check_type="OUTLIER",
                    severity="MEDIUM",
                    field_name=field_name,
                    value=val,
                    expected=f"[{lower_bound:.4f}, {upper_bound:.4f}]",
                    message=f"{field_name}={val:.4f} 超出 IQR 范围",
                    timestamp=record.get("observation_time"),
                    record_id=str(idx),
                ))

        return issues

    def _get_numeric_fields(self, data_type: str) -> list[str]:
        """根据数据类型确定要检查的数值字段。"""
        field_map: dict[str, list[str]] = {
            "candles": ["open", "high", "low", "close", "volume"],
            "price": ["price", "volume_24h", "quote_volume_24h"],
            "market": ["price", "volume_24h"],
            "onchain": ["value"],
            "etf": ["net_flow_usd", "total_holdings_btc"],
            "derivatives": ["funding_rate", "open_interest_usd"],
            "macro": ["value"],
            "sentiment": ["value"],
        }
        return field_map.get(data_type.lower(), ["price", "volume"])

    # ==================================================================
    # 连续性检查
    # ==================================================================

    def check_continuity(
        self,
        records: Sequence[dict[str, Any]],
        expected_interval_seconds: int,
        *,
        time_field: str = "observation_time",
    ) -> CheckResult:
        """检查时间序列连续性。

        间隔超过 ``continuity_factor × expected_interval`` 视为跳跃。
        """
        result = CheckResult(check_type="CONTINUITY", records_checked=len(records))

        # 提取并排序时间
        timestamps: list[datetime] = []
        for record in records:
            ts = record.get(time_field)
            if isinstance(ts, datetime):
                timestamps.append(ts)
            elif isinstance(ts, str):
                from app.services.data_normalizer import parse_timestamp
                parsed = parse_timestamp(ts)
                if parsed:
                    timestamps.append(parsed)

        if len(timestamps) < 2:
            result.records_passed = len(timestamps)
            result.score = 100.0
            result.message = "数据点不足，跳过连续性检查"
            return result

        timestamps.sort()
        max_gap = timedelta(seconds=expected_interval_seconds * self._continuity_factor)
        jumps = 0

        for i in range(1, len(timestamps)):
            gap = timestamps[i] - timestamps[i - 1]
            if gap > max_gap:
                jumps += 1
                result.issues.append(CheckIssue(
                    check_type="CONTINUITY",
                    severity="MEDIUM" if gap <= max_gap * 3 else "HIGH",
                    field_name=time_field,
                    message=(
                        f"时间跳跃: {timestamps[i-1].strftime('%Y-%m-%d %H:%M')} → "
                        f"{timestamps[i].strftime('%Y-%m-%d %H:%M')} "
                        f"(间隔 {gap.total_seconds()/3600:.1f}h, "
                        f"期望 {expected_interval_seconds/3600:.1f}h)"
                    ),
                    timestamp=timestamps[i],
                ))

        result.records_passed = len(timestamps) - jumps
        result.records_failed = jumps
        result.score = ((len(timestamps) - jumps) / len(timestamps) * 100) if timestamps else 100.0
        result.message = f"连续性检查: {jumps} 个跳跃点 (共 {len(timestamps)} 个时间点)"
        return result

    # ==================================================================
    # 重复数据检测
    # ==================================================================

    def check_duplicates(
        self,
        records: Sequence[dict[str, Any]],
        data_type: str,
        symbol: str,
    ) -> CheckResult:
        """检测重复数据（相同业务键的记录）。"""
        result = CheckResult(check_type="DUPLICATE", records_checked=len(records))

        # 构造业务键
        key_fields = self._get_key_fields(data_type)
        seen: dict[tuple, int] = {}
        duplicates: list[int] = []

        for idx, record in enumerate(records):
            key = tuple(
                str(record.get(f, "")) for f in key_fields
            )
            if key in seen:
                duplicates.append(idx)
                result.issues.append(CheckIssue(
                    check_type="DUPLICATE",
                    severity="LOW",
                    message=f"重复记录: key={key}, 首次出现 idx={seen[key]}, 重复 idx={idx}",
                    timestamp=record.get("observation_time"),
                    record_id=str(idx),
                ))
            else:
                seen[key] = idx

        result.records_passed = len(records) - len(duplicates)
        result.records_failed = len(duplicates)
        result.score = (result.records_passed / len(records) * 100) if records else 100.0
        result.message = f"重复检测: {len(duplicates)} 条重复"
        return result

    def _get_key_fields(self, data_type: str) -> list[str]:
        """获取数据类型的业务唯一键字段。"""
        key_map: dict[str, list[str]] = {
            "candles": ["symbol", "interval", "observation_time", "source_id"],
            "price": ["symbol", "observation_time", "source_id"],
            "market": ["symbol", "observation_time", "source_id"],
            "onchain": ["metric_name", "observation_time", "source_id"],
            "etf": ["ticker", "observation_time"],
            "derivatives": ["symbol", "exchange", "data_type", "observation_time"],
            "macro": ["series_id", "observation_date"],
            "sentiment": ["source_type", "metric_name", "observation_time"],
        }
        return key_map.get(data_type.lower(), ["observation_time"])

    # ==================================================================
    # 得分计算
    # ==================================================================

    @staticmethod
    def _compute_overall_score(checks: list[CheckResult]) -> float:
        """计算综合得分（各检查项加权平均）。"""
        if not checks:
            return 100.0

        # 权重：范围检查 30%、异常值 25%、连续性 25%、重复 20%
        weights: dict[str, float] = {
            "RANGE": 0.30,
            "OUTLIER": 0.25,
            "CONTINUITY": 0.25,
            "DUPLICATE": 0.20,
        }

        total_weight = 0.0
        weighted_sum = 0.0
        for check in checks:
            w = weights.get(check.check_type, 0.1)
            weighted_sum += check.score * w
            total_weight += w

        return weighted_sum / total_weight if total_weight > 0 else 100.0

    @staticmethod
    def _determine_status(score: float) -> str:
        """根据得分确定状态。"""
        if score >= 90:
            return "OK"
        elif score >= 70:
            return "WARNING"
        elif score >= 50:
            return "ERROR"
        else:
            return "CRITICAL"


# ==========================================================================
# 辅助函数
# ==========================================================================


def _safe_decimal(value: Any) -> Decimal | None:
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
        try:
            d = Decimal(value.strip())
            return d if d.is_finite() else None
        except (InvalidOperation, ValueError):
            return None
    return None


__all__ = [
    "CheckIssue",
    "CheckResult",
    "DataCheckReport",
    "DataChecker",
    "RANGE_DEFINITIONS",
]
