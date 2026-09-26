"""OnChain API 路由 — 链上指标与交易所资金流。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
Service 调用失败或不可用时返回 503。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger

from app.services.onchain_service import OnChainService
from app.services.provider_service import ServiceResult, get_provider_service

router = APIRouter(prefix="/onchain", tags=["OnChain"])

_STARTUP_LOCK = asyncio.Lock()

# 指标名（小写）→ OnChainService 方法名与数据库指标名
_METRIC_METHODS: dict[str, tuple[str, str]] = {
    "mvrv": ("get_mvrv", "MVRV"),
    "sopr": ("get_sopr", "SOPR"),
    "nupl": ("get_nupl", "NUPL"),
    "puell_multiple": ("get_puell_multiple", "PUELL_MULTIPLE"),
    "puell": ("get_puell_multiple", "PUELL_MULTIPLE"),
    "reserve_risk": ("get_reserve_risk", "RESERVE_RISK"),
    "realized_cap": ("get_realized_cap", "REALIZED_CAP"),
    "rhodl": ("get_rhodl", "RHODL"),
    "active_addresses": ("get_active_addresses", "ACTIVE_ADDRESSES"),
    "transaction_count": ("get_transaction_count", "TRANSACTION_COUNT"),
    "hash_rate": ("get_hash_rate", "HASH_RATE"),
    "difficulty": ("get_difficulty", "DIFFICULTY"),
    "lth_supply": ("get_lth_supply", "LTH_SUPPLY"),
    "sth_supply": ("get_sth_supply", "STH_SUPPLY"),
}

# 供应分布路由涉及的指标
_SUPPLY_KEYS: tuple[tuple[str, str], ...] = (
    ("lth", "get_lth_supply"),
    ("sth", "get_sth_supply"),
)


async def _onchain_service() -> OnChainService:
    """构造 OnChainService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return OnChainService(ps.manager)


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
            result.error or "链上数据获取失败",
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


@router.get("/metrics")
async def list_metrics() -> dict[str, Any]:
    """链上指标列表（MVRV/SOPR/NUPL 等，来自指标注册中心 onchain 分类）。"""
    try:
        from app.indicators.registry import list_indicators

        items = list_indicators("onchain")
        return _ok(items, count=len(items))
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取链上指标列表失败: {}", exc)
        return _unavailable(f"指标注册中心不可用: {exc}")


@router.get("/metrics/{name}")
async def get_metric_history(
    name: str,
    start: datetime | None = Query(default=None, description="起始时间（ISO）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO）"),
) -> dict[str, Any]:
    """特定链上指标数据：给定区间返回历史序列，否则返回最新值。"""
    try:
        svc = await _onchain_service()
        key = name.strip().lower()
        if start is not None and end is not None:
            if start > end:
                raise HTTPException(status_code=400, detail="start 不得晚于 end")
            result = await svc.get_metric_history(name, start, end)
            return _result_payload(result, metric=name.upper(), mode="history")
        if key in _METRIC_METHODS:
            method, metric_name = _METRIC_METHODS[key]
            result = await getattr(svc, method)()
            return _result_payload(result, metric=metric_name, mode="latest")
        # 未知指标名：尝试按数据库指标名查询近 30 天序列
        now = datetime.now(timezone.utc)
        result = await svc.get_metric_history(name, now - timedelta(days=30), now)
        return _result_payload(result, metric=name.upper(), mode="history")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取链上指标 {} 失败: {}", name, exc)
        return _unavailable(f"链上指标服务不可用: {exc}")


@router.get("/exchange-flow")
async def get_exchange_flow(
    start: datetime | None = Query(default=None, description="起始时间（ISO）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO）"),
    exchange: str | None = Query(default=None, description="交易所过滤（空为聚合）"),
) -> dict[str, Any]:
    """交易所资金流（流入/流出/净流入历史序列）。"""
    try:
        svc = await _onchain_service()
        now = datetime.now(timezone.utc)
        end = end or now
        start = start or end - timedelta(days=30)
        if start > end:
            raise HTTPException(status_code=400, detail="start 不得晚于 end")
        result = await svc.get_exchange_flows(start, end, exchange)
        return _result_payload(
            result, start=start.isoformat(), end=end.isoformat(), exchange=exchange
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取交易所资金流失败: {}", exc)
        return _unavailable(f"交易所资金流服务不可用: {exc}")


@router.get("/supply")
async def get_supply() -> dict[str, Any]:
    """供应分布（LTH/STH 长短期持有者供应）。"""
    try:
        svc = await _onchain_service()
        results = await asyncio.gather(
            *(getattr(svc, method)() for _, method in _SUPPLY_KEYS)
        )
        data: dict[str, Any] = {}
        sources: list[str] = []
        for (label, _), result in zip(_SUPPLY_KEYS, results):
            data[label] = result.data if result.success else None
            if result.success and result.source:
                sources.append(result.source)
        if all(not r.success for r in results):
            return _unavailable(
                results[0].error or "供应分布数据获取失败", source=results[0].source
            )
        return _ok(data, sources=sources)
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取供应分布失败: {}", exc)
        return _unavailable(f"供应分布服务不可用: {exc}")


__all__ = ["router"]
