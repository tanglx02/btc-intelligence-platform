"""ETF API 路由 — 现货 ETF 资金流与持仓。

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

from app.services.etf_service import ETFService
from app.services.provider_service import ServiceResult, get_provider_service

router = APIRouter(prefix="/etf", tags=["ETF"])

_STARTUP_LOCK = asyncio.Lock()

# period 参数 → 天数
_PERIOD_DAYS: dict[str, int] = {"7d": 7, "30d": 30, "90d": 90}


async def _etf_service() -> ETFService:
    """构造 ETFService（复用全局 ProviderService 的 Manager，确保已启动）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ETFService(ps.manager)


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
            result.error or "ETF 数据获取失败",
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


def _parse_period(period: str) -> int:
    """解析 period 参数，非法值抛 400。"""
    days = _PERIOD_DAYS.get(period.strip().lower())
    if days is None:
        raise HTTPException(
            status_code=400,
            detail=f"非法 period 参数: {period!r}，可选值: {', '.join(_PERIOD_DAYS)}",
        )
    return days


@router.get("/flows")
async def get_etf_flows(
    period: str = Query(default="7d", description="统计周期: 7d / 30d / 90d"),
    ticker: str | None = Query(default=None, description="ETF 代码过滤（空为全市场聚合）"),
) -> dict[str, Any]:
    """ETF 资金流：指定周期的每日明细序列 + 周期累计净流入。"""
    try:
        days = _parse_period(period)
        now = datetime.now(timezone.utc)
        end = now
        start = end - timedelta(days=days)
        svc = await _etf_service()
        detail, summary = await asyncio.gather(
            svc.get_historical_flows(start, end, ticker),
            svc.get_net_flow(f"{days}d", end_date=end),
        )
        if not detail.success and not summary.success:
            return _unavailable(
                detail.error or summary.error or "ETF 资金流数据获取失败",
                source=detail.source or summary.source,
            )
        return _ok(
            {
                "period": f"{days}d",
                "ticker": ticker,
                "total_net_flow": summary.data if summary.success else None,
                "daily_flows": detail.data if detail.success else [],
            },
            detail_available=detail.success,
            summary_available=summary.success,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取 ETF 资金流失败: {}", exc)
        return _unavailable(f"ETF 资金流服务不可用: {exc}")


@router.get("/holdings")
async def get_etf_holdings(
    date: datetime | None = Query(default=None, description="快照日期（空为最新）"),
) -> dict[str, Any]:
    """各现货 ETF 持仓量（BTC / USD / AUM）。"""
    try:
        svc = await _etf_service()
        result = await svc.get_holdings(date)
        return _result_payload(result, as_of=date.isoformat() if date else "latest")
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取 ETF 持仓失败: {}", exc)
        return _unavailable(f"ETF 持仓服务不可用: {exc}")


@router.get("/summary")
async def get_etf_summary() -> dict[str, Any]:
    """ETF 汇总：单日净流入、近 7 日累计净流入、最新持仓。"""
    try:
        svc = await _etf_service()
        daily, weekly, holdings = await asyncio.gather(
            svc.get_daily_flow(),
            svc.get_net_flow("7d"),
            svc.get_holdings(),
        )
        if not daily.success and not weekly.success and not holdings.success:
            return _unavailable(
                daily.error or weekly.error or holdings.error or "ETF 汇总数据获取失败",
                source=daily.source or weekly.source or holdings.source,
            )
        return _ok(
            {
                "daily_net_flow": daily.data if daily.success else None,
                "net_flow_7d": weekly.data if weekly.success else None,
                "holdings": holdings.data if holdings.success else None,
            },
            daily_available=daily.success,
            weekly_available=weekly.success,
            holdings_available=holdings.success,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取 ETF 汇总失败: {}", exc)
        return _unavailable(f"ETF 汇总服务不可用: {exc}")


__all__ = ["router"]
