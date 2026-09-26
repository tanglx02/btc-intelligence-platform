"""历史波动率 Historical Volatility。

对应指标字典 §5.9 ``tech.hv``。

公式
----
    r_t   = ln(Close_t / Close_{t-1})          # 对数收益率
    HV(N) = std(r, N) × √periods_per_year

加密货币全年交易，年化因子 ``periods_per_year`` 默认取 365。
参数 N ∈ {30, 60, 90}，默认 30。

输出两列：value（年化波动率，小数形式，如 0.82 表示 82%）与 hv_percent（百分比形式）。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import (
    TRADING_DAYS_PER_YEAR,
    annualize,
    log_returns,
    rolling_std,
    to_float_series,
)

__all__ = ["HistoricalVolatilityIndicator", "historical_volatility"]


class HistoricalVolatilityIndicator(IndicatorBase):
    """历史波动率，``tech.hv``。"""

    @property
    def name(self) -> str:
        return "tech.hv"

    @property
    def display_name(self) -> str:
        return "历史波动率"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"period": 30, "periods_per_year": TRADING_DAYS_PER_YEAR}

    @property
    def min_rows(self) -> int:
        return 2

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算年化历史波动率。

        Parameters
        ----------
        data:
            含 ``close`` 与时间列的 DataFrame。
        **params:
            ``period``：滚动窗口 N，默认 30；
            ``periods_per_year``：年化因子，默认 365。
        """
        p = self._resolve_params(params)
        period = int(p["period"])
        ppy = int(p["periods_per_year"])
        if period <= 0:
            raise ValueError("period 必须为正整数")

        time_series = self._extract_time(data)
        n = data.height
        if "close" not in data.columns or n < 2:
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "hv_percent": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
        else:
            close = to_float_series(data["close"], "close")
            returns = log_returns(close)
            # 对数收益率滚动标准差（样本标准差 ddof=1）年化
            vol = annualize(rolling_std(returns, period, ddof=1, min_periods=period), ppy)
            hv_percent = (vol * 100.0).alias("hv_percent")
            frame = pl.DataFrame({"value": vol.alias("value"), "hv_percent": hv_percent})

        return self._build_result(
            time_series,
            frame,
            params={"period": period, "periods_per_year": ppy},
        )


def historical_volatility(
    data: pl.DataFrame | pl.Series,
    period: int = 30,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    column: str = "close",
) -> pl.Series:
    """便捷函数：返回年化历史波动率序列（小数形式）。"""
    if isinstance(data, pl.DataFrame):
        df = data
    else:
        df = pl.DataFrame({column: data})
    return HistoricalVolatilityIndicator().calculate(
        df, period=period, periods_per_year=periods_per_year
    ).values["value"]
