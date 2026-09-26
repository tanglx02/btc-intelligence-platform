"""Macro API 路由 — 宏观经济指标与事件日历。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
Service 调用失败或不可用时返回 503。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session
from app.models.macro import MacroEvent
from app.services.macro_service import MacroService
from app.services.provider_service import ServiceResult, get_provider_service

router = APIRouter(prefix="/macro", tags=["Macro"])

_STARTUP_LOCK = asyncio.Lock()

# 与 MacroService._SERIES_ID 一致的指标清单（key → 展示信息）
_MACRO_INDICATORS: tuple[dict[str, str], ...] = (
    {"key": "dxy", "name": "美元指数", "frequency": "DAILY"},
    {"key": "fed_rate", "name": "联邦基金利率", "frequency": "MONTHLY"},
    {"key": "treasury_2y", "name": "美债收益率 2Y", "frequency": "DAILY"},
    {"key": "treasury_5y", "name": "美债收益率 5Y", "frequency": "DAILY"},
    {"key": "treasury_10y", "name": "美债收益率 10Y", "frequency": "DAILY"},
    {"key": "treasury_30y", "name": "美债收益率 30Y", "frequency": "DAILY"},
    {"key": "real_yield", "name": "10Y 实际收益率 (TIPS)", "frequency": "DAILY"},
    {"key": "cpi", "name": "CPI 消费者物价指数", "frequency": "MONTHLY"},
    {"key": "pce", "name": "PCE 物价指数", "frequency": "MONTHLY"},
    {"key": "unemployment", "name": "失业率", "frequency": "MONTHLY"},
    {"key": "nfp", "name": "非农就业人数", "frequency": "MONTHLY"},
    {"key": "gdp", "name": "GDP 国内生产总值", "frequency": "QUARTERLY"},
    {"key": "m2", "name": "M2 货币供应量", "frequency": "MONTHLY"},
    {"key": "fed_balance_sheet", "name": "美联储资产负债表", "frequency": "WEEKLY"},
)

_MACRO_KEYS: frozenset[str] = frozenset(item["key"] for item in _MACRO_INDICATORS)

_IMPORTANCE_LEVELS: frozenset[str] = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})


async def _macro_service() -> MacroService:
    """构造 MacroService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return MacroService(ps.manager)


def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    """统一成功响应。"""
    return {"success": True, "data": data, "meta": meta}


def _unavailable(message: str, **meta: Any) -> JSONResponse:
    """统一 503 响应（数据服务不可用）。"""
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "data": None,
            "error": message or "数据服务暂时不可用",
            "meta": meta,
        },
    )


def _result_payload(result: ServiceResult, **extra_meta: Any) -> Any:
    """ServiceResult → 统一响应（失败时 503）。"""
    if not result.success:
        return _unavailable(
            result.error or "宏观数据获取失败",
            source=result.source,
            quality_status=(
                result.quality_status.value
                if hasattr(result.quality_status, "value")
                else str(result.quality_status)
            ),
            **extra_meta,
        )
    return _ok(
        result.data,
        source=result.source,
        quality_status=(
            result.quality_status.value
            if hasattr(result.quality_status, "value")
            else str(result.quality_status)
        ),
        is_cached=result.is_cached,
        is_stale=result.is_stale,
        is_failover=result.is_failover,
        response_time_ms=round(result.response_time_ms, 1),
        **extra_meta,
    )


@router.get("/indicators")
async def list_indicators() -> dict[str, Any]:
    """宏观指标列表（DXY/利率/CPI/M2 等支持的序列清单）。"""
    return _ok([dict(item) for item in _MACRO_INDICATORS], count=len(_MACRO_INDICATORS))


@router.get("/series/{name}")
async def get_series(
    name: str,
    start: datetime | None = Query(default=None, description="起始时间（ISO）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO）"),
) -> dict[str, Any]:
    """特定宏观指标序列（严格 release_date 过滤防未来数据泄漏）。"""
    try:
        key = name.strip().lower()
        if key not in _MACRO_KEYS:
            raise HTTPException(
                status_code=404,
                detail=f"未知宏观指标: {name!r}，可用: {', '.join(sorted(_MACRO_KEYS))}",
            )
        now = datetime.now(timezone.utc)
        end = end or now
        start = start or end - timedelta(days=365)
        if start > end:
            raise HTTPException(status_code=400, detail="start 不得晚于 end")
        svc = await _macro_service()
        result = await svc.get_macro_series(key, start, end)
        return _result_payload(
            result, indicator=key, start=start.isoformat(), end=end.isoformat()
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取宏观序列 {} 失败: {}", name, exc)
        return _unavailable(f"宏观序列服务不可用: {exc}")


def _event_to_dict(row: MacroEvent) -> dict[str, Any]:
    """MacroEvent ORM → 可序列化 dict。"""
    return {
        "id": str(row.id),
        "event_type": row.event_type,
        "event_name": row.event_name,
        "event_time": row.event_time.isoformat() if row.event_time else None,
        "importance": row.importance,
        "actual_value": row.actual_value,
        "forecast_value": row.forecast_value,
        "previous_value": row.previous_value,
        "description": row.description,
        "market_impact": row.market_impact,
        "btc_price_at_event": (
            float(row.btc_price_at_event)
            if isinstance(row.btc_price_at_event, Decimal)
            else row.btc_price_at_event
        ),
        "is_recurring": row.is_recurring,
        "source_url": row.source_url,
    }


@router.get("/events")
async def list_events(
    start: datetime | None = Query(default=None, description="起始时间（ISO）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO）"),
    event_type: str | None = Query(default=None, description="事件类型过滤，如 FOMC / CPI"),
    importance: str | None = Query(
        default=None, description="重要性过滤: LOW / MEDIUM / HIGH / CRITICAL"
    ),
    upcoming: bool = Query(default=False, description="仅显示未来事件"),
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
) -> dict[str, Any]:
    """宏观经济事件日历（查 macro_events 表）。"""
    try:
        if importance is not None:
            imp = importance.strip().upper()
            if imp not in _IMPORTANCE_LEVELS:
                raise HTTPException(
                    status_code=400,
                    detail=f"非法 importance: {importance!r}，可选: "
                    f"{', '.join(sorted(_IMPORTANCE_LEVELS))}",
                )
            importance = imp
        now = datetime.now(timezone.utc)
        end = end or (now + timedelta(days=30) if upcoming else now)
        start = start or (now if upcoming else now - timedelta(days=30))
        if start > end:
            raise HTTPException(status_code=400, detail="start 不得晚于 end")
        async for session in get_db_session():
            stmt = select(MacroEvent).where(
                MacroEvent.event_time >= start,
                MacroEvent.event_time <= end,
            )
            if event_type:
                stmt = stmt.where(MacroEvent.event_type == event_type.strip().upper())
            if importance:
                stmt = stmt.where(MacroEvent.importance == importance)
            stmt = stmt.order_by(MacroEvent.event_time.desc()).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
            items = [_event_to_dict(r) for r in rows]
            return _ok(
                items,
                count=len(items),
                start=start.isoformat(),
                end=end.isoformat(),
                event_type=event_type,
                importance=importance,
            )
        return _ok([], count=0)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取宏观事件日历失败: {}", exc)
        return _unavailable(f"宏观事件日历不可用: {exc}")


__all__ = ["router"]
