"""Failover 故障切换集成测试（全部使用内存 Stub，无网络 / DB / Redis 依赖）。

覆盖：
- HTTP 错误分类：429/401/403/5xx/超时/DNS/TLS -> ErrorType（NetworkErrorClassifier 纯函数）；
- ErrorPolicy 策略映射：RATE_LIMIT / AUTH_ERROR / DNS_ERROR / DATA_FORMAT / TIMEOUT ...；
- ProviderManager.execute_with_failover：
  * 主 Provider 失败 -> 备用自动接管（is_failover 标记 + 事件记录）；
  * 全部 Provider 失败 -> 有缓存返回 STALE 数据（绝不生成假数据）、无缓存返回 INVALID；
  * 不可用 Provider 跳过、方法缺失跳过、异常兜底 UNKNOWN 分类；
- FailoverEngine.fetch_with_failover：重试次数、标记离线、防抖冷却跳过；
- Recovery Threshold：连续成功 N 次探测恢复 Primary，失败重置 + 指数退避；
- AntiFlappingGuard：观察窗口、冷却期、指数退避、上限、重置。
"""

from __future__ import annotations

import socket
import ssl
from datetime import datetime, timedelta

import httpx
import pytest

from app.providers.base.config import ProviderConfig, RetryConfig
from app.providers.base.provider import BaseProvider
from app.providers.base.registry import ProviderRegistry
from app.providers.base.types import (
    ErrorType,
    FetchResult,
    HealthState,
    ProviderLifecycleStatus,
    ProviderMetadata,
    QualityStatus,
)
from app.providers.failover import (
    AntiFlappingConfig,
    AntiFlappingGuard,
    FailoverEngine,
    RecoveryConfig,
    RecoveryState,
)
from app.providers.manager import ProviderManager
from app.providers.network import NetworkErrorClassifier


class StubProvider(BaseProvider):
    """行为可控的内存 Provider（不发起任何网络请求）。

    fail_with 不为 None 时，get_current_price 永远返回对应错误类型的失败结果。
    """

    def __init__(
        self,
        name: str,
        *,
        category: str = "market",
        priority: int = 10,
        fail_with: ErrorType | None = None,
        data: dict | None = None,
        health_ok: bool = True,
    ):
        config = ProviderConfig(name=name, category=category, priority=priority)
        super().__init__(config)
        # 直接置为可用状态（跳过 initialize 的 HTTP 客户端创建）
        self._status = ProviderLifecycleStatus.READY
        self._health_state = HealthState.ONLINE
        self._fail_with = fail_with
        self._data = data or {"price": 100000.0, "source": name}
        self._health_ok = health_ok
        self.call_count = 0

    async def health_check(self) -> bool:
        return self._health_ok

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(name=self.name, category=self.category)

    def get_supported_data_types(self) -> list[str]:
        return ["current_price"]

    async def get_current_price(self, **kwargs) -> FetchResult:
        self.call_count += 1
        if self._fail_with is not None:
            return FetchResult(
                success=False,
                error=f"{self._fail_with.value} simulated",
                error_type=self._fail_with,
                provider_name=self.name,
            )
        return FetchResult(
            success=True, data=dict(self._data), provider_name=self.name
        )


class NoFetchStubProvider(BaseProvider):
    """不实现 get_current_price 的 Stub（用于方法缺失路径）。"""

    def __init__(self, name: str, *, priority: int = 10):
        config = ProviderConfig(name=name, category="market", priority=priority)
        super().__init__(config)
        self._status = ProviderLifecycleStatus.READY
        self._health_state = HealthState.ONLINE

    async def health_check(self) -> bool:
        return True

    def get_metadata(self) -> ProviderMetadata:
        return ProviderMetadata(name=self.name, category=self.category)

    def get_supported_data_types(self) -> list[str]:
        return []


# ===========================================================================
# 共享 fixtures（模块级：registry 单例隔离 + 引擎收尾清理）
# ===========================================================================
@pytest.fixture
def registry():
    """独立的 ProviderRegistry（重置单例，避免测试间污染）。"""
    ProviderRegistry.reset_instance()
    reg = ProviderRegistry()
    yield reg
    ProviderRegistry.reset_instance()


@pytest.fixture
def manager(registry):
    """ProviderManager（基于独立 registry）。"""
    return ProviderManager(registry)


@pytest.fixture
async def engine(registry):
    """FailoverEngine（防抖关闭以便恢复测试，收尾清理探测任务）。"""
    eng = FailoverEngine(
        registry,
        recovery_config=RecoveryConfig(recovery_threshold=3, probe_interval_initial=30),
        anti_flapping_config=AntiFlappingConfig(enabled=False),
    )
    yield eng
    await eng.stop()


# ===========================================================================
# HTTP 错误分类（NetworkErrorClassifier 纯函数）
# ===========================================================================
class TestHTTPErrorClassification:
    """HTTP 状态码与网络异常到 ErrorType 的分类映射。"""

    @pytest.mark.parametrize(
        "status,expected",
        [
            (429, ErrorType.RATE_LIMIT),
            (401, ErrorType.AUTH_ERROR),
            (403, ErrorType.AUTH_ERROR),
            (500, ErrorType.SERVER_ERROR),
            (502, ErrorType.SERVER_ERROR),
            (503, ErrorType.SERVER_ERROR),
            (504, ErrorType.SERVER_ERROR),
            (404, ErrorType.NETWORK_ERROR),
            (418, ErrorType.NETWORK_ERROR),
        ],
    )
    def test_classify_status_code(self, status, expected):
        assert NetworkErrorClassifier.classify_status_code(status) == expected

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectTimeout("connect timeout"),
            httpx.ReadTimeout("read timeout"),
            httpx.WriteTimeout("write timeout"),
            httpx.PoolTimeout("pool timeout"),
        ],
    )
    def test_timeout_exceptions_classified(self, exc):
        assert NetworkErrorClassifier.classify(exc) == ErrorType.TIMEOUT

    def test_dns_error_from_gaierror(self):
        err = socket.gaierror("Name or service not known")
        assert NetworkErrorClassifier.classify(err) == ErrorType.DNS_ERROR

    def test_dns_error_from_connect_error_with_gaierror_cause(self):
        inner = socket.gaierror(11001, "getaddrinfo failed")
        outer = httpx.ConnectError("All connection attempts failed")
        outer.__cause__ = inner
        assert NetworkErrorClassifier.classify(outer) == ErrorType.DNS_ERROR

    def test_connection_refused_with_oserror_cause(self):
        outer = httpx.ConnectError("Connection refused")
        outer.__cause__ = OSError("Connection refused")
        assert NetworkErrorClassifier.classify(outer) == ErrorType.CONNECTION_REFUSED

    def test_tls_error(self):
        err = ssl.SSLCertVerificationError("certificate verify failed")
        assert NetworkErrorClassifier.classify(err) == ErrorType.TLS_ERROR

    def test_decoding_error_is_data_format(self):
        assert NetworkErrorClassifier.classify(
            httpx.DecodingError("bad json")
        ) == ErrorType.DATA_FORMAT

    def test_unknown_exception(self):
        assert NetworkErrorClassifier.classify(ValueError("unrelated")) == ErrorType.UNKNOWN


# ===========================================================================
# ErrorPolicy 策略映射
# ===========================================================================
class TestErrorPolicyMapping:
    """错误类型 -> 处理策略映射（retry / failover / notify_admin ...）。"""

    @pytest.mark.parametrize(
        "error_type,expected",
        [
            # 429：可重试但不切换（限流退避）
            (ErrorType.RATE_LIMIT, {"retry": True, "failover": False, "retry_max": 2}),
            # 认证错误：不重试、通知管理员
            (ErrorType.AUTH_ERROR, {"retry": False, "failover": True, "notify_admin": True}),
            # DNS 错误：不重试、标记网络故障
            (
                ErrorType.DNS_ERROR,
                {"retry": False, "failover": True, "mark_network_issue": True},
            ),
            # 服务端错误：重试 + 切换
            (ErrorType.SERVER_ERROR, {"retry": True, "failover": True, "retry_max": 3}),
            # 超时：重试 1 次 + 切换
            (ErrorType.TIMEOUT, {"retry": True, "failover": True, "retry_max": 1}),
            # 数据格式错误：不重试、禁用 Provider
            (
                ErrorType.DATA_FORMAT,
                {"retry": False, "failover": True, "action": "disable_provider"},
            ),
            # 网络错误：重试 + 切换
            (ErrorType.NETWORK_ERROR, {"retry": True, "failover": True}),
        ],
    )
    def test_policy_fields(self, engine, error_type, expected):
        policy = engine._error_policies[error_type]
        for key, value in expected.items():
            assert getattr(policy, key) == value, f"{error_type}.{key} != {value}"

    @pytest.mark.parametrize(
        "error_type,should_offline",
        [
            (ErrorType.DNS_ERROR, True),
            (ErrorType.AUTH_ERROR, True),
            (ErrorType.DATA_FORMAT, True),
            (ErrorType.NETWORK_ERROR, True),
            (ErrorType.TIMEOUT, False),
            (ErrorType.SERVER_ERROR, False),
            (ErrorType.RATE_LIMIT, False),
            (ErrorType.DATA_QUALITY, False),
            (None, False),
        ],
    )
    def test_should_mark_offline(self, engine, error_type, should_offline):
        assert engine._should_mark_offline(error_type) is should_offline


# ===========================================================================
# ProviderManager.execute_with_failover
# ===========================================================================
class TestProviderManagerFailover:
    """主备切换与降级缓存。"""

    async def test_primary_failure_falls_back_to_secondary(self, registry, manager):
        """主 Provider 失败 -> 备用自动接管。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.RATE_LIMIT)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"
        assert result.is_failover is True
        assert result.data["source"] == "secondary"
        assert primary.call_count == 1
        assert secondary.call_count == 1

    async def test_failover_event_recorded(self, registry, manager):
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.RATE_LIMIT)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        await manager.execute_with_failover("market", "get_current_price")

        events = manager.get_failover_events()
        assert len(events) >= 1
        assert events[0].original_provider == "primary"
        assert events[0].reason == "rate_limit"

    async def test_unavailable_provider_is_skipped(self, registry, manager):
        """不可用（OFFLINE）的 Provider 直接跳过。"""
        primary = StubProvider("primary", priority=10)
        primary.set_status(ProviderLifecycleStatus.OFFLINE)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"
        assert result.is_failover is False  # 第一次有效尝试即成功
        assert primary.call_count == 0

    async def test_success_updates_cache(self, registry, manager):
        provider = StubProvider("primary", priority=10)
        registry.register(provider)

        await manager.execute_with_failover("market", "get_current_price")

        assert "market:get_current_price" in manager._cache

    async def test_all_failed_with_cache_returns_stale(self, registry, manager):
        """全部失败 -> 返回缓存数据并标记 STALE（绝不生成假数据）。"""
        primary = StubProvider("primary", priority=10)
        registry.register(primary)

        # 第一次成功 -> 写入缓存
        ok = await manager.execute_with_failover("market", "get_current_price")
        assert ok.success is True
        cached_data = ok.data

        # 之后全部失败
        primary._fail_with = ErrorType.SERVER_ERROR

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is True  # 降级成功
        assert result.is_stale is True
        assert result.quality_status == QualityStatus.STALE
        assert result.provider_name == "cache"
        assert result.data == cached_data  # 是缓存的真实数据，不是伪造
        assert result.metadata["is_degraded"] is True
        assert result.error_type is None

    async def test_all_failed_without_cache_returns_invalid(self, registry, manager):
        """全部失败且无缓存 -> INVALID，绝不生成假数据。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.TIMEOUT)
        secondary = StubProvider("secondary", priority=20, fail_with=ErrorType.TIMEOUT)
        registry.register(primary)
        registry.register(secondary)

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is False
        assert result.data is None
        assert result.quality_status == QualityStatus.INVALID
        assert result.error_type == ErrorType.TIMEOUT
        assert result.metadata["all_providers_failed"] is True

    async def test_no_providers_registered_returns_invalid(self, registry, manager):
        """未注册任何 Provider -> INVALID 降级。"""
        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is False
        assert result.data is None
        assert result.quality_status == QualityStatus.INVALID
        assert "No providers" in (result.error or "")

    async def test_provider_exception_falls_back_to_next(self, registry, manager):
        """Provider 方法抛异常 -> 按 UNKNOWN 错误继续切换下一个。"""
        primary = StubProvider("primary", priority=10)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        async def _raise(**kwargs):
            raise ValueError("boom")

        primary.get_current_price = _raise

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"
        assert result.is_failover is True

    async def test_missing_method_is_skipped(self, registry, manager):
        """Provider 缺少请求的方法 -> 跳过继续下一个。"""
        primary = NoFetchStubProvider("no_fetch", priority=10)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await manager.execute_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"


# ===========================================================================
# FailoverEngine.fetch_with_failover（重试 + 离线标记 + 防抖）
# ===========================================================================
class TestFailoverEngineFetch:
    """FailoverEngine 请求路由行为。"""

    def _fast_retry(self, provider: StubProvider) -> None:
        """设置极短退避，避免测试 sleep 过久。"""
        provider.config.retry = RetryConfig(count=3, backoff_factor=0.01)

    async def test_dns_error_no_retry_failover_and_mark_offline(self, registry, engine):
        """DNS 错误：不重试、切换备用、主 Provider 标记 OFFLINE 并启动恢复。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.DNS_ERROR)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"
        # DNS 错误不重试
        assert primary.call_count == 1
        # DNS 在离线标记集合中
        assert primary.status == ProviderLifecycleStatus.OFFLINE
        assert primary.health_state == HealthState.OFFLINE
        # 恢复探测已注册
        assert "primary" in engine._recovery_states

    async def test_auth_error_marks_offline(self, registry, engine):
        """403/401 认证错误：不重试、标记离线、切换备用。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.AUTH_ERROR)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.provider_name == "secondary"
        assert primary.call_count == 1  # AUTH 不重试
        assert primary.status == ProviderLifecycleStatus.OFFLINE

    async def test_timeout_retries_once_then_fails_over(self, registry, engine):
        """超时：按策略重试 1 次（共 2 次调用）后切换备用；不标记离线。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.TIMEOUT)
        self._fast_retry(primary)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.provider_name == "secondary"
        assert primary.call_count == 2  # 初始 1 次 + 重试 1 次
        # 超时不在离线标记集合
        assert primary.status == ProviderLifecycleStatus.READY

    async def test_rate_limit_retries_twice_then_fails_over(self, registry, engine):
        """429 限流：重试 2 次（共 3 次调用）后切换备用；不标记离线。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.RATE_LIMIT)
        self._fast_retry(primary)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.provider_name == "secondary"
        assert primary.call_count == 3
        assert primary.status == ProviderLifecycleStatus.READY

    async def test_all_failed_returns_last_error(self, registry, engine):
        """全部失败 -> 返回最后一个 Provider 的失败结果。"""
        primary = StubProvider("primary", priority=10, fail_with=ErrorType.DNS_ERROR)
        secondary = StubProvider("secondary", priority=20, fail_with=ErrorType.TIMEOUT)
        self._fast_retry(secondary)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.success is False
        assert result.provider_name == "secondary"
        assert result.error_type == ErrorType.TIMEOUT

    async def test_no_providers_returns_invalid(self, registry, engine):
        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.success is False
        assert result.quality_status == QualityStatus.INVALID

    async def test_exception_classified_unknown(self, registry, engine):
        """未预期异常 -> UNKNOWN 分类，继续切换备用。"""
        primary = StubProvider("primary", priority=10)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        async def _raise(**kwargs):
            raise RuntimeError("unexpected")

        primary.get_current_price = _raise

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "secondary"

    async def test_cooldown_provider_skipped(self, registry):
        """防抖冷却期中的 Provider 被跳过。"""
        engine = FailoverEngine(
            registry,
            anti_flapping_config=AntiFlappingConfig(enabled=True),
        )
        primary = StubProvider("primary", priority=10)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        # 手动注入冷却期
        engine._anti_flapping._cooldown["primary"] = datetime.utcnow() + timedelta(
            seconds=300
        )

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.provider_name == "secondary"
        assert primary.call_count == 0
        await engine.stop()

    async def test_success_on_primary_not_marked_failover(self, registry, engine):
        """首个 Provider 成功 -> is_failover=False。"""
        primary = StubProvider("primary", priority=10)
        secondary = StubProvider("secondary", priority=20)
        registry.register(primary)
        registry.register(secondary)

        result = await engine.fetch_with_failover("market", "get_current_price")

        assert result.success is True
        assert result.provider_name == "primary"
        assert result.is_failover is False


# ===========================================================================
# Recovery Threshold 恢复机制
# ===========================================================================
class TestRecoveryThreshold:
    """连续成功 N 次探测恢复 Primary。"""

    async def test_probe_success_restores_provider_after_threshold(
        self, registry, engine
    ):
        """连续 3 次探测成功 -> 恢复 READY/ONLINE，计数清零，状态移除。"""
        provider = StubProvider("primary", priority=10, health_ok=True)
        provider.set_status(ProviderLifecycleStatus.OFFLINE)
        provider._consecutive_failures = 5
        registry.register(provider)

        state = RecoveryState(
            provider_name="primary", is_recovering=True, probe_interval=30
        )
        engine._recovery_states["primary"] = state

        await engine._probe_provider("primary", state)  # 1/3
        assert state.consecutive_probes == 1
        assert "primary" in engine._recovery_states

        await engine._probe_provider("primary", state)  # 2/3
        assert state.consecutive_probes == 2
        assert "primary" in engine._recovery_states  # 未达阈值仍恢复中

        await engine._probe_provider("primary", state)  # 3/3 -> 达到阈值
        assert "primary" not in engine._recovery_states
        assert provider.status == ProviderLifecycleStatus.READY
        assert provider.health_state == HealthState.ONLINE
        assert provider._consecutive_failures == 0

    async def test_probe_failure_resets_counter_and_backs_off(self, registry, engine):
        """探测失败 -> 计数归零，探测间隔指数退避（30 -> 60 -> 120）。"""
        provider = StubProvider("primary", priority=10, health_ok=False)
        registry.register(provider)

        state = RecoveryState(
            provider_name="primary", is_recovering=True, probe_interval=30
        )
        engine._recovery_states["primary"] = state

        await engine._probe_provider("primary", state)
        assert state.consecutive_probes == 0
        assert state.probe_interval == 60

        await engine._probe_provider("primary", state)
        assert state.consecutive_probes == 0
        assert state.probe_interval == 120

    async def test_mixed_probes_require_consecutive_success(self, registry, engine):
        """成功-失败-成功：失败打断连续计数，需重新累计。"""
        provider = StubProvider("primary", priority=10, health_ok=True)
        registry.register(provider)

        state = RecoveryState(
            provider_name="primary", is_recovering=True, probe_interval=30
        )
        engine._recovery_states["primary"] = state

        await engine._probe_provider("primary", state)
        assert state.consecutive_probes == 1

        provider._health_ok = False
        await engine._probe_provider("primary", state)
        assert state.consecutive_probes == 0  # 中断重置

        provider._health_ok = True
        await engine._probe_provider("primary", state)
        assert state.consecutive_probes == 1  # 重新累计
        assert "primary" in engine._recovery_states  # 未恢复

    async def test_start_recovery_registers_state_and_task(self, engine):
        """_start_recovery：注册恢复状态 + 启动后台探测任务（幂等）。"""
        engine._start_recovery("primary")
        state = engine._recovery_states["primary"]
        assert state.is_recovering is True
        assert state.next_probe_time is not None
        assert engine._running is True
        assert engine._recovery_task is not None

        # 重复调用幂等：不重建状态
        first_state = engine._recovery_states["primary"]
        engine._start_recovery("primary")
        assert engine._recovery_states["primary"] is first_state


# ===========================================================================
# AntiFlappingGuard 防抖动
# ===========================================================================
class TestAntiFlappingGuard:
    """Provider 状态频繁切换的防抖保护。"""

    @staticmethod
    def _guard(**overrides) -> AntiFlappingGuard:
        config = AntiFlappingConfig(
            enabled=True, min_observation_window=300, cooldown_period=300, **overrides
        )
        return AntiFlappingGuard(config)

    def test_disabled_guard_always_allows(self):
        guard = AntiFlappingGuard(AntiFlappingConfig(enabled=False))
        guard.record_state_change("a")
        assert guard.can_change_state("a") is True

    def test_observation_window_blocks_rapid_flip(self):
        guard = self._guard()
        assert guard.can_change_state("a") is True
        guard.record_state_change("a")
        # 观察窗口（300s）内禁止再次变更
        assert guard.can_change_state("a") is False

    def test_flap_count_increases_cooldown(self):
        guard = self._guard()
        guard.record_state_change("a")
        guard.record_state_change("a")
        guard.record_state_change("a")  # flap=3 -> 300 * 2^2 = 1200s
        assert guard.get_flap_count("a") == 3
        remaining = (guard._cooldown["a"] - datetime.utcnow()).total_seconds()
        assert 1100 <= remaining <= 1200

    def test_cooldown_capped_at_max(self):
        guard = self._guard()
        for _ in range(12):
            guard.record_state_change("a")
        remaining = (guard._cooldown["a"] - datetime.utcnow()).total_seconds()
        assert remaining <= 3600  # 不超过 max_cooldown

    def test_reset_flap_count_clears_cooldown(self):
        guard = self._guard()
        guard.record_state_change("a")
        assert guard.is_in_cooldown("a") is True
        guard.reset_flap_count("a")
        assert guard.get_flap_count("a") == 0
        assert guard.is_in_cooldown("a") is False

    def test_cooldown_expires(self):
        guard = self._guard()
        guard._cooldown["a"] = datetime.utcnow() - timedelta(seconds=1)
        assert guard.is_in_cooldown("a") is False
