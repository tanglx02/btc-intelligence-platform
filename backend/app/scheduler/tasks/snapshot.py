"""组合快照任务：为所有有计划的用户生成每日组合净值快照。

observation_time 统一取当日 UTC 零点（幂等：同日重跑更新既有行）。
单用户失败仅告警并继续，不阻断其他用户。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.models.portfolio import PortfolioSnapshot, UserPlan
from app.portfolio.service import PortfolioService
from app.services.market_service import MarketService
from app.services.provider_service import get_provider_service

_STARTUP_LOCK = asyncio.Lock()

_PROVIDER_SYMBOL = "BTC/USDT"


async def _ensure_provider_service():
    """获取全局 ProviderService 并确保已启动（惰性）。"""
    ps = get_provider_service()
    if not getattr(ps, "_started", False):
        async with _STARTUP_LOCK:
            if not getattr(ps, "_started", False):
                await ps.startup()
    return ps


async def _current_price() -> Decimal | None:
    """实时 BTC 价格（失败返回 None，快照退化为按投入成本计值）。"""
    try:
        ps = await _ensure_provider_service()
        result = await MarketService(ps.manager).get_current_price(_PROVIDER_SYMBOL)
        if result.success and isinstance(result.data, dict):
            for key in ("price", "last", "last_price", "close"):
                value = result.data.get(key)
                if value is not None:
                    return Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取实时价格失败，快照将按投入成本计值: {}", exc)
    return None


async def generate_daily_snapshot() -> dict[str, Any]:
    """为所有用户生成当日组合快照（任务 daily_snapshot_1d）。"""
    async with get_db_session_ctx() as session:
        user_ids = (
            (await session.execute(select(UserPlan.user_id).distinct()))
            .scalars()
            .all()
        )
    if not user_ids:
        logger.info("无任何用户计划，跳过快照生成")
        return {"users": 0, "created": 0, "updated": 0, "skipped": 0}

    price = await _current_price()
    now = datetime.now(UTC)
    observation_time = now.replace(hour=0, minute=0, second=0, microsecond=0)

    created = updated = skipped = 0
    for user_id in user_ids:
        try:
            async with get_db_session_ctx() as session:
                service = PortfolioService(session)
                holdings = await service.get_holdings(user_id, price)

                market_value = holdings.market_value
                total_value = (
                    market_value if market_value is not None else holdings.total_invested
                )
                if market_value is not None:
                    btc_value = market_value
                elif holdings.total_btc and price:
                    btc_value = holdings.total_btc * price
                else:
                    btc_value = Decimal("0")

                cumulative_return: Decimal | None = None
                if holdings.total_invested and holdings.total_invested > 0:
                    cumulative_return = (
                        (total_value - holdings.total_invested) / holdings.total_invested
                    )

                existing = (
                    (
                        await session.execute(
                            select(PortfolioSnapshot).where(
                                PortfolioSnapshot.user_id == user_id,
                                PortfolioSnapshot.observation_time == observation_time,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

                if existing is None:
                    session.add(
                        PortfolioSnapshot(
                            user_id=user_id,
                            observation_time=observation_time,
                            snapshot_time=now,
                            total_value=total_value,
                            total_invested=holdings.total_invested,
                            cash_balance=Decimal("0"),
                            btc_value=btc_value,
                            btc_holdings=holdings.total_btc,
                            unrealized_pnl=holdings.unrealized_pnl,
                            realized_pnl=holdings.realized_pnl,
                            cumulative_return=cumulative_return,
                            allocation=(
                                {"current_price": float(price)} if price is not None else {}
                            ),
                            metrics={"transaction_count": holdings.transaction_count},
                        )
                    )
                    created += 1
                else:
                    existing.snapshot_time = now
                    existing.total_value = total_value
                    existing.total_invested = holdings.total_invested
                    existing.btc_value = btc_value
                    existing.btc_holdings = holdings.total_btc
                    existing.unrealized_pnl = holdings.unrealized_pnl
                    existing.realized_pnl = holdings.realized_pnl
                    existing.cumulative_return = cumulative_return
                    updated += 1
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            logger.warning("用户 {} 快照生成失败（跳过）: {}", user_id, exc)

    logger.info(
        "每日快照完成: users={} created={} updated={} skipped={}",
        len(user_ids), created, updated, skipped,
    )
    return {
        "users": len(user_ids),
        "created": created,
        "updated": updated,
        "skipped": skipped,
    }


__all__ = ["generate_daily_snapshot"]
