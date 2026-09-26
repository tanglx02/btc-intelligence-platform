"""指数平滑异同移动平均线 MACD。

对应指标字典 §5.3 ``tech.macd``。

公式
----
    DIF (macd_line)   = EMA(Close, fast) − EMA(Close, slow)
    DEA (signal_line) = EMA(DIF, signal)
    Histogram         = DIF − DEA

默认参数 (fast=12, slow=26, signal=9)。输出三列：macd_line / signal_line / histogram。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series

__all__ = ["MACDIndicator", "macd"]


def _ema(series: pl.Series, span: int, min_periods: int | None = None) -> pl.Series:
    """标准 EMA（alpha = 2/(span+1)，adjust=False 递推）。"""
    return series.ewm_mean(
        span=span,
        adjust=False,
        ignore_nulls=True,
        min_samples=min_periods if min_periods is not None else span,
    )


class MACDIndicator(IndicatorBase):
    """MACD 指标，``tech.macd``。"""

    @property
    def name(self) -> str:
        return "tech.macd"

    @property
    def display_name(self) -> str:
        return "指数平滑异同移动平均线"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"fast": 12, "slow": 26, "signal": 9}

    @property
    def min_rows(self) -> int:
        return 2

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 MACD。

        Parameters
        ----------
        data:
            含 ``close`` 与时间列的 DataFrame。
        **params:
            ``fast`` / ``slow`` / ``signal``：快线、慢线、信号线周期。
        """
        p = self._resolve_params(params)
        fast = int(p["fast"])
        slow = int(p["slow"])
        signal = int(p["signal"])
        if min(fast, slow, signal) <= 0:
            raise ValueError("fast/slow/signal 必须为正整数")

        time_series = self._extract_time(data)
        n = data.height
        if "close" not in data.columns or n < 2:
            frame = pl.DataFrame(
                {
                    "macd_line": pl.Series([None] * n, dtype=pl.Float64),
                    "signal_line": pl.Series([None] * n, dtype=pl.Float64),
                    "histogram": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
        else:
            close = to_float_series(data["close"], "close")
            ema_fast = _ema(close, fast, min_periods=fast)
            ema_slow = _ema(close, slow, min_periods=slow)
            macd_line = (ema_fast - ema_slow).alias("macd_line")
            signal_line = _ema(macd_line, signal, min_periods=signal).alias("signal_line")
            histogram = (macd_line - signal_line).alias("histogram")
            frame = pl.DataFrame(
                {"macd_line": macd_line, "signal_line": signal_line, "histogram": histogram}
            )

        return self._build_result(
            time_series,
            frame,
            params={"fast": fast, "slow": slow, "signal": signal},
        )


def macd(
    data: pl.DataFrame | pl.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    column: str = "close",
) -> pl.DataFrame:
    """便捷函数：返回含 macd_line / signal_line / histogram 三列的 DataFrame。"""
    if isinstance(data, pl.DataFrame):
        df = data
    else:
        df = pl.DataFrame({column: data})
    return MACDIndicator().calculate(df, fast=fast, slow=slow, signal=signal).values
