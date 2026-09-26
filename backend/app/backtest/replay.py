"""历史回放系统（对应 14-backtest-architecture.md §5）。

提供两种视角的市场状态恢复：

- **当时视角（perspective="current" / then）**：严格 Point-in-Time，只呈现
  ``date`` 当日及之前系统实际可见的信息（价格、指标、引擎状态、宏观），用于
  「如果当时我在这个位置，会看到什么」的复盘。通过构造以 ``date`` 为 as_of 的
  :class:`~app.backtest.data_feed.PointInTimeDataFeed` 强制防泄漏；
- **事后视角（perspective="hindsight" / after）**：在当前时点回看，额外提供未来
  30/90/180/365 天的实际走势（收益、回撤、区间高低点），用于事后归因与教学。
  该视角**刻意越过 PIT 边界**读取未来价格，故仅用于回放展示，绝不参与任何
  回测决策（§5.2）。

关键约束：``get_market_state`` 走 PIT feed（then 视角可信）；``get_future_outcome``
走原始价格序列（after 视角，标注为事后数据）。二者物理隔离，杜绝把未来数据
混入当时视角。
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from datetime import date as _date
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.data_feed import PointInTimeDataFeed, _as_utc
from app.backtest.types import (
    Bar,
    FutureOutcome,
    MarketStateAtDate,
    ReplaySnapshot,
)
from app.models.market import Candle, MarketPrice

# 当时视角默认恢复的指标（可按需覆盖）
DEFAULT_INDICATORS: list[str] = [
    "onchain.mvrv",
    "onchain.nupl",
    "onchain.sopr",
    "momentum.rsi",
    "volatility.realized_vol",
]
# 事后视角默认 horizon（天）
DEFAULT_HORIZONS: list[int] = [30, 90, 180, 365]


def _to_utc(dt: datetime) -> datetime:
    """确保 datetime 带 UTC 时区（naive 视为 UTC）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _coerce_datetime(d: Any) -> datetime:
    """把 date / datetime / ISO 字符串统一为 tz-aware UTC datetime。"""
    if isinstance(d, datetime):
        return _to_utc(d)
    if isinstance(d, _date):
        return datetime.combine(d, time.min, tzinfo=UTC)
    if isinstance(d, str):
        return _to_utc(datetime.fromisoformat(d))
    raise TypeError(f"无法解析日期: {d!r}")


class HistoryReplay:
    """历史回放系统（§5）。

    Usage::

        replay = HistoryReplay(session=session, symbol="BTCUSDT")
        snap = await replay.get_snapshot(datetime(2022, 6, 18), perspective="hindsight")
        snap.market_state.risk_level        # 当时视角（PIT）
        snap.future_outcome.horizons[90]    # 事后视角（未来 90 天）
    """

    def __init__(
        self,
        session: AsyncSession | None = None,
        prices_df: pl.DataFrame | None = None,
        indicators: dict[str, pl.DataFrame] | None = None,
        states_df: pl.DataFrame | None = None,
        macro_df: pl.DataFrame | None = None,
        symbol: str = "BTCUSDT",
        indicator_names: list[str] | None = None,
    ):
        """初始化。

        Args:
            session: 异步数据库会话（DB 模式）。
            prices_df: 内存价格序列（含 date/time、close，可选 open/high/low）。
            indicators: 内存指标 {name: df(date, value, percentile?)}。
            states_df: 内存引擎状态 df(date, cycle_phase?, valuation_level?,
                risk_level?, risk_score?, regime?)。
            macro_df: 内存宏观 df(series_id, observation_date, release_date, value)。
            symbol: 交易对。
            indicator_names: 当时视角恢复的指标 code 列表（默认 DEFAULT_INDICATORS）。
        """
        self._session = session
        self._prices_df = prices_df
        self._indicators = indicators
        self._states_df = states_df
        self._macro_df = macro_df
        self._symbol = symbol
        self._indicator_names = indicator_names or DEFAULT_INDICATORS

    # ---- 公共 API ----

    async def get_snapshot(
        self, date: Any, perspective: str = "current"
    ) -> ReplaySnapshot:
        """获取指定日期的完整市场快照。

        Args:
            date: 目标日期（date / datetime / ISO 字符串）。
            perspective: ``"current"`` 当时视角（只看已知信息）；``"hindsight"``
                事后视角（附未来 30/90/180/365 天走势）。

        Returns:
            ReplaySnapshot（market_state 恒有；future_outcome 仅事后视角填充）。
        """
        dt = _coerce_datetime(date)
        market_state = await self.get_market_state(dt)
        future: FutureOutcome | None = None
        if perspective == "hindsight":
            future = await self.get_future_outcome(dt)
        return ReplaySnapshot(
            date=dt, perspective=perspective, market_state=market_state,
            future_outcome=future,
        )

    async def get_market_state(self, date: Any) -> MarketStateAtDate:
        """恢复当日市场状态（价格、指标、引擎输出、宏观），严格 Point-in-Time。

        通过构造以 ``date`` 为 as_of 的 PIT feed 实现防泄漏：任何 observation_time
        或 fetch_time 超过 ``date`` 的数据均不可见。
        """
        dt = _coerce_datetime(date)
        feed = PointInTimeDataFeed(
            current_time=dt, session=self._session, prices_df=self._prices_df,
            indicators=self._indicators, macro_df=self._macro_df, states_df=self._states_df,
        )
        price = await feed.get_price(self._symbol, dt)
        state = await feed.get_engine_state(dt)

        ind_values: dict[str, float | None] = {}
        ind_pcts: dict[str, float | None] = {}
        for name in self._indicator_names:
            val = await feed.get_indicator(name, dt)
            pct = await feed.get_indicator_percentile(name, dt)
            ind_values[name] = val
            ind_pcts[name] = pct

        candle = await self._pit_candle(dt)
        data_avail = {
            "price": price is not None,
            "candle": candle is not None,
            "engine_state": bool(state),
            "indicators": {k: (v is not None) for k, v in ind_values.items()},
        }
        return MarketStateAtDate(
            date=dt, price=price, candle=candle,
            indicators=ind_values, indicator_percentiles=ind_pcts,
            cycle_phase=state.get("cycle_phase"),
            valuation_level=state.get("valuation_level"),
            risk_level=state.get("risk_level"),
            risk_score=state.get("risk_score"),
            regime=state.get("regime"),
            recomputed=False,   # as-run：消费历史落库值，非重算
            data_availability=data_avail,
        )

    async def get_future_outcome(
        self, date: Any, days: list[int] | None = None
    ) -> FutureOutcome:
        """获取事后视角的未来走势（刻意越过 PIT 边界，仅用于回放展示，§5.2）。

        Args:
            date: 基准日期。
            days: horizon 列表（默认 [30, 90, 180, 365]）。

        Returns:
            FutureOutcome，horizons[N] = {future_date, price, return_pct,
            max_drawdown, min_price, max_price, days_available}。
        """
        base_dt = _coerce_datetime(date)
        horizons = days or DEFAULT_HORIZONS
        series = await self._raw_price_series(base_dt)  # [(datetime, Decimal)] 升序，含 base 之后
        base_price = self._price_at_or_before(series, base_dt)
        out: dict[int, dict[str, Any]] = {}
        for h in horizons:
            target = _to_utc(base_dt + _days_delta(h))
            window = [
                (t, p) for t, p in series
                if base_dt <= t <= target
            ]
            fp = self._price_at_or_before(series, target)
            entry: dict[str, Any] = {
                "future_date": target,
                "price": fp,
                "return_pct": None,
                "max_drawdown": None,
                "min_price": None,
                "max_price": None,
                "days_available": len(window),
            }
            if fp is not None and base_price is not None and base_price > 0:
                entry["return_pct"] = float((fp - base_price) / base_price)
            if window:
                prices = [p for _, p in window]
                entry["min_price"] = min(prices)
                entry["max_price"] = max(prices)
                entry["max_drawdown"] = _max_drawdown_decimal(prices)
            out[h] = entry
        return FutureOutcome(base_date=base_dt, horizons=out)

    # ---- 内部：PIT 当日 K 线（用于 candle 字段）----

    async def _pit_candle(self, dt: datetime) -> Bar | None:
        """取当日（或之前最近一根）K 线，PIT 过滤（fetch_time <= dt）。"""
        if self._prices_df is not None:
            df = self._prices_df
            if "symbol" in df.columns:
                df = df.filter(pl.col("symbol") == self._symbol)
            tcol = _pick_time_col(df)
            if tcol is None:
                return None
            naive = _as_utc(dt).replace(tzinfo=None)
            norm = _normalize_ts(df, tcol)
            filtered = norm.filter(pl.col("_ts") <= naive).sort("_ts")
            if filtered.height == 0:
                return None
            row = filtered.row(-1, named=True)
            close = Decimal(str(row["close"]))
            return Bar(
                time=_to_utc(row["_ts"].to_pydatetime() if hasattr(row["_ts"], "to_pydatetime")
                             else row["_ts"]),
                open=Decimal(str(row.get("open", close))),
                high=Decimal(str(row.get("high", close))),
                low=Decimal(str(row.get("low", close))),
                close=close,
                volume=Decimal(str(row.get("volume", 0))),
                symbol=self._symbol,
            )
        if self._session is not None:
            row = (await self._session.execute(
                select(Candle)
                .where(Candle.symbol == self._symbol)
                .where(Candle.observation_time <= dt)
                .where(Candle.fetch_time <= dt)
                .order_by(Candle.observation_time.desc())
                .limit(1)
            )).scalars().first()
            if row is not None:
                return Bar(
                    time=_to_utc(row.observation_time), open=Decimal(row.open),
                    high=Decimal(row.high), low=Decimal(row.low), close=Decimal(row.close),
                    volume=Decimal(row.volume or 0), symbol=self._symbol,
                )
        return None

    # ---- 内部：原始价格序列（事后视角，无 PIT）----

    async def _raw_price_series(self, base_dt: datetime) -> list[tuple[datetime, Decimal]]:
        """加载 base_dt 当日及之后的原始价格序列（升序），供未来走势计算。

        注意：此路径**不施加 PIT 过滤**，是事后视角专用；不得用于回测决策。
        """
        series: list[tuple[datetime, Decimal]] = []
        if self._prices_df is not None:
            df = self._prices_df
            if "symbol" in df.columns:
                df = df.filter(pl.col("symbol") == self._symbol)
            tcol = _pick_time_col(df)
            if tcol is None:
                return []
            norm = _normalize_ts(df, tcol)
            naive = _as_utc(base_dt).replace(tzinfo=None)
            norm = norm.filter(pl.col("_ts") >= naive).sort("_ts")
            for row in norm.iter_rows(named=True):
                t = row["_ts"]
                tdt = _to_utc(t.to_pydatetime() if hasattr(t, "to_pydatetime") else t)
                series.append((tdt, Decimal(str(row["close"]))))
            return series
        if self._session is not None:
            rows = (await self._session.execute(
                select(Candle.observation_time, Candle.close)
                .where(Candle.symbol == self._symbol)
                .where(Candle.observation_time >= base_dt)
                .order_by(Candle.observation_time.asc())
            )).all()
            if rows:
                for obs, close in rows:
                    series.append((_to_utc(obs), Decimal(str(close))))
                return series
            # 回退 market_prices
            rows2 = (await self._session.execute(
                select(MarketPrice.observation_time, MarketPrice.price)
                .where(MarketPrice.symbol == self._symbol)
                .where(MarketPrice.observation_time >= base_dt)
                .order_by(MarketPrice.observation_time.asc())
            )).all()
            for obs, price in rows2:
                series.append((_to_utc(obs), Decimal(str(price))))
        return series

    @staticmethod
    def _price_at_or_before(
        series: list[tuple[datetime, Decimal]], target: datetime
    ) -> Decimal | None:
        """取 <= target 的最新价格（series 已升序）。"""
        latest: Decimal | None = None
        for t, p in series:
            if t <= target:
                latest = p
            else:
                break
        return latest


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _days_delta(days: int) -> Any:
    """构造 timedelta（局部导入避免顶层循环依赖开销）。"""
    from datetime import timedelta

    return timedelta(days=days)


def _pick_time_col(df: pl.DataFrame) -> str | None:
    """挑选时间列名。"""
    for c in ("_ts", "time", "date", "observation_time", "ts"):
        if c in df.columns:
            return c
    return None


def _normalize_ts(df: pl.DataFrame, tcol: str) -> pl.DataFrame:
    """为 df 增加 naive UTC 的 _ts 列（去时区、date→datetime）。"""
    d = df
    if tcol != "_ts":
        d = d.rename({tcol: "_ts"})
    if d.schema["_ts"] == pl.Date:
        d = d.with_columns(pl.col("_ts").cast(pl.Datetime("us")))
    if isinstance(d.schema["_ts"], pl.Datetime) and d.schema["_ts"].time_zone is not None:
        d = d.with_columns(
            pl.col("_ts").dt.convert_time_zone("UTC").dt.replace_time_zone(None)
        )
    return d


def _max_drawdown_decimal(prices: list[Decimal]) -> float | None:
    """区间最大回撤（正数表示，如 0.35 = 35%）。"""
    peak: Decimal | None = None
    mdd = Decimal("0")
    for p in prices:
        if peak is None or p > peak:
            peak = p
        if peak and peak > 0:
            dd = (peak - p) / peak
            if dd > mdd:
                mdd = dd
    return float(mdd) if mdd is not None else None


__all__ = ["HistoryReplay", "DEFAULT_INDICATORS", "DEFAULT_HORIZONS"]
