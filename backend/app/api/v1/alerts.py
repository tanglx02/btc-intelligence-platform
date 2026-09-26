"""预警系统路由（/api/v1/alerts）。

端点分组：规则 CRUD / 事件 / 渠道 / SMTP / 摘要 / 统计与模板。

约定：
- SMTP 密码任何响应中只返回掩码（``***``），更新时空密码表示保留旧值；
- 端点函数不写联合类型返回注解（避免 FastAPI 挂载异常），直接返回 dict；
- 服务层异常统一转 500/503 JSONResponse，不向外抛。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Query
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.alerts.channels.registry import ChannelRegistry
from app.alerts.rule_templates import get_template, get_templates
from app.core.config import settings
from app.core.database import get_db_session_ctx
from app.models.alert import AlertChannelConfig
from app.schemas import err, ok
from app.services.alert_service import get_alert_service

router = APIRouter(prefix="/alerts", tags=["Alerts"])


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------


class RuleUpdateRequest(BaseModel):
    """规则部分更新请求体（字段可选，仅更新提供的字段）。"""

    rule_name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    severity: str | None = None
    category: str | None = None
    condition_tree: dict | None = None
    duration_seconds: int | None = None
    consecutive_count: int | None = Field(default=None, ge=1)
    cooldown_seconds: int | None = Field(default=None, ge=0)
    channels: list[str] | None = None
    channel_config: dict | None = None
    min_data_quality: str | None = None
    require_multi_source: bool | None = None
    priority: int | None = None
    tags: list[str] | None = None
    is_enabled: bool | None = None


class ChannelCreateRequest(BaseModel):
    """添加通知渠道配置请求体。"""

    channel_type: str = "EMAIL"
    channel_name: str = Field(min_length=1, max_length=100)
    config: dict = Field(default_factory=dict)
    is_primary: bool = False


class SmtpConfigRequest(BaseModel):
    """SMTP 配置更新请求体（password=None/"" 表示保留旧值）。"""

    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    user: str | None = None
    password: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    use_tls: bool | None = None
    use_ssl: bool | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    recipient: str | None = None


class TestRequest(BaseModel):
    """测试发送请求体。"""

    recipient: str | None = None


class DigestConfigRequest(BaseModel):
    """摘要配置更新请求体。"""

    enabled: bool | None = None
    daily_hour: int | None = Field(default=None, ge=0, le=23)
    weekly_day: int | None = Field(default=None, ge=0, le=6)
    weekly_hour: int | None = Field(default=None, ge=0, le=23)
    recipients: list[str] | None = None


class DigestSendRequest(BaseModel):
    """手动发送摘要请求体。"""

    type: str = Field(default="daily", pattern="^(daily|weekly)$")
    recipients: list[str] | None = None


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

_SMTP_CONFIG_KEYS = (
    "enabled", "host", "port", "user", "password", "from_email",
    "from_name", "use_tls", "use_ssl", "timeout_seconds", "recipient",
)


def _mask(secret: str | None) -> str:
    """敏感信息掩码（已配置 -> ***；空 -> ""）。"""
    return "***" if secret else ""


def _mask_smtp_config(config: dict) -> dict:
    """SMTP 配置脱敏副本（password 掩码）。"""
    masked = {k: v for k, v in config.items() if k in _SMTP_CONFIG_KEYS}
    masked["password"] = _mask(masked.get("password"))
    return masked


def _channel_row_to_dict(row: AlertChannelConfig) -> dict:
    """AlertChannelConfig ORM -> 响应 dict（SMTP 配置密码脱敏）。"""
    config = dict(row.config or {})
    if row.channel_type == "SMTP":
        config = _mask_smtp_config(config)
    return {
        "id": str(row.id),
        "channel_type": row.channel_type,
        "channel_name": row.channel_name,
        "config": config,
        "is_primary": row.is_primary,
        "is_verified": row.is_verified,
        "is_enabled": row.is_enabled,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def _iso(value):
    """datetime -> ISO 字符串（None 安全）。"""
    if value is None:
        return None
    dt = value if value.tzinfo else value.replace(tzinfo=UTC)
    return dt.isoformat()


async def _load_smtp_runtime_config() -> dict:
    """合并 SMTP 配置：settings 默认 + DB 系统级覆盖（DB 键优先）。"""
    merged: dict = {
        "enabled": settings.smtp_enabled,
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "user": settings.smtp_user,
        "password": settings.smtp_password,
        "from_email": settings.smtp_from_email,
        "from_name": settings.smtp_from_name,
        "use_tls": settings.smtp_use_tls,
        "use_ssl": settings.smtp_use_ssl,
        "timeout_seconds": settings.smtp_timeout_seconds,
        "recipient": settings.alert_default_recipient,
    }
    try:
        async with get_db_session_ctx() as session:
            row = (
                await session.execute(
                    select(AlertChannelConfig)
                    .where(
                        AlertChannelConfig.channel_type == "SMTP",
                        AlertChannelConfig.user_id.is_(None),  # type: ignore[union-attr]
                    )
                    .order_by(AlertChannelConfig.updated_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        if row is not None:
            for key, value in dict(row.config or {}).items():
                if value is not None and value != "":
                    merged[key] = value
    except Exception as e:  # noqa: BLE001 - DB 不可用回退 env
        logger.warning(f"alerts API: SMTP 运行时配置读取失败，回退 env: {e}")
    return merged


async def _save_smtp_runtime_config(updates: dict) -> None:
    """保存 SMTP 运行时配置（upsert 系统级 SMTP 行；空密码保留旧值）。"""
    clean = {k: v for k, v in updates.items() if k in _SMTP_CONFIG_KEYS and v is not None}
    if not clean.get("password"):
        clean.pop("password", None)  # 空密码 = 保留旧值
    async with get_db_session_ctx() as session:
        row = (
            await session.execute(
                select(AlertChannelConfig)
                .where(
                    AlertChannelConfig.channel_type == "SMTP",
                    AlertChannelConfig.user_id.is_(None),  # type: ignore[union-attr]
                )
                .order_by(AlertChannelConfig.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                AlertChannelConfig(
                    channel_type="SMTP",
                    channel_name="系统 SMTP",
                    config=clean,
                    is_enabled=True,
                )
            )
        else:
            stored = dict(row.config or {})
            for key, value in clean.items():
                if key == "password" and not value:
                    continue  # 空密码 = 保留旧值
                stored[key] = value
            row.config = stored


# ----------------------------------------------------------------------
# 规则 CRUD
# ----------------------------------------------------------------------


@router.post("/rules")
async def create_rule(body: dict):
    """创建预警规则（body 校验复用 AlertRuleCreate）。"""
    try:
        from app.alerts.schemas import AlertRuleCreate

        data = AlertRuleCreate.model_validate(body)
        result = await get_alert_service().create_rule(data)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_CREATE_FAILED", f"创建规则失败: {e}", status=400)


@router.get("/rules")
async def list_rules(
    status: str | None = Query(default=None, description="ACTIVE/PAUSED/ARCHIVED/ERROR"),
    severity: str | None = Query(default=None, description="INFO~CRITICAL"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    """预警规则列表（分页）。"""
    try:
        return ok(await get_alert_service().list_rules(status, severity, page, size))
    except Exception as e:  # noqa: BLE001
        return err("RULE_LIST_FAILED", f"查询规则失败: {e}", status=500)


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: UUID):
    """规则详情。"""
    try:
        result = await get_alert_service().get_rule(rule_id)
        if result is None:
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_GET_FAILED", f"查询规则失败: {e}", status=500)


@router.put("/rules/{rule_id}")
async def update_rule(rule_id: UUID, body: RuleUpdateRequest):
    """更新规则（部分字段）。"""
    try:
        updates = body.model_dump(exclude_unset=True, exclude_none=True)
        result = await get_alert_service().update_rule(rule_id, updates)
        if result is None:
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_UPDATE_FAILED", f"更新规则失败: {e}", status=400)


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: UUID):
    """删除规则（软删：is_enabled=False + state=ARCHIVED）。"""
    try:
        deleted = await get_alert_service().delete_rule(rule_id)
        if not deleted:
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok({"deleted": True, "rule_id": str(rule_id)})
    except Exception as e:  # noqa: BLE001
        return err("RULE_DELETE_FAILED", f"删除规则失败: {e}", status=500)


@router.post("/rules/{rule_id}/test")
async def test_rule(rule_id: UUID):
    """测试规则（立即求值一次，返回是否命中 + 证据链）。"""
    try:
        result = await get_alert_service().test_rule(rule_id)
        if not result.get("found"):
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_TEST_FAILED", f"规则测试失败: {e}", status=500)


@router.post("/rules/{rule_id}/pause")
async def pause_rule(rule_id: UUID):
    """暂停规则。"""
    try:
        result = await get_alert_service().pause_rule(rule_id)
        if result is None:
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_PAUSE_FAILED", f"暂停规则失败: {e}", status=500)


@router.post("/rules/{rule_id}/resume")
async def resume_rule(rule_id: UUID):
    """恢复规则。"""
    try:
        result = await get_alert_service().resume_rule(rule_id)
        if result is None:
            return err("NOT_FOUND", "规则不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("RULE_RESUME_FAILED", f"恢复规则失败: {e}", status=500)


@router.post("/rules/{rule_id}/backtest")
async def backtest_rule(
    rule_id: UUID,
    start: datetime | None = Query(default=None, description="开始时间（默认 90 天前）"),
    end: datetime | None = Query(default=None, description="结束时间（默认当前）"),
):
    """历史触发模拟（每日快照式求值 + 触发后 7/30/90 天表现）。"""
    try:
        end_dt = end or datetime.now(UTC)
        start_dt = start or (end_dt - timedelta(days=90))
        return ok(await get_alert_service().backtest_rule(rule_id, start_dt, end_dt))
    except Exception as e:  # noqa: BLE001
        return err("RULE_BACKTEST_FAILED", f"历史回测失败: {e}", status=500)


# ----------------------------------------------------------------------
# 事件
# ----------------------------------------------------------------------


@router.get("/events")
async def get_events(
    rule_id: UUID | None = Query(default=None),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
):
    """触发历史（分页 + 规则/时间过滤）。"""
    try:
        return ok(
            await get_alert_service().get_events(rule_id, start, end, page, size)
        )
    except Exception as e:  # noqa: BLE001
        return err("EVENT_LIST_FAILED", f"查询事件失败: {e}", status=500)


@router.post("/events/{event_id}/ack")
async def ack_event(event_id: UUID):
    """确认事件。"""
    try:
        result = await get_alert_service().ack_event(event_id)
        if result is None:
            return err("NOT_FOUND", "事件不存在", status=404)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        return err("EVENT_ACK_FAILED", f"确认事件失败: {e}", status=500)


# ----------------------------------------------------------------------
# 渠道
# ----------------------------------------------------------------------


@router.get("/channels")
async def list_channels():
    """渠道列表（可用渠道类型 + 已保存配置）。"""
    try:
        async with get_db_session_ctx() as session:
            rows = (
                await session.execute(
                    select(AlertChannelConfig)
                    .order_by(AlertChannelConfig.channel_type.asc())
                )
            ).scalars().all()
        available = ChannelRegistry.registered_types() + ["WEBHOOK", "TELEGRAM"]
        return ok({
            "available": sorted(set(available)),
            "items": [_channel_row_to_dict(r) for r in rows],
        })
    except Exception as e:  # noqa: BLE001
        return err("CHANNEL_LIST_FAILED", f"查询渠道失败: {e}", status=500)


@router.post("/channels")
async def create_channel(body: ChannelCreateRequest):
    """添加通知渠道配置。"""
    try:
        async with get_db_session_ctx() as session:
            row = AlertChannelConfig(
                channel_type=body.channel_type.upper(),
                channel_name=body.channel_name,
                config=body.config,
                is_primary=body.is_primary,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            return ok(_channel_row_to_dict(row))
    except Exception as e:  # noqa: BLE001
        return err("CHANNEL_CREATE_FAILED", f"添加渠道失败: {e}", status=400)


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: UUID, body: TestRequest | None = None):
    """测试渠道（发送测试邮件）。"""
    try:
        async with get_db_session_ctx() as session:
            row = await session.get(AlertChannelConfig, channel_id)
        if row is None:
            return err("NOT_FOUND", "渠道不存在", status=404)
        channel_impl = ChannelRegistry.get(row.channel_type)
        if channel_impl is None:
            return err(
                "CHANNEL_NOT_AVAILABLE",
                f"渠道类型 {row.channel_type} 尚未实现",
                status=400,
            )
        config = dict(row.config or {})
        recipient = (
            (body.recipient if body else None)
            or config.get("recipient")
            or settings.alert_default_recipient
        )
        if not recipient:
            return err("MISSING_RECIPIENT", "未指定收件人", status=400)
        result = await channel_impl.test(recipient, config)
        return ok({
            "channel_type": row.channel_type,
            "recipient": recipient,
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "response": result.response,
        })
    except Exception as e:  # noqa: BLE001
        return err("CHANNEL_TEST_FAILED", f"渠道测试失败: {e}", status=500)


# ----------------------------------------------------------------------
# SMTP
# ----------------------------------------------------------------------


@router.get("/smtp/config")
async def get_smtp_config():
    """获取 SMTP 配置（密码掩码）。"""
    try:
        merged = await _load_smtp_runtime_config()
        return ok(_mask_smtp_config(merged))
    except Exception as e:  # noqa: BLE001
        return err("SMTP_CONFIG_GET_FAILED", f"读取 SMTP 配置失败: {e}", status=500)


@router.put("/smtp/config")
async def update_smtp_config(body: SmtpConfigRequest):
    """更新 SMTP 配置（空密码保留旧值）。"""
    try:
        await _save_smtp_runtime_config(body.model_dump(exclude_unset=True))
        merged = await _load_smtp_runtime_config()
        return ok(_mask_smtp_config(merged))
    except Exception as e:  # noqa: BLE001
        return err("SMTP_CONFIG_UPDATE_FAILED", f"更新 SMTP 配置失败: {e}", status=400)


@router.post("/smtp/test")
async def test_smtp(body: TestRequest | None = None):
    """测试 SMTP（发测试邮件，返回成功/失败/错误原因/响应）。"""
    try:
        merged = await _load_smtp_runtime_config()
        recipient = (
            (body.recipient if body else None)
            or merged.get("recipient")
            or settings.alert_default_recipient
        )
        if not recipient:
            return err("MISSING_RECIPIENT", "未指定收件人", status=400)

        from app.alerts.channels.email_smtp import EmailChannel

        result = await EmailChannel().test(recipient, merged)
        return ok({
            "recipient": recipient,
            "success": result.success,
            "message_id": result.message_id,
            "error": result.error,
            "response": result.response,
        })
    except Exception as e:  # noqa: BLE001
        return err("SMTP_TEST_FAILED", f"SMTP 测试失败: {e}", status=500)


# ----------------------------------------------------------------------
# 摘要
# ----------------------------------------------------------------------


@router.get("/digest/config")
async def get_digest_config():
    """摘要配置。"""
    try:
        from app.alerts.digest import get_digest_service

        config = await get_digest_service().get_digest_config()
        return ok(config)
    except Exception as e:  # noqa: BLE001
        return err("DIGEST_CONFIG_GET_FAILED", f"读取摘要配置失败: {e}", status=500)


@router.put("/digest/config")
async def update_digest_config(body: DigestConfigRequest):
    """更新摘要配置。"""
    try:
        from app.alerts.digest import get_digest_service

        config = await get_digest_service().update_digest_config(
            body.model_dump(exclude_unset=True)
        )
        return ok(config)
    except Exception as e:  # noqa: BLE001
        return err("DIGEST_CONFIG_UPDATE_FAILED", f"更新摘要配置失败: {e}", status=400)


@router.post("/digest/send")
async def send_digest(body: DigestSendRequest):
    """手动发送摘要（daily / weekly）。"""
    try:
        from app.alerts.digest import get_digest_service

        service = get_digest_service()
        if body.type == "weekly":
            result = await service.send_weekly_report(body.recipients)
        else:
            result = await service.send_daily_digest(body.recipients)
        return ok({"type": body.type, **result})
    except Exception as e:  # noqa: BLE001
        return err("DIGEST_SEND_FAILED", f"发送摘要失败: {e}", status=500)


# ----------------------------------------------------------------------
# 统计与模板
# ----------------------------------------------------------------------


@router.get("/stats")
async def get_stats():
    """统计（总规则数/活跃数/今日触发/各 severity 分布）。"""
    try:
        return ok(await get_alert_service().get_stats())
    except Exception as e:  # noqa: BLE001
        return err("STATS_FAILED", f"统计查询失败: {e}", status=500)


@router.get("/templates")
async def list_templates():
    """预设模板列表。"""
    return ok(get_templates())


@router.get("/templates/{template_id}")
async def get_template_detail(template_id: str):
    """模板详情。"""
    template = get_template(template_id)
    if template is None:
        return err("NOT_FOUND", f"模板 {template_id} 不存在", status=404)
    return ok(template)
