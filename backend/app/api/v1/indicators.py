"""指标路由（/api/v1/indicators）。

- ``/list``                — 指标清单（registry 摘要 + 元数据字典合并）
- ``/definitions/{name}``  — 单指标完整定义（普通解释 + 专业详情）
- ``/{name}``              — 实时计算指标（K线数据 -> polars -> calculator）

注意路由声明顺序：``/list`` 与 ``/definitions/{name}`` 必须先于 ``/{name}``，
否则会被通配路径吞掉。
"""

import asyncio
import math
from datetime import date, datetime

import polars as pl
from fastapi import APIRouter, Query, Request

from app.schemas import err, ok

router = APIRouter(prefix="/indicators", tags=["Indicators"])

# 计算端点中保留给 K 线取数的查询参数，其余参数原样传给指标
_RESERVED_PARAMS = {"symbol", "interval", "start", "end", "limit"}


# --------------------------------------------------------------------------- #
# 序列化辅助
# --------------------------------------------------------------------------- #
def _df_to_records(df: pl.DataFrame) -> list[dict]:
    """Polars DataFrame -> JSON 友好记录列表（datetime -> ISO，NaN/Inf -> null）。"""
    records: list[dict] = []
    for row in df.to_dicts():
        clean: dict = {}
        for k, v in row.items():
            if isinstance(v, (datetime, date)):
                clean[k] = v.isoformat()
            elif isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean[k] = None
            elif hasattr(v, "item") and not isinstance(v, (int, float, bool, str)):
                # numpy 标量
                try:
                    item = v.item()
                    clean[k] = (
                        None
                        if isinstance(item, float) and (math.isnan(item) or math.isinf(item))
                        else item
                    )
                except Exception:  # noqa: BLE001
                    clean[k] = str(v)
            else:
                clean[k] = v
        records.append(clean)
    return records


def _coerce_param(raw: str):
    """查询参数字符串 -> int/float/bool/str（供指标 **params 使用）。"""
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            continue
    return raw


async def _fetch_candle_df(
    symbol: str, interval: str, start, end, limit: int
) -> pl.DataFrame:
    """通过 MarketService 拉取 K 线并转为指标计算所需的 polars DataFrame。

    Raises:
        RuntimeError: 数据不可用（Provider 全部失败 / 数据为空）。
    """
    from app.services.market_service import MarketService
    from app.services.provider_service import get_provider_service

    service = MarketService(get_provider_service().manager)
    result = await service.get_ohlcv(
        symbol=symbol, interval=interval, start=start, end=end, limit=limit
    )
    if not result.success:
        raise RuntimeError(result.error or "K线数据不可用")
    candles = result.data or []
    if not candles:
        raise RuntimeError("K线数据为空（本地库与 Provider 均无数据）")

    rows = [
        {
            "time": c.get("timestamp"),
            "open": float(c.get("open")) if c.get("open") is not None else None,
            "high": float(c.get("high")) if c.get("high") is not None else None,
            "low": float(c.get("low")) if c.get("low") is not None else None,
            "close": float(c.get("close")) if c.get("close") is not None else None,
            "volume": float(c.get("volume")) if c.get("volume") is not None else None,
            "quote_volume": (
                float(c.get("quote_volume")) if c.get("quote_volume") is not None else None
            ),
        }
        for c in candles
    ]
    df = pl.DataFrame(
        rows,
        schema={
            "time": pl.Datetime("us"),
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "quote_volume": pl.Float64,
        },
    ).sort("time")
    return df


# --------------------------------------------------------------------------- #
# 端点
# --------------------------------------------------------------------------- #
@router.get("/list")
async def list_indicators(
    category: str | None = Query(
        default=None, description="分类过滤: technical/onchain/derivatives/composite"
    ),
):
    """指标清单：registry 摘要合并元数据字典（双模式解释摘要）。"""
    try:
        from app.indicators.registry import INDICATOR_DEFINITIONS, registry

        summaries = registry.list_indicators(category=category)
        for item in summaries:
            meta = INDICATOR_DEFINITIONS.get(item["name"], {})
            item["unit"] = meta.get("unit")
            item["value_range"] = meta.get("value_range")
            item["description_simple"] = meta.get("description_simple")
            item["consumed_by"] = meta.get("consumed_by", [])

        return ok(
            {
                "count": len(summaries),
                "categories": registry.categories(),
                "indicators": summaries,
            }
        )
    except Exception as e:  # noqa: BLE001
        return err("REGISTRY_ERROR", f"获取指标清单失败: {e}", status=503)


@router.get("/definitions/{name}")
async def get_indicator_definition(name: str):
    """获取指标完整定义（普通用户解释 + 专业模式详情 + 依赖与消费方）。"""
    try:
        from app.indicators.registry import registry

        definition = registry.get_indicator_definition(name)
        return ok(definition)
    except KeyError:
        return err("NOT_FOUND", f"未注册的指标: {name}", status=404)
    except Exception as e:  # noqa: BLE001
        return err("REGISTRY_ERROR", f"获取指标定义失败: {e}", status=503)


@router.get("/{name}")
async def calculate_indicator(
    request: Request,
    name: str,
    symbol: str = Query(default="BTCUSDT", description="交易对"),
    interval: str = Query(default="1d", description="K线间隔: 1m/5m/15m/30m/1h/4h/1d/1w"),
    start: datetime | None = Query(default=None, description="起始时间（ISO8601，可选）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO8601，可选）"),
    limit: int = Query(default=1000, ge=10, le=5000, description="K线数量上限"),
):
    """计算指定指标（查询参数除 symbol/interval/start/end/limit 外全部透传给指标）。

    示例：``GET /api/v1/indicators/tech.rsi?interval=1d&period=21``
    """
    from app.indicators.calculator import calculator
    from app.indicators.registry import registry

    # 1. 指标必须已注册
    try:
        registry.get_indicator(name)
    except KeyError:
        return err(
            "NOT_FOUND",
            f"未注册的指标: {name}（可用: {registry.names()}）",
            status=404,
        )

    # 2. 组装指标参数（保留参数之外的查询参数）
    params: dict = {}
    for key, value in request.query_params.multi_items():
        if key in _RESERVED_PARAMS or key in params:
            continue
        params[key] = _coerce_param(value)

    # 3. 取数
    try:
        df = await _fetch_candle_df(symbol, interval, start, end, limit)
    except RuntimeError as e:
        return err("DATA_UNAVAILABLE", str(e), status=503)
    except Exception as e:  # noqa: BLE001
        return err("DATA_UNAVAILABLE", f"获取K线数据失败: {e}", status=503)

    # 4. 计算（CPU 密集，放线程池避免阻塞事件循环）
    try:
        result = await asyncio.to_thread(
            calculator.calculate_indicator, name, df, **params
        )
    except KeyError as e:
        return err("NOT_FOUND", f"未注册的指标: {e}", status=404)
    except Exception as e:  # noqa: BLE001
        return err(
            "CALCULATION_FAILED",
            f"指标 {name} 计算失败: {e}",
            status=422,
        )

    values = result.values
    return ok(
        {
            "name": result.name,
            "primary_column": result.primary_column,
            "value_columns": result.value_columns,
            "rows_count": values.height,
            "last": _df_to_records(values.tail(1))[0] if values.height else None,
            "rows": _df_to_records(values),
            "metadata": result.metadata,
            "calculated_at": result.calculated_at.isoformat(),
            "input": {"symbol": symbol, "interval": interval, "candles": df.height},
        }
    )


__all__ = ["router"]
