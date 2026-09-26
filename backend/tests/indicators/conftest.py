"""指标引擎测试共享 fixtures。

提供确定性的 OHLCV 与链上/衍生品输入数据，供各测试模块复用。
所有随机数据均使用固定 seed，保证可复现。
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest


def _daily_time(n: int, start: tuple[int, int, int] = (2023, 1, 1)) -> pl.Series:
    return pl.datetime_range(
        pl.datetime(*start),
        pl.datetime(*start) + pl.duration(days=n - 1),
        "1d",
        eager=True,
    ).alias("time")


@pytest.fixture
def ohlcv() -> pl.DataFrame:
    """200 行带上升趋势的日线 OHLCV（确定性）。"""
    n = 200
    rng = np.random.default_rng(42)
    base = np.linspace(20000.0, 40000.0, n)
    wiggle = rng.normal(0, 150, n).cumsum() * 0.02
    close = base + wiggle
    high = close + np.abs(rng.normal(0, 120, n))
    low = close - np.abs(rng.normal(0, 120, n))
    volume = np.abs(rng.normal(1200, 300, n)) + 100
    return pl.DataFrame(
        {
            "time": _daily_time(n),
            "open": close + rng.normal(0, 80, n),
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


@pytest.fixture
def rising_series() -> pl.DataFrame:
    """严格单调上涨序列（RSI 应=100，percentile 应=100）。"""
    n = 60
    close = np.linspace(100.0, 200.0, n)
    return pl.DataFrame(
        {
            "time": _daily_time(n),
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "volume": np.full(n, 1000.0),
        }
    )


@pytest.fixture
def falling_series() -> pl.DataFrame:
    """严格单调下跌序列（RSI 应=0）。"""
    n = 60
    close = np.linspace(200.0, 100.0, n)
    return pl.DataFrame(
        {
            "time": _daily_time(n),
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "volume": np.full(n, 1000.0),
        }
    )


@pytest.fixture
def full_data(ohlcv: pl.DataFrame) -> pl.DataFrame:
    """在 OHLCV 基础上追加链上 / 衍生品 / 宏观列（供全指标计算）。"""
    n = ohlcv.height
    close = ohlcv["close"]
    rng = np.random.default_rng(7)
    return ohlcv.with_columns(
        [
            # 链上
            (close * 1.9e7).alias("market_cap"),
            (close * 1.3e7).alias("realized_cap"),
            (close * 1.9e7 - close * 1.3e7).alias("realized_profit"),
            (close * 1.9e7).alias("realized_value"),
            pl.Series("supply", np.full(n, 1.9e7)),
            pl.Series("miner_revenue_usd", np.linspace(3e8, 9e8, n)),
            pl.Series("hodl_bank", np.linspace(1e5, 4e5, n)),
            pl.Series("rhodl_ratio", np.linspace(0.4, 1.2, n)),
            pl.Series("sopr", np.linspace(0.98, 1.06, n)),
            pl.Series("asopr", np.linspace(0.985, 1.055, n)),
            pl.Series("lth_sopr", np.linspace(0.99, 1.05, n)),
            # 衍生品
            pl.Series("funding_rate", np.linspace(0.0001, 0.0005, n)),
            pl.Series("open_interest_usd", np.linspace(5e9, 1.2e10, n)),
            pl.Series("open_interest", np.linspace(2.5e5, 4e5, n)),
            pl.Series("mark_price", close.to_numpy()),
            pl.Series("taker_buy_volume", np.linspace(2e4, 5e4, n)),
            pl.Series("taker_sell_volume", np.linspace(1.8e4, 5.2e4, n)),
            pl.Series("noise", rng.normal(0, 1, n)),
        ]
    )
