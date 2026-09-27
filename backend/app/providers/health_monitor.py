"""HealthMonitor — Provider 健康监控器。

职责：
1. 定期主动健康探测（每个 Provider 独立频率）
2. 被动采集请求指标（实时）
3. 计算综合健康评分（8 维度加权移动平均）
4. 驱动状态机转换（基于评分和错误类型）
5. 触发优先级调整
6. 更新监控面板数据
7. 状态变更事件发布（Redis Pub/Sub）
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger

from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    ErrorType,
    HealthState,
    ProviderLifecycleStatus,
    ProviderMetrics,
)
from app.utils.datetime_utils import utcnow


@dataclass
class ScoringWeights:
    """评分维度权重配置。"""

    accuracy: float = 0.15
    latency: float = 0.15
    stability: float = 0.20
    success_rate: float = 0.20
    completeness: float = 0.10
    reliability: float = 0.10
    timeliness: float = 0.05
    reachability: float = 0.05


@dataclass
class ScoringParams:
    """评分参数配置。"""

    latency_baseline_ms: float = 100.0
    latency_scale_ms: float = 50.0
    stability_target_hours: float = 168.0  # 7 天
    reliability_penalty_per_failure: float = 10.0
    accuracy_deviation_penalty: float = 1000.0
    timeliness_max_delay_seconds: float = 300.0
    slow_latency_threshold_ms: float = 3000.0
    degraded_score_threshold: float = 75.0
    offline_score_threshold: float = 20.0
    consecutive_failure_threshold: int = 3
    consecutive_success_threshold: int = 3


class WeightedMovingAverage:
    """加权移动平均 — 近期数据权重更高。"""

    def __init__(self, window_size: int = 100, decay_factor: float = 0.95):
        self.window_size = window_size
        self.decay_factor = decay_factor
        self._values: list[tuple[datetime, float]] = []

    def add(self, value: float, timestamp: datetime | None = None) -> None:
        """添加数据点。"""
        ts = timestamp or utcnow()
        self._values.append((ts, value))
        if len(self._values) > self.window_size:
            self._values.pop(0)

    def compute(self) -> float:
        """计算加权移动平均。"""
        if not self._values:
            return 0.0

        n = len(self._values)
        weighted_sum = 0.0
        weight_total = 0.0

        for i, (ts, value) in enumerate(self._values):
            weight = self.decay_factor ** (n - i - 1)
            weighted_sum += value * weight
            weight_total += weight

        return weighted_sum / weight_total if weight_total > 0 else 0.0

    @property
    def count(self) -> int:
        return len(self._values)


class TimeDecayScorer:
    """时间衰减评分器。"""

    def __init__(self, half_life_minutes: float = 60.0):
        self.half_life_seconds = half_life_minutes * 60

    def decay_factor(self, elapsed_seconds: float) -> float:
        """计算衰减因子 (0-1)。"""
        if elapsed_seconds <= 0:
            return 1.0
        return math.pow(0.5, elapsed_seconds / self.half_life_seconds)

    def update_score(
        self, old_score: float, new_score: float, elapsed_seconds: float
    ) -> float:
        """更新时间衰减评分。"""
        decay = self.decay_factor(elapsed_seconds)
        return old_score * decay + new_score * (1 - decay)


@dataclass
class ProviderScoreState:
    """Provider 评分状态（内部使用）。"""

    provider_name: str
    current_score: float = 0.0
    last_update_time: datetime | None = None
    latency_wma: WeightedMovingAverage = field(default_factory=WeightedMovingAverage)
    success_wma: WeightedMovingAverage = field(default_factory=WeightedMovingAverage)
    dimension_scores: dict[str, float] = field(default_factory=dict)


class HealthMonitor:
    """Provider 健康监控器。

    核心职责：
    1. 定期健康检查
    2. 综合评分计算（8 维度）
    3. 状态机驱动
    4. 优先级调整触发

    Usage:
        monitor = HealthMonitor(registry, manager)
        await monitor.start()  # 启动后台监控任务
        await monitor.stop()   # 停止监控
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        manager: Any = None,  # ProviderManager（避免循环导入）
        weights: ScoringWeights | None = None,
        params: ScoringParams | None = None,
        check_interval: int = 60,
    ):
        """初始化健康监控器。

        Args:
            registry: Provider 注册中心
            manager: Provider 管理器（用于更新优先级）
            weights: 评分权重配置
            params: 评分参数配置
            check_interval: 健康检查间隔（秒）
        """
        self._registry = registry
        self._manager = manager
        self._weights = weights or ScoringWeights()
        self._params = params or ScoringParams()
        self._check_interval = check_interval

        self._score_states: dict[str, ProviderScoreState] = {}
        self._decay_scorer = TimeDecayScorer(half_life_minutes=60.0)
        self._monitor_task: asyncio.Task | None = None
        self._running = False

        # 状态变更回调
        self._state_change_callbacks: list = []

    # ---- 生命周期 ----

    async def start(self) -> None:
        """启动后台健康监控任务。"""
        if self._running:
            logger.warning("HealthMonitor is already running")
            return

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(f"HealthMonitor started (interval={self._check_interval}s)")

    async def stop(self) -> None:
        """停止后台健康监控任务。"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("HealthMonitor stopped")

    async def _monitor_loop(self) -> None:
        """后台监控循环。"""
        while self._running:
            try:
                await self.run_periodic_check()
            except Exception as e:
                logger.exception(f"Error in health monitor loop: {e}")

            await asyncio.sleep(self._check_interval)

    # ---- 核心方法 ----

    async def run_periodic_check(self) -> None:
        """执行一次完整的健康检查周期。

        对所有已启用的 Provider：
        1. 执行主动健康探测
        2. 采集运行时指标
        3. 计算综合评分
        4. 评估状态转换
        5. 更新优先级队列
        """
        providers = self._registry.get_providers(enabled_only=True)

        for provider in providers:
            try:
                await self._check_provider(provider)
            except Exception as e:
                logger.exception(f"Error checking provider '{provider.name}': {e}")

    async def _check_provider(self, provider: BaseProvider) -> None:
        """检查单个 Provider 的健康状态。

        Args:
            provider: Provider 实例
        """
        # 跳过已禁用的 Provider
        if provider.status == ProviderLifecycleStatus.DISABLED:
            return

        # 执行健康探测
        provider.set_status(ProviderLifecycleStatus.HEALTH_CHECKING)
        health_ok = False

        try:
            health_ok = await asyncio.wait_for(
                provider.health_check(),
                timeout=provider.config.health_check_timeout,
            )
        except TimeoutError:
            logger.warning(f"[{provider.name}] Health check timed out")
            health_ok = False
        except Exception as e:
            logger.error(f"[{provider.name}] Health check error: {e}")
            health_ok = False

        # 采集指标
        metrics = provider.get_metrics()

        # 计算评分
        score = self.compute_score(provider.name, metrics)
        score = self._update_score_with_decay(provider.name, score)

        # 评估状态转换
        new_state = self._evaluate_state(provider, score, metrics, health_ok)

        if new_state != provider.health_state:
            await self._transition_state(provider, new_state, score)

        # 更新优先级（如果 manager 存在）
        if self._manager and hasattr(self._manager, "update_health_score"):
            self._manager.update_health_score(provider.name, score)

    def compute_score(self, provider_name: str, metrics: ProviderMetrics) -> float:
        """计算 Provider 的综合健康评分 (0-100)。

        8 维度加权求和：
        - 数据准确性 (15%)
        - 响应速度 (15%)
        - 稳定性 (20%)
        - 成功率 (20%)
        - 数据完整性 (10%)
        - 历史故障率 (10%)
        - 数据时效性 (5%)
        - 网络可达性 (5%)

        Args:
            provider_name: Provider 名称
            metrics: 运行时指标

        Returns:
            综合评分 (0-100)
        """
        dimension_scores = {
            "accuracy": self._score_accuracy(metrics),
            "latency": self._score_latency(metrics),
            "stability": self._score_stability(metrics),
            "success_rate": self._score_success_rate(metrics),
            "completeness": self._score_completeness(metrics),
            "reliability": self._score_reliability(metrics),
            "timeliness": self._score_timeliness(metrics),
            "reachability": self._score_reachability(metrics),
        }

        # 加权求和
        final_score = sum(
            dimension_scores[dim] * getattr(self._weights, dim)
            for dim in dimension_scores
        )

        # 保存到状态
        if provider_name in self._score_states:
            self._score_states[provider_name].dimension_scores = dimension_scores

        return max(0.0, min(100.0, final_score))

    # ---- 维度评分计算 ----

    def _score_accuracy(self, metrics: ProviderMetrics) -> float:
        """数据准确性评分（基于交叉验证偏差）。"""
        # 简化实现：假设准确性为 1.0（需要交叉验证模块支持）
        accuracy = metrics.data_accuracy
        return max(0.0, min(100.0, accuracy * 100))

    def _score_latency(self, metrics: ProviderMetrics) -> float:
        """响应速度评分。"""
        if metrics.avg_latency_ms <= 0:
            return 50.0  # 无数据时给中等分

        baseline = self._params.latency_baseline_ms
        scale = self._params.latency_scale_ms

        # 公式：100 - (latency - baseline) / scale
        score = 100.0 - (metrics.avg_latency_ms - baseline) / scale
        return max(0.0, min(100.0, score))

    def _score_stability(self, metrics: ProviderMetrics) -> float:
        """稳定性评分（基于连续运行时间）。"""
        target_hours = self._params.stability_target_hours
        uptime_hours = metrics.uptime_seconds / 3600.0

        score = min(100.0, (uptime_hours / target_hours) * 100)
        return score

    def _score_success_rate(self, metrics: ProviderMetrics) -> float:
        """成功率评分。"""
        if metrics.total_requests == 0:
            return 100.0  # 无请求时给满分

        success_rate = (metrics.total_requests - metrics.total_failures) / metrics.total_requests
        return success_rate * 100

    def _score_completeness(self, metrics: ProviderMetrics) -> float:
        """数据完整性评分。"""
        completeness = metrics.data_completeness
        return max(0.0, min(100.0, completeness * 100))

    def _score_reliability(self, metrics: ProviderMetrics) -> float:
        """历史故障率评分（反向映射）。"""
        penalty = self._params.reliability_penalty_per_failure
        failure_count = metrics.failure_count_7d

        score = max(0.0, 100.0 - failure_count * penalty)
        return score

    def _score_timeliness(self, metrics: ProviderMetrics) -> float:
        """数据时效性评分。"""
        max_delay = self._params.timeliness_max_delay_seconds
        delay = metrics.data_delay_seconds

        if delay <= 0:
            return 100.0

        score = max(0.0, 100.0 - (delay / max_delay) * 100)
        return score

    def _score_reachability(self, metrics: ProviderMetrics) -> float:
        """网络可达性评分。"""
        if metrics.network_reachable:
            return metrics.reachability_score * 100
        return 0.0

    # ---- 评分衰减 ----

    def _update_score_with_decay(self, provider_name: str, new_score: float) -> float:
        """应用时间衰减更新最终评分。"""
        now = utcnow()

        if provider_name not in self._score_states:
            self._score_states[provider_name] = ProviderScoreState(
                provider_name=provider_name,
                current_score=new_score,
                last_update_time=now,
            )
            return new_score

        state = self._score_states[provider_name]
        old_score = state.current_score
        last_time = state.last_update_time or now

        elapsed = (now - last_time).total_seconds()
        updated_score = self._decay_scorer.update_score(old_score, new_score, elapsed)

        state.current_score = updated_score
        state.last_update_time = now

        return updated_score

    # ---- 状态机 ----

    def _evaluate_state(
        self,
        provider: BaseProvider,
        score: float,
        metrics: ProviderMetrics,
        health_check_passed: bool,
    ) -> HealthState:
        """评估 Provider 应该处于的健康状态。

        Args:
            provider: Provider 实例
            score: 综合评分
            metrics: 运行时指标
            health_check_passed: 健康检查是否通过

        Returns:
            建议的 HealthState
        """
        # 管理员禁用
        if provider.status == ProviderLifecycleStatus.DISABLED:
            return HealthState.DISABLED

        # 健康检查失败
        if not health_check_passed:
            if metrics.consecutive_failures >= self._params.consecutive_failure_threshold:
                return HealthState.OFFLINE
            return HealthState.NETWORK_ERROR

        # 根据错误类型判断
        if metrics.last_error_type:
            error_type = ErrorType(metrics.last_error_type) if isinstance(
                metrics.last_error_type, str
            ) else metrics.last_error_type

            if error_type == ErrorType.RATE_LIMIT:
                return HealthState.RATE_LIMITED
            if error_type == ErrorType.AUTH_ERROR:
                return HealthState.AUTH_ERROR
            if error_type in (ErrorType.DATA_FORMAT, ErrorType.DATA_QUALITY):
                return HealthState.DATA_ERROR
            if error_type in (
                ErrorType.DNS_ERROR,
                ErrorType.NETWORK_ERROR,
                ErrorType.CONNECTION_REFUSED,
            ):
                return HealthState.NETWORK_ERROR

        # 根据评分判断
        if score < self._params.offline_score_threshold:
            return HealthState.OFFLINE

        if score < self._params.degraded_score_threshold:
            return HealthState.DEGRADED

        # 根据延迟判断
        if metrics.avg_latency_ms > self._params.slow_latency_threshold_ms:
            return HealthState.SLOW

        # 根据连续失败判断
        if metrics.consecutive_failures >= self._params.consecutive_failure_threshold:
            return HealthState.OFFLINE

        # 一切正常
        return HealthState.ONLINE

    async def _transition_state(
        self, provider: BaseProvider, new_state: HealthState, score: float
    ) -> None:
        """执行状态转换。

        Args:
            provider: Provider 实例
            new_state: 新状态
            score: 当前评分
        """
        old_state = provider.health_state

        # 更新健康状态
        provider.set_health_state(new_state)

        # 更新生命周期状态
        if new_state == HealthState.ONLINE:
            provider.set_status(ProviderLifecycleStatus.RUNNING)
        elif new_state == HealthState.OFFLINE:
            provider.set_status(ProviderLifecycleStatus.OFFLINE)
        elif new_state == HealthState.DISABLED:
            provider.set_status(ProviderLifecycleStatus.DISABLED)
        elif new_state in (HealthState.DEGRADED, HealthState.SLOW):
            provider.set_status(ProviderLifecycleStatus.DEGRADED)

        logger.info(
            f"[{provider.name}] State transition: {old_state.value} -> {new_state.value} "
            f"(score={score:.1f})"
        )

        # 触发回调
        for callback in self._state_change_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(provider.name, old_state, new_state, score)
                else:
                    callback(provider.name, old_state, new_state, score)
            except Exception as e:
                logger.error(f"Error in state change callback: {e}")

    # ---- 公开方法 ----

    def get_score(self, provider_name: str) -> float:
        """获取 Provider 的当前评分。"""
        state = self._score_states.get(provider_name)
        return state.current_score if state else 0.0

    def get_score_breakdown(self, provider_name: str) -> dict[str, float]:
        """获取 Provider 的评分维度明细。"""
        state = self._score_states.get(provider_name)
        return state.dimension_scores if state else {}

    def on_state_change(self, callback) -> None:
        """注册状态变更回调。

        Args:
            callback: 回调函数，签名 (provider_name, old_state, new_state, score)
        """
        self._state_change_callbacks.append(callback)

    async def check_provider_now(self, provider_name: str) -> float | None:
        """立即检查指定 Provider（不等待定时任务）。

        Args:
            provider_name: Provider 名称

        Returns:
            评分，未找到 Provider 返回 None
        """
        provider = self._registry.get_provider(provider_name)
        if not provider:
            logger.warning(f"Provider '{provider_name}' not found")
            return None

        await self._check_provider(provider)
        return self.get_score(provider_name)


__all__ = [
    "HealthMonitor",
    "ProviderScoreState",
    "ScoringParams",
    "ScoringWeights",
    "TimeDecayScorer",
    "WeightedMovingAverage",
]
