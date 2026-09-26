"""累计成交量差 CVD（Cumulative Volume Delta）。

对应指标字典 §7.7 ``deriv.cvd``。

公式
----
    Volume Delta(t) = Taker Buy(t) − Taker Sell(t)
    CVD             = Σ Volume Delta（自锚定点累计）

锚定周期 ∈ {day, week, month, none}：``none`` 表示全序列累积（不重置）；
其余按对应周期在边界重置（默认每日 UTC 00:00 重置）。

数据来源候选列：``taker_buy_volume`` / ``taker_sell_volume``（主动买卖量），
或直接提供 ``volume_delta`` 列。CVD 与价格背离（价格涨但 CVD 跌）是重要警示信号。

输出列：value（CVD 累计值）、volume_delta（单期净主动量）。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import TIME_COLUMN, IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series

__all__ = ["CVDIndicator", "cvd"]

_TAKER_BUY = ("taker_buy_volume", "taker_buy", "buy_volume")
_TAKER_SELL = ("taker_sell_volume", "taker_sell", "sell_volume")
_DELTA_DIRECT = ("volume_delta", "delta")

_ANCHOR_TRUNC = {"day": "1d", "session": "1d", "week": "1w", "month": "1mo"}


class CVDIndicator(IndicatorBase):
    """累计成交量差，``deriv.cvd``。"""

    @property
    def name(self) -> str:
        return "deriv.cvd"

    @property
    def display_name(self) -> str:
        return "累计成交量差"

    @property
    def category(self) -> str:
        return IndicatorCategory.DERIVATIVES

    @property
    def required_data(self) -> list[str]:
        return ["taker_buy_volume", "taker_sell_volume"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"anchor": "none"}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        if any(c in data.columns for c in _DELTA_DIRECT):
            return True
        return any(c in data.columns for c in _TAKER_BUY) and any(
            c in data.columns for c in _TAKER_SELL
        )

    def _resolve_delta(self, data: pl.DataFrame) -> pl.Series:
        """解析单期 Volume Delta：优先直接列，其次 buy − sell。"""
        direct_name, direct = self._first_column(data, list(_DELTA_DIRECT))
        if direct_name is not None:
            return direct
        buy = self._first_column(data, list(_TAKER_BUY))[1]
        sell = self._first_column(data, list(_TAKER_SELL))[1]
        return (buy - sell).alias("volume_delta")

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 CVD。

        Parameters
        ----------
        data:
            含 taker 买卖量（或 volume_delta）与时间列的 DataFrame。
        **params:
            ``anchor``：锚定周期 ∈ {none, day/session, week, month}，默认 ``none``（全序列累积）。
        """
        p = self._resolve_params(params)
        anchor = str(p.get("anchor", "none")).lower()

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "volume_delta": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params={"anchor": anchor})

        delta = to_float_series(self._resolve_delta(data), "volume_delta")

        work = pl.DataFrame({TIME_COLUMN: time_series, "volume_delta": delta})
        is_temporal = work[TIME_COLUMN].dtype in (pl.Datetime, pl.Date)
        if is_temporal:
            work = work.sort(TIME_COLUMN)

        trunc = _ANCHOR_TRUNC.get(anchor)
        if is_temporal and trunc is not None:
            group = (
                work[TIME_COLUMN].dt.truncate(trunc)
                if work[TIME_COLUMN].dtype == pl.Datetime
                else work[TIME_COLUMN]
            )
            work = work.with_columns(group.alias("_anchor"))
        else:
            work = work.with_columns(pl.lit(0).alias("_anchor"))

        result = work.with_columns(
            (pl.col("volume_delta").cum_sum().over("_anchor")).alias("_cvd")
        )
        cvd_np = result["_cvd"].to_numpy().astype("float64")
        delta_np = result["volume_delta"].to_numpy().astype("float64")

        frame = pl.DataFrame(
            {
                "value": pl.Series("value", cvd_np, dtype=pl.Float64),
                "volume_delta": pl.Series("volume_delta", delta_np, dtype=pl.Float64),
            }
        )
        return self._build_result(
            result[TIME_COLUMN],
            frame,
            params={"anchor": anchor},
            extra_metadata={"sorted_by_time": is_temporal},
        )


def cvd(data: pl.DataFrame, anchor: str = "none") -> pl.Series:
    """便捷函数：返回 CVD 累计序列。"""
    return CVDIndicator().calculate(data, anchor=anchor).values["value"]
