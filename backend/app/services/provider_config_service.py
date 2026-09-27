"""ProviderConfigService — Provider 配置 DB 化（配置后台化核心）。

职责：
1. ``import_yaml_to_db``：将 providers.yaml 导入 providers 表（表空时自动，
   或显式 force 覆盖）——首次启动把 YAML 变为 DB 种子数据；
2. ``build_provider_config``：以 DB 行为基准、YAML 仅补 defaults，合成最终
   ProviderConfig（含敏感凭据解密），供实例化/热重载使用；
3. ``save_provider_config``：后台修改落库（凭据加密存储 + 审计日志）；
4. ``get_provider_detail``：Provider 配置明细（敏感值掩码，供 API）。

合并优先级（高 -> 低）::

    config_overrides（用户保存的补充项，凭据密文）
      > providers 表行字段（priority/is_enabled/base_url/timeout/...）
        > providers.yaml（defaults + 该 provider 段）
          > ProviderConfig 内置默认值

热生效由 ProviderService.reload_provider 完成（本服务只管配置）。
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from sqlalchemy import func, select

from app.core import crypto
from app.models.enums import ProviderCategory
from app.models.provider import Provider
from app.providers.base.config import ConfigLoader, ProviderConfig

#: overrides 中凭据字段（加密存储、构建时单独解密）
_CREDENTIAL_OVERRIDE_KEYS = ("api_key", "api_secret", "api_passphrase")

#: 轻量变更字段（不触发 Provider 实例重建，见 ProviderService.apply_config_change）
LIGHTWEIGHT_FIELDS = frozenset({"is_enabled", "priority", "is_locked"})


class ProviderConfigService:
    """Provider 配置 DB 化服务（无状态，方法级容错）。"""

    def __init__(self, config_path: str = "config/providers.yaml") -> None:
        self._config_path = config_path

    # ------------------------------------------------------------------
    # YAML -> DB 导入
    # ------------------------------------------------------------------

    async def import_yaml_to_db(self, *, force: bool = False) -> int:
        """将 providers.yaml 导入 providers 表。

        Args:
            force: True 时覆盖已有行（保留已有加密凭据与 config_overrides）；
                   False 时仅表空才导入。

        Returns:
            写入/更新的行数；DB 或 YAML 不可用返回 0（不抛出）。
        """
        from app.core.database import get_db_session_ctx

        yaml_data = self._load_yaml()
        if yaml_data is None:
            return 0

        providers_cfg: dict[str, dict[str, Any]] = yaml_data.get("providers", {}) or {}
        try:
            async with get_db_session_ctx() as session:
                total = int(
                    (
                        await session.execute(select(func.count()).select_from(Provider))
                    ).scalar_one()
                )
                if total > 0 and not force:
                    logger.debug("providers 表非空，跳过 YAML 导入")
                    return 0

                existing = {
                    row.name: row
                    for row in (
                        await session.execute(select(Provider))
                    ).scalars().all()
                }
                written = 0
                for category_group, providers in providers_cfg.items():
                    category = self._to_category(category_group)
                    if category is None:
                        logger.warning(f"YAML 分组 '{category_group}' 无对应类别，跳过导入")
                        continue
                    if not isinstance(providers, dict):
                        continue
                    for name, raw in providers.items():
                        if not isinstance(raw, dict):
                            continue
                        row = existing.get(name)
                        self._upsert_from_yaml(
                            session, row, name, category, raw, force=force
                        )
                        written += 1
                logger.info(
                    f"providers.yaml 导入完成（force={force}）：{written} 条"
                )
                return written
        except Exception as e:  # noqa: BLE001 - DB 不可用不崩溃
            logger.warning(f"providers.yaml 导入 DB 失败: {e}")
            return 0

    def _upsert_from_yaml(
        self,
        session: Any,
        row: Provider | None,
        name: str,
        category: ProviderCategory,
        raw: dict[str, Any],
        *,
        force: bool,
    ) -> None:
        """单个 provider 的 YAML -> DB 写入（保留加密凭据/overrides）。"""
        decrypted_key = crypto.decrypt_value(row.api_key_encrypted) if row else None
        api_key = str(raw.get("api_key") or "").strip()
        # env 未设置时 YAML 解析为空串 —— 保留 DB 已有加密凭据
        keep_old_key = not api_key and bool(decrypted_key)

        timeout_cfg = self._normalize_timeout(raw.get("timeout"))
        retry_cfg = raw.get("retry") if isinstance(raw.get("retry"), dict) else None
        rl_cfg = raw.get("rate_limit") if isinstance(raw.get("rate_limit"), dict) else {}
        proxy_url = raw.get("proxy") or None

        fields = dict(
            category=category,
            base_url=str(raw.get("base_url") or "") or (row.base_url if row else ""),
            priority=int(raw.get("priority", 100)),
            is_enabled=bool(raw.get("enabled", True)),
            is_locked=bool(raw.get("locked", False)),
            proxy_config={"type": "direct"} if not proxy_url else {"type": "http", "url": proxy_url},
            timeout_config=timeout_cfg,
            retry_config=retry_cfg,
            rate_limit=int(rl_cfg.get("requests", 60)),
            rate_limit_window=int(rl_cfg.get("period", 60)),
            description=raw.get("description") or (row.description if row else None),
            supported_symbols=list(raw.get("supported_symbols") or ["BTC"]),
            supported_intervals=list(raw.get("supported_intervals") or []),
            config_source="YAML",
        )

        if row is None:
            row = Provider(name=name, **fields)
            row.api_key_encrypted = crypto.encrypt_value(api_key) if api_key else None
            row.config_overrides = {}
            session.add(row)
        else:
            if force:
                for k, v in fields.items():
                    setattr(row, k, v)
            if api_key and not keep_old_key:
                row.api_key_encrypted = crypto.encrypt_value(api_key)
            row.config_overrides = row.config_overrides or {}

    # ------------------------------------------------------------------
    # 构建 ProviderConfig（DB 优先，YAML 兜底）
    # ------------------------------------------------------------------

    async def build_provider_config(self, name: str) -> ProviderConfig | None:
        """按 DB 配置构建 ProviderConfig；providers 表无此行返回 None。"""
        from app.core.database import get_db_session_ctx

        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(
                        select(Provider).where(Provider.name == name)
                    )
                ).scalar_one_or_none()
        except Exception as e:  # noqa: BLE001 - DB 不可用由调用方回退 YAML
            logger.warning(f"build_provider_config('{name}') 查询 DB 失败: {e}")
            return None

        if row is None:
            return None

        yaml_defaults, yaml_raw = self._yaml_lookup(name)
        overrides = dict(row.config_overrides or {})

        # 1) YAML defaults + 该 provider 原始段（底层兜底）
        defaults: dict[str, Any] = {**yaml_defaults}
        raw: dict[str, Any] = {**yaml_raw}

        # 2) DB 行字段（覆盖 YAML）
        if row.base_url:
            raw["base_url"] = row.base_url
        raw["enabled"] = bool(row.is_enabled)
        raw["priority"] = int(row.priority)
        raw["locked"] = bool(row.is_locked)
        proxy_cfg = row.proxy_config or {}
        if isinstance(proxy_cfg, dict) and proxy_cfg.get("url"):
            raw["proxy"] = proxy_cfg["url"]
        if isinstance(row.timeout_config, dict) and row.timeout_config:
            raw["timeout"] = row.timeout_config
        if isinstance(row.retry_config, dict) and row.retry_config:
            raw["retry"] = row.retry_config
        if row.rate_limit:
            raw["rate_limit"] = {
                "requests": int(row.rate_limit),
                "period": int(row.rate_limit_window or 60),
            }
        if row.description:
            raw["description"] = row.description
        if row.supported_symbols:
            raw["supported_symbols"] = list(row.supported_symbols)
        if row.supported_intervals:
            raw["supported_intervals"] = list(row.supported_intervals)
        if row.api_key_encrypted:
            decrypted = crypto.decrypt_value(row.api_key_encrypted)
            if decrypted:
                raw["api_key"] = decrypted

        # 3) overrides（最高优先级；凭据解密后同样并入 raw）
        for k, v in overrides.items():
            if k in _CREDENTIAL_OVERRIDE_KEYS:
                continue
            if v is not None:
                raw[k] = v
        for cred in _CREDENTIAL_OVERRIDE_KEYS:
            cipher = overrides.get(cred)
            if cipher:
                decrypted = crypto.decrypt_if_encrypted(cipher)
                if decrypted:
                    raw[cred] = decrypted

        category = getattr(row.category, "value", row.category)
        return ConfigLoader.parse_provider_config(
            name=name,
            category=str(category).lower(),
            raw=raw,
            defaults=defaults,
        )

    # ------------------------------------------------------------------
    # 保存（后台修改 -> DB + 审计）
    # ------------------------------------------------------------------

    async def save_provider_config(self, name: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        """保存 Provider 配置修改（凭据加密 + 审计日志）。

        约定：``api_key/api_secret/api_passphrase`` 传空串 = 保留旧值；
        传非空 = 加密存储；显式 null（None）= 清除（仅 overrides 凭据支持）。

        Returns:
            更新后的掩码明细 dict；Provider 不存在返回 None。
        """
        from app.core.database import get_db_session_ctx

        async with get_db_session_ctx() as session:
            row = (
                await session.execute(select(Provider).where(Provider.name == name))
            ).scalar_one_or_none()
            if row is None:
                return None

            old_masked = self._mask_row(row)
            overrides = dict(row.config_overrides or {})
            column_map = {
                "priority": "priority",
                "is_enabled": "is_enabled",
                "is_locked": "is_locked",
                "base_url": "base_url",
            }
            for src, dst in column_map.items():
                if updates.get(src) is not None:
                    setattr(row, dst, updates[src])

            if "proxy" in updates:
                proxy = updates["proxy"]
                row.proxy_config = (
                    {"type": "direct"} if not proxy else {"type": "http", "url": str(proxy)}
                )
            if updates.get("timeout_config") is not None:
                row.timeout_config = self._normalize_timeout(updates["timeout_config"])
            if updates.get("retry_config") is not None:
                retry = updates["retry_config"]
                row.retry_config = retry if isinstance(retry, dict) else None
            if updates.get("rate_limit") is not None:
                rl = updates["rate_limit"]
                if isinstance(rl, dict):
                    row.rate_limit = int(rl.get("requests", row.rate_limit or 60))
                    row.rate_limit_window = int(rl.get("period", row.rate_limit_window or 60))
                    if rl.get("burst") is not None:
                        overrides["rate_limit_burst"] = rl["burst"]

            # 凭据处理
            for cred in _CREDENTIAL_OVERRIDE_KEYS:
                if cred not in updates:
                    continue
                value = updates[cred]
                if value is None:  # 显式清除（api_key 清除走 DB 列）
                    overrides.pop(cred, None)
                    if cred == "api_key":
                        row.api_key_encrypted = None
                elif str(value).strip() == "":
                    continue  # 空串 = 保留旧值
                elif cred == "api_key":
                    row.api_key_encrypted = crypto.encrypt_value(str(value))
                else:
                    overrides[cred] = crypto.encrypt_value(str(value))

            row.config_overrides = overrides
            row.config_source = "DB"

            session.add(
                _audit_entry(
                    resource_name=name,
                    old_values=old_masked,
                    new_values=self._mask_row(row),
                )
            )
            new_masked = self._mask_row(row)

        logger.info(f"Provider config saved: {name} (source=DB)")
        return new_masked

    # ------------------------------------------------------------------
    # 明细（掩码）
    # ------------------------------------------------------------------

    async def get_provider_detail(self, name: str) -> dict[str, Any] | None:
        """Provider 配置明细（敏感值掩码；无此行返回 None）。"""
        from app.core.database import get_db_session_ctx

        try:
            async with get_db_session_ctx() as session:
                row = (
                    await session.execute(select(Provider).where(Provider.name == name))
                ).scalar_one_or_none()
                if row is None:
                    return None
                return self._mask_row(row)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"get_provider_detail('{name}') 失败: {e}")
            return None

    def _mask_row(self, row: Provider) -> dict[str, Any]:
        """Provider 行 -> 掩码配置明细。"""
        return mask_provider_row(row)

    # ------------------------------------------------------------------
    # YAML 辅助
    # ------------------------------------------------------------------

    def _load_yaml(self) -> dict[str, Any] | None:
        """加载 YAML（env 已解析）；失败返回 None。"""
        try:
            return ConfigLoader.load(self._config_path)
        except FileNotFoundError:
            logger.warning(f"Provider YAML 配置不存在: {self._config_path}")
            return None
        except Exception as e:  # noqa: BLE001
            logger.error(f"Provider YAML 配置解析失败: {e}")
            return None

    def _yaml_lookup(self, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """在 YAML 中查找 provider：返回 (defaults, 该 provider 原始段)。"""
        data = self._load_yaml()
        if data is None:
            return {}, {}
        defaults = data.get("defaults", {}) or {}
        for _group, providers in (data.get("providers", {}) or {}).items():
            if isinstance(providers, dict) and name in providers and isinstance(providers[name], dict):
                return defaults, providers[name]
        return defaults, {}

    @staticmethod
    def _to_category(group: str) -> ProviderCategory | None:
        """YAML 分组名 -> ProviderCategory（大写匹配）。"""
        try:
            return ProviderCategory(group.upper())
        except ValueError:
            return None

    @staticmethod
    def _normalize_timeout(value: Any) -> dict[str, Any]:
        """超时配置归一化：标量 -> 四段同值；dict 原样。"""
        if isinstance(value, (int, float)):
            v = float(value)
            return {"connect": v, "read": v, "write": v, "pool": v}
        if isinstance(value, dict):
            return dict(value)
        return {}


def mask_provider_row(row: Provider) -> dict[str, Any]:
    """Provider 行 -> 掩码配置明细（模块级，供 API 层直接复用）。"""
    overrides = dict(row.config_overrides or {})
    rl_burst = overrides.pop("rate_limit_burst", None)
    has_api_key = bool(row.api_key_encrypted) or bool(overrides.get("api_key"))
    return {
        "base_url": row.base_url,
        "proxy": (row.proxy_config or {}).get("url")
        if isinstance(row.proxy_config, dict)
        else None,
        "timeout_config": row.timeout_config,
        "retry_config": row.retry_config,
        "rate_limit": {
            "requests": row.rate_limit,
            "period": row.rate_limit_window,
            "burst": rl_burst,
        },
        "api_key": crypto.mask_secret(True if has_api_key else None),
        "api_secret": crypto.mask_secret(overrides.get("api_secret")),
        "api_passphrase": crypto.mask_secret(overrides.get("api_passphrase")),
        "priority": row.priority,
        "is_enabled": row.is_enabled,
        "is_locked": row.is_locked,
        "config_source": row.config_source,
        "config_overrides": {
            k: crypto.mask_secret(v) if k in _CREDENTIAL_OVERRIDE_KEYS else v
            for k, v in overrides.items()
        },
    }


def _audit_entry(
    *,
    resource_name: str,
    old_values: dict[str, Any],
    new_values: dict[str, Any],
) -> Any:
    """构造 Provider 配置变更审计日志（延迟导入避免测试环境依赖）。"""
    from app.models.system import AuditLog

    return AuditLog(
        action="CONFIG_CHANGE",
        action_category="PROVIDER",
        resource_type="PROVIDER",
        resource_name=resource_name,
        old_values=old_values,
        new_values=new_values,
        is_success=True,
    )


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_provider_config_service: ProviderConfigService | None = None


def get_provider_config_service() -> ProviderConfigService:
    """获取全局 ProviderConfigService 单例。"""
    global _provider_config_service
    if _provider_config_service is None:
        _provider_config_service = ProviderConfigService()
    return _provider_config_service


def set_provider_config_service(service: ProviderConfigService | None) -> None:
    """替换全局单例（测试用）。"""
    global _provider_config_service
    _provider_config_service = service


__all__ = [
    "LIGHTWEIGHT_FIELDS",
    "ProviderConfigService",
    "get_provider_config_service",
    "mask_provider_row",
    "set_provider_config_service",
]
