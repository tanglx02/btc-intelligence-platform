"""Point-in-Time 数据供给器（对应 14-backtest-architecture.md §2，防未来数据泄漏）。

这是回测系统**最高优先级约束**的实现。违反任何一条的回测结果视为无效。

核心机制（§2.1）：所有含时间语义的数据均具备 observation_time 与 fetch_time
双字段，PIT 查询强制双重过滤：
    observation_time <= as_of  AND  fetch_time <= as_of
即「数据所属时间不超前」且「当时系统已经拿到」。同一 observation_time 存在多
版本（修订）时，返回 fetch_time <= as_of 中最新的一版（当时能看到的版本）。

宏观数据三时间轴（§2.2，强制）：macro_series 用 **release_date <= as_of** 过滤
（不是 observation_date）——2022-06 CPI 在 2022-07-13 才发布，as_of=2022-07-01
时不可见。修订版本按 release/revision 时间过滤，禁止用修订后数据模拟过去。

时间旅行断言（§2.5）：本供给器记录全部数据访问的 (source, observation_time,
fetch_time, as_of)，validate_no_leakage() 断言 max(fetch_time) <= as_of_max，
违例即回测失败。

支持两种模式：
- DB 模式：传入 AsyncSession，实时查询标准化表（authoritative）；
- 内存模式：传入预加载的 Polars DataFrame（无 IO，便于测试与向量化回测）。
两种模式共享同一 PIT 语义与访问审计。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.engine import CycleState, MarketRegime, RiskScore, ValuationState
from app.models.indicator import IndicatorDefinition, IndicatorValue
from app.models.macro import MacroSeries
from app.models.market import Candle, MarketPrice


class LookAheadViolationError(Exception):
    """检测到未来数据泄漏（前视偏差）。"""


def _as_utc(dt: datetime) -> datetime:
    """确保 datetime 带 UTC 时区（naive 视为 UTC）。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _naive(dt: datetime) -> datetime:
    """转为 naive UTC datetime（供 Polars 内存比较，Polars 默认存 naive）。"""
    return _as_utc(dt).replace(tzinfo=None)


@dataclass
class DataAccessException:
    """单次数据访问审计记录（§2.5 时间旅行断言用）。"""

    source: str                       # 表名：market_prices / indicator_values / macro_series ...
    observation_time: datetime | None
    fetch_time: datetime | None
    as_of: datetime                   # 访问时的模拟时间
    released: bool = True             # 是否通过 PIT 过滤（False 表示本应不可见但被拦截）

    @property
    def is_violation(self) -> bool:
        """是否构成泄漏：fetch_time 或 observation_time 超过 as_of。"""
        as_of = _as_utc(self.as_of)
        if self.fetch_time is not None and _as_utc(self.fetch_time) > as_of:
            return True
        if self.observation_time is not None and _as_utc(self.observation_time) > as_of:
            return True
        return False


@dataclass
class _PriceRow:
    price: Decimal
    observation_time: datetime
    fetch_time: datetime | None = None


class PointInTimeDataFeed:
    """防未来数据泄漏的数据供给器。

    Usage（DB 模式）::

        feed = PointInTimeDataFeed(current_time=start, session=session)
        feed.advance_time(bar_time)
        price = await feed.get_price("BTCUSDT", bar_time)
        mvrv = await feed.get_indicator("onchain.mvrv", bar_time)

    Usage（内存模式）::

        feed = PointInTimeDataFeed(current_time=start, prices_df=df, indicators={...})
    """

    def __init__(
        self,
        current_time: datetime,
        session: AsyncSession | None = None,
        prices_df: pl.DataFrame | None = None,
        indicators: dict[str, pl.DataFrame] | None = None,
        macro_df: pl.DataFrame | None = None,
        states_df: pl.DataFrame | None = None,
    ):
        """初始化。

        Args:
            current_time: 初始模拟时间（as_of 游标）。
            session: 异步数据库会话（DB 模式）。
            prices_df: 内存价格序列，须含 date/time、close（可含 open/high/low/volume、
                fetch_time）。
            indicators: 内存指标 {name: df(date, value, percentile?, fetch_time?)}。
            macro_df: 内存宏观 df(series_id, observation_date, release_date, value,
                revision_number?)。
            states_df: 内存引擎状态 df(date, cycle_phase?, valuation_level?,
                risk_level?, risk_score?, regime?)。
        """
        self._current_time = _as_utc(current_time)
        self._session = session
        self._prices_df = self._normalize_time(prices_df) if prices_df is not None else None
        self._indicators = (
            {k.lower(): self._normalize_time(v) for k, v in indicators.items()}
            if indicators else None
        )
        self._macro_df = macro_df
        self._states_df = self._normalize_time(states_df) if states_df is not None else None
        # 访问审计日志
        self._access_log: list[DataAccessException] = []
        self._as_of_max = self._current_time

    # ---- 时间游标 ----

    @property
    def current_time(self) -> datetime:
        """当前模拟时间（as_of）。"""
        return self._current_time

    def advance_time(self, new_time: datetime) -> None:
        """推进模拟时间（严格单调，§1.2 Clock 事件时间单调）。"""
        new_time = _as_utc(new_time)
        if new_time < self._current_time:
            raise ValueError(
                f"模拟时间不得回退：{new_time} < {self._current_time}（Clock 单调约束）"
            )
        self._current_time = new_time
        if new_time > self._as_of_max:
            self._as_of_max = new_time

    # ---- 价格 ----

    async def get_price(self, symbol: str, time: datetime) -> Decimal | None:
        """获取价格（只返回 observation_time <= current_time 且 <= time 的数据）。

        Args:
            symbol: 交易对。
            time: 请求的数据时间（会被 as_of 游标截断）。

        Returns:
            价格 Decimal；无可用数据返回 None。
        """
        as_of = self._current_time
        effective = min(_as_utc(time), as_of)  # 不得超过 as_of（防越界访问）
        if self._prices_df is not None:
            return self._mem_price(symbol, effective)
        if self._session is not None:
            return await self._db_price(symbol, effective)
        return None

    def _mem_price(self, symbol: str, as_of: datetime) -> Decimal | None:
        """内存模式价格查找（PIT：date <= as_of 的最新一条）。"""
        df = self._prices_df
        if df is None or df.height == 0:
            return None
        if "symbol" in df.columns:
            df = df.filter(pl.col("symbol") == symbol)
        has_fetch = "fetch_time" in df.columns
        naive_as_of = _naive(as_of)
        mask = pl.col("_ts") <= naive_as_of
        if has_fetch:
            mask = mask & (pl.col("fetch_time").is_null() | (pl.col("fetch_time") <= naive_as_of))
        filtered = df.filter(mask).sort("_ts")
        if filtered.height == 0:
            return None
        row = filtered.row(-1, named=True)
        self._record("market_prices", row.get("_ts_dt"), row.get("fetch_time"), as_of)
        return Decimal(str(row["close"]))

    async def _db_price(self, symbol: str, as_of: datetime) -> Decimal | None:
        """DB 模式价格查找：优先 candles（日线），回退 market_prices。

        双重过滤 observation_time <= as_of AND fetch_time <= as_of（§2.1）。
        """
        assert self._session is not None
        # candles（含 fetch_time，PIT 双重过滤）
        stmt = (
            select(Candle.close, Candle.observation_time, Candle.fetch_time)
            .where(Candle.symbol == symbol)
            .where(Candle.observation_time <= as_of)
            .where(Candle.fetch_time <= as_of)
            .order_by(Candle.observation_time.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is not None:
            close, obs, fetch = row
            self._record("candles", obs, fetch, as_of)
            return Decimal(close)
        # market_prices 回退
        stmt2 = (
            select(MarketPrice.price, MarketPrice.observation_time, MarketPrice.fetch_time)
            .where(MarketPrice.symbol == symbol)
            .where(MarketPrice.observation_time <= as_of)
            .where(MarketPrice.fetch_time <= as_of)
            .order_by(MarketPrice.observation_time.desc())
            .limit(1)
        )
        row2 = (await self._session.execute(stmt2)).first()
        if row2 is not None:
            price, obs, fetch = row2
            self._record("market_prices", obs, fetch, as_of)
            return Decimal(price)
        return None

    async def load_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        interval: str = "1d",
    ) -> pl.DataFrame:
        """加载 [start, end] 区间的 K 线序列（按时间升序），供事件循环遍历。

        注意：加载区间价格本身不构成泄漏——引擎按时间顺序逐 Bar 推进 as_of，
        策略只能基于当前及之前的 Bar 决策；指标/宏观查询另行 PIT 过滤。
        DB 模式下仍施加 fetch_time <= end 的 as-run 过滤。
        """
        if self._prices_df is not None:
            df = self._prices_df
            if "symbol" in df.columns:
                df = df.filter(pl.col("symbol") == symbol)
            df = df.filter((pl.col("_ts") >= _naive(start)) & (pl.col("_ts") <= _naive(end)))
            return df.sort("_ts")
        if self._session is not None:
            stmt = (
                select(Candle)
                .where(Candle.symbol == symbol)
                .where(Candle.observation_time >= _as_utc(start))
                .where(Candle.observation_time <= _as_utc(end))
                .where(Candle.fetch_time <= _as_utc(end))
                .order_by(Candle.observation_time.asc())
            )
            rows = (await self._session.execute(stmt)).scalars().all()
            return pl.DataFrame({
                "time": [r.observation_time for r in rows],
                "open": [float(r.open) for r in rows],
                "high": [float(r.high) for r in rows],
                "low": [float(r.low) for r in rows],
                "close": [float(r.close) for r in rows],
                "volume": [float(r.volume) for r in rows],
            })
        return pl.DataFrame()

    # ---- 指标 ----

    async def get_indicator(self, name: str, time: datetime) -> float | None:
        """获取指标值（严格 Point-in-Time：observation_time/fetch_time <= as_of）。

        Args:
            name: 指标代码（如 "onchain.mvrv" / "tech.rsi"）。
            time: 请求时间（被 as_of 截断）。

        Returns:
            指标 float 值；无可用数据返回 None（消费方按 §4.5 降级为 UNKNOWN）。
        """
        as_of = self._current_time
        effective = min(_as_utc(time), as_of)
        key = name.lower()
        if self._indicators is not None:
            return self._mem_indicator(key, effective)
        if self._session is not None:
            return await self._db_indicator(key, name, effective)
        return None

    async def get_indicator_percentile(self, name: str, time: datetime) -> float | None:
        """获取指标历史分位（PIT）。as-run 落库值优先，禁止回测时重算全样本分位（§2.3）。"""
        as_of = self._current_time
        effective = min(_as_utc(time), as_of)
        key = name.lower()
        if self._indicators is not None:
            df = self._indicators.get(key)
            if df is None or "percentile" not in df.columns:
                return None
            filtered = self._pit_filter_mem(df, effective)
            if filtered.height == 0:
                return None
            row = filtered.row(-1, named=True)
            pct = row.get("percentile")
            return float(pct) if pct is not None else None
        if self._session is not None:
            defn = await self._indicator_def(name)
            if defn is None:
                return None
            stmt = (
                select(IndicatorValue.percentile, IndicatorValue.observation_time,
                       IndicatorValue.fetch_time)
                .where(IndicatorValue.indicator_id == defn)
                .where(IndicatorValue.observation_time <= effective)
                .where(IndicatorValue.fetch_time <= effective)
                .order_by(IndicatorValue.observation_time.desc())
                .limit(1)
            )
            row = (await self._session.execute(stmt)).first()
            if row is not None:
                pct, obs, fetch = row
                self._record("indicator_values", obs, fetch, as_of)
                return float(pct) if pct is not None else None
        return None

    def _mem_indicator(self, key: str, as_of: datetime) -> float | None:
        df = self._indicators.get(key) if self._indicators else None
        if df is None:
            return None
        filtered = self._pit_filter_mem(df, as_of)
        if filtered.height == 0:
            return None
        row = filtered.row(-1, named=True)
        self._record("indicator_values", row.get("_ts_dt"), row.get("fetch_time"), as_of)
        val = row.get("value")
        return float(val) if val is not None else None

    def _pit_filter_mem(self, df: pl.DataFrame, as_of: datetime) -> pl.DataFrame:
        """内存 PIT 过滤：_ts <= as_of 且 (fetch_time 空 或 <= as_of)，按 _ts 升序。"""
        naive_as_of = _naive(as_of)
        mask = pl.col("_ts") <= naive_as_of
        if "fetch_time" in df.columns:
            mask = mask & (pl.col("fetch_time").is_null() | (pl.col("fetch_time") <= naive_as_of))
        return df.filter(mask).sort("_ts")

    async def _db_indicator(self, key: str, name: str, as_of: datetime) -> float | None:
        assert self._session is not None
        defn = await self._indicator_def(name)
        if defn is None:
            return None
        stmt = (
            select(IndicatorValue.value, IndicatorValue.observation_time, IndicatorValue.fetch_time)
            .where(IndicatorValue.indicator_id == defn)
            .where(IndicatorValue.observation_time <= as_of)
            .where(IndicatorValue.fetch_time <= as_of)
            .order_by(IndicatorValue.observation_time.desc(), IndicatorValue.fetch_time.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is not None:
            value, obs, fetch = row
            self._record("indicator_values", obs, fetch, as_of)
            return float(value)
        return None

    async def _indicator_def(self, name: str) -> Any | None:
        """按 code 解析 indicator_definitions.id（缓存）。"""
        assert self._session is not None
        if not hasattr(self, "_defn_cache"):
            self._defn_cache: dict[str, Any] = {}
        if name in self._defn_cache:
            return self._defn_cache[name]
        stmt = select(IndicatorDefinition.id).where(IndicatorDefinition.code == name).limit(1)
        defn = (await self._session.execute(stmt)).scalar_one_or_none()
        self._defn_cache[name] = defn
        return defn

    # ---- 宏观（三时间轴，§2.2）----

    async def get_macro_data(self, series_id: str, time: datetime) -> float | None:
        """获取宏观数据（使用 release_date <= current_time，不是 observation_date）。

        这是防泄漏的关键：宏观数据用发布日期过滤。同一 observation_date 存在
        多修订版本时，返回 release_date/revision_date <= as_of 中版本号最大的一版。
        """
        as_of = self._current_time
        effective = min(_as_utc(time), as_of)
        eff_date = effective.date()
        if self._macro_df is not None:
            return self._mem_macro(series_id, eff_date)
        if self._session is not None:
            return await self._db_macro(series_id, eff_date)
        return None

    def _mem_macro(self, series_id: str, as_of_date: date) -> float | None:
        df = self._macro_df
        if df is None or df.height == 0:
            return None
        df = df.filter(pl.col("series_id") == series_id)
        # release_date <= as_of（三时间轴核心）
        df = df.filter(pl.col("release_date") <= as_of_date)
        if df.height == 0:
            return None
        # 同 observation_date 取版本号最大（当时能看到的最新修订）
        rev_col = "revision_number" if "revision_number" in df.columns else None
        df = df.sort(["observation_date"] + ([rev_col] if rev_col else []))
        # 取 observation_date 最新的一版
        row = df.row(-1, named=True)
        self._record("macro_series", row.get("observation_date"), row.get("release_date"),
                     datetime.combine(as_of_date, datetime.min.time(), tzinfo=UTC))
        val = row.get("value")
        return float(val) if val is not None else None

    async def _db_macro(self, series_id: str, as_of_date: date) -> float | None:
        assert self._session is not None
        # release_date <= as_of：当时已发布的版本；按 observation_date、revision_number 排序取最新
        stmt = (
            select(MacroSeries.value, MacroSeries.observation_date, MacroSeries.release_date)
            .where(MacroSeries.series_id == series_id)
            .where(MacroSeries.release_date <= as_of_date)
            .order_by(
                MacroSeries.observation_date.desc(), MacroSeries.revision_number.desc()
            )
            .limit(1)
        )
        row = (await self._session.execute(stmt)).first()
        if row is not None:
            value, obs_date, release_date = row
            self._record(
                "macro_series",
                datetime.combine(obs_date, datetime.min.time(), tzinfo=UTC),
                datetime.combine(release_date, datetime.min.time(), tzinfo=UTC),
                self._current_time,
            )
            return float(value)
        return None

    # ---- 引擎状态（as-run 优先，§5.1）----

    async def get_engine_state(self, time: datetime) -> dict[str, Any]:
        """获取当日引擎状态（cycle/valuation/risk/regime），PIT 过滤。

        as-run 模式消费历史落库行；缺失时返回空 dict（消费方降级）。
        """
        as_of = self._current_time
        effective = min(_as_utc(time), as_of)
        if self._states_df is not None:
            return self._mem_state(effective)
        if self._session is not None:
            return await self._db_state(effective)
        return {}

    def _mem_state(self, as_of: datetime) -> dict[str, Any]:
        df = self._states_df
        if df is None:
            return {}
        filtered = self._pit_filter_mem(df, as_of)
        if filtered.height == 0:
            return {}
        row = filtered.row(-1, named=True)
        self._record("engine_states", row.get("_ts_dt"), row.get("fetch_time"), as_of)
        return {k: v for k, v in row.items() if not k.startswith("_")}

    async def _db_state(self, as_of: datetime) -> dict[str, Any]:
        assert self._session is not None
        out: dict[str, Any] = {}
        # cycle_states
        cyc = (await self._session.execute(
            select(CycleState.phase, CycleState.observation_time)
            .where(CycleState.observation_time <= as_of)
            .order_by(CycleState.observation_time.desc()).limit(1)
        )).first()
        if cyc:
            out["cycle_phase"] = cyc[0].value if hasattr(cyc[0], "value") else cyc[0]
            self._record("cycle_states", cyc[1], cyc[1], as_of)
        # valuation_states
        val = (await self._session.execute(
            select(ValuationState.valuation_level, ValuationState.observation_time)
            .where(ValuationState.observation_time <= as_of)
            .order_by(ValuationState.observation_time.desc()).limit(1)
        )).first()
        if val:
            out["valuation_level"] = val[0].value if hasattr(val[0], "value") else val[0]
            self._record("valuation_states", val[1], val[1], as_of)
        # risk_scores
        risk = (await self._session.execute(
            select(RiskScore.risk_level, RiskScore.overall_risk, RiskScore.observation_time)
            .where(RiskScore.observation_time <= as_of)
            .order_by(RiskScore.observation_time.desc()).limit(1)
        )).first()
        if risk:
            out["risk_level"] = risk[0].value if hasattr(risk[0], "value") else risk[0]
            out["risk_score"] = float(risk[1]) if risk[1] is not None else None
            self._record("risk_scores", risk[2], risk[2], as_of)
        # market_regimes
        reg = (await self._session.execute(
            select(MarketRegime.overall_regime, MarketRegime.observation_time)
            .where(MarketRegime.observation_time <= as_of)
            .order_by(MarketRegime.observation_time.desc()).limit(1)
        )).first()
        if reg:
            out["regime"] = reg[0]
            self._record("market_regimes", reg[1], reg[1], as_of)
        return out

    # ---- 访问审计与泄漏检测（§2.5）----

    def _record(
        self,
        source: str,
        observation_time: Any | None,
        fetch_time: Any | None,
        as_of: datetime,
    ) -> None:
        """记录一次数据访问（时间旅行断言用）。"""
        obs = self._to_dt(observation_time)
        fetch = self._to_dt(fetch_time)
        self._access_log.append(DataAccessException(source, obs, fetch, _as_utc(as_of)))

    @staticmethod
    def _to_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return _as_utc(value)
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time(), tzinfo=UTC)
        return None

    @property
    def access_log(self) -> list[DataAccessException]:
        """全部数据访问审计记录。"""
        return list(self._access_log)

    def validate_no_leakage(self, accessed_data: list[DataAccessException] | None = None) -> bool:
        """验证没有访问未来数据（§2.5 时间旅行断言）。

        断言：所有访问记录的 fetch_time 与 observation_time 均 <= 各自 as_of，
        且全局 max(fetch_time) <= as_of_max。

        Args:
            accessed_data: 待校验记录；None 时校验内部访问日志全部记录。

        Returns:
            True = 无泄漏。

        Raises:
            LookAheadViolationError: 检测到泄漏。
        """
        records = accessed_data if accessed_data is not None else self._access_log
        for rec in records:
            if rec.is_violation:
                raise LookAheadViolationError(
                    f"未来数据泄漏：source={rec.source} observation_time={rec.observation_time} "
                    f"fetch_time={rec.fetch_time} > as_of={rec.as_of}"
                )
        # 全局断言：max(fetch_time) <= as_of_max
        fetch_times = [
            _as_utc(r.fetch_time) for r in records if r.fetch_time is not None
        ]
        if fetch_times and max(fetch_times) > self._as_of_max:
            raise LookAheadViolationError(
                f"全局前视断言失败：max(fetch_time)={max(fetch_times)} "
                f"> as_of_max={self._as_of_max}"
            )
        return True

    # ---- 内部工具 ----

    @staticmethod
    def _normalize_time(df: pl.DataFrame) -> pl.DataFrame:
        """为 df 增加统一的 ``_ts``（naive UTC datetime）与 ``_ts_dt`` 列，供 PIT 比较。

        支持 date/time/timestamp/observation_time 作为时间列；带时区的列先转 UTC 再去 tz。
        """
        out = df
        time_col = None
        for cand in ("time", "date", "timestamp", "observation_time"):
            if cand in out.columns:
                time_col = cand
                break
        if time_col is None:
            raise ValueError("DataFrame 须含 time/date/timestamp/observation_time 列")
        dtype = out[time_col].dtype
        if dtype == pl.Date:
            out = out.with_columns(pl.col(time_col).cast(pl.Datetime).alias("_ts"))
        elif isinstance(dtype, pl.Datetime):
            if getattr(dtype, "time_zone", None) is not None:
                out = out.with_columns(
                    pl.col(time_col).dt.convert_time_zone("UTC").dt.replace_time_zone(None).alias("_ts")
                )
            else:
                out = out.with_columns(pl.col(time_col).alias("_ts"))
        else:
            out = out.with_columns(pl.col(time_col).cast(pl.Datetime, strict=False).alias("_ts"))
        out = out.with_columns(pl.col("_ts").alias("_ts_dt"))
        # fetch_time 归一（若存在）
        if "fetch_time" in out.columns:
            ft_dtype = out["fetch_time"].dtype
            if ft_dtype == pl.Date:
                out = out.with_columns(pl.col("fetch_time").cast(pl.Datetime))
            elif (
                isinstance(ft_dtype, pl.Datetime)
                and getattr(ft_dtype, "time_zone", None) is not None
            ):
                out = out.with_columns(
                    pl.col("fetch_time").dt.convert_time_zone("UTC").dt.replace_time_zone(None)
                )
        return out
