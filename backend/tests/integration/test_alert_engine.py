"""预警引擎集成测试（无 DB 依赖：mock session + 内存降级组件）。

覆盖：
- 求值器全部叶子条件（threshold/change_pct/range_enter/range_exit/
  cross_above/cross_below/percentile/state_change）；
- 组合条件 Kleene 三值逻辑（AND / OR / NOT / 嵌套）；
- 三态语义：数据缺失 / stale -> UNKNOWN -> 不触发；
- CooldownManager：触发冷却、恢复后重新触发；
- DurationTracker：持续满足 N 秒、中断重置；
- Consecutive：连续 N 次满足、中断重置；
- 条件树 JSON 解析与校验；
- AlertEngine.run_scan_once 全流程（mock DB 会话）。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.sql.selectable import Select

from app.alerts.cooldown import CooldownManager
from app.alerts.conditions.duration import DurationTracker
from app.alerts.evaluator import AlertEvaluator
from app.alerts.schemas import AlertRuleCreate, ConditionNode
from app.alerts.engine import AlertEngine
from app.models.alert import AlertEvent, AlertRule
from app.models.enums import AlertStatus

from tests.integration.conftest import make_context, make_rule


# ===========================================================================
# 求值器：叶子条件
# ===========================================================================
class TestEvaluatorLeafConditions:
    """叶子条件求值。"""

    def setup_method(self) -> None:
        self.evaluator = AlertEvaluator()

    def _eval(self, tree: dict, context):
        return self.evaluator.evaluate(tree, context)

    # ---- threshold ----

    def test_threshold_gt(self):
        ctx = make_context(price=101000.0)
        tree = {"type": "threshold", "metric": "price", "operator": "gt", "value": 100000}
        result = self._eval(tree, ctx)
        assert result.triggered and result.result == "TRUE"

    def test_threshold_lt(self):
        ctx = make_context(price=95000.0)
        tree = {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000}
        assert self._eval(tree, ctx).triggered

    def test_threshold_boundary_gte_lte(self):
        ctx = make_context(price=100000.0)
        assert self._eval(
            {"type": "threshold", "metric": "price", "operator": "gte", "value": 100000},
            ctx,
        ).triggered
        assert self._eval(
            {"type": "threshold", "metric": "price", "operator": "lte", "value": 100000},
            ctx,
        ).triggered

    def test_threshold_indicator_metric(self):
        ctx = make_context(indicators={"mvrv": 3.2})
        tree = {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0}
        assert self._eval(tree, ctx).triggered

    # ---- change_pct ----

    def test_change_pct_window(self):
        ctx = make_context(price_changes={"BTC/USDT": {"24h": -5.3, "1h": 0.4}})
        tree = {"type": "change_pct", "metric": "price", "operator": "lt",
                "value": -5, "window_hours": 24}
        result = self._eval(tree, ctx)
        assert result.triggered
        assert result.evidence[0]["window_hours"] == 24

    def test_change_pct_default_window_24h(self):
        ctx = make_context(price_changes={"BTC/USDT": {"24h": 6.0}})
        tree = {"type": "change_pct", "metric": "price", "operator": "gt", "value": 5}
        assert self._eval(tree, ctx).triggered

    def test_change_pct_missing_window_unknown(self):
        ctx = make_context(price_changes={"BTC/USDT": {"24h": 6.0}})
        tree = {"type": "change_pct", "metric": "price", "operator": "gt",
                "value": 5, "window_hours": 7 * 24}
        result = self._eval(tree, ctx)
        assert result.result == "UNKNOWN" and not result.triggered

    # ---- range_enter / range_exit ----

    def test_range_enter(self):
        ctx = make_context(indicators={"funding_rate": 0.05})
        tree = {"type": "range_enter", "metric": "funding_rate", "low": 0.0, "high": 0.1}
        assert self._eval(tree, ctx).triggered

    def test_range_enter_outside(self):
        ctx = make_context(indicators={"funding_rate": 0.5})
        tree = {"type": "range_enter", "metric": "funding_rate", "low": 0.0, "high": 0.1}
        assert self._eval(tree, ctx).result == "FALSE"

    def test_range_exit(self):
        ctx = make_context(indicators={"funding_rate": 0.5})
        tree = {"type": "range_exit", "metric": "funding_rate", "low": 0.0, "high": 0.1}
        assert self._eval(tree, ctx).triggered

    def test_range_exit_inside(self):
        ctx = make_context(indicators={"funding_rate": 0.05})
        tree = {"type": "range_exit", "metric": "funding_rate", "low": 0.0, "high": 0.1}
        assert self._eval(tree, ctx).result == "FALSE"

    def test_range_missing_value_unknown(self):
        ctx = make_context(indicators={"mvrv": 2.0})
        tree = {"type": "range_enter", "metric": "funding_rate", "low": 0.0, "high": 0.1}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    # ---- cross_above / cross_below ----

    def test_cross_above_fixed_threshold(self):
        now = datetime.now(UTC)
        ctx = make_context(history={"rsi": [(now - timedelta(hours=1), 45.0), (now, 65.0)]})
        tree = {"type": "cross_above", "metric": "rsi", "value": 60}
        result = self._eval(tree, ctx)
        assert result.triggered
        assert result.evidence[0]["prev"] == 45.0
        assert result.evidence[0]["current"] == 65.0

    def test_cross_above_not_crossed(self):
        now = datetime.now(UTC)
        ctx = make_context(history={"rsi": [(now - timedelta(hours=1), 61.0), (now, 65.0)]})
        tree = {"type": "cross_above", "metric": "rsi", "value": 60}
        assert self._eval(tree, ctx).result == "FALSE"

    def test_cross_below(self):
        now = datetime.now(UTC)
        ctx = make_context(history={"rsi": [(now - timedelta(hours=1), 55.0), (now, 35.0)]})
        tree = {"type": "cross_below", "metric": "rsi", "value": 40}
        assert self._eval(tree, ctx).triggered

    def test_cross_with_metric_b_reference(self):
        now = datetime.now(UTC)
        ctx = make_context(
            indicators={"ma50": 50.0},
            history={"price_ma": [(now - timedelta(hours=1), 48.0), (now, 52.0)]},
        )
        tree = {"type": "cross_above", "metric": "price_ma", "metric_b": "ma50"}
        assert self._eval(tree, ctx).triggered

    def test_cross_insufficient_history_unknown(self):
        now = datetime.now(UTC)
        ctx = make_context(history={"rsi": [(now, 65.0)]})
        tree = {"type": "cross_above", "metric": "rsi", "value": 60}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    # ---- percentile ----

    def test_percentile_above(self):
        ctx = make_context(percentiles={"mvrv": 92.0})
        tree = {"type": "percentile", "metric": "mvrv", "percentile": 90}
        assert self._eval(tree, ctx).triggered

    def test_percentile_below(self):
        ctx = make_context(percentiles={"mvrv": 85.0})
        tree = {"type": "percentile", "metric": "mvrv", "percentile": 90}
        assert self._eval(tree, ctx).result == "FALSE"

    def test_percentile_missing_unknown(self):
        ctx = make_context(percentiles={})
        tree = {"type": "percentile", "metric": "mvrv", "percentile": 90}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    # ---- state_change ----

    def test_state_change_detected(self):
        ctx = make_context(
            engine_states={"cycle": "UPTREND"}, engine_prev={"cycle": "RECOVERY"}
        )
        tree = {"type": "state_change", "engine": "cycle"}
        assert self._eval(tree, ctx).triggered

    def test_state_change_no_change(self):
        ctx = make_context(
            engine_states={"cycle": "UPTREND"}, engine_prev={"cycle": "UPTREND"}
        )
        tree = {"type": "state_change", "engine": "cycle"}
        assert self._eval(tree, ctx).result == "FALSE"

    def test_state_change_from_to_filter(self):
        ctx = make_context(
            engine_states={"cycle": "UPTREND"}, engine_prev={"cycle": "DOWNTURN"}
        )
        match = {"type": "state_change", "engine": "cycle",
                 "from_state": "DOWNTURN", "to_state": "UPTREND"}
        assert self._eval(match, ctx).triggered
        mismatch = {"type": "state_change", "engine": "cycle",
                    "from_state": "RECOVERY", "to_state": "UPTREND"}
        assert self._eval(mismatch, ctx).result == "FALSE"

    def test_state_change_missing_engine_unknown(self):
        ctx = make_context(engine_states={}, engine_prev={})
        tree = {"type": "state_change", "engine": "cycle"}
        assert self._eval(tree, ctx).result == "UNKNOWN"


# ===========================================================================
# 组合条件（Kleene 三值逻辑）
# ===========================================================================
class TestCompositeConditions:
    """AND / OR / NOT / 嵌套组合。"""

    def setup_method(self) -> None:
        self.evaluator = AlertEvaluator()

    def _eval(self, tree: dict, context):
        return self.evaluator.evaluate(tree, context)

    def test_and_all_true(self):
        ctx = make_context(price=95000.0, indicators={"mvrv": 3.5})
        tree = {
            "type": "AND",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).triggered

    def test_and_one_false(self):
        ctx = make_context(price=95000.0, indicators={"mvrv": 2.5})
        tree = {
            "type": "AND",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).result == "FALSE"

    def test_or_with_one_true(self):
        ctx = make_context(price=95000.0, indicators={"mvrv": 2.5})
        tree = {
            "type": "OR",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).triggered

    def test_or_all_false(self):
        ctx = make_context(price=100000.0, indicators={"mvrv": 2.5})
        tree = {
            "type": "OR",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).result == "FALSE"

    def test_not(self):
        ctx = make_context(price=100000.0)
        tree = {
            "type": "NOT",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
            ],
        }
        assert self._eval(tree, ctx).triggered

    def test_nested_or_of_ands(self):
        """嵌套：(A AND B) OR (C AND D)。"""
        ctx = make_context(price=100000.0, indicators={"mvrv": 3.5, "funding_rate": 0.2})
        tree = {
            "type": "OR",
            "children": [
                {
                    "type": "AND",
                    "children": [
                        {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                        {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
                    ],
                },
                {
                    "type": "AND",
                    "children": [
                        {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
                        {"type": "threshold", "metric": "funding_rate", "operator": "gt", "value": 0.1},
                    ],
                },
            ],
        }
        result = self._eval(tree, ctx)
        assert result.triggered  # 第二个 AND 成立

    def test_and_unknown_propagation(self):
        """AND：UNKNOWN 不确定（非 FALSE）。"""
        ctx = make_context(price=95000.0)  # mvrv 缺失
        tree = {
            "type": "AND",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).result == "UNKNOWN"

    def test_or_true_overrides_unknown(self):
        """OR：TRUE 直接成立（即使另一分支 UNKNOWN）。"""
        ctx = make_context(price=95000.0)
        tree = {
            "type": "OR",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0},
            ],
        }
        assert self._eval(tree, ctx).result == "TRUE"

    def test_not_unknown_stays_unknown(self):
        ctx = make_context(price=None)
        tree = {
            "type": "NOT",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000},
            ],
        }
        assert self._eval(tree, ctx).result == "UNKNOWN"

    def test_evidence_chain_contains_leaf_details(self):
        ctx = make_context(price=95000.0)
        tree = {"type": "threshold", "metric": "price", "operator": "lt", "value": 98000}
        result = self._eval(tree, ctx)
        assert len(result.evidence) == 1
        assert result.evidence[0]["result"] == "TRUE"
        assert result.evidence[0]["value"] == 95000.0
        assert result.first_true_description() is not None


# ===========================================================================
# 三态语义：数据缺失 / stale
# ===========================================================================
class TestThreeStateSemantics:
    """UNKNOWN：数据缺失不触发、亦不误报。"""

    def setup_method(self) -> None:
        self.evaluator = AlertEvaluator()

    def _eval(self, tree: dict, context):
        return self.evaluator.evaluate(tree, context)

    def test_missing_metric_unknown(self):
        ctx = make_context(price=None)
        tree = {"type": "threshold", "metric": "price", "operator": "gt", "value": 90000}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    def test_stale_metric_unknown(self):
        ctx = make_context(indicators={"mvrv": 3.5}, stale={"mvrv"})
        tree = {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    def test_stale_cross_metric_unknown(self):
        now = datetime.now(UTC)
        ctx = make_context(history={"rsi": [(now, 65.0)]}, stale={"rsi"})
        tree = {"type": "cross_above", "metric": "rsi", "value": 60}
        assert self._eval(tree, ctx).result == "UNKNOWN"

    def test_unknown_never_triggers(self):
        ctx = make_context(price=None)
        tree = {"type": "threshold", "metric": "price", "operator": "gt", "value": 1}
        result = self._eval(tree, ctx)
        assert result.triggered is False and result.result == "UNKNOWN"


# ===========================================================================
# CooldownManager
# ===========================================================================
class TestCooldownManager:
    """冷却与恢复闸门。"""

    async def test_first_trigger_allowed(self):
        mgr = CooldownManager()
        rule = make_rule(condition_tree={}, cooldown_seconds=3600)
        ok, reason = await mgr.can_trigger(rule)
        assert ok and reason is None

    async def test_suppressed_within_cooldown(self):
        mgr = CooldownManager()
        rule = make_rule(
            condition_tree={},
            cooldown_seconds=3600,
            last_triggered_at=datetime.now(UTC) - timedelta(seconds=60),
        )
        ok, reason = await mgr.can_trigger(rule)
        assert not ok and reason == "COOLDOWN"

    async def test_allowed_after_cooldown_expires(self):
        mgr = CooldownManager()
        rule = make_rule(
            condition_tree={},
            cooldown_seconds=60,
            last_triggered_at=datetime.now(UTC) - timedelta(seconds=120),
        )
        ok, reason = await mgr.can_trigger(rule)
        assert ok and reason is None

    async def test_recovery_pending_blocks_retrigger(self):
        mgr = CooldownManager()
        rule = make_rule(condition_tree={}, cooldown_seconds=0)
        await mgr.mark_triggered(rule.id, datetime.now(UTC))
        ok, reason = await mgr.can_trigger(rule)
        assert not ok and reason == "RECOVERY_PENDING"

    async def test_recovered_allows_retrigger(self):
        """恢复后可重新触发（冷却秒数为 0 时）。"""
        mgr = CooldownManager()
        rule = make_rule(condition_tree={}, cooldown_seconds=0)
        await mgr.mark_triggered(rule.id, datetime.now(UTC))
        await mgr.mark_recovered(rule.id, datetime.now(UTC))
        ok, reason = await mgr.can_trigger(rule)
        assert ok and reason is None

    async def test_rate_limit_user_level(self, monkeypatch):
        """用户级每小时限流（内存降级模式）。"""
        # 隔离配置中心：alert.max_per_hour 可能经 env 回退覆盖构造值（热生效语义）
        class _NoOverrideSvc:
            def get_sync(self, key, default=None):
                return default

        monkeypatch.setattr(
            "app.services.settings_service._settings_service", _NoOverrideSvc()
        )

        mgr = CooldownManager(hourly_limit=3)
        uid = uuid4()
        assert await mgr.check_rate_limit(uid) is True
        assert await mgr.check_rate_limit(uid) is True
        assert await mgr.check_rate_limit(uid) is True
        assert await mgr.check_rate_limit(uid) is False

    async def test_rate_limit_system_rule_unlimited(self):
        mgr = CooldownManager(hourly_limit=1)
        for _ in range(5):
            assert await mgr.check_rate_limit(None) is True

    async def test_redis_failure_degrades_to_memory(self):
        """Redis 故障自动降级内存。"""

        class BrokenRedis:
            async def incr(self, key):
                raise ConnectionError("redis down")

        mgr = CooldownManager(redis_client=BrokenRedis())
        uid = uuid4()
        assert mgr._redis is not None
        assert await mgr.check_rate_limit(uid) is True
        assert mgr._redis is None  # 已降级


# ===========================================================================
# DurationTracker
# ===========================================================================
class TestDurationTracker:
    """持续时间跟踪（内存降级模式）。"""

    async def test_first_met_starts_timing(self):
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_duration(rule_id, True, 60) is False  # 开始计时

    async def test_no_duration_config_passthrough(self):
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_duration(rule_id, True, None) is True
        assert await tracker.check_duration(rule_id, True, 0) is True

    async def test_met_after_duration_elapsed(self):
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_duration(rule_id, True, 60) is False
        # 模拟时间流逝：把起始时间拨回 120s 前
        key = f"alert:duration:{rule_id}"
        started, _ = tracker._memory[key]
        tracker._memory[key] = (started - 120, _)
        assert await tracker.check_duration(rule_id, True, 60) is True

    async def test_reset_on_condition_broken(self):
        """中断重置：条件不满足立即清零计时。"""
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_duration(rule_id, True, 60) is False
        assert await tracker.check_duration(rule_id, False, 60) is False
        key = f"alert:duration:{rule_id}"
        assert key not in tracker._memory  # 已重置
        # 重新满足 -> 重新计时
        assert await tracker.check_duration(rule_id, True, 60) is False

    async def test_consecutive_counting(self):
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_consecutive(rule_id, True, 3) is False  # 1
        assert await tracker.check_consecutive(rule_id, True, 3) is False  # 2
        assert await tracker.check_consecutive(rule_id, True, 3) is True   # 3

    async def test_consecutive_reset_on_break(self):
        """中断重置：任一次不满足即清零。"""
        tracker = DurationTracker()
        rule_id = uuid4()
        await tracker.check_consecutive(rule_id, True, 3)
        await tracker.check_consecutive(rule_id, True, 3)
        assert await tracker.check_consecutive(rule_id, False, 3) is False
        assert await tracker.check_consecutive(rule_id, True, 3) is False  # 重新计数
        await tracker.check_consecutive(rule_id, True, 3)
        assert await tracker.check_consecutive(rule_id, True, 3) is True

    async def test_consecutive_disabled_below_2(self):
        tracker = DurationTracker()
        rule_id = uuid4()
        assert await tracker.check_consecutive(rule_id, True, 1) is True
        assert await tracker.check_consecutive(rule_id, True, 0) is True


# ===========================================================================
# 条件树 JSON 解析
# ===========================================================================
class TestConditionTreeParsing:
    """条件树 JSON 解析与校验。"""

    def test_parse_valid_leaf(self):
        node = ConditionNode.model_validate(
            {"type": "threshold", "metric": "price", "operator": "lt", "value": 90000}
        )
        node.validate_tree()
        assert node.metric == "price"

    def test_parse_valid_nested_tree(self):
        node = ConditionNode.model_validate({
            "type": "AND",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 90000},
                {
                    "type": "OR",
                    "children": [
                        {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3},
                        {"type": "state_change", "engine": "cycle"},
                    ],
                },
            ],
        })
        node.validate_tree()

    def test_reject_unknown_type(self):
        node = ConditionNode.model_validate({"type": "bogus", "metric": "price"})
        with pytest.raises(ValueError, match="未知条件类型"):
            node.validate_tree()

    def test_reject_empty_composite(self):
        node = ConditionNode.model_validate({"type": "AND", "children": []})
        with pytest.raises(ValueError, match="必须包含子节点"):
            node.validate_tree()

    def test_reject_not_with_multiple_children(self):
        node = ConditionNode.model_validate({
            "type": "NOT",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 1},
                {"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 1},
            ],
        })
        with pytest.raises(ValueError, match="NOT"):
            node.validate_tree()

    def test_reject_leaf_with_children(self):
        node = ConditionNode.model_validate({
            "type": "threshold",
            "metric": "price",
            "operator": "lt",
            "value": 1,
            "children": [{"type": "threshold", "metric": "mvrv", "operator": "gt", "value": 1}],
        })
        with pytest.raises(ValueError, match="不应包含子节点"):
            node.validate_tree()

    def test_reject_depth_exceeded(self):
        deep = {"type": "threshold", "metric": "price", "operator": "gt", "value": 1}
        for _ in range(6):
            deep = {"type": "AND", "children": [deep]}
        with pytest.raises(ValueError, match="深度"):
            deep and ConditionNode.model_validate(deep).validate_tree()

    def test_collect_metrics_and_engines(self):
        node = ConditionNode.model_validate({
            "type": "AND",
            "children": [
                {"type": "threshold", "metric": "price", "operator": "lt", "value": 90000},
                {"type": "cross_above", "metric": "rsi", "metric_b": "ma50"},
                {"type": "state_change", "engine": "cycle"},
            ],
        })
        assert node.collect_metrics() == {"price", "rsi", "ma50"}
        assert node.collect_engines() == {"cycle"}

    def test_create_rule_schema_fills_condition_text(self):
        data = AlertRuleCreate.model_validate({
            "rule_name": "价格预警",
            "condition_tree": {
                "type": "threshold", "metric": "price", "operator": "lt", "value": 90000,
            },
        })
        assert data.condition_text is not None
        assert "price" in data.condition_text
        assert data.cooldown_seconds == 86400  # 默认 24h

    def test_create_rule_schema_rejects_bad_tree(self):
        with pytest.raises(Exception):
            AlertRuleCreate.model_validate({
                "rule_name": "坏规则",
                "condition_tree": {"type": "bogus", "metric": "price"},
            })

    def test_template_condition_tree_compatible(self):
        """全部预设模板的条件树必须能通过 AlertRuleCreate 校验。"""
        from app.alerts.rule_templates import ALERT_TEMPLATES

        assert len(ALERT_TEMPLATES) >= 5
        for tpl in ALERT_TEMPLATES:
            payload = {"rule_name": tpl["name"], **tpl["defaults"]}
            data = AlertRuleCreate.model_validate(payload)
            assert data.condition_tree is not None


# ===========================================================================
# AlertEngine 全流程（mock DB）
# ===========================================================================
class FakeResult:
    """SQLAlchemy Result 的最小 mock。"""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class FakeSession:
    """AsyncSession 最小 mock：按查询实体返回预置行。"""

    def __init__(self, rules=(), events=()):
        self.rules = list(rules)
        self.events = list(events)
        self.added: list = []

    async def execute(self, stmt):
        if isinstance(stmt, Select):
            descs = stmt.column_descriptions
            entity = descs[0]["entity"] if descs else None
            if entity is AlertEvent:
                # 模拟 WHERE status IN (TRIGGERED, ACKED)
                active = [
                    e for e in self.events
                    if e.status in (AlertStatus.TRIGGERED, AlertStatus.ACKED)
                ]
                return FakeResult(active)
            if entity is AlertRule:
                return FakeResult(self.rules)
        return FakeResult([])

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def get(self, model, pk):
        if model is AlertRule:
            return next((r for r in self.rules if r.id == pk), None)
        if model is AlertEvent:
            return next((e for e in self.events if e.id == pk), None)
        return None


class StubContextBuilder:
    """上下文构建器桩：返回预置 AlertContext。"""

    def __init__(self, ctx):
        self._ctx = ctx
        self.build_calls = 0

    def extract_required_metrics(self, rules):
        return set()

    async def build(self, required_metrics, engines=None, user_id=None):
        self.build_calls += 1
        return self._ctx


def make_event(rule_id, status=AlertStatus.TRIGGERED):
    return SimpleNamespace(id=uuid4(), rule_id=rule_id, status=status,
                           resolved_at=None, severity=None)


@pytest.fixture
def engine_env(monkeypatch):
    """构造带 mock DB 的 AlertEngine 工厂；并记录 dispatch 调用。"""

    def _factory(rules, context, events=()):
        session = FakeSession(rules=rules, events=events)
        builder = StubContextBuilder(context)
        engine = AlertEngine(context_builder=builder, evaluator=AlertEvaluator())

        @asynccontextmanager
        async def _fake_db_ctx():
            yield session

        dispatched: list = []

        async def _noop_dispatch(self, event):
            dispatched.append(event)

        monkeypatch.setattr("app.alerts.engine.get_db_session_ctx", _fake_db_ctx)
        monkeypatch.setattr(AlertEngine, "_try_dispatch", _noop_dispatch)
        return engine, session, builder, dispatched

    return _factory


class TestAlertEngineScan:
    """run_scan_once 全流程。"""

    async def test_triggers_and_persists_event(self, engine_env):
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            cooldown_seconds=0,
        )
        ctx = make_context(price=90000.0)
        engine, session, builder, dispatched = engine_env([rule], ctx)

        report = await engine.run_scan_once()

        assert report["rules_evaluated"] == 1
        assert report["triggered"] == 1
        assert report["errors"] == 0
        assert len(session.added) == 1
        event = session.added[0]
        assert event.rule_id == rule.id
        assert event.title.startswith("[HIGH]")
        assert len(dispatched) == 1
        # 规则计数更新（DB 行回写）
        assert rule.trigger_count == 1
        assert rule.consecutive_triggers == 1
        assert rule.last_triggered_at is not None

    async def test_no_trigger_when_condition_false(self, engine_env):
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
        )
        ctx = make_context(price=100000.0)
        engine, session, _, dispatched = engine_env([rule], ctx)

        report = await engine.run_scan_once()

        assert report["triggered"] == 0
        assert report["recovered"] == 0
        assert session.added == [] and dispatched == []

    async def test_unknown_when_data_missing(self, engine_env):
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "gt", "value": 90000},
        )
        ctx = make_context(price=None)
        engine, _, _, _ = engine_env([rule], ctx)

        report = await engine.run_scan_once()

        assert report["unknown"] == 1
        assert report["triggered"] == 0

    async def test_cooldown_suppresses_repeated_trigger(self, engine_env):
        """触发后冷却期内（等恢复状态）不重复触发。"""
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            cooldown_seconds=3600,
        )
        ctx = make_context(price=90000.0)
        engine, _, _, dispatched = engine_env([rule], ctx)

        first = await engine.run_scan_once()
        assert first["triggered"] == 1

        second = await engine.run_scan_once()
        assert second["triggered"] == 0
        assert second["suppressed"] == 1
        assert len(dispatched) == 1  # 仍只投递过一次

    async def test_recovery_then_retrigger(self, engine_env):
        """条件恢复 -> 事件 RESOLVED -> 条件再满足可重新触发。"""
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            cooldown_seconds=0,
        )
        ctx_true = make_context(price=90000.0)
        engine, session, builder, _ = engine_env([rule], ctx_true)

        # 第 1 轮：触发
        first = await engine.run_scan_once()
        assert first["triggered"] == 1

        # 造一个活跃事件（模拟第 1 轮落库）
        active_event = make_event(rule.id)
        session.events.append(active_event)

        # 第 2 轮：条件不再满足 -> 恢复
        builder._ctx = make_context(price=100000.0)
        second = await engine.run_scan_once()
        assert second["recovered"] == 1
        assert active_event.status == AlertStatus.RESOLVED
        assert active_event.resolved_at is not None
        assert rule.consecutive_triggers == 0

        # 第 3 轮：条件重新满足 -> 再次触发
        builder._ctx = make_context(price=90000.0)
        third = await engine.run_scan_once()
        assert third["triggered"] == 1

    async def test_quality_gate_blocks_low_quality(self, engine_env):
        """数据质量低于门槛 -> 视同 UNKNOWN，不触发。"""
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            min_data_quality="VERIFIED",
        )
        ctx = make_context(price=90000.0, data_quality={"price": "STALE"})
        engine, _, _, _ = engine_env([rule], ctx)

        report = await engine.run_scan_once()
        assert report["unknown"] == 1
        assert report["triggered"] == 0

    async def test_multi_source_requirement(self, engine_env):
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            require_multi_source=True,
        )
        ctx = make_context(price=90000.0, price_sources=1)
        engine, _, builder, _ = engine_env([rule], ctx)

        report = await engine.run_scan_once()
        assert report["triggered"] == 0

        builder._ctx = make_context(price=90000.0, price_sources=2)
        report2 = await engine.run_scan_once()
        assert report2["triggered"] == 1

    async def test_duration_gate(self, engine_env):
        """持续满足 N 秒才触发；首轮仅开始计时。"""
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            duration_seconds=60,
            cooldown_seconds=0,
        )
        ctx = make_context(price=90000.0)
        engine, _, builder, _ = engine_env([rule], ctx)

        first = await engine.run_scan_once()
        assert first["triggered"] == 0  # 开始计时

        # 模拟 120s 已过
        key = f"alert:duration:{rule.id}"
        started, expires = engine._duration._memory[key]
        engine._duration._memory[key] = (started - 120, expires)

        second = await engine.run_scan_once()
        assert second["triggered"] == 1

    async def test_consecutive_gate(self, engine_env):
        """连续 N 次满足才触发。"""
        rule = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            consecutive_count=3,
            cooldown_seconds=0,
        )
        ctx = make_context(price=90000.0)
        engine, session, builder, _ = engine_env([rule], ctx)

        assert (await engine.run_scan_once())["triggered"] == 0  # 1/3
        assert (await engine.run_scan_once())["triggered"] == 0  # 2/3

        # 中断一次（条件不满足） -> 重置
        builder._ctx = make_context(price=100000.0)
        await engine.run_scan_once()

        # 重新累计 1/3、2/3
        builder._ctx = make_context(price=90000.0)
        assert (await engine.run_scan_once())["triggered"] == 0
        assert (await engine.run_scan_once())["triggered"] == 0

        # 第 3 次连续满足 -> 触发
        third = await engine.run_scan_once()
        assert third["triggered"] == 1

    async def test_single_rule_failure_isolated(self, engine_env):
        """单规则求值失败不影响其他规则。"""
        bad = make_rule(
            condition_tree="not-a-dict",  # 非法条件树 -> 求值器抛错 -> errors 计数
            rule_name="bad-rule",
            priority=10,
        )
        good = make_rule(
            condition_tree={"type": "threshold", "metric": "price",
                            "operator": "lt", "value": 95000},
            rule_name="good-rule",
            priority=20,
        )
        ctx = make_context(price=90000.0)
        engine, _, _, _ = engine_env([bad, good], ctx)

        report = await engine.run_scan_once()
        assert report["errors"] >= 1
        assert report["triggered"] == 1  # good-rule 正常触发

    async def test_db_unavailable_skips_scan_gracefully(self, monkeypatch):
        """DB 不可用 -> 跳过本轮，不崩溃。"""
        engine = AlertEngine()

        @asynccontextmanager
        async def _broken_db():
            raise ConnectionError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr("app.alerts.engine.get_db_session_ctx", _broken_db)
        report = await engine.run_scan_once()
        assert report["errors"] == 1
        assert report["db_available"] is False
        assert report["rules_evaluated"] == 0

    async def test_no_rules_short_circuit(self, engine_env):
        engine, _, _, _ = engine_env([], make_context(price=90000.0))
        report = await engine.run_scan_once()
        assert report["rules_evaluated"] == 0
