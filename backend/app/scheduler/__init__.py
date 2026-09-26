"""asyncio 任务调度器包。

纯 asyncio 实现的轻量任务调度器（不依赖 APScheduler）：
- SchedulerService：注册/启动/停止/暂停/恢复/单次执行；
- TaskDefinition：任务定义（名称、协程、间隔、优先级、分组）；
- get_scheduler()：全局单例（惰性注册默认任务）。
"""

from .scheduler import SchedulerService, TaskDefinition, get_scheduler, set_scheduler

__all__ = [
    "SchedulerService",
    "TaskDefinition",
    "get_scheduler",
    "set_scheduler",
]
