"""Providers API 路由 — 数据源管理（列表/健康详情/连通性测试/故障切换/评分）。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
数据库或 Provider 子系统不可用时返回 503，Provider 不存在返回 404。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.models.enums import ProviderCategory
from app.models.provider import (
    Provider,
    ProviderFailoverEvent,
    ProviderHealth,
    ProviderScore,
)
from app.services.provider_config_service import mask_provider_row
from app.services.provider_service import get_provider_service

router = APIRouter(prefix="/providers", tags=["Providers"])

_STARTUP_LOCK = asyncio.Lock()

# 故障切换事件默认回溯窗口（天）
_FAILOVER_WINDOW_DAYS = 30


async def _provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    """统一成功响应。"""
    return {"success": True, "data": data, "meta": meta}


def _unavailable(message: str, **meta: Any) -> JSONResponse:
    """统一 503 响应（数据服务不可用）。"""
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "data": None,
            "error": message or "数据服务暂时不可用",
            "meta": meta,
        },
    )


def _enum_value(v: Any) -> Any:
    """枚举 → value（非枚举原样返回）。"""
    return getattr(v, "value", v)


def _dec(v: Any) -> float | None:
    """Decimal/Numeric → float。"""
    return float(v) if v is not None else None


async def _get_provider_by_name(session: AsyncSession, name: str) -> Provider:
    """按名称查 Provider（大小写不敏感，YAML key 小写），不存在抛 404。"""
    provider = (
        await session.execute(
            select(Provider).where(func.lower(Provider.name) == name.strip().lower())
        )
    ).scalars().first()
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Provider {name!r} 不存在")
    return provider


def _latest_health_subquery():
    """每个 Provider 最新健康快照的窗口子查询。"""
    return (
        select(
            ProviderHealth.provider_id.label("provider_id"),
            func.max(ProviderHealth.check_time).label("check_time"),
        )
        .group_by(ProviderHealth.provider_id)
        .subquery()
    )


def _health_to_dict(h: ProviderHealth | None) -> dict[str, Any] | None:
    """ProviderHealth ORM → dict。"""
    if h is None:
        return None
    return {
        "check_time": h.check_time.isoformat() if h.check_time else None,
        "status": _enum_value(h.status),
        "response_time_ms": h.response_time_ms,
        "success_rate_1h": _dec(h.success_rate_1h),
        "success_rate_24h": _dec(h.success_rate_24h),
        "consecutive_failures": h.consecutive_failures,
        "today_failures": h.today_failures,
        "today_requests": h.today_requests,
        "data_latency_ms": h.data_latency_ms,
        "last_error": h.last_error,
        "last_error_type": h.last_error_type,
        "http_status": h.http_status,
        "is_rate_limited": h.is_rate_limited,
    }


def _score_to_dict(s: ProviderScore | None) -> dict[str, Any] | None:
    """ProviderScore ORM → dict。"""
    if s is None:
        return None
    return {
        "scored_at": s.scored_at.isoformat() if s.scored_at else None,
        "accuracy_score": _dec(s.accuracy_score),
        "latency_score": _dec(s.latency_score),
        "stability_score": _dec(s.stability_score),
        "completeness_score": _dec(s.completeness_score),
        "consistency_score": _dec(s.consistency_score),
        "overall_score": _dec(s.overall_score),
        "rank": s.rank,
    }


def _provider_to_dict(provider: Provider, health: ProviderHealth | None) -> dict[str, Any]:
    """Provider + 最新健康快照 → 列表项（config 为掩码后的配置明细）。"""
    return {
        "id": str(provider.id),
        "name": provider.name,
        "category": _enum_value(provider.category),
        "base_url": provider.base_url,
        "priority": provider.priority,
        "is_enabled": provider.is_enabled,
        "status": _enum_value(provider.status),
        "health_score": _dec(provider.health_score),
        "consecutive_failures": provider.consecutive_failures,
        "last_success_at": (
            provider.last_success_at.isoformat() if provider.last_success_at else None
        ),
        "last_failure_at": (
            provider.last_failure_at.isoformat() if provider.last_failure_at else None
        ),
        "description": provider.description,
        "latest_health": _health_to_dict(health),
        "config": mask_provider_row(provider),
    }


@router.get("/")
async def list_providers(
    category: str | None = Query(default=None, description="类别过滤: MARKET/ONCHAIN/ETF/..."),
    enabled_only: bool = Query(default=False, description="仅显示启用的 Provider"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Provider 列表 + 状态（providers 表 join 最新 provider_health 快照）。"""
    try:
        category_filter: ProviderCategory | None = None
        if category:
            try:
                category_filter = ProviderCategory(category.strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"非法 category: {category}，可选: {[c.value for c in ProviderCategory]}",
                ) from exc
        latest = _latest_health_subquery()
        stmt = (
            select(Provider, ProviderHealth)
            .join(latest, latest.c.provider_id == Provider.id, isouter=True)
            .join(
                ProviderHealth,
                and_(
                    ProviderHealth.provider_id == latest.c.provider_id,
                    ProviderHealth.check_time == latest.c.check_time,
                ),
                isouter=True,
            )
            .order_by(Provider.category, Provider.priority)
        )
        if category_filter is not None:
            stmt = stmt.where(Provider.category == category_filter)
        if enabled_only:
            stmt = stmt.where(Provider.is_enabled.is_(True))
        rows = (await session.execute(stmt)).all()
        items = [_provider_to_dict(p, h) for p, h in rows]
        return _ok(items, count=len(items), category=category)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询 Provider 列表失败: {}", exc)
        return _unavailable(f"Provider 列表服务不可用: {exc}")


@router.get("/failover-events")
async def list_failover_events(
    limit: int = Query(default=100, ge=1, le=500, description="返回条数"),
    days: int = Query(default=_FAILOVER_WINDOW_DAYS, ge=1, le=365, description="回溯天数"),
    data_category: str | None = Query(default=None, description="数据类别过滤"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """故障切换事件（查 provider_failover_events 表，按发生时间降序）。"""
    try:
        category_filter: ProviderCategory | None = None
        if data_category:
            try:
                category_filter = ProviderCategory(data_category.strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"非法 data_category: {data_category}",
                ) from exc
        since = datetime.now(timezone.utc) - timedelta(days=days)
        from_p = Provider.__table__.alias("from_provider")
        to_p = Provider.__table__.alias("to_provider")
        stmt = (
            select(
                ProviderFailoverEvent,
                from_p.c.name.label("from_name"),
                to_p.c.name.label("to_name"),
            )
            .join(from_p, from_p.c.id == ProviderFailoverEvent.from_provider_id)
            .join(to_p, to_p.c.id == ProviderFailoverEvent.to_provider_id, isouter=True)
            .where(ProviderFailoverEvent.occurred_at >= since)
            .order_by(ProviderFailoverEvent.occurred_at.desc())
            .limit(limit)
        )
        if category_filter is not None:
            stmt = stmt.where(ProviderFailoverEvent.data_category == category_filter)
        rows = (await session.execute(stmt)).all()
        items = [
            {
                "id": str(ev.id),
                "from_provider": from_name,
                "to_provider": to_name,
                "data_category": _enum_value(ev.data_category),
                "symbol": ev.symbol,
                "trigger_reason": ev.trigger_reason,
                "error_message": ev.error_message,
                "occurred_at": ev.occurred_at.isoformat() if ev.occurred_at else None,
                "resolved_at": ev.resolved_at.isoformat() if ev.resolved_at else None,
                "resolution_type": _enum_value(ev.resolution_type),
                "duration_seconds": ev.duration_seconds,
                "requests_affected": ev.requests_affected,
            }
            for ev, from_name, to_name in rows
        ]
        return _ok(items, count=len(items), since=since.isoformat(), limit=limit)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询故障切换事件失败: {}", exc)
        return _unavailable(f"故障切换事件服务不可用: {exc}")


@router.get("/scores")
async def list_scores(
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
    category: str | None = Query(default=None, description="类别过滤"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Provider 评分排名（每个 Provider 最新一次评分，按综合分降序）。"""
    try:
        category_filter: ProviderCategory | None = None
        if category:
            try:
                category_filter = ProviderCategory(category.strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail=f"非法 category: {category}"
                ) from exc
        latest = (
            select(
                ProviderScore.provider_id.label("provider_id"),
                func.max(ProviderScore.scored_at).label("scored_at"),
            )
            .group_by(ProviderScore.provider_id)
            .subquery()
        )
        stmt = (
            select(Provider, ProviderScore)
            .join(latest, latest.c.provider_id == Provider.id)
            .join(
                ProviderScore,
                and_(
                    ProviderScore.provider_id == latest.c.provider_id,
                    ProviderScore.scored_at == latest.c.scored_at,
                ),
            )
            .order_by(ProviderScore.overall_score.desc())
            .limit(limit)
        )
        if category_filter is not None:
            stmt = stmt.where(Provider.category == category_filter)
        rows = (await session.execute(stmt)).all()
        items = [
            {
                "provider": p.name,
                "category": _enum_value(p.category),
                **(_score_to_dict(s) or {}),
            }
            for p, s in rows
        ]
        return _ok(items, count=len(items))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询 Provider 评分失败: {}", exc)
        return _unavailable(f"Provider 评分服务不可用: {exc}")


@router.get("/{name}/health")
async def get_provider_health(
    name: str,
    history_limit: int = Query(default=50, ge=1, le=500, description="历史健康快照条数"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """单个 Provider 健康详情：状态、评分、最近健康快照历史。"""
    try:
        provider = await _get_provider_by_name(session, name)
        history = (
            (
                await session.execute(
                    select(ProviderHealth)
                    .where(ProviderHealth.provider_id == provider.id)
                    .order_by(ProviderHealth.check_time.desc())
                    .limit(history_limit)
                )
            )
            .scalars()
            .all()
        )
        latest_score = (
            (
                await session.execute(
                    select(ProviderScore)
                    .where(ProviderScore.provider_id == provider.id)
                    .order_by(ProviderScore.scored_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return _ok(
            {
                "provider": _provider_to_dict(provider, history[0] if history else None),
                "latest_score": _score_to_dict(latest_score),
                "history": [_health_to_dict(h) for h in history],
            },
            name=provider.name,
            history_count=len(history),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询 Provider {} 健康详情失败: {}", name, exc)
        return _unavailable(f"Provider 健康详情服务不可用: {exc}")


@router.post("/{name}/test")
async def test_provider(
    name: str,
) -> dict[str, Any]:
    """对指定 Provider 立即执行一次连通性/健康检查（实时探测）。"""
    try:
        ps = await _provider_service()
        snapshot = await ps.check_provider_health(name.strip())
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"Provider {name!r} 不存在")
        return _ok(snapshot, name=name, tested_at=datetime.now(timezone.utc).isoformat())
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("测试 Provider {} 连通性失败: {}", name, exc)
        return _unavailable(f"Provider 连通性测试服务不可用: {exc}")


# ---------------------------------------------------------------------------
# 配置后台化端点（读写 DB 配置 + 热重载生效）
# ---------------------------------------------------------------------------


class ProviderConfigUpdate(BaseModel):
    """Provider 配置部分更新请求体（仅提交的字段会被更新）。"""

    priority: int | None = None
    is_enabled: bool | None = None
    is_locked: bool | None = None
    base_url: str | None = None
    proxy: str | None = None
    timeout_config: dict[str, Any] | None = None
    retry_config: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None


class SyncFromYamlRequest(BaseModel):
    """YAML -> DB 显式同步请求体。"""

    overwrite: bool = False


@router.post("/sync-from-yaml")
async def sync_from_yaml(body: SyncFromYamlRequest | None = None) -> dict[str, Any]:
    """显式从 providers.yaml 导入配置到 providers 表。

    overwrite=False 时仅表空才导入；True 时覆盖已有行（保留加密凭据与
    后台修改的 config_overrides）。
    """
    try:
        from app.services.provider_config_service import get_provider_config_service

        overwrite = bool(body.overwrite) if body is not None else False
        count = await get_provider_config_service().import_yaml_to_db(force=overwrite)
        return _ok({"imported": count, "overwrite": overwrite})
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("providers.yaml 同步失败: {}", exc)
        return _unavailable(f"YAML 同步服务不可用: {exc}")


@router.put("/{name}")
async def update_provider(
    name: str,
    body: ProviderConfigUpdate,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """更新 Provider 配置（落库 + 热重载生效，响应掩码）。

    凭据约定：api_key/api_secret/api_passphrase 传 ""=保留旧值，
    传非空=加密存储，传 null=清除。轻量字段（enabled/priority/locked）
    直接改运行实例免重建；其余字段触发实例热重载。
    """
    try:
        updates = body.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=400, detail="请求体为空，无可更新字段")

        provider = await _get_provider_by_name(session, name)
        canonical = provider.name

        from app.services.provider_config_service import get_provider_config_service

        saved = await get_provider_config_service().save_provider_config(canonical, updates)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"Provider {name!r} 不存在")

        ps = await _provider_service()
        result = await ps.apply_config_change(canonical, updates)
        return _ok(
            {"name": canonical, "config": saved, **result},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("更新 Provider {} 配置失败: {}", name, exc)
        return _unavailable(f"Provider 配置更新服务不可用: {exc}")


@router.post("/{name}/reload")
async def reload_provider(
    name: str,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """手动热重载指定 Provider（重建实例 + 重建优先级队列）。"""
    try:
        provider = await _get_provider_by_name(session, name)
        canonical = provider.name
        ps = await _provider_service()
        reloaded = await ps.reload_provider(canonical)
        return _ok(
            {
                "name": canonical,
                "reloaded": reloaded,
                "note": None if reloaded else "Provider 已禁用或重载失败，运行时状态已同步",
            }
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("热重载 Provider {} 失败: {}", name, exc)
        return _unavailable(f"Provider 热重载服务不可用: {exc}")


__all__ = ["router"]
