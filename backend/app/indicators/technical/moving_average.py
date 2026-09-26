"""移动平均线家族（SMA / EMA / WMA）。

对应指标字典 §5.1 ``tech.sma`` / ``tech.ema`` / ``tech.wma``。

公式
----
- SMA(N) = Σ(Close_i, i=1..N) / N
- EMA(N)：EMA_t = Close_t × k + EMA_{t-1} × (1 − k)，k = 2 / (N + 1)
- WMA(N) = Σ(Close_{N-i} × (N-i), i=0..N-1) / Σ(1..N)

支持周期 N ∈ {7, 25, 50, 99, 100, 200, 365}（日线默认全集；小时线默认 {25, 99, 200}）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_mean, to_float_series

__all__ = ["MovingAverageBase", "SMAIndicator", "EMAIndicator", "WMAIndicator", "sma", "ema", "wma"]

# 常用周期集合（见指标字典 §5.1）
COMMON_PERIODS: tuple[int, ...] = (7, 25, 50, 99, 100, 200, 365)


class MovingAverageBase(IndicatorBase):
    """移动平均指标公共基类。

    统一处理 ``close`` 列提取、周期参数与结果组装；子类只需实现 ``_ma``。
    """

    #: 计算所用价格列
    price_column: str = "close"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return [self.price_column]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"period": 50}

    def _ma(self, prices: pl.Series, period: int) -> pl.Series:
        """子类实现的具体移动平均算法。"""
        raise NotImplementedError

    @property
    def min_rows(self) -> int:
        return 1

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算移动平均线。

        Parameters
        ----------
        data:
            含 ``close``（或 :attr:`price_column`）与时间列的 DataFrame。
        **params:
            ``period``：窗口周期 N，默认 50。
        """
        p = self._resolve_params(params)
        period = int(p["period"])
        if period <= 0:
            raise ValueError("period 必须为正整数")

        time_series = self._extract_time(data)
        if self.price_column not in data.columns:
            values = pl.Series("value", [None] * data.height, dtype=pl.Float64)
        else:
            prices = to_float_series(data[self.price_column], name="close")
            values = self._ma(prices, period).alias("value")

        value_frame = pl.DataFrame({"value": values})
        return self._build_result(
            time_series,
            value_frame,
            params={"period": period},
            extra_metadata={"ma_type": self.name.split(".")[-1]},
        )


class SMAIndicator(MovingAverageBase):
    """简单移动平均（Simple Moving Average），``tech.sma``。"""

    @property
    def name(self) -> str:
        return "tech.sma"

    @property
    def display_name(self) -> str:
        return "简单移动平均线"

    def _ma(self, prices: pl.Series, period: int) -> pl.Series:
        return rolling_mean(prices, period, min_periods=period)


class EMAIndicator(MovingAverageBase):
    """指数移动平均（Exponential Moving Average），``tech.ema``。

    使用 Polars ``ewm_mean(span=period, adjust=False)`` 实现标准 EMA，
    平滑因子 k = 2 / (period + 1)，对近期价格赋予更高权重。
    """

    @property
    def name(self) -> str:
        return "tech.ema"

    @property
    def display_name(self) -> str:
        return "指数移动平均线"

    def _ma(self, prices: pl.Series, period: int) -> pl.Series:
        # span=N 等价于 alpha = 2/(N+1)；adjust=False 采用递推式 EMA
        return prices.ewm_mean(span=period, adjust=False, ignore_nulls=True, min_samples=period)


class WMAIndicator(MovingAverageBase):
    """加权移动平均（Weighted Moving Average），``tech.wma``。

    线性加权：窗口内最新样本权重为 N，最旧样本权重为 1，
    分母为 Σ(1..N) = N(N+1)/2。使用 numpy 卷积向量化计算。
    """

    @property
    def name(self) -> str:
        return "tech.wma"

    @property
    def display_name(self) -> str:
        return "加权移动平均线"

    def _ma(self, prices: pl.Series, period: int) -> pl.Series:
        arr = prices.to_numpy().astype("float64")
        n = arr.size
        out = np.full(n, np.nan, dtype="float64")
        if n < period or period <= 0:
            return pl.Series(prices.name, out, dtype=pl.Float64)

        weights = np.arange(1, period + 1, dtype="float64")  # 旧->新 权重 1..N
        denom = weights.sum()
        # convolve 'valid'[k] = Σ_j arr[k+j] * weights[j]，对应窗口 [k, k+period-1]
        conv = np.convolve(arr, weights[::-1], mode="valid")
        out[period - 1:] = conv / denom
        return pl.Series(prices.name, out, dtype=pl.Float64)


# --------------------------------------------------------------------------- #
# 便捷函数式接口（供其他模块 / 测试直接调用）
# --------------------------------------------------------------------------- #
def sma(data: pl.DataFrame | pl.Series, period: int = 50, column: str = "close") -> pl.Series:
    """计算简单移动平均。``data`` 可为 DataFrame 或价格 Series。"""
    series = data[column] if isinstance(data, pl.DataFrame) else data
    return rolling_mean(to_float_series(series, "close"), period, min_periods=period).alias("sma")


def ema(data: pl.DataFrame | pl.Series, period: int = 50, column: str = "close") -> pl.Series:
    """计算指数移动平均。"""
    series = data[column] if isinstance(data, pl.DataFrame) else data
    return (
        to_float_series(series, "close")
        .ewm_mean(span=period, adjust=False, ignore_nulls=True, min_samples=period)
        .alias("ema")
    )


def wma(data: pl.DataFrame | pl.Series, period: int = 50, column: str = "close") -> pl.Series:
    """计算加权移动平均。"""
    series = data[column] if isinstance(data, pl.DataFrame) else data
    return WMAIndicator()._ma(to_float_series(series, "close"), period).alias("wma")  # noqa: SLF001
