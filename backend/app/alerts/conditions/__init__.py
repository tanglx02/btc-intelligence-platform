"""预警 conditions 子包：条件增强检查（持续时间/连续次数等）。"""

from .duration import DurationTracker

__all__ = ["DurationTracker"]
