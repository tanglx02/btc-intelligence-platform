"""定投模拟引擎（对应 15-portfolio-architecture.md §3）。

职责：按计划的 StrategyConfig 在每个投入日计算「本期应投金额」，模拟成交，
产出资产曲线与绩效指标。**只模拟、不下单**（§0 总体原则）。

内置策略模式（§3.2，全部可配置）：
1. FIXED — 固定金额定投
2. DIP_BUY — 下跌加仓（距 ATH 回撤梯度倍数）
3. DRAWDOWN_BUY — 回撤加仓（距近 N 日高点回撤梯度）
4. VALUATION_BUY — 估值加仓（MVRV/NUPL 低估加仓）
5. RISK_ADJUSTED — 风险调整（风险评分高时减少投入）
6. CUSTOM — 用户自定义规则（RuleEngine）

设计约束：
- 防未来数据泄漏为最高优先级：每个投入日只用截至当日（含）的价格与指标；
- ATH / 近 N 日高点用「截至当日的滚动最大值」，不用全样本；
- 全部金额用 Decimal；Polars 用于价格数据处理；
- 相同输入必产生相同输出（无随机、无 IO）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import polars as pl

from app.backtest import metrics as bt_metrics
from app.portfolio.rule_engine import (
    Rule,
    RuleAction,
    RuleContext,
    RuleEngine,
)

# 频率 -> 天数间隔（BIWEEKLY/MONTHLY 用日历推算，DAILY/WEEKLY 用固定间隔）
DAYS_PER_YEAR = 365
_Q_BTC = Decimal("0.00000001")
_Q_AMT = Decimal("0.01")

# ATH 回撤梯度默认表（§3.2 dip_ath）：回撤区间 -> 倍数
DEFAULT_DIP_ATH_GRADIENT: list[tuple[Decimal, Decimal]] = [
    # (回撤阈值下限，即 distance_from_ath ≤ 此值时触发, 倍数)
    (Decimal("-0.40"), Decimal("2.5")),
    (Decimal("-0.30"), Decimal("2.0")),
    (Decimal("-0.20"), Decimal("1.5")),
    (Decimal("-0.10"), Decimal("1.2")),
]
# 风险调整默认倍数表（§3.2 risk_adjust）
DEFAULT_RISK_MULTIPLIERS: dict[str, Decimal] = {
    "VERY_LOW": Decimal("1.3"), "LOW": Decimal("1.2"), "MODERATE": Decimal("1.0"),
    "HIGH": Decimal("0.5"), "VERY_HIGH": Decimal("0.3"), "EXTREME": Decimal("0.2"),
}
# 估值加仓默认梯度（MVRV 分位 -> 倍数）
DEFAULT_VALUATION_GRADIENT: list[tuple[float, Decimal]] = [
    (15.0, Decimal("2.0")),
    (30.0, Decimal("1.5")),
]


@dataclass
class DCAConfig:
    """定投模拟配置。"""

    initial_capital: Decimal = Decimal("0")      # 初始资金（首日一次性投入）
    periodic_amount: Decimal = Decimal("0")      # 每期基础投入
    frequency: str = "MONTHLY"                   # DAILY/WEEKLY/BIWEEKLY/MONTHLY
    start_date: date = field(default_factory=date.today)
    end_date: date | None = None
    # FIXED/DIP_BUY/DRAWDOWN_BUY/VALUATION_BUY/RISK_ADJUSTED/CUSTOM
    strategy: str = "FIXED"
    rules: list[Rule] | None = None           # CUSTOM 策略规则
    fee_rate: Decimal = Decimal("0.001")         # 手续费率
    currency: str = "CNY"                        # 计价货币
    dca_day: int = 1                             # 每月第 N 日 / 每周第 N 天
    max_single_buy: Decimal | None = None     # 单次投入上限（约束终审）
    cash_reserve: Decimal = Decimal("0")         # 现金储备下限
    drawdown_window: int = 90                    # DRAWDOWN_BUY 的近期高点窗口（天）
    # 策略参数覆盖
    dip_gradient: list[tuple[Decimal, Decimal]] | None = None
    risk_multipliers: dict[str, Decimal] | None = None
    valuation_gradient: list[tuple[float, Decimal]] | None = None

    def __post_init__(self) -> None:
        self.frequency = self.frequency.upper()
        self.strategy = self.strategy.upper()
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date 不得早于 start_date")


@dataclass
class SimTransaction:
    """模拟成交记录。"""

    tx_date: date
    side: str                    # BUY / SELL
    price: Decimal               # 成交价
    amount: Decimal              # 投入金额（含费前）
    fee: Decimal                 # 手续费
    quantity_btc: Decimal        # 成交 BTC 数量
    cumulative_invested: Decimal
    cumulative_btc: Decimal
    avg_cost_after: Decimal
    trigger_reason: str          # SCHEDULED_DCA / DIP_BUY / ...
    multiplier: Decimal = Decimal("1")
    triggered_rules: list[str] = field(default_factory=list)


@dataclass
class SimulationResult:
    """定投模拟结果（§3 + 14 号文档 §4.1 指标）。"""

    total_invested: Decimal
    final_btc: Decimal
    final_value: Decimal
    avg_cost: Decimal
    total_return: Decimal | None           # 总收益率（小数口径）
    annualized_return: Decimal | None      # 资金加权 IRR
    max_drawdown: Decimal
    sharpe_ratio: float | None
    transactions: list[SimTransaction]
    equity_curve: pl.DataFrame                # date, invested, btc_amount, value, avg_cost, price
    total_fees: Decimal = Decimal("0")
    transaction_count: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)


class DCASimulator:
    """定投模拟引擎。

    Usage::

        sim = DCASimulator()
        result = await sim.simulate(config, price_data)
        print(result.final_value, result.annualized_return)
    """

    def __init__(self, rule_engine: RuleEngine | None = None):
        self._engine = rule_engine or RuleEngine()

    async def simulate(
        self,
        config: DCAConfig,
        price_data: pl.DataFrame,
        indicators: dict[str, pl.DataFrame] | None = None,
        engine_states: pl.DataFrame | None = None,
    ) -> SimulationResult:
        """运行定投模拟。

        Args:
            config: 定投配置。
            price_data: 价格序列 DataFrame，须含 ``date``（Date/Datetime）与
                ``close``（价格）列，按日期升序。可选 ``high`` 列。
            indicators: 指标序列 {"mvrv": df(date,value,percentile?), ...}，
                用于 VALUATION_BUY / CUSTOM。缺省时相关条件求值为 UNKNOWN。
            engine_states: 引擎状态 df(date, risk_level?, cycle_stage?, ...)，
                用于 RISK_ADJUSTED / CUSTOM。

        Returns:
            SimulationResult（含逐笔模拟成交与资产曲线）。

        防泄漏保证：投入日 D 的市场上下文仅使用 date ≤ D 的价格/指标数据；
        ATH 与近期高点用截至 D 的滚动最大值。
        """
        df = self._normalize_price(price_data)
        dates = df["date"].to_list()
        prices = [Decimal(str(p)) for p in df["close"].to_list()]
        if not dates:
            return self._empty_result(config)

        end = config.end_date or dates[-1]
        invest_dates = self._investment_dates(config, dates[0], min(end, dates[-1]))

        ind_lookup = self._build_indicator_lookup(indicators)
        state_lookup = self._build_state_lookup(engine_states)

        cash = Decimal(config.initial_capital) if config.initial_capital else Decimal("0")
        # 若有初始资金但 periodic 独立，现金池用于约束校验；此处把初始资金视为可投现金
        available_cash = cash + Decimal("0")
        btc_balance = Decimal("0")
        cum_invested = Decimal("0")
        cum_buy_amount = Decimal("0")
        cum_buy_btc = Decimal("0")
        avg_cost = Decimal("0")
        total_fees = Decimal("0")
        transactions: list[SimTransaction] = []

        # 首日一次性投入初始资金
        first_invested = False

        # 资产曲线（逐日）
        curve_dates: list[date] = []
        curve_invested: list[float] = []
        curve_btc: list[float] = []
        curve_value: list[float] = []
        curve_avg_cost: list[float] = []
        curve_price: list[float] = []

        # 逐日遍历（防泄漏：每日只见到当日及之前的数据）
        for i, d in enumerate(dates):
            if d > end:
                break
            price = prices[i]

            # 首日投入初始资金
            if not first_invested and config.initial_capital > 0 and d >= config.start_date:
                qty, fee = self._calc_buy(config.initial_capital, price, config.fee_rate)
                btc_balance += qty
                cum_invested += config.initial_capital
                cum_buy_amount += config.initial_capital
                cum_buy_btc += qty
                avg_cost = self._avg_cost(cum_buy_amount, cum_buy_btc)
                total_fees += fee
                transactions.append(SimTransaction(
                    tx_date=d, side="BUY", price=price, amount=config.initial_capital,
                    fee=fee, quantity_btc=qty, cumulative_invested=cum_invested,
                    cumulative_btc=btc_balance, avg_cost_after=avg_cost,
                    trigger_reason="INITIAL_CAPITAL", multiplier=Decimal("1"),
                ))
                first_invested = True

            # 投入日：计算本期应投金额
            if d in invest_dates and price > 0:
                ctx = self._build_context(
                    d, i, dates, prices, df, config, ind_lookup, state_lookup,
                    cum_invested, available_cash,
                )
                base = Decimal(config.periodic_amount)
                amount, multiplier, reason, triggered = self._resolve_amount(
                    config, ctx, base
                )
                amount = self._apply_constraints(amount, config, available_cash)
                if amount > 0:
                    qty, fee = self._calc_buy(amount, price, config.fee_rate)
                    btc_balance += qty
                    cum_invested += amount
                    cum_buy_amount += amount
                    cum_buy_btc += qty
                    avg_cost = self._avg_cost(cum_buy_amount, cum_buy_btc)
                    total_fees += fee
                    available_cash -= (amount)
                    transactions.append(SimTransaction(
                        tx_date=d, side="BUY", price=price, amount=amount, fee=fee,
                        quantity_btc=qty, cumulative_invested=cum_invested,
                        cumulative_btc=btc_balance, avg_cost_after=avg_cost,
                        trigger_reason=reason, multiplier=multiplier,
                        triggered_rules=triggered,
                    ))

            # 记录当日资产曲线
            market_value = btc_balance * price
            curve_dates.append(d)
            curve_invested.append(float(cum_invested))
            curve_btc.append(float(btc_balance))
            curve_value.append(float(market_value))
            curve_avg_cost.append(float(avg_cost))
            curve_price.append(float(price))

        equity_curve = pl.DataFrame({
            "date": curve_dates,
            "invested": curve_invested,
            "btc_amount": curve_btc,
            "value": curve_value,
            "avg_cost": curve_avg_cost,
            "price": curve_price,
        })

        return self._build_result(
            config, transactions, equity_curve, btc_balance, cum_invested,
            avg_cost, total_fees,
        )

    # ---- 内部：价格数据处理 ----

    @staticmethod
    def _normalize_price(price_data: pl.DataFrame) -> pl.DataFrame:
        """规范化价格 DataFrame：统一 date 列（Date 类型）、升序、去重。"""
        df = price_data
        if "date" not in df.columns:
            if "time" in df.columns:
                df = df.rename({"time": "date"})
            elif "timestamp" in df.columns:
                df = df.rename({"timestamp": "date"})
            else:
                raise ValueError("price_data 须含 date/time/timestamp 列")
        if isinstance(df["date"].dtype, pl.Datetime):
            df = df.with_columns(pl.col("date").dt.date())
        if "close" not in df.columns:
            raise ValueError("price_data 须含 close 列")
        df = df.select(["date", "close"] + (["high"] if "high" in df.columns else []))
        df = df.sort("date").unique(subset=["date"], keep="last")
        return df

    @staticmethod
    def _investment_dates(config: DCAConfig, start: date, end: date) -> set[date]:
        """按频率生成投入日集合。

        DAILY：每个交易日；WEEKLY：每周第 dca_day 天（ISO 1-7）；
        BIWEEKLY：每两周一次（自 start 起每 14 天）；MONTHLY：每月第 dca_day 日
        （超出当月天数则取月末）。遇到无数据日由调用方在 dates 中自然跳过。
        """
        result: set[date] = set()
        freq = config.frequency
        if freq == "DAILY":
            cur = max(start, config.start_date)
            while cur <= end:
                result.add(cur)
                cur += timedelta(days=1)
            return result
        if freq == "WEEKLY":
            cur = start
            target_weekday = min(max(config.dca_day, 1), 7)
            while cur <= end:
                if cur.isoweekday() == target_weekday and cur >= config.start_date:
                    result.add(cur)
                cur += timedelta(days=1)
            return result
        if freq == "BIWEEKLY":
            cur = max(start, config.start_date)
            while cur <= end:
                result.add(cur)
                cur += timedelta(days=14)
            return result
        # MONTHLY（默认）
        dom = min(max(config.dca_day, 1), 28)
        y, m = start.year, start.month
        while True:
            try:
                d = date(y, m, dom)
            except ValueError:
                d = date(y, m, 28)
            if d > end:
                break
            if d >= max(start, config.start_date):
                result.add(d)
            m += 1
            if m > 12:
                m = 1
                y += 1
        return result

    @staticmethod
    def _build_indicator_lookup(
        indicators: dict[str, pl.DataFrame] | None,
    ) -> dict[str, dict[date, tuple[float | None, float | None]]]:
        """指标 df -> {name: {date: (value, percentile)}}。"""
        lookup: dict[str, dict[date, tuple[float | None, float | None]]] = {}
        if not indicators:
            return lookup
        for name, df in indicators.items():
            d = df
            if "date" not in d.columns:
                if "time" in d.columns:
                    d = d.rename({"time": "date"})
                else:
                    continue
            if isinstance(d["date"].dtype, pl.Datetime):
                d = d.with_columns(pl.col("date").dt.date())
            has_pct = "percentile" in d.columns
            per: dict[date, tuple[float | None, float | None]] = {}
            for row in d.iter_rows(named=True):
                val = row.get("value")
                pct = row.get("percentile") if has_pct else None
                per[row["date"]] = (
                    float(val) if val is not None else None,
                    float(pct) if pct is not None else None,
                )
            lookup[name.lower()] = per
        return lookup

    @staticmethod
    def _build_state_lookup(
        engine_states: pl.DataFrame | None,
    ) -> dict[date, dict[str, Any]]:
        """引擎状态 df -> {date: {risk_level, cycle_stage, ...}}。"""
        lookup: dict[date, dict[str, Any]] = {}
        if engine_states is None or engine_states.height == 0:
            return lookup
        d = engine_states
        if "date" not in d.columns:
            if "time" in d.columns:
                d = d.rename({"time": "date"})
            else:
                return lookup
        if isinstance(d["date"].dtype, pl.Datetime):
            d = d.with_columns(pl.col("date").dt.date())
        for row in d.iter_rows(named=True):
            lookup[row["date"]] = {k: v for k, v in row.items() if k != "date"}
        return lookup

    # ---- 内部：市场上下文（防泄漏核心）----

    def _build_context(
        self,
        d: date,
        idx: int,
        dates: list[date],
        prices: list[Decimal],
        df: pl.DataFrame,
        config: DCAConfig,
        ind_lookup: dict[str, dict[date, tuple[float | None, float | None]]],
        state_lookup: dict[date, dict[str, Any]],
        cum_invested: Decimal,
        cash_balance: Decimal,
    ) -> RuleContext:
        """构造投入日 d 的市场上下文，严格只用 date ≤ d 的数据。"""
        price = prices[idx]
        # ATH：截至当日（含）的历史最高价
        ath = max(prices[: idx + 1])
        distance_from_ath = (price - ath) / ath if ath > 0 else None
        # 近 N 日高点：截至当日的滚动窗口
        window = config.drawdown_window
        lo = max(0, idx - window + 1)
        recent_high = max(prices[lo: idx + 1])
        drawdown_recent = (price - recent_high) / recent_high if recent_high > 0 else None

        stale: set[str] = set()
        ctx = RuleContext(
            price=price,
            ath=ath,
            distance_from_ath=(
                distance_from_ath.quantize(Decimal("0.000001"))
                if distance_from_ath is not None else None
            ),
            recent_high=recent_high,
            drawdown_from_recent=(
                drawdown_recent.quantize(Decimal("0.000001"))
                if drawdown_recent is not None else None
            ),
            day_of_week=d.isoweekday(),
            day_of_month=d.day,
            date_iso=d.isoformat(),
            total_invested=cum_invested,
            cash_balance=cash_balance,
            stale_fields=stale,
        )

        # 指标（PIT：只取 date ≤ d 的最新值）
        for name, per in ind_lookup.items():
            val, pct = self._pit_lookup(per, d)
            ctx.indicators[name] = val
            ctx.indicator_percentiles[name] = pct
            if val is None:
                stale.add(f"indicators.{name}")
            if pct is None:
                stale.add(f"percentiles.{name}")
        # 便捷字段
        ctx.mvrv = ctx.indicators.get("mvrv")
        ctx.nupl = ctx.indicators.get("nupl")
        ctx.rsi = ctx.indicators.get("rsi")

        # 引擎状态（PIT）
        state = self._pit_state(state_lookup, d)
        if state:
            ctx.risk_level = state.get("risk_level")
            ctx.cycle_stage = state.get("cycle_stage")
            ctx.valuation_state = state.get("valuation_state")
            ctx.regime_label = state.get("regime_label")
        if ctx.risk_level is None:
            stale.add("risk_level")
        if ctx.cycle_stage is None:
            stale.add("cycle_stage")
        return ctx

    @staticmethod
    def _pit_lookup(
        per: dict[date, tuple[float | None, float | None]], d: date
    ) -> tuple[float | None, float | None]:
        """Point-in-Time 查找：取 date ≤ d 的最新值。"""
        if d in per:
            return per[d]
        candidates = [k for k in per if k <= d]
        if not candidates:
            return (None, None)
        return per[max(candidates)]

    @staticmethod
    def _pit_state(state_lookup: dict[date, dict[str, Any]], d: date) -> dict[str, Any] | None:
        """Point-in-Time 引擎状态查找。"""
        if d in state_lookup:
            return state_lookup[d]
        candidates = [k for k in state_lookup if k <= d]
        if not candidates:
            return None
        return state_lookup[max(candidates)]

    # ---- 内部：策略金额解析 ----

    def _resolve_amount(
        self, config: DCAConfig, ctx: RuleContext, base: Decimal
    ) -> tuple[Decimal, Decimal, str, list[str]]:
        """按策略计算本期应投金额，返回 (amount, multiplier, reason, triggered_rules)。"""
        strategy = config.strategy
        if strategy == "FIXED":
            return base, Decimal("1"), "SCHEDULED_DCA", []
        if strategy == "DIP_BUY":
            return self._dip_buy(config, ctx, base)
        if strategy == "DRAWDOWN_BUY":
            return self._drawdown_buy(config, ctx, base)
        if strategy == "VALUATION_BUY":
            return self._valuation_buy(config, ctx, base)
        if strategy == "RISK_ADJUSTED":
            return self._risk_adjusted(config, ctx, base)
        if strategy == "CUSTOM":
            return self._custom(config, ctx, base)
        # 未知策略：退化为固定定投
        return base, Decimal("1"), "SCHEDULED_DCA", []

    def _dip_buy(self, config: DCAConfig, ctx: RuleContext, base: Decimal
                 ) -> tuple[Decimal, Decimal, str, list[str]]:
        """下跌加仓：距 ATH 回撤梯度倍数。数据不可用时退化为基础定投。"""
        gradient = config.dip_gradient or DEFAULT_DIP_ATH_GRADIENT
        dist = ctx.distance_from_ath
        if dist is None:
            return base, Decimal("1"), "DIP_BUY", []
        multiplier = Decimal("1")
        for threshold, factor in gradient:  # 已按阈值降序（更深回撤在前）
            if dist <= threshold:
                multiplier = factor
                break
        return (base * multiplier), multiplier, "DIP_BUY", ["dip_ath"]

    def _drawdown_buy(self, config: DCAConfig, ctx: RuleContext, base: Decimal
                      ) -> tuple[Decimal, Decimal, str, list[str]]:
        """回撤加仓：距近 N 日高点回撤梯度。"""
        gradient = config.dip_gradient or DEFAULT_DIP_ATH_GRADIENT
        dd = ctx.drawdown_from_recent
        if dd is None:
            return base, Decimal("1"), "DRAWDOWN_BUY", []
        multiplier = Decimal("1")
        for threshold, factor in gradient:
            if dd <= threshold:
                multiplier = factor
                break
        return (base * multiplier), multiplier, "DRAWDOWN_BUY", ["dip_recent"]

    def _valuation_buy(self, config: DCAConfig, ctx: RuleContext, base: Decimal
                       ) -> tuple[Decimal, Decimal, str, list[str]]:
        """估值加仓：MVRV/NUPL 分位低估时加仓（取最大倍数）。"""
        gradient = config.valuation_gradient or DEFAULT_VALUATION_GRADIENT
        triggered: list[str] = []
        multiplier = Decimal("1")
        mvrv_pct = ctx.indicator_percentiles.get("mvrv")
        if mvrv_pct is not None:
            for pct_threshold, factor in gradient:
                if mvrv_pct <= pct_threshold:
                    if factor > multiplier:
                        multiplier = factor
                    triggered.append(f"mvrv_pct≤{pct_threshold}")
                    break
        nupl = ctx.nupl
        if nupl is not None and nupl < 0.25:
            f = Decimal("1.3")
            if f > multiplier:
                multiplier = f
            triggered.append("nupl<0.25")
        if not triggered:
            return base, Decimal("1"), "VALUATION_BUY", []
        return (base * multiplier), multiplier, "VALUATION_BUY", triggered

    def _risk_adjusted(self, config: DCAConfig, ctx: RuleContext, base: Decimal
                       ) -> tuple[Decimal, Decimal, str, list[str]]:
        """风险调整：按 Risk Overall 反向缩放投入。"""
        table = config.risk_multipliers or DEFAULT_RISK_MULTIPLIERS
        level = ctx.risk_level
        if level is None:
            return base, Decimal("1"), "RISK_ADJUSTED", []
        multiplier = table.get(level, Decimal("1"))
        return (base * multiplier), multiplier, "RISK_ADJUSTED", [f"risk={level}"]

    def _custom(self, config: DCAConfig, ctx: RuleContext, base: Decimal
                ) -> tuple[Decimal, Decimal, str, list[str]]:
        """自定义规则：交由 RuleEngine 求值。"""
        rules = config.rules or []
        if not rules:
            return base, Decimal("1"), "CUSTOM", []
        action: RuleAction = self._engine.evaluate(rules, ctx)
        if action.skip:
            return Decimal("0"), Decimal("0"), "CUSTOM_SKIP", action.triggered_rules
        if action.fixed_amount is not None:
            amount = action.fixed_amount + action.extra_buy_amount
            return amount, Decimal("1"), "CUSTOM_FIXED", action.triggered_rules
        amount = base * action.amount_multiplier + action.extra_buy_amount
        return amount, action.amount_multiplier, "CUSTOM", action.triggered_rules

    # ---- 内部：约束与成交计算 ----

    @staticmethod
    def _apply_constraints(amount: Decimal, config: DCAConfig, available_cash: Decimal) -> Decimal:
        """约束终审（§4.4）：max_single_buy 封顶、cash_reserve 下限。

        注意：模拟中 available_cash 为软约束参考；纯定投（无现金池概念）时
        不因现金不足而中断纪律，仅受 max_single_buy 封顶。
        """
        if amount <= 0:
            return Decimal("0")
        if config.max_single_buy is not None and amount > config.max_single_buy:
            amount = Decimal(config.max_single_buy)
        return amount.quantize(_Q_AMT, rounding=ROUND_HALF_UP)

    @staticmethod
    def _calc_buy(amount: Decimal, price: Decimal, fee_rate: Decimal) -> tuple[Decimal, Decimal]:
        """计算买入的 (BTC 数量, 手续费)。手续费从投入金额中扣除。"""
        if price <= 0:
            return Decimal("0"), Decimal("0")
        fee = (amount * fee_rate).quantize(_Q_AMT, rounding=ROUND_HALF_UP)
        net = amount - fee
        qty = (net / price).quantize(_Q_BTC, rounding=ROUND_HALF_UP)
        return qty, fee

    @staticmethod
    def _avg_cost(cum_buy_amount: Decimal, cum_buy_btc: Decimal) -> Decimal:
        """移动加权平均成本。"""
        if cum_buy_btc <= 0:
            return Decimal("0")
        return (cum_buy_amount / cum_buy_btc).quantize(_Q_BTC, rounding=ROUND_HALF_UP)

    # ---- 内部：结果构造 ----

    def _empty_result(self, config: DCAConfig) -> SimulationResult:
        empty_curve = pl.DataFrame({
            "date": [], "invested": [], "btc_amount": [],
            "value": [], "avg_cost": [], "price": [],
        })
        return SimulationResult(
            total_invested=Decimal("0"), final_btc=Decimal("0"),
            final_value=Decimal("0"), avg_cost=Decimal("0"),
            total_return=None, annualized_return=None,
            max_drawdown=Decimal("0"), sharpe_ratio=None,
            transactions=[], equity_curve=empty_curve,
        )

    def _build_result(
        self,
        config: DCAConfig,
        transactions: list[SimTransaction],
        equity_curve: pl.DataFrame,
        final_btc: Decimal,
        cum_invested: Decimal,
        avg_cost: Decimal,
        total_fees: Decimal,
    ) -> SimulationResult:
        """由资产曲线与交易构造 SimulationResult（复用回测指标模块）。"""
        final_value = (
            Decimal(str(equity_curve["value"][-1])) if equity_curve.height else Decimal("0")
        )
        final_value = final_value.quantize(_Q_AMT, rounding=ROUND_HALF_UP)
        total_return = (
            (final_value / cum_invested - 1).quantize(Decimal("0.000001"))
            if cum_invested > 0 else None
        )
        mdd = Decimal(str(bt_metrics.max_drawdown(equity_curve["value"].to_list())))

        # 全部绩效指标复用回测 metrics（口径与回测一致）
        metrics = bt_metrics.calculate_all(
            equity_curve, trades=[], risk_free_rate=0.02
        )
        annualized = metrics.get("annualized_return")
        sharpe = metrics.get("sharpe_ratio")

        return SimulationResult(
            total_invested=cum_invested.quantize(_Q_AMT, rounding=ROUND_HALF_UP),
            final_btc=final_btc,
            final_value=final_value,
            avg_cost=avg_cost,
            total_return=total_return,
            annualized_return=(
                Decimal(str(annualized)).quantize(Decimal("0.000001"))
                if annualized is not None else None
            ),
            max_drawdown=mdd.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            sharpe_ratio=sharpe,
            transactions=transactions,
            equity_curve=equity_curve,
            total_fees=total_fees.quantize(_Q_AMT, rounding=ROUND_HALF_UP),
            transaction_count=len(transactions),
            metrics=metrics,
        )
