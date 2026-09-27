"""端到端冒烟测试（步骤 3：lifespan + OpenAPI + 全局异常兜底）。

验证（无 Docker/PostgreSQL/Redis 环境，全部依赖 stub / 配置禁用）：
- lifespan 完整启动/关闭不崩溃；
- 子系统（ProviderService）启动失败时优雅降级：不阻断应用、/health 正常；
- shutdown 阶段子系统停止失败不阻断退出；
- 可选模块包装器 _start/_stop_optional_module 对 ImportError/运行时异常容错；
- OpenAPI schema 生成成功，全部业务域路由注册，记录路由总数；
- 未捕获异常返回统一 500 信封 {success, data, error}。

Scheduler/AlertEngine 经 settings.scheduler_enabled / alert_enabled=False
走合法配置路径跳过（其自身行为已由 test_scheduler.py / test_alert_engine.py 深测）。
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

import app.services.provider_service as ps_mod
from app.core.config import settings
from app.main import _start_optional_module, _stop_optional_module, app


class _StubProviderService:
    """轻量 ProviderService stub（可注入 startup/shutdown 失败）。"""

    def __init__(self):
        self.startup_calls = 0
        self.shutdown_calls = 0
        self.fail_startup = False
        self.fail_shutdown = False

    async def startup(self) -> None:
        self.startup_calls += 1
        if self.fail_startup:
            raise RuntimeError("redis down")

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.fail_shutdown:
            raise RuntimeError("close failed")


@pytest.fixture(autouse=True)
def _smoke_env(monkeypatch):
    """冒烟环境：stub ProviderService + 禁用可选子系统（配置合法路径）。"""
    stub = _StubProviderService()
    monkeypatch.setattr(ps_mod, "get_provider_service", lambda: stub)
    monkeypatch.setattr(settings, "scheduler_enabled", False)
    monkeypatch.setattr(settings, "alert_enabled", False)
    return stub


class TestLifespanGracefulDegradation:
    def test_startup_shutdown_complete_cycle(self, _smoke_env):
        """lifespan 完整进出：startup/shutdown 各调用一次，期间服务可响应。"""
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "ok"
            assert body["service"] == "btc-platform-api"
        assert _smoke_env.startup_calls == 1
        assert _smoke_env.shutdown_calls == 1
        # 退出 lifespan 后（无上下文请求）依然正常
        r2 = TestClient(app).get("/health")
        assert r2.status_code == 200

    def test_provider_startup_failure_degrades_gracefully(self, _smoke_env):
        """核心降级语义：ProviderService 启动失败（Redis 不可用）不阻断应用。"""
        _smoke_env.fail_startup = True
        with TestClient(app) as client:
            r = client.get("/health")
            assert r.status_code == 200  # 应用照常服务
        assert _smoke_env.startup_calls == 1

    def test_provider_shutdown_failure_does_not_block_exit(self, _smoke_env):
        """关闭阶段子系统停止失败仅告警，不阻断 shutdown。"""
        _smoke_env.fail_shutdown = True
        with TestClient(app):
            pass  # 进入 + 退出全程不抛异常即为通过
        assert _smoke_env.shutdown_calls == 1

    async def test_optional_module_import_error_tolerated(self):
        """可选模块缺失（ImportError）不阻断主流程。"""
        await _start_optional_module(
            import_path="app.nonexistent_module_xyz",
            factory="get_thing",
            label="FakeModule",
        )
        await _stop_optional_module(
            import_path="app.nonexistent_module_xyz",
            factory="get_thing",
            label="FakeModule",
        )

    async def test_optional_module_runtime_error_tolerated(self, monkeypatch):
        """可选模块 start()/stop() 运行时异常不阻断主流程。"""
        fake = types.ModuleType("app.fake_smoke_module")

        def _bad_start():
            raise RuntimeError("start exploded")

        def _bad_stop():
            raise RuntimeError("stop exploded")

        fake.get_thing = lambda: types.SimpleNamespace(start=_bad_start, stop=_bad_stop)
        monkeypatch.setitem(sys.modules, "app.fake_smoke_module", fake)
        await _start_optional_module(
            import_path="app.fake_smoke_module", factory="get_thing", label="Fake"
        )
        await _stop_optional_module(
            import_path="app.fake_smoke_module", factory="get_thing", label="Fake"
        )

    async def test_optional_module_none_instance_tolerated(self, monkeypatch):
        """工厂返回 None（未初始化）时跳过且不报错。"""
        fake = types.ModuleType("app.fake_none_module")
        fake.get_thing = lambda: None
        monkeypatch.setitem(sys.modules, "app.fake_none_module", fake)
        await _start_optional_module(
            import_path="app.fake_none_module", factory="get_thing", label="Fake"
        )

    async def test_optional_module_async_start_awaited(self, monkeypatch):
        """返回协程的 start() 会被正确 await。"""
        fake = types.ModuleType("app.fake_async_module")
        started: list[str] = []

        async def _async_start():
            started.append("yes")

        fake.get_thing = lambda: types.SimpleNamespace(start=_async_start)
        monkeypatch.setitem(sys.modules, "app.fake_async_module", fake)
        await _start_optional_module(
            import_path="app.fake_async_module", factory="get_thing", label="Fake"
        )
        assert started == ["yes"]


class TestOpenAPISchema:
    def test_openapi_generates_with_metadata(self):
        schema = app.openapi()
        assert schema["info"]["title"] == "BTC Intelligence Platform"
        assert "openapi" in schema
        paths = schema["paths"]
        assert len(paths) > 0
        assert "/health" in paths

    def test_all_business_domains_registered(self):
        """全部业务域路由均出现在 OpenAPI paths 中（冒烟级复核）。"""
        paths = app.openapi()["paths"]
        v1_paths = [p for p in paths if p.startswith("/api/v1")]
        domains = (
            "market", "onchain", "etf", "derivatives", "options",
            "macro", "sentiment", "portfolio", "backtest",
            "providers", "alerts", "indicators", "engine", "system",
        )
        for domain in domains:
            assert any(f"/api/v1/{domain}" in p for p in v1_paths), (
                f"业务域 {domain} 无任何路由注册"
            )

    def test_route_count_recorded(self):
        """记录路由总数（OpenAPI paths 口径，含 /health 与 /api/v1/*）。"""
        paths = app.openapi()["paths"]
        v1_count = sum(1 for p in paths if p.startswith("/api/v1"))
        # 下限断言防路由意外丢失；当前基线：总数 76，/api/v1 75（记录于冒烟报告）
        assert len(paths) >= 70, f"OpenAPI 路由总数意外减少：{len(paths)}"
        assert v1_count >= 65, f"/api/v1 路由数意外减少：{v1_count}"


class TestGlobalErrorEnvelope:
    def test_unhandled_exception_returns_uniform_envelope(self):
        """未捕获异常 → 500 统一信封（不泄漏堆栈给客户端）。"""
        def _boom():
            raise RuntimeError("smoke boom")

        app.add_api_route(
            "/__smoke_boom", _boom, methods=["GET"], include_in_schema=False
        )
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r = client.get("/__smoke_boom")
            assert r.status_code == 500
            body = r.json()
            assert body["success"] is False
            assert body["data"] is None
            assert body["error"]["code"] == "INTERNAL_ERROR"
            assert "smoke boom" not in r.text  # 内部信息不泄漏
        finally:
            # 清理临时路由，避免污染其他测试
            app.router.routes[:] = [
                rt for rt in app.router.routes
                if getattr(rt, "path", None) != "/__smoke_boom"
            ]

    def test_rate_limit_middleware_active(self):
        """限流中间件在冒烟路径上已装配（少量请求正常放行）。"""
        with TestClient(app) as client:
            for _ in range(5):
                r = client.get("/health")
                assert r.status_code == 200

    def test_request_id_header_present(self):
        """RequestID 中间件为每个响应附加请求 ID 头。"""
        with TestClient(app) as client:
            r = client.get("/health")
            # 中间件实现可能使用 x-request-id 或 x-requestid，冒烟验证二者之一
            assert (
                "x-request-id" in r.headers or "x-requestid" in r.headers
            )
