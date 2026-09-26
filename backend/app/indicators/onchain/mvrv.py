"""市值与已实现市值比率 MVRV。

对应指标字典 §6.1 ``onchain.mvrv``。

公式
----
    Market Cap   = Close × Circulating Supply
    MVRV         = Market Cap / Realized Cap
    MVRV Z-Score = (Market Cap − Realized Cap) / StdDev(Market Cap)   # 派生变体

输入依赖 ``market_prices(market_cap 或 close+supply)`` 与
``onchain_metrics(realized_cap)``。当缺少 market_cap 时，自动由 close × supply 推算；
当缺少 realized_cap 时，尝试由 realized_price × supply 推算。

输出：value（MVRV 倍数）与 mvrv_z（Z-Score 变体，滚动标准差窗口默认全历史）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import safe_divide

__all__ = ["MVRVIndicator", "mvrv"]


def resolve_market_cap(data: pl.DataFrame) -> pl.Series:
    """解析 Market Cap 序列：优先直接列，其次 close × supply。"""
    for cand in ("market_cap", "mcap", "market_value", "cap"):
        if cand in data.columns:
            return data[cand].cast(pl.Float64, strict=False).alias("market_cap")
    price_col = next((c for c in ("close", "price") if c in data.columns), None)
    supply_col = next(
        (c for c in ("supply", "circulating_supply", "float") if c in data.columns), None
    )
    if price_col and supply_col:
        price = data[price_col].cast(pl.Float64, strict=False)
        supply = data[supply_col].cast(pl.Float64, strict=False)
        return (price * supply).alias("market_cap")
    return pl.Series("market_cap", [None] * data.height, dtype=pl.Float64)


def resolve_realized_cap(data: pl.DataFrame) -> pl.Series:
    """解析 Realized Cap 序列：优先直接列，其次 realized_price × supply。"""
    for cand in ("realized_cap", "realized_value", "rcap"):
        if cand in data.columns:
            return data[cand].cast(pl.Float64, strict=False).alias("realized_cap")
    rp_col = next((c for c in ("realized_price",) if c in data.columns), None)
    supply_col = next(
        (c for c in ("supply", "circulating_supply", "float") if c in data.columns), None
    )
    if rp_col and supply_col:
        rp = data[rp_col].cast(pl.Float64, strict=False)
        supply = data[supply_col].cast(pl.Float64, strict=False)
        return (rp * supply).alias("realized_cap")
    return pl.Series("realized_cap", [None] * data.height, dtype=pl.Float64)


class MVRVIndicator(IndicatorBase):
    """市值/已实现市值比率，``onchain.mvrv``。"""

    @property
    def name(self) -> str:
        return "onchain.mvrv"

    @property
    def display_name(self) -> str:
        return "市值与已实现市值比率"

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def required_data(self) -> list[str]:
        # 至少需要能推算出 market_cap 与 realized_cap
        return ["realized_cap"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"z_window": 0}  # 0 表示 Z-Score 使用扩展（全历史）标准差

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        has_mcap = (
            any(c in data.columns for c in ("market_cap", "mcap", "market_value", "cap"))
            or (("close" in data.columns or "price" in data.columns)
                and any(c in data.columns for c in ("supply", "circulating_supply", "float")))
        )
        has_rcap = (
            any(c in data.columns for c in ("realized_cap", "realized_value", "rcap"))
            or ("realized_price" in data.columns
                and any(c in data.columns for c in ("supply", "circulating_supply", "float")))
        )
        return has_mcap and has_rcap

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 MVRV 及 Z-Score 变体。

        Parameters
        ----------
        data:
            含 market_cap / realized_cap（或可推算列）与时间列的 DataFrame。
        **params:
            ``z_window``：Z-Score 标准差滚动窗口，0 表示使用扩展全历史标准差。
        """
        p = self._resolve_params(params)
        z_window = int(p.get("z_window", 0) or 0)

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "mvrv_z": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params={"z_window": z_window})

        mcap = resolve_market_cap(data)
        rcap = resolve_realized_cap(data)
        mvrv = safe_divide(mcap, rcap, default=float("nan")).alias("value")

        # Z-Score = (MarketCap − RealizedCap) / std(MarketCap)
        mcap_np = mcap.to_numpy().astype("float64")
        rcap_np = rcap.to_numpy().astype("float64")
        if z_window and z_window > 0:
            std = mcap.rolling_std(window_size=z_window, ddof=1, min_samples=max(2, z_window // 2))
            std_np = std.to_numpy().astype("float64")
        else:
            std_np = self._expanding_std(mcap_np)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = np.where(
                std_np > 0, (mcap_np - rcap_np) / np.where(std_np > 0, std_np, np.nan), np.nan
            )

        frame = pl.DataFrame(
            {
                "value": mvrv,
                "mvrv_z": pl.Series("mvrv_z", z, dtype=pl.Float64),
            }
        )
        return self._build_result(time_series, frame, params={"z_window": z_window})

    @staticmethod
    def _expanding_std(arr: np.ndarray) -> np.ndarray:
        """扩展窗口（累积）标准差，样本数 < 2 处返回 NaN。"""
        n = arr.size
        out = np.full(n, np.nan, dtype="float64")
        for i in range(1, n):
            window = arr[: i + 1]
            window = window[np.isfinite(window)]
            if window.size >= 2:
                out[i] = float(np.std(window, ddof=1))
        return out


def mvrv(data: pl.DataFrame) -> pl.Series:
    """便捷函数：返回 MVRV 序列。"""
    return MVRVIndicator().calculate(data).values["value"]
