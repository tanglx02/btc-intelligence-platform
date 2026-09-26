"""RHODL 比率。

对应指标字典 §6.8 ``onchain.rhodl``。

公式
----
    RHODL = 短期持有者供应比例 / 长期持有者供应比例
          ≈ STH Supply / LTH Supply

其中（RHODL 原始口径）：STH 供应 = 币龄 ≤ 48 周的供应量，LTH 供应 = 币龄 > 48 周的供应量。
⚠️ 48 周口径与全系统 LTH 通用的 155 天口径不同，专业展示必须注明。

新人占比冲高说明市场热度极高、常接近顶部；极低说明无人问津、常接近底部。
数据来源候选列：``rhodl_ratio`` / ``rhodl``（直接值）；缺失时由
``sth_supply_48w`` / ``lth_supply_48w``（或 ``sth_supply`` / ``lth_supply``）推算。
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import safe_divide

__all__ = ["RHODLIndicator", "rhodl"]

_DIRECT = ("rhodl_ratio", "rhodl")
_STH = ("sth_supply_48w", "sth_supply", "supply_sth_48w")
_LTH = ("lth_supply_48w", "lth_supply", "supply_lth_48w")


class RHODLIndicator(IndicatorBase):
    """RHODL 比率，``onchain.rhodl``。"""

    @property
    def name(self) -> str:
        return "onchain.rhodl"

    @property
    def display_name(self) -> str:
        return "RHODL 比率"

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def required_data(self) -> list[str]:
        return ["rhodl_ratio"]

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        if any(c in data.columns for c in _DIRECT):
            return True
        return any(c in data.columns for c in _STH) and any(c in data.columns for c in _LTH)

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 RHODL 比率。

        Parameters
        ----------
        data:
            含 RHODL 直接列（或 STH/LTH 供应列）与时间列的 DataFrame。
        """
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame({"value": pl.Series([None] * n, dtype=pl.Float64)})
            return self._build_result(time_series, frame, params={})

        direct_name, direct = self._first_column(data, list(_DIRECT))
        if direct_name is not None:
            value = direct.alias("value")
            meta = {"source": direct_name}
        else:
            sth = self._first_column(data, list(_STH))[1]
            lth = self._first_column(data, list(_LTH))[1]
            value = safe_divide(sth, lth, default=float("nan")).alias("value")
            meta = {"source": "computed_sth_over_lth"}

        frame = pl.DataFrame({"value": value})
        return self._build_result(time_series, frame, params={}, extra_metadata=meta)


def rhodl(data: pl.DataFrame) -> pl.Series:
    """便捷函数：返回 RHODL 比率序列。"""
    return RHODLIndicator().calculate(data).values["value"]
