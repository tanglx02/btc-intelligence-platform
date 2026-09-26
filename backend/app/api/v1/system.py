"""系统运维路由（/api/v1/system）。

提供系统健康检查、统计、任务管理、数据质量报告与 Provider 状态汇总。
数据库查询使用 async session；单表查询失败不影响其余部分（逐项容错）。
"""

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from loguru import logger
from sqlalchemy import func, select, text

from app.core.database import get_db_session_ctx
from app.schemas import err, ok

router = APIRouter(prefix="/system", tags=["System"])

# Redis 健康检查用的惰性客户端
_redis_client: Any = None
_redis_checked = False


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #
def _val(v: Any) -> Any:
    """ORM 字段值 -> JSON 安全值（枚举/Decimal/datetime/UUID）。"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        f = float(v)
        return None if f != f else f
    if isinstance(v, UUID):
        return str(v)
    if isinstance(v, datetime):
        return v.isoformat()
    return getattr(v, "value", None) or str(v)


async def _check_db() -> dict[str, Any]:
    """数据库连通性检查（SELECT 1，带耗时）。"""
    started = time.perf_counter()
    try:
        async with get_db_session_ctx() as session:
            await session.execute(text("SELECT 1"))
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


async def _check_redis() -> dict[str, Any]:
    """Redis 连通性检查（PING，带耗时；客户端惰性复用）。"""
    global _redis_client, _redis_checked
    started = time.perf_counter()
    try:
        if _redis_client is None:
            from redis.asyncio import Redis

            from app.core.config import settings

            _redis_client = Redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
        await _redis_client.ping()
        _redis_checked = True
        return {
            "ok": True,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    except Exception as e:  # noqa: BLE001
        _redis_client = None
        _redis_checked = True
        return {"ok": False, "error": str(e)}


async def _provider_summary() -> dict[str, Any]:
    """Provider 状态汇总：优先取内存 Registry，失败降级查 providers 表。"""
    # 1) Registry 内存态（ProviderService 已启动时可用）
    try:
        from app.services.provider_service import get_provider_service

        entries = get_provider_service().list_providers()
        if entries:
            by_status: dict[str, int] = {}
            items = []
            for e in entries:
                status = _val(e.get("status")) or "UNKNOWN"
                by_status[status] = by_status.get(status, 0) + 1
                items.append(
                    {
                        "name": e.get("name"),
                        "category": _val(e.get("category")),
                        "status": status,
                        "health_state": _val(e.get("health_state")),
                        "priority": e.get("priority"),
                        "is_enabled": e.get("is_enabled"),
                        "is_available": e.get("is_available"),
                    }
                )
            return {
                "source": "registry",
                "total": len(items),
                "by_status": by_status,
                "items": items,
            }
    except Exception as e:  # noqa: BLE001 - 服务未启动等场景
        logger.debug(f"Provider registry summary unavailable: {e}")

    # 2) 数据库态
    try:
        from app.models import Provider

        async with get_db_session_ctx() as session:
            rows = (
                await session.execute(
                    select(Provider.status, func.count()).group_by(Provider.status)
                )
            ).all()
            total = (
                await session.execute(select(func.count()).select_from(Provider))
            ).scalar_one()
        by_status = {_val(status): int(cnt) for status, cnt in rows}
        return {"source": "database", "total": int(total), "by_status": by_status}
    except Exception as e:  # noqa: BLE001
        return {"source": "none", "error": str(e), "total": 0, "by_status": {}}


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("/health")
async def system_health():
    """系统健康检查：DB 连接、Redis 连接与 Provider 状态汇总。"""
    db_info = await _check_db()
    redis_info = await _check_redis()
    providers_info = await _provider_summary()

    # Redis 不可用视为降级而非故障（缓存层可降级）
    overall = "ok" if db_info["ok"] else "degraded"
    if not db_info["ok"] and not redis_info["ok"]:
        overall = "critical"

    return ok(
        {
            "status": overall,
            "database": db_info,
            "redis": redis_info,
            "providers": providers_info,
            "time": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/stats")
async def system_stats():
    """系统统计：核心表行数、最近数据时间与 Provider 数量（逐表容错）。"""
    from app.models import (
        Candle,
        Derivative,
        EtfFlow,
        IndicatorValue,
        MacroSeries,
        MarketPrice,
        OnchainMetric,
        Provider,
        Sentiment,
    )

    # 表名 -> ORM 模型
    tables = {
        "candles": Candle,
        "market_prices": MarketPrice,
        "onchain_metrics": OnchainMetric,
        "etf_flows": EtfFlow,
        "derivatives": Derivative,
        "macro_series": MacroSeries,
        "sentiment": Sentiment,
        "indicator_values": IndicatorValue,
    }

    table_stats: dict[str, Any] = {}
    async with get_db_session_ctx() as session:
        for name, model in tables.items():
            try:
                count = (
                    await session.execute(select(func.count()).select_from(model))
                ).scalar_one()
                entry: dict[str, Any] = {"rows": int(count)}
                time_col = getattr(model, "observation_time", None)
                if time_col is not None:
                    latest = (await session.execute(select(func.max(time_col)))).scalar()
                    entry["latest_observation_time"] = _val(latest)
                table_stats[name] = entry
            except Exception as e:  # noqa: BLE001 - 单表失败不影响整体
                table_stats[name] = {"error": str(e)}

        provider_count = 0
        try:
            provider_count = int(
                (await session.execute(select(func.count()).select_from(Provider))).scalar_one()
            )
        except Exception as e:  # noqa: BLE001
            table_stats["providers"] = {"error": str(e)}

    return ok(
        {
            "tables": table_stats,
            "provider_count": provider_count,
            "time": datetime.now(UTC).isoformat(),
        }
    )


@router.get("/jobs")
async def list_jobs(
    enabled: bool | None = Query(default=None, description="按启用状态过滤"),
    group: str | None = Query(default=None, description="按任务组过滤（MARKET/ONCHAIN/...）"),
    limit: int = Query(default=100, ge=1, le=500, description="返回条数"),
):
    """查询系统调度任务列表（system_jobs 表）。"""
    from app.models import SystemJob

    try:
        stmt = select(SystemJob).order_by(SystemJob.priority, SystemJob.next_run_at).limit(limit)
        if enabled is not None:
            stmt = stmt.where(SystemJob.is_enabled == enabled)
        if group:
            stmt = stmt.where(SystemJob.job_group == group.upper())

        async with get_db_session_ctx() as session:
            rows = (await session.execute(stmt)).scalars().all()

        jobs = [
            {
                "id": _val(j.id),
                "job_name": j.job_name,
                "job_type": j.job_type,
                "job_group": j.job_group,
                "description": j.description,
                "schedule_cron": j.schedule_cron,
                "schedule_interval_seconds": j.schedule_interval_seconds,
                "priority": j.priority,
                "status": _val(j.status),
                "is_enabled": j.is_enabled,
                "last_run_at": _val(j.last_run_at),
                "next_run_at": _val(j.next_run_at),
                "last_duration_ms": j.last_duration_ms,
                "avg_duration_ms": j.avg_duration_ms,
                "retry_count": j.retry_count,
                "consecutive_failures": j.consecutive_failures,
                "last_error": j.last_error,
            }
            for j in rows
        ]
        return ok({"count": len(jobs), "jobs": jobs})
    except Exception as e:  # noqa: BLE001
        return err("DB_ERROR", f"查询任务列表失败: {e}", status=503)


@router.post("/jobs/{job_id}/run")
async def run_job(job_id: UUID):
    """手动触发任务（MVP：登记 next_run_at 状态，实际执行由 Scheduler 完成）。"""
    from app.models import SystemJob

    try:
        async with get_db_session_ctx() as session:
            row = await session.execute(select(SystemJob).where(SystemJob.id == job_id))
            job = row.scalar_one_or_none()
            if job is None:
                return err("NOT_FOUND", f"任务不存在: {job_id}", status=404)
            if not job.is_enabled:
                return err("JOB_DISABLED", f"任务已禁用: {job.job_name}", status=409)

            now = datetime.now(UTC)
            job.next_run_at = now
            job_name = job.job_name

        logger.info(f"Manual trigger registered for job '{job_name}' (next_run_at=now)")
        return ok(
            {
                "job_id": str(job_id),
                "job_name": job_name,
                "queued": True,
                "note": "已登记立即执行，实际执行由 Scheduler 在下个调度周期完成",
            }
        )
    except Exception as e:  # noqa: BLE001
        return err("DB_ERROR", f"触发任务失败: {e}", status=503)


@router.get("/data-quality")
async def data_quality_report(
    status: str | None = Query(default=None, description="按状态过滤（OK/WARNING/ERROR/CRITICAL）"),
    category: str | None = Query(default=None, description="按数据类别过滤（MARKET/ONCHAIN/...）"),
    hours: int = Query(default=24, ge=1, le=720, description="回溯小时数"),
    limit: int = Query(default=100, ge=1, le=500, description="返回条数"),
):
    """数据质量报告（data_quality 表最近记录，按检查时间倒序）。"""
    from app.models import DataQuality

    try:
        since = datetime.now(UTC) - timedelta(hours=hours)
        stmt = (
            select(DataQuality)
            .where(DataQuality.check_time >= since)
            .order_by(DataQuality.check_time.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(DataQuality.status == status.upper())
        if category:
            stmt = stmt.where(DataQuality.data_category == category.upper())

        async with get_db_session_ctx() as session:
            rows = (await session.execute(stmt)).scalars().all()

        records = [
            {
                "check_time": _val(r.check_time),
                "data_category": _val(r.data_category),
                "table_name": r.table_name,
                "check_type": r.check_type,
                "status": r.status,
                "severity": r.severity,
                "records_checked": r.records_checked,
                "records_passed": r.records_passed,
                "records_failed": r.records_failed,
                "completeness_pct": _val(r.completeness_pct),
                "gaps_found": r.gaps_found or [],
                "conflicts_found": r.conflicts_found or [],
                "anomalies_found": r.anomalies_found or [],
                "auto_fixed": r.auto_fixed,
                "requires_manual": r.requires_manual,
            }
            for r in rows
        ]
        return ok(
            {
                "count": len(records),
                "since": since.isoformat(),
                "records": records,
            }
        )
    except Exception as e:  # noqa: BLE001
        return err("DB_ERROR", f"查询数据质量报告失败: {e}", status=503)


@router.get("/providers")
async def provider_status_list():
    """Provider 状态列表（providers 表 LEFT JOIN 最新 provider_health 快照）。"""
    from app.models import Provider, ProviderHealth

    try:
        async with get_db_session_ctx() as session:
            providers = (
                await session.execute(
                    select(Provider).order_by(Provider.category, Provider.priority)
                )
            ).scalars().all()

            # 每个 Provider 最新一条健康快照（PostgreSQL DISTINCT ON）
            health_rows = (
                await session.execute(
                    select(ProviderHealth)
                    .distinct(ProviderHealth.provider_id)
                    .order_by(ProviderHealth.provider_id, ProviderHealth.check_time.desc())
                )
            ).scalars().all()
            latest_health = {h.provider_id: h for h in health_rows}

        items = []
        for p in providers:
            h = latest_health.get(p.id)
            items.append(
                {
                    "id": _val(p.id),
                    "name": p.name,
                    "category": _val(p.category),
                    "base_url": p.base_url,
                    "priority": p.priority,
                    "is_enabled": p.is_enabled,
                    "status": _val(p.status),
                    "health_score": _val(p.health_score),
                    "consecutive_failures": p.consecutive_failures,
                    "last_success_at": _val(p.last_success_at),
                    "last_failure_at": _val(p.last_failure_at),
                    "description": p.description,
                    "health": (
                        {
                            "check_time": _val(h.check_time),
                            "status": _val(h.status),
                            "response_time_ms": h.response_time_ms,
                            "success_rate_1h": _val(h.success_rate_1h),
                            "success_rate_24h": _val(h.success_rate_24h),
                            "is_rate_limited": h.is_rate_limited,
                            "last_error": h.last_error,
                        }
                        if h is not None
                        else None
                    ),
                }
            )
        return ok({"count": len(items), "providers": items})
    except Exception as e:  # noqa: BLE001
        return err("DB_ERROR", f"查询 Provider 状态失败: {e}", status=503)


__all__ = ["router"]
