"""市场数据采集任务：当前价格 + 最近日线 K 线。

任务函数自行初始化依赖（ProviderService 全局单例惰性启动），
异常向上抛出由调度器记录（不影响其他任务）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.enums import CandleInterval, ProviderCategory
from app.models.provider import Provider
from app.services.data_store import CandleRecord, PriceRecord, get_normalized_data_store
from app.services.market_service import MarketService
from app.services.provider_service import get_provider_service

_STARTUP_LOCK = asyncio.Lock()

_SYMBOL = "BTCUSDT"

# 价格数据常见字段别名（宽容解析不同 Provider 的返回结构）
_PRICE_KEYS = ("price", "last", "last_price", "close")
_BID_KEYS = ("bid", "best_bid")
_ASK_KEYS = ("ask", "best_ask")
_VOLUME_KEYS = ("volume_24h", "volume", "vol_24h")
_HIGH_KEYS = ("high_24h", "high")
_LOW_KEYS = ("low_24h", "low")
_CHANGE_KEYS = ("price_change_pct_24h", "change_pct_24h", "price_change_percent_24h")


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性，仅一次）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


async def _resolve_source_id(name: str | None = None, category: str = "MARKET") -> Any:
    """解析 Provider source_id：指定名称优先，回退类别内优先级最高的启用 Provider。"""
    async with get_db_session_ctx() as session:
        if name:
            row = (
                (await session.execute(select(Provider).where(Provider.name == name)))
                .scalars()
                .first()
            )
            if row is not None:
                return row.id
        cat = ProviderCategory(category)
        row = (
            (
                await session.execute(
                    select(Provider)
                    .where(Provider.category == cat, Provider.is_enabled.is_(True))
                    .order_by(Provider.priority)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return row.id
    raise RuntimeError(f"未找到可用的 {category} Provider，无法确定 source_id")


def _dec_or_none(value: Any) -> Decimal | None:
    """任意数值 → Decimal（失败返回 None）。"""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        return None


def _first_present(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _parse_time(row: dict[str, Any]) -> datetime | None:
    """K 线行时间字段解析（datetime / epoch 秒或毫秒 / ISO 字符串）。"""
    raw = None
    for key in ("time", "timestamp", "date", "open_time"):
        if row.get(key) is not None:
            raw = row[key]
            break
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:  # 毫秒
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(raw, str):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _extract_rows(data: Any) -> list[dict[str, Any]]:
    """宽容提取 K 线行列表（list[dict] / dataclass / 嵌套 dict）。"""
    if isinstance(data, list):
        rows: list[dict[str, Any]] = []
        for item in data:
            if isinstance(item, dict):
                rows.append(item)
            elif hasattr(item, "__dict__"):
                rows.append(vars(item))
        return rows
    if isinstance(data, dict):
        for key in ("candles", "data", "bars", "items", "rows"):
            if isinstance(data.get(key), list):
                return _extract_rows(data[key])
    return []


async def collect_market_data() -> dict[str, Any]:
    """采集当前 BTC 价格并写入 market_prices 表（任务 market_1m）。"""
    ps = await _ensure_provider_service()
    result = await MarketService(ps.manager).get_current_price(_SYMBOL)
    if not result.success or not isinstance(result.data, dict):
        raise RuntimeError(f"获取当前价格失败: {result.error or '空数据'}")
    price = _first_present(result.data, _PRICE_KEYS)
    if price is None:
        raise RuntimeError("当前价格数据缺少 price 字段")

    source_id = await _resolve_source_id(None, "MARKET")
    now = datetime.now(UTC)
    record = PriceRecord(
        source_id=source_id,
        observation_time=now,
        symbol=_SYMBOL,
        price=_dec_or_none(price),
        bid=_dec_or_none(_first_present(result.data, _BID_KEYS)),
        ask=_dec_or_none(_first_present(result.data, _ASK_KEYS)),
        volume_24h=_dec_or_none(_first_present(result.data, _VOLUME_KEYS)),
        high_24h=_dec_or_none(_first_present(result.data, _HIGH_KEYS)),
        low_24h=_dec_or_none(_first_present(result.data, _LOW_KEYS)),
        price_change_pct_24h=_dec_or_none(_first_present(result.data, _CHANGE_KEYS)),
        quality_status=result.quality_status,
        metadata={"task": "market_1m", "source": result.source},
    )
    store_result = await get_normalized_data_store().store_prices([record])
    logger.info(
        "价格采集完成 price={} inserted={} updated={} skipped={}",
        price, store_result.inserted, store_result.updated, store_result.skipped_duplicate,
    )
    return {
        "symbol": _SYMBOL,
        "price": float(price),
        "stored": store_result.inserted + store_result.updated,
        "skipped_duplicate": store_result.skipped_duplicate,
        "rejected_invalid": store_result.rejected_invalid,
    }


async def collect_candles() -> dict[str, Any]:
    """采集最近 BTC 日线 K 线（回看 3 天）并写入 candles 表（任务 market_candles_15m）。"""
    ps = await _ensure_provider_service()
    svc = MarketService(ps.manager)
    now = datetime.now(UTC)
    result = await svc.get_ohlcv(
        _SYMBOL, interval="1d", start=now - timedelta(days=3), end=now, limit=10
    )
    if not result.success:
        raise RuntimeError(f"获取 K 线失败: {result.error or '未知错误'}")
    rows = _extract_rows(result.data)
    if not rows:
        return {"symbol": _SYMBOL, "candles": 0, "note": "无 K 线数据返回"}

    source_id = await _resolve_source_id(None, "MARKET")
    records: list[CandleRecord] = []
    for row in rows:
        obs_time = _parse_time(row)
        close = _dec_or_none(_first_present(row, ("close",)))
        if obs_time is None or close is None:
            continue
        records.append(
            CandleRecord(
                source_id=source_id,
                observation_time=obs_time,
                symbol=_SYMBOL,
                interval=CandleInterval.D1,
                open=_dec_or_none(row.get("open")),
                high=_dec_or_none(row.get("high")),
                low=_dec_or_none(row.get("low")),
                close=close,
                volume=_dec_or_none(row.get("volume")),
                quote_volume=_dec_or_none(row.get("quote_volume")),
                trades=row.get("trades") if isinstance(row.get("trades"), int) else None,
                quality_status=result.quality_status,
                metadata={"task": "market_candles_15m", "source": result.source},
            )
        )
    if not records:
        return {"symbol": _SYMBOL, "candles": 0, "note": "K 线行均缺少时间或收盘价"}

    store_result = await get_normalized_data_store().store_candles(records)
    logger.info(
        "K 线采集完成 received={} stored={} skipped={}",
        len(records), store_result.inserted + store_result.updated, store_result.skipped_duplicate,
    )
    return {
        "symbol": _SYMBOL,
        "received": len(records),
        "stored": store_result.inserted + store_result.updated,
        "skipped_duplicate": store_result.skipped_duplicate,
        "rejected_invalid": store_result.rejected_invalid,
    }


__all__ = ["collect_candles", "collect_market_data"]
