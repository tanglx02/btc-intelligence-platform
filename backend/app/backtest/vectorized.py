"""向量化快速回测引擎（对应 14-backtest-architecture.md §1.3）。

将全部信号逻辑表达为 Polars 列式运算（条件矩阵 → 投入金额矩阵 → 持仓/净值
矩阵），单策略 2015~今 全区间回测目标 < 2 秒。用于参数扫描、网格搜索与快速
预筛；结果标注 ``engine=VECTORIZED, approximate=true``，**正式记录以事件驱动
引擎为准**（§1.4）。

与事件驱动引擎的一致性（§1.1 CI 断言，容差 < 0.1%）：
- **同一资金模型**：periodic 定投为外部现金流（薪资），不消耗初始资金池；
  initial_capital 首日一次性部署；
- **同一成交约定**：fill_mode=NEXT_OPEN 时信号于次 Bar 开盘成交、SAME_CLOSE
  时同 Bar 收盘成交，均按 ``ref_price × (1 ± slippage)`` 计算成交价，手续费
  ``fee = amount × fee_rate``，净额 ``net = amount − fee``，数量 ``qty = net /
  fill_price``；
- **同一投入日调度**：复用 :class:`~app.backtest.strategy.DCAStrategyBase`
  生成的投入日集合，保证与事件驱动逐 Bar 判定完全一致；
- **同一梯度表**：dip/drawdown/valuation/risk 的乘数梯度直接取自策略实例。

限制（approximate 语义）：
- valuation_dca / risk_adjusted 需要指标分位或引擎状态列；内存模式下从 feed
  预取，缺失时按 §4.5 降级为基础定投（乘数 1.0），与事件驱动在数据缺失时的
  降级一致；
- rule_based / custom 的任意规则组合不做列式展开，降级为基础定投并告警；
- cash_reserve 约束（现金储备下限）在向量化路径不建模（默认 0，且外部现金流
  模型下现金恒 ≈ 0）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import polars as pl
from loguru import logger

from app.backtest import metrics as bt_metrics
from app.backtest.data_feed import PointInTimeDataFeed
from app.backtest.strategy import Strategy, create_strategy
from app.backtest.types import (
    Q_AMOUNT,
    Q_BTC,
    BacktestConfig,
    BacktestResult,
    DataMode,
    EngineType,
    FillMode,
    Side,
    Trade,
)

_Q_PRICE = Decimal("0.00000001")


def _to_utc(dt: datetime) -> datetime:
    """确保 datetime 带 UTC 时区（naive 视为 UTC）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class VectorizedBacktest:
    """向量化快速回测（Polars 列式运算）。

    Usage::

        vt = VectorizedBacktest(feed=feed)
        result = await vt.run(config)          # approximate=True
        assert result.engine == EngineType.VECTORIZED
    """

    def __init__(
        self,
        feed: PointInTimeDataFeed | None = None,
        recent_high_window: int = 90,
    ):
        """初始化。

        Args:
            feed: PIT 数据供给器（内存模式下用于预取指标分位/引擎状态列）。
            recent_high_window: 近期高点滚动窗口（天），用于 drawdown_buy。
        """
        self._feed = feed
        self._window = recent_high_window

    async def run(
        self,
        config: BacktestConfig,
        prices_df: pl.DataFrame | None = None,
    ) -> BacktestResult:
        """执行向量化回测。

        Args:
            config: 回测配置（fill_mode / fee_rate / slippage 与事件驱动同义）。
            prices_df: 预加载价格序列；None 时从 feed 加载。

        Returns:
            BacktestResult（engine=VECTORIZED, approximate=True）。
        """
        started = datetime.now(UTC)
        try:
            px = await self._load_prices(config, prices_df)
            if px is None or px.height == 0:
                return self._failed(config, started, "回测区间无可用 K 线数据")

            strategy = create_strategy(config.strategy_name, config.strategy_params)
            strategy.on_start(config)

            n = px.height
            ts = px["ts"].to_list()                     # naive UTC datetime
            dates = [t.date() for t in ts]
            open_ = px["open"].to_list()
            close = px["close"].to_list()

            # ---- 1. 投入日调度（复用策略，保证与事件驱动一致）----
            invest_mask = [strategy.is_investment_day(d) for d in dates]

            # ---- 2. 乘数矩阵（列式）----
            mult, periodic_reason = self._multiplier_column(config, strategy, px, dates)

            # ---- 3. 信号金额矩阵 ----
            base = float(config.periodic_investment or 0)
            initial_cap = float(config.initial_capital or 0)
            initial_bar = 0 if initial_cap > 0 else -1
            max_single = float(config.max_single_buy) if config.max_single_buy else None

            signal_amt = [0.0] * n
            signal_reason: list[str] = [""] * n
            for i in range(n):
                if i == initial_bar:
                    signal_amt[i] = initial_cap
                    signal_reason[i] = "INITIAL_CAPITAL"
                elif invest_mask[i] and base > 0:
                    amt = base * mult[i]
                    signal_amt[i] = amt
                    signal_reason[i] = periodic_reason
                if max_single is not None and signal_amt[i] > max_single:
                    signal_amt[i] = max_single

            # ---- 4. 成交（滑点 + 手续费），按 fill_mode 决定执行时点 ----
            next_open = config.fill_mode == FillMode.NEXT_OPEN
            slip = float(config.slippage or 0)
            fee_rate = float(config.fee_rate or 0)

            exec_amt = [0.0] * n
            exec_reason: list[str] = [""] * n
            ref_price = [0.0] * n
            for i in range(n):
                if next_open:
                    # 信号于次 Bar 开盘成交：exec[i] 来自 signal[i-1]
                    if i >= 1:
                        exec_amt[i] = signal_amt[i - 1]
                        exec_reason[i] = signal_reason[i - 1]
                        ref_price[i] = open_[i]
                else:
                    exec_amt[i] = signal_amt[i]
                    exec_reason[i] = signal_reason[i]
                    ref_price[i] = close[i]

            fee = [0.0] * n
            net = [0.0] * n
            qty = [0.0] * n
            fill_price = [0.0] * n
            slip_cost = [0.0] * n
            for i in range(n):
                if exec_amt[i] <= 0 or ref_price[i] <= 0:
                    continue
                fp = ref_price[i] * (1.0 + slip)
                f = exec_amt[i] * fee_rate
                nt = exec_amt[i] - f
                if fp <= 0 or nt <= 0:
                    continue
                q = nt / fp
                fill_price[i] = fp
                fee[i] = f
                net[i] = fp * q          # ≈ nt
                qty[i] = q
                slip_cost[i] = abs(fp - ref_price[i]) * q

            # ---- 5. 累计持仓 / 投入 / 成本（列式 cumsum）----
            cum_btc = _cumsum(qty)
            cum_invested = _cumsum(exec_amt)          # gross ≈ exec_amt
            cum_fee = _cumsum(fee)

            # ---- 6. 现金账户（仅初始资金部署影响现金）----
            initial_exec_bar = (1 if next_open else 0) if initial_cap > 0 else -1
            cash = [0.0] * n
            for i in range(n):
                cash[i] = initial_cap if (initial_exec_bar >= 0 and i < initial_exec_bar) else 0.0

            # ---- 7. 净值曲线 value = cash + btc × close ----
            value = [cash[i] + cum_btc[i] * close[i] for i in range(n)]
            avg_cost = [
                (cum_invested[i] / cum_btc[i]) if cum_btc[i] > 0 else 0.0 for i in range(n)
            ]

            curve = pl.DataFrame({
                "date": dates,
                "value": value,
                "invested": cum_invested,
                "cash": cash,
                "btc_amount": cum_btc,
                "avg_cost": avg_cost,
                "price": close,
            })

            # ---- 8. 逐笔成交记录 ----
            trades = self._build_trades(
                ts, fill_price, qty, net, fee, slip_cost, exec_amt,
                exec_reason, cum_btc, cum_invested, avg_cost,
            )

            metrics = bt_metrics.calculate_all(
                curve, trades=trades, risk_free_rate=config.risk_free_rate,
                initial_capital=0.0,
            )

            # ---- 9. 泄漏校验（内存/DB feed 均适用）----
            leakage_ok = True
            if self._feed is not None:
                try:
                    self._feed.advance_time(_to_utc(ts[-1]))
                    self._feed.validate_no_leakage()
                except Exception as exc:  # noqa: BLE001
                    logger.error("向量化回测前视检测失败: {}", exc)
                    return self._failed(config, started, f"未来数据泄漏: {exc}")

            bench = bt_metrics.lump_sum_benchmark(
                dates, close, float(cum_invested[-1]) if cum_invested else 0.0
            )
            completed = datetime.now(UTC)
            return BacktestResult(
                config=config, status="COMPLETED", started_at=started, completed_at=completed,
                duration_seconds=int((completed - started).total_seconds()),
                metrics=metrics, equity_curve=curve, trades=trades,
                initial_capital=Decimal(config.initial_capital),
                total_invested=_dec(cum_invested[-1] if cum_invested else 0.0, Q_AMOUNT),
                final_value=_dec(value[-1] if value else 0.0, Q_AMOUNT),
                total_fees=_dec(cum_fee[-1] if cum_fee else 0.0, Q_AMOUNT),
                btc_accumulated=_dec(cum_btc[-1] if cum_btc else 0.0, Q_BTC),
                avg_cost_basis=_dec(avg_cost[-1] if avg_cost else 0.0, _Q_PRICE),
                engine=EngineType.VECTORIZED, data_mode=config.data_mode, approximate=True,
                leakage_validated=leakage_ok,
                benchmark_return=(
                    _dec(bench, Decimal("0.000001")) if bench is not None else None
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 回测失败需捕获并落库
            logger.exception("向量化回测执行异常: {}", exc)
            return self._failed(config, started, str(exc))

    # ---- 数据加载 ----

    async def _load_prices(
        self, config: BacktestConfig, prices_df: pl.DataFrame | None
    ) -> pl.DataFrame | None:
        """加载并归一化价格序列为 ts/open/high/low/close 列（Float64）。"""
        df: pl.DataFrame | None = None
        if prices_df is not None:
            df = prices_df
        elif self._feed is not None:
            start = datetime.combine(config.start_date, time.min, tzinfo=UTC)
            end = datetime.combine(config.end_date, time.max, tzinfo=UTC)
            df = await self._feed.load_bars(config.symbol, start, end, config.interval)
        if df is None or df.height == 0:
            return None
        return self._normalize(df)

    @staticmethod
    def _normalize(df: pl.DataFrame) -> pl.DataFrame:
        """归一化列名与类型：产出 ts(naive UTC datetime) + open/high/low/close(Float64)。"""
        d = df
        # 时间列
        if "_ts" in d.columns:
            d = d.rename({"_ts": "ts"})
        elif "time" in d.columns:
            d = d.rename({"time": "ts"})
        elif "date" in d.columns:
            d = d.rename({"date": "ts"})
        else:
            raise ValueError("价格序列缺少时间列（ts/time/date/_ts）")
        # 日期 -> datetime
        if d.schema["ts"] == pl.Date:
            d = d.with_columns(pl.col("ts").cast(pl.Datetime("us")))
        # 去时区（统一 naive UTC）
        if isinstance(d.schema["ts"], pl.Datetime) and d.schema["ts"].time_zone is not None:
            d = d.with_columns(
                pl.col("ts").dt.convert_time_zone("UTC").dt.replace_time_zone(None)
            )
        d = d.sort("ts")
        # OHLC 补全
        if "open" not in d.columns:
            d = d.with_columns(pl.col("close").alias("open"))
        for col in ("high", "low"):
            if col not in d.columns:
                d = d.with_columns(pl.col("close").alias(col))
        d = d.with_columns([
            pl.col("open").cast(pl.Float64),
            pl.col("high").cast(pl.Float64),
            pl.col("low").cast(pl.Float64),
            pl.col("close").cast(pl.Float64),
        ])
        return d.select(["ts", "open", "high", "low", "close"])

    # ---- 乘数矩阵 ----

    def _multiplier_column(
        self,
        config: BacktestConfig,
        strategy: Strategy,
        px: pl.DataFrame,
        dates: list[date],
    ) -> tuple[list[float], str]:
        """按策略类型计算逐 Bar 投入乘数（列式），返回 (乘数列表, 触发原因)。"""
        n = px.height
        name = (config.strategy_name or "").lower()
        close = px["close"]

        if name in {"fixed_dca", "fixed"}:
            return [1.0] * n, "SCHEDULED_DCA"

        if name in {"dip_buy"}:
            ath = close.cum_max()
            dist = ((close - ath) / ath).to_list()
            grad = _gradient_pairs(getattr(strategy, "gradient", None))
            mult = [_apply_gradient(dist[i], grad) for i in range(n)]
            return mult, "DIP_BUY"

        if name in {"drawdown_buy"}:
            recent = close.rolling_max(window_size=self._window, min_periods=1)
            dd = ((close - recent) / recent).to_list()
            grad = _gradient_pairs(getattr(strategy, "gradient", None))
            mult = [_apply_gradient(dd[i], grad) for i in range(n)]
            return mult, "DRAWDOWN_BUY"

        if name in {"valuation_dca", "valuation"}:
            indicator = getattr(strategy, "indicator", "onchain.mvrv")
            pct = self._percentile_series(indicator, dates)
            grad = _valuation_pairs(getattr(strategy, "gradient", None))
            mult = [_apply_valuation(pct[i], grad) for i in range(n)]
            return mult, "VALUATION_BUY"

        if name in {"risk_adjusted_dca", "risk_adjusted"}:
            levels = self._risk_level_series(dates)
            table = {k: float(v) for k, v in (getattr(strategy, "multipliers", {}) or {}).items()}
            mult = [(table.get(levels[i], 1.0) if levels[i] else 1.0) for i in range(n)]
            return mult, "RISK_ADJUSTED"

        # rule_based / custom：不做列式展开，降级为基础定投
        logger.warning(
            "向量化引擎不支持策略 {!r} 的规则展开，降级为基础定投（approximate）",
            config.strategy_name,
        )
        return [1.0] * n, "SCHEDULED_DCA"

    def _percentile_series(self, indicator: str, dates: list[date]) -> list[float | None]:
        """从 feed 内存预取指标分位序列，按日期前向对齐；缺失返回 [None]*n。"""
        n = len(dates)
        if self._feed is None:
            return [None] * n
        inds = getattr(self._feed, "_indicators", None)
        if not inds:
            return [None] * n
        df = inds.get(indicator.lower())
        if df is None or "percentile" not in df.columns:
            return [None] * n
        return _asof_series(df, dates, "percentile")

    def _risk_level_series(self, dates: list[date]) -> list[str | None]:
        """从 feed 内存引擎状态预取 risk_level 序列，按日期前向对齐。"""
        n = len(dates)
        if self._feed is None:
            return [None] * n
        sdf = getattr(self._feed, "_states_df", None)
        if sdf is None or "risk_level" not in sdf.columns:
            return [None] * n
        return _asof_series(sdf, dates, "risk_level")

    # ---- 交易记录 ----

    @staticmethod
    def _build_trades(
        ts: list[datetime], fill_price: list[float], qty: list[float], net: list[float],
        fee: list[float], slip_cost: list[float], exec_amt: list[float],
        exec_reason: list[str], cum_btc: list[float], cum_invested: list[float],
        avg_cost: list[float],
    ) -> list[Trade]:
        """由列式结果重建逐笔 Trade 记录（仅 BUY）。"""
        trades: list[Trade] = []
        seq = 0
        for i in range(len(ts)):
            if qty[i] <= 0:
                continue
            seq += 1
            trades.append(Trade(
                trade_number=seq, time=_to_utc(ts[i]), side=Side.BUY,
                price=_dec(fill_price[i], _Q_PRICE),
                quantity_btc=_dec(qty[i], Q_BTC),
                amount=_dec(net[i], Q_AMOUNT),
                fee=_dec(fee[i], Decimal("0.000001")),
                slippage=_dec(slip_cost[i], Decimal("0.000001")),
                trigger_reason=exec_reason[i] or "SCHEDULED_DCA",
                cumulative_btc=_dec(cum_btc[i], Q_BTC),
                cumulative_invested=_dec(cum_invested[i], Q_AMOUNT),
                avg_cost_after=_dec(avg_cost[i], _Q_PRICE),
            ))
        return trades

    @staticmethod
    def _failed(config: BacktestConfig, started: datetime, message: str) -> BacktestResult:
        """构造失败结果。"""
        completed = datetime.now(UTC)
        return BacktestResult(
            config=config, status="FAILED", error_message=message,
            started_at=started, completed_at=completed,
            duration_seconds=int((completed - started).total_seconds()),
            engine=EngineType.VECTORIZED, data_mode=DataMode.AS_RUN, approximate=True,
        )


# ---------------------------------------------------------------------------
# 列式辅助
# ---------------------------------------------------------------------------

def _cumsum(xs: list[float]) -> list[float]:
    """前缀和（Polars 列式）。"""
    if not xs:
        return []
    return pl.Series(xs).cum_sum().to_list()


def _dec(x: Any, quant: Decimal) -> Decimal:
    """float/Decimal -> 量化 Decimal（可复现）。"""
    if x is None:
        return Decimal("0")
    return Decimal(str(x)).quantize(quant, rounding=ROUND_HALF_UP)


def _gradient_pairs(raw: Any) -> list[tuple[float, float]]:
    """把策略梯度（list[(Decimal, Decimal)]）转为 float 对，按阈值升序（最负优先）。"""
    if not raw:
        return []
    pairs = [(float(t), float(f)) for t, f in raw]
    return sorted(pairs, key=lambda x: x[0])


def _apply_gradient(dist: float | None, grad: list[tuple[float, float]]) -> float:
    """梯度映射：返回首个满足 dist <= threshold 的 factor（阈值升序）；无则 1.0。

    与 :meth:`app.backtest.strategy.DipBuyStrategy.compute_multiplier` 语义一致
    （策略按最负阈值优先判定）。
    """
    if dist is None or not grad:
        return 1.0
    for threshold, factor in grad:
        if dist <= threshold:
            return factor
    return 1.0


def _valuation_pairs(raw: Any) -> list[tuple[float, float]]:
    """估值梯度（list[(float 分位, Decimal 倍数)]）转 float 对，按分位升序。"""
    if not raw:
        return []
    return sorted([(float(p), float(f)) for p, f in raw], key=lambda x: x[0])


def _apply_valuation(pct: float | None, grad: list[tuple[float, float]]) -> float:
    """估值乘数（复刻 ValuationDCAStrategy.compute_multiplier 的分位梯度部分）。

    低估档（threshold <= 50）：pct <= threshold 取更大倍数并中断；
    高估档（threshold > 50）：pct >= threshold 取更小倍数。
    """
    if pct is None or not grad:
        return 1.0
    multiplier = 1.0
    for threshold, factor in grad:
        if threshold > 50:
            if pct >= threshold and factor < multiplier:
                multiplier = factor
        elif pct <= threshold:
            if factor > multiplier:
                multiplier = factor
            break
    return multiplier


def _asof_series(
    df: pl.DataFrame, dates: list[date], value_col: str
) -> list[Any]:
    """把 (含 _ts/ts/date + value_col) 的 DataFrame 按 dates 前向对齐（as-of）。

    对每个目标日期，取 observation_time <= 该日 的最新一行的 value_col；无则 None。
    """
    d = df
    if "_ts" in d.columns:
        tcol = "_ts"
    elif "ts" in d.columns:
        tcol = "ts"
    elif "date" in d.columns:
        tcol = "date"
    else:
        return [None] * len(dates)
    d = d.select([pl.col(tcol).alias("_t"), pl.col(value_col).alias("_v")]).sort("_t")
    if d.schema["_t"] == pl.Date:
        d = d.with_columns(pl.col("_t").cast(pl.Datetime("us")))
    if isinstance(d.schema["_t"], pl.Datetime) and d.schema["_t"].time_zone is not None:
        d = d.with_columns(pl.col("_t").dt.convert_time_zone("UTC").dt.replace_time_zone(None))
    left = pl.DataFrame({"_t": [datetime.combine(x, time.min) for x in dates]}).sort("_t")
    joined = left.join_asof(d, on="_t", strategy="backward")
    # join_asof 会排序，需还原到 dates 原顺序
    lookup = {row["_t"]: row["_v"] for row in joined.iter_rows(named=True)}
    return [lookup.get(datetime.combine(x, time.min)) for x in dates]


__all__ = ["VectorizedBacktest"]
