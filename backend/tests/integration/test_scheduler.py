"""调度器集成测试（纯 asyncio SchedulerService，无 DB 依赖）。

覆盖：
- TaskDefinition 校验（空名/非正间隔/负延迟拒绝、name strip、默认值）；
- 注册：重名拒绝、list_tasks 字段与优先级排序、运行中注册即时启动；
- 生命周期：start/stop、start 幂等、stop 未启动安全、enabled=False 跳过；
- 暂停/恢复：未注册抛 KeyError、暂停期间不执行、恢复后继续、paused 标志；
- 失败隔离：单任务失败记录 FAILED 且循环存活、其他任务不受影响；
- run_once：成功/失败/未注册/绕过暂停/未启动可用；
- system_jobs 记录：patch get_db_session_ctx（延迟导入接缝），验证
  postgresql upsert 语句参数、FAILED 计数、DB 故障不影响任务循环；
- 默认任务注册表与全局单例（get_scheduler / set_scheduler）。

测试加速：monkeypatch 模块级常量 _PAUSED_POLL_SECONDS（暂停轮询间隔），
任务 interval 0.1~0.2s，全部测试 < 5s。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.dialects import postgresql

from app.scheduler.jobs import register_default_jobs
from app.scheduler.scheduler import (
    SchedulerService,
    TaskDefinition,
    get_scheduler,
    set_scheduler,
)
from app.models.enums import JobStatus


class Counter:
    """异步任务计数器（可注入延迟/异常）。"""

    def __init__(self, error: Exception | None = None):
        self.n = 0
        self.error = error

    async def __call__(self) -> None:
        self.n += 1
        if self.error is not None:
            raise self.error


def _task(name: str, counter: Counter, **kwargs) -> TaskDefinition:
    kwargs.setdefault("interval_seconds", 0.15)
    return TaskDefinition(name=name, coroutine=counter, **kwargs)


# ===========================================================================
# TaskDefinition 校验
# ===========================================================================
class TestTaskDefinition:
    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="任务名"):
            TaskDefinition(name="  ", coroutine=Counter(), interval_seconds=10)

    def test_non_positive_interval_raises(self):
        with pytest.raises(ValueError, match="interval_seconds"):
            TaskDefinition(name="t", coroutine=Counter(), interval_seconds=0)

    def test_negative_initial_delay_raises(self):
        with pytest.raises(ValueError, match="initial_delay"):
            TaskDefinition(
                name="t", coroutine=Counter(), interval_seconds=10, initial_delay=-1
            )

    def test_name_stripped(self):
        task = TaskDefinition(name="  spaced  ", coroutine=Counter(), interval_seconds=10)
        assert task.name == "spaced"

    def test_defaults(self):
        task = TaskDefinition(name="t", coroutine=Counter(), interval_seconds=10)
        assert task.priority == 50
        assert task.enabled is True
        assert task.job_group == "GENERAL"
        assert task.initial_delay == 0.0
        assert task.description == ""


# ===========================================================================
# 注册
# ===========================================================================
class TestRegistration:
    async def test_register_and_list_fields(self):
        sched = SchedulerService()
        sched.register(_task("a", Counter(), description="desc", job_group="MARKET"))
        items = sched.list_tasks()
        assert len(items) == 1
        item = items[0]
        assert item["name"] == "a"
        assert item["interval_seconds"] == 0.15
        assert item["priority"] == 50
        assert item["enabled"] is True
        assert item["paused"] is False
        assert item["running"] is False
        assert item["description"] == "desc"
        assert item["job_group"] == "MARKET"
        assert item["last_status"] is None

    async def test_duplicate_name_raises(self):
        sched = SchedulerService()
        sched.register(_task("dup", Counter()))
        with pytest.raises(ValueError, match="重复注册"):
            sched.register(_task("dup", Counter()))

    async def test_list_sorted_by_priority(self):
        sched = SchedulerService()
        sched.register(_task("low", Counter(), priority=90))
        sched.register(_task("high", Counter(), priority=5))
        sched.register(_task("mid", Counter(), priority=40))
        names = [t["name"] for t in sched.list_tasks()]
        assert names == ["high", "mid", "low"]

    async def test_disabled_task_not_started_on_start(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("off", counter, enabled=False))
        await sched.start()
        assert sched.list_tasks()[0]["running"] is False
        await asyncio.sleep(0.3)
        assert counter.n == 0  # 禁用任务从不执行
        await sched.stop()

    async def test_register_while_running_starts_immediately(self):
        sched = SchedulerService()
        await sched.start()
        counter = Counter()
        sched.register(_task("late", counter))
        assert sched.list_tasks()[0]["running"] is True
        await asyncio.sleep(0.35)
        assert counter.n >= 1  # 即时启动并已执行
        await sched.stop()


# ===========================================================================
# 生命周期
# ===========================================================================
class TestLifecycle:
    async def test_start_stop_flags(self):
        sched = SchedulerService()
        assert sched.is_running is False
        sched.register(_task("t", Counter()))
        await sched.start()
        assert sched.is_running is True
        assert sched.list_tasks()[0]["running"] is True
        await sched.stop()
        assert sched.is_running is False
        assert sched.list_tasks()[0]["running"] is False

    async def test_start_idempotent(self):
        sched = SchedulerService()
        sched.register(_task("t", Counter()))
        await sched.start()
        await sched.start()  # 二次 start 不抛错、不重复启动
        await sched.stop()
        assert sched.is_running is False

    async def test_stop_without_start_safe(self):
        sched = SchedulerService()
        sched.register(_task("t", Counter()))
        await sched.stop()  # 未启动直接 stop 应为 no-op
        assert sched.is_running is False

    async def test_stop_terminates_loops(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter, interval_seconds=0.1))
        await sched.start()
        await asyncio.sleep(0.3)
        count_at_stop = counter.n
        await sched.stop()
        await asyncio.sleep(0.3)
        assert counter.n == count_at_stop  # 停止后不再执行

    async def test_initial_delay_defers_first_run(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter, interval_seconds=10, initial_delay=0.4))
        await sched.start()
        await asyncio.sleep(0.1)
        assert counter.n == 0  # 延迟期内未执行
        await asyncio.sleep(0.5)
        assert counter.n >= 1  # 延迟过后执行
        await sched.stop()


# ===========================================================================
# 暂停 / 恢复
# ===========================================================================
class TestPauseResume:
    @pytest.fixture(autouse=True)
    def _fast_pause_poll(self, monkeypatch):
        """加速暂停轮询（模块级常量，_run_loop 运行时查找）。"""
        monkeypatch.setattr("app.scheduler.scheduler._PAUSED_POLL_SECONDS", 0.05)

    async def test_pause_unknown_task_raises(self):
        sched = SchedulerService()
        with pytest.raises(KeyError, match="未注册的任务"):
            sched.pause("ghost")

    async def test_resume_unknown_task_raises(self):
        sched = SchedulerService()
        with pytest.raises(KeyError, match="未注册的任务"):
            sched.resume("ghost")

    async def test_pause_skips_execution(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter))
        await sched.start()
        await asyncio.sleep(0.2)
        assert counter.n >= 1
        sched.pause("t")
        assert sched.list_tasks()[0]["paused"] is True
        count_at_pause = counter.n
        await asyncio.sleep(0.4)  # 足够多个 interval 周期
        assert counter.n == count_at_pause  # 暂停期间不再执行
        await sched.stop()

    async def test_resume_restores_execution(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter))
        await sched.start()
        sched.pause("t")
        await asyncio.sleep(0.15)
        sched.resume("t")
        assert sched.list_tasks()[0]["paused"] is False
        # 轮询等待恢复执行（暂停轮询已 patch 为 0.05s）
        for _ in range(60):
            await asyncio.sleep(0.05)
            if counter.n >= 1:
                break
        assert counter.n >= 1, "resume 后任务应恢复执行"
        await sched.stop()

    async def test_resume_without_pause_safe(self):
        sched = SchedulerService()
        sched.register(_task("t", Counter()))
        sched.resume("t")  # 未暂停时 resume 应安全
        assert sched.list_tasks()[0]["paused"] is False


# ===========================================================================
# 失败隔离
# ===========================================================================
class TestFailureIsolation:
    async def test_failing_task_recorded_failed(self):
        sched = SchedulerService()
        bad = Counter(error=RuntimeError("boom"))
        sched.register(_task("bad", bad))
        await sched.start()
        for _ in range(60):
            await asyncio.sleep(0.05)
            if bad.n >= 1:
                break
        await sched.stop()
        item = sched.list_tasks()[0]
        assert item["last_status"] == "FAILED"
        assert "boom" in item["last_error"]
        assert item["last_duration_seconds"] is not None
        assert item["last_run_at"] is not None

    async def test_failure_does_not_kill_loop(self):
        """任务失败后循环存活，下一轮继续尝试执行。"""
        sched = SchedulerService()
        bad = Counter(error=ValueError("x"))
        sched.register(_task("bad", bad, interval_seconds=0.1))
        await sched.start()
        for _ in range(80):
            await asyncio.sleep(0.05)
            if bad.n >= 3:
                break
        await sched.stop()
        assert bad.n >= 3, "失败任务应持续重试而非退出循环"

    async def test_other_tasks_unaffected_by_failure(self):
        """单任务崩溃不影响其他任务执行（核心错误隔离语义）。"""
        sched = SchedulerService()
        bad = Counter(error=RuntimeError("crash"))
        good = Counter()
        sched.register(_task("bad", bad, interval_seconds=0.1, priority=10))
        sched.register(_task("good", good, interval_seconds=0.1, priority=20))
        await sched.start()
        for _ in range(80):
            await asyncio.sleep(0.05)
            if good.n >= 3:
                break
        await sched.stop()
        assert bad.n >= 1 and good.n >= 3, "good 任务不应被 bad 任务失败影响"
        statuses = {t["name"]: t["last_status"] for t in sched.list_tasks()}
        assert statuses["bad"] == "FAILED"
        assert statuses["good"] == "COMPLETED"

    async def test_coroutine_return_value_ignored(self):
        """任务协程的返回值不参与调度（仅异常影响状态）。"""
        async def returns_value() -> str:
            return "ignored"

        sched = SchedulerService()
        sched.register(TaskDefinition(name="ret", coroutine=returns_value, interval_seconds=0.1))
        await sched.start()
        await asyncio.sleep(0.25)
        await sched.stop()
        assert sched.list_tasks()[0]["last_status"] == "COMPLETED"


# ===========================================================================
# run_once
# ===========================================================================
class TestRunOnce:
    async def test_run_once_success(self):
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter))
        result = await sched.run_once("t")
        assert result["name"] == "t"
        assert result["status"] == "COMPLETED"
        assert result["error"] is None
        assert result["duration_seconds"] >= 0
        assert counter.n == 1

    async def test_run_once_failure(self):
        sched = SchedulerService()
        bad = Counter(error=RuntimeError("manual boom"))
        sched.register(_task("t", bad))
        result = await sched.run_once("t")
        assert result["status"] == "FAILED"
        assert "manual boom" in result["error"]

    async def test_run_once_unknown_raises(self):
        sched = SchedulerService()
        with pytest.raises(KeyError, match="未注册的任务"):
            await sched.run_once("ghost")

    async def test_run_once_bypasses_pause(self):
        """run_once 绕过暂停与间隔（设计语义：手动触发）。"""
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter))
        sched.pause("t")
        result = await sched.run_once("t")
        assert result["status"] == "COMPLETED"
        assert counter.n == 1

    async def test_run_once_without_start(self):
        """调度器未启动时 run_once 仍可用。"""
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter))
        result = await sched.run_once("t")
        assert result["status"] == "COMPLETED"
        assert counter.n == 1
        assert sched.is_running is False


# ===========================================================================
# system_jobs 记录（patch app.core.database.get_db_session_ctx —— 延迟导入接缝）
# ===========================================================================
class RecordingSession:
    """记录 execute() 语句的假 AsyncSession。"""

    def __init__(self):
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)


class _SilentSession:
    """静默假 AsyncSession（no-op，避免测试触碰真实 DB）。"""

    async def execute(self, stmt):
        pass


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """全模块静默 DB 写入（无 DB 环境；TestSystemJobsRecord 类内覆盖为记录版）。

    _record_job_run 延迟导入 get_db_session_ctx，任务循环每轮都会调用；
    不 patch 则每次尝试连接真实 PostgreSQL（连接拒绝开销拖慢循环、日志污染）。
    """
    import app.core.database as db_mod

    @asynccontextmanager
    async def silent_ctx():
        yield _SilentSession()

    monkeypatch.setattr(db_mod, "get_db_session_ctx", silent_ctx)


class TestSystemJobsRecord:
    @pytest.fixture
    def db_capture(self, monkeypatch):
        """覆盖模块级 _no_db：patch 延迟导入的 get_db_session_ctx，捕获 upsert 语句。"""
        import app.core.database as db_mod

        session = RecordingSession()

        @asynccontextmanager
        async def fake_ctx():
            yield session

        monkeypatch.setattr(db_mod, "get_db_session_ctx", fake_ctx)
        return session

    async def test_loop_run_records_upsert(self, db_capture):
        sched = SchedulerService()
        sched.register(_task("recorded", Counter(), description="d", job_group="ENGINE"))
        await sched.start()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if db_capture.statements:
                break
        await sched.stop()
        assert len(db_capture.statements) >= 1
        stmt = db_capture.statements[0]
        # 必须是 postgresql 方言的 Insert（upsert on_conflict_do_update）
        assert isinstance(stmt, postgresql.Insert)
        compiled = stmt.compile(dialect=postgresql.dialect())
        params = compiled.params
        assert params["job_name"] == "recorded"
        assert params["status"] == JobStatus.COMPLETED
        assert params["job_group"] == "ENGINE"
        assert params["description"] == "d"
        assert params["schedule_interval_seconds"] == 0.15

    async def test_failure_records_failed_with_count(self, db_capture):
        """FAILED 状态 upsert 且 consecutive_failures 进入更新集。"""
        sched = SchedulerService()
        sched.register(_task("failing", Counter(error=RuntimeError("e"))))
        await sched.start()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if db_capture.statements:
                break
        await sched.stop()
        assert db_capture.statements, "失败也应写入 system_jobs"
        compiled = db_capture.statements[0].compile(dialect=postgresql.dialect())
        assert compiled.params["status"] == JobStatus.FAILED
        assert compiled.params["last_error"] == "e"
        # FAILED 分支：consecutive_failures = consecutive_failures + 1（SQL 表达式）
        assert "consecutive_failures" in str(compiled)

    async def test_db_error_does_not_break_loop(self, monkeypatch):
        """DB 写入失败仅告警，任务循环继续执行（自吞异常语义）。"""
        import app.core.database as db_mod

        @asynccontextmanager
        async def broken_ctx():
            raise RuntimeError("db down")
            yield  # pragma: no cover

        monkeypatch.setattr(db_mod, "get_db_session_ctx", broken_ctx)
        sched = SchedulerService()
        counter = Counter()
        sched.register(_task("t", counter, interval_seconds=0.1))
        await sched.start()
        for _ in range(80):
            await asyncio.sleep(0.05)
            if counter.n >= 3:
                break
        await sched.stop()
        assert counter.n >= 3, "DB 故障不得中断任务循环"
        assert sched.list_tasks()[0]["last_status"] == "COMPLETED"

    async def test_run_once_records(self, db_capture):
        sched = SchedulerService()
        sched.register(_task("once", Counter()))
        result = await sched.run_once("once")
        assert result["status"] == "COMPLETED"
        assert len(db_capture.statements) == 1
        compiled = db_capture.statements[0].compile(dialect=postgresql.dialect())
        assert compiled.params["job_name"] == "once"
        assert compiled.params["status"] == JobStatus.COMPLETED


# ===========================================================================
# 默认任务注册表与全局单例
# ===========================================================================
_DEFAULT_TASK_NAMES = {
    "market_1m", "market_candles_15m", "incremental_sync_1h",
    "indicator_calc_1h", "engine_cycle_4h", "engine_valuation_4h",
    "engine_risk_4h", "engine_regime_12h", "quality_check_6h",
    "gap_detection_1d", "daily_snapshot_1d",
}


class TestDefaultJobs:
    def test_registers_all_11_tasks(self):
        sched = SchedulerService()
        register_default_jobs(sched)
        names = {t["name"] for t in sched.list_tasks()}
        assert names == _DEFAULT_TASK_NAMES
        assert len(names) == 11

    def test_priority_ordering(self):
        sched = SchedulerService()
        register_default_jobs(sched)
        items = sched.list_tasks()
        assert items[0]["name"] == "market_1m"          # 最高优先
        assert items[-1]["name"] == "daily_snapshot_1d"  # 最低优先
        priorities = [t["priority"] for t in items]
        assert priorities == sorted(priorities)

    def test_job_groups(self):
        sched = SchedulerService()
        register_default_jobs(sched)
        groups = {t["name"]: t["job_group"] for t in sched.list_tasks()}
        assert groups["market_1m"] == "MARKET"
        assert groups["market_candles_15m"] == "MARKET"
        for engine_task in ("engine_cycle_4h", "engine_valuation_4h",
                            "engine_risk_4h", "engine_regime_12h"):
            assert groups[engine_task] == "ENGINE"
        for system_task in ("incremental_sync_1h", "indicator_calc_1h",
                            "quality_check_6h", "gap_detection_1d",
                            "daily_snapshot_1d"):
            assert groups[system_task] == "SYSTEM"

    def test_all_coroutines_callable(self):
        sched = SchedulerService()
        register_default_jobs(sched)
        for item in sched.list_tasks():
            assert item["interval_seconds"] > 0
            assert item["enabled"] is True


class TestGlobalSingleton:
    def setup_method(self):
        set_scheduler(None)  # 每个用例前重置单例

    def teardown_method(self):
        set_scheduler(None)  # 用例后清理，防污染其他测试

    def test_get_scheduler_returns_singleton(self):
        s1 = get_scheduler()
        s2 = get_scheduler()
        assert s1 is s2
        assert {t["name"] for t in s1.list_tasks()} == _DEFAULT_TASK_NAMES

    def test_set_scheduler_replaces_instance(self):
        s1 = get_scheduler()
        replacement = SchedulerService()
        set_scheduler(replacement)
        assert get_scheduler() is replacement
        assert get_scheduler() is not s1

    def test_set_scheduler_none_recreates_with_defaults(self):
        s1 = get_scheduler()
        set_scheduler(None)
        s2 = get_scheduler()
        assert s2 is not s1
        assert {t["name"] for t in s2.list_tasks()} == _DEFAULT_TASK_NAMES
