"""ATH（历史最高价）相关指标。

对应指标字典 §5.11（Drawdown / ATH 口径）。

包含三个指标：

- ``composite.ath``：历史最高价追踪（Point-in-Time 累积最大值）；
- ``composite.distance_from_ath``：当前价格距 ATH 的百分比（≤ 0）；
- ``composite.ath_breakout``：当期是否创出新的 ATH（1=突破 / 0=未突破）。

⚠️ **Point-in-Time 约束**：ATH 必须使用「截至 observation_time 的历史最高价」，
本实现采用累积最大值（``cum_max``），回测 2017 年时不会使用 2021 年的 ATH，
从根本上避免未来数据泄漏（14 号文档 §2 强制检测项）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series

__all__ = [
    "all_time_high",
    "resolve_price",
    "ATHIndicator",
    "DistanceFromATHIndicator",
    "ATHBreakoutIndicator",
]

_PRICE_PRIORITY = ("high", "close", "price")


def resolve_price(data: pl.DataFrame, column: str | None = None) -> pl.Series:
    """解析用于 ATH 计算的价格序列。

    优先级：显式 ``column`` > ``high`` > ``close`` > ``price``。
    ATH 通常采用最高价口径（含盘中新高）。
    """
    if column and column in data.columns:
        return to_float_series(data[column], column)
    for cand in _PRICE_PRIORITY:
        if cand in data.columns:
            return to_float_series(data[cand], cand)
    return pl.Series("price", [None] * data.height, dtype=pl.Float64)


def all_time_high(price: pl.Series) -> pl.Series:
    """Point-in-Time 历史最高价：累积最大值 ``cum_max``。"""
    return to_float_series(price).cum_max()


class _ATHBase(IndicatorBase):
    """ATH 家族公共基类。"""

    @property
    def category(self) -> str:
        return IndicatorCategory.COMPOSITE

    @property
    def required_data(self) -> list[str]:
        return ["high"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"price_column": None}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        return any(c in data.columns for c in _PRICE_PRIORITY)


class ATHIndicator(_ATHBase):
    """历史最高价追踪，``composite.ath``。"""

    @property
    def name(self) -> str:
        return "composite.ath"

    @property
    def display_name(self) -> str:
        return "历史最高价"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算累积历史最高价。"""
        p = self._resolve_params(params)
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(time_series, frame, params=p)

        price = resolve_price(data, p.get("price_column"))
        ath = all_time_high(price).alias("value")
        frame = pl.DataFrame({"value": ath})
        return self._build_result(time_series, frame, params=p)


class DistanceFromATHIndicator(_ATHBase):
    """距历史最高价百分比，``composite.distance_from_ath``。

    公式：``DistanceFromATH = Price / ATH − 1``（≤ 0，0 表示正处于 ATH）。
    输出 value 为小数形式，distance_percent 为百分比形式。
    """

    @property
    def name(self) -> str:
        return "composite.distance_from_ath"

    @property
    def display_name(self) -> str:
        return "距历史最高价百分比"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        p = self._resolve_params(params)
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "distance_percent": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params=p)

        price = resolve_price(data, p.get("price_column"))
        ath = all_time_high(price)
        price_np = price.to_numpy().astype("float64")
        ath_np = ath.to_numpy().astype("float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            dist = np.where(
                ath_np > 0, price_np / np.where(ath_np > 0, ath_np, np.nan) - 1.0, np.nan
            )

        frame = pl.DataFrame(
            {
                "value": pl.Series("value", dist, dtype=pl.Float64),
                "distance_percent": pl.Series("distance_percent", dist * 100.0, dtype=pl.Float64),
            }
        )
        return self._build_result(time_series, frame, params=p)


class ATHBreakoutIndicator(_ATHBase):
    """ATH 突破标记，``composite.ath_breakout``。

    当期价格 ≥ 上一期为止的历史最高价时标记为突破（1），否则 0。
    首个数据点视为突破（尚无历史）。
    """

    @property
    def name(self) -> str:
        return "composite.ath_breakout"

    @property
    def display_name(self) -> str:
        return "历史最高价突破"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        p = self._resolve_params(params)
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(time_series, frame, params=p)

        price = resolve_price(data, p.get("price_column"))
        price_np = price.to_numpy().astype("float64")
        ath_prev = np.empty(n, dtype="float64")
        ath_prev[0] = np.nan
        if n > 1:
            ath_prev[1:] = np.maximum.accumulate(price_np)[:-1]
        breakout = np.where(
            np.isnan(ath_prev), 1.0, np.where(price_np >= ath_prev, 1.0, 0.0)
        )
        breakout = np.where(np.isfinite(price_np), breakout, np.nan)

        frame = pl.DataFrame({"value": pl.Series("value", breakout, dtype=pl.Float64)})
        return self._build_result(time_series, frame, params=p)
