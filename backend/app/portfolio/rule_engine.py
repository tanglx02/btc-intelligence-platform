"""可配置加仓规则引擎（对应 15-portfolio-architecture.md §4）。

与回测引擎共用同一实现，保证「回测的策略 = 将来执行的策略」语义一致。

规则格式：``IF <condition> THEN <action>``，每条规则携带 name / priority /
enabled / 普通用户解释文案。

核心设计：
1. 三态求值 —— 条件返回 True / False / None(UNKNOWN)。引用数据 STALE(≥24h)/
   缺失时求值为 UNKNOWN，该规则不生效（视同未命中），**基础定投照常执行**
   （定投纪律优先于条件失效，§4.5）；
2. 优先级与冲突解决（§4.4）：按 priority 升序求值；multiply_amount 连乘；
   skip 优先级最高（命中即 0 投入）；fixed_amount 覆盖累计乘数（后命中者胜）；
   同 priority 冲突取更保守动作并标记 CONFLICT_RESOLVED；
3. 纯计算 —— 无 IO、无副作用、相同 RuleContext + rules 必产生相同 RuleAction。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Protocol

# 风险等级有序枚举（用于 gte/lte 比较）
RISK_ORDER: dict[str, int] = {
    "VERY_LOW": 0, "LOW": 1, "MODERATE": 2,
    "HIGH": 3, "VERY_HIGH": 4, "EXTREME": 5,
}
# 估值等级有序枚举
VALUATION_ORDER: dict[str, int] = {
    "DEEP_UNDERVALUED": 0, "UNDERVALUED": 1, "FAIR": 2,
    "OVERVALUED": 3, "EXTREME_OVERVALUED": 4,
}
# 周期阶段有序枚举
CYCLE_ORDER: dict[str, int] = {
    "DEEP_BEAR": 0, "BEAR": 1, "BOTTOM_BUILDING": 2, "RECOVERY": 3,
    "UPTREND": 4, "ACCELERATION": 5, "DISTRIBUTION": 6,
    "TOP_RISK": 7, "DECLINE": 8,
}
# 组合条件最大嵌套深度（防配置失控，§4.2）
MAX_CONDITION_DEPTH = 3


class ConditionResult(Enum):
    """条件三态求值结果。"""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"  # 引用数据不可用/STALE，规则不生效


@dataclass
class RuleContext:
    """规则求值上下文（EvaluationContext，§4.2）。

    所有值均可为 None（数据缺失），并携带数据可用性标记。上下文由
    DCA Simulator / BacktestEngine 在每个投入日快照当前市场状态后构造。
    """

    # 价格
    price: Optional[Decimal] = None
    ath: Optional[Decimal] = None                 # 历史最高价
    distance_from_ath: Optional[Decimal] = None   # 距 ATH 百分比（负数，如 -0.30）
    recent_high: Optional[Decimal] = None         # 近 N 日高点
    drawdown_from_recent: Optional[Decimal] = None  # 距近期高点回撤（负数）
    # 技术指标
    ma: dict[str, Optional[Decimal]] = field(default_factory=dict)  # {"sma200d": ...}
    indicators: dict[str, Optional[float]] = field(default_factory=dict)  # {"mvrv": 0.8}
    indicator_percentiles: dict[str, Optional[float]] = field(default_factory=dict)
    rsi: Optional[float] = None
    nupl: Optional[float] = None
    mvrv: Optional[float] = None
    funding_annualized: Optional[float] = None
    # 引擎状态
    risk_level: Optional[str] = None
    valuation_state: Optional[str] = None
    cycle_stage: Optional[str] = None
    regime_label: Optional[str] = None
    # 时间
    day_of_week: Optional[int] = None    # 1=周一 ... 7=周日（ISO）
    day_of_month: Optional[int] = None
    date_iso: Optional[str] = None       # "2026-01-01"
    # 计划自身状态
    total_invested: Optional[Decimal] = None
    cash_balance: Optional[Decimal] = None
    days_since_last_invest: Optional[int] = None
    # 数据可用性：字段名 -> 是否 STALE(≥24h)/缺失（True 表示不可用）
    stale_fields: set[str] = field(default_factory=set)

    def is_available(self, field_name: str) -> bool:
        """字段是否可用（非 STALE 且非缺失）。"""
        return field_name not in self.stale_fields

    def value_or_none(self, field_name: str) -> Any:
        """取字段值；若标记为不可用则返回 None（触发 UNKNOWN）。"""
        if not self.is_available(field_name):
            return None
        return getattr(self, field_name, None)


# ---------------------------------------------------------------------------
# 条件（Condition）—— 策略模式，可组合
# ---------------------------------------------------------------------------

class Condition(Protocol):
    """条件协议：evaluate(ctx) -> ConditionResult。"""

    def evaluate(self, ctx: RuleContext) -> ConditionResult: ...

    def explain(self) -> str: ...


def _cmp_decimal(value: Optional[Decimal], lte: Optional[Decimal], gt: Optional[Decimal],
                 gte: Optional[Decimal], lt: Optional[Decimal]) -> ConditionResult:
    """通用数值区间比较（None 值返回 UNKNOWN）。"""
    if value is None:
        return ConditionResult.UNKNOWN
    v = Decimal(value)
    if lte is not None and not (v <= Decimal(lte)):
        return ConditionResult.FALSE
    if lt is not None and not (v < Decimal(lt)):
        return ConditionResult.FALSE
    if gt is not None and not (v > Decimal(gt)):
        return ConditionResult.FALSE
    if gte is not None and not (v >= Decimal(gte)):
        return ConditionResult.FALSE
    return ConditionResult.TRUE


def _cmp_float(value: Optional[float], lte: Optional[float], gt: Optional[float],
               gte: Optional[float], lt: Optional[float]) -> ConditionResult:
    """通用浮点区间比较（None 返回 UNKNOWN）。"""
    if value is None:
        return ConditionResult.UNKNOWN
    if lte is not None and not (value <= lte):
        return ConditionResult.FALSE
    if lt is not None and not (value < lt):
        return ConditionResult.FALSE
    if gt is not None and not (value > gt):
        return ConditionResult.FALSE
    if gte is not None and not (value >= gte):
        return ConditionResult.FALSE
    return ConditionResult.TRUE


@dataclass
class PriceBelow(Condition):
    """price_below(X)：价格低于阈值。"""

    value: Decimal
    field_name: str = "price"

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        return _cmp_decimal(ctx.value_or_none(self.field_name), self.value, None, None, None)

    def explain(self) -> str:
        return f"价格低于 {self.value}"


@dataclass
class PriceAbove(Condition):
    """price_above(X)：价格高于阈值。"""

    value: Decimal
    field_name: str = "price"

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        return _cmp_decimal(ctx.value_or_none(self.field_name), None, None, self.value, None)

    def explain(self) -> str:
        return f"价格高于 {self.value}"


@dataclass
class DistanceFromAthBelow(Condition):
    """distance_from_ath_below(X%)：距历史高点回撤达到 X（区间语义 lte/gt）。

    distance_from_ath 为负数（如 -0.30 表示回撤 30%）。默认 lte=-X。
    """

    lte: Optional[Decimal] = None
    gt: Optional[Decimal] = None

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        val = ctx.value_or_none("distance_from_ath")
        return _cmp_decimal(val, self.lte, self.gt, None, None)

    def explain(self) -> str:
        parts = []
        if self.lte is not None:
            parts.append(f"距 ATH 回撤 ≥ {abs(Decimal(self.lte)) * 100:.0f}%")
        if self.gt is not None:
            parts.append(f"且 < {abs(Decimal(self.gt)) * 100:.0f}%")
        return "".join(parts) or "距 ATH 回撤条件"


@dataclass
class DrawdownExceeds(Condition):
    """drawdown_exceeds(X%)：从近期高点回撤达到阈值（drawdown_from_recent ≤ -X）。"""

    threshold: Decimal

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        val = ctx.value_or_none("drawdown_from_recent")
        return _cmp_decimal(val, -abs(Decimal(self.threshold)), None, None, None)

    def explain(self) -> str:
        return f"近期回撤超过 {abs(Decimal(self.threshold)) * 100:.0f}%"


@dataclass
class IndicatorValue(Condition):
    """indicator_value：指标原始值区间比较（如 mvrv_below / rsi_below / nupl_below）。"""

    code: str
    lte: Optional[float] = None
    gt: Optional[float] = None
    gte: Optional[float] = None
    lt: Optional[float] = None

    def _resolve(self, ctx: RuleContext) -> Optional[float]:
        if not ctx.is_available(f"indicators.{self.code}"):
            return None
        # 优先从 indicators dict 取，回退到同名顶层字段（rsi/nupl/mvrv）
        if self.code in ctx.indicators:
            return ctx.indicators[self.code]
        return getattr(ctx, self.code, None)

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        return _cmp_float(self._resolve(ctx), self.lte, self.gt, self.gte, self.lt)

    def explain(self) -> str:
        if self.lte is not None:
            return f"{self.code} ≤ {self.lte}"
        if self.gte is not None:
            return f"{self.code} ≥ {self.gte}"
        return f"{self.code} 条件"


@dataclass
class IndicatorPercentile(Condition):
    """indicator_percentile：指标历史分位区间比较（0-100）。"""

    code: str
    lte: Optional[float] = None
    gte: Optional[float] = None
    gt: Optional[float] = None
    lt: Optional[float] = None

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        if not ctx.is_available(f"percentiles.{self.code}"):
            return ConditionResult.UNKNOWN
        val = ctx.indicator_percentiles.get(self.code)
        return _cmp_float(val, self.lte, self.gt, self.gte, self.lt)

    def explain(self) -> str:
        if self.lte is not None:
            return f"{self.code} 分位 ≤ {self.lte}%"
        return f"{self.code} 分位条件"


@dataclass
class RiskLevelCondition(Condition):
    """risk_level：有序枚举比较（gte/lte/in）。"""

    gte: Optional[str] = None
    lte: Optional[str] = None
    in_levels: Optional[list[str]] = None

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        level = ctx.value_or_none("risk_level")
        if level is None:
            return ConditionResult.UNKNOWN
        if self.in_levels is not None:
            return ConditionResult.TRUE if level in self.in_levels else ConditionResult.FALSE
        idx = RISK_ORDER.get(level)
        if idx is None:
            return ConditionResult.UNKNOWN
        if self.gte is not None and idx < RISK_ORDER.get(self.gte, 0):
            return ConditionResult.FALSE
        if self.lte is not None and idx > RISK_ORDER.get(self.lte, 99):
            return ConditionResult.FALSE
        return ConditionResult.TRUE

    def explain(self) -> str:
        if self.in_levels:
            return f"风险等级属于 {self.in_levels}"
        if self.gte:
            return f"风险等级 ≥ {self.gte}"
        if self.lte:
            return f"风险等级 ≤ {self.lte}"
        return "风险等级条件"


@dataclass
class StateCondition(Condition):
    """valuation_state / cycle_stage / regime_label：in 集合判定。"""

    field_name: str
    in_values: list[str]

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        val = ctx.value_or_none(self.field_name)
        if val is None:
            return ConditionResult.UNKNOWN
        return ConditionResult.TRUE if val in self.in_values else ConditionResult.FALSE

    def explain(self) -> str:
        return f"{self.field_name} 属于 {self.in_values}"


@dataclass
class DayOfWeek(Condition):
    """day_of_week(X)：ISO 星期（1=周一）。"""

    day: int

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        val = ctx.value_or_none("day_of_week")
        if val is None:
            return ConditionResult.UNKNOWN
        return ConditionResult.TRUE if val == self.day else ConditionResult.FALSE

    def explain(self) -> str:
        return f"星期 {self.day}"


@dataclass
class DayOfMonth(Condition):
    """day_of_month(X)：每月第 N 日。"""

    day: int

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        val = ctx.value_or_none("day_of_month")
        if val is None:
            return ConditionResult.UNKNOWN
        return ConditionResult.TRUE if val == self.day else ConditionResult.FALSE

    def explain(self) -> str:
        return f"每月第 {self.day} 日"


@dataclass
class DateRange(Condition):
    """date_range：日期区间（ISO 字符串闭区间比较）。"""

    start: Optional[str] = None
    end: Optional[str] = None

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        d = ctx.value_or_none("date_iso")
        if d is None:
            return ConditionResult.UNKNOWN
        if self.start and d < self.start:
            return ConditionResult.FALSE
        if self.end and d > self.end:
            return ConditionResult.FALSE
        return ConditionResult.TRUE

    def explain(self) -> str:
        return f"日期在 {self.start or '起始'} ~ {self.end or '至今'}"


@dataclass
class CompositeCondition(Condition):
    """all / any / not 组合条件（深度 ≤ MAX_CONDITION_DEPTH）。

    三态逻辑（Kleene）：
    - AND：任一 FALSE → FALSE；无 FALSE 但有 UNKNOWN → UNKNOWN；全 TRUE → TRUE；
    - OR：任一 TRUE → TRUE；无 TRUE 但有 UNKNOWN → UNKNOWN；全 FALSE → FALSE；
    - NOT：TRUE↔FALSE，UNKNOWN 保持。
    """

    op: str  # "all" / "any" / "not"
    children: list[Condition]
    depth: int = 1

    def __post_init__(self) -> None:
        self.op = self.op.lower()
        if self.op not in {"all", "any", "not"}:
            raise ValueError(f"未知组合算子: {self.op}")
        if self.op == "not" and len(self.children) != 1:
            raise ValueError("not 组合必须有且仅有一个子条件")
        if self.depth > MAX_CONDITION_DEPTH:
            raise ValueError(f"组合条件嵌套深度超过上限 {MAX_CONDITION_DEPTH}")

    def evaluate(self, ctx: RuleContext) -> ConditionResult:
        results = [c.evaluate(ctx) for c in self.children]
        if self.op == "not":
            r = results[0]
            if r == ConditionResult.TRUE:
                return ConditionResult.FALSE
            if r == ConditionResult.FALSE:
                return ConditionResult.TRUE
            return ConditionResult.UNKNOWN
        if self.op == "all":
            if any(r == ConditionResult.FALSE for r in results):
                return ConditionResult.FALSE
            if any(r == ConditionResult.UNKNOWN for r in results):
                return ConditionResult.UNKNOWN
            return ConditionResult.TRUE
        # any
        if any(r == ConditionResult.TRUE for r in results):
            return ConditionResult.TRUE
        if any(r == ConditionResult.UNKNOWN for r in results):
            return ConditionResult.UNKNOWN
        return ConditionResult.FALSE

    def explain(self) -> str:
        joiner = {"all": " 且 ", "any": " 或 ", "not": " 非 "}[self.op]
        inner = joiner.join(f"({c.explain()})" for c in self.children)
        return inner


# ---------------------------------------------------------------------------
# 动作（Action）
# ---------------------------------------------------------------------------

class ActionKind(Enum):
    """动作类型。"""

    MULTIPLY = "multiply_amount"
    FIXED = "fixed_amount"
    EXTRA_BUY = "extra_buy"
    SKIP = "skip"
    PAUSE = "pause"
    NOTIFY_ONLY = "notify_only"


@dataclass
class Action:
    """规则动作。

    kind 决定语义；参数按 kind 使用：
    - MULTIPLY: factor（倍数）
    - FIXED: value（固定金额）
    - EXTRA_BUY: value（额外追加金额）
    - SKIP: 无
    - PAUSE: days
    - NOTIFY_ONLY: message
    """

    kind: ActionKind
    factor: Optional[Decimal] = None
    value: Optional[Decimal] = None
    days: Optional[int] = None
    message: Optional[str] = None

    def explain(self) -> str:
        if self.kind == ActionKind.MULTIPLY:
            return f"投入金额 ×{self.factor}"
        if self.kind == ActionKind.FIXED:
            return f"投入固定金额 {self.value}"
        if self.kind == ActionKind.EXTRA_BUY:
            return f"额外追加 {self.value}"
        if self.kind == ActionKind.SKIP:
            return "本期不投入"
        if self.kind == ActionKind.PAUSE:
            return f"暂停计划 {self.days} 天"
        return f"仅提醒：{self.message or ''}"


# 便捷动作构造器
def multiply_amount(factor: Decimal | float | str) -> Action:
    return Action(ActionKind.MULTIPLY, factor=Decimal(str(factor)))


def invest_fixed(amount: Decimal | float | str) -> Action:
    return Action(ActionKind.FIXED, value=Decimal(str(amount)))


def extra_buy(amount: Decimal | float | str) -> Action:
    return Action(ActionKind.EXTRA_BUY, value=Decimal(str(amount)))


def skip() -> Action:
    return Action(ActionKind.SKIP)


def pause(days: int) -> Action:
    return Action(ActionKind.PAUSE, days=days)


def notify_only(message: str) -> Action:
    return Action(ActionKind.NOTIFY_ONLY, message=message)


# ---------------------------------------------------------------------------
# 规则
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    """单条规则：IF condition THEN action。"""

    name: str
    priority: int
    condition: Condition
    action: Action
    enabled: bool = True
    description: Optional[str] = None  # 普通用户解释文案


@dataclass
class RuleAction:
    """规则引擎求值结果。"""

    amount_multiplier: Decimal = Decimal("1")  # 累计乘数（skip 时为 0）
    fixed_amount: Optional[Decimal] = None      # 命中的固定金额（覆盖乘数）
    extra_buy_amount: Decimal = Decimal("0")    # 额外追加金额
    skip: bool = False
    pause_days: Optional[int] = None
    triggered_rules: list[str] = field(default_factory=list)   # 命中规则名（进决策日志）
    notifications: list[str] = field(default_factory=list)      # 提醒文案
    conflict_resolved: bool = False                             # 是否发生同优先级冲突裁决
    unknown_conditions: list[str] = field(default_factory=list) # 因数据不可用未参与判定的规则

    @property
    def effective_multiplier(self) -> Decimal:
        """最终乘数（skip 优先，为 0）。"""
        return Decimal("0") if self.skip else self.amount_multiplier


class RuleEngine:
    """可配置加仓规则引擎（§4.4 优先级与冲突解决）。"""

    def evaluate(self, rules: list[Rule], context: RuleContext) -> RuleAction:
        """评估所有启用规则，返回最终动作。

        流程：
        1. 过滤 enabled 规则，按 priority 升序排序；
        2. 逐条求值条件：UNKNOWN → 记入 unknown_conditions 不生效；FALSE → 跳过；
           TRUE → 应用动作；
        3. 动作叠加：MULTIPLY 连乘；FIXED 覆盖（后命中者胜）；SKIP 立即置 skip；
           EXTRA_BUY 累加；PAUSE 取最大天数；NOTIFY_ONLY 收集文案；
        4. 同 priority 的互斥动作（FIXED/SKIP 与 MULTIPLY）冲突时取更保守动作。
        """
        result = RuleAction()
        active = sorted((r for r in rules if r.enabled), key=lambda r: r.priority)

        # 记录每个 priority 层级已应用的"互斥动作强度"用于冲突裁决
        seen_priority_action: dict[int, ActionKind] = {}

        for rule in active:
            outcome = rule.condition.evaluate(context)
            if outcome == ConditionResult.UNKNOWN:
                result.unknown_conditions.append(rule.name)
                continue
            if outcome == ConditionResult.FALSE:
                continue

            # 命中
            action = rule.action
            result.triggered_rules.append(rule.name)
            explanation = rule.description or action.explain()

            if action.kind == ActionKind.SKIP:
                result.skip = True
                result.notifications.append(explanation)
                continue
            if action.kind == ActionKind.NOTIFY_ONLY:
                result.notifications.append(explanation)
                continue
            if action.kind == ActionKind.PAUSE:
                days = action.days or 0
                result.pause_days = max(result.pause_days or 0, days)
                result.notifications.append(explanation)
                continue
            if action.kind == ActionKind.EXTRA_BUY:
                result.extra_buy_amount += Decimal(action.value or 0)
                continue

            # 互斥动作冲突裁决（同 priority）
            prev_kind = seen_priority_action.get(rule.priority)
            if prev_kind is not None and prev_kind != action.kind:
                # 同优先级不同互斥动作：取更保守者（投入更少）
                result.conflict_resolved = True
                if self._is_more_conservative(action.kind, prev_kind):
                    pass  # 当前动作更保守，继续应用
                else:
                    continue  # 已有动作更保守，跳过当前
            seen_priority_action[rule.priority] = action.kind

            if action.kind == ActionKind.MULTIPLY:
                result.amount_multiplier *= Decimal(action.factor or 1)
            elif action.kind == ActionKind.FIXED:
                # 后命中的 fixed 覆盖先前（priority 升序 → 数值大者后处理，最终生效）
                result.fixed_amount = Decimal(action.value or 0)

        return result

    @staticmethod
    def _is_more_conservative(a: ActionKind, b: ActionKind) -> bool:
        """判定动作 a 是否比 b 更保守（投入更少）。保守序：SKIP < FIXED < MULTIPLY。"""
        rank = {ActionKind.SKIP: 0, ActionKind.FIXED: 1, ActionKind.MULTIPLY: 2}
        return rank.get(a, 2) < rank.get(b, 2)

    def explain_action(self, base_amount: Decimal, action: RuleAction) -> str:
        """生成普通用户解释文案（§3.2 示例）。

        如：「今日建议投入 7,500 元 = 基础 5,000 × 1.5，原因：...」
        """
        if action.skip:
            reason = "；".join(action.notifications) or "规则命中"
            return f"今日建议不投入（0 元）。原因：{reason}"
        final = action.fixed_amount if action.fixed_amount is not None else (
            base_amount * action.amount_multiplier
        )
        final += action.extra_buy_amount
        parts = [f"今日建议投入 {final} 元"]
        if action.fixed_amount is not None:
            parts.append(f"= 固定金额 {action.fixed_amount}")
        else:
            parts.append(f"= 基础 {base_amount} × {action.amount_multiplier}")
        if action.extra_buy_amount > 0:
            parts.append(f"+ 额外追加 {action.extra_buy_amount}")
        if action.triggered_rules:
            parts.append("，触发规则：" + "、".join(action.triggered_rules))
        if action.notifications:
            parts.append("。提醒：" + "；".join(action.notifications))
        if action.unknown_conditions:
            parts.append(
                f"。（{('、'.join(action.unknown_conditions))} 因数据不可用未参与判定）"
            )
        return "".join(parts)


# ---------------------------------------------------------------------------
# 配置解析：把 StrategyConfig 的 JSON 规则转为 Rule 对象（§6.1）
# ---------------------------------------------------------------------------

_CONDITION_BUILDERS: dict[str, Any] = {
    "price_below": lambda p: PriceBelow(Decimal(str(p["value"])), p.get("field", "price")),
    "price_above": lambda p: PriceAbove(Decimal(str(p["value"])), p.get("field", "price")),
    "price_vs_ma": lambda p: _build_price_vs_ma(p),
    "drawdown_from_ath": lambda p: DistanceFromAthBelow(
        Decimal(str(p["lte"])) if p.get("lte") is not None else None,
        Decimal(str(p["gt"])) if p.get("gt") is not None else None,
    ),
    "drawdown_from_recent_high": lambda p: DrawdownExceeds(
        Decimal(str(abs(p.get("lte", p.get("threshold", 0)))))
    ),
    "indicator_value": lambda p: IndicatorValue(
        p["code"],
        lte=p.get("lte"), gt=p.get("gt"), gte=p.get("gte"), lt=p.get("lt"),
    ),
    "indicator_percentile": lambda p: IndicatorPercentile(
        p["code"], lte=p.get("lte"), gte=p.get("gte"), gt=p.get("gt"), lt=p.get("lt"),
    ),
    "funding_annualized": lambda p: IndicatorValue(
        "funding_annualized", gte=p.get("gte"), lte=p.get("lte"),
    ),
    "risk_level": lambda p: RiskLevelCondition(
        gte=p.get("gte"), lte=p.get("lte"), in_levels=p.get("in"),
    ),
    "valuation_state": lambda p: StateCondition("valuation_state", p.get("in", [])),
    "cycle_stage": lambda p: StateCondition("cycle_stage", p.get("in", [])),
    "regime_label": lambda p: StateCondition("regime_label", p.get("in", [])),
    "date_range": lambda p: DateRange(p.get("from"), p.get("to")),
    "day_of_month": lambda p: DayOfMonth(int(p["eq"])),
    "day_of_week": lambda p: DayOfWeek(int(p["eq"])),
}


def _build_price_vs_ma(params: dict[str, Any]) -> Condition:
    """price_vs_ma：价格相对均线（{ma:"sma200d", op:"below"}）。"""
    ma_key = params.get("ma", "sma200d")
    op = params.get("op", "below")

    @dataclass
    class _PriceVsMa(Condition):
        def evaluate(self, ctx: RuleContext) -> ConditionResult:
            price = ctx.value_or_none("price")
            ma_val = ctx.ma.get(ma_key) if ctx.is_available(f"ma.{ma_key}") else None
            if price is None or ma_val is None:
                return ConditionResult.UNKNOWN
            if op == "below":
                return ConditionResult.TRUE if Decimal(price) < Decimal(ma_val) else ConditionResult.FALSE
            return ConditionResult.TRUE if Decimal(price) > Decimal(ma_val) else ConditionResult.FALSE

        def explain(self) -> str:
            return f"价格{'低于' if op == 'below' else '高于'} {ma_key}"

    return _PriceVsMa()


_ACTION_BUILDERS: dict[str, Any] = {
    "multiply_amount": lambda p: multiply_amount(p["factor"]),
    "fixed_amount": lambda p: invest_fixed(p["value"]),
    "extra_buy": lambda p: extra_buy(p["value"]),
    "skip": lambda p: skip(),
    "pause": lambda p: pause(int(p.get("days", 0))),
    "notify_only": lambda p: notify_only(p.get("message", "")),
}


def build_condition(spec: dict[str, Any], depth: int = 1) -> Condition:
    """从 JSON 条件规格递归构造 Condition（支持 all/any/not 嵌套）。"""
    if depth > MAX_CONDITION_DEPTH:
        raise ValueError(f"条件嵌套深度超过上限 {MAX_CONDITION_DEPTH}")
    for logic in ("all", "any", "not"):
        if logic in spec:
            children_spec = spec[logic]
            if logic == "not":
                children_spec = [children_spec] if isinstance(children_spec, dict) else children_spec
            children = [build_condition(c, depth + 1) for c in children_spec]
            return CompositeCondition(logic, children, depth)
    op = spec.get("op")
    if op not in _CONDITION_BUILDERS:
        raise ValueError(f"未知条件算子: {op!r}")
    return _CONDITION_BUILDERS[op](spec)


def build_action(spec: dict[str, Any]) -> Action:
    """从 JSON 动作规格构造 Action。"""
    op = spec.get("op")
    if op not in _ACTION_BUILDERS:
        raise ValueError(f"未知动作算子: {op!r}")
    return _ACTION_BUILDERS[op](spec)


def build_rule(spec: dict[str, Any]) -> Rule:
    """从 StrategyConfig 单条规则 JSON 构造 Rule（§6.1 格式）。"""
    return Rule(
        name=spec.get("id", spec.get("name", "rule")),
        priority=int(spec.get("priority", 50)),
        condition=build_condition(spec["if"]),
        action=build_action(spec["then"]),
        enabled=bool(spec.get("enabled", True)),
        description=spec.get("description"),
    )


def build_rules(specs: list[dict[str, Any]]) -> list[Rule]:
    """批量构造规则列表。"""
    return [build_rule(s) for s in specs]
