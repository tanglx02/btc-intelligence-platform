"""净未实现盈亏 NUPL（Net Unrealized Profit / Loss）。

对应指标字典 §6.6 ``onchain.nupl``。

公式
----
    NUPL = (Market Cap − Realized Cap) / Market Cap = 1 − 1 / MVRV

输出范围通常在 −1 ~ 1：>0.75 极度贪婪（周期顶部区），<0 全网整体亏损（大底区）。
Market Cap 与 Realized Cap 的解析口径与 MVRV 完全一致（复用 mvrv 模块的解析函数）。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import safe_divide
from .mvrv import resolve_market_cap, resolve_realized_cap

__all__ = ["NUPLIndicator", "nupl"]


class NUPLIndicator(IndicatorBase):
    """净未实现盈亏，``onchain.nupl``。"""

    @property
    def name(self) -> str:
        return "onchain.nupl"

    @property
    def display_name(self) -> str:
        return "净未实现盈亏"

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def required_data(self) -> list[str]:
        return ["realized_cap"]

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        """与 MVRV 相同的输入要求：可推算 market_cap 与 realized_cap。"""
        from .mvrv import MVRVIndicator

        return MVRVIndicator().validate_input(data)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 NUPL。

        Parameters
        ----------
        data:
            含 market_cap / realized_cap（或可推算列）与时间列的 DataFrame。
        """
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(time_series, frame, params={})

        mcap = resolve_market_cap(data)
        rcap = resolve_realized_cap(data)
        unrealized = mcap - rcap
        nupl = safe_divide(unrealized, mcap, default=float("nan")).alias("value")
        frame = pl.DataFrame({"value": nupl})
        return self._build_result(time_series, frame, params={})


def nupl(data: pl.DataFrame) -> pl.Series:
    """便捷函数：返回 NUPL 序列。"""
    return NUPLIndicator().calculate(data).values["value"]
