"""DataQualityEngine — 数据质量主引擎。

对应架构文档：
- 《10-data-quality.md》全文
- 整合 checker / scorer / reporter 三大组件

职责：
- ``check_data_quality`` — 全面质量检查（五维度）
- ``get_quality_score`` — 获取质量评分
- ``get_quality_report`` — 生成质量报告

五维度检查：
1. 完整性：缺失数据点比例
2. 准确性：异常值检测（Z-score > 3）
3. 一致性：多源对比结果
4. 时效性：最后更新时间 vs 预期
5. 有效性：数据范围检查（price > 0, volume >= 0）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Sequence

from loguru import logger
from sqlalchemy import select, func as sa_func, and_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db_session_ctx, get_session_factory
from app.models import Candle, MarketPrice, OnchainMetric
from app.models.enums import CandleInterval, ProviderCategory, QualityStatus
from app.models.system import DataQuality
from app.services.data_store import utcnow
from app.engines.quality.checker import DataChecker, DataCheckReport
from app.engines.quality.scorer import (
    QualityLevel,
    QualityScore,
    QualityScorer,
    get_quality_scorer,
)
from app.engines.quality.reporter import (
    QualityReport,
    QualityReporter,
    BatchQualityReport,
    get_quality_reporter,
)


# ==========================================================================
# 数据类型配置
# ==========================================================================

#: data_type → (ORM 模型, 默认间隔秒数, 目标表名)
DATA_TYPE_CONFIG: dict[str, tuple[type, int, str]] = {
    "candles": (Candle, 3600, "candles"),
    "market": (MarketPrice, 60, "market_prices"),
    "price": (MarketPrice, 60, "market_prices"),
    "onchain": (OnchainMetric, 86400, "onchain_metrics"),
}

#: K线周期 → 间隔秒数
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}


# ==========================================================================
# DataQualityEngine
# ==========================================================================


class DataQualityEngine:
    """数据质量主引擎。

    整合 DataChecker、QualityScorer、QualityReporter，提供统一的质量检查入口。

    Usage::

        engine = DataQualityEngine()

        # 全面质量检查
        report = await engine.check_data_quality(
            data_type="candles", symbol="BTCUSDT",
            time_range=(start_dt, end_dt),
        )
        print(report.to_text())

        # 获取评分
        score = await engine.get_quality_score("candles", "BTCUSDT")
        print(f"Score: {score.total_score:.1f} ({score.level.value})")
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        checker: DataChecker | None = None,
        scorer: QualityScorer | None = None,
        reporter: QualityReporter | None = None,
    ) -> None:
        """初始化质量引擎。

        Args:
            session_factory: 异步会话工厂
            checker: 数据检查器
            scorer: 质量评分器
            reporter: 报告生成器
        """
        self._session_factory = session_factory or get_session_factory()
        self._checker = checker or DataChecker()
        self._scorer = scorer or get_quality_scorer()
        self._reporter = reporter or get_quality_reporter()

    # ==================================================================
    # 公开接口
    # ==================================================================

    async def check_data_quality(
        self,
        data_type: str,
        symbol: str,
        time_range: tuple[datetime, datetime] | None = None,
        *,
        interval: str | None = None,
        metric: str | None = None,
        sample_limit: int = 10000,
        persist: bool = True,
    ) -> QualityReport:
        """全面数据质量检查（五维度）。

        Args:
            data_type: 数据类型（candles / price / onchain 等）
            symbol: 资产符号
            time_range: 检查时间范围 (start, end)；None 时默认最近 24h
            interval: K线周期（行情类）
            metric: 指标名（链上类）
            sample_limit: 最大采样记录数
            persist: 是否持久化报告

        Returns:
            QualityReport 完整质量报告
        """
        now = utcnow()
        if time_range is None:
            time_range = (now - timedelta(hours=24), now)

        start, end = time_range
        start = _ensure_utc(start)
        end = _ensure_utc(end)

        logger.info(
            f"开始质量检查: {data_type}/{symbol} "
            f"[{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%Y-%m-%d %H:%M')}]"
        )

        # 获取数据
        records = await self._fetch_records(
            data_type=data_type,
            symbol=symbol,
            start=start,
            end=end,
            interval=interval,
            metric=metric,
            limit=sample_limit,
        )

        # 确定期望间隔
        expected_interval = self._resolve_interval(data_type, interval)

        # 执行检查
        check_report = self._checker.run_all_checks(
            records=records,
            data_type=data_type,
            symbol=symbol,
            expected_interval_seconds=expected_interval,
            time_range_start=start,
            time_range_end=end,
        )

        # 计算完整性
        total_expected = self._calc_expected_records(start, end, expected_interval)
        total_found = len(records)

        # 计算时效性
        last_update_ago = await self._get_last_update_age(data_type, symbol, interval, metric)

        # 计算一致性（从 data_quality 表查询冲突记录）
        conflict_count = await self._get_conflict_count(data_type, symbol, start, end)

        # 计算有效性（无效记录数）
        invalid_count = sum(
            1 for r in records
            if r.get("quality_status") in (QualityStatus.INVALID, "INVALID")
        )

        # 异常值数量
        outlier_count = 0
        for check in check_report.checks:
            if check.check_type == "OUTLIER":
                outlier_count = check.records_failed
                break

        # 综合评分
        score = self._scorer.compute_from_checks(
            data_type=data_type,
            symbol=symbol,
            total_expected=total_expected,
            total_found=total_found,
            outlier_count=outlier_count,
            conflict_count=conflict_count,
            last_update_seconds_ago=last_update_ago,
            expected_update_interval_seconds=float(expected_interval),
            invalid_count=invalid_count,
            time_range_start=start,
            time_range_end=end,
        )

        # 生成报告
        report = self._reporter.generate_report(
            data_type=data_type,
            symbol=symbol,
            check_report=check_report,
            score=score,
            time_range_start=start,
            time_range_end=end,
        )

        # 持久化
        if persist:
            await self._reporter.persist_report(report)

        logger.info(
            f"质量检查完成: {data_type}/{symbol} "
            f"score={report.total_score:.1f} level={report.level.value} "
            f"records={total_found}/{total_expected}"
        )
        return report

    async def get_quality_score(
        self,
        data_type: str,
        symbol: str,
        *,
        time_range: tuple[datetime, datetime] | None = None,
        interval: str | None = None,
        metric: str | None = None,
    ) -> QualityScore:
        """获取质量评分（不生成完整报告）。

        Args:
            data_type: 数据类型
            symbol: 资产符号
            time_range: 时间范围
            interval: K线周期
            metric: 指标名

        Returns:
            QualityScore 评分结果
        """
        report = await self.check_data_quality(
            data_type=data_type,
            symbol=symbol,
            time_range=time_range,
            interval=interval,
            metric=metric,
            persist=False,
        )
        if report.score is not None:
            return report.score

        # fallback
        return self._scorer.compute_score(
            data_type=data_type,
            symbol=symbol,
            completeness_pct=report.total_score,
        )

    async def get_quality_report(
        self,
        *,
        data_types: Sequence[str] | None = None,
        symbols: Sequence[str] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
    ) -> BatchQualityReport:
        """生成批量质量报告。

        Args:
            data_types: 要检查的数据类型列表；None 时检查所有已配置类型
            symbols: 资产符号列表；None 时默认 BTCUSDT
            time_range: 检查时间范围

        Returns:
            BatchQualityReport 批量报告
        """
        types_to_check = list(data_types) if data_types else list(DATA_TYPE_CONFIG.keys())
        symbols_to_check = list(symbols) if symbols else ["BTCUSDT"]

        reports: list[QualityReport] = []
        for data_type in types_to_check:
            for symbol in symbols_to_check:
                try:
                    report = await self.check_data_quality(
                        data_type=data_type,
                        symbol=symbol,
                        time_range=time_range,
                        persist=False,
                    )
                    reports.append(report)
                except Exception as exc:
                    logger.error(f"质量检查异常: {data_type}/{symbol}: {exc}")
                    # 生成失败报告
                    failed_report = QualityReport(
                        data_type=data_type,
                        symbol=symbol,
                        total_score=0.0,
                        level=QualityLevel.CRITICAL,
                        alert_level="CRITICAL",
                        alert_message=f"检查异常: {exc}",
                        should_alert=True,
                    )
                    reports.append(failed_report)

        batch = self._reporter.generate_batch_report(reports)

        # 持久化汇总
        for report in reports:
            await self._reporter.persist_report(report)

        return batch

    async def get_latest_quality_records(
        self,
        data_type: str | None = None,
        *,
        limit: int = 20,
        status_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """查询最近的质量检查记录。

        Args:
            data_type: 数据类型过滤
            limit: 返回数量上限
            status_filter: 状态过滤（OK / WARNING / ERROR / CRITICAL）

        Returns:
            质量记录列表
        """
        async with get_db_session_ctx() as session:
            stmt = select(DataQuality).order_by(DataQuality.check_time.desc()).limit(limit)

            if status_filter:
                stmt = stmt.where(DataQuality.status == status_filter)

            result = await session.execute(stmt)
            records = result.scalars().all()

            return [
                {
                    "id": str(r.id),
                    "check_time": r.check_time.isoformat() if r.check_time else None,
                    "data_category": r.data_category.value if r.data_category else None,
                    "table_name": r.table_name,
                    "check_type": r.check_type,
                    "status": r.status,
                    "severity": r.severity,
                    "records_checked": r.records_checked,
                    "completeness_pct": float(r.completeness_pct) if r.completeness_pct else None,
                    "issues": r.issues,
                }
                for r in records
            ]

    # ==================================================================
    # 内部实现
    # ==================================================================

    async def _fetch_records(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str | None = None,
        metric: str | None = None,
        limit: int = 10000,
    ) -> list[dict[str, Any]]:
        """从数据库获取指定范围内的数据记录。"""
        config = DATA_TYPE_CONFIG.get(data_type.lower())
        if config is None:
            logger.warning(f"未配置的 data_type: {data_type}，返回空记录")
            return []

        model, _, _ = config

        async with get_db_session_ctx() as session:
            stmt = select(model).where(
                model.observation_time >= start,
                model.observation_time <= end,
            ).limit(limit)

            # 过滤条件
            if hasattr(model, "symbol") and symbol:
                stmt = stmt.where(model.symbol == symbol)
            if hasattr(model, "metric_name") and metric:
                stmt = stmt.where(model.metric_name == metric)
            if hasattr(model, "interval") and interval:
                candle_interval = _parse_interval(interval)
                if candle_interval is not None:
                    stmt = stmt.where(model.interval == candle_interval)

            result = await session.execute(stmt)
            rows = result.scalars().all()

            # 转为字典列表
            records: list[dict[str, Any]] = []
            for row in rows:
                record: dict[str, Any] = {}
                for col in model.__table__.columns:
                    val = getattr(row, col.name, None)
                    if isinstance(val, Decimal):
                        record[col.name] = float(val)
                    elif isinstance(val, datetime):
                        record[col.name] = val
                    else:
                        record[col.name] = val
                records.append(record)

            return records

    def _resolve_interval(self, data_type: str, interval: str | None) -> int:
        """解析期望数据间隔（秒）。"""
        if interval and interval in INTERVAL_SECONDS:
            return INTERVAL_SECONDS[interval]
        config = DATA_TYPE_CONFIG.get(data_type.lower())
        if config:
            return config[1]
        return 3600  # 默认 1 小时

    @staticmethod
    def _calc_expected_records(start: datetime, end: datetime, interval_seconds: int) -> int:
        """计算期望记录数。"""
        if interval_seconds <= 0:
            return 0
        delta = (end - start).total_seconds()
        return max(1, int(delta / interval_seconds))

    async def _get_last_update_age(
        self,
        data_type: str,
        symbol: str,
        interval: str | None = None,
        metric: str | None = None,
    ) -> float | None:
        """获取距上次更新的秒数。"""
        config = DATA_TYPE_CONFIG.get(data_type.lower())
        if config is None:
            return None

        model, _, _ = config

        try:
            async with get_db_session_ctx() as session:
                stmt = select(sa_func.max(model.observation_time))

                if hasattr(model, "symbol") and symbol:
                    stmt = stmt.where(model.symbol == symbol)
                if hasattr(model, "metric_name") and metric:
                    stmt = stmt.where(model.metric_name == metric)
                if hasattr(model, "interval") and interval:
                    candle_interval = _parse_interval(interval)
                    if candle_interval:
                        stmt = stmt.where(model.interval == candle_interval)

                result = await session.execute(stmt)
                last_time = result.scalar()

                if last_time is None:
                    return None

                now = utcnow()
                if last_time.tzinfo is None:
                    last_time = last_time.replace(tzinfo=timezone.utc)
                return (now - last_time).total_seconds()

        except Exception as exc:
            logger.debug(f"获取最后更新时间失败: {exc}")
            return None

    async def _get_conflict_count(
        self,
        data_type: str,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> int:
        """查询时间范围内的冲突记录数。"""
        try:
            async with get_db_session_ctx() as session:
                stmt = select(sa_func.count(DataQuality.id)).where(
                    DataQuality.check_type == "CROSS_VALIDATION",
                    DataQuality.check_time >= start,
                    DataQuality.check_time <= end,
                    DataQuality.status.in_(["WARNING", "ERROR", "CRITICAL"]),
                )
                result = await session.execute(stmt)
                return result.scalar() or 0
        except Exception as exc:
            logger.debug(f"查询冲突记录失败: {exc}")
            return 0


# ==========================================================================
# 辅助函数
# ==========================================================================


def _ensure_utc(value: datetime) -> datetime:
    """将 datetime 归一化为 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_interval(interval: str | None) -> CandleInterval | None:
    """解析K线周期字符串为枚举。"""
    if not interval:
        return None
    aliases: dict[str, CandleInterval] = {
        "1m": CandleInterval.M1, "5m": CandleInterval.M5,
        "15m": CandleInterval.M15, "30m": CandleInterval.M30,
        "1h": CandleInterval.H1, "4h": CandleInterval.H4,
        "1d": CandleInterval.D1, "1w": CandleInterval.W1,
    }
    return aliases.get(interval.lower())


# ==========================================================================
# 全局单例
# ==========================================================================

_quality_engine: DataQualityEngine | None = None


def get_quality_engine() -> DataQualityEngine:
    """获取全局 DataQualityEngine 单例。"""
    global _quality_engine
    if _quality_engine is None:
        _quality_engine = DataQualityEngine()
    return _quality_engine


def set_quality_engine(engine: DataQualityEngine) -> None:
    """设置全局 DataQualityEngine 单例。"""
    global _quality_engine
    _quality_engine = engine


__all__ = [
    "DATA_TYPE_CONFIG",
    "DataQualityEngine",
    "INTERVAL_SECONDS",
    "get_quality_engine",
    "set_quality_engine",
]
