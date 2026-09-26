"""每日组合快照服务（对应 15-portfolio-architecture.md §5）。

职责：每日 UTC 收盘后（或补洞回填时）为用户生成投资组合净值快照，
写入 portfolio_snapshots 表，供资产曲线图与收益/回撤统计使用。

快照口径（§5.1）：
- total_invested：截至当日累计净投入；
- btc_holdings：截至当日持仓；
- avg_cost：移动加权平均成本；
- btc_value：当日收盘市值；
- cumulative_return / max_drawdown：基于快照市值净值序列滚动计算。

原则：快照只追加/更新当日行，历史行不重算（除非用户显式触发重算）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest import metrics as bt_metrics
from app.models.market import Candle
from app.models.portfolio import PortfolioSnapshot, UserTransaction
from app.portfolio.calculator import PortfolioCalculator, q_amount, q_rate

_Q_BTC = Decimal("0.00000001")


def _day_bounds(d: date) -> tuple[datetime, datetime]:
    """返回某日的 [起, 止) UTC 时间边界。"""
    start = datetime.combine(d, time.min, tzinfo=UTC)
    end = start + timedelta(days=1)
    return start, end


class SnapshotService:
    """每日组合快照服务。

    Usage::

        svc = SnapshotService(session)
        snap = await svc.generate_daily_snapshot(user_id, date(2026, 1, 1), Decimal("95000"))
    """

    def __init__(self, session: AsyncSession, symbol: str = "BTCUSDT"):
        """初始化。

        Args:
            session: 异步数据库会话。
            symbol: 补生成历史快照时用于查询日线的交易对。
        """
        self._session = session
        self._symbol = symbol

    async def generate_daily_snapshot(
        self,
        user_id: UUID,
        snapshot_date: date,
        btc_price: Decimal,
        cash_balance: Decimal = Decimal("0"),
    ) -> PortfolioSnapshot:
        """生成指定日期的组合快照（持仓、市值、收益率、回撤）。

        Args:
            user_id: 用户 ID。
            snapshot_date: 快照日（UTC）。
            btc_price: 当日 BTC 收盘价（快照用价格）。
            cash_balance: 现金余额（默认 0，纯持仓口径）。

        Returns:
            写入/更新后的 PortfolioSnapshot 行。
        """
        day_start, day_end = _day_bounds(snapshot_date)

        # 截至当日（含）的全部交易，按时间升序
        txs = await self._fetch_transactions_until(user_id, day_end)
        holdings = PortfolioCalculator.calculate_holdings(txs)

        btc_value = q_amount(holdings.btc_balance * Decimal(btc_price))
        total_value = q_amount(btc_value + Decimal(cash_balance))
        unrealized_pnl = q_amount(btc_value - holdings.total_cost)

        # 取历史快照计算累计/日收益率与回撤
        prior = await self._fetch_prior_snapshots(user_id, day_start)
        obs_time = datetime.combine(snapshot_date, time.min, tzinfo=UTC)
        series_values = [float(s.total_value) for s in prior] + [float(total_value)]
        series_dates = [s.observation_time.date() for s in prior] + [snapshot_date]

        cumulative_return = (
            q_rate(total_value / holdings.total_invested - 1)
            if holdings.total_invested > 0 else None
        )
        daily_return = self._daily_return(prior, total_value)
        max_dd = q_rate(Decimal(str(bt_metrics.max_drawdown(series_values))))
        current_dd_series = bt_metrics.max_drawdown_series(series_values)
        current_dd = q_rate(Decimal(str(current_dd_series[-1]))) if current_dd_series else None

        # 30 日波动率与夏普（基于快照日收益序列）
        rets = bt_metrics.daily_returns_from_values(series_values)
        volatility_30d = self._volatility(rets[-30:])
        sharpe = bt_metrics.sharpe_ratio(rets)
        annualized = bt_metrics.irr_from_equity_curve(
            series_dates, [float(holdings.total_invested)] * len(series_dates), float(total_value)
        )

        allocation = {
            "BTC": float(btc_value),
            "CASH": float(cash_balance),
        }

        snap = await self._upsert_snapshot(
            user_id=user_id,
            obs_time=obs_time,
            total_value=total_value,
            total_value_cny=total_value,  # 本实现以 CNY 为默认计价，多币种由上层换算
            total_invested=q_amount(holdings.total_invested),
            cash_balance=q_amount(Decimal(cash_balance)),
            btc_value=btc_value,
            btc_holdings=holdings.btc_balance.quantize(_Q_BTC, rounding=ROUND_HALF_UP),
            unrealized_pnl=unrealized_pnl,
            realized_pnl=q_amount(holdings.realized_pnl),
            daily_return=daily_return,
            cumulative_return=cumulative_return,
            annualized_return=(
                Decimal(str(annualized)).quantize(Decimal("0.000001"))
                if annualized is not None else None
            ),
            max_drawdown=max_dd,
            current_drawdown=current_dd,
            volatility_30d=volatility_30d,
            sharpe_ratio=(
                Decimal(str(sharpe)).quantize(Decimal("0.0001")) if sharpe is not None else None
            ),
            allocation=allocation,
            metrics={
                "avg_cost": str(holdings.avg_cost),
                "price_used": str(btc_price),
                "transaction_count": holdings.transaction_count,
                "total_fees": str(holdings.total_fees),
            },
        )
        logger.debug(
            "生成组合快照 user={} date={} total_value={}", user_id, snapshot_date, total_value
        )
        return snap

    async def generate_historical_snapshots(
        self,
        user_id: UUID,
        start: date,
        end: date,
        prices: dict[date, Decimal] | None = None,
    ) -> list[PortfolioSnapshot]:
        """补生成 [start, end] 区间的历史快照。

        Args:
            user_id: 用户 ID。
            start: 起始日（含）。
            end: 结束日（含）。
            prices: 每日价格 {date: price}；None 时从 candles 表查询日线收盘价。

        Returns:
            生成的快照列表（缺价格数据的日期跳过并记录日志）。
        """
        if end < start:
            raise ValueError("end 不得早于 start")
        price_map = prices or await self._fetch_daily_prices(start, end)
        results: list[PortfolioSnapshot] = []
        cur = start
        while cur <= end:
            price = price_map.get(cur)
            if price is None:
                logger.warning("跳过快照生成 user={} date={}：无价格数据", user_id, cur)
                cur += timedelta(days=1)
                continue
            snap = await self.generate_daily_snapshot(user_id, cur, Decimal(price))
            results.append(snap)
            cur += timedelta(days=1)
        logger.info(
            "补生成历史快照 user={} [{} ~ {}] 共 {} 天", user_id, start, end, len(results)
        )
        return results

    # ---- 内部：数据查询 ----

    async def _fetch_transactions_until(
        self, user_id: UUID, until: datetime
    ) -> list[UserTransaction]:
        """查询截至 until（不含）的用户交易，按时间升序。"""
        stmt = (
            select(UserTransaction)
            .where(UserTransaction.user_id == user_id)
            .where(UserTransaction.transaction_time < until)
            .order_by(UserTransaction.transaction_time.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def _fetch_prior_snapshots(
        self, user_id: UUID, before: datetime
    ) -> list[PortfolioSnapshot]:
        """查询 before（不含）之前的历史快照，按观测时间升序。"""
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.user_id == user_id)
            .where(PortfolioSnapshot.observation_time < before)
            .order_by(PortfolioSnapshot.observation_time.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def _fetch_daily_prices(self, start: date, end: date) -> dict[date, Decimal]:
        """从 candles 表查询日线收盘价 {date: close}。"""
        start_dt = datetime.combine(start, time.min, tzinfo=UTC)
        end_dt = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
        stmt = (
            select(Candle.observation_time, Candle.close)
            .where(Candle.symbol == self._symbol)
            .where(Candle.observation_time >= start_dt)
            .where(Candle.observation_time < end_dt)
            .order_by(Candle.observation_time.asc())
        )
        rows = (await self._session.execute(stmt)).all()
        out: dict[date, Decimal] = {}
        for obs, close in rows:
            d = obs.date() if isinstance(obs, datetime) else obs
            out[d] = Decimal(close)
        return out

    # ---- 内部：指标计算 ----

    @staticmethod
    def _daily_return(prior: list[PortfolioSnapshot], total_value: Decimal) -> Decimal | None:
        """日收益率 = 今日总值 / 昨日总值 − 1。"""
        if not prior:
            return None
        prev_value = prior[-1].total_value
        if prev_value and prev_value > 0:
            return q_rate(total_value / Decimal(prev_value) - 1)
        return None

    @staticmethod
    def _volatility(daily_rets: list[float]) -> Decimal | None:
        """年化波动率 = 日收益率标准差 × √365。"""
        rets = [r for r in daily_rets if r == r]  # 过滤 NaN
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vol = (var ** 0.5) * (365 ** 0.5)
        return Decimal(str(vol)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # ---- 内部：写入（存在则更新，否则插入）----

    async def _upsert_snapshot(
        self, user_id: UUID, obs_time: datetime, **fields: object
    ) -> PortfolioSnapshot:
        """按 (user_id, observation_time) upsert 快照行。

        因表主键为复合 (id, user_id, observation_time) 且无 (user_id, observation_time)
        唯一约束，此处显式查询已有行后 update/insert，避免依赖 on_conflict。
        """
        stmt = (
            select(PortfolioSnapshot)
            .where(PortfolioSnapshot.user_id == user_id)
            .where(PortfolioSnapshot.observation_time == obs_time)
            .limit(1)
        )
        existing = (await self._session.execute(stmt)).scalars().first()
        if existing is not None:
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.snapshot_time = datetime.now(UTC)
            await self._session.flush()
            return existing
        snap = PortfolioSnapshot(
            user_id=user_id,
            observation_time=obs_time,
            snapshot_time=datetime.now(UTC),
            **fields,  # type: ignore[arg-type]
        )
        self._session.add(snap)
        await self._session.flush()
        return snap


# 复用 calculator 的量化函数导出（便于上层统一精度）
__all__ = ["SnapshotService", "q_amount", "q_rate"]
