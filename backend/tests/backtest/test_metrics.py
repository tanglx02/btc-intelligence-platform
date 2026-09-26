"""backtest.metrics 绩效指标测试。"""

from __future__ import annotations

import math
from datetime import date

import polars as pl

from app.backtest import metrics as m


def test_max_drawdown_basic() -> None:
    """峰值 120 -> 谷值 60 = 50% 回撤。"""
    assert math.isclose(m.max_drawdown([100, 120, 60, 90]), 0.5, rel_tol=1e-9)


def test_max_drawdown_monotonic_up_is_zero() -> None:
    assert m.max_drawdown([10, 20, 30, 40]) == 0.0


def test_max_drawdown_series_last_point() -> None:
    series = m.max_drawdown_series([100, 120, 60, 90])
    # 末点 90 相对峰值 120 = 25%
    assert math.isclose(series[-1], 0.25, rel_tol=1e-9)


def test_solve_irr_simple() -> None:
    """-100 @ t0, +110 @ t1 -> IRR ≈ 10%。"""
    irr = m.solve_irr([(0.0, -100.0), (1.0, 110.0)])
    assert irr is not None
    assert math.isclose(irr, 0.10, abs_tol=1e-6)


def test_daily_returns() -> None:
    rets = m.daily_returns_from_values([100.0, 110.0, 105.0])
    assert math.isclose(rets[0], 0.10, rel_tol=1e-9)
    assert math.isclose(rets[1], 105.0 / 110.0 - 1.0, rel_tol=1e-9)


def test_calculate_all_keys_and_signs() -> None:
    """calculate_all 返回 §4.1 全部键，且回撤非负、净值增长时总收益为正。"""
    dates = [date(2023, 1, i + 1) for i in range(10)]
    values = [100.0 + 10 * i for i in range(10)]
    invested = [100.0 + 10 * i for i in range(10)]
    curve = pl.DataFrame({
        "date": dates, "value": values, "invested": invested,
        "btc_amount": [v / 30000 for v in values], "avg_cost": [30000.0] * 10,
    })
    out = m.calculate_all(curve)
    for key in (
        "total_return", "annualized_return", "max_drawdown", "sharpe_ratio",
        "sortino_ratio", "calmar_ratio", "win_rate", "profit_factor",
        "total_trades", "total_fees", "final_value", "final_btc", "avg_cost",
    ):
        assert key in out
    assert out["max_drawdown"] >= 0.0
    assert out["final_value"] == values[-1]


def test_calculate_all_empty_curve() -> None:
    """空净值曲线安全返回（不抛异常）。"""
    empty = pl.DataFrame({"date": [], "value": [], "invested": []})
    out = m.calculate_all(empty)
    assert out["total_trades"] == 0
    assert out["total_return"] is None


def test_lump_sum_benchmark() -> None:
    """首日 100 -> 末日 150 = 50% 一次性买入基准。"""
    dates = [date(2023, 1, 1), date(2023, 6, 1)]
    prices = [100.0, 150.0]
    assert math.isclose(m.lump_sum_benchmark(dates, prices, 1000.0), 0.5, rel_tol=1e-9)


def test_yearly_returns() -> None:
    """跨年净值切分年度收益。"""
    dates = [date(2022, 12, 31), date(2023, 12, 31)]
    values = [100.0, 130.0]
    yr = m.yearly_returns(dates, values)
    assert 2023 in yr


def test_backtest_metrics_facade_delegates() -> None:
    """BacktestMetrics 门面委托到模块纯函数。"""
    assert m.BacktestMetrics.max_drawdown([100, 120, 60, 90]) == m.max_drawdown([100, 120, 60, 90])


def test_determinism_same_input_same_output() -> None:
    """相同输入必产生相同输出（可复现约束）。"""
    curve = pl.DataFrame({
        "date": [date(2023, 1, i + 1) for i in range(5)],
        "value": [100.0, 102.0, 101.0, 105.0, 107.0],
        "invested": [100.0, 100.0, 100.0, 100.0, 100.0],
    })
    a = m.calculate_all(curve)
    b = m.calculate_all(curve)
    assert a == b
