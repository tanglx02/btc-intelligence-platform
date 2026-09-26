"""数据质量任务：价格一致性校验 + 时间序列缺口补洞。

run_quality_check：实时价 vs 本地最新 MarketPrice 偏差检查，结果写入 data_quality 表。
run_gap_detection：日线 K 线缺口检测与自动补洞（GapDetector 一体化流程）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session_ctx
from app.models.enums import ProviderCategory
from app.models.market import MarketPrice
from app.models.system import DataQuality
from app.services.market_service import MarketService
from app.services.provider_service import get_provider_service

_STARTUP_LOCK = asyncio.Lock()

_SYMBOL = "BTCUSDT"
_PROVIDER_SYMBOL = "BTC/USDT"

# 价格一致性阈值（% 偏差）：<=1% OK，<=3% WARNING，>3% ERROR
_OK_THRESHOLD = Decimal("1.0")
_WARN_THRESHOLD = Decimal("3.0")

# DB 最新价格陈旧阈值（秒）：超过则附加 STALE 告警
_STALE_SECONDS = 3600 * 2


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _grade(deviation: Decimal) -> tuple[str, str]:
    """偏差百分比 → (status, severity)。"""
    if deviation <= _OK_THRESHOLD:
        return "OK", "INFO"
    if deviation <= _WARN_THRESHOLD:
        return "WARNING", "MEDIUM"
    return "ERROR", "HIGH"


_STATUS_ORDER = {"OK": 0, "WARNING": 1, "ERROR": 2, "CRITICAL": 3}


def _merge_status(*statuses: str) -> str:
    """取多个状态中最严重的。"""
    return max(statuses, key=lambda s: _STATUS_ORDER.get(s, 0))


async def _latest_db_price(session: AsyncSession) -> MarketPrice | None:
    """本地最新 BTC 价格记录。"""
    return (
        (
            await session.execute(
                select(MarketPrice)
                .where(MarketPrice.symbol == _SYMBOL)
                .order_by(MarketPrice.observation_time.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def run_quality_check() -> dict[str, Any]:
    """价格一致性检查并写入 data_quality 表（任务 quality_check_6h）。

    实时价与本地最新 MarketPrice 偏差 <=1% 为 OK，<=3% 为 WARNING，
    超过为 ERROR；本地数据超过 2 小时未更新附加 STALE 告警。
    """
    ps = await _ensure_provider_service()
    result = await MarketService(ps.manager).get_current_price(_PROVIDER_SYMBOL)
    if not result.success or not isinstance(result.data, dict):
        raise RuntimeError(f"获取实时价格失败: {result.error or '空数据'}")
    live_price = _first_present(result.data, ("price", "last", "last_price", "close"))
    if live_price is None:
        raise RuntimeError("实时价格数据缺少 price 字段")

    now = datetime.now(UTC)
    live_dec = Decimal(str(live_price))

    async with get_db_session_ctx() as session:
        latest = await _latest_db_price(session)
        issues: list[dict[str, Any]] = []
        source_id = None

        if latest is None:
            status, severity = "WARNING", "MEDIUM"
            issues.append(
                {
                    "type": "NO_DB_RECORD",
                    "message": "本地无 MarketPrice 记录，无法对比",
                }
            )
            deviation = None
        else:
            source_id = latest.source_id
            obs_time = latest.observation_time
            if obs_time.tzinfo is None:
                obs_time = obs_time.replace(tzinfo=UTC)
            deviation = abs(live_dec - latest.price) / latest.price * Decimal("100")
            status, severity = _grade(deviation)
            issues.append(
                {
                    "type": "PRICE_DEVIATION",
                    "deviation_pct": float(deviation),
                    "live_price": float(live_dec),
                    "db_price": float(latest.price),
                    "db_observation_time": obs_time.isoformat(),
                }
            )
            stale_seconds = (now - obs_time).total_seconds()
            if stale_seconds > _STALE_SECONDS:
                status = _merge_status(status, "WARNING")
                severity = _merge_status(severity, "MEDIUM")
                issues.append(
                    {
                        "type": "STALE_DATA",
                        "age_seconds": int(stale_seconds),
                        "threshold_seconds": _STALE_SECONDS,
                    }
                )

        record = DataQuality(
            check_time=now,
            source_id=source_id,
            data_category=ProviderCategory.MARKET,
            table_name="market_prices",
            check_type="CONSISTENCY",
            status=status,
            severity=severity,
            records_checked=1,
            records_passed=1 if status == "OK" else 0,
            records_failed=0 if status == "OK" else 1,
            completeness_pct=Decimal("100.000") if status == "OK" else Decimal("0.000"),
            issues=issues,
            time_range_start=latest.observation_time if latest else None,
            time_range_end=now,
        )
        session.add(record)

    logger.info(
        "价格一致性检查完成 status={} deviation={}%",
        status, round(float(deviation), 3) if deviation is not None else "N/A",
    )
    return {
        "check_type": "CONSISTENCY",
        "status": status,
        "deviation_pct": float(deviation) if deviation is not None else None,
        "live_price": float(live_dec),
        "issues": issues,
    }


async def run_gap_detection() -> dict[str, Any]:
    """日线 K 线缺口检测与自动补洞（任务 gap_detection_1d）。

    回看 7 天，期望间隔 86400s（日线）。补洞内部含退避重试，
    部分失败仅告警不抛出（下轮任务继续尝试）。
    """
    from app.services.gap_detector import get_gap_detector

    await _ensure_provider_service()

    now = datetime.now(UTC)
    detector = get_gap_detector()
    result = await detector.detect_and_fill(
        data_type="candles",
        symbol=_SYMBOL,
        start_date=now - timedelta(days=7),
        end_date=now,
        expected_interval=86400,
        interval="1d",
    )
    payload = result.as_dict()
    if result.failed_gaps > 0:
        logger.warning(
            "补洞部分失败: total={} filled={} failed={}",
            result.total_gaps, result.filled_gaps, result.failed_gaps,
        )
    else:
        logger.info(
            "补洞完成: total={} filled={}", result.total_gaps, result.filled_gaps
        )
    return payload


__all__ = ["run_gap_detection", "run_quality_check"]
