"""数学工具函数单元测试（app.indicators.utils.math_helpers）。"""

from __future__ import annotations

import numpy as np
import polars as pl

from app.indicators.utils.math_helpers import (
    annualize,
    expanding_percentile_rank,
    log_returns,
    percentile_rank,
    rolling_correlation,
    rolling_mean,
    rolling_percentile_rank,
    rolling_std,
    safe_divide,
    simple_returns,
    to_float_series,
    wilder_smooth,
)


def test_to_float_series_casts_and_names():
    s = to_float_series([1, 2, 3], name="x")
    assert s.dtype == pl.Float64
    assert s.name == "x"
    assert s.to_list() == [1.0, 2.0, 3.0]


def test_rolling_mean_matches_numpy():
    arr = np.arange(1, 11, dtype=float)
    got = rolling_mean(pl.Series(arr), period=3).to_numpy()
    # 前 2 个为 null
    assert np.isnan(got[0]) and np.isnan(got[1])
    expected = np.convolve(arr, np.ones(3) / 3, mode="valid")
    assert np.allclose(got[2:], expected)


def test_rolling_std_ddof():
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    got = rolling_std(pl.Series(arr), period=3, ddof=1).to_numpy()
    # 手工：std([1,2,3], ddof=1)=1.0
    assert np.isclose(got[2], 1.0)
    assert np.isclose(got[4], np.std([3, 4, 5], ddof=1))


def test_wilder_smooth_constant_input():
    """常量输入经 Wilder 平滑仍为该常量（种子=均值，递推不变）。"""
    s = pl.Series([5.0] * 20)
    out = wilder_smooth(s, period=5).to_numpy()
    # 前 period-1 个为 null
    assert np.isnan(out[:4]).all()
    assert np.allclose(out[4:], 5.0)


def test_wilder_smooth_seed_is_sma():
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    out = wilder_smooth(pl.Series(arr), period=3).to_numpy()
    # 种子（index 2）= SMA(1,2,3)=2.0
    assert np.isclose(out[2], 2.0)
    # 递推：next = prev + (x - prev)/period
    expected_3 = out[2] + (arr[3] - out[2]) / 3
    assert np.isclose(out[3], expected_3)


def test_log_returns_formula():
    prices = pl.Series([100.0, 110.0, 105.0])
    got = log_returns(prices).to_numpy()
    assert np.isnan(got[0])
    assert np.isclose(got[1], np.log(110 / 100))
    assert np.isclose(got[2], np.log(105 / 110))


def test_log_returns_nonpositive_is_null():
    got = log_returns(pl.Series([100.0, 0.0, -5.0, 100.0])).to_numpy()
    assert np.isnan(got[1]) and np.isnan(got[2])


def test_simple_returns_formula():
    got = simple_returns(pl.Series([100.0, 110.0])).to_numpy()
    assert np.isnan(got[0])
    assert np.isclose(got[1], 0.1)


def test_annualize_sqrt_scaling():
    got = annualize(pl.Series([0.02, 0.04]), periods_per_year=365).to_numpy()
    assert np.isclose(got[0], 0.02 * np.sqrt(365))
    assert np.isclose(got[1], 0.04 * np.sqrt(365))


def test_percentile_rank_strict_less_than():
    # 样本 [1,2,3,4]，当前值 3：严格小于 3 的有 {1,2} => 2/4*100=50
    assert percentile_rank([1, 2, 3, 4], current=3) == 50.0
    assert percentile_rank([1, 2, 3, 4], current=5) == 100.0
    assert percentile_rank([1, 2, 3, 4], current=0) == 0.0


def test_rolling_percentile_rank_excludes_self():
    arr = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    got = rolling_percentile_rank(pl.Series(arr), window=3).to_numpy()
    # 单调上升，窗口内历史均小于当前 => 100
    valid = got[~np.isnan(got)]
    assert np.allclose(valid, 100.0)


def test_expanding_percentile_rank_monotonic():
    arr = np.linspace(1, 50, 50)
    got = expanding_percentile_rank(pl.Series(arr), min_samples=2).to_numpy()
    valid = got[~np.isnan(got)]
    # 严格递增 => 每个点相对全部历史都是最高 => 100
    assert np.allclose(valid, 100.0)


def test_rolling_correlation_perfect_positive():
    x = pl.Series(np.arange(20, dtype=float))
    y = pl.Series(np.arange(20, dtype=float) * 2 + 3)
    got = rolling_correlation(x, y, window=10).to_numpy()
    valid = got[~np.isnan(got)]
    assert np.allclose(valid, 1.0, atol=1e-8)


def test_rolling_correlation_perfect_negative():
    x = pl.Series(np.arange(20, dtype=float))
    y = pl.Series(-np.arange(20, dtype=float))
    got = rolling_correlation(x, y, window=10).to_numpy()
    valid = got[~np.isnan(got)]
    assert np.allclose(valid, -1.0, atol=1e-8)


def test_safe_divide_scalar_zero():
    assert safe_divide(1.0, 0.0, default=99.0) == 99.0
    assert safe_divide(6.0, 3.0) == 2.0


def test_safe_divide_series_zero_and_nan():
    a = pl.Series([6.0, 1.0, 2.0])
    b = pl.Series([3.0, 0.0, np.nan])
    got = safe_divide(a, b, default=-1.0)
    arr = np.asarray(got, dtype=float)
    assert np.isclose(arr[0], 2.0)
    assert arr[1] == -1.0  # 分母 0
    assert arr[2] == -1.0  # 分母 NaN


def test_safe_divide_inf_denominator():
    assert safe_divide(1.0, float("inf"), default=7.0) == 7.0
