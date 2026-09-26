"""portfolio.dca_simulator 定投模拟引擎测试。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import polars as pl

from app.portfolio.dca_simulator import DCAConfig, DCASimulator, SimulationResult


def _price_df(n: int = 120, start: date = date(2023, 1, 1)) -> pl.DataFrame:
    """构造确定性价格序列（先跌后涨，用于触发逢跌加仓）。"""
    dates = [start + timedelta(days=i) for i in range(n)]
    prices = []
    for i in range(n):
        if i < n // 2:
            prices.append(50000.0 - 400.0 * i)      # 下跌段
        else:
            prices.append(50000.0 - 400.0 * (n // 2) + 500.0 * (i - n // 2))
    return pl.DataFrame({"date": dates, "close": prices})


async def test_fixed_dca_simulation() -> None:
    """固定定投：每日投入 100，累计 BTC 与投入为正。"""
    cfg = DCAConfig(
        initial_capital=Decimal("0"), periodic_amount=Decimal("100"),
        frequency="DAILY", start_date=date(2023, 1, 1), end_date=date(2023, 4, 30),
        strategy="FIXED", fee_rate=Decimal("0.001"),
    )
    result = await DCASimulator().simulate(cfg, _price_df())
    assert isinstance(result, SimulationResult)
    assert result.final_btc > 0
    assert result.total_invested > 0
    assert result.transaction_count > 0
    assert result.equity_curve.height > 0
    for col in ("date", "invested", "btc_amount", "value"):
        assert col in result.equity_curve.columns


async def test_fixed_dca_avg_cost_matches_manual() -> None:
    """无手续费时，avg_cost = 总投入 / 总 BTC。"""
    cfg = DCAConfig(
        periodic_amount=Decimal("100"), frequency="DAILY",
        start_date=date(2023, 1, 1), end_date=date(2023, 2, 1),
        strategy="FIXED", fee_rate=Decimal("0"),
    )
    result = await DCASimulator().simulate(cfg, _price_df(32))
    assert result.final_btc > 0
    implied = (result.total_invested / result.final_btc).quantize(Decimal("0.00000001"))
    assert abs(implied - result.avg_cost) <= Decimal("0.01")


async def test_dip_buy_invests_more_than_fixed_on_decline() -> None:
    """下跌行情中，逢跌加仓累计投入 ≥ 固定定投。"""
    df = _price_df()
    fixed = await DCASimulator().simulate(
        DCAConfig(periodic_amount=Decimal("100"), frequency="DAILY",
                  start_date=date(2023, 1, 1), end_date=date(2023, 4, 30),
                  strategy="FIXED", fee_rate=Decimal("0")),
        df,
    )
    dip = await DCASimulator().simulate(
        DCAConfig(periodic_amount=Decimal("100"), frequency="DAILY",
                  start_date=date(2023, 1, 1), end_date=date(2023, 4, 30),
                  strategy="DIP_BUY", fee_rate=Decimal("0")),
        df,
    )
    assert dip.total_invested >= fixed.total_invested
    assert dip.final_btc >= fixed.final_btc


async def test_empty_price_data_returns_empty_result() -> None:
    """空价格序列安全返回（不抛异常）。"""
    cfg = DCAConfig(periodic_amount=Decimal("100"), frequency="DAILY",
                    start_date=date(2023, 1, 1), strategy="FIXED")
    empty = pl.DataFrame({"date": [], "close": []})
    result = await DCASimulator().simulate(cfg, empty)
    assert result.final_btc == 0
    assert result.transaction_count == 0


async def test_simulation_determinism() -> None:
    """相同输入必产生相同输出。"""
    cfg = DCAConfig(periodic_amount=Decimal("100"), frequency="DAILY",
                    start_date=date(2023, 1, 1), end_date=date(2023, 3, 1),
                    strategy="FIXED")
    df = _price_df(60)
    a = await DCASimulator().simulate(cfg, df)
    b = await DCASimulator().simulate(cfg, df)
    assert a.final_btc == b.final_btc
    assert a.total_invested == b.total_invested
