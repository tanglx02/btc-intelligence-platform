"""未平仓合约量分析 Open Interest Analysis。

对应指标字典 §7.2 ``deriv.open_interest``。

功能
----
- OI(USD) 序列整理：优先 ``open_interest_usd``，缺失时由 ``open_interest``(BTC) × 标记价格推算；
- OI 变化率：``OI_change = OI_t / OI_{t-window} − 1``；
- OI 与价格的相关性：滚动窗口内 OI 收益率与价格收益率的皮尔逊相关系数
  （用于背离检测：价格新高 + OI 下降 = 动能衰减）；
- 杠杆率代理：若存在 ``market_cap`` 列，输出 ``oi_mcap_ratio = OI / Market Cap``。

⚠️ OI 绝对值受市场规模增长影响，禁止直接用全历史分位判过热，应结合 OI/MCap 与变化率。

输出列：value（OI USD）、oi_change、price_corr、oi_mcap_ratio。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import rolling_correlation, simple_returns, to_float_series

__all__ = ["OpenInterestAnalysis", "open_interest_analysis"]

_OI_USD = ("open_interest_usd", "oi_usd", "open_interest_value")
_OI = ("open_interest", "oi")
_PRICE = ("mark_price", "close", "price")


class OpenInterestAnalysis(IndicatorBase):
    """未平仓合约量变化分析，``deriv.open_interest``。"""

    @property
    def name(self) -> str:
        return "deriv.open_interest"

    @property
    def display_name(self) -> str:
        return "未平仓合约量"

    @property
    def category(self) -> str:
        return IndicatorCategory.DERIVATIVES

    @property
    def required_data(self) -> list[str]:
        return ["open_interest_usd"]

    @property
    def default_params(self) -> dict[str, Any]:
        return {"change_window": 1, "corr_window": 30}

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        return any(c in data.columns for c in _OI_USD) or any(c in data.columns for c in _OI)

    def _resolve_oi_usd(self, data: pl.DataFrame) -> pl.Series:
        """解析 OI(USD)：优先直接列，其次 OI(BTC) × 价格。"""
        usd_name, usd = self._first_column(data, list(_OI_USD))
        if usd_name is not None:
            return usd
        oi_name, oi = self._first_column(data, list(_OI))
        price_name, price = self._first_column(data, list(_PRICE))
        if oi_name is not None and price_name is not None:
            return (oi * price).alias("open_interest_usd")
        return pl.Series("open_interest_usd", [None] * data.height, dtype=pl.Float64)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 OI 变化与相关性。

        Parameters
        ----------
        data:
            含 OI 列（USD 或 BTC）与时间列的 DataFrame；如含 close/price 列则计算相关性。
        **params:
            ``change_window``：OI 变化率的回看周期，默认 1；
            ``corr_window``：相关性滚动窗口，默认 30。
        """
        p = self._resolve_params(params)
        change_window = int(p["change_window"])
        corr_window = int(p["corr_window"])

        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = self._empty_frame(n)
            return self._build_result(time_series, frame, params=p)

        oi = to_float_series(self._resolve_oi_usd(data), "open_interest_usd")
        oi_np = oi.to_numpy().astype("float64")

        # OI 变化率
        shifted = np.empty(n, dtype="float64")
        shifted[:change_window] = np.nan
        if n > change_window:
            shifted[change_window:] = oi_np[:-change_window]
        with np.errstate(divide="ignore", invalid="ignore"):
            oi_change = np.where(
                shifted > 0, oi_np / np.where(shifted > 0, shifted, np.nan) - 1.0, np.nan
            )

        # OI 与价格相关性（基于各自简单收益率）
        price_name, price = self._first_column(data, list(_PRICE))
        if price_name is not None:
            oi_ret = simple_returns(oi)
            price_ret = simple_returns(price)
            corr = rolling_correlation(oi_ret, price_ret, corr_window)
        else:
            corr = pl.Series("price_corr", [None] * n, dtype=pl.Float64)

        # OI / Market Cap（杠杆率代理）
        if "market_cap" in data.columns:
            mcap = to_float_series(data["market_cap"], "market_cap").to_numpy().astype("float64")
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = np.where(mcap > 0, oi_np / np.where(mcap > 0, mcap, np.nan), np.nan)
        else:
            ratio = np.full(n, np.nan, dtype="float64")

        frame = pl.DataFrame(
            {
                "value": oi.alias("value"),
                "oi_change": pl.Series("oi_change", oi_change, dtype=pl.Float64),
                "price_corr": corr.alias("price_corr"),
                "oi_mcap_ratio": pl.Series("oi_mcap_ratio", ratio, dtype=pl.Float64),
            }
        )
        return self._build_result(time_series, frame, params=p)

    @staticmethod
    def _empty_frame(n: int) -> pl.DataFrame:
        cols = ["value", "oi_change", "price_corr", "oi_mcap_ratio"]
        return pl.DataFrame({c: pl.Series([None] * n, dtype=pl.Float64) for c in cols})


def open_interest_analysis(data: pl.DataFrame, **params: Any) -> pl.DataFrame:
    """便捷函数：返回 OI 分析多列 DataFrame。"""
    return OpenInterestAnalysis().calculate(data, **params).values
