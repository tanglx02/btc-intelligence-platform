"""平均真实波幅 ATR（Average True Range）。

对应指标字典 §5.5 ``tech.atr``。

公式
----
    TR  = max(High − Low, |High − Close_prev|, |Low − Close_prev|)
    ATR = Wilder 平滑(TR, N)
    ATR% = ATR / Close × 100

使用 Wilder 平滑（首值为 N 期简单平均，其后递推），默认 N = 14。
输出两列：value（ATR，USD）与 atr_percent（ATR%）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series, wilder_smooth

__all__ = ["ATRIndicator", "true_range", "atr"]


def true_range(high: pl.Series, low: pl.Series, close: pl.Series) -> pl.Series:
    """计算真实波幅 TR 序列。

    ``TR = max(H−L, |H−Close_prev|, |L−Close_prev|)``。首行无前收盘，
    退化为 ``H − L``。需要 high / low / close 等长。
    """
    h = to_float_series(high, "high").to_numpy().astype("float64")
    lo = to_float_series(low, "low").to_numpy().astype("float64")
    c = to_float_series(close, "close").to_numpy().astype("float64")
    n = h.size
    prev_close = np.empty(n, dtype="float64")
    prev_close[0] = np.nan
    if n > 1:
        prev_close[1:] = c[:-1]

    hl = h - lo
    with np.errstate(invalid="ignore"):
        hpc = np.abs(h - prev_close)
        lpc = np.abs(lo - prev_close)
        tr = np.nanmax(np.vstack([hl, hpc, lpc]), axis=0)
        # 首行 prev_close 为 NaN，nanmax 会忽略它，回落到 H-L
    return pl.Series("tr", tr, dtype=pl.Float64)


class ATRIndicator(IndicatorBase):
    """平均真实波幅，``tech.atr``。"""

    @property
    def name(self) -> str:
        return "tech.atr"

    @property
    def display_name(self) -> str:
        return "平均真实波幅"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["high", "low", "close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"period": 14}

    @property
    def min_rows(self) -> int:
        return 2

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 ATR 与 ATR%。

        Parameters
        ----------
        data:
            含 ``high`` / ``low`` / ``close`` 与时间列的 DataFrame。
        **params:
            ``period``：Wilder 平滑周期 N，默认 14。
        """
        p = self._resolve_params(params)
        period = int(p["period"])
        if period <= 0:
            raise ValueError("period 必须为正整数")

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "atr_percent": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
        else:
            tr = true_range(data["high"], data["low"], data["close"])
            atr_series = wilder_smooth(tr, period).alias("value")
            close = to_float_series(data["close"], "close")
            close_np = close.to_numpy().astype("float64")
            atr_np = atr_series.to_numpy().astype("float64")
            with np.errstate(divide="ignore", invalid="ignore"):
                pct = np.where(close_np > 0, atr_np / close_np * 100.0, np.nan)
            frame = pl.DataFrame(
                {
                    "value": atr_series,
                    "atr_percent": pl.Series("atr_percent", pct, dtype=pl.Float64),
                }
            )

        return self._build_result(time_series, frame, params={"period": period})


def atr(data: pl.DataFrame, period: int = 14) -> pl.Series:
    """便捷函数：返回 ATR 序列（USD）。"""
    return ATRIndicator().calculate(data, period=period).values["value"]
