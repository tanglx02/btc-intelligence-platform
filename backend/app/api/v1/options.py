"""Options API 路由 — 期权聚合指标（OI/成交量/IV/Put-Call）。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
Service 调用失败或不可用时返回 503。
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from loguru import logger

from app.services.options_service import OptionsService
from app.services.provider_service import ServiceResult, get_provider_service

router = APIRouter(prefix="/options", tags=["Options"])

_STARTUP_LOCK = asyncio.Lock()


async def _options_service() -> OptionsService:
    """构造 OptionsService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return OptionsService(ps.manager)


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
            result.error or "期权数据获取失败",
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


@router.get("/overview")
async def get_overview(
    symbol: str = Query(default="BTC", description="标的，如 BTC / ETH"),
) -> dict[str, Any]:
    """期权概览：总 OI / 成交量 / Put-Call 比率 / IV 聚合（逐项降级）。"""
    try:
        svc = await _options_service()
        oi, volume, pcr, iv = await asyncio.gather(
            svc.get_options_oi(symbol),
            svc.get_options_volume(symbol),
            svc.get_put_call_ratio(symbol),
            svc.get_implied_volatility(symbol),
        )
        if not any(r.success for r in (oi, volume, pcr, iv)):
            return _unavailable(
                oi.error or volume.error or "期权概览数据获取失败",
                source=oi.source or volume.source,
                symbol=symbol,
            )
        return _ok(
            {
                "open_interest": oi.data if oi.success else None,
                "volume": volume.data if volume.success else None,
                "put_call_ratio": pcr.data if pcr.success else None,
                "iv": iv.data if iv.success else None,
            },
            symbol=symbol,
            oi_available=oi.success,
            volume_available=volume.success,
            pcr_available=pcr.success,
            iv_available=iv.success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取期权概览失败: {}", exc)
        return _unavailable(f"期权概览服务不可用: {exc}")


@router.get("/iv")
async def get_implied_volatility(
    symbol: str = Query(default="BTC", description="标的，如 BTC / ETH"),
) -> dict[str, Any]:
    """隐含波动率（ATM IV + DVOL 指数）。"""
    try:
        svc = await _options_service()
        result = await svc.get_implied_volatility(symbol)
        return _result_payload(result, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取隐含波动率失败: {}", exc)
        return _unavailable(f"隐含波动率服务不可用: {exc}")


@router.get("/put-call")
async def get_put_call(
    symbol: str = Query(default="BTC", description="标的，如 BTC / ETH"),
) -> dict[str, Any]:
    """Put/Call 比率（成交量与未平仓量两个口径）。"""
    try:
        svc = await _options_service()
        result = await svc.get_put_call_ratio(symbol)
        return _result_payload(result, symbol=symbol)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取 Put/Call 比率失败: {}", exc)
        return _unavailable(f"Put/Call 比率服务不可用: {exc}")


__all__ = ["router"]
