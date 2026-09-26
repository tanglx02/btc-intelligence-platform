"""策略基类与内置策略（对应 14-backtest-architecture.md §1.2 / §6）。

设计原则：
- **策略无状态副作用**：同一 (bar, portfolio, context) 输入必产生同一 Signal
  输出（§1.2）。策略不直接访问数据库，全部市场数据由引擎通过 PointInTimeDataFeed
  预取后封装为 ``MarketContext`` 传入——这既保证 Point-in-Time 防泄漏，又使策略
  成为可独立测试的纯函数；
- **规则同源**：DCA 类策略与 portfolio.rule_engine 共用规则求值逻辑，保证
  「回测的策略 = 将来执行的策略」（§6.1）；
- on_bar 为同步方法：数据访问的异步性由引擎在调用前完成（DataHandler 准备
  IndicatorEvent），策略只消费已就绪的 PIT 上下文。

内置策略：
- FixedDCAStrategy：固定定投（基准策略）
- DipBuyStrategy：逢跌加仓（ATH 回撤梯度）
- DrawdownBuyStrategy：回撤加仓（近 N 日高点回撤梯度）
- ValuationDCAStrategy：估值定投（MVRV/NUPL 低估多投）
- RiskAdjustedDCAStrategy：风险平价（Risk 反向缩放）
- RuleBasedStrategy：自定义规则组合（StrategyConfig）
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.backtest.types import BacktestConfig, Bar, PortfolioState, Side, Signal
from app.portfolio.rule_engine import Rule, RuleContext, RuleEngine

# ATH 回撤梯度默认表（§6.2 逢跌加仓）
DEFAULT_DIP_GRADIENT: list[tuple[Decimal, Decimal]] = [
    (Decimal("-0.40"), Decimal("2.0")),
    (Decimal("-0.30"), Decimal("1.5")),
    (Decimal("-0.20"), Decimal("1.2")),
    (Decimal("-0.10"), Decimal("1.0")),
]
# 风险平价默认倍数表（§6.2）
DEFAULT_RISK_MULTIPLIERS: dict[str, Decimal] = {
    "VERY_LOW": Decimal("1.3"), "LOW": Decimal("1.2"), "MODERATE": Decimal("1.0"),
    "HIGH": Decimal("0.5"), "VERY_HIGH": Decimal("0.3"), "EXTREME": Decimal("0.2"),
}
# 估值定投默认梯度（MVRV 分位 -> 倍数，§6.2）
DEFAULT_VALUATION_GRADIENT: list[tuple[float, Decimal]] = [
    (15.0, Decimal("3.0")),
    (30.0, Decimal("2.0")),
    (85.0, Decimal("0.5")),
]


@dataclass
class MarketContext:
    """单根 Bar 的 Point-in-Time 市场上下文（由引擎预取后传入策略）。

    所有字段均为「截至 bar 时间（含）当时可见」的数据；缺失字段为 None，
    策略据此按 §4.5 降级语义处理（数据不可用时该条件不生效，基础定投照常）。
    """

    as_of: date
    price: Decimal
    ath: Decimal | None = None                     # 截至当日历史最高
    distance_from_ath: Decimal | None = None       # 距 ATH（负数，如 -0.30）
    recent_high: Decimal | None = None             # 近 N 日高点
    drawdown_from_recent: Decimal | None = None    # 距近期高点回撤（负数）
    is_investment_day: bool = False                   # 是否为定投投入日
    base_amount: Decimal = Decimal("0")               # 本期基础投入金额
    indicators: dict[str, float | None] = field(default_factory=dict)
    indicator_percentiles: dict[str, float | None] = field(default_factory=dict)
    risk_level: str | None = None
    cycle_stage: str | None = None
    valuation_level: str | None = None
    regime: str | None = None
    day_of_week: int | None = None
    day_of_month: int | None = None


class Strategy(ABC):
    """策略基类。

    子类实现 :meth:`on_bar`；DCA 类策略继承 :class:`DCAStrategyBase` 复用
    投入日调度与金额裁剪逻辑。
    """

    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}
        self._config: BacktestConfig | None = None

    def on_start(self, config: BacktestConfig) -> None:
        """回测开始时注入配置（引擎调用）。"""
        self._config = config

    @property
    def config(self) -> BacktestConfig | None:
        return self._config

    @abstractmethod
    def on_bar(
        self, bar: Bar, portfolio: PortfolioState, context: MarketContext
    ) -> Signal | None:
        """每根 K 线触发，返回交易信号或 None。

        Args:
            bar: 当前 K 线。
            portfolio: 当前组合状态（现金、持仓）。
            context: PIT 市场上下文（引擎预取）。

        Returns:
            Signal（含方向、金额/数量、乘数、触发原因）或 None（不交易）。
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# DCA 策略基类
# ---------------------------------------------------------------------------

class DCAStrategyBase(Strategy):
    """DCA 类策略基类：处理投入日调度、基础金额与约束裁剪。

    子类只需实现 :meth:`compute_multiplier` 返回本期投入倍数。
    """

    name = "dca_base"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        self._invest_dates: set[date] | None = None
        self._initial_done = False

    # ---- 投入日调度 ----

    def is_investment_day(self, d: date) -> bool:
        """判断某日是否为投入日（引擎亦可用此预置 context.is_investment_day）。"""
        if self._invest_dates is None:
            self._build_schedule()
        return d in (self._invest_dates or set())

    def _build_schedule(self) -> None:
        """按 config 频率预生成投入日集合（确定性，无 IO）。"""
        cfg = self._config
        if cfg is None:
            self._invest_dates = set()
            return
        freq = (cfg.frequency or "MONTHLY").upper()
        start, end = cfg.start_date, cfg.end_date
        day = cfg.dca_day
        dates: set[date] = set()
        if freq == "DAILY":
            cur = start
            while cur <= end:
                dates.add(cur)
                cur += timedelta(days=1)
        elif freq == "WEEKLY":
            target = min(max(day, 1), 7)
            cur = start
            while cur <= end:
                if cur.isoweekday() == target:
                    dates.add(cur)
                cur += timedelta(days=1)
        elif freq == "BIWEEKLY":
            cur = start
            while cur <= end:
                dates.add(cur)
                cur += timedelta(days=14)
        else:  # MONTHLY
            dom = min(max(day, 1), 28)
            y, m = start.year, start.month
            while True:
                try:
                    d = date(y, m, dom)
                except ValueError:
                    d = date(y, m, 28)
                if d > end:
                    break
                if d >= start:
                    dates.add(d)
                m += 1
                if m > 12:
                    m, y = 1, y + 1
        self._invest_dates = dates

    # ---- 金额裁剪（约束终审，§4.4）----

    def apply_constraints(self, amount: Decimal, portfolio: PortfolioState) -> Decimal:
        """应用 max_single_buy 与 cash_reserve 约束裁剪投入金额。"""
        cfg = self._config
        if amount <= 0:
            return Decimal("0")
        if cfg is not None and cfg.max_single_buy is not None and amount > cfg.max_single_buy:
            amount = Decimal(cfg.max_single_buy)
        if cfg is not None and cfg.cash_reserve > 0:
            available = portfolio.cash - cfg.cash_reserve
            if amount > available:
                amount = max(available, Decimal("0"))
        return amount

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        """子类实现：返回 (投入倍数, 触发原因, 命中规则列表)。"""
        return Decimal("1"), "SCHEDULED_DCA", []

    def on_bar(
        self, bar: Bar, portfolio: PortfolioState, context: MarketContext
    ) -> Signal | None:
        """投入日产生 BUY 信号；首日投入初始资金。"""
        d = context.as_of
        # 首日一次性投入初始资金
        cfg = self._config
        if (
            not self._initial_done
            and cfg is not None
            and cfg.initial_capital > 0
            and d >= cfg.start_date
        ):
            self._initial_done = True
            amount = self.apply_constraints(cfg.initial_capital, portfolio)
            if amount > 0 and amount <= portfolio.cash:
                return Signal(
                    time=bar.time, direction=Side.BUY, target_amount=amount,
                    reason="INITIAL_CAPITAL", multiplier=Decimal("1"),
                )
            # 初始资金不足现金时仍尝试（现金由引擎注入），否则跳过
            if amount > 0:
                return Signal(
                    time=bar.time, direction=Side.BUY, target_amount=cfg.initial_capital,
                    reason="INITIAL_CAPITAL", multiplier=Decimal("1"),
                )

        # 非投入日不交易
        is_invest = context.is_investment_day or self.is_investment_day(d)
        if not is_invest:
            return None
        base = context.base_amount
        if base is None or base <= 0:
            if cfg is not None and cfg.periodic_investment:
                base = Decimal(cfg.periodic_investment)
            else:
                return None
        multiplier, reason, triggered = self.compute_multiplier(context)
        amount = base * multiplier
        amount = self.apply_constraints(amount, portfolio)
        if amount <= 0:
            return Signal(
                time=bar.time, direction=Side.BUY, target_amount=Decimal("0"),
                multiplier=Decimal("0"), reason=f"{reason}_SKIP", triggered_rules=triggered,
            )
        return Signal(
            time=bar.time, direction=Side.BUY, target_amount=amount,
            multiplier=multiplier, reason=reason, triggered_rules=triggered,
        )


# ---------------------------------------------------------------------------
# 内置策略
# ---------------------------------------------------------------------------

class FixedDCAStrategy(DCAStrategyBase):
    """固定定投策略（§6.2 基准策略）：每期投入固定金额，无条件规则。"""

    name = "fixed_dca"

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        return Decimal("1"), "SCHEDULED_DCA", []


class DipBuyStrategy(DCAStrategyBase):
    """逢跌加仓策略（§6.2）：按距 ATH 回撤梯度放大投入。"""

    name = "dip_buy"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        grad = self.params.get("gradient")
        self.gradient: list[tuple[Decimal, Decimal]] = (
            [(Decimal(str(t)), Decimal(str(f))) for t, f in grad]
            if grad else DEFAULT_DIP_GRADIENT
        )

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        dist = context.distance_from_ath
        if dist is None:
            return Decimal("1"), "DIP_BUY", []
        for threshold, factor in self.gradient:
            if dist <= threshold:
                return factor, "DIP_BUY", [f"ath_dd≤{threshold}"]
        return Decimal("1"), "DIP_BUY", []


class DrawdownBuyStrategy(DCAStrategyBase):
    """回撤加仓策略（§3.2 dip_recent）：按距近 N 日高点回撤梯度放大投入。"""

    name = "drawdown_buy"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        grad = self.params.get("gradient")
        self.gradient: list[tuple[Decimal, Decimal]] = (
            [(Decimal(str(t)), Decimal(str(f))) for t, f in grad]
            if grad else DEFAULT_DIP_GRADIENT
        )

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        dd = context.drawdown_from_recent
        if dd is None:
            return Decimal("1"), "DRAWDOWN_BUY", []
        for threshold, factor in self.gradient:
            if dd <= threshold:
                return factor, "DRAWDOWN_BUY", [f"recent_dd≤{threshold}"]
        return Decimal("1"), "DRAWDOWN_BUY", []


class ValuationDCAStrategy(DCAStrategyBase):
    """估值定投策略（§6.2）：MVRV/NUPL 低估多投、高估少投。

    默认：MVRV 分位 <30% ×2、<15% ×3、>85% ×0.5；NUPL <0.25 ×1.3。
    数据不可用（分位缺失）时退化为基础定投（§4.5）。
    """

    name = "valuation_dca"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        grad = self.params.get("gradient")
        self.gradient: list[tuple[float, Decimal]] = (
            [(float(p), Decimal(str(f))) for p, f in grad]
            if grad else DEFAULT_VALUATION_GRADIENT
        )
        self.indicator = self.params.get("indicator", "onchain.mvrv")

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        triggered: list[str] = []
        multiplier = Decimal("1")
        pct = context.indicator_percentiles.get(self.indicator)
        pct = pct if pct is not None else context.indicator_percentiles.get("mvrv")
        if pct is not None:
            # 梯度按分位升序判定（<15 优先于 <30），高估档单独处理
            for threshold, factor in sorted(self.gradient, key=lambda x: x[0]):
                if threshold > 50:  # 高估档：分位 ≥ threshold 时减仓
                    if pct >= threshold:
                        if factor < multiplier:
                            multiplier = factor
                        triggered.append(f"pct≥{threshold}")
                elif pct <= threshold:
                    if factor > multiplier:
                        multiplier = factor
                    triggered.append(f"pct≤{threshold}")
                    break
        nupl = context.indicators.get("nupl")
        if nupl is not None and nupl < 0.25:
            f = Decimal(str(self.params.get("nupl_factor", "1.3")))
            if f > multiplier:
                multiplier = f
            triggered.append("nupl<0.25")
        if not triggered:
            return Decimal("1"), "VALUATION_BUY", []
        return multiplier, "VALUATION_BUY", triggered


class RiskAdjustedDCAStrategy(DCAStrategyBase):
    """风险平价策略（§6.2）：按 Risk Overall 反向缩放投入。"""

    name = "risk_adjusted_dca"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(params)
        table = self.params.get("multipliers")
        self.multipliers: dict[str, Decimal] = (
            {k: Decimal(str(v)) for k, v in table.items()} if table
            else DEFAULT_RISK_MULTIPLIERS
        )

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        level = context.risk_level
        if level is None:
            return Decimal("1"), "RISK_ADJUSTED", []
        mult = self.multipliers.get(level, Decimal("1"))
        return mult, "RISK_ADJUSTED", [f"risk={level}"]


class RuleBasedStrategy(DCAStrategyBase):
    """自定义规则策略（§6.1）：由 StrategyConfig 规则集驱动，复用 RuleEngine。

    params 应含 ``rules``（list[Rule] 或 JSON 规格列表）。规则求值上下文由
    MarketContext 转换为 RuleContext。
    """

    name = "rule_based"

    def __init__(
        self,
        params: dict[str, Any] | None = None,
        rule_engine: RuleEngine | None = None,
    ):
        super().__init__(params)
        self._engine = rule_engine or RuleEngine()
        self.rules: list[Rule] = self._resolve_rules(self.params.get("rules", []))

    @staticmethod
    def _resolve_rules(raw: Any) -> list[Rule]:
        """接受 Rule 对象列表或 JSON 规格列表。"""
        if not raw:
            return []
        if isinstance(raw[0], Rule):
            return list(raw)
        from app.portfolio.rule_engine import build_rules

        return build_rules(raw)

    def compute_multiplier(self, context: MarketContext) -> tuple[Decimal, str, list[str]]:
        if not self.rules:
            return Decimal("1"), "CUSTOM", []
        ctx = self._to_rule_context(context)
        action = self._engine.evaluate(self.rules, ctx)
        if action.skip:
            return Decimal("0"), "CUSTOM_SKIP", action.triggered_rules
        if action.fixed_amount is not None:
            # 固定金额通过 base_amount 覆盖表达：乘数 = fixed / base
            base = context.base_amount
            if base and base > 0:
                return (action.fixed_amount / base), "CUSTOM_FIXED", action.triggered_rules
            return Decimal("1"), "CUSTOM_FIXED", action.triggered_rules
        return action.amount_multiplier, "CUSTOM", action.triggered_rules

    @staticmethod
    def _to_rule_context(context: MarketContext) -> RuleContext:
        """MarketContext -> RuleContext（数据缺失字段进 stale_fields）。"""
        stale: set[str] = set()
        rc = RuleContext(
            price=context.price,
            ath=context.ath,
            distance_from_ath=context.distance_from_ath,
            recent_high=context.recent_high,
            drawdown_from_recent=context.drawdown_from_recent,
            indicators=dict(context.indicators),
            indicator_percentiles=dict(context.indicator_percentiles),
            risk_level=context.risk_level,
            valuation_state=context.valuation_level,
            cycle_stage=context.cycle_stage,
            regime_label=context.regime,
            day_of_week=context.day_of_week,
            day_of_month=context.day_of_month,
            date_iso=context.as_of.isoformat(),
            stale_fields=stale,
        )
        if context.distance_from_ath is None:
            stale.add("distance_from_ath")
        if context.drawdown_from_recent is None:
            stale.add("drawdown_from_recent")
        if context.risk_level is None:
            stale.add("risk_level")
        if context.cycle_stage is None:
            stale.add("cycle_stage")
        return rc


# ---------------------------------------------------------------------------
# 策略注册表
# ---------------------------------------------------------------------------

STRATEGY_REGISTRY: dict[str, type[Strategy]] = {
    FixedDCAStrategy.name: FixedDCAStrategy,
    DipBuyStrategy.name: DipBuyStrategy,
    DrawdownBuyStrategy.name: DrawdownBuyStrategy,
    ValuationDCAStrategy.name: ValuationDCAStrategy,
    RiskAdjustedDCAStrategy.name: RiskAdjustedDCAStrategy,
    RuleBasedStrategy.name: RuleBasedStrategy,
    # 别名（与 portfolio 策略命名对齐）
    "fixed": FixedDCAStrategy,
    "dip_buy": DipBuyStrategy,
    "drawdown_buy": DrawdownBuyStrategy,
    "valuation_dca": ValuationDCAStrategy,
    "risk_adjusted": RiskAdjustedDCAStrategy,
    "custom": RuleBasedStrategy,
}


def create_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    """按名称创建策略实例。

    Args:
        name: 策略名（见 STRATEGY_REGISTRY）。
        params: 策略参数。

    Raises:
        ValueError: 未知策略名。
    """
    key = (name or "").lower()
    cls = STRATEGY_REGISTRY.get(key)
    if cls is None:
        raise ValueError(
            f"未知策略: {name!r}（可用：{sorted(set(STRATEGY_REGISTRY))}）"
        )
    return cls(params)


__all__ = [
    "MarketContext",
    "Strategy",
    "DCAStrategyBase",
    "FixedDCAStrategy",
    "DipBuyStrategy",
    "DrawdownBuyStrategy",
    "ValuationDCAStrategy",
    "RiskAdjustedDCAStrategy",
    "RuleBasedStrategy",
    "STRATEGY_REGISTRY",
    "create_strategy",
]
