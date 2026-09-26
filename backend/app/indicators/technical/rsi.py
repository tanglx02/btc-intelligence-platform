"""相对强弱指数 RSI（Relative Strength Index）。

对应指标字典 §5.2 ``tech.rsi``。

公式
----
    RSI = 100 − 100 / (1 + RS)，RS = AvgGain(N) / AvgLoss(N)

AvgGain / AvgLoss 采用 **Wilder 平滑**（非简单平均）：首值为 N 期简单平均，
其后 ``Avg_t = (Avg_{t-1} × (N−1) + X_t) / N``。

输出范围 0–100。参数 N ∈ {14, 21}，默认 14。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series, wilder_smooth

__all__ = ["RSIIndicator", "rsi"]

# 单元素 null 序列，用于对齐 diff 产生的首行
_NULL_ONE = pl.Series([None], dtype=pl.Float64)


def _align_after_diff(smoothed: pl.Series) -> pl.Series:
    """在 Wilder 平滑结果前补一个 null，使其与原始序列（含 diff 首行）对齐。"""
    return pl.concat([_NULL_ONE, smoothed])


class RSIIndicator(IndicatorBase):
    """相对强弱指数，``tech.rsi``。"""

    @property
    def name(self) -> str:
        return "tech.rsi"

    @property
    def display_name(self) -> str:
        return "相对强弱指数"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["close"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"period": 14}

    @property
    def min_rows(self) -> int:
        return 2

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 RSI。

        Parameters
        ----------
        data:
            含 ``close`` 与时间列的 DataFrame。
        **params:
            ``period``：Wilder 平滑周期 N，默认 14。
        """
        p = self._resolve_params(params)
        period = int(p["period"])
        if period <= 0:
            raise ValueError("period 必须为正整数")

        time_series = self._extract_time(data)
        n = data.height
        if "close" not in data.columns or n < 2:
            values = pl.Series("value", [None] * n, dtype=pl.Float64)
        else:
            close = to_float_series(data["close"], "close")
            values = self._rsi(close, period).alias("value")

        value_frame = pl.DataFrame({"value": values})
        return self._build_result(time_series, value_frame, params={"period": period})

    @staticmethod
    def _rsi(close: pl.Series, period: int) -> pl.Series:
        """核心 RSI 计算（Wilder 平滑）。"""
        n = close.len()
        delta = close.diff()  # 首行为 null

        # 上涨幅度 / 下跌幅度（下跌取正值）
        gain = delta.clip(lower_bound=0.0)
        loss = (-delta).clip(lower_bound=0.0)

        # 丢掉 diff 产生的首行 null 后做 Wilder 平滑，再补回 null 对齐
        avg_gain = _align_after_diff(wilder_smooth(gain.tail(n - 1), period))
        avg_loss = _align_after_diff(wilder_smooth(loss.tail(n - 1), period))

        gain_np = avg_gain.to_numpy().astype("float64")
        loss_np = avg_loss.to_numpy().astype("float64")
        denom = gain_np + loss_np
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(
                denom > 0,
                100.0 * gain_np / np.where(denom > 0, denom, np.nan),
                np.nan,
            )
            # 完全无波动（denom==0）时，若已有平滑值则视为中性偏强 100（无亏损）
            no_move = (denom == 0) & np.isfinite(gain_np) & np.isfinite(loss_np)
            out = np.where(no_move, 100.0, out)
            # 消除浮点误差导致的越界（如 100.00000000000001），NaN 会被保留
            out = np.clip(out, 0.0, 100.0)
        return pl.Series(close.name, out, dtype=pl.Float64)


def rsi(data: pl.DataFrame | pl.Series, period: int = 14, column: str = "close") -> pl.Series:
    """便捷函数：计算 RSI 序列。"""
    series = data[column] if isinstance(data, pl.DataFrame) else data
    return RSIIndicator._rsi(to_float_series(series, "close"), period).alias("rsi")  # noqa: SLF001
