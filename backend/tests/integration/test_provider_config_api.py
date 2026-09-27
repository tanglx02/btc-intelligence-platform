"""Provider 配置 API 测试（httpx ASGITransport + mock 依赖，无 DB / 网络）。

覆盖：
- GET /providers：响应新增 config 明细（api_key 掩码、config_source），原字段保持；
- PUT /providers/{name}：updates 传给保存层、触发热重载、响应掩码、404/400；
- POST /providers/{name}/reload：热重载触发；
- POST /providers/sync-from-yaml：显式导入。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.core.database import get_db_session
from app.main import app


@pytest.fixture
async def client():
    """httpx AsyncClient（ASGITransport，不触发 lifespan）。"""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _cleanup_overrides():
    """清理 dependency_overrides（避免跨用例泄漏）。"""
    yield
    app.dependency_overrides.pop(get_db_session, None)


class _Result:
    """假 SQLAlchemy Result（scalars().first()/.all()）。"""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, stmt):
        return _Result(self._rows)


def override_session(rows) -> None:
    """覆盖 get_db_session 依赖（providers 表查询返回预置行）。"""

    async def _dep():
        yield _FakeSession(rows)

    app.dependency_overrides[get_db_session] = _dep


def make_provider_row(**overrides):
    """providers 表行鸭子类型（_provider_to_dict + mask_provider_row 所需字段）。"""
    defaults = dict(
        id="00000000-0000-0000-0000-000000000001",
        name="binance",
        category="MARKET",
        base_url="https://api.binance.com",
        priority=10,
        is_enabled=True,
        status="ONLINE",
        health_score=95.5,
        consecutive_failures=0,
        last_success_at=None,
        last_failure_at=None,
        description="全球最大加密货币交易所",
        # mask_provider_row 所需
        config_overrides={"api_secret": "enc:v1:BBBB"},
        proxy_config={"type": "direct"},
        timeout_config={"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 5.0},
        retry_config=None,
        rate_limit=1200,
        rate_limit_window=60,
        api_key_encrypted="enc:v1:AAAA",
        is_locked=False,
        config_source="DB",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class FakeProviderConfigService:
    """替代 ProviderConfigService（记录保存调用）。"""

    def __init__(self):
        self.received: tuple | None = None

    async def save_provider_config(self, name, updates):
        self.received = (name, dict(updates))
        return {"api_key": "***", "config_source": "DB"}


class FakeProviderService:
    """替代 ProviderService（记录热重载调用，标记已启动避免真实 startup）。"""

    def __init__(self):
        self._started = True
        self.calls: list[tuple] = []

    async def apply_config_change(self, name, updates):
        self.calls.append((name, dict(updates)))
        return {"applied": list(updates), "reloaded": "base_url" in updates}

    async def reload_provider(self, name):
        self.calls.append(("reload", name))
        return True


# ===========================================================================
# GET /providers — config 明细（掩码）
# ===========================================================================
class TestListProviders:
    async def test_list_includes_masked_config(self, client):
        override_session([(make_provider_row(), None)])
        resp = await client.get("/api/v1/providers/")
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        item = body["data"][0]
        # config 明细：掩码 + 来源
        assert item["config"]["api_key"] == "***"
        assert item["config"]["api_secret"] == "***"
        assert item["config"]["config_source"] == "DB"
        assert item["config"]["base_url"] == "https://api.binance.com"
        assert item["config"]["rate_limit"]["requests"] == 1200
        # 原有响应结构保持（只增字段）
        assert item["name"] == "binance"
        assert item["priority"] == 10
        assert item["latest_health"] is None


# ===========================================================================
# PUT /providers/{name} — 落库 + 热重载 + 掩码
# ===========================================================================
class TestUpdateProvider:
    async def test_put_saves_reloads_and_masks(self, client, monkeypatch):
        override_session([make_provider_row()])
        fake_svc = FakeProviderConfigService()
        fake_ps = FakeProviderService()
        monkeypatch.setattr(
            "app.services.provider_config_service.get_provider_config_service",
            lambda: fake_svc,
        )
        monkeypatch.setattr(
            "app.api.v1.providers.get_provider_service", lambda: fake_ps
        )

        resp = await client.put(
            "/api/v1/providers/BINANCE",  # 大小写不敏感（兼容旧命名）
            json={"priority": 5, "api_key": "new-secret"},
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        name, updates = fake_svc.received
        assert name == "binance"  # 以 DB 行名为准
        assert updates["priority"] == 5
        assert updates["api_key"] == "new-secret"  # 加密在保存层内完成
        assert fake_ps.calls[0][0] == "binance"  # 保存后触发热重载
        assert body["data"]["config"]["api_key"] == "***"  # 响应掩码

    async def test_put_unknown_provider_404(self, client, monkeypatch):
        override_session([])
        monkeypatch.setattr(
            "app.services.provider_config_service.get_provider_config_service",
            lambda: FakeProviderConfigService(),
        )
        monkeypatch.setattr(
            "app.api.v1.providers.get_provider_service", lambda: FakeProviderService()
        )
        resp = await client.put("/api/v1/providers/ghost", json={"priority": 5})
        assert resp.status_code == 404

    async def test_put_empty_body_400(self, client):
        override_session([make_provider_row()])
        resp = await client.put("/api/v1/providers/binance", json={})
        assert resp.status_code == 400


# ===========================================================================
# POST /providers/{name}/reload
# ===========================================================================
class TestReloadEndpoint:
    async def test_reload_triggers_hot_reload(self, client, monkeypatch):
        override_session([make_provider_row()])
        fake_ps = FakeProviderService()
        monkeypatch.setattr(
            "app.api.v1.providers.get_provider_service", lambda: fake_ps
        )
        resp = await client.post("/api/v1/providers/binance/reload")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"]["reloaded"] is True
        assert fake_ps.calls == [("reload", "binance")]


# ===========================================================================
# POST /providers/sync-from-yaml
# ===========================================================================
class TestSyncFromYaml:
    async def test_sync_default_no_overwrite(self, client, monkeypatch):
        class Svc:
            async def import_yaml_to_db(self, force=False):
                assert force is False
                return 3

        monkeypatch.setattr(
            "app.services.provider_config_service.get_provider_config_service",
            lambda: Svc(),
        )
        resp = await client.post("/api/v1/providers/sync-from-yaml", json={})
        assert resp.status_code == 200
        assert resp.json()["data"] == {"imported": 3, "overwrite": False}

    async def test_sync_overwrite_flag_passed(self, client, monkeypatch):
        seen = {}

        class Svc:
            async def import_yaml_to_db(self, force=False):
                seen["force"] = force
                return 8

        monkeypatch.setattr(
            "app.services.provider_config_service.get_provider_config_service",
            lambda: Svc(),
        )
        resp = await client.post(
            "/api/v1/providers/sync-from-yaml", json={"overwrite": True}
        )
        assert resp.status_code == 200
        assert seen["force"] is True
        assert resp.json()["data"]["imported"] == 8
