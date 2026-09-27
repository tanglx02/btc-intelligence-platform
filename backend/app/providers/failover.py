"""FailoverEngine — 自动故障切换引擎。

职责：
1. 错误分类器：根据 HTTP 状态码/异常类型决定处理策略
2. 自动切换逻辑：按优先级依次尝试 Provider
3. Recovery Threshold：连续成功 N 次 + 响应正常 + 质量正常才恢复
4. 防抖动（Anti-Flapping）：最小观察窗口、指数退避恢复间隔
5. Failover Event 记录（写入数据库）
6. 全部失败时的降级处理
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from loguru import logger

from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.config_apply import apply_dataclass_fields
from app.providers.base.types import (
    ErrorType,
    FailoverEvent,
    FetchResult,
    HealthState,
    ProviderLifecycleStatus,
    QualityStatus,
)
from app.utils.datetime_utils import utcnow


@dataclass
class RecoveryConfig:
    """恢复机制配置。"""

    recovery_threshold: int = 3  # 连续成功 N 次才恢复
    probe_interval_initial: int = 30  # 首次探测间隔（秒）
    probe_interval_max: int = 3600  # 最大探测间隔（秒）
    probe_backoff_multiplier: float = 2.0  # 探测退避乘数
    trial_request_count: int = 5  # 试用期请求数
    trial_success_rate: float = 0.9  # 试用期最低成功率
    response_time_tolerance: float = 1.5  # 响应时间容忍倍数
    min_observation_window: int = 300  # 最小观察窗口（秒）


@dataclass
class AntiFlappingConfig:
    """防抖动配置。"""

    enabled: bool = True
    min_observation_window: int = 300  # 最小观察窗口（秒）
    cooldown_period: int = 300  # 基础冷却期（秒）
    backoff_multiplier: float = 2.0  # 冷却退避乘数
    max_cooldown: int = 3600  # 最大冷却期（秒）
    consecutive_failure_threshold: int = 3  # 连续失败阈值


@dataclass
class ErrorPolicy:
    """错误处理策略。"""

    retry: bool = True
    retry_max: int = 3
    failover: bool = True
    action: str = "auto_failover"
    mark_network_issue: bool = False
    notify_admin: bool = False


class AntiFlappingGuard:
    """防抖动守卫 — 防止 Provider 状态频繁切换。"""

    def __init__(self, config: AntiFlappingConfig):
        self._config = config
        self._last_change: dict[str, datetime] = {}
        self._flap_count: dict[str, int] = {}
        self._cooldown: dict[str, datetime] = {}

    def can_change_state(self, provider_name: str) -> bool:
        """判断是否允许状态变更。

        Args:
            provider_name: Provider 名称

        Returns:
            True 允许变更，False 在冷却期内
        """
        if not self._config.enabled:
            return True

        now = utcnow()

        # 冷却期检查
        if provider_name in self._cooldown:
            if now < self._cooldown[provider_name]:
                logger.debug(
                    f"[{provider_name}] State change blocked: in cooldown "
                    f"until {self._cooldown[provider_name].isoformat()}"
                )
                return False

        # 最小观察窗口检查
        if provider_name in self._last_change:
            elapsed = (now - self._last_change[provider_name]).total_seconds()
            if elapsed < self._config.min_observation_window:
                logger.debug(
                    f"[{provider_name}] State change blocked: observation window "
                    f"({elapsed:.0f}s < {self._config.min_observation_window}s)"
                )
                return False

        return True

    def record_state_change(self, provider_name: str) -> None:
        """记录状态变更，更新冷却期。

        Args:
            provider_name: Provider 名称
        """
        if not self._config.enabled:
            return

        now = utcnow()
        self._last_change[provider_name] = now

        # 检测抖动：短时间内多次变更
        self._flap_count[provider_name] = self._flap_count.get(provider_name, 0) + 1

        # 指数退避冷却期
        cooldown = self._config.cooldown_period * (
            self._config.backoff_multiplier ** (self._flap_count[provider_name] - 1)
        )
        cooldown = min(cooldown, self._config.max_cooldown)
        self._cooldown[provider_name] = now + timedelta(seconds=cooldown)

        logger.debug(
            f"[{provider_name}] State change recorded, cooldown={cooldown:.0f}s "
            f"(flap_count={self._flap_count[provider_name]})"
        )

    def is_in_cooldown(self, provider_name: str) -> bool:
        """是否处于冷却期。"""
        if provider_name not in self._cooldown:
            return False
        return utcnow() < self._cooldown[provider_name]

    def reset_flap_count(self, provider_name: str) -> None:
        """Provider 稳定运行一段时间后重置抖动计数。"""
        self._flap_count[provider_name] = 0
        if provider_name in self._cooldown:
            del self._cooldown[provider_name]

    def get_flap_count(self, provider_name: str) -> int:
        """获取抖动计数。"""
        return self._flap_count.get(provider_name, 0)


@dataclass
class RecoveryState:
    """Provider 恢复状态。"""

    provider_name: str
    is_recovering: bool = False
    consecutive_probes: int = 0
    probe_interval: int = 30
    next_probe_time: datetime | None = None
    trial_requests: int = 0
    trial_successes: int = 0
    last_probe_time: datetime | None = None


class FailoverEngine:
    """自动故障切换引擎。

    核心职责：
    1. 错误分类与策略决策
    2. 自动切换 Provider
    3. Recovery Threshold 恢复机制
    4. 防抖动保护
    5. Failover Event 记录

    Usage:
        engine = FailoverEngine(registry, health_monitor)
        result = await engine.fetch_with_failover(
            category="market",
            method="get_current_price",
            symbol="BTC/USDT"
        )
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        health_monitor: Any = None,
        recovery_config: RecoveryConfig | None = None,
        anti_flapping_config: AntiFlappingConfig | None = None,
    ):
        """初始化故障切换引擎。

        Args:
            registry: Provider 注册中心
            health_monitor: 健康监控器（可选）
            recovery_config: 恢复机制配置
            anti_flapping_config: 防抖动配置
        """
        self._registry = registry
        self._health_monitor = health_monitor
        self._recovery_config = recovery_config or RecoveryConfig()
        self._anti_flapping = AntiFlappingGuard(anti_flapping_config or AntiFlappingConfig())

        self._error_policies = self._build_error_policies()
        self._recovery_states: dict[str, RecoveryState] = {}
        self._failover_events: list[FailoverEvent] = []

        # 恢复探测任务
        self._recovery_task: asyncio.Task | None = None
        self._running = False

    def apply_runtime_config(
        self,
        *,
        recovery: dict[str, Any] | None = None,
        anti_flapping: dict[str, Any] | None = None,
    ) -> None:
        """运行时更新恢复/防抖动配置（SettingsService 接入，仅覆盖提供的字段）。"""
        apply_dataclass_fields(self._recovery_config, recovery, label="failover.recovery")
        apply_dataclass_fields(
            self._anti_flapping._config, anti_flapping, label="failover.anti_flapping"
        )

    def _build_error_policies(self) -> dict[ErrorType, ErrorPolicy]:
        """构建错误处理策略映射。"""
        return {
            ErrorType.RATE_LIMIT: ErrorPolicy(
                retry=True, retry_max=2, failover=False, action="rate_limit_backoff"
            ),
            ErrorType.SERVER_ERROR: ErrorPolicy(
                retry=True, retry_max=3, failover=True, action="auto_failover"
            ),
            ErrorType.TIMEOUT: ErrorPolicy(
                retry=True, retry_max=1, failover=True, action="auto_failover"
            ),
            ErrorType.DNS_ERROR: ErrorPolicy(
                retry=False, retry_max=0, failover=True,
                action="auto_failover", mark_network_issue=True
            ),
            ErrorType.CONNECTION_REFUSED: ErrorPolicy(
                retry=True, retry_max=2, failover=True, action="auto_failover"
            ),
            ErrorType.AUTH_ERROR: ErrorPolicy(
                retry=False, retry_max=0, failover=True,
                action="notify_admin", notify_admin=True
            ),
            ErrorType.NETWORK_ERROR: ErrorPolicy(
                retry=True, retry_max=2, failover=True, action="auto_failover"
            ),
            ErrorType.TLS_ERROR: ErrorPolicy(
                retry=True, retry_max=1, failover=True, action="auto_failover"
            ),
            ErrorType.DATA_FORMAT: ErrorPolicy(
                retry=False, retry_max=0, failover=True,
                action="disable_provider", notify_admin=True
            ),
            ErrorType.DATA_QUALITY: ErrorPolicy(
                retry=False, retry_max=0, failover=True, action="auto_failover"
            ),
            ErrorType.STALE_DATA: ErrorPolicy(
                retry=False, retry_max=0, failover=True, action="auto_failover"
            ),
            ErrorType.EMPTY_RESPONSE: ErrorPolicy(
                retry=True, retry_max=1, failover=True, action="auto_failover"
            ),
            ErrorType.UNKNOWN: ErrorPolicy(
                retry=True, retry_max=1, failover=True, action="auto_failover"
            ),
        }

    # ---- 核心方法 ----

    async def fetch_with_failover(
        self,
        category: str,
        method: str,
        data_type: str | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """带自动 Failover 的数据获取。

        Args:
            category: 数据类别
            method: 方法名
            data_type: 数据类型标识
            **kwargs: 方法参数

        Returns:
            FetchResult
        """
        data_type = data_type or f"{category}:{method}"
        providers = self._registry.get_providers_by_priority(category)

        if not providers:
            logger.error(f"No providers available for category '{category}'")
            return FetchResult(
                success=False,
                error="No providers available",
                error_type=ErrorType.NETWORK_ERROR,
                quality_status=QualityStatus.INVALID,
            )

        last_result: FetchResult | None = None

        for provider in providers:
            # 跳过冷却期中的 Provider
            if self._anti_flapping.is_in_cooldown(provider.name):
                logger.debug(f"Skipping {provider.name}: in cooldown")
                continue

            # 跳过不可用的 Provider
            if not provider.is_available:
                logger.debug(f"Skipping {provider.name}: not available")
                continue

            # 执行请求（含重试）
            result = await self._attempt_with_retry(provider, method, **kwargs)

            if result.success:
                # 成功：记录并返回
                result.is_failover = provider != providers[0]
                logger.info(
                    f"Fetch success: {data_type} from {provider.name} "
                    f"(failover={result.is_failover})"
                )
                return result

            # 失败：记录并继续
            last_result = result
            self._record_failover_event(
                data_type=data_type,
                original_provider=provider.name,
                reason=result.error_type.value if result.error_type else "unknown",
                error_detail=result.error or "",
                latency_ms=result.response_time_ms,
            )

            # 根据错误类型决定是否标记 Provider 离线
            if self._should_mark_offline(result.error_type):
                await self._mark_provider_offline(provider, result)

            # 检查错误策略是否允许 failover（如 RATE_LIMIT：仅重试不切换）
            policy = self._error_policies.get(
                result.error_type or ErrorType.UNKNOWN, ErrorPolicy()
            )
            if not policy.failover:
                logger.warning(
                    f"Failover disabled for {result.error_type} "
                    f"(action={policy.action}), stop trying other providers "
                    f"for {data_type}"
                )
                return result

        # 所有 Provider 都失败
        logger.error(f"All providers failed for {data_type}")
        return last_result or FetchResult(
            success=False,
            error="All providers failed",
            error_type=ErrorType.NETWORK_ERROR,
            quality_status=QualityStatus.INVALID,
        )

    async def _attempt_with_retry(
        self, provider: BaseProvider, method: str, **kwargs: Any
    ) -> FetchResult:
        """单 Provider 的重试逻辑。

        Args:
            provider: Provider 实例
            method: 方法名
            **kwargs: 方法参数

        Returns:
            FetchResult
        """
        method_fn = getattr(provider, method, None)
        if not method_fn or not callable(method_fn):
            return FetchResult(
                success=False,
                error=f"Method '{method}' not found on provider '{provider.name}'",
                error_type=ErrorType.UNKNOWN,
                provider_name=provider.name,
            )

        last_result: FetchResult | None = None
        max_retries = provider.config.retry.count

        for attempt in range(max_retries + 1):
            try:
                result = await method_fn(**kwargs)

                if result.success:
                    return result

                last_result = result

                # 获取错误策略
                policy = self._error_policies.get(
                    result.error_type or ErrorType.UNKNOWN,
                    ErrorPolicy()
                )

                # 判断是否可重试
                if not policy.retry or attempt >= policy.retry_max:
                    return result

                # 计算重试延迟
                delay = self._calculate_retry_delay(attempt, provider.config.retry.backoff_factor)

                logger.warning(
                    f"[{provider.name}] {method} failed (attempt {attempt + 1}/{max_retries + 1}), "
                    f"retrying in {delay:.1f}s: {result.error}"
                )

                await asyncio.sleep(delay)

            except Exception as e:
                last_result = FetchResult(
                    success=False,
                    error=f"{type(e).__name__}: {str(e)}",
                    error_type=ErrorType.UNKNOWN,
                    provider_name=provider.name,
                )
                logger.exception(f"[{provider.name}] Unexpected error in {method}")
                break

        return last_result or FetchResult(
            success=False,
            error="Max retries exceeded",
            error_type=ErrorType.UNKNOWN,
            provider_name=provider.name,
        )

    @staticmethod
    def _calculate_retry_delay(attempt: int, backoff_factor: float) -> float:
        """计算重试延迟（指数退避）。"""
        return min(60.0, backoff_factor * (2 ** attempt))

    def _should_mark_offline(self, error_type: ErrorType | None) -> bool:
        """判断是否应该标记 Provider 离线。"""
        if not error_type:
            return False

        # 这些错误类型应该标记离线
        offline_errors = {
            ErrorType.DNS_ERROR,
            ErrorType.AUTH_ERROR,
            ErrorType.DATA_FORMAT,
            ErrorType.NETWORK_ERROR,
        }

        return error_type in offline_errors

    async def _mark_provider_offline(
        self, provider: BaseProvider, result: FetchResult
    ) -> None:
        """标记 Provider 离线并启动恢复探测。

        Args:
            provider: Provider 实例
            result: 失败的 FetchResult
        """
        # 防抖动检查
        if not self._anti_flapping.can_change_state(provider.name):
            logger.debug(f"[{provider.name}] Cannot mark offline: anti-flapping guard")
            return

        # 更新状态
        provider.set_status(ProviderLifecycleStatus.OFFLINE)
        provider.set_health_state(HealthState.OFFLINE)

        # 记录状态变更
        self._anti_flapping.record_state_change(provider.name)

        logger.warning(
            f"[{provider.name}] Marked OFFLINE due to {result.error_type}"
        )

        # 启动恢复探测
        self._start_recovery(provider.name)

    # ---- 恢复机制 ----

    def _start_recovery(self, provider_name: str) -> None:
        """启动 Provider 恢复探测。

        Args:
            provider_name: Provider 名称
        """
        if provider_name in self._recovery_states:
            state = self._recovery_states[provider_name]
            if state.is_recovering:
                return  # 已在恢复中

        state = RecoveryState(
            provider_name=provider_name,
            is_recovering=True,
            probe_interval=self._recovery_config.probe_interval_initial,
            next_probe_time=utcnow() + timedelta(
                seconds=self._recovery_config.probe_interval_initial
            ),
        )

        self._recovery_states[provider_name] = state

        logger.info(
            f"[{provider_name}] Recovery started, first probe in "
            f"{state.probe_interval}s"
        )

        # 启动恢复任务（如果未运行）
        if not self._running:
            self._running = True
            self._recovery_task = asyncio.create_task(self._recovery_loop())

    async def _recovery_loop(self) -> None:
        """恢复探测循环。"""
        while self._running and self._recovery_states:
            now = utcnow()

            for provider_name, state in list(self._recovery_states.items()):
                if not state.is_recovering:
                    continue

                # 检查是否到达探测时间
                if state.next_probe_time and now >= state.next_probe_time:
                    await self._probe_provider(provider_name, state)

            await asyncio.sleep(5)  # 每 5 秒检查一次

        self._running = False

    async def _probe_provider(
        self, provider_name: str, state: RecoveryState
    ) -> None:
        """探测 Provider 是否恢复。

        Args:
            provider_name: Provider 名称
            state: 恢复状态
        """
        provider = self._registry.get_provider(provider_name)
        if not provider:
            logger.warning(f"[{provider_name}] Provider not found, stopping recovery")
            del self._recovery_states[provider_name]
            return

        logger.debug(f"[{provider_name}] Recovery probe #{state.consecutive_probes + 1}")

        try:
            health_ok = await asyncio.wait_for(
                provider.health_check(),
                timeout=provider.config.health_check_timeout,
            )
        except Exception as e:
            logger.warning(f"[{provider_name}] Recovery probe failed: {e}")
            health_ok = False

        state.last_probe_time = utcnow()

        if health_ok:
            state.consecutive_probes += 1
            logger.info(
                f"[{provider_name}] Recovery probe success "
                f"({state.consecutive_probes}/{self._recovery_config.recovery_threshold})"
            )

            # 检查是否达到恢复阈值
            if state.consecutive_probes >= self._recovery_config.recovery_threshold:
                await self._complete_recovery(provider, state)
        else:
            # 探测失败：重置计数，增加退避间隔
            state.consecutive_probes = 0
            state.probe_interval = min(
                int(state.probe_interval * self._recovery_config.probe_backoff_multiplier),
                self._recovery_config.probe_interval_max,
            )

            logger.warning(
                f"[{provider_name}] Recovery probe failed, "
                f"next probe in {state.probe_interval}s"
            )

        # 设置下次探测时间
        state.next_probe_time = utcnow() + timedelta(seconds=state.probe_interval)

    async def _complete_recovery(
        self, provider: BaseProvider, state: RecoveryState
    ) -> None:
        """完成恢复：将 Provider 恢复为可用状态。

        Args:
            provider: Provider 实例
            state: 恢复状态
        """
        provider_name = provider.name

        # 防抖动检查
        if not self._anti_flapping.can_change_state(provider_name):
            logger.debug(f"[{provider_name}] Recovery blocked by anti-flapping guard")
            return

        # 恢复状态
        provider.set_status(ProviderLifecycleStatus.READY)
        provider.set_health_state(HealthState.ONLINE)

        # 重置连续失败计数
        provider._consecutive_failures = 0
        provider._consecutive_successes = 0

        # 记录状态变更
        self._anti_flapping.record_state_change(provider_name)

        # 清除恢复状态
        del self._recovery_states[provider_name]

        logger.info(f"[{provider_name}] Recovery complete, provider is ONLINE")

        # 记录恢复事件
        self._record_failover_event(
            data_type="recovery",
            original_provider=provider_name,
            reason="recovered",
            error_detail="Provider recovered after probing",
            recovery_action="restore_provider",
        )

    # ---- 事件记录 ----

    def _record_failover_event(
        self,
        data_type: str,
        original_provider: str,
        reason: str,
        error_detail: str,
        latency_ms: float = 0.0,
        retry_count: int = 0,
        new_provider: str | None = None,
        recovery_action: str = "auto_failover",
    ) -> None:
        """记录 Failover 事件。"""
        event = FailoverEvent(
            event_id=uuid4(),
            timestamp=utcnow(),
            data_type=data_type,
            original_provider=original_provider,
            new_provider=new_provider,
            reason=reason,
            error_detail=error_detail,
            recovery_action=recovery_action,
            retry_count=retry_count,
            latency_ms=latency_ms,
            is_all_failed=new_provider is None,
        )

        self._failover_events.append(event)

        # 限制内存中的事件数量
        if len(self._failover_events) > 1000:
            self._failover_events = self._failover_events[-500:]

        logger.debug(
            f"Failover event: {original_provider} -> {new_provider or 'NONE'} "
            f"(reason={reason})"
        )

    def get_failover_events(self, limit: int = 100) -> list[FailoverEvent]:
        """获取最近的 Failover 事件。"""
        return sorted(
            self._failover_events, key=lambda e: e.timestamp, reverse=True
        )[:limit]

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动故障切换引擎。"""
        if self._running:
            return

        self._running = True
        self._recovery_task = asyncio.create_task(self._recovery_loop())
        logger.info("FailoverEngine started")

    async def stop(self) -> None:
        """停止故障切换引擎。"""
        self._running = False

        if self._recovery_task:
            self._recovery_task.cancel()
            try:
                await self._recovery_task
            except asyncio.CancelledError:
                pass
            self._recovery_task = None

        logger.info("FailoverEngine stopped")


__all__ = [
    "AntiFlappingConfig",
    "AntiFlappingGuard",
    "ErrorPolicy",
    "FailoverEngine",
    "RecoveryConfig",
    "RecoveryState",
]
