"""集成测试共享 fixtures 与工具（无 DB / Redis 依赖）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.alerts.context_builder import AlertContext
from app.models.enums import AlertSeverity


# --------------------------------------------------------------------------- #
# 预警规则 mock 工厂（鸭子类型，规避 DB）
# --------------------------------------------------------------------------- #
def make_rule(
    *,
    condition_tree: dict,
    rule_name: str = "test-rule",
    severity: str = "HIGH",
    priority: int = 50,
    duration_seconds: int | None = None,
    consecutive_count: int = 1,
    cooldown_seconds: int = 0,
    min_data_quality: str = "ESTIMATED",
    require_multi_source: bool = False,
    user_id: Any = None,
    last_triggered_at: datetime | None = None,
    consecutive_triggers: int = 0,
    trigger_count: int = 0,
) -> SimpleNamespace:
    """构造 AlertRule 同形对象（引擎/冷却/求值所需全部属性）。"""
    return SimpleNamespace(
        id=uuid4(),
        user_id=user_id,
        rule_name=rule_name,
        description=f"描述：{rule_name}",
        severity=AlertSeverity(severity),
        category="MARKET",
        target_symbol="BTC",
        condition_tree=condition_tree,
        condition_text=None,
        duration_seconds=duration_seconds,
        consecutive_count=consecutive_count,
        cooldown_seconds=cooldown_seconds,
        min_data_quality=min_data_quality,
        require_multi_source=require_multi_source,
        priority=priority,
        last_triggered_at=last_triggered_at,
        consecutive_triggers=consecutive_triggers,
        trigger_count=trigger_count,
        is_enabled=True,
        state="ACTIVE",
    )


# --------------------------------------------------------------------------- #
# 预警上下文工厂
# --------------------------------------------------------------------------- #
def make_context(
    *,
    price: float | None = 100000.0,
    indicators: dict[str, float] | None = None,
    stale: set[str] | None = None,
    price_changes: dict[str, dict[str, float]] | None = None,
    history: dict[str, list[tuple[datetime, float]]] | None = None,
    percentiles: dict[str, float] | None = None,
    engine_states: dict[str, str] | None = None,
    engine_prev: dict[str, str] | None = None,
    data_quality: dict[str, str] | None = None,
    price_sources: int = 1,
) -> AlertContext:
    """构造 AlertContext（缺省价格 100000）。"""
    ctx = AlertContext(timestamp=datetime.now(UTC))
    if price is not None:
        ctx.prices["BTC/USDT"] = price
    if indicators:
        ctx.indicators.update(indicators)
    if stale:
        ctx.stale_fields.update(stale)
    if price_changes:
        ctx.price_changes.update(price_changes)
    if history:
        ctx.indicator_history.update(history)
    if percentiles:
        ctx.indicator_percentiles.update(percentiles)
    if engine_states:
        ctx.engine_states.update(engine_states)
    if engine_prev:
        ctx.engine_prev_states.update(engine_prev)
    if data_quality:
        ctx.data_quality.update(data_quality)
    ctx.metadata["price_sources"] = price_sources
    return ctx


def hours_ago(n: float) -> datetime:
    """n 小时前的 UTC 时间。"""
    return datetime.now(UTC) - timedelta(hours=n)


@pytest.fixture
def rule_factory():
    """规则工厂 fixture。"""
    return make_rule


@pytest.fixture
def context_factory():
    """上下文工厂 fixture。"""
    return make_context
