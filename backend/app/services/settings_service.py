"""SettingsService — 系统设置统一读写入口（DB 配置中心）。

数据模型：``system_settings`` 表，键为点分命名空间（如
``alert.scan_interval_seconds`` / ``provider.health_check.interval``），
值统一 ``{"v": ...}`` JSONB 包装。

读取优先级：**DB > 环境变量（settings.* 静态配置）> 默认值**。

特性：
1. 10s TTL 内存缓存（全量加载）；DB 不可用时自动回退环境变量，不崩溃；
2. ``get_sync``：同步读取（缓存 + env 回退），供无法 await 的调用方
   （property / 同步循环）使用；缓存 miss 时自动调度一次后台异步刷新，
   下一轮调用即可看到 DB 最新值（最终一致）；
3. ``set``：upsert + 审计日志（AuditLog.CONFIG_CHANGE）+ 缓存即时失效；
4. ``is_secret`` 键：加密存储（Fernet，见 app.core.crypto），读取时掩码；
5. 键名尾部含 ``password / secret / key / token`` 的键自动按敏感处理。

热生效路径：
- 后台修改 -> set() 写 DB + 更新缓存 -> 下一轮循环/下一次调用即读到新值。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core import crypto
from app.core.config import settings

#: 缓存 TTL（秒）
CACHE_TTL_SECONDS = 10.0

#: 已知敏感键（追加于自动检测规则之外）
SECRET_KEYS: frozenset[str] = frozenset({
    "alert.smtp_password",
    "platform.secret_key",
})

#: 自动敏感检测后缀
_SECRET_SUFFIXES = ("password", "secret", "api_key", "apikey", "token", "passphrase")

#: sentinel：env 回退查无此属性
_MISS = object()


def is_secret_key(key: str) -> bool:
    """判断设置键是否为敏感键（显式集合优先，再按后缀自动检测）。"""
    if key in SECRET_KEYS:
        return True
    tail = key.rsplit(".", 1)[-1].lower()
    return any(tail.endswith(s) for s in _SECRET_SUFFIXES)


class SettingsService:
    """系统设置读写服务（单例，见 :func:`get_settings_service`）。"""

    def __init__(self, cache_ttl: float = CACHE_TTL_SECONDS) -> None:
        self._cache_ttl = cache_ttl
        # key -> (value, is_secret)；value 已解包（is_secret 已解密，仅限内部使用）
        self._cache: dict[str, tuple[Any, bool]] = {}
        self._loaded_at: float = 0.0  # monotonic；0 表示从未成功加载
        self._refresh_pending = False

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    async def get(self, key: str, default: Any = None) -> Any:
        """读取设置：DB（缓存）-> env（settings.* 属性）-> default。"""
        cached = self._read_cached(key)
        if cached is not _MISS:
            return default if cached is None else cached

        # 缓存过期/未加载：强制同步刷新一次（DB 失败容错）
        await self._load_all()
        cached = self._read_cached(key)
        if cached is not _MISS:
            return default if cached is None else cached

        return self._env_fallback(key, default)

    def get_sync(self, key: str, default: Any = None) -> Any:
        """同步读取：缓存（含过期旧值）-> env -> default，并调度后台刷新。

        适用场景：无法 await 的调用方（property、同步循环、数据类）。
        DB 中已存在但缓存尚未加载的值会在下一轮调用时生效（最终一致）。
        """
        entry = self._cache.get(key, _MISS)
        if entry is not _MISS:
            # stale-while-revalidate：过期旧值先用，同时触发刷新
            if not self._cache_fresh():
                self._schedule_refresh()
            return default if entry[0] is None else entry[0]
        self._schedule_refresh()
        return self._env_fallback(key, default)

    async def get_all(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """全量/按命名空间读取（is_secret 掩码；供 API 展示）。"""
        rows: list[dict[str, Any]] = []
        try:
            from app.core.database import get_db_session_ctx
            from app.models.system import SystemSetting

            async with get_db_session_ctx() as session:
                result = await session.execute(select(SystemSetting).order_by(SystemSetting.key))
                for row in result.scalars().all():
                    if namespace and not row.key.startswith(f"{namespace}."):
                        continue
                    value = crypto.decrypt_value((row.value or {}).get("v")) \
                        if row.is_secret else (row.value or {}).get("v")
                    rows.append({
                        "key": row.key,
                        "value": crypto.mask_secret(value) if row.is_secret else value,
                        "is_secret": bool(row.is_secret),
                        "description": row.description,
                        "updated_by": row.updated_by,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    })
        except Exception as e:  # noqa: BLE001 - DB 不可用降级为缓存快照
            logger.warning(f"SettingsService.get_all 失败（回退缓存快照）: {e}")
            for key in sorted(self._cache):
                if namespace and not key.startswith(f"{namespace}."):
                    continue
                value, is_secret = self._cache[key]
                rows.append({
                    "key": key,
                    "value": crypto.mask_secret(value) if is_secret else value,
                    "is_secret": is_secret,
                    "description": None,
                    "updated_by": None,
                    "updated_at": None,
                })
        return rows

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    async def set(
        self,
        key: str,
        value: Any,
        *,
        updated_by: str | None = None,
        description: str | None = None,
    ) -> bool:
        """写入设置（upsert）+ 审计日志 + 缓存即时更新。

        Args:
            key: 设置键（点分命名空间）
            value: 设置值（JSON 兼容标量/结构）
            updated_by: 操作人标识
            description: 首次创建时的说明

        Returns:
            是否成功落库
        """
        from app.core.database import get_db_session_ctx
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from app.models.system import AuditLog, SystemSetting

        secret = is_secret_key(key)
        stored_value = crypto.encrypt_value(str(value)) if secret else value

        try:
            async with get_db_session_ctx() as session:
                old = (
                    await session.execute(
                        select(SystemSetting).where(SystemSetting.key == key)
                    )
                ).scalar_one_or_none()
                old_masked = ("***" if old.is_secret else (old.value or {}).get("v")) \
                    if old is not None else None

                stmt = pg_insert(SystemSetting.__table__).values(
                    key=key,
                    value={"v": stored_value},
                    description=description,
                    is_secret=secret,
                    updated_by=updated_by,
                )
                update_set: dict[str, Any] = {
                    "value": {"v": stored_value},
                    "is_secret": secret,
                    "updated_by": updated_by,
                }
                if description is not None:
                    update_set["description"] = description
                stmt = stmt.on_conflict_do_update(index_elements=[SystemSetting.key], set_=update_set)
                await session.execute(stmt)

                session.add(AuditLog(
                    action="CONFIG_CHANGE",
                    action_category="CONFIG",
                    resource_type="SETTING",
                    resource_name=key,
                    old_values={"value": old_masked},
                    new_values={"value": "***" if secret else value},
                    is_success=True,
                ))
        except Exception as e:  # noqa: BLE001 - DB 不可用仅报错不崩溃
            logger.error(f"SettingsService.set('{key}') 失败: {e}")
            return False

        # 写缓存（即时热生效）
        self._cache[key] = (value, secret)
        self._loaded_at = time.monotonic()
        logger.info(f"Setting updated: {key} (by={updated_by or 'system'})")
        return True

    def invalidate(self) -> None:
        """清空缓存（下次读取时重新从 DB 加载）。"""
        self._cache.clear()
        self._loaded_at = 0.0

    # ------------------------------------------------------------------
    # 内部：缓存与回退
    # ------------------------------------------------------------------

    def _cache_fresh(self) -> bool:
        """缓存是否在 TTL 内且已成功加载过。"""
        return self._loaded_at > 0 and (time.monotonic() - self._loaded_at) < self._cache_ttl

    def _read_cached(self, key: str) -> Any:
        """按 key 读缓存：TTL 内命中返回值；过期或未命中返回 _MISS。"""
        entry = self._cache.get(key, _MISS)
        if entry is _MISS:
            return _MISS
        return entry[0] if self._cache_fresh() else _MISS

    def _env_fallback(self, key: str, default: Any = None) -> Any:
        """env 兜底：点分 key -> settings 属性（alert.x.y -> ALERT_X_Y 兼容小写下划线）。"""
        attr = key.replace(".", "_")
        value = getattr(settings, attr, _MISS)
        if value is _MISS:
            # 兼容嵌套段大写缩写等场景：逐级拼前缀也查一遍（如 provider.failover.recovery_threshold）
            return default
        return value

    def _schedule_refresh(self) -> None:
        """调度一次后台缓存刷新（事件循环内 fire-and-forget，去重）。"""
        if self._refresh_pending:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无事件循环（同步上下文/测试）——跳过
        self._refresh_pending = True
        task = loop.create_task(self._refresh())
        task.add_done_callback(self._refresh_done)

    def _refresh_done(self, _task: asyncio.Task) -> None:
        self._refresh_pending = False

    async def _refresh(self) -> None:
        try:
            await self._load_all()
        except Exception as e:  # noqa: BLE001 - 后台刷新失败仅记录
            logger.debug(f"SettingsService 后台刷新失败: {e}")

    async def _load_all(self) -> None:
        """全量加载 system_settings 到缓存（is_secret 解密；DB 失败保留旧缓存）。"""
        try:
            from app.core.database import get_db_session_ctx
            from app.models.system import SystemSetting

            new_cache: dict[str, tuple[Any, bool]] = {}
            async with get_db_session_ctx() as session:
                rows = (
                    await session.execute(select(SystemSetting))
                ).scalars().all()
            for row in rows:
                raw = (row.value or {}).get("v")
                value = crypto.decrypt_value(raw) if row.is_secret else raw
                new_cache[row.key] = (value, bool(row.is_secret))
            self._cache = new_cache
            self._loaded_at = time.monotonic()
        except Exception as e:  # noqa: BLE001
            # 保留旧缓存；从未加载过则保持 _loaded_at=0（后续走 env 回退）
            logger.debug(f"SettingsService._load_all 失败: {e}")
            if self._loaded_at == 0:
                self._loaded_at = time.monotonic() - self._cache_ttl  # 标记为已过期


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_settings_service: SettingsService | None = None


def get_settings_service() -> SettingsService:
    """获取全局 SettingsService 单例。"""
    global _settings_service
    if _settings_service is None:
        _settings_service = SettingsService()
    return _settings_service


def set_settings_service(service: SettingsService | None) -> None:
    """替换全局单例（测试用）。"""
    global _settings_service
    _settings_service = service


# ---------------------------------------------------------------------------
# 存量明文密码一次性加密迁移（AlertChannelConfig SMTP/DIGEST）
# ---------------------------------------------------------------------------

async def migrate_channel_password_encryption() -> int:
    """将 AlertChannelConfig(SMTP/DIGEST) config 中明文 password 加密后写回。

    幂等：已是 ``enc:v1:`` 密文的跳过。DB 不可用时返回 0（不抛出，
    由启动流程容错调用）。
    """
    try:
        from app.core.database import get_db_session_ctx
        from sqlalchemy import select

        from app.models.alert import AlertChannelConfig

        migrated = 0
        async with get_db_session_ctx() as session:
            rows = (
                await session.execute(
                    select(AlertChannelConfig).where(
                        AlertChannelConfig.channel_type.in_(["SMTP", "DIGEST"])
                    )
                )
            ).scalars().all()
            for row in rows:
                config = dict(row.config or {})
                password = config.get("password")
                if not password or crypto.is_encrypted(str(password)):
                    continue
                config["password"] = crypto.encrypt_value(str(password))
                row.config = config
                migrated += 1
        if migrated:
            logger.info(f"AlertChannelConfig 明文密码加密迁移完成: {migrated} 条")
        return migrated
    except Exception as e:  # noqa: BLE001 - 启动迁移失败不阻断应用
        logger.warning(f"AlertChannelConfig 密码加密迁移失败（跳过）: {e}")
        return 0


__all__ = [
    "CACHE_TTL_SECONDS",
    "SECRET_KEYS",
    "SettingsService",
    "get_settings_service",
    "is_secret_key",
    "migrate_channel_password_encryption",
    "set_settings_service",
]
