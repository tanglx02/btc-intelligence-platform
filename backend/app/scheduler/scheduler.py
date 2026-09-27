"""纯 asyncio 任务调度器（不依赖 APScheduler）。

设计要点：
1. 每个任务一个 ``asyncio.Task`` 循环，错误隔离 —— 单任务崩溃不影响其他任务；
2. 分片 sleep（5s 步进）保证 stop()/暂停检查的及时响应；
3. 优雅停止：等待运行中任务完成，超时 10s 强制取消；
4. 每次运行结果记录到 system_jobs 表（upsert），DB 失败不影响任务循环。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

_STOP_POLL_SECONDS = 5.0  # 分片 sleep 步进（停机/暂停响应粒度）
_STOP_WAIT_TIMEOUT = 10.0  # 优雅停止等待超时（秒）
_PAUSED_POLL_SECONDS = 5.0  # 暂停状态轮询间隔（秒）
_RECORD_INTERVAL_HINT = 60  # 未注册任务写入 DB 时的默认调度间隔


@dataclass
class TaskDefinition:
    """调度任务定义。"""

    name: str
    coroutine: Callable[[], Awaitable[Any]]
    interval_seconds: int
    priority: int = 50  # 数字越小越优先启动
    enabled: bool = True
    description: str = ""
    job_group: str = "GENERAL"  # MARKET / ENGINE / SYSTEM / GENERAL...
    initial_delay: float = 0.0  # 启动后延迟（错峰用，秒）

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("任务名不得为空")
        if self.interval_seconds <= 0:
            raise ValueError(f"任务 {self.name} 的 interval_seconds 必须为正数")
        if self.initial_delay < 0:
            raise ValueError(f"任务 {self.name} 的 initial_delay 不得为负数")


class SchedulerService:
    """纯 asyncio 任务调度器。

    Usage::

        scheduler = get_scheduler()
        await scheduler.start()
        ...
        await scheduler.stop()
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskDefinition] = {}
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._paused: set[str] = set()
        self._running = False
        self._last_results: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 注册与生命周期
    # ------------------------------------------------------------------

    def register(self, task: TaskDefinition) -> None:
        """注册任务（重名抛错；调度器运行中注册则立即启动该任务循环）。"""
        if task.name in self._tasks:
            raise ValueError(f"任务 {task.name!r} 已注册，不得重复注册")
        self._tasks[task.name] = task
        if self._running and task.enabled and task.name not in self._running_tasks:
            self._running_tasks[task.name] = asyncio.create_task(
                self._run_loop(task), name=f"scheduler:{task.name}"
            )
            logger.info("任务 {} 已注册并即时启动（间隔 {}s）", task.name, task.interval_seconds)
        else:
            logger.info(
                "任务 {} 已注册（间隔 {}s，enabled={}）", task.name, task.interval_seconds, task.enabled
            )

    async def start(self) -> None:
        """启动所有已注册（且启用）的任务循环。"""
        if self._running:
            logger.warning("调度器已在运行，忽略重复 start")
            return
        self._running = True
        # 启动时以 system_jobs 表中的配置覆盖默认调度（DB 不可用时使用默认值）
        await self._load_overrides_from_db()
        started = 0
        for task in sorted(self._tasks.values(), key=lambda t: (t.priority, t.name)):
            if not task.enabled:
                logger.info("任务 {} 已禁用，跳过启动", task.name)
                continue
            if task.name in self._running_tasks and not self._running_tasks[task.name].done():
                continue
            self._running_tasks[task.name] = asyncio.create_task(
                self._run_loop(task), name=f"scheduler:{task.name}"
            )
            started += 1
        logger.info("调度器已启动：{} / {} 个任务运行中", started, len(self._tasks))

    async def stop(self) -> None:
        """优雅停止：等待当前任务完成，超时 10s 强制取消。"""
        if not self._running:
            return
        logger.info("调度器停止中（等待运行中任务，最长 {}s）...", _STOP_WAIT_TIMEOUT)
        self._running = False
        tasks = [t for t in self._running_tasks.values() if not t.done()]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=_STOP_WAIT_TIMEOUT)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                logger.warning("强制取消 {} 个未在超时内完成的任务", len(pending))
        self._running_tasks.clear()
        logger.info("调度器已停止")

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    def pause(self, task_name: str) -> None:
        """暂停任务（循环保持存活，到点只跳过执行）。"""
        if task_name not in self._tasks:
            raise KeyError(f"未注册的任务: {task_name!r}")
        self._paused.add(task_name)
        logger.info("任务 {} 已暂停", task_name)

    def resume(self, task_name: str) -> None:
        """恢复已暂停的任务。"""
        if task_name not in self._tasks:
            raise KeyError(f"未注册的任务: {task_name!r}")
        self._paused.discard(task_name)
        logger.info("任务 {} 已恢复", task_name)

    async def run_once(self, task_name: str) -> dict[str, Any]:
        """立即执行一次指定任务（绕过暂停与间隔），返回执行结果摘要。"""
        task = self._tasks.get(task_name)
        if task is None:
            raise KeyError(f"未注册的任务: {task_name!r}")
        started = time.monotonic()
        status, error = "COMPLETED", None
        try:
            await task.coroutine()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            status, error = "FAILED", str(exc)
            logger.exception("任务 {}（手动单次执行）失败: {}", task.name, exc)
        duration = time.monotonic() - started
        logger.info("任务 {} 手动单次执行 {}，耗时 {:.1f}s", task.name, status, duration)
        try:
            await self._record_job_run(task.name, status, duration, error)
        except Exception as exc:  # noqa: BLE001
            logger.warning("记录任务运行状态失败（{}）: {}", task.name, exc)
        return {
            "name": task.name,
            "status": status,
            "duration_seconds": round(duration, 1),
            "error": error,
        }

    async def update_interval(self, task_name: str, seconds: int) -> None:
        """更新任务调度间隔（改 task.interval_seconds，下一轮生效）并同步 DB。

        Args:
            task_name: 任务名
            seconds: 新间隔（秒，必须为正数）

        Raises:
            KeyError: 任务未注册
            ValueError: 间隔非正数
        """
        task = self._tasks.get(task_name)
        if task is None:
            raise KeyError(f"未注册的任务: {task_name!r}")
        if seconds <= 0:
            raise ValueError(f"任务 {task_name!r} 的 interval_seconds 必须为正数")
        old = task.interval_seconds
        task.interval_seconds = int(seconds)
        await self._update_job_config(task_name, interval=int(seconds))
        logger.info("任务 {} 调度间隔更新: {}s -> {}s（下一轮生效）", task_name, old, seconds)

    async def set_enabled(self, task_name: str, enabled: bool) -> None:
        """启用/禁用任务（复用 _paused 暂停机制，循环存活到点跳过）并同步 DB。

        Raises:
            KeyError: 任务未注册
        """
        task = self._tasks.get(task_name)
        if task is None:
            raise KeyError(f"未注册的任务: {task_name!r}")
        task.enabled = bool(enabled)
        if enabled:
            self._paused.discard(task_name)
        else:
            self._paused.add(task_name)
        await self._update_job_config(task_name, enabled=bool(enabled))
        logger.info("任务 {} 已{}", task_name, "启用" if enabled else "禁用")

    def list_tasks(self) -> list[dict[str, Any]]:
        """列出所有任务及状态（按优先级升序）。"""
        items: list[dict[str, Any]] = []
        for task in sorted(self._tasks.values(), key=lambda t: (t.priority, t.name)):
            loop = self._running_tasks.get(task.name)
            last = self._last_results.get(task.name, {})
            items.append(
                {
                    "name": task.name,
                    "interval_seconds": task.interval_seconds,
                    "priority": task.priority,
                    "enabled": task.enabled,
                    "paused": task.name in self._paused,
                    "running": loop is not None and not loop.done(),
                    "description": task.description,
                    "job_group": task.job_group,
                    "last_status": last.get("status"),
                    "last_duration_seconds": last.get("duration"),
                    "last_error": last.get("error"),
                    "last_run_at": last.get("at"),
                }
            )
        return items

    @property
    def is_running(self) -> bool:
        """调度器是否处于运行状态。"""
        return self._running

    # ------------------------------------------------------------------
    # 内部：任务循环
    # ------------------------------------------------------------------

    async def _run_loop(self, task: TaskDefinition) -> None:
        """单任务循环：执行 → 记录 → 睡眠到下次；任何异常都被隔离在循环内。"""
        if task.initial_delay > 0:
            logger.info("任务 {} 将在 {}s 后首次执行", task.name, task.initial_delay)
            await self._interruptible_sleep(task.initial_delay)
        while self._running:
            try:
                if task.name in self._paused:
                    await self._interruptible_sleep(_PAUSED_POLL_SECONDS)
                    continue
                started = time.monotonic()
                status, error = "COMPLETED", None
                try:
                    await task.coroutine()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    status, error = "FAILED", str(exc)
                    logger.exception("任务 {} 执行失败: {}", task.name, exc)
                duration = time.monotonic() - started
                logger.info("任务 {} {}，耗时 {:.1f}s", task.name, status.lower(), duration)
                self._last_results[task.name] = {
                    "status": status,
                    "duration": round(duration, 1),
                    "error": error,
                    "at": datetime.now(UTC).isoformat(),
                }
                try:
                    await self._record_job_run(task.name, status, duration, error)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("记录任务运行状态失败（{}）: {}", task.name, exc)
                await self._interruptible_sleep(float(task.interval_seconds))
            except asyncio.CancelledError:
                logger.info("任务 {} 循环被取消", task.name)
                raise
            except Exception as exc:  # noqa: BLE001
                # 兜底：循环本身绝不允许静默退出
                logger.exception("任务 {} 循环出现未捕获错误（继续运行）: {}", task.name, exc)
                await self._interruptible_sleep(float(task.interval_seconds))

    async def _interruptible_sleep(self, seconds: float) -> None:
        """分片 sleep：步进 _STOP_POLL_SECONDS，保证停机/取消及时响应。"""
        remaining = seconds
        while remaining > 0 and self._running:
            step = min(_STOP_POLL_SECONDS, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # ------------------------------------------------------------------
    # 内部：配置持久化（system_jobs 双重身份：仅读写配置字段）
    # ------------------------------------------------------------------

    async def _update_job_config(
        self,
        name: str,
        *,
        interval: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        """写入 system_jobs 配置字段（schedule_interval_seconds/is_enabled）。

        仅覆盖配置字段，不碰运行记录字段（status/last_run_at/...）；
        失败仅告警不影响任务运行。
        """
        task = self._tasks.get(name)
        try:
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            from app.core.database import get_db_session_ctx
            from app.models.system import SystemJob

            values: dict[str, Any] = dict(
                job_name=name,
                job_type="SCHEDULED",
                job_group=task.job_group if task else "GENERAL",
                description=task.description if task else None,
                priority=task.priority if task else 50,
            )
            if interval is not None:
                values["schedule_interval_seconds"] = int(interval)
            if enabled is not None:
                values["is_enabled"] = bool(enabled)
            async with get_db_session_ctx() as session:
                stmt = pg_insert(SystemJob.__table__).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[SystemJob.job_name],
                    set_={k: v for k, v in values.items() if k != "job_name"},
                )
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 system_jobs 配置失败（task={}）: {}", name, exc)

    async def _load_overrides_from_db(self) -> int:
        """启动时从 system_jobs 表读取配置覆盖（schedule_interval_seconds/is_enabled）。

        只读配置字段回写内存任务定义；DB 不可用时容错返回 0。
        """
        try:
            from sqlalchemy import select as sa_select

            from app.core.database import get_db_session_ctx
            from app.models.system import SystemJob

            async with get_db_session_ctx() as session:
                rows = (await session.execute(sa_select(SystemJob))).scalars().all()
        except Exception as exc:  # noqa: BLE001 - DB 不可用使用默认调度
            logger.warning("读取 system_jobs 配置覆盖失败（使用默认调度）: {}", exc)
            return 0

        applied = 0
        for row in rows:
            task = self._tasks.get(row.job_name)
            if task is None:
                continue
            try:
                db_interval = int(row.schedule_interval_seconds or 0)
            except (TypeError, ValueError):
                db_interval = 0
            if db_interval > 0 and db_interval != task.interval_seconds:
                task.interval_seconds = db_interval
                applied += 1
                logger.info(
                    "任务 {} 调度间隔按 system_jobs 覆盖: {}s", task.name, db_interval
                )
            if row.is_enabled is False and task.enabled:
                task.enabled = False
                self._paused.add(task.name)
                applied += 1
                logger.info("任务 {} 按 system_jobs 配置保持禁用", task.name)
        if applied:
            logger.info("system_jobs 配置覆盖已应用: {} 处", applied)
        return applied

    # ------------------------------------------------------------------
    # 内部：运行记录（system_jobs 表 upsert）
    # ------------------------------------------------------------------

    async def _record_job_run(
        self,
        name: str,
        status: str,
        duration: float,
        error: str | None = None,
    ) -> None:
        """记录任务运行结果到 system_jobs 表（按 job_name upsert）。

        本方法自身吞掉所有异常（由调用方再兜底日志），确保 DB 故障不影响任务循环。
        """
        from app.core.database import get_db_session_ctx
        from app.models.enums import JobStatus
        from app.models.system import SystemJob

        task = self._tasks.get(name)
        interval = task.interval_seconds if task else _RECORD_INTERVAL_HINT
        now = datetime.now(UTC)
        try:
            async with get_db_session_ctx() as session:
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                stmt = pg_insert(SystemJob.__table__).values(
                    job_name=name,
                    job_type="SCHEDULED",
                    job_group=task.job_group if task else "GENERAL",
                    description=task.description if task else None,
                    schedule_interval_seconds=interval,
                    priority=task.priority if task else 50,
                    is_enabled=task.enabled if task else True,
                    status=JobStatus(status),
                    last_run_at=now,
                    started_at=now,
                    completed_at=now,
                    last_duration_ms=int(duration * 1000),
                    next_run_at=now + timedelta(seconds=interval),
                    last_error=error,
                    consecutive_failures=1 if status == "FAILED" else 0,
                )
                update_set: dict[str, Any] = {
                    "status": JobStatus(status),
                    "last_run_at": now,
                    "completed_at": now,
                    "last_duration_ms": int(duration * 1000),
                    "last_error": error,
                    "next_run_at": now + timedelta(seconds=interval),
                }
                if status == "FAILED":
                    update_set["consecutive_failures"] = (
                        SystemJob.__table__.c.consecutive_failures + 1
                    )
                else:
                    update_set["consecutive_failures"] = 0
                stmt = stmt.on_conflict_do_update(
                    index_elements=[SystemJob.job_name], set_=update_set
                )
                await session.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写入 system_jobs 失败（task={}）: {}", name, exc)


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_scheduler: SchedulerService | None = None


def get_scheduler() -> SchedulerService:
    """获取全局调度器单例（首次调用时注册默认任务）。"""
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerService()
        # 延迟导入避免 scheduler.py ↔ jobs.py 循环依赖
        from app.scheduler.jobs import register_default_jobs

        register_default_jobs(_scheduler)
    return _scheduler


def set_scheduler(scheduler: SchedulerService | None) -> None:
    """替换全局单例（测试/多实例场景用）。"""
    global _scheduler
    _scheduler = scheduler


__all__ = ["SchedulerService", "TaskDefinition", "get_scheduler", "set_scheduler"]
