"""backtest 事件驱动引擎、向量化引擎与策略测试。

含 §1.1 双引擎一致性断言：同一策略、同一数据下，事件驱动（权威）与向量化
（近似）结果的收益率/终值偏差应在容差内。
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import polars as pl
import pytest

from app.backtest.data_feed import PointInTimeDataFeed
from app.backtest.engine import BacktestEngine
from app.backtest.strategy import (
    DipBuyStrategy,
    FixedDCAStrategy,
    create_strategy,
)
from app.backtest.types import BacktestConfig, EngineType, FillMode
from app.backtest.vectorized import VectorizedBacktest

_N = 120
_START = date(2023, 1, 1)
_END = _START + timedelta(days=_N - 1)


def _bars_df(n: int = _N) -> pl.DataFrame:
    """确定性合成 K 线（正弦叠加趋势），无随机。"""
    dates = [datetime.combine(_START + timedelta(days=i), time.min) for i in range(n)]
    close = [30000.0 + 50.0 * i + 3000.0 * math.sin(i / 15.0) for i in range(n)]
    open_ = [close[0]] + close[:-1]
    high = [max(o, c) * 1.01 for o, c in zip(open_, close, strict=True)]
    low = [min(o, c) * 0.99 for o, c in zip(open_, close, strict=True)]
    return pl.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low, "close": close,
    })


def _config(fill_mode: FillMode, strategy: str = "fixed_dca") -> BacktestConfig:
    return BacktestConfig(
        strategy_name=strategy,
        initial_capital=Decimal("0"),
        periodic_investment=Decimal("100"),
        frequency="DAILY",
        start_date=_START,
        end_date=_END,
        fee_rate=Decimal("0.001"),
        slippage=Decimal("0.0005"),
        fill_mode=fill_mode,
        symbol="BTCUSDT",
    )


def _feed(df: pl.DataFrame) -> PointInTimeDataFeed:
    start_dt = datetime.combine(_START, time.min, tzinfo=UTC)
    return PointInTimeDataFeed(current_time=start_dt, prices_df=df)


async def test_event_driven_runs() -> None:
    """事件驱动回测跑通并产出权威结果。"""
    df = _bars_df()
    engine = BacktestEngine(feed=_feed(df))
    res = await engine.run(_config(FillMode.SAME_CLOSE))
    assert res.status == "COMPLETED"
    assert res.engine == EngineType.EVENT_DRIVEN
    assert res.approximate is False
    assert res.leakage_validated is True
    assert res.btc_accumulated > 0
    assert len(res.trades) > 0
    assert res.equity_curve is not None and res.equity_curve.height == _N


async def test_vectorized_runs() -> None:
    """向量化回测跑通并标注 approximate。"""
    df = _bars_df()
    vt = VectorizedBacktest(feed=_feed(df))
    res = await vt.run(_config(FillMode.SAME_CLOSE))
    assert res.status == "COMPLETED"
    assert res.engine == EngineType.VECTORIZED
    assert res.approximate is True
    assert res.btc_accumulated > 0


async def test_dual_engine_consistency_same_close() -> None:
    """§1.1 一致性（SAME_CLOSE）：两引擎终值/收益偏差 < 0.5%。"""
    df = _bars_df()
    cfg = _config(FillMode.SAME_CLOSE)
    res_e = await BacktestEngine(feed=_feed(df)).run(cfg)
    res_v = await VectorizedBacktest(feed=_feed(df)).run(cfg)
    assert res_e.status == res_v.status == "COMPLETED"

    fv_e, fv_v = float(res_e.final_value), float(res_v.final_value)
    assert math.isclose(fv_e, fv_v, rel_tol=0.005), (fv_e, fv_v)

    btc_e, btc_v = float(res_e.btc_accumulated), float(res_v.btc_accumulated)
    assert math.isclose(btc_e, btc_v, rel_tol=0.005), (btc_e, btc_v)

    tr_e = res_e.metrics["total_return"]
    tr_v = res_v.metrics["total_return"]
    assert tr_e is not None and tr_v is not None
    assert abs(tr_e - tr_v) < 0.01


async def test_dual_engine_consistency_next_open() -> None:
    """§1.1 一致性（NEXT_OPEN 次开盘成交）：终值偏差 < 1%。"""
    df = _bars_df()
    cfg = _config(FillMode.NEXT_OPEN)
    res_e = await BacktestEngine(feed=_feed(df)).run(cfg)
    res_v = await VectorizedBacktest(feed=_feed(df)).run(cfg)
    assert res_e.status == res_v.status == "COMPLETED"
    fv_e, fv_v = float(res_e.final_value), float(res_v.final_value)
    assert math.isclose(fv_e, fv_v, rel_tol=0.01), (fv_e, fv_v)


async def test_no_data_returns_failed() -> None:
    """区间无数据时回测失败而非崩溃。"""
    empty = pl.DataFrame({
        "date": [], "open": [], "high": [], "low": [], "close": [],
    }).with_columns(pl.col("date").cast(pl.Datetime("us")))
    engine = BacktestEngine(feed=_feed(empty))
    res = await engine.run(_config(FillMode.SAME_CLOSE))
    assert res.status == "FAILED"


def test_create_strategy_registry() -> None:
    """策略注册表按名创建，未知策略抛 ValueError。"""
    assert isinstance(create_strategy("fixed_dca"), FixedDCAStrategy)
    assert isinstance(create_strategy("dip_buy"), DipBuyStrategy)
    assert isinstance(create_strategy("fixed"), FixedDCAStrategy)   # 别名
    with pytest.raises(ValueError):
        create_strategy("no_such_strategy")


async def test_dip_buy_accumulates_more_on_decline() -> None:
    """下跌行情中逢跌加仓累计 BTC ≥ 固定定投。"""
    n = 90
    dates = [datetime.combine(_START + timedelta(days=i), time.min) for i in range(n)]
    close = [50000.0 - 300.0 * i for i in range(n)]      # 单调下跌
    df = pl.DataFrame({
        "date": dates, "open": close, "high": close, "low": close, "close": close,
    })
    end = _START + timedelta(days=n - 1)
    base = BacktestConfig(
        strategy_name="fixed_dca", initial_capital=Decimal("0"),
        periodic_investment=Decimal("100"), frequency="DAILY",
        start_date=_START, end_date=end, fill_mode=FillMode.SAME_CLOSE,
    )
    dip = BacktestConfig(
        strategy_name="dip_buy", initial_capital=Decimal("0"),
        periodic_investment=Decimal("100"), frequency="DAILY",
        start_date=_START, end_date=end, fill_mode=FillMode.SAME_CLOSE,
    )
    res_fixed = await BacktestEngine(feed=_feed(df)).run(base)
    res_dip = await BacktestEngine(feed=_feed(df)).run(dip)
    assert res_dip.total_invested >= res_fixed.total_invested
    assert res_dip.btc_accumulated >= res_fixed.btc_accumulated
