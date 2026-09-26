"""价格回撤指标（Drawdown / MaxDrawdown / DrawdownDuration）。

对应指标字典 §5.11 ``tech.drawdown``（此处归入 composite 复合指标）。

公式
----
    DD_ATH   = Close / ATH − 1                    # 距历史最高价回撤（Point-in-Time）
    DD_RH(N) = Close / max(Close, N 期) − 1        # 距滚动 N 期高点回撤
    MaxDrawdown = min(DD_ATH)（截至当期的历史最深回撤，累积最小值）
    DrawdownDuration = 距上一次创出 ATH 的周期数（当前回撤持续时间）

⚠️ ATH 采用 Point-in-Time 累积最大值，避免回测中的未来数据泄漏。

包含三个指标类：
``composite.drawdown`` / ``composite.max_drawdown`` / ``composite.drawdown_duration``。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series
from .ath import all_time_high

__all__ = [
    "drawdown_from_high",
    "DrawdownIndicator",
    "MaxDrawdownIndicator",
    "DrawdownDurationIndicator",
]

_PRICE_PRIORITY = ("close", "high", "price")


def _resolve_close(data: pl.DataFrame, column: str | None) -> pl.Series:
    """解析回撤计算所用价格序列（优先 close）。"""
    if column and column in data.columns:
        return to_float_series(data[column], column)
    for cand in _PRICE_PRIORITY:
        if cand in data.columns:
            return to_float_series(data[cand], cand)
    return pl.Series("price", [None] * data.height, dtype=pl.Float64)


def drawdown_from_high(price: pl.Series, high: pl.Series) -> pl.Series:
    """由价格与对应高点序列计算回撤（小数形式，≤ 0）。"""
    p = to_float_series(price).to_numpy().astype("float64")
    h = to_float_series(high).to_numpy().astype("float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(h > 0, p / np.where(h > 0, h, np.nan) - 1.0, np.nan)


class _DrawdownBase(IndicatorBase):
    """回撤家族公共基类。"""

    @property
    def category(self) -> str:
        return IndicatorCategory.COMPOSITE

    @property
    def required_data(self) -> list[str]:
        return ["close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"price_column": None, "rolling_window": 0}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        return any(c in data.columns for c in _PRICE_PRIORITY)


class DrawdownIndicator(_DrawdownBase):
    """价格回撤，``composite.drawdown``。"""

    @property
    def name(self) -> str:
        return "composite.drawdown"

    @property
    def display_name(self) -> str:
        return "价格回撤"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算距 ATH 回撤与距滚动高点回撤。

        Parameters
        ----------
        data:
            含 close（或 high/price）与时间列的 DataFrame。
        **params:
            ``price_column``：显式价格列；
            ``rolling_window``：滚动高点窗口 N，>0 时额外输出 ``dd_rolling`` 列。
        """
        p = self._resolve_params(params)
        rolling_window = int(p.get("rolling_window", 0) or 0)
        time_series = self._extract_time(data)
        n = data.height

        cols: dict[str, pl.Series] = {
            "value": pl.Series([None] * n, dtype=pl.Float64),
            "dd_percent": pl.Series([None] * n, dtype=pl.Float64),
        }
        if rolling_window > 0:
            cols["dd_rolling"] = pl.Series([None] * n, dtype=pl.Float64)

        if self.validate_input(data):
            price = _resolve_close(data, p.get("price_column"))
            ath = all_time_high(price)
            dd = drawdown_from_high(price, ath)
            cols["value"] = pl.Series("value", dd, dtype=pl.Float64)
            cols["dd_percent"] = pl.Series("dd_percent", dd * 100.0, dtype=pl.Float64)
            if rolling_window > 0:
                rh = price.rolling_max(window_size=rolling_window, min_samples=1)
                dd_r = drawdown_from_high(price, rh)
                cols["dd_rolling"] = pl.Series("dd_rolling", dd_r, dtype=pl.Float64)

        frame = pl.DataFrame(cols)
        return self._build_result(time_series, frame, params=p)


class MaxDrawdownIndicator(_DrawdownBase):
    """最大回撤（累积），``composite.max_drawdown``。

    ``value`` 为截至当期为止的历史最深回撤（累积最小值，Point-in-Time）；
    ``metadata['max_drawdown']`` 为整段样本的最大回撤。
    """

    @property
    def name(self) -> str:
        return "composite.max_drawdown"

    @property
    def display_name(self) -> str:
        return "最大回撤"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        p = self._resolve_params(params)
        time_series = self._extract_time(data)
        n = data.height

        max_dd = float("nan")
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(
                time_series, frame, params=p, extra_metadata={"max_drawdown": None}
            )

        price = _resolve_close(data, p.get("price_column"))
        ath = all_time_high(price)
        dd = drawdown_from_high(price, ath)
        dd_series = pl.Series("dd", dd, dtype=pl.Float64)
        running_max_dd = dd_series.cum_min().alias("value")  # 累积最小（最深回撤）
        finite = dd_series.drop_nulls()
        if finite.len() > 0:
            max_dd = float(finite.min())  # type: ignore[arg-type]

        frame = pl.DataFrame({"value": running_max_dd})
        return self._build_result(
            time_series, frame, params=p, extra_metadata={"max_drawdown": max_dd}
        )


class DrawdownDurationIndicator(_DrawdownBase):
    """回撤持续时间，``composite.drawdown_duration``。

    ``value`` 为距上一次创出 ATH（新高）所经过的周期数；创新高当期归零。
    用于衡量「已经回撤了多久」，长持续时间叠加深度回撤常对应熊市底部区域。
    """

    @property
    def name(self) -> str:
        return "composite.drawdown_duration"

    @property
    def display_name(self) -> str:
        return "回撤持续时间"

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        p = self._resolve_params(params)
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(time_series, frame, params=p)

        price = _resolve_close(data, p.get("price_column"))
        arr = price.to_numpy().astype("float64")
        duration = np.full(n, np.nan, dtype="float64")
        last_peak = -1
        running_max = -np.inf
        for i in range(n):
            val = arr[i]
            if not np.isfinite(val):
                continue
            if last_peak < 0 or val >= running_max:
                # 新高：回撤归零，刷新峰值
                running_max = max(running_max, val)
                last_peak = i
                duration[i] = 0.0
            else:
                duration[i] = float(i - last_peak)

        current = float(duration[-1]) if np.isfinite(duration[-1]) else None
        frame = pl.DataFrame({"value": pl.Series("value", duration, dtype=pl.Float64)})
        return self._build_result(
            time_series,
            frame,
            params=p,
            extra_metadata={"current_duration": current},
        )
