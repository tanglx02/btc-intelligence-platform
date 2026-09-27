"""Settings API 路由（/api/v1/settings）— 系统设置读写（配置后台化）。

- GET /settings?namespace=alert|provider|platform  全部/分组设置（is_secret 掩码）
- PUT /settings/{key}  更新单项（upsert + 审计 + 缓存即时失效 → 热生效）

设置键为点分命名空间（如 ``alert.scan_interval_seconds``），写入后由
各消费方（AlertEngine / dispatcher / FailoverEngine / HealthMonitor ...）
在下一轮循环/调用时读取生效。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel

from app.schemas import err, ok
from app.services.settings_service import get_settings_service

router = APIRouter(prefix="/settings", tags=["Settings"])

#: 允许通过 API 写入的命名空间白名单（防误写其它命名空间）
_ALLOWED_NAMESPACES = ("alert", "provider", "platform")


class SettingUpdate(BaseModel):
    """设置更新请求体。"""

    value: Any
    updated_by: str | None = None
    description: str | None = None


def _check_namespace(key: str) -> str | None:
    """校验 key 命名空间；非法返回错误信息，合法返回 None。"""
    namespace = key.split(".", 1)[0].lower()
    if "." not in key:
        return f"设置键必须为点分命名空间格式（如 alert.scan_interval_seconds），得到: {key!r}"
    if namespace not in _ALLOWED_NAMESPACES:
        return (
            f"命名空间 {namespace!r} 不在白名单 {_ALLOWED_NAMESPACES} 内，"
            "调度任务配置请使用 PUT /system/jobs/{job_name}"
        )
    return None


@router.get("")
async def list_settings(
    namespace: str | None = Query(default=None, description="按命名空间过滤: alert/provider/platform"),
) -> dict[str, Any]:
    """全部/分组设置列表（is_secret 值掩码显示）。"""
    try:
        rows = await get_settings_service().get_all(
            namespace.strip().lower() if namespace else None
        )
        return ok({"count": len(rows), "settings": rows, "namespace": namespace})
    except Exception as e:  # noqa: BLE001
        logger.exception("查询设置列表失败: {}", e)
        return err("DB_ERROR", f"查询设置列表失败: {e}", status=503)


@router.put("/{key}")
async def update_setting(key: str, body: SettingUpdate) -> dict[str, Any]:
    """更新单项设置（upsert + 审计日志 + 缓存即时更新）。

    敏感键（尾部含 password/secret/key/token/passphrase）自动加密存储。
    """
    key = key.strip()
    error = _check_namespace(key)
    if error:
        return err("INVALID_KEY", error, status=400)

    try:
        success = await get_settings_service().set(
            key,
            body.value,
            updated_by=body.updated_by,
            description=body.description,
        )
        if not success:
            return err("DB_ERROR", f"写入设置 {key!r} 失败（DB 不可用）", status=503)
        return ok({"key": key, "updated": True, "value": body.value})
    except Exception as e:  # noqa: BLE001
        logger.exception("更新设置 {} 失败: {}", key, e)
        return err("DB_ERROR", f"更新设置失败: {e}", status=503)


__all__ = ["router"]
