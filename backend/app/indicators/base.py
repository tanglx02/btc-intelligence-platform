"""指标计算基类与结果容器。

定义全系统指标计算的统一契约（见 ``docs/architecture/12-indicator-dictionary.md``）：

- 每个指标是 :class:`IndicatorBase` 的子类，声明唯一 ``name``、分类、所需输入列；
- 计算产出统一的 :class:`IndicatorResult`，其 ``values`` 为 Polars DataFrame，
  首列固定为时间列 ``time``，其后为一个或多个数值列（如布林带的 upper/middle/lower）；
- 数据不足时以 ``null`` 填充而非抛异常，保证批处理链路稳健。

输入 DataFrame 约定
------------------
传入 :meth:`IndicatorBase.calculate` 的 ``data`` 应包含：

- 一个时间列，列名由 :attr:`IndicatorBase.time_column` 指定（默认 ``"time"``）；
- :attr:`IndicatorBase.required_data` 中声明的全部特征列（如 ``close``、``high``）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import polars as pl

__all__ = ["IndicatorResult", "IndicatorBase", "IndicatorCategory"]


class IndicatorCategory:
    """指标分类常量（与 indicator_definitions.category 对应）。"""

    TECHNICAL = "technical"
    ONCHAIN = "onchain"
    DERIVATIVES = "derivatives"
    COMPOSITE = "composite"

    ALL = (TECHNICAL, ONCHAIN, DERIVATIVES, COMPOSITE)


# 输出 DataFrame 的统一时间列名
TIME_COLUMN = "time"


@dataclass
class IndicatorResult:
    """指标计算结果。

    Attributes
    ----------
    name:
        指标唯一标识名（同 :attr:`IndicatorBase.name`）。
    values:
        Polars DataFrame，首列为 ``time``，其后为一个或多个数值列。
    metadata:
        附加元数据：本次计算使用的参数、数据源、样本量、计算耗时等。
    calculated_at:
        计算发生的 UTC 时间戳。
    """

    name: str
    values: pl.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)
    calculated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def value_columns(self) -> list[str]:
        """除时间列外的全部数值列名。"""
        return [c for c in self.values.columns if c != TIME_COLUMN]

    @property
    def primary_column(self) -> str:
        """主数值列名。

        多列指标（如布林带）约定主列名为 ``value``，若不存在则取第一个数值列。
        """
        if "value" in self.values.columns:
            return "value"
        cols = self.value_columns
        return cols[0] if cols else "value"

    def last(self, column: str | None = None) -> float | None:
        """返回最新一条指标值（用于快照 / 缓存）。

        Parameters
        ----------
        column:
            目标列名，默认取 :attr:`primary_column`。

        Returns
        -------
        float 或 None
            最新值；序列为空或该点为 null 时返回 ``None``。
        """
        col = column or self.primary_column
        if self.values.height == 0 or col not in self.values.columns:
            return None
        series = self.values[col]
        # 取最后一个非 null 值
        non_null = series.drop_nulls()
        if non_null.len() == 0:
            return None
        return float(non_null[-1])

    def latest_time(self) -> datetime | None:
        """返回最新数据点的观测时间。"""
        if self.values.height == 0 or TIME_COLUMN not in self.values.columns:
            return None
        return self.values[TIME_COLUMN][-1]


class IndicatorBase(ABC):
    """指标计算基类。

    子类需实现 ``name`` / ``display_name`` / ``category`` / ``required_data``
    与 :meth:`calculate`。可选覆写 :attr:`default_params`、:attr:`min_rows`、
    :attr:`time_column` 与 :meth:`validate_input`。
    """

    #: 输入 DataFrame 中时间列的列名
    time_column: str = TIME_COLUMN

    @property
    @abstractmethod
    def name(self) -> str:
        """指标唯一标识名（如 ``tech.rsi``）。"""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """显示名称（中文）。"""
        ...

    @property
    @abstractmethod
    def category(self) -> str:
        """分类：technical / onchain / derivatives / composite。"""
        ...

    @property
    @abstractmethod
    def required_data(self) -> list[str]:
        """所需输入数据列名（不含时间列）。"""
        ...

    @property
    def default_params(self) -> dict[str, Any]:
        """默认参数。"""
        return {}

    @property
    def min_rows(self) -> int:
        """产生首个有效值所需的最小行数；不足时结果全为 null。"""
        return 1

    # ------------------------------------------------------------------ #
    # 计算入口
    # ------------------------------------------------------------------ #
    @abstractmethod
    def calculate(self, data: pl.DataFrame, **params: Any) -> IndicatorResult:
        """计算指标。

        Parameters
        ----------
        data:
            含时间列与 :attr:`required_data` 特征列的输入 DataFrame。
        **params:
            覆盖 :attr:`default_params` 的计算参数。

        Returns
        -------
        IndicatorResult
            统一结构的计算结果。
        """
        ...

    # ------------------------------------------------------------------ #
    # 校验与辅助
    # ------------------------------------------------------------------ #
    def validate_input(self, data: pl.DataFrame) -> bool:
        """验证输入数据是否满足计算要求。

        校验项：

        1. ``data`` 非空；
        2. :attr:`required_data` 中声明的列全部存在；
        3. 行数不少于 :attr:`min_rows`。

        时间列缺失 **不** 视为错误（会自动生成序号索引），以便对纯数值序列复用。
        """
        if data is None or data.height == 0:
            return False
        missing = [c for c in self.required_data if c not in data.columns]
        if missing:
            return False
        if data.height < self.min_rows:
            return False
        return True

    def _missing_columns(self, data: pl.DataFrame) -> list[str]:
        """返回输入中缺失的必需列名。"""
        return [c for c in self.required_data if c not in data.columns]

    def _resolve_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """将用户传入参数与默认参数合并（用户参数优先）。"""
        merged = dict(self.default_params)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged

    def _extract_time(self, data: pl.DataFrame) -> pl.Series:
        """提取时间列；缺失时生成 0..n-1 的整数序号作为占位时间。"""
        if self.time_column in data.columns:
            return data[self.time_column]
        return pl.Series(TIME_COLUMN, range(data.height), dtype=pl.Int64)

    def _column(self, data: pl.DataFrame, name: str) -> pl.Series:
        """安全提取某列并转为 float64；列不存在时返回全 null 序列。"""
        if name in data.columns:
            return data[name].cast(pl.Float64, strict=False)
        return pl.Series(name, [None] * data.height, dtype=pl.Float64)

    def _first_column(
        self, data: pl.DataFrame, candidates: list[str]
    ) -> tuple[str | None, pl.Series]:
        """按优先级在 ``candidates`` 中返回首个存在于 ``data`` 的列。

        链上/衍生品指标常面临不同 Provider 字段命名差异，本方法提供
        统一的「候选列名」探测机制。

        Returns
        -------
        tuple[str | None, polars.Series]
            命中的列名（未命中为 ``None``）与对应的 float64 序列
            （未命中时为全 null 序列）。
        """
        for cand in candidates:
            if cand in data.columns:
                return cand, data[cand].cast(pl.Float64, strict=False)
        return None, pl.Series("value", [None] * data.height, dtype=pl.Float64)

    def _build_result(
        self,
        time_series: pl.Series,
        value_frame: pl.DataFrame,
        params: dict[str, Any],
        extra_metadata: dict[str, Any] | None = None,
    ) -> IndicatorResult:
        """组装统一的 :class:`IndicatorResult`。

        Parameters
        ----------
        time_series:
            时间列序列。
        value_frame:
            一个或多个数值列（不含时间列）。
        params:
            本次计算实际使用的参数（写入 metadata）。
        extra_metadata:
            额外的元数据字典。
        """
        frame = value_frame.with_columns(time_series.alias(TIME_COLUMN))
        # 保证 time 列在最前
        ordered = frame.select([TIME_COLUMN, *value_frame.columns])
        metadata: dict[str, Any] = {
            "params": params,
            "category": self.category,
            "display_name": self.display_name,
            "rows": ordered.height,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        return IndicatorResult(
            name=self.name,
            values=ordered,
            metadata=metadata,
            calculated_at=datetime.now(UTC),
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<{self.__class__.__name__} name={self.name!r} category={self.category!r}>"
