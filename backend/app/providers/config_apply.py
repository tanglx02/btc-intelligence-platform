"""运行时配置应用工具。

供 HealthMonitor / FailoverEngine 等组件在运行时接收 SettingsService
下发的配置覆盖：按 dataclass 字段现值类型做安全转换后 setattr，
仅覆盖「提供了且非 None」的字段，类型非法时记录告警并跳过。
"""

from __future__ import annotations

import dataclasses
from typing import Any

from loguru import logger


def apply_dataclass_fields(
    obj: Any,
    updates: dict[str, Any] | None,
    *,
    label: str = "",
) -> None:
    """将 updates 应用到 dataclass 实例（类型安全，仅覆盖提供的字段）。

    Args:
        obj: dataclass 实例
        updates: 字段名 -> 新值（None 值跳过；非 dataclass 字段跳过）
        label: 日志标签（如 "failover.recovery"）
    """
    if not updates:
        return
    valid = {f.name for f in dataclasses.fields(obj)}
    applied: list[str] = []
    for key, value in updates.items():
        if value is None or key not in valid:
            continue
        try:
            current = getattr(obj, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, str):
                value = str(value)
            setattr(obj, key, value)
            applied.append(key)
        except (TypeError, ValueError) as e:
            logger.warning(
                f"apply_dataclass_fields[{label}] '{key}'={value!r} invalid: {e}"
            )
    if applied:
        logger.debug(f"apply_dataclass_fields[{label}] applied: {applied}")


__all__ = ["apply_dataclass_fields"]
