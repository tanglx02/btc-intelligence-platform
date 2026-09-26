"""数据质量报告生成器 — 汇总检查结果并输出结构化报告。

对应架构文档：
- 《10-data-quality.md》§5 质量报告
- 《10-data-quality.md》§6 告警与通知

职责：
- 汇总 DataChecker 和 QualityScorer 的结果
- 生成人类可读的文本报告和机器可解析的 JSON 报告
- 提供告警判定（是否需要通知）
- 持久化报告到 data_quality 表
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session_ctx
from app.models.enums import ProviderCategory
from app.models.system import DataQuality
from app.engines.quality.checker import CheckResult, DataCheckReport
from app.engines.quality.scorer import (
    QualityLevel,
    QualityScore,
    QualityScorer,
    get_quality_scorer,
)
from app.services.data_store import utcnow


# ==========================================================================
# 报告结构
# ==========================================================================


@dataclass
class QualityReport:
    """完整的数据质量报告。"""

    report_id: str = field(default_factory=lambda: uuid4().hex[:16])
    data_type: str = ""
    symbol: str = ""
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    time_range_start: datetime | None = None
    time_range_end: datetime | None = None

    # 评分
    score: QualityScore | None = None
    total_score: float = 0.0
    level: QualityLevel = QualityLevel.EXCELLENT

    # 检查详情
    check_report: DataCheckReport | None = None
    total_records: int = 0
    issues_summary: dict[str, int] = field(default_factory=dict)

    # 告警
    should_alert: bool = False
    alert_level: str = ""  # INFO / WARNING / ERROR / CRITICAL
    alert_message: str = ""

    # 建议
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "report_id": self.report_id,
            "data_type": self.data_type,
            "symbol": self.symbol,
            "generated_at": self.generated_at.isoformat(),
            "time_range_start": self.time_range_start.isoformat() if self.time_range_start else None,
            "time_range_end": self.time_range_end.isoformat() if self.time_range_end else None,
            "total_score": round(self.total_score, 2),
            "level": self.level.value,
            "total_records": self.total_records,
            "issues_summary": self.issues_summary,
            "should_alert": self.should_alert,
            "alert_level": self.alert_level,
            "alert_message": self.alert_message,
            "recommendations": self.recommendations,
            "score_details": self.score.to_dict() if self.score else None,
            "check_details": self.check_report.to_dict() if self.check_report else None,
        }

    def to_text(self) -> str:
        """生成人类可读的文本报告。"""
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("数据质量报告")
        lines.append("=" * 60)
        lines.append(f"报告ID: {self.report_id}")
        lines.append(f"生成时间: {self.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"数据类型: {self.data_type}")
        lines.append(f"资产符号: {self.symbol}")

        if self.time_range_start and self.time_range_end:
            lines.append(
                f"检查范围: {self.time_range_start.strftime('%Y-%m-%d %H:%M')} ~ "
                f"{self.time_range_end.strftime('%Y-%m-%d %H:%M')}"
            )

        lines.append("")
        lines.append(f"综合评分: {self.total_score:.1f} / 100")
        lines.append(f"质量等级: {self.level.value}")
        lines.append(f"记录总数: {self.total_records}")
        lines.append("")

        # 各维度得分
        if self.score and self.score.dimensions:
            lines.append("-" * 40)
            lines.append("维度得分:")
            for dim in self.score.dimensions:
                bar = "█" * int(dim.score / 5) + "░" * (20 - int(dim.score / 5))
                lines.append(
                    f"  {dim.dimension.value:<15} {bar} {dim.score:5.1f} "
                    f"(权重 {dim.weight*100:.0f}%)"
                )
            lines.append("")

        # 问题汇总
        if self.issues_summary:
            lines.append("-" * 40)
            lines.append("问题汇总:")
            for issue_type, count in sorted(self.issues_summary.items()):
                if count > 0:
                    lines.append(f"  {issue_type}: {count}")
            lines.append("")

        # 告警
        if self.should_alert:
            lines.append("-" * 40)
            lines.append(f"⚠️ 告警 [{self.alert_level}]: {self.alert_message}")
            lines.append("")

        # 建议
        if self.recommendations:
            lines.append("-" * 40)
            lines.append("改进建议:")
            for i, rec in enumerate(self.recommendations, 1):
                lines.append(f"  {i}. {rec}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)


@dataclass
class BatchQualityReport:
    """批量质量报告（多数据类型/多资产）。"""

    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    reports: list[QualityReport] = field(default_factory=list)
    overall_score: float = 0.0
    overall_level: QualityLevel = QualityLevel.EXCELLENT
    total_alerts: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""
        return {
            "generated_at": self.generated_at.isoformat(),
            "overall_score": round(self.overall_score, 2),
            "overall_level": self.overall_level.value,
            "total_reports": len(self.reports),
            "total_alerts": self.total_alerts,
            "reports": [r.to_dict() for r in self.reports],
        }

    def summary_text(self) -> str:
        """生成汇总文本。"""
        lines: list[str] = []
        lines.append("=" * 60)
        lines.append("数据质量汇总报告")
        lines.append("=" * 60)
        lines.append(f"生成时间: {self.generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append(f"综合评分: {self.overall_score:.1f} / 100 ({self.overall_level.value})")
        lines.append(f"报告数量: {len(self.reports)}")
        lines.append(f"告警数量: {self.total_alerts}")
        lines.append("")

        for report in self.reports:
            status_icon = "✅" if report.level in (QualityLevel.EXCELLENT, QualityLevel.GOOD) else "⚠️"
            lines.append(
                f"  {status_icon} {report.data_type}/{report.symbol}: "
                f"{report.total_score:.1f} ({report.level.value})"
            )

        lines.append("=" * 60)
        return "\n".join(lines)


# ==========================================================================
# QualityReporter 主类
# ==========================================================================


class QualityReporter:
    """质量报告生成器。

    Usage::

        reporter = QualityReporter()
        report = reporter.generate_report(
            data_type="candles",
            symbol="BTCUSDT",
            check_report=checker.run_all_checks(records, ...),
            score=scorer.compute_score(...),
        )
        print(report.to_text())
    """

    def __init__(
        self,
        scorer: QualityScorer | None = None,
        *,
        alert_threshold: float = 70.0,
        critical_threshold: float = 50.0,
        persist_reports: bool = True,
    ) -> None:
        """初始化报告生成器。

        Args:
            scorer: 质量评分器
            alert_threshold: 低于此分数触发 WARNING 告警
            critical_threshold: 低于此分数触发 CRITICAL 告警
            persist_reports: 是否将报告持久化到数据库
        """
        self._scorer = scorer or get_quality_scorer()
        self._alert_threshold = alert_threshold
        self._critical_threshold = critical_threshold
        self._persist_reports = persist_reports

    def generate_report(
        self,
        data_type: str,
        symbol: str,
        *,
        check_report: DataCheckReport | None = None,
        score: QualityScore | None = None,
        time_range_start: datetime | None = None,
        time_range_end: datetime | None = None,
    ) -> QualityReport:
        """生成单项质量报告。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            check_report: DataChecker 的检查报告
            score: QualityScorer 的评分结果
            time_range_start: 检查范围起始
            time_range_end: 检查范围结束

        Returns:
            QualityReport 质量报告
        """
        report = QualityReport(
            data_type=data_type,
            symbol=symbol,
            time_range_start=time_range_start,
            time_range_end=time_range_end,
        )

        # 评分
        if score is not None:
            report.score = score
            report.total_score = score.total_score
            report.level = score.level
            report.recommendations = score.recommendations

        # 检查详情
        if check_report is not None:
            report.check_report = check_report
            report.total_records = check_report.total_records

            # 汇总问题
            issues_summary: dict[str, int] = {}
            for check in check_report.checks:
                issues_summary[check.check_type] = len(check.issues)
            report.issues_summary = issues_summary

            # 如果没有独立评分，从检查报告推断
            if score is None:
                report.total_score = check_report.overall_score
                report.level = self._scorer._determine_level(check_report.overall_score)

        # 告警判定
        report.should_alert = report.total_score < self._alert_threshold
        if report.total_score <= self._critical_threshold:
            report.alert_level = "CRITICAL"
            report.alert_message = (
                f"{data_type}/{symbol} 质量评分 {report.total_score:.1f} "
                f"严重低于阈值 {self._critical_threshold}"
            )
        elif report.total_score < self._alert_threshold:
            report.alert_level = "WARNING"
            report.alert_message = (
                f"{data_type}/{symbol} 质量评分 {report.total_score:.1f} "
                f"低于阈值 {self._alert_threshold}"
            )
        else:
            report.alert_level = "INFO"

        return report

    def generate_batch_report(
        self, reports: Sequence[QualityReport]
    ) -> BatchQualityReport:
        """生成批量汇总报告。

        Args:
            reports: 单项报告列表

        Returns:
            BatchQualityReport 汇总报告
        """
        batch = BatchQualityReport(reports=list(reports))

        if reports:
            scores = [r.total_score for r in reports]
            batch.overall_score = sum(scores) / len(scores)
            batch.overall_level = self._scorer._determine_level(batch.overall_score)
            batch.total_alerts = sum(1 for r in reports if r.should_alert)

        return batch

    async def persist_report(self, report: QualityReport) -> bool:
        """将报告持久化到 data_quality 表。

        Args:
            report: 质量报告

        Returns:
            是否持久化成功
        """
        if not self._persist_reports:
            return False

        try:
            async with get_db_session_ctx() as session:
                category = self._infer_category(report.data_type)
                status = "OK"
                severity = "INFO"

                if report.level == QualityLevel.CRITICAL:
                    status = "CRITICAL"
                    severity = "CRITICAL"
                elif report.level == QualityLevel.WARNING:
                    status = "ERROR"
                    severity = "HIGH"
                elif report.total_score < 90:
                    status = "WARNING"
                    severity = "MEDIUM"

                quality_record = DataQuality(
                    id=uuid4(),
                    check_time=utcnow(),
                    data_category=category,
                    table_name=self._infer_table(report.data_type),
                    check_type="QUALITY_REPORT",
                    status=status,
                    severity=severity,
                    records_checked=report.total_records,
                    records_passed=int(report.total_records * report.total_score / 100),
                    records_failed=int(report.total_records * (100 - report.total_score) / 100),
                    completeness_pct=round(report.total_score, 2) if report.total_score else None,
                    issues=[{
                        "type": "quality_report",
                        "report_id": report.report_id,
                        "total_score": round(report.total_score, 2),
                        "level": report.level.value,
                        "issues_summary": report.issues_summary,
                        "recommendations": report.recommendations[:5],
                    }],
                    anomalies_found=(
                        report.check_report.checks[1].issues[:10]
                        if report.check_report and len(report.check_report.checks) > 1
                        else []
                    ),
                    requires_manual=report.level == QualityLevel.CRITICAL,
                    time_range_start=report.time_range_start,
                    time_range_end=report.time_range_end,
                )

                session.add(quality_record)
                await session.commit()
                logger.debug(f"质量报告已持久化: {report.report_id}")
                return True

        except Exception as exc:
            logger.error(f"持久化质量报告失败: {exc}")
            return False

    @staticmethod
    def _infer_category(data_type: str) -> ProviderCategory:
        """推断数据类别。"""
        category_map: dict[str, ProviderCategory] = {
            "candles": ProviderCategory.MARKET,
            "market": ProviderCategory.MARKET,
            "price": ProviderCategory.MARKET,
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
            "candles": "candles",
            "market": "market_prices",
            "price": "market_prices",
            "onchain": "onchain_metrics",
            "etf": "etf_flows",
            "derivatives": "derivatives",
            "macro": "macro_series",
            "sentiment": "sentiment",
        }
        return table_map.get(data_type.lower(), "unknown")


# ==========================================================================
# 全局单例
# ==========================================================================

_quality_reporter: QualityReporter | None = None


def get_quality_reporter() -> QualityReporter:
    """获取全局 QualityReporter 单例。"""
    global _quality_reporter
    if _quality_reporter is None:
        _quality_reporter = QualityReporter()
    return _quality_reporter


def set_quality_reporter(reporter: QualityReporter) -> None:
    """设置全局 QualityReporter 单例。"""
    global _quality_reporter
    _quality_reporter = reporter


__all__ = [
    "BatchQualityReport",
    "QualityReport",
    "QualityReporter",
    "get_quality_reporter",
    "set_quality_reporter",
]
