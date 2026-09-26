"""portfolio.rule_engine 规则引擎测试（三态逻辑 + 优先级冲突解决）。"""

from __future__ import annotations

from decimal import Decimal

from app.portfolio.rule_engine import (
    CompositeCondition,
    ConditionResult,
    DistanceFromAthBelow,
    DrawdownExceeds,
    PriceBelow,
    RiskLevelCondition,
    Rule,
    RuleContext,
    RuleEngine,
    invest_fixed,
    multiply_amount,
    skip,
)


def _engine() -> RuleEngine:
    return RuleEngine()


def test_price_below_true_and_false() -> None:
    cond = PriceBelow(value=Decimal("100"))
    assert cond.evaluate(RuleContext(price=Decimal("90"))) == ConditionResult.TRUE
    assert cond.evaluate(RuleContext(price=Decimal("110"))) == ConditionResult.FALSE


def test_missing_data_is_unknown() -> None:
    """数据缺失 -> UNKNOWN（规则不生效）。"""
    cond = PriceBelow(value=Decimal("100"))
    assert cond.evaluate(RuleContext(price=None)) == ConditionResult.UNKNOWN


def test_stale_field_forces_unknown() -> None:
    """字段标记为 STALE -> value_or_none 返回 None -> UNKNOWN。"""
    ctx = RuleContext(price=Decimal("50"), stale_fields={"price"})
    assert PriceBelow(value=Decimal("100")).evaluate(ctx) == ConditionResult.UNKNOWN


def test_distance_from_ath_below() -> None:
    """距 ATH 回撤 ≤ -20% 触发。"""
    cond = DistanceFromAthBelow(lte=Decimal("-0.20"))
    assert cond.evaluate(RuleContext(distance_from_ath=Decimal("-0.30"))) == ConditionResult.TRUE
    assert cond.evaluate(RuleContext(distance_from_ath=Decimal("-0.10"))) == ConditionResult.FALSE


def test_drawdown_exceeds() -> None:
    cond = DrawdownExceeds(threshold=Decimal("0.25"))
    ctx_deep = RuleContext(drawdown_from_recent=Decimal("-0.30"))
    ctx_shallow = RuleContext(drawdown_from_recent=Decimal("-0.10"))
    assert cond.evaluate(ctx_deep) == ConditionResult.TRUE
    assert cond.evaluate(ctx_shallow) == ConditionResult.FALSE


def test_composite_all_kleene() -> None:
    """AND：任一 FALSE -> FALSE；无 FALSE 但有 UNKNOWN -> UNKNOWN。"""
    c = CompositeCondition("all", [PriceBelow(Decimal("100")), PriceBelow(Decimal("200"))])
    assert c.evaluate(RuleContext(price=Decimal("90"))) == ConditionResult.TRUE
    assert c.evaluate(RuleContext(price=Decimal("150"))) == ConditionResult.FALSE
    # price 缺失 -> 两个子条件 UNKNOWN -> AND 结果 UNKNOWN
    assert c.evaluate(RuleContext(price=None)) == ConditionResult.UNKNOWN


def test_composite_any_and_not() -> None:
    any_c = CompositeCondition("any", [PriceBelow(Decimal("50")), PriceBelow(Decimal("200"))])
    assert any_c.evaluate(RuleContext(price=Decimal("150"))) == ConditionResult.TRUE
    not_c = CompositeCondition("not", [PriceBelow(Decimal("100"))])
    assert not_c.evaluate(RuleContext(price=Decimal("150"))) == ConditionResult.TRUE


def test_risk_level_ordered() -> None:
    cond = RiskLevelCondition(gte="HIGH")
    assert cond.evaluate(RuleContext(risk_level="VERY_HIGH")) == ConditionResult.TRUE
    assert cond.evaluate(RuleContext(risk_level="LOW")) == ConditionResult.FALSE
    assert cond.evaluate(RuleContext(risk_level=None)) == ConditionResult.UNKNOWN


def test_multiply_actions_compound() -> None:
    """两条 MULTIPLY 命中：乘数连乘 1.5 × 2 = 3。"""
    rules = [
        Rule(name="r1", priority=1, condition=PriceBelow(Decimal("100")),
             action=multiply_amount(Decimal("1.5"))),
        Rule(name="r2", priority=2, condition=PriceBelow(Decimal("200")),
             action=multiply_amount(Decimal("2"))),
    ]
    action = _engine().evaluate(rules, RuleContext(price=Decimal("90")))
    assert action.amount_multiplier == Decimal("3.0")
    assert set(action.triggered_rules) == {"r1", "r2"}


def test_skip_wins_over_multiply() -> None:
    """SKIP 命中即置 skip，effective_multiplier=0。"""
    rules = [
        Rule(name="buy", priority=1, condition=PriceBelow(Decimal("200")),
             action=multiply_amount(Decimal("2"))),
        Rule(name="halt", priority=2, condition=PriceBelow(Decimal("100")), action=skip()),
    ]
    action = _engine().evaluate(rules, RuleContext(price=Decimal("90")))
    assert action.skip is True
    assert action.effective_multiplier == Decimal("0")


def test_disabled_rule_ignored() -> None:
    rules = [
        Rule(name="off", priority=1, condition=PriceBelow(Decimal("200")),
             action=multiply_amount(Decimal("5")), enabled=False),
    ]
    action = _engine().evaluate(rules, RuleContext(price=Decimal("90")))
    assert action.amount_multiplier == Decimal("1")
    assert action.triggered_rules == []


def test_fixed_amount_overrides_multiplier() -> None:
    """FIXED 命中记录 fixed_amount（覆盖乘数语义由消费方处理）。"""
    rules = [
        Rule(name="fix", priority=1, condition=PriceBelow(Decimal("100")),
             action=invest_fixed(Decimal("500"))),
    ]
    action = _engine().evaluate(rules, RuleContext(price=Decimal("90")))
    assert action.fixed_amount == Decimal("500")


def test_unknown_rule_recorded_not_applied() -> None:
    """数据不可用的规则进 unknown_conditions，不影响基础乘数。"""
    rules = [
        Rule(name="needs_risk", priority=1, condition=RiskLevelCondition(gte="HIGH"),
             action=multiply_amount(Decimal("3"))),
    ]
    action = _engine().evaluate(rules, RuleContext(risk_level=None))
    assert action.amount_multiplier == Decimal("1")
    assert "needs_risk" in action.unknown_conditions
