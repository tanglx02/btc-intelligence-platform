"""布林带 Bollinger Bands。

对应指标字典 §5.6 ``tech.bollinger``。

公式
----
    Mid       = SMA(Close, N)
    Upper     = Mid + k × σ(Close, N)
    Lower     = Mid − k × σ(Close, N)
    %B        = (Close − Lower) / (Upper − Lower)
    Bandwidth = (Upper − Lower) / Mid

σ 为样本标准差（ddof=1）。默认 (N=20, k=2)。
输出五列：upper / middle / lower / bandwidth / percent_b（value 别名指向 middle）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_mean, rolling_std, to_float_series

__all__ = ["BollingerBandsIndicator", "bollinger_bands"]


class BollingerBandsIndicator(IndicatorBase):
    """布林带，``tech.bollinger``。"""

    @property
    def name(self) -> str:
        return "tech.bollinger"

    @property
    def display_name(self) -> str:
        return "布林带"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"period": 20, "std_dev": 2.0}

    @property
    def min_rows(self) -> int:
        return 1

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算布林带。

        Parameters
        ----------
        data:
            含 ``close`` 与时间列的 DataFrame。
        **params:
            ``period``：中轨 SMA 窗口 N，默认 20；
            ``std_dev``：标准差倍数 k，默认 2。
        """
        p = self._resolve_params(params)
        period = int(p["period"])
        k = float(p["std_dev"])
        if period <= 0:
            raise ValueError("period 必须为正整数")

        time_series = self._extract_time(data)
        n = data.height
        if "close" not in data.columns or n == 0:
            frame = self._empty_frame(n)
        else:
            close = to_float_series(data["close"], "close")
            close_np = close.to_numpy().astype("float64")

            middle = rolling_mean(close, period, min_periods=period)
            std = rolling_std(close, period, ddof=1, min_periods=period)
            upper = (middle + k * std).alias("upper")
            lower = (middle - k * std).alias("lower")

            mid_np = middle.to_numpy().astype("float64")
            up_np = upper.to_numpy().astype("float64")
            lo_np = lower.to_numpy().astype("float64")
            width = up_np - lo_np

            with np.errstate(divide="ignore", invalid="ignore"):
                bandwidth = np.where(mid_np != 0, width / mid_np, np.nan)
                percent_b = np.where(
                    width > 0, (close_np - lo_np) / np.where(width > 0, width, np.nan), np.nan
                )

            frame = pl.DataFrame(
                {
                    "upper": upper,
                    "middle": middle.alias("middle"),
                    "lower": lower,
                    "bandwidth": pl.Series("bandwidth", bandwidth, dtype=pl.Float64),
                    "percent_b": pl.Series("percent_b", percent_b, dtype=pl.Float64),
                    # value 别名指向中轨，便于 IndicatorResult.primary_column 统一取值
                    "value": middle.alias("value"),
                }
            )

        return self._build_result(
            time_series,
            frame,
            params={"period": period, "std_dev": k},
        )

    @staticmethod
    def _empty_frame(n: int) -> pl.DataFrame:
        cols = ["upper", "middle", "lower", "bandwidth", "percent_b", "value"]
        return pl.DataFrame({c: pl.Series([None] * n, dtype=pl.Float64) for c in cols})


def bollinger_bands(
    data: pl.DataFrame | pl.Series, period: int = 20, std_dev: float = 2.0, column: str = "close"
) -> pl.DataFrame:
    """便捷函数：返回布林带多列 DataFrame。"""
    if isinstance(data, pl.DataFrame):
        df = data
    else:
        df = pl.DataFrame({column: data})
    return BollingerBandsIndicator().calculate(df, period=period, std_dev=std_dev).values
