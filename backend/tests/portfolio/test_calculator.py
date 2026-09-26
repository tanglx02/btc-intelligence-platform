"""portfolio.calculator 持仓与收益计算测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.portfolio.calculator import Holdings, PortfolioCalculator


@dataclass
class Tx:
    """交易鸭子类型（对齐 TransactionLike 协议）。"""

    side: str
    amount: Decimal
    price: Decimal
    quantity_btc: Decimal
    fee: Decimal = Decimal("0")


@dataclass
class Snap:
    total_value: Decimal
    observation_time: datetime


def _dt(day: int) -> datetime:
    return datetime(2023, 1, day, tzinfo=UTC)


def test_holdings_two_buys_weighted_avg_cost() -> None:
    """两次买入：移动加权平均成本 = (100+200)/2 = 150。"""
    txs = [
        Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1")),
        Tx("BUY", Decimal("200"), Decimal("200"), Decimal("1")),
    ]
    h = PortfolioCalculator.calculate_holdings(txs)
    assert isinstance(h, Holdings)
    assert h.btc_balance == Decimal("2.00000000")
    assert h.avg_cost == Decimal("150.00000000")
    assert h.total_buy_amount == Decimal("300.00")


def test_holdings_buy_with_fee_included_in_cost() -> None:
    """买入手续费计入成本：amount=100, fee=1 -> 1 BTC 成本 101。"""
    txs = [Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1"), fee=Decimal("1"))]
    h = PortfolioCalculator.calculate_holdings(txs)
    assert h.avg_cost == Decimal("101.00000000")
    assert h.total_fees == Decimal("1.00")


def test_dca_cost_ignores_sells() -> None:
    """calculate_dca_cost 只统计买入。"""
    txs = [
        Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1")),
        Tx("SELL", Decimal("150"), Decimal("150"), Decimal("1")),
        Tx("BUY", Decimal("200"), Decimal("200"), Decimal("1")),
    ]
    cost = PortfolioCalculator.calculate_dca_cost(txs)
    assert cost == Decimal("150.00000000")


def test_sell_realizes_pnl_and_keeps_avg_cost() -> None:
    """卖出不改变 avg_cost，实现盈亏 = 净卖出 - 卖出量×avg_cost。"""
    txs = [
        Tx("BUY", Decimal("200"), Decimal("100"), Decimal("2")),   # avg_cost=100
        Tx("SELL", Decimal("150"), Decimal("150"), Decimal("1")),  # 卖 1 BTC @150
    ]
    h = PortfolioCalculator.calculate_holdings(txs)
    assert h.btc_balance == Decimal("1.00000000")
    assert h.avg_cost == Decimal("100.00000000")     # 卖出不改 avg_cost
    assert h.realized_pnl == Decimal("50.00")        # 150 - 1*100


def test_full_liquidation_resets_cost_basis() -> None:
    """清仓后 avg_cost 归零、余额为零。"""
    txs = [
        Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1")),
        Tx("SELL", Decimal("120"), Decimal("120"), Decimal("1")),
    ]
    h = PortfolioCalculator.calculate_holdings(txs)
    assert h.btc_balance == Decimal("0")
    assert h.avg_cost == Decimal("0")
    assert h.realized_pnl == Decimal("20.00")


def test_quantity_inferred_from_amount_price() -> None:
    """quantity_btc 缺省时由 amount/price 推导。"""
    txs = [Tx("BUY", Decimal("100"), Decimal("50"), Decimal("0"))]
    h = PortfolioCalculator.calculate_holdings(txs)
    assert h.btc_balance == Decimal("2.00000000")


def test_unknown_side_raises() -> None:
    """未知方向抛 ValueError。"""
    txs = [Tx("HODL", Decimal("1"), Decimal("1"), Decimal("1"))]
    try:
        PortfolioCalculator.calculate_holdings(txs)
    except ValueError:
        return
    raise AssertionError("应对未知方向抛 ValueError")


def test_performance_with_price() -> None:
    """给定现价时计算市值、浮盈与收益率。"""
    txs = [Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1"))]
    p = PortfolioCalculator.calculate_performance(txs, current_price=Decimal("150"))
    assert p.current_value == Decimal("150.00")
    assert p.unrealized_pnl == Decimal("50.00")       # 150 - 成本 100
    assert p.pnl == Decimal("50.00")


def test_max_drawdown_from_snapshots() -> None:
    """快照市值 100->120->60->90：最大回撤 50%。"""
    snaps = [
        Snap(Decimal("100"), _dt(1)),
        Snap(Decimal("120"), _dt(2)),
        Snap(Decimal("60"), _dt(3)),
        Snap(Decimal("90"), _dt(4)),
    ]
    dd = PortfolioCalculator.calculate_max_drawdown(snaps)
    assert dd == Decimal("0.500000")
    cur = PortfolioCalculator.calculate_current_drawdown(snaps)
    assert cur == Decimal("0.250000")     # 90 相对峰值 120


def test_determinism() -> None:
    """相同输入必产生相同输出。"""
    txs = [Tx("BUY", Decimal("100"), Decimal("100"), Decimal("1"))]
    a = PortfolioCalculator.calculate_holdings(txs)
    b = PortfolioCalculator.calculate_holdings(txs)
    assert a == b
