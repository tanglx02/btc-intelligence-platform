"""智能监测预警系统核心包。

模块职责：
- :mod:`app.alerts.engine`          — AlertEngine 主引擎（后台扫描 + 触发/恢复编排）
- :mod:`app.alerts.evaluator`       — 条件树三态求值（TRUE/FALSE/UNKNOWN）
- :mod:`app.alerts.context_builder` — 扫描上下文批量构建（一次扫描一次取数）
- :mod:`app.alerts.conditions`      — 持续时间/连续次数增强条件
- :mod:`app.alerts.cooldown`        — 冷却窗口与用户级速率限制
- :mod:`app.alerts.schemas`         — 条件树 DTO（JSON 持久化格式）

三态语义与 ``app.portfolio.rule_engine`` 保持一致：数据缺失/过期 -> UNKNOWN，
规则不触发也不误报。通知渠道投递（email/模板/dispatcher）由后续任务提供，
引擎通过 ``try import app.alerts.dispatcher`` 可选接入。
"""

from .engine import AlertEngine, get_alert_engine, set_alert_engine

__all__ = [
    "AlertEngine",
    "get_alert_engine",
    "set_alert_engine",
]
