"""市场行情路由（/api/v1/market）。

全部端点通过 :class:`~app.services.market_service.MarketService` 获取数据：
- Service 不可用（Provider 未启动 / 构造失败）-> 503
- ServiceResult.success=False -> 503（附 Provider 层错误信息）
"""

from datetime import datetime

from fastapi import APIRouter, Query

from app.schemas import err, ok, service_unavailable

router = APIRouter(prefix="/market", tags=["Market"])

# 惰性单例（首次请求时基于 ProviderService 的 Manager 构建）
_market_service: object | None = None
_market_service_failed = False


def _get_market_service() -> object:
    """获取 MarketService 惰性单例；构建失败抛 RuntimeError（由端点转 503）。"""
    global _market_service, _market_service_failed
    if _market_service is not None:
        return _market_service
    if _market_service_failed:
        raise RuntimeError("MarketService 构建失败（ProviderService 不可用）")
    try:
        from app.services.market_service import MarketService
        from app.services.provider_service import get_provider_service

        _market_service = MarketService(get_provider_service().manager)
        return _market_service
    except Exception as e:  # noqa: BLE001
        _market_service_failed = True
        raise RuntimeError(f"MarketService 构建失败: {e}") from e


def _from_service_result(result: object):
    """ServiceResult -> 统一响应 dict；失败时返回 JSONResponse(503)。"""
    if not getattr(result, "success", False):
        return err(
            "SERVICE_UNAVAILABLE",
            getattr(result, "error", None) or "Service not available",
            status=503,
        )
    return ok(
        getattr(result, "data", None),
        source=getattr(result, "source", None) or None,
        quality_status=getattr(result, "quality_status", None),
        cache_hit=bool(getattr(result, "is_cached", False)),
    )


@router.get("/price")
async def get_price(
    symbol: str = Query(default="BTCUSDT", description="交易对，如 BTCUSDT / BTC/USDT"),
):
    """获取当前实时价格（带多源交叉验证与短 TTL 缓存）。"""
    try:
        service = _get_market_service()
        result = await service.get_current_price(symbol=symbol)
        return _from_service_result(result)
    except RuntimeError as e:
        return service_unavailable(str(e))
    except Exception as e:  # noqa: BLE001
        return service_unavailable(f"获取价格失败: {e}")


@router.get("/ohlcv")
async def get_ohlcv(
    symbol: str = Query(default="BTCUSDT", description="交易对"),
    interval: str = Query(default="1h", description="K线间隔: 1m/5m/15m/30m/1h/4h/1d/1w"),
    start: datetime | None = Query(default=None, description="起始时间（ISO8601，可选）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO8601，可选）"),
    limit: int = Query(default=500, ge=1, le=5000, description="返回条数上限"),
):
    """获取 K 线序列（本地 candles 表优先，缺失区间自动从 Provider 补齐并落库）。"""
    try:
        service = _get_market_service()
        result = await service.get_ohlcv(
            symbol=symbol, interval=interval, start=start, end=end, limit=limit
        )
        return _from_service_result(result)
    except RuntimeError as e:
        return service_unavailable(str(e))
    except Exception as e:  # noqa: BLE001
        return service_unavailable(f"获取K线失败: {e}")


@router.get("/stats")
async def get_stats(
    symbol: str = Query(default="BTCUSDT", description="交易对"),
):
    """获取 24 小时统计（涨跌幅、成交量、24h 高低价等）。"""
    try:
        service = _get_market_service()
        result = await service.get_24h_stats(symbol=symbol)
        return _from_service_result(result)
    except RuntimeError as e:
        return service_unavailable(str(e))
    except Exception as e:  # noqa: BLE001
        return service_unavailable(f"获取24h统计失败: {e}")


@router.get("/overview")
async def get_market_overview(
    symbol: str = Query(default="BTCUSDT", description="交易对"),
):
    """获取市场概览（价格 + 24h 变化 + 市值 + ATH + 当前回撤）。

    并发聚合多个子请求，部分子源失败时对应字段为 null 并在 metadata 中声明。
    """
    try:
        service = _get_market_service()
        result = await service.get_market_overview(symbol=symbol)
        return _from_service_result(result)
    except RuntimeError as e:
        return service_unavailable(str(e))
    except Exception as e:  # noqa: BLE001
        return service_unavailable(f"获取市场概览失败: {e}")


@router.get("/orderbook")
async def get_orderbook(
    symbol: str = Query(default="BTCUSDT", description="交易对"),
    limit: int = Query(default=20, ge=1, le=500, description="买卖各返回档位数"),
):
    """获取订单簿深度（买卖各 limit 档）。"""
    try:
        service = _get_market_service()
        result = await service.get_order_book(symbol=symbol, limit=limit)
        return _from_service_result(result)
    except RuntimeError as e:
        return service_unavailable(str(e))
    except Exception as e:  # noqa: BLE001
        return service_unavailable(f"获取订单簿失败: {e}")


__all__ = ["router"]
