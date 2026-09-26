"""PortfolioService — 个人资金计划与资产管理服务（对应 15-portfolio-architecture.md）。

职责：
1. 计划管理（Plan CRUD + 状态机流转：ACTIVE ↔ PAUSED → COMPLETED/CANCELLED）；
2. 交易账本（Ledger）：录入/查询/删除交易，交易后自动重算持仓聚合字段；
3. 持仓与绩效查询：总 BTC、平均成本、市值、浮盈亏、最大回撤、收益率；
4. 组合历史（快照序列）查询。

设计约束：
- 账本与计划分离：Plan 是「打算怎么做」，Ledger 是「实际发生什么」；
- 用户数据至高：所有操作在本地数据库完成，不依赖任何外部 Provider；
- user 级隔离：每个查询都强制带 user_id 过滤，杜绝越权；
- 金额一律 Decimal，数量精度 satoshi（8 位）。

会话管理：本服务接受外部传入的 AsyncSession（通常由 FastAPI 依赖注入或
get_db_session_ctx 提供），自身不 commit，由调用方控制事务边界。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PlanStatus, TradeSide
from app.models.portfolio import (
    PortfolioSnapshot,
    UserPlan,
    UserTransaction,
)
from app.portfolio.calculator import PortfolioCalculator, q_amount, q_btc
from app.schemas.portfolio import (
    CreatePlanSchema,
    CreateTransactionSchema,
    PlanPerformance,
    PortfolioHoldings,
    UpdatePlanSchema,
)

_Q_BTC = Decimal("0.00000001")
_Q_AMT = Decimal("0.01")


class PlanNotFoundError(Exception):
    """计划不存在或不属于该用户。"""


class PlanStateError(Exception):
    """计划状态机非法流转。"""


class PortfolioService:
    """个人资金计划与资产管理服务。

    Usage::

        async with get_db_session_ctx() as session:
            svc = PortfolioService(session)
            plan = await svc.create_plan(user_id, CreatePlanSchema(...))
            holdings = await svc.get_holdings(user_id)
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    # =====================================================================
    # 计划管理
    # =====================================================================

    async def create_plan(self, user_id: UUID, plan_data: CreatePlanSchema) -> UserPlan:
        """创建资金计划（状态初始为 ACTIVE），并推算首次投入日。"""
        plan = UserPlan(
            user_id=user_id,
            plan_name=plan_data.plan_name,
            plan_type=plan_data.plan_type,
            initial_capital=plan_data.initial_capital,
            currency=plan_data.currency,
            periodic_amount=plan_data.periodic_amount,
            periodic_frequency=plan_data.periodic_frequency,
            monthly_income=plan_data.monthly_income,
            start_date=plan_data.start_date,
            end_date=plan_data.end_date,
            investment_horizon_years=plan_data.investment_horizon_years,
            target_asset=plan_data.target_asset,
            cash_reserve=plan_data.cash_reserve,
            cash_reserve_pct=plan_data.cash_reserve_pct,
            max_single_investment=plan_data.max_single_investment,
            dca_rules=plan_data.dca_rules,
            risk_params=plan_data.risk_params,
            max_drawdown_tolerance=plan_data.max_drawdown_tolerance,
            stop_loss_enabled=plan_data.stop_loss_enabled,
            stop_loss_pct=plan_data.stop_loss_pct,
            drawdown_buy_rules=plan_data.drawdown_buy_rules or [],
            valuation_buy_rules=plan_data.valuation_buy_rules or [],
            custom_rules=plan_data.custom_rules or [],
            status=PlanStatus.ACTIVE,
            next_investment_date=self._next_investment_date(
                plan_data.periodic_frequency, plan_data.start_date, 1
            ),
        )
        self._session.add(plan)
        await self._session.flush()
        logger.info("创建资金计划 user={} plan={} id={}", user_id, plan.plan_name, plan.id)
        return plan

    async def get_plans(
        self, user_id: UUID, status: PlanStatus | None = None
    ) -> list[UserPlan]:
        """查询用户全部计划（可按状态过滤），按创建时间降序。"""
        stmt = select(UserPlan).where(UserPlan.user_id == user_id)
        if status is not None:
            stmt = stmt.where(UserPlan.status == status)
        stmt = stmt.order_by(UserPlan.created_at.desc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_plan(self, plan_id: UUID, user_id: UUID) -> UserPlan:
        """查询单个计划（强制 user 隔离），不存在则抛 PlanNotFoundError。"""
        stmt = select(UserPlan).where(UserPlan.id == plan_id).where(UserPlan.user_id == user_id)
        plan = (await self._session.execute(stmt)).scalars().first()
        if plan is None:
            raise PlanNotFoundError(f"计划 {plan_id} 不存在或不属于用户 {user_id}")
        return plan

    async def update_plan(
        self, plan_id: UUID, user_id: UUID, updates: UpdatePlanSchema
    ) -> UserPlan:
        """更新计划字段（仅更新显式提供的字段）。归档态（COMPLETED/CANCELLED）不可改。"""
        plan = await self.get_plan(plan_id, user_id)
        if plan.status in (PlanStatus.COMPLETED, PlanStatus.CANCELLED):
            raise PlanStateError(f"计划已归档（{plan.status.value}），不可修改")
        data = updates.model_dump(exclude_unset=True)
        for field_name, value in data.items():
            setattr(plan, field_name, value)
        # 定投参数变更后重算下次投入日
        if {"periodic_frequency", "start_date"} & data.keys():
            plan.next_investment_date = self._next_investment_date(
                plan.periodic_frequency, plan.start_date, 1
            )
        await self._session.flush()
        logger.info("更新资金计划 plan={} fields={}", plan_id, sorted(data.keys()))
        return plan

    async def pause_plan(self, plan_id: UUID, user_id: UUID) -> UserPlan:
        """暂停计划（ACTIVE → PAUSED）。"""
        return await self._transition(plan_id, user_id, PlanStatus.PAUSED, {PlanStatus.ACTIVE})

    async def resume_plan(self, plan_id: UUID, user_id: UUID) -> UserPlan:
        """恢复计划（PAUSED → ACTIVE）。"""
        return await self._transition(plan_id, user_id, PlanStatus.ACTIVE, {PlanStatus.PAUSED})

    async def complete_plan(self, plan_id: UUID, user_id: UUID) -> UserPlan:
        """完成计划（ACTIVE → COMPLETED，只读归档）。"""
        return await self._transition(
            plan_id, user_id, PlanStatus.COMPLETED, {PlanStatus.ACTIVE, PlanStatus.PAUSED}
        )

    async def cancel_plan(self, plan_id: UUID, user_id: UUID) -> UserPlan:
        """取消计划（ACTIVE/PAUSED → CANCELLED，只读归档）。"""
        return await self._transition(
            plan_id, user_id, PlanStatus.CANCELLED, {PlanStatus.ACTIVE, PlanStatus.PAUSED}
        )

    async def delete_plan(self, plan_id: UUID, user_id: UUID) -> bool:
        """删除计划（级联删除由外键 ondelete=CASCADE 处理关联交易）。"""
        plan = await self.get_plan(plan_id, user_id)
        await self._session.delete(plan)
        await self._session.flush()
        logger.info("删除资金计划 plan={} user={}", plan_id, user_id)
        return True

    async def _transition(
        self, plan_id: UUID, user_id: UUID, target: PlanStatus, allowed_from: set[PlanStatus]
    ) -> UserPlan:
        """计划状态机流转（校验合法来源状态）。"""
        plan = await self.get_plan(plan_id, user_id)
        if plan.status not in allowed_from:
            raise PlanStateError(
                f"非法状态流转：{plan.status.value} → {target.value}"
                f"（允许来源：{[s.value for s in allowed_from]}）"
            )
        plan.status = target
        if target == PlanStatus.PAUSED:
            plan.current_phase = "PAUSED"
        elif target == PlanStatus.ACTIVE:
            plan.current_phase = "ACTIVE"
            plan.next_investment_date = self._next_investment_date(
                plan.periodic_frequency, max(plan.start_date, date.today()), 1
            )
        await self._session.flush()
        logger.info("计划状态流转 plan={} → {}", plan_id, target.value)
        return plan

    # =====================================================================
    # 交易账本
    # =====================================================================

    async def add_transaction(
        self, user_id: UUID, tx_data: CreateTransactionSchema
    ) -> UserTransaction:
        """录入交易记录，并重算关联计划的累计聚合字段。

        amount / quantity_btc / price 三者按 §schema 校验规则互相推算。
        """
        price = Decimal(tx_data.price) if tx_data.price is not None else Decimal("0")
        amount = Decimal(tx_data.amount) if tx_data.amount is not None else None
        qty = Decimal(tx_data.quantity_btc) if tx_data.quantity_btc is not None else None
        if amount is not None and qty is None:
            qty = (amount / price).quantize(_Q_BTC, rounding=ROUND_HALF_UP) if price > 0 else None
        if qty is not None and amount is None:
            amount = (qty * price).quantize(_Q_AMT, rounding=ROUND_HALF_UP)
        if amount is None or qty is None:
            raise ValueError("无法确定交易金额与数量：请提供 price 及 amount/quantity_btc")

        tx_time = tx_data.transaction_time or datetime.now(UTC)
        if tx_time.tzinfo is None:
            tx_time = tx_time.replace(tzinfo=UTC)

        side = TradeSide.BUY if tx_data.side.upper() == "BUY" else TradeSide.SELL
        tx = UserTransaction(
            user_id=user_id,
            plan_id=tx_data.plan_id,
            transaction_time=tx_time,
            transaction_type=tx_data.transaction_type,
            side=side,
            amount=amount,
            currency=tx_data.currency,
            price=price,
            quantity_btc=qty,
            fee=Decimal(tx_data.fee or 0),
            fee_currency=tx_data.fee_currency,
            source=tx_data.source,
            exchange_name=tx_data.exchange_name,
            order_id=tx_data.order_id,
            notes=tx_data.notes,
            tags=tx_data.tags or [],
        )
        self._session.add(tx)
        await self._session.flush()

        # 重算该用户全部交易的累计字段（cumulative_*）与计划聚合
        await self._recompute_cumulative(user_id, tx_data.plan_id)
        logger.info(
            "录入交易 user={} side={} amount={} btc={}", user_id, side.value, amount, qty
        )
        return tx

    async def get_transactions(
        self,
        user_id: UUID,
        plan_id: UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[UserTransaction]:
        """查询交易记录（可按计划、时间区间过滤），按时间升序。"""
        stmt = select(UserTransaction).where(UserTransaction.user_id == user_id)
        if plan_id is not None:
            stmt = stmt.where(UserTransaction.plan_id == plan_id)
        if start is not None:
            stmt = stmt.where(UserTransaction.transaction_time >= start)
        if end is not None:
            stmt = stmt.where(UserTransaction.transaction_time <= end)
        stmt = stmt.order_by(UserTransaction.transaction_time.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    async def delete_transaction(self, tx_id: UUID, user_id: UUID) -> bool:
        """删除交易记录（强制 user 隔离），并重算累计字段。

        注：架构文档 §2.1 建议软删除留痕，此处提供硬删除用于用户纠错；
        审计留痕由上层 audit_logs 负责。
        """
        stmt = (
            select(UserTransaction)
            .where(UserTransaction.id == tx_id)
            .where(UserTransaction.user_id == user_id)
        )
        tx = (await self._session.execute(stmt)).scalars().first()
        if tx is None:
            return False
        plan_id = tx.plan_id
        await self._session.delete(tx)
        await self._session.flush()
        await self._recompute_cumulative(user_id, plan_id)
        logger.info("删除交易 tx={} user={}", tx_id, user_id)
        return True

    async def _recompute_cumulative(
        self, user_id: UUID, plan_id: UUID | None
    ) -> None:
        """重算用户交易的累计字段与计划聚合统计。

        cumulative_invested / cumulative_btc / avg_cost_after 按时间顺序回放；
        计划聚合（total_invested/total_btc/avg_cost）用移动加权平均成本口径。
        """
        txs = await self.get_transactions(user_id)
        cum_invested = Decimal("0")
        cum_btc = Decimal("0")
        cum_buy_amount = Decimal("0")
        cum_buy_btc = Decimal("0")
        plan_invested: dict[UUID | None, Decimal] = {}
        plan_btc: dict[UUID | None, Decimal] = {}
        for tx in txs:
            amount = Decimal(tx.amount or 0)
            qty = Decimal(tx.quantity_btc or 0)
            fee = Decimal(tx.fee or 0)
            side = tx.side if isinstance(tx.side, str) else tx.side.value
            if side == TradeSide.BUY.value:
                cum_invested += amount
                cum_btc += qty
                cum_buy_amount += amount + fee
                cum_buy_btc += qty
                plan_invested[tx.plan_id] = plan_invested.get(tx.plan_id, Decimal("0")) + amount
                plan_btc[tx.plan_id] = plan_btc.get(tx.plan_id, Decimal("0")) + qty
            else:
                cum_btc -= qty
                plan_btc[tx.plan_id] = plan_btc.get(tx.plan_id, Decimal("0")) - qty
            tx.cumulative_invested = q_amount(cum_invested)
            tx.cumulative_btc = q_btc(cum_btc)
            tx.avg_cost_after = (
                (cum_buy_amount / cum_buy_btc).quantize(_Q_BTC, rounding=ROUND_HALF_UP)
                if cum_buy_btc > 0 else Decimal("0")
            )

        # 更新涉及计划的聚合字段
        affected_plans = {plan_id} if plan_id is not None else set(plan_invested) | set(plan_btc)
        for pid in affected_plans:
            if pid is None:
                continue
            plan = (
                await self._session.execute(
                    select(UserPlan).where(UserPlan.id == pid).where(UserPlan.user_id == user_id)
                )
            ).scalars().first()
            if plan is None:
                continue
            p_btc = plan_btc.get(pid, Decimal("0"))
            p_inv = plan_invested.get(pid, Decimal("0"))
            plan.total_invested = q_amount(max(p_inv, Decimal("0")))
            plan.total_btc = q_btc(max(p_btc, Decimal("0")))
            plan.avg_cost = (
                (p_inv / p_btc).quantize(_Q_BTC, rounding=ROUND_HALF_UP)
                if p_btc > 0 else Decimal("0")
            )
            plan.last_investment_at = datetime.now(UTC)
        await self._session.flush()

    # =====================================================================
    # 持仓与绩效查询
    # =====================================================================

    async def get_holdings(
        self, user_id: UUID, current_price: Decimal | None = None
    ) -> PortfolioHoldings:
        """查询用户总持仓（总 BTC、平均成本、市值、盈亏）。

        Args:
            user_id: 用户 ID。
            current_price: 当前 BTC 价格（None 时市值/浮盈亏为 None）。
        """
        txs = await self.get_transactions(user_id)
        h = PortfolioCalculator.calculate_holdings(txs)
        price = Decimal(current_price) if current_price is not None else None
        market_value = unrealized = unrealized_pct = None
        if price is not None:
            market_value = q_amount(h.btc_balance * price)
            unrealized = q_amount(market_value - h.total_cost)
            if h.total_cost > 0:
                unrealized_pct = (unrealized / h.total_cost).quantize(
                    Decimal("0.000001"), rounding=ROUND_HALF_UP
                )
        return PortfolioHoldings(
            total_btc=h.btc_balance,
            avg_cost=h.avg_cost,
            total_invested=h.total_invested,
            total_cost=h.total_cost,
            current_price=price,
            market_value=market_value,
            unrealized_pnl=unrealized,
            unrealized_pnl_pct=unrealized_pct,
            realized_pnl=h.realized_pnl,
            total_buy_amount=h.total_buy_amount,
            total_sell_amount=h.total_sell_amount,
            total_fees=h.total_fees,
            transaction_count=h.transaction_count,
        )

    async def get_plan_performance(
        self, plan_id: UUID, user_id: UUID, current_price: Decimal | None = None
    ) -> PlanPerformance:
        """查询单计划绩效（投入、持仓、成本、收益、回撤）。"""
        plan = await self.get_plan(plan_id, user_id)
        txs = await self.get_transactions(user_id, plan_id=plan_id)
        snapshots = await self.get_portfolio_history(user_id, plan_id=plan_id)
        perf = PortfolioCalculator.calculate_performance(txs, current_price, snapshots)
        return PlanPerformance(
            plan_id=plan.id,
            total_invested=perf.total_invested,
            total_btc=perf.btc_balance,
            avg_cost=perf.avg_cost,
            realized_pnl=perf.realized_pnl,
            current_price=Decimal(current_price) if current_price is not None else None,
            market_value=perf.current_value,
            total_pnl=perf.pnl,
            total_return_pct=perf.pnl_percentage,
            max_drawdown=perf.max_drawdown,
            current_drawdown=perf.current_drawdown,
        )

    async def get_portfolio_history(
        self,
        user_id: UUID,
        start: date | None = None,
        end: date | None = None,
        plan_id: UUID | None = None,
    ) -> list[PortfolioSnapshot]:
        """查询组合净值快照序列（资产曲线数据），按观测时间升序。

        Args:
            user_id: 用户 ID。
            start: 起始日（含）。
            end: 结束日（含）。
            plan_id: 预留的计划过滤（当前 portfolio_snapshots 为用户级聚合，
                该参数保留以对齐 API 契约，暂不参与过滤）。
        """
        stmt = select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id)
        if start is not None:
            stmt = stmt.where(
                PortfolioSnapshot.observation_time
                >= datetime.combine(start, time.min, tzinfo=UTC)
            )
        if end is not None:
            stmt = stmt.where(
                PortfolioSnapshot.observation_time
                <= datetime.combine(end, time.max, tzinfo=UTC)
            )
        stmt = stmt.order_by(PortfolioSnapshot.observation_time.asc())
        return list((await self._session.execute(stmt)).scalars().all())

    # =====================================================================
    # 内部工具
    # =====================================================================

    @staticmethod
    def _next_investment_date(frequency: str, from_date: date, day: int = 1) -> date:
        """按频率推算 from_date 之后（含当日）的第一个投入日。

        DAILY：当日；WEEKLY：本周/下周第 day 天（ISO）；
        BIWEEKLY：当日（起始锚点）；MONTHLY：本月/下月第 day 日。
        """
        freq = (frequency or "MONTHLY").upper()
        if freq == "DAILY":
            return from_date
        if freq == "WEEKLY":
            target = min(max(day, 1), 7)
            delta = (target - from_date.isoweekday()) % 7
            return from_date + timedelta(days=delta)
        if freq == "BIWEEKLY":
            return from_date
        # MONTHLY
        dom = min(max(day, 1), 28)
        if from_date.day <= dom:
            try:
                return from_date.replace(day=dom)
            except ValueError:
                return from_date.replace(day=28)
        # 下个月
        y, m = from_date.year, from_date.month + 1
        if m > 12:
            m, y = 1, y + 1
        try:
            return date(y, m, dom)
        except ValueError:
            return date(y, m, 28)


__all__ = [
    "PortfolioService",
    "PlanNotFoundError",
    "PlanStateError",
    "PortfolioHoldings",
    "PlanPerformance",
]
