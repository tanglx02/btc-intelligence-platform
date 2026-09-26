"""Derivatives API 路由 — 衍生品指标（资金费率/OI/清算/多空比）。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
Service 调用失败或不可用时返回 503。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

from app.services.derivatives_service import DerivativesService
from app.services.provider_service import ServiceResult, get_provider_service

router = APIRouter(prefix="/derivatives", tags=["Derivatives"])

_STARTUP_LOCK = asyncio.Lock()


async def _derivatives_service() -> DerivativesService:
    """构造 DerivativesService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return DerivativesService(ps.manager)


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
            result.error or "衍生品数据获取失败",
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


@router.get("/funding")
async def get_funding(
    symbol: str = Query(default="BTC/USDT", description="合约对，如 BTC/USDT"),
    limit: int = Query(default=1, ge=1, le=100, description="返回条数"),
) -> dict[str, Any]:
    """资金费率（当期 + 预测）。"""
    try:
        svc = await _derivatives_service()
        result = await svc.get_funding_rate(symbol, limit)
        return _result_payload(result, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取资金费率失败: {}", exc)
        return _unavailable(f"资金费率服务不可用: {exc}")


@router.get("/open-interest")
async def get_open_interest(
    symbol: str = Query(default="BTC/USDT", description="合约对，如 BTC/USDT"),
) -> dict[str, Any]:
    """未平仓合约量（OI）。"""
    try:
        svc = await _derivatives_service()
        result = await svc.get_open_interest(symbol)
        return _result_payload(result, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取未平仓合约失败: {}", exc)
        return _unavailable(f"未平仓合约服务不可用: {exc}")


@router.get("/liquidations")
async def get_liquidations(
    symbol: str = Query(default="BTC/USDT", description="合约对，如 BTC/USDT"),
    period: str = Query(default="1h", description="统计周期，如 1h / 4h / 24h"),
) -> dict[str, Any]:
    """清算数据（多头/空头清算额）。"""
    try:
        svc = await _derivatives_service()
        result = await svc.get_liquidation(symbol, period)
        return _result_payload(result, symbol=symbol, period=period)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取清算数据失败: {}", exc)
        return _unavailable(f"清算数据服务不可用: {exc}")


@router.get("/long-short")
async def get_long_short(
    symbol: str = Query(default="BTC/USDT", description="合约对，如 BTC/USDT"),
) -> dict[str, Any]:
    """多空持仓/账户比。"""
    try:
        svc = await _derivatives_service()
        result = await svc.get_long_short_ratio(symbol)
        return _result_payload(result, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取多空比失败: {}", exc)
        return _unavailable(f"多空比服务不可用: {exc}")


@router.get("/overview")
async def get_overview(
    symbol: str = Query(default="BTC/USDT", description="合约对，如 BTC/USDT"),
) -> dict[str, Any]:
    """衍生品总览：funding + OI + 清算 + 多空比聚合（逐项降级）。"""
    try:
        svc = await _derivatives_service()
        funding, oi, liquidation, long_short = await asyncio.gather(
            svc.get_funding_rate(symbol),
            svc.get_open_interest(symbol),
            svc.get_liquidation(symbol, "1h"),
            svc.get_long_short_ratio(symbol),
        )
        if not any(r.success for r in (funding, oi, liquidation, long_short)):
            return _unavailable(
                funding.error or oi.error or "衍生品总览数据获取失败",
                source=funding.source or oi.source,
                symbol=symbol,
            )
        return _ok(
            {
                "funding": funding.data if funding.success else None,
                "open_interest": oi.data if oi.success else None,
                "liquidations": liquidation.data if liquidation.success else None,
                "long_short": long_short.data if long_short.success else None,
            },
            symbol=symbol,
            funding_available=funding.success,
            oi_available=oi.success,
            liquidation_available=liquidation.success,
            long_short_available=long_short.success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取衍生品总览失败: {}", exc)
        return _unavailable(f"衍生品总览服务不可用: {exc}")


__all__ = ["router"]
