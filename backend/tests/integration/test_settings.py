"""SettingsService / 加密层集成测试（无 DB：mock 会话层为 DB 不可用语义）。

覆盖：
- crypto：加解密 roundtrip、解密失败容错（返回 None 不抛）、明文兼容、掩码；
- is_secret_key：显式集合 + 后缀自动检测；
- SettingsService：缓存读取、env 回退、缺省值、set 无 DB 优雅失败、
  get_all 掩码 + DB 失败回退缓存快照、invalidate；
- migrate_channel_password_encryption：无 DB 返回 0（启动容错语义）。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import pytest

from app.core import crypto
from app.core.config import settings
from app.services.settings_service import (
    SettingsService,
    is_secret_key,
    migrate_channel_password_encryption,
)


@pytest.fixture
def no_db(monkeypatch):
    """patch 延迟导入的 get_db_session_ctx：进入即抛错（DB 不可用语义）。"""
    import app.core.database as db_mod

    @asynccontextmanager
    async def broken_ctx():
        raise RuntimeError("db down")
        yield  # pragma: no cover

    monkeypatch.setattr(db_mod, "get_db_session_ctx", broken_ctx)


# ===========================================================================
# crypto 加密层
# ===========================================================================
class TestCrypto:
    def test_roundtrip(self):
        cipher = crypto.encrypt_value("s3cret-value")
        assert cipher.startswith("enc:v1:")
        assert "s3cret" not in cipher  # 密文不含明文
        assert crypto.decrypt_value(cipher) == "s3cret-value"

    def test_decrypt_invalid_token_returns_none(self):
        """密文非法（无法解密）返回 None 而非抛异常。"""
        assert crypto.decrypt_value("enc:v1:not-a-valid-fernet-token") is None

    def test_decrypt_plain_text_returns_none(self):
        """无前缀（非密文）返回 None。"""
        assert crypto.decrypt_value("plain-text") is None

    def test_decrypt_if_encrypted_plain_passthrough(self):
        """存量明文兼容：无前缀原样返回，有前缀解密。"""
        assert crypto.decrypt_if_encrypted("plain") == "plain"
        cipher = crypto.encrypt_value("hidden")
        assert crypto.decrypt_if_encrypted(cipher) == "hidden"

    def test_mask_secret(self):
        assert crypto.mask_secret("abc") == "***"
        assert crypto.mask_secret(None) == ""

    def test_is_encrypted(self):
        assert crypto.is_encrypted(crypto.encrypt_value("x"))
        assert not crypto.is_encrypted("plain")

    def test_derived_key_stable(self):
        """同一 SECRET_KEY 派生密钥稳定：跨实例可解密。"""
        cipher = crypto.encrypt_value("payload")
        crypto.reset_fernet()
        assert crypto.decrypt_value(cipher) == "payload"


# ===========================================================================
# 敏感键判定
# ===========================================================================
class TestSecretKeyDetection:
    def test_suffix_detection(self):
        assert is_secret_key("alert.smtp_password")
        assert is_secret_key("provider.api_key")
        assert is_secret_key("platform.secret_key")
        assert not is_secret_key("alert.scan_interval_seconds")
        assert not is_secret_key("provider.health_check.interval")


# ===========================================================================
# SettingsService
# ===========================================================================
class TestSettingsService:
    def test_get_sync_cache_hit(self):
        svc = SettingsService()
        svc._cache["alert.x"] = (42, False)
        assert svc.get_sync("alert.x", 0) == 42

    def test_get_sync_env_fallback(self):
        """缓存 miss 时回退 env（点分 key -> settings 属性）。"""
        svc = SettingsService()
        assert svc.get_sync("alert.default_recipient", None) == settings.alert_default_recipient

    def test_get_sync_default_when_missing(self, no_db):
        svc = SettingsService()
        assert svc.get_sync("alert.definitely_missing", "fallback") == "fallback"

    async def test_get_uses_cache(self, no_db):
        svc = SettingsService()
        svc._cache["k"] = ("v", False)
        svc._loaded_at = time.monotonic()
        assert await svc.get("k") == "v"

    async def test_get_env_fallback(self, no_db):
        svc = SettingsService()
        assert await svc.get("alert.default_recipient") == settings.alert_default_recipient

    async def test_get_missing_returns_default(self, no_db):
        svc = SettingsService()
        assert await svc.get("nope.missing", 7) == 7

    async def test_set_fails_gracefully_without_db(self, no_db):
        """DB 不可用时 set 返回 False，不抛异常。"""
        svc = SettingsService()
        assert await svc.set("alert.x", 1) is False

    async def test_get_all_masks_secret_and_falls_back_to_cache(self, no_db):
        """get_all：敏感键掩码；DB 失败回退缓存快照。"""
        svc = SettingsService()
        svc._cache = {
            "alert.smtp_password": ("plain-pw", True),
            "alert.max_per_hour": (5, False),
        }
        rows = await svc.get_all("alert")
        by_key = {r["key"]: r for r in rows}
        assert by_key["alert.smtp_password"]["value"] == "***"
        assert by_key["alert.smtp_password"]["is_secret"] is True
        assert by_key["alert.max_per_hour"]["value"] == 5

    async def test_get_all_namespace_filter(self, no_db):
        svc = SettingsService()
        svc._cache = {"alert.a": (1, False), "provider.b": (2, False)}
        rows = await svc.get_all("provider")
        assert [r["key"] for r in rows] == ["provider.b"]

    async def test_invalidate_clears_cache(self, no_db):
        svc = SettingsService()
        svc._cache["k"] = ("v", False)
        svc.invalidate()
        assert await svc.get("k", "d") == "d"


# ===========================================================================
# 存量密码迁移
# ===========================================================================
class TestMigrateChannelPasswords:
    async def test_no_db_returns_zero(self, no_db):
        """DB 不可用时返回 0，不抛异常（启动容错语义）。"""
        assert await migrate_channel_password_encryption() == 0
