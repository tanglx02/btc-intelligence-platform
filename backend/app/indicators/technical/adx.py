"""平均趋向指数 ADX（Average Directional Index）。

对应指标字典 §5.4 ``tech.adx``。

公式
----
    +DM = UpMove   if (UpMove > DownMove and UpMove > 0) else 0
    −DM = DownMove if (DownMove > UpMove and DownMove > 0) else 0
    UpMove   = High_t − High_{t-1}
    DownMove = Low_{t-1} − Low_t
    TR       = max(H−L, |H−Close_prev|, |L−Close_prev|)
    +DI      = 100 × Wilder(+DM, N) / Wilder(TR, N)
    −DI      = 100 × Wilder(−DM, N) / Wilder(TR, N)
    DX       = 100 × |+DI − −DI| / (+DI + −DI)
    ADX      = Wilder(DX, N)

默认 N = 14，需要 high / low / close。输出三列：adx / plus_di / minus_di。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series, wilder_smooth
from .atr import true_range

__all__ = ["ADXIndicator", "adx"]

_NULL_ONE = pl.Series([None], dtype=pl.Float64)


def _align_after_diff(smoothed: pl.Series) -> pl.Series:
    """在平滑结果前补 null，使其与含 diff 首行的原始序列对齐。"""
    return pl.concat([_NULL_ONE, smoothed])


class ADXIndicator(IndicatorBase):
    """平均趋向指数，``tech.adx``。"""

    @property
    def name(self) -> str:
        return "tech.adx"

    @property
    def display_name(self) -> str:
        return "平均趋向指数"

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
        """计算 ADX / +DI / −DI。

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
            frame = self._empty_frame(n)
        else:
            high = to_float_series(data["high"], "high")
            low = to_float_series(data["low"], "low")

            up_move = high.diff()
            down_move = -low.diff()  # = low_prev - low

            up_np = up_move.to_numpy().astype("float64")
            down_np = down_move.to_numpy().astype("float64")
            plus_dm = np.where((up_np > down_np) & (up_np > 0), up_np, 0.0)
            minus_dm = np.where((down_np > up_np) & (down_np > 0), down_np, 0.0)
            plus_dm[0] = np.nan  # diff 首行无效
            minus_dm[0] = np.nan

            plus_dm_s = pl.Series("plus_dm", plus_dm, dtype=pl.Float64)
            minus_dm_s = pl.Series("minus_dm", minus_dm, dtype=pl.Float64)

            tr = true_range(high, low, to_float_series(data["close"], "close"))
            atr_s = wilder_smooth(tr, period)

            # DM 首行为 null，tail(1) 后平滑再对齐
            plus_dm_smooth = _align_after_diff(wilder_smooth(plus_dm_s.tail(n - 1), period))
            minus_dm_smooth = _align_after_diff(wilder_smooth(minus_dm_s.tail(n - 1), period))

            atr_np = atr_s.to_numpy().astype("float64")
            pdi_np = plus_dm_smooth.to_numpy().astype("float64")
            mdi_np = minus_dm_smooth.to_numpy().astype("float64")

            with np.errstate(divide="ignore", invalid="ignore"):
                plus_di = np.where(atr_np > 0, 100.0 * pdi_np / atr_np, np.nan)
                minus_di = np.where(atr_np > 0, 100.0 * mdi_np / atr_np, np.nan)
                di_sum = plus_di + minus_di
                dx = np.where(
                    di_sum > 0,
                    100.0 * np.abs(plus_di - minus_di) / np.where(di_sum > 0, di_sum, np.nan),
                    np.nan,
                )

            dx_s = pl.Series("dx", dx, dtype=pl.Float64)
            # DX 前若干行为 null，wilder_smooth 会以首个非 null 段作为种子；
            # 为保证标准 ADX 口径，从首个有效 DX 起做 Wilder 平滑再回填对齐。
            adx_np = self._smooth_dx(dx, period)

            frame = pl.DataFrame(
                {
                    "adx": pl.Series("adx", adx_np, dtype=pl.Float64),
                    "plus_di": pl.Series("plus_di", plus_di, dtype=pl.Float64),
                    "minus_di": pl.Series("minus_di", minus_di, dtype=pl.Float64),
                }
            )
            _ = dx_s  # 保留中间量便于调试

        return self._build_result(time_series, frame, params={"period": period})

    @staticmethod
    def _empty_frame(n: int) -> pl.DataFrame:
        return pl.DataFrame(
            {
                "adx": pl.Series([None] * n, dtype=pl.Float64),
                "plus_di": pl.Series([None] * n, dtype=pl.Float64),
                "minus_di": pl.Series([None] * n, dtype=pl.Float64),
            }
        )

    @staticmethod
    def _smooth_dx(dx: np.ndarray, period: int) -> np.ndarray:
        """对 DX 序列（含前导 NaN）做 Wilder 平滑，返回与输入对齐的 ADX。

        以首个有效 DX 起的 ``period`` 个值求简单平均作为种子，其后递推。
        有效样本不足 ``period`` 时返回全 NaN。
        """
        n = dx.size
        out = np.full(n, np.nan, dtype="float64")
        valid_idx = np.where(np.isfinite(dx))[0]
        if valid_idx.size < period:
            return out
        start = valid_idx[0]
        seed_end = start + period
        if seed_end > n:
            return out
        seed_vals = dx[start:seed_end]
        if not np.all(np.isfinite(seed_vals)):
            return out
        prev = float(np.mean(seed_vals))
        out[seed_end - 1] = prev
        alpha = 1.0 / period
        for i in range(seed_end, n):
            if np.isfinite(dx[i]):
                prev = prev * (1 - alpha) + dx[i] * alpha
            out[i] = prev
        return out


def adx(data: pl.DataFrame, period: int = 14) -> pl.DataFrame:
    """便捷函数：返回含 adx / plus_di / minus_di 三列的 DataFrame。"""
    return ADXIndicator().calculate(data, period=period).values
