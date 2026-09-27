"""ProviderService 热重载测试（mock 配置服务，无 DB / 网络）。

覆盖：
- reload_provider：旧实例 shutdown 释放、新实例注册并 initialize、
  优先级队列重建、健康评分状态清理；
- reload 禁用实例：仅摘除不注册；
- apply_config_change：轻量变更（priority/is_enabled/is_locked）免重建、
  重字段变更触发完整 reload。
"""

from __future__ import annotations

import pytest

from app.providers.base.config import ConfigLoader
from app.providers.base.registry import ProviderRegistry
from app.services import provider_service as ps_module
from app.services.provider_service import ProviderService


class FakeProvider:
    """最小 Provider 鸭子类型（registry/manager/热重载所需接口）。"""

    provider_key = "fakeprov"

    def __init__(self, config):
        self.config = config
        self.name = config.name
        self.category = config.category
        self.status = None
        self.initialized = False
        self.shutdown_called = False

    @property
    def priority(self) -> int:
        return self.config.priority

    @property
    def is_enabled(self) -> bool:
        return self.config.enabled

    async def initialize(self):
        self.initialized = True

    async def shutdown(self):
        self.shutdown_called = True

    def set_status(self, status):
        self.status = status

    def enable(self):
        self.config.enabled = True

    def disable(self):
        self.config.enabled = False


class FakeConfigService:
    """替代 ProviderConfigService：始终返回预置配置。"""

    def __init__(self, config):
        self._config = config
        self.build_calls = 0

    async def build_provider_config(self, name):
        self.build_calls += 1
        return self._config


def make_config(name: str = "fakeprov", enabled: bool = True, priority: int = 10):
    return ConfigLoader.parse_provider_config(
        name=name,
        category="market",
        raw={"enabled": enabled, "priority": priority},
        defaults={},
    )


@pytest.fixture
def env():
    """ProviderService + 假配置服务 + 记录版队列重建（不触网络/DB）。"""
    service = ProviderService(enable_cache=False)
    registry = service._registry
    registry.register_class("fakeprov", FakeProvider)
    fake_svc = FakeConfigService(make_config())

    queue_calls: list[str] = []
    service._manager.build_priority_queue = lambda cat: queue_calls.append(cat)

    return service, registry, fake_svc, queue_calls


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例后清空注册中心（ProviderRegistry 为全局单例）。"""
    yield
    ProviderRegistry().clear()


# ===========================================================================
# reload_provider：完整实例重建
# ===========================================================================
class TestReloadProvider:
    async def test_reload_replaces_instance_and_rebuilds_queue(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)

        old = FakeProvider(make_config())
        registry.register(old)
        service._health_monitor._score_states["fakeprov"] = "stale"

        assert await service.reload_provider("fakeprov") is True

        new = registry.get_provider("fakeprov")
        assert new is not old  # registry 中是新实例
        assert new.initialized is True
        assert old.shutdown_called is True  # 旧实例已 shutdown（释放连接池）
        assert "market" in queue_calls  # 类别队列已重建
        assert "fakeprov" not in service._health_monitor._score_states  # 评分状态清理

    async def test_reload_disabled_unregisters_without_registering(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        fake_svc._config = make_config(enabled=False)
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)

        old = FakeProvider(make_config())
        registry.register(old)

        ok = await service.reload_provider("fakeprov")

        assert ok is False  # 禁用不视为注册成功
        assert registry.get_provider("fakeprov") is None  # 已从运行时摘除
        assert old.shutdown_called is True
        assert "market" in queue_calls

    async def test_reload_no_config_returns_false(self, env, monkeypatch):
        """DB 与 YAML 均无配置时返回 False，不影响运行时。"""
        service, registry, fake_svc, _ = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        monkeypatch.setattr(
            service, "_build_config_from_yaml", lambda name: None
        )
        fake_svc._config = None

        assert await service.reload_provider("fakeprov") is False
        assert registry.get_provider("fakeprov") is None


# ===========================================================================
# apply_config_change：轻量变更 vs 完整重载
# ===========================================================================
class TestApplyConfigChange:
    async def test_lightweight_priority_no_rebuild(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        provider = FakeProvider(make_config())
        registry.register(provider)

        result = await service.apply_config_change("fakeprov", {"priority": 5})

        assert result == {"applied": ["priority"], "reloaded": False}
        assert fake_svc.build_calls == 0  # 未触发实例重建
        assert provider.priority == 5  # registry.update_priority 生效
        assert "market" in queue_calls  # 队列已重建

    async def test_lightweight_enable_disable_no_rebuild(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        provider = FakeProvider(make_config())
        registry.register(provider)

        result = await service.apply_config_change("fakeprov", {"is_enabled": False})

        assert result["reloaded"] is False
        assert fake_svc.build_calls == 0
        assert provider.is_enabled is False
        assert "is_enabled" in result["applied"]
        assert "market" in queue_calls

    async def test_lightweight_locked_updates_config(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        provider = FakeProvider(make_config())
        registry.register(provider)

        result = await service.apply_config_change("fakeprov", {"is_locked": True})

        assert result["reloaded"] is False
        assert fake_svc.build_calls == 0
        assert provider.config.locked is True

    async def test_heavy_change_triggers_full_reload(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        provider = FakeProvider(make_config())
        registry.register(provider)

        result = await service.apply_config_change("fakeprov", {"base_url": "https://new"})

        assert result["reloaded"] is True
        assert fake_svc.build_calls == 1  # 触发了完整重载
        assert registry.get_provider("fakeprov") is not provider  # 实例已替换
        assert "base_url" in result["applied"]

    async def test_mixed_change_applies_both_paths(self, env, monkeypatch):
        service, registry, fake_svc, queue_calls = env
        monkeypatch.setattr(ps_module, "get_provider_config_service", lambda: fake_svc)
        provider = FakeProvider(make_config())
        registry.register(provider)

        result = await service.apply_config_change(
            "fakeprov", {"priority": 7, "base_url": "https://new"}
        )

        assert "priority" in result["applied"]
        assert "base_url" in result["applied"]
        assert result["reloaded"] is True
        assert fake_svc.build_calls == 1
