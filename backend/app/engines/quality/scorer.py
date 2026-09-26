"""数据质量评分计算 — 五维度加权评分。

对应架构文档：
- 《10-data-quality.md》§5 质量评分与告警
- 评分公式：QualityScore = 完整性×30% + 准确性×25% + 一致性×20% + 时效性×15% + 有效性×10%
- 评分范围：0-100
- 四级告警：> 90 优秀 / > 70 良好 / > 50 警告 / <= 50 严重
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ==========================================================================
# 评分维度与权重
# ==========================================================================


class QualityDimension(str, Enum):
    """质量评分维度。"""

    COMPLETENESS = "completeness"   # 完整性
    ACCURACY = "accuracy"           # 准确性
    CONSISTENCY = "consistency"     # 一致性
    TIMELINESS = "timeliness"       # 时效性
    VALIDITY = "validity"           # 有效性


#: 各维度权重（文档 §5 定义，总和 = 1.0）
DIMENSION_WEIGHTS: dict[QualityDimension, float] = {
    QualityDimension.COMPLETENESS: 0.30,
    QualityDimension.ACCURACY: 0.25,
    QualityDimension.CONSISTENCY: 0.20,
    QualityDimension.TIMELINESS: 0.15,
    QualityDimension.VALIDITY: 0.10,
}


class QualityLevel(str, Enum):
    """质量等级（四级告警）。"""

    EXCELLENT = "EXCELLENT"  # > 90
    GOOD = "GOOD"            # > 70
    WARNING = "WARNING"      # > 50
    CRITICAL = "CRITICAL"    # <= 50


#: 质量等级对应的中文描述
LEVEL_DESCRIPTIONS: dict[QualityLevel, str] = {
    QualityLevel.EXCELLENT: "优秀 — 数据质量高，无需关注",
    QualityLevel.GOOD: "良好 — 数据质量可接受，少量问题",
    QualityLevel.WARNING: "警告 — 数据质量下降，需要关注",
    QualityLevel.CRITICAL: "严重 — 数据质量低，需立即处理",
}


# ==========================================================================
# 评分结构
# ==========================================================================


@dataclass
class DimensionScore:
    """单维度评分详情。"""

    dimension: QualityDimension
    score: float  # 0-100
    weight: float
    weighted_score: float  # score × weight
    details: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "dimension": self.dimension.value,
            "score": round(self.score, 2),
            "weight": self.weight,
            "weighted_score": round(self.weighted_score, 2),
            "details": self.details,
            "metrics": self.metrics,
        }


@dataclass
class QualityScore:
    """综合质量评分结果。"""

    data_type: str
    symbol: str
    total_score: float  # 0-100
    level: QualityLevel
    dimensions: list[DimensionScore] = field(default_factory=list)
    evaluated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None
    recommendations: list[str] = field(default_factory=list)

    @property
    def is_healthy(self) -> bool:
        """是否健康（GOOD 或 EXCELLENT）。"""
        return self.level in (QualityLevel.EXCELLENT, QualityLevel.GOOD)

    @property
    def needs_attention(self) -> bool:
        """是否需要关注。"""
        return self.level in (QualityLevel.WARNING, QualityLevel.CRITICAL)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "data_type": self.data_type,
            "symbol": self.symbol,
            "total_score": round(self.total_score, 2),
            "level": self.level.value,
            "level_description": LEVEL_DESCRIPTIONS.get(self.level, ""),
            "is_healthy": self.is_healthy,
            "evaluated_at": self.evaluated_at.isoformat(),
            "time_range_start": self.time_range_start.isoformat() if self.time_range_start else None,
            "time_range_end": self.time_range_end.isoformat() if self.time_range_end else None,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "recommendations": self.recommendations,
        }


# ==========================================================================
# QualityScorer 主类
# ==========================================================================


class QualityScorer:
    """质量评分计算器。

    根据五个维度的原始指标计算综合质量评分。

    Usage::

        scorer = QualityScorer()
        score = scorer.compute_score(
            data_type="candles",
            symbol="BTCUSDT",
            completeness_pct=98.5,
            accuracy_pct=95.0,
            consistency_pct=99.0,
            timeliness_pct=100.0,
            validity_pct=97.5,
        )
        print(f"评分: {score.total_score:.1f} ({score.level.value})")
    """

    def __init__(self, *, custom_weights: dict[str, float] | None = None) -> None:
        """初始化评分器。

        Args:
            custom_weights: 自定义权重（覆盖默认值），键为维度名
        """
        self._weights = dict(DIMENSION_WEIGHTS)
        if custom_weights:
            for key, weight in custom_weights.items():
                try:
                    dim = QualityDimension(key)
                    self._weights[dim] = weight
                except ValueError:
                    pass
            # 归一化权重
            total = sum(self._weights.values())
            if total > 0:
                self._weights = {k: v / total for k, v in self._weights.items()}

    @property
    def weights(self) -> dict[str, float]:
        """当前使用的权重配置。"""
        return {k.value: v for k, v in self._weights.items()}

    def compute_score(
        self,
        data_type: str,
        symbol: str,
        *,
        completeness_pct: float = 100.0,
        accuracy_pct: float = 100.0,
        consistency_pct: float = 100.0,
        timeliness_pct: float = 100.0,
        validity_pct: float = 100.0,
        completeness_details: str = "",
        accuracy_details: str = "",
        consistency_details: str = "",
        timeliness_details: str = "",
        validity_details: str = "",
        completeness_metrics: dict[str, Any] | None = None,
        accuracy_metrics: dict[str, Any] | None = None,
        consistency_metrics: dict[str, Any] | None = None,
        timeliness_metrics: dict[str, Any] | None = None,
        validity_metrics: dict[str, Any] | None = None,
        time_range_start: datetime | None = None,
        time_range_end: datetime | None = None,
    ) -> QualityScore:
        """计算综合质量评分。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            completeness_pct: 完整性得分 (0-100)
            accuracy_pct: 准确性得分 (0-100)
            consistency_pct: 一致性得分 (0-100)
            timeliness_pct: 时效性得分 (0-100)
            validity_pct: 有效性得分 (0-100)
            completeness_details: 完整性说明
            accuracy_details: 准确性说明
            consistency_details: 一致性说明
            timeliness_details: 时效性说明
            validity_details: 有效性说明
            completeness_metrics: 完整性指标详情
            accuracy_metrics: 准确性指标详情
            consistency_metrics: 一致性指标详情
            timeliness_metrics: 时效性指标详情
            validity_metrics: 有效性指标详情
            time_range_start: 评估时间范围起始
            time_range_end: 评估时间范围结束

        Returns:
            QualityScore 综合评分结果
        """
        # 构建各维度评分
        raw_scores: dict[QualityDimension, tuple[float, str, dict[str, Any]]] = {
            QualityDimension.COMPLETENESS: (
                _clamp(completeness_pct), completeness_details,
                completeness_metrics or {},
            ),
            QualityDimension.ACCURACY: (
                _clamp(accuracy_pct), accuracy_details,
                accuracy_metrics or {},
            ),
            QualityDimension.CONSISTENCY: (
                _clamp(consistency_pct), consistency_details,
                consistency_metrics or {},
            ),
            QualityDimension.TIMELINESS: (
                _clamp(timeliness_pct), timeliness_details,
                timeliness_metrics or {},
            ),
            QualityDimension.VALIDITY: (
                _clamp(validity_pct), validity_details,
                validity_metrics or {},
            ),
        }

        dimensions: list[DimensionScore] = []
        total_weighted = 0.0

        for dim, (score, details, metrics) in raw_scores.items():
            weight = self._weights.get(dim, 0.0)
            weighted = score * weight
            total_weighted += weighted
            dimensions.append(DimensionScore(
                dimension=dim,
                score=score,
                weight=weight,
                weighted_score=weighted,
                details=details,
                metrics=metrics,
            ))

        # 综合得分
        total_score = _clamp(total_weighted)
        level = self._determine_level(total_score)
        recommendations = self._generate_recommendations(dimensions, level)

        return QualityScore(
            data_type=data_type,
            symbol=symbol,
            total_score=total_score,
            level=level,
            dimensions=dimensions,
            time_range_start=time_range_start,
            time_range_end=time_range_end,
            recommendations=recommendations,
        )

    def compute_from_checks(
        self,
        data_type: str,
        symbol: str,
        *,
        total_expected: int,
        total_found: int,
        outlier_count: int,
        conflict_count: int,
        last_update_seconds_ago: float | None,
        expected_update_interval_seconds: float,
        invalid_count: int,
        time_range_start: datetime | None = None,
        time_range_end: datetime | None = None,
    ) -> QualityScore:
        """从原始检查指标计算评分（便捷方法）。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            total_expected: 期望记录总数
            total_found: 实际找到记录数
            outlier_count: 异常值数量
            conflict_count: 冲突数量
            last_update_seconds_ago: 距上次更新的秒数
            expected_update_interval_seconds: 期望更新间隔（秒）
            invalid_count: 无效记录数
            time_range_start: 时间范围起始
            time_range_end: 时间范围结束

        Returns:
            QualityScore 综合评分
        """
        # 完整性：实际/期望
        completeness = (total_found / total_expected * 100) if total_expected > 0 else 0.0

        # 准确性：1 - (异常值 / 总记录)
        accuracy = ((1 - outlier_count / max(total_found, 1)) * 100) if total_found > 0 else 0.0

        # 一致性：1 - (冲突 / 总记录)
        consistency = ((1 - conflict_count / max(total_found, 1)) * 100) if total_found > 0 else 0.0

        # 时效性：根据最后更新时间与期望间隔的比值
        if last_update_seconds_ago is not None and expected_update_interval_seconds > 0:
            ratio = last_update_seconds_ago / expected_update_interval_seconds
            if ratio <= 1.0:
                timeliness = 100.0
            elif ratio <= 2.0:
                timeliness = 100 - (ratio - 1) * 50  # 1-2 倍线性下降
            elif ratio <= 5.0:
                timeliness = 50 - (ratio - 2) * 15  # 2-5 倍加速下降
            else:
                timeliness = max(0.0, 5.0)
        else:
            timeliness = 50.0  # 无法判定时给中间分

        # 有效性：1 - (无效 / 总记录)
        validity = ((1 - invalid_count / max(total_found, 1)) * 100) if total_found > 0 else 0.0

        return self.compute_score(
            data_type=data_type,
            symbol=symbol,
            completeness_pct=completeness,
            accuracy_pct=accuracy,
            consistency_pct=consistency,
            timeliness_pct=timeliness,
            validity_pct=validity,
            completeness_details=f"期望 {total_expected} 条，实际 {total_found} 条",
            accuracy_details=f"异常值 {outlier_count} 个",
            consistency_details=f"冲突 {conflict_count} 个",
            timeliness_details=(
                f"距上次更新 {last_update_seconds_ago:.0f}s"
                if last_update_seconds_ago is not None else "无更新记录"
            ),
            validity_details=f"无效记录 {invalid_count} 条",
            completeness_metrics={
                "total_expected": total_expected,
                "total_found": total_found,
            },
            accuracy_metrics={"outlier_count": outlier_count},
            consistency_metrics={"conflict_count": conflict_count},
            timeliness_metrics={
                "last_update_seconds_ago": last_update_seconds_ago,
                "expected_interval": expected_update_interval_seconds,
            },
            validity_metrics={"invalid_count": invalid_count},
            time_range_start=time_range_start,
            time_range_end=time_range_end,
        )

    # ==================================================================
    # 内部方法
    # ==================================================================

    @staticmethod
    def _determine_level(score: float) -> QualityLevel:
        """根据得分判定质量等级（四级告警）。"""
        if score > 90:
            return QualityLevel.EXCELLENT
        elif score > 70:
            return QualityLevel.GOOD
        elif score > 50:
            return QualityLevel.WARNING
        else:
            return QualityLevel.CRITICAL

    @staticmethod
    def _generate_recommendations(
        dimensions: list[DimensionScore], level: QualityLevel
    ) -> list[str]:
        """根据各维度得分生成改进建议。"""
        recommendations: list[str] = []

        for dim_score in dimensions:
            if dim_score.score < 70:
                dim = dim_score.dimension
                if dim == QualityDimension.COMPLETENESS:
                    recommendations.append("数据完整性不足，建议执行补洞任务填补缺失时间段")
                elif dim == QualityDimension.ACCURACY:
                    recommendations.append("存在较多异常值，建议检查数据源质量或启用更严格的过滤")
                elif dim == QualityDimension.CONSISTENCY:
                    recommendations.append("多源数据一致性差，建议增加交叉验证频率或更换 Provider")
                elif dim == QualityDimension.TIMELINESS:
                    recommendations.append("数据更新不及时，建议检查同步任务状态或缩短同步间隔")
                elif dim == QualityDimension.VALIDITY:
                    recommendations.append("存在无效数据，建议加强 Write Gate 校验规则")

        if level == QualityLevel.CRITICAL:
            recommendations.insert(0, "⚠️ 数据质量严重下降，需立即排查原因")
        elif level == QualityLevel.WARNING:
            recommendations.insert(0, "数据质量下降，建议近期关注并处理")

        return recommendations


# ==========================================================================
# 辅助函数
# ==========================================================================


def _clamp(value: float, min_val: float = 0.0, max_val: float = 100.0) -> float:
    """将值限制在 [min_val, max_val] 范围内。"""
    return max(min_val, min(max_val, value))


# ==========================================================================
# 全局单例
# ==========================================================================

_quality_scorer: QualityScorer | None = None


def get_quality_scorer() -> QualityScorer:
    """获取全局 QualityScorer 单例。"""
    global _quality_scorer
    if _quality_scorer is None:
        _quality_scorer = QualityScorer()
    return _quality_scorer


def set_quality_scorer(scorer: QualityScorer) -> None:
    """设置全局 QualityScorer 单例。"""
    global _quality_scorer
    _quality_scorer = scorer


__all__ = [
    "DIMENSION_WEIGHTS",
    "DimensionScore",
    "LEVEL_DESCRIPTIONS",
    "QualityDimension",
    "QualityLevel",
    "QualityScore",
    "QualityScorer",
    "get_quality_scorer",
    "set_quality_scorer",
]
