"""指标计算任务：加载日线 OHLCV → 批量计算核心指标 → 写入 indicator_values。

技术类（technical）与复合类（composite）指标仅依赖本地 candles 表，无外部 IO。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import polars as pl
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.database import get_db_session_ctx
from app.models.enums import CandleInterval, ProviderCategory, QualityStatus
from app.models.indicator import IndicatorDefinition, IndicatorValue
from app.models.market import Candle
from app.models.provider import Provider
from app.services.provider_service import get_provider_service

_STARTUP_LOCK = asyncio.Lock()

_SYMBOL = "BTCUSDT"
_MIN_CANDLES = 30  # 少于该数量时跳过计算（多数指标需预热窗口）
_MAX_CANDLES = 400

#: indicator_definitions.code → (registry 指标名, 结果取值列, 参数覆盖)。
#: registry 命名（tech.*）与 definitions.code（RSI_14 等）是两套体系，
#: 此处显式桥接；缺定义 / 计算失败 / 取值列无有效值时跳过。
#: 链上 / 衍生品 / 宏观类指标（MVRV 等）依赖外部数据源，不在本地计算范围。
_CODE_SPECS: dict[str, tuple[str, str | None, dict[str, Any]]] = {
    "RSI_14": ("tech.rsi", None, {}),  # value 列，period=14 与默认一致
    "MACD": ("tech.macd", "macd_line", {}),  # 取快线
    "BB_WIDTH": ("tech.bollinger", "bandwidth", {}),
    "MA_200": ("tech.sma", None, {"period": 200}),
}


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


async def _resolve_source_id() -> Any:
    """解析 source_id：市场类别优先级最高的启用 Provider。"""
    async with get_db_session_ctx() as session:
        row = (
            (
                await session.execute(
                    select(Provider)
                    .where(
                        Provider.category == ProviderCategory.MARKET,
                        Provider.is_enabled.is_(True),
                    )
                    .order_by(Provider.priority)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            return row.id
    raise RuntimeError("未找到可用的 MARKET Provider，无法确定 source_id")


def _to_utc_datetime(value: Any) -> datetime | None:
    """date/datetime/epoch → aware datetime（UTC）。"""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    return None


def _is_valid_number(value: Any) -> bool:
    """非空且非 NaN。"""
    if value is None:
        return False
    if isinstance(value, float) and value != value:
        return False
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _extract_latest(
    res_values: pl.DataFrame,
    fallback_time: datetime,
    value_col: str | None = None,
) -> tuple[datetime, float, float | None] | None:
    """从指标结果（首列 time + 数值列）取最后一行有效值 → (observation_time, value, percentile)。

    Args:
        value_col: 指定取值列（如 MACD 的 ``macd_line``、布林带的 ``bandwidth``）；
            None 时取首个非 percentile 数值列（多列结果的首列可能不是目标语义）。
    """
    if res_values.is_empty():
        return None
    tail = res_values.tail(1)
    time_col = tail.columns[0]
    raw_time = tail[time_col][0]
    obs_time = _to_utc_datetime(raw_time) or fallback_time

    value: float | None = None
    percentile: float | None = None
    for col in tail.columns:
        if col == time_col:
            continue
        candidate = tail[col][0]
        if not _is_valid_number(candidate):
            continue
        lowered = col.lower()
        if lowered.endswith("percentile") or lowered == "pct":
            percentile = float(candidate)
        elif value_col is not None:
            if col == value_col and value is None:
                value = float(candidate)
        elif value is None:
            value = float(candidate)
    if value is None:
        return None
    return obs_time, value, percentile


async def calculate_indicators() -> dict[str, Any]:
    """计算核心指标并写入 indicator_values（任务 indicator_calc_1h）。"""
    from app.indicators.calculator import IndicatorCalculator

    await _ensure_provider_service()  # 确保子系统就绪（指标本身仅用本地数据）

    async with get_db_session_ctx() as session:
        rows = (
            (
                await session.execute(
                    select(Candle)
                    .where(Candle.symbol == _SYMBOL, Candle.interval == CandleInterval.D1)
                    .order_by(Candle.observation_time.desc())
                    .limit(_MAX_CANDLES)
                )
            )
            .scalars()
            .all()
        )
        if len(rows) < _MIN_CANDLES:
            raise RuntimeError(f"日线数据不足（{len(rows)} 条 < {_MIN_CANDLES}），跳过指标计算")
        rows = list(reversed(rows))  # 时间升序

        def _col(attr: str) -> list[float | None]:
            return [float(getattr(r, attr)) if getattr(r, attr) is not None else None for r in rows]

        df = pl.DataFrame(
            {
                # 指标基类约定时间列名为 "time"（TIME_COLUMN），缺失时会回退为行号序号
                "time": [r.observation_time.replace(tzinfo=None) for r in rows],
                "open": _col("open"),
                "high": _col("high"),
                "low": _col("low"),
                "close": _col("close"),
                "volume": _col("volume"),
            }
        )

        # 计算 _CODE_SPECS 涉及的 registry 指标（参数按 registry 名合并）
        reg_params: dict[str, dict[str, Any]] = {}
        for _code, (_reg, _col, _overrides) in _CODE_SPECS.items():
            reg_params.setdefault(_reg, dict(_overrides))
        calculator = IndicatorCalculator(enable_cache=False)
        results = calculator.calculate_batch(
            sorted(reg_params), df, params=reg_params, stop_on_error=False
        )
        if not results:
            raise RuntimeError("指标计算无任何成功结果")
        logger.info("指标批量计算完成：{} / {} 个成功", len(results), len(reg_params))

        source_id = await _resolve_source_id()
        definitions = (await session.execute(select(IndicatorDefinition))).scalars().all()
        code_to_id = {d.code: d.id for d in definitions}
        now = datetime.now(UTC)
        fallback_time = rows[-1].observation_time
        if fallback_time.tzinfo is None:
            fallback_time = fallback_time.replace(tzinfo=UTC)

        persisted = 0
        skipped_no_definition = 0
        for code, (reg_name, value_col, _overrides) in _CODE_SPECS.items():
            indicator_id = code_to_id.get(code)
            if indicator_id is None:
                skipped_no_definition += 1
                continue
            result = results.get(reg_name)
            if result is None:
                continue
            extracted = _extract_latest(result.values, fallback_time, value_col)
            if extracted is None:
                continue
            obs_time, value, percentile = extracted
            params_used: dict[str, Any] = {}
            try:
                params_used = dict(result.metadata.get("params", {})) if result.metadata else {}
            except Exception:  # noqa: BLE001
                params_used = {}

            stmt = (
                pg_insert(IndicatorValue.__table__)
                .values(
                    indicator_id=indicator_id,
                    source_id=source_id,
                    observation_time=obs_time,
                    fetch_time=now,
                    symbol=_SYMBOL,
                    value=Decimal(str(value)),
                    percentile=(
                        Decimal(str(percentile)) if percentile is not None else None
                    ),
                    params_used=params_used,
                    metadata={"task": "indicator_calc_1h"},  # __table__ 插入用 DB 列名（非 ORM 属性名）
                    quality_status=QualityStatus.VERIFIED,
                    updated_at=now,
                )
                .on_conflict_do_update(
                    # DB 中是唯一索引（非命名约束），用列组定位冲突目标
                    index_elements=["indicator_id", "symbol", "observation_time"],
                    set_={
                        "value": Decimal(str(value)),
                        "percentile": (
                            Decimal(str(percentile)) if percentile is not None else None
                        ),
                        "params_used": params_used,
                        "fetch_time": now,
                        "quality_status": QualityStatus.VERIFIED,
                        "updated_at": now,
                    },
                )
            )
            await session.execute(stmt)
            persisted += 1

    logger.info(
        "指标持久化完成：persisted={} skipped_no_definition={}", persisted, skipped_no_definition
    )
    return {
        "symbol": _SYMBOL,
        "computed": len(results),
        "persisted": persisted,
        "skipped_no_definition": skipped_no_definition,
    }


__all__ = ["calculate_indicators"]
