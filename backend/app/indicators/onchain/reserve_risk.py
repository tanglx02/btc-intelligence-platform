"""储备风险 Reserve Risk。

对应指标字典 §6.9 ``onchain.reserve_risk``。

公式
----
    Reserve Risk = Price / HODL Bank

HODL Bank = Σ(币龄带宽加权供应量)，衡量长期持有者的「囤币信心」。数值跨越多个
数量级，展示必须用对数轴，故额外输出 ``log10`` 列。

历史低分位（如 <10%）多次对应大级别底部；高分位对应顶部区域。
数据来源候选列：``reserve_risk``（直接值）；缺失时由 ``price``/``close`` 与
``hodl_bank``/``hodl_bank_value`` 推算。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from ..base import IndicatorBase, IndicatorCategory, IndicatorResult
from ..utils.math_helpers import safe_divide

__all__ = ["ReserveRiskIndicator", "reserve_risk"]

_DIRECT = ("reserve_risk",)
_PRICE = ("price", "close")
_HODL_BANK = ("hodl_bank", "hodl_bank_value", "hodl_banks")


class ReserveRiskIndicator(IndicatorBase):
    """储备风险，``onchain.reserve_risk``。"""

    @property
    def name(self) -> str:
        return "onchain.reserve_risk"

    @property
    def display_name(self) -> str:
        return "储备风险"

    @property
    def category(self) -> str:
        return IndicatorCategory.ONCHAIN

    @property
    def required_data(self) -> list[str]:
        return ["reserve_risk"]

    @property
    def min_rows(self) -> int:
        return 1

    def validate_input(self, data: pl.DataFrame) -> bool:
        if data is None or data.height == 0:
            return False
        if any(c in data.columns for c in _DIRECT):
            return True
        return any(c in data.columns for c in _PRICE) and any(
            c in data.columns for c in _HODL_BANK
        )

    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算 Reserve Risk 及其 log10。

        Parameters
        ----------
        data:
            含 reserve_risk 直接列（或 price + hodl_bank）与时间列的 DataFrame。
        """
        time_series = self._extract_time(data)
        n = data.height
        if not self.validate_input(data):
            frame = pl.DataFrame(
                {
                    "value": pl.Series([None] * n, dtype=pl.Float64),
                    "log10": pl.Series([None] * n, dtype=pl.Float64),
                }
            )
            return self._build_result(time_series, frame, params={})

        direct_name, direct = self._first_column(data, list(_DIRECT))
        if direct_name is not None:
            value = direct
            meta = {"source": direct_name}
        else:
            price = self._first_column(data, list(_PRICE))[1]
            bank = self._first_column(data, list(_HODL_BANK))[1]
            value = safe_divide(price, bank, default=float("nan"))
            meta = {"source": "computed_price_over_hodl_bank"}

        value_np = value.to_numpy().astype("float64")
        with np.errstate(divide="ignore", invalid="ignore"):
            log10 = np.where(value_np > 0, np.log10(value_np), np.nan)

        frame = pl.DataFrame(
            {
                "value": pl.Series("value", value_np, dtype=pl.Float64),
                "log10": pl.Series("log10", log10, dtype=pl.Float64),
            }
        )
        return self._build_result(time_series, frame, params={}, extra_metadata=meta)


def reserve_risk(data: pl.DataFrame) -> pl.Series:
    """便捷函数：返回 Reserve Risk 序列。"""
    return ReserveRiskIndicator().calculate(data).values["value"]
