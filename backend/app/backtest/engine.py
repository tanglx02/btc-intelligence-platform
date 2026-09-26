"""事件驱动回测引擎（对应 14-backtest-architecture.md §1.2）。

模拟真实交易流程，是**结果权威来源**（写入 backtest_results 的正式记录）。

事件流：BarEvent（K线收盘）→ IndicatorEvent（指标/引擎输出就绪）→
SignalEvent（策略产生意向）→ OrderEvent（组合校验后生成订单）→
FillEvent（成交模拟回报）。

关键设计：
- **DataHandler 唯一数据入口**，强制走 PointInTimeDataFeed（§2.1），按当前模拟
  时间逐步释放数据，架构上杜绝越界访问；
- **成交假设**（§1.2，写入报告）：信号在 Bar 收盘产生，默认按**次 Bar 开盘价 +
  滑点**成交（fill_mode=NEXT_OPEN，更保守）；可配置同 Bar 收盘成交；禁止按当日
  最低价等乐观假设；
- **防泄漏**：引擎结束时调用 feed.validate_no_leakage()，任何前视访问导致回测失败；
- **可复现**：无随机、无 IO 副作用（DB 读取亦为确定性 PIT 查询），相同输入必产生
  相同输出；
- **资金模型**：periodic 定投为外部现金流（薪资），不消耗初始资金池；initial_capital
  首日一次性部署。该模型与 portfolio.dca_simulator 及向量化引擎一致（§1.1 CI 断言）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import polars as pl
from loguru import logger

from app.backtest import metrics as bt_metrics
from app.backtest.data_feed import PointInTimeDataFeed
from app.backtest.strategy import MarketContext, Strategy, create_strategy
from app.backtest.types import (
    Q_AMOUNT,
    Q_BTC,
    Q_FEE,
    BacktestConfig,
    BacktestResult,
    Bar,
    EngineType,
    EquityPoint,
    Fill,
    FillMode,
    Order,
    OrderType,
    PortfolioState,
    Side,
    Signal,
    Trade,
)

_Q_PRICE = Decimal("0.00000001")


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class ExecutionSimulator:
    """成交模拟器（§1.2）：滑点、手续费、全额成交（日级流动性充足假设）。"""

    def __init__(self, config: BacktestConfig):
        self._config = config

    def compute_fill_price(self, ref_price: Decimal, side: Side) -> Decimal:
        """成交价 = 参考价 × (1 ± slippage)。BUY 抬价、SELL 压价。"""
        slip = Decimal(self._config.slippage or 0)
        if side == Side.BUY:
            return (ref_price * (Decimal("1") + slip)).quantize(_Q_PRICE, rounding=ROUND_HALF_UP)
        return (ref_price * (Decimal("1") - slip)).quantize(_Q_PRICE, rounding=ROUND_HALF_UP)

    def simulate(self, order: Order, ref_price: Decimal, ref_time: datetime) -> Fill | None:
        """模拟订单成交，返回 Fill（资金/数量不足返回 None）。"""
        side = order.side
        fill_price = self.compute_fill_price(ref_price, side)
        fee_rate = Decimal(self._config.fee_rate or 0)

        if order.amount is not None:
            gross = Decimal(order.amount)
            fee = (gross * fee_rate).quantize(Q_FEE, rounding=ROUND_HALF_UP)
            net = gross - fee
            if fill_price <= 0 or net <= 0:
                return None
            qty = (net / fill_price).quantize(Q_BTC, rounding=ROUND_HALF_UP)
            slippage_cost = (abs(fill_price - ref_price) * qty).quantize(
                Q_FEE, rounding=ROUND_HALF_UP
            )
            amount = (fill_price * qty).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
        elif order.quantity is not None:
            qty = Decimal(order.quantity)
            amount = (fill_price * qty).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
            fee = (amount * fee_rate).quantize(Q_FEE, rounding=ROUND_HALF_UP)
            slippage_cost = (abs(fill_price - ref_price) * qty).quantize(
                Q_FEE, rounding=ROUND_HALF_UP
            )
        else:
            return None

        return Fill(
            time=ref_time, side=side, price=fill_price, quantity=qty,
            amount=amount, fee=fee, slippage_cost=slippage_cost,
            order_id=order.id, reason=order.reason,
        )


class PortfolioManager:
    """组合逐笔记账（§1.2）：现金账户 + BTC 持仓 + 移动加权平均成本。"""

    def __init__(self, config: BacktestConfig):
        self._config = config
        self.state = PortfolioState(
            initial_capital=Decimal(config.initial_capital),
            cash=Decimal(config.initial_capital),
        )
        self._cum_buy_amount = Decimal("0")
        self._cum_buy_btc = Decimal("0")

    def apply_fill(self, fill: Fill, is_initial: bool = False) -> Trade:
        """应用成交回报，更新持仓与现金，返回 Trade 记录。"""
        st = self.state
        st.trade_count += 1
        st.total_fees += fill.fee
        st.total_slippage += fill.slippage_cost
        pnl: Decimal | None = None

        if fill.side == Side.BUY:
            gross = fill.amount + fill.fee  # 本次总投入（含费）
            if is_initial:
                st.cash -= gross  # 初始资金从现金池支出
            # periodic 为外部现金流，不消耗初始池（cash 不变）
            st.position.quantity = (st.position.quantity + fill.quantity).quantize(
                Q_BTC, rounding=ROUND_HALF_UP
            )
            self._cum_buy_amount += gross
            self._cum_buy_btc += fill.quantity
            st.total_invested = (st.total_invested + gross).quantize(
                Q_AMOUNT, rounding=ROUND_HALF_UP
            )
            st.position.avg_cost = self._avg_cost()
            st.position.total_cost = (
                st.position.quantity * st.position.avg_cost
            ).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
        else:  # SELL
            proceeds = fill.amount - fill.fee
            st.cash += proceeds
            sell_qty = min(fill.quantity, st.position.quantity)
            cost_of_sold = sell_qty * st.position.avg_cost
            pnl = (proceeds - cost_of_sold).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
            st.realized_pnl += pnl
            st.position.realized_pnl = st.realized_pnl
            st.position.quantity = (st.position.quantity - sell_qty).quantize(
                Q_BTC, rounding=ROUND_HALF_UP
            )
            if st.position.quantity <= 0:
                st.position.quantity = Decimal("0")
                self._cum_buy_amount = Decimal("0")
                self._cum_buy_btc = Decimal("0")
                st.position.avg_cost = Decimal("0")
            st.position.total_cost = (
                st.position.quantity * st.position.avg_cost
            ).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)

        return Trade(
            trade_number=st.trade_count, time=fill.time, side=fill.side,
            price=fill.price, quantity_btc=fill.quantity, amount=fill.amount,
            fee=fill.fee, slippage=fill.slippage_cost, trigger_reason=fill.reason,
            pnl=pnl, cumulative_btc=st.position.quantity,
            cumulative_invested=st.total_invested, avg_cost_after=st.position.avg_cost,
        )

    def _avg_cost(self) -> Decimal:
        if self._cum_buy_btc <= 0:
            return Decimal("0")
        return (self._cum_buy_amount / self._cum_buy_btc).quantize(
            Q_BTC, rounding=ROUND_HALF_UP
        )


@dataclass
class _PendingOrder:
    """待成交订单（NEXT_OPEN 模式跨 Bar 撮合）。"""

    order: Order
    signal_time: datetime
    is_initial: bool = False


class BacktestEngine:
    """事件驱动回测引擎。

    Usage::

        feed = PointInTimeDataFeed(current_time=start, prices_df=df)
        engine = BacktestEngine(feed=feed)
        result = await engine.run(config)
        print(result.metrics["annualized_return"])
    """

    def __init__(
        self,
        feed: PointInTimeDataFeed | None = None,
        strategy: Strategy | None = None,
        recent_high_window: int = 90,
    ):
        """初始化。

        Args:
            feed: PIT 数据供给器；None 时 run() 须显式传入 bars。
            strategy: 策略实例；None 时按 config.strategy_name 创建。
            recent_high_window: 近期高点滚动窗口（天），用于 drawdown_buy。
        """
        self._feed = feed
        self._strategy_override = strategy
        self._window = recent_high_window

    async def run(
        self,
        config: BacktestConfig,
        bars: list[Bar] | None = None,
    ) -> BacktestResult:
        """执行回测（§1.2 事件循环）。

        Args:
            config: 回测配置。
            bars: 预构造的 Bar 列表（可选）；None 时从 feed 加载。

        Returns:
            BacktestResult（含指标、资产曲线、逐笔交易、泄漏校验结果）。
        """
        started = datetime.now(UTC)
        self._last_config = config
        try:
            strategy = self._strategy_override or create_strategy(
                config.strategy_name, config.strategy_params
            )
            strategy.on_start(config)
            bar_list = bars if bars is not None else await self._load_bars(config)
            if not bar_list:
                return self._failed(config, started, "回测区间无可用 K 线数据")

            pm = PortfolioManager(config)
            executor = ExecutionSimulator(config)

            # 滚动 ATH 与近期高点（PIT：仅用已见 Bar）
            running_ath = Decimal("0")
            recent_closes: deque[Decimal] = deque(maxlen=self._window)

            equity_points: list[EquityPoint] = []
            trades: list[Trade] = []
            pending: _PendingOrder | None = None
            prev_value: Decimal | None = None
            peak_value = Decimal("0")

            for bar in bar_list:
                bar_time = _to_utc(bar.time)
                # 1. 推进模拟时钟 + 释放数据（DataHandler 唯一入口）
                if self._feed is not None:
                    self._feed.advance_time(bar_time)

                # 2. 次 Bar 开盘撮合上一根 Bar 的信号订单（NEXT_OPEN）
                if pending is not None and config.fill_mode == FillMode.NEXT_OPEN:
                    fill = executor.simulate(pending.order, bar.open, bar_time)
                    if fill is not None:
                        trades.append(pm.apply_fill(fill, pending.is_initial))
                    pending = None

                # 3. 更新滚动统计（含当前 Bar）
                running_ath = max(running_ath, bar.close)
                recent_closes.append(bar.close)

                # 4. 构造 PIT 市场上下文（IndicatorEvent）
                context = await self._build_context(bar, config, running_ath, recent_closes)

                # 5. 策略产生信号（SignalEvent）
                signal = strategy.on_bar(bar, pm.state, context)

                # 6. 信号 → 订单（组合校验，OrderEvent）
                if signal is not None:
                    order = self._signal_to_order(signal, bar, pm.state, config)
                    if order is not None:
                        is_initial = signal.reason == "INITIAL_CAPITAL"
                        if config.fill_mode == FillMode.SAME_CLOSE:
                            fill = executor.simulate(order, bar.close, bar_time)
                            if fill is not None:
                                trades.append(pm.apply_fill(fill, is_initial))
                        else:
                            pending = _PendingOrder(order, bar_time, is_initial)

                # 7. 记录每日净值快照
                value = pm.state.total_value(bar.close).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
                peak_value = max(peak_value, value)
                drawdown = (
                    (peak_value - value) / peak_value if peak_value > 0 else Decimal("0")
                )
                daily_return = (
                    (value / prev_value - 1) if prev_value and prev_value > 0 else None
                )
                cum_return = (
                    (value / pm.state.total_invested - 1)
                    if pm.state.total_invested > 0 else None
                )
                equity_points.append(EquityPoint(
                    time=bar_time, portfolio_value=value, cash_balance=pm.state.cash,
                    btc_holdings=pm.state.position.quantity, btc_price=bar.close,
                    avg_cost=pm.state.position.avg_cost,
                    unrealized_pnl=(value - pm.state.cash - pm.state.position.total_cost),
                    realized_pnl=pm.state.realized_pnl,
                    cumulative_invested=pm.state.total_invested,
                    drawdown=drawdown.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP),
                    daily_return=(
                        daily_return.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                        if daily_return is not None else None
                    ),
                    cumulative_return=(
                        cum_return.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                        if cum_return is not None else None
                    ),
                ))
                prev_value = value

            # 收盘前若仍有 pending（最后一根 Bar 的信号），按最后收盘价撮合
            if pending is not None:
                last_bar = bar_list[-1]
                fill = executor.simulate(pending.order, last_bar.close, _to_utc(last_bar.time))
                if fill is not None:
                    trades.append(pm.apply_fill(fill, pending.is_initial))

            # 8. 泄漏校验（§2.5 时间旅行断言）
            leakage_ok = True
            if self._feed is not None:
                try:
                    self._feed.validate_no_leakage()
                except Exception as exc:  # noqa: BLE001 - 泄漏即回测失败
                    logger.error("回测前视检测失败: {}", exc)
                    return self._failed(config, started, f"未来数据泄漏: {exc}")

            return self._build_result(config, started, pm, equity_points, trades, leakage_ok)
        except Exception as exc:  # noqa: BLE001 - 回测失败需捕获并落库
            logger.exception("回测执行异常: {}", exc)
            return self._failed(config, started, str(exc))

    # ---- 内部：数据加载与上下文 ----

    async def _load_bars(self, config: BacktestConfig) -> list[Bar]:
        """从 feed 加载区间 K 线并转为 Bar 列表。"""
        if self._feed is None:
            return []
        start = datetime.combine(config.start_date, time.min, tzinfo=UTC)
        end = datetime.combine(config.end_date, time.max, tzinfo=UTC)
        df = await self._feed.load_bars(config.symbol, start, end, config.interval)
        return self._df_to_bars(df, config.symbol)

    @staticmethod
    def _df_to_bars(df: pl.DataFrame, symbol: str) -> list[Bar]:
        """Polars DataFrame -> Bar 列表（按时间升序）。"""
        if df is None or df.height == 0:
            return []
        d = df.sort("_ts") if "_ts" in df.columns else df
        time_col = "_ts_dt" if "_ts_dt" in d.columns else (
            "time" if "time" in d.columns else "date"
        )
        bars: list[Bar] = []
        for row in d.iter_rows(named=True):
            t = row[time_col]
            if isinstance(t, date) and not isinstance(t, datetime):
                t = datetime.combine(t, time.min, tzinfo=UTC)
            close = Decimal(str(row["close"]))
            open_ = Decimal(str(row.get("open", row["close"])))
            high = Decimal(str(row.get("high", close)))
            low = Decimal(str(row.get("low", close)))
            vol = Decimal(str(row.get("volume", 0)))
            bars.append(Bar(
                time=_to_utc(t), open=open_, high=high, low=low, close=close,
                volume=vol, symbol=symbol,
            ))
        return bars

    async def _build_context(
        self,
        bar: Bar,
        config: BacktestConfig,
        running_ath: Decimal,
        recent_closes: deque[Decimal],
    ) -> MarketContext:
        """构造当前 Bar 的 PIT 市场上下文（异步查询指标/引擎状态）。"""
        bar_date = _to_utc(bar.time).date()
        price = bar.close
        dist_ath = (
            ((price - running_ath) / running_ath).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            if running_ath > 0 else None
        )
        recent_high = max(recent_closes) if recent_closes else None
        dd_recent = (
            ((price - recent_high) / recent_high).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
            if recent_high and recent_high > 0 else None
        )

        ctx = MarketContext(
            as_of=bar_date, price=price, ath=running_ath, distance_from_ath=dist_ath,
            recent_high=recent_high, drawdown_from_recent=dd_recent,
            base_amount=Decimal(config.periodic_investment or 0),
            day_of_week=bar_date.isoweekday(), day_of_month=bar_date.day,
        )

        # 通过 PIT feed 查询指标与引擎状态（严格 as_of 过滤）
        if self._feed is not None:
            state = await self._feed.get_engine_state(bar.time)
            ctx.risk_level = state.get("risk_level")
            ctx.cycle_stage = state.get("cycle_phase")
            ctx.valuation_level = state.get("valuation_level")
            ctx.regime = state.get("regime")
            for ind_name in self._required_indicators(config):
                val = await self._feed.get_indicator(ind_name, bar.time)
                pct = await self._feed.get_indicator_percentile(ind_name, bar.time)
                ctx.indicators[ind_name.split(".")[-1]] = val
                if pct is not None:
                    ctx.indicator_percentiles[ind_name.split(".")[-1]] = pct
                ctx.indicator_percentiles[ind_name] = pct
                ctx.indicators[ind_name] = val
        return ctx

    @staticmethod
    def _required_indicators(config: BacktestConfig) -> list[str]:
        """从策略参数推断需要的指标列表（估值/自定义策略）。"""
        params = config.strategy_params or {}
        names: set[str] = set()
        if params.get("indicator"):
            names.add(str(params["indicator"]))
        for rule in params.get("rules", []) or []:
            if isinstance(rule, dict):
                code = _extract_indicator_code(rule.get("if"))
                if code:
                    names.add(code)
        # 估值策略默认需要 MVRV
        if config.strategy_name.lower() in {"valuation_dca", "valuation"}:
            names.add("onchain.mvrv")
            names.add("onchain.nupl")
        return sorted(names)

    def _signal_to_order(
        self, signal: Signal, bar: Bar, portfolio: PortfolioState, config: BacktestConfig
    ) -> Order | None:
        """信号 → 订单（组合校验：金额/资金充足性）。"""
        if signal.direction == Side.BUY:
            amount = signal.target_amount
            if amount is None or amount <= 0:
                return None
            return Order(
                time=bar.time, side=Side.BUY, order_type=OrderType.MARKET,
                amount=amount, reason=signal.reason, signal_time=signal.time,
            )
        # SELL
        qty = signal.target_quantity
        if qty is None and signal.target_amount is not None and bar.close > 0:
            qty = (signal.target_amount / bar.close).quantize(Q_BTC, rounding=ROUND_HALF_UP)
        if qty is None or qty <= 0:
            return None
        qty = min(qty, portfolio.position.quantity)
        if qty <= 0:
            return None
        return Order(
            time=bar.time, side=Side.SELL, order_type=OrderType.MARKET,
            quantity=qty, reason=signal.reason, signal_time=signal.time,
        )

    # ---- 内部：结果构造 ----

    def _build_result(
        self,
        config: BacktestConfig,
        started: datetime,
        pm: PortfolioManager,
        equity_points: list[EquityPoint],
        trades: list[Trade],
        leakage_ok: bool,
    ) -> BacktestResult:
        """由记账状态与净值序列构造 BacktestResult（计算 §4 指标）。"""
        completed = datetime.now(UTC)
        curve = self._equity_to_df(equity_points)
        st = pm.state
        metrics = bt_metrics.calculate_all(
            curve, trades=trades, risk_free_rate=config.risk_free_rate,
            initial_capital=0.0,  # initial_capital 已计入 invested 曲线
        )
        final_value = (
            Decimal(str(metrics["final_value"])).quantize(Q_AMOUNT, rounding=ROUND_HALF_UP)
            if metrics.get("final_value") is not None else Decimal("0")
        )
        # 基准：BTC 一次性买入
        prices = [float(p.btc_price) for p in equity_points if p.btc_price is not None]
        dates = [_to_utc(p.time).date() for p in equity_points]
        bench = bt_metrics.lump_sum_benchmark(
            dates, prices, float(st.total_invested)
        )
        return BacktestResult(
            config=config, status="COMPLETED", started_at=started, completed_at=completed,
            duration_seconds=int((completed - started).total_seconds()),
            metrics=metrics, equity_curve=curve, equity_points=equity_points, trades=trades,
            initial_capital=Decimal(config.initial_capital),
            total_invested=st.total_invested, final_value=final_value,
            total_fees=st.total_fees.quantize(Q_AMOUNT, rounding=ROUND_HALF_UP),
            btc_accumulated=st.position.quantity, avg_cost_basis=st.position.avg_cost,
            engine=EngineType.EVENT_DRIVEN, data_mode=config.data_mode, approximate=False,
            leakage_validated=leakage_ok,
            benchmark_return=(
                Decimal(str(bench)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                if bench is not None else None
            ),
        )

    @staticmethod
    def _equity_to_df(points: list[EquityPoint]) -> pl.DataFrame:
        """净值序列 -> Polars DataFrame（列名对齐 metrics.calculate_all）。"""
        if not points:
            return pl.DataFrame({
                "date": [], "value": [], "invested": [], "cash": [],
                "btc_amount": [], "avg_cost": [], "price": [],
            })
        return pl.DataFrame({
            "date": [_to_utc(p.time).date() for p in points],
            "value": [float(p.portfolio_value) for p in points],
            "invested": [float(p.cumulative_invested or 0) for p in points],
            "cash": [float(p.cash_balance) for p in points],
            "btc_amount": [float(p.btc_holdings) for p in points],
            "avg_cost": [float(p.avg_cost or 0) for p in points],
            "price": [float(p.btc_price) if p.btc_price is not None else None for p in points],
        })

    @staticmethod
    def _failed(config: BacktestConfig, started: datetime, message: str) -> BacktestResult:
        """构造失败结果。"""
        completed = datetime.now(UTC)
        return BacktestResult(
            config=config, status="FAILED", error_message=message,
            started_at=started, completed_at=completed,
            duration_seconds=int((completed - started).total_seconds()),
            engine=EngineType.EVENT_DRIVEN, data_mode=config.data_mode,
        )

    # ---- 单 Bar / 单订单处理（对外暴露的细粒度接口，便于测试）----

    def _process_bar(
        self, bar: Bar, strategy: Strategy, portfolio: PortfolioManager,
        context: MarketContext, config: BacktestConfig, executor: ExecutionSimulator,
    ) -> Trade | None:
        """处理单个 K 线：策略 → 信号 → 订单 → 同 Bar 收盘成交（SAME_CLOSE）。

        供单元测试与 SAME_CLOSE 模式复用；主循环在 run() 中内联以支持 NEXT_OPEN。
        """
        signal = strategy.on_bar(bar, portfolio.state, context)
        if signal is None:
            return None
        order = self._signal_to_order(signal, bar, portfolio.state, config)
        if order is None:
            return None
        fill = executor.simulate(order, bar.close, _to_utc(bar.time))
        if fill is None:
            return None
        return portfolio.apply_fill(fill, signal.reason == "INITIAL_CAPITAL")

    def _execute_order(
        self, order: Order, bar: Bar, pm: PortfolioManager, price: Decimal | None = None
    ) -> Trade | None:
        """模拟订单执行（含手续费和滑点），返回成交记录。"""
        executor = ExecutionSimulator(
            self._last_config or BacktestConfig(strategy_name="fixed_dca")
        )
        ref = price if price is not None else bar.close
        fill = executor.simulate(order, ref, _to_utc(bar.time))
        if fill is None:
            return None
        return pm.apply_fill(fill, order.reason == "INITIAL_CAPITAL")

    _last_config: BacktestConfig | None = None


def _extract_indicator_code(cond: Any) -> str | None:
    """从 JSON 条件规格递归提取 indicator code（供预取指标）。"""
    if isinstance(cond, dict):
        if "code" in cond:
            return str(cond["code"])
        for key in ("all", "any"):
            if key in cond and isinstance(cond[key], list):
                for sub in cond[key]:
                    code = _extract_indicator_code(sub)
                    if code:
                        return code
        if "not" in cond:
            return _extract_indicator_code(cond["not"])
    return None
