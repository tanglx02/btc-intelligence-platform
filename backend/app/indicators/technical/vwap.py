"""成交量加权平均价 VWAP（Volume Weighted Average Price）。

对应指标字典 §5.8 ``tech.vwap``。

公式
----
    TypicalPrice = (High + Low + Close) / 3
    VWAP         = Σ(TypicalPrice_i × Volume_i) / Σ(Volume_i)

按锚定周期重置累积：

- ``session`` / ``day``：日内 VWAP，每日 UTC 00:00 重置；
- ``week``：每周重置；
- ``month``：每月重置；
- ``none``：全序列累积（Anchored VWAP，锚定序列首行）。

需要 high / low / close / volume。输出列：value（VWAP）与 vwap_deviation（价格相对 VWAP 乖离率）。
加密货币 7×24 交易，日内以 UTC 00:00 为界。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import TIME_COLUMN, IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import to_float_series

__all__ = ["VWAPIndicator", "vwap"]

# 锚定周期到 Polars 截断粒度的映射
_ANCHOR_TRUNC = {
    "session": "1d",
    "day": "1d",
    "week": "1w",
    "month": "1mo",
}


class VWAPIndicator(IndicatorBase):
    """成交量加权平均价，``tech.vwap``。"""

    @property
    def name(self) -> str:
        return "tech.vwap"

    @property
    def display_name(self) -> str:
        return "成交量加权平均价"

    @property
    def category(self) -> str:
        return IndicatorCategory.TECHNICAL

    @property
    def required_data(self) -> list[str]:
        return ["high", "low", "close", "volume"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"anchor": "session"}

    @property
    def min_rows(self) -> int:
        return 1

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 VWAP。

        Parameters
        ----------
        data:
            含 ``high`` / ``low`` / ``close`` / ``volume`` 与时间列的 DataFrame。
            若时间列为日期时间类型，将按时间升序排序后计算。
        **params:
            ``anchor``：锚定周期 ∈ {session/day, week, month, none}，默认 ``session``。
        """
        p = self._resolve_params(params)
        anchor = str(p.get("anchor", "session")).lower()

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "vwap_deviation": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params={"anchor": anchor})

        high = to_float_series(data["high"], "high")
        low = to_float_series(data["low"], "low")
        close = to_float_series(data["close"], "close")
        volume = to_float_series(data["volume"], "volume")

        typical = (high + low + close) / 3.0
        pv = (typical * volume).alias("pv")
        vol = volume.alias("vol")

        work = pl.DataFrame(
            {
                TIME_COLUMN: time_series,
                "pv": pv,
                "vol": vol,
                "close": close,
            }
        )

        # 时间列为日期时间类型时，按其升序排序保证累积正确
        is_temporal = work[TIME_COLUMN].dtype in (pl.Datetime, pl.Date)
        if is_temporal:
            work = work.sort(TIME_COLUMN)

        # 构造锚定分组键
        trunc = _ANCHOR_TRUNC.get(anchor)
        if is_temporal and trunc is not None:
            tcol = work[TIME_COLUMN]
            group = tcol.dt.truncate(trunc) if tcol.dtype == pl.Datetime else tcol
            work = work.with_columns(group.alias("_anchor"))
        else:
            work = work.with_columns(pl.lit(0).alias("_anchor"))

        result = work.with_columns(
            [
                (pl.col("pv").cum_sum().over("_anchor")).alias("_cum_pv"),
                (pl.col("vol").cum_sum().over("_anchor")).alias("_cum_vol"),
            ]
        )

        cum_pv = result["_cum_pv"].to_numpy().astype("float64")
        cum_vol = result["_cum_vol"].to_numpy().astype("float64")
        close_np = result["close"].to_numpy().astype("float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            vwap_np = np.where(cum_vol > 0, cum_pv / np.where(cum_vol > 0, cum_vol, np.nan), np.nan)
            deviation = np.where(
                vwap_np != 0, close_np / np.where(vwap_np != 0, vwap_np, np.nan) - 1.0, np.nan
            )

        frame = pl.DataFrame(
            {
                "value": pl.Series("value", vwap_np, dtype=pl.Float64),
                "vwap_deviation": pl.Series("vwap_deviation", deviation, dtype=pl.Float64),
            }
        )
        out_time = result[TIME_COLUMN]
        return self._build_result(
            out_time,
            frame,
            params={"anchor": anchor},
            extra_metadata={"sorted_by_time": is_temporal},
        )


def vwap(data: pl.DataFrame, anchor: str = "session") -> pl.Series:
    """便捷函数：返回 VWAP 序列。"""
    return VWAPIndicator().calculate(data, anchor=anchor).values["value"]
