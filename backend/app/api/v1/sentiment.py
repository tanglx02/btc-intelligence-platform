"""Sentiment API 路由 — 市场情绪（恐惧贪婪指数/社交情绪）。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
Service 调用失败或不可用时返回 503。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

from app.services.provider_service import ServiceResult, get_provider_service
from app.services.sentiment_service import SentimentService

router = APIRouter(prefix="/sentiment", tags=["Sentiment"])

_STARTUP_LOCK = asyncio.Lock()


async def _sentiment_service() -> SentimentService:
    """构造 SentimentService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return SentimentService(ps.manager)


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
            result.error or "情绪数据获取失败",
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


@router.get("/fear-greed")
async def get_fear_greed(
    limit: int = Query(default=1, ge=1, le=365, description="返回最近 N 天（1=仅最新）"),
) -> dict[str, Any]:
    """恐惧贪婪指数：limit=1 返回最新值，>1 返回最近 N 天序列。"""
    try:
        svc = await _sentiment_service()
        if limit <= 1:
            result = await svc.get_fear_greed()
            return _result_payload(result, mode="latest")
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=limit)
        result = await svc.get_fear_greed_history(start, now)
        return _result_payload(
            result, mode="history", limit=limit, start=start.isoformat(), end=now.isoformat()
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取恐惧贪婪指数失败: {}", exc)
        return _unavailable(f"恐惧贪婪指数服务不可用: {exc}")


@router.get("/overview")
async def get_overview(
    symbol: str = Query(default="BTC", description="标的，如 BTC"),
    period: str = Query(default="24h", description="社交情绪统计周期"),
) -> dict[str, Any]:
    """情绪概览：恐惧贪婪 + 社交情绪 + 社交讨论量聚合（逐项降级）。"""
    try:
        svc = await _sentiment_service()
        fear_greed, social, volume = await asyncio.gather(
            svc.get_fear_greed(),
            svc.get_social_sentiment(period, symbol),
            svc.get_social_volume(period, symbol),
        )
        if not any(r.success for r in (fear_greed, social, volume)):
            return _unavailable(
                fear_greed.error or social.error or "情绪概览数据获取失败",
                source=fear_greed.source or social.source,
                symbol=symbol,
            )
        return _ok(
            {
                "fear_greed": fear_greed.data if fear_greed.success else None,
                "social_sentiment": social.data if social.success else None,
                "social_volume": volume.data if volume.success else None,
            },
            symbol=symbol,
            period=period,
            fear_greed_available=fear_greed.success,
            social_available=social.success,
            volume_available=volume.success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取情绪概览失败: {}", exc)
        return _unavailable(f"情绪概览服务不可用: {exc}")


__all__ = ["router"]
