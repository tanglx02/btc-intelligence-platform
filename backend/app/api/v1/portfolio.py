"""Portfolio API 路由 — 个人资金计划/交易账本/持仓绩效/定投模拟/快照。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
本地数据库或依赖服务不可用时返回 503。

user_id 提取顺序：认证中间件注入的 request.state.user_id →
``x-user-id`` 请求头 → 开发兜底用户（未接入认证时的本地默认）。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import polars as pl
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.models.enums import CandleInterval, PlanStatus
from app.models.market import Candle
from app.portfolio.dca_simulator import DCAConfig, DCASimulator, SimulationResult
from app.portfolio.service import PlanNotFoundError, PlanStateError, PortfolioService
from app.schemas.portfolio import (
    CreatePlanSchema,
    CreateTransactionSchema,
    PlanPerformance,
    PlanResponse,
    PortfolioHoldings,
    SnapshotResponse,
    TransactionResponse,
    UpdatePlanSchema,
)
from app.services.provider_service import get_provider_service

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])

_STARTUP_LOCK = asyncio.Lock()

# 未接入认证时的开发兜底用户 ID
_DEV_USER_ID = UUID("00000000-0000-0000-0000-000000000001")

_SIM_FREQUENCIES = {"DAILY", "WEEKLY", "BIWEEKLY", "MONTHLY"}
_SIM_STRATEGIES = {"FIXED", "DIP_BUY", "DRAWDOWN_BUY", "VALUATION_BUY", "RISK_ADJUSTED", "CUSTOM"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    """统一成功响应。"""
    return {"success": True, "data": data, "meta": meta}


def _unavailable(message: str, **meta: Any) -> dict[str, Any]:
    """统一 503 响应体（由 HTTPException(503) 抛出，保持 FastAPI 错误结构）。"""
    return {"success": False, "data": None, "error": message or "服务暂时不可用", "meta": meta}


def _get_user_id(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="x-user-id"),
) -> UUID:
    """从请求中提取用户 ID（中间件注入 → 请求头 → 开发兜底）。"""
    state_uid = getattr(request.state, "user_id", None)
    if state_uid:
        try:
            return UUID(str(state_uid))
        except ValueError:
            pass
    if x_user_id:
        try:
            return UUID(x_user_id.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="x-user-id 必须为合法 UUID") from exc
    return _DEV_USER_ID


async def _current_price() -> Decimal | None:
    """尝试获取当前 BTC 价（失败时返回 None，不阻断持仓查询）。"""
    try:
        ps = get_provider_service()
        if not getattr(ps, "_started", False):
            async with _STARTUP_LOCK:
                if not getattr(ps, "_started", False):
                    await ps.startup()
        result = await ps.get_current_price("BTC/USDT")
        if result.success and isinstance(result.data, dict):
            price = result.data.get("price")
            if price is not None:
                return Decimal(str(price))
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取当前价格失败（持仓按无价口径返回）: {}", exc)
    return None


async def _svc(session: AsyncSession) -> PortfolioService:
    return PortfolioService(session)


async def _load_candles(
    session: AsyncSession,
    start: datetime,
    end: datetime,
    symbol: str = "BTCUSDT",
    interval: CandleInterval = CandleInterval.D1,
) -> pl.DataFrame:
    """从 candles 表加载价格序列（date/close[/high]），供定投模拟。"""
    stmt = (
        select(Candle)
        .where(
            Candle.symbol == symbol,
            Candle.interval == interval,
            Candle.observation_time >= start,
            Candle.observation_time <= end,
        )
        .order_by(Candle.observation_time.asc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    if not rows:
        return pl.DataFrame(schema={"date": pl.Date, "close": pl.Float64, "high": pl.Float64})
    data = {
        "date": [r.observation_time.date() for r in rows],
        "close": [float(r.close) if r.close is not None else None for r in rows],
        "high": [float(r.high) if r.high is not None else None for r in rows],
    }
    return pl.DataFrame(data)


def _jsonify(obj: Any) -> Any:
    """递归把 Decimal/date/datetime 转成 JSON 可序列化类型。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonify(v) for v in obj]
    return obj


def _simulation_payload(result: SimulationResult) -> dict[str, Any]:
    """SimulationResult → 统一响应 data。"""
    curve = result.equity_curve.tail(500).to_dicts()
    txs = result.transactions[-100:]
    return {
        "summary": {
            "total_invested": result.total_invested,
            "final_btc": result.final_btc,
            "final_value": result.final_value,
            "avg_cost": result.avg_cost,
            "total_return": result.total_return,
            "annualized_return": result.annualized_return,
            "max_drawdown": result.max_drawdown,
            "sharpe_ratio": result.sharpe_ratio,
            "total_fees": result.total_fees,
            "transaction_count": result.transaction_count,
        },
        "metrics": result.metrics,
        "transactions": [_jsonify(vars(t)) for t in txs],
        "equity_curve": _jsonify(curve),
    }


# ---------------------------------------------------------------------------
# 计划 CRUD
# ---------------------------------------------------------------------------

@router.post("/plans", status_code=201)
async def create_plan(
    plan_data: CreatePlanSchema,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """创建资金计划。"""
    try:
        svc = await _svc(session)
        plan = await svc.create_plan(user_id, plan_data)
        return _ok(PlanResponse.model_validate(plan).model_dump(), plan_id=str(plan.id))
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建资金计划失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"创建计划失败: {exc}")) from exc


@router.get("/plans")
async def list_plans(
    status: str | None = Query(default=None, description="状态过滤: ACTIVE/PAUSED/COMPLETED/CANCELLED"),
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """查询用户计划列表（可按状态过滤）。"""
    try:
        status_filter: PlanStatus | None = None
        if status:
            try:
                status_filter = PlanStatus(status.strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"非法 status: {status}，可选: {[s.value for s in PlanStatus]}",
                ) from exc
        svc = await _svc(session)
        plans = await svc.get_plans(user_id, status_filter)
        items = [PlanResponse.model_validate(p).model_dump() for p in plans]
        return _ok(items, count=len(items), user_id=str(user_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询计划列表失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询计划失败: {exc}")) from exc


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """查询计划详情（user 隔离）。"""
    try:
        svc = await _svc(session)
        plan = await svc.get_plan(plan_id, user_id)
        return _ok(PlanResponse.model_validate(plan).model_dump())
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询计划 {} 失败: {}", plan_id, exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询计划失败: {exc}")) from exc


@router.put("/plans/{plan_id}")
async def update_plan(
    plan_id: UUID,
    updates: UpdatePlanSchema,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """更新计划字段（归档态不可改）。"""
    try:
        svc = await _svc(session)
        plan = await svc.update_plan(plan_id, user_id, updates)
        return _ok(PlanResponse.model_validate(plan).model_dump())
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("更新计划 {} 失败: {}", plan_id, exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"更新计划失败: {exc}")) from exc


@router.delete("/plans/{plan_id}")
async def delete_plan(
    plan_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """删除计划（级联删除关联交易）。"""
    try:
        svc = await _svc(session)
        await svc.delete_plan(plan_id, user_id)
        return _ok({"deleted": True, "plan_id": str(plan_id)})
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("删除计划 {} 失败: {}", plan_id, exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"删除计划失败: {exc}")) from exc


# ---------------------------------------------------------------------------
# 交易账本
# ---------------------------------------------------------------------------

@router.post("/transactions", status_code=201)
async def add_transaction(
    tx_data: CreateTransactionSchema,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """录入交易记录（amount/quantity_btc/price 互相推算）。"""
    try:
        svc = await _svc(session)
        tx = await svc.add_transaction(user_id, tx_data)
        return _ok(TransactionResponse.model_validate(tx).model_dump(), tx_id=str(tx.id))
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("录入交易失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"录入交易失败: {exc}")) from exc


@router.get("/transactions")
async def list_transactions(
    plan_id: UUID | None = Query(default=None, description="按计划过滤"),
    start: datetime | None = Query(default=None, description="起始时间（ISO）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO）"),
    limit: int = Query(default=200, ge=1, le=1000, description="返回条数上限"),
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """查询交易记录（可按计划/时间区间过滤，时间升序）。"""
    try:
        if start is not None and end is not None and start > end:
            raise HTTPException(status_code=400, detail="start 不得晚于 end")
        svc = await _svc(session)
        txs = await svc.get_transactions(user_id, plan_id, start, end)
        txs = txs[-limit:]
        items = [TransactionResponse.model_validate(t).model_dump() for t in txs]
        return _ok(items, count=len(items), plan_id=str(plan_id) if plan_id else None)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询交易记录失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询交易失败: {exc}")) from exc


# ---------------------------------------------------------------------------
# 持仓 / 绩效
# ---------------------------------------------------------------------------

@router.get("/holdings")
async def get_holdings(
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """当前持仓（总 BTC、平均成本、市值、浮盈亏；价格获取失败时无市值口径）。"""
    try:
        svc = await _svc(session)
        price = await _current_price()
        holdings = await svc.get_holdings(user_id, price)
        return _ok(holdings.model_dump(), user_id=str(user_id), price_available=price is not None)
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询持仓失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询持仓失败: {exc}")) from exc


@router.get("/performance")
async def get_performance(
    plan_id: UUID = Query(description="计划 ID"),
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """单计划绩效（投入、持仓、成本、收益、回撤）。"""
    try:
        svc = await _svc(session)
        price = await _current_price()
        perf = await svc.get_plan_performance(plan_id, user_id, price)
        return _ok(
            PlanPerformance.model_validate(perf).model_dump(),
            price_available=price is not None,
        )
    except PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PlanStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询计划绩效失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询绩效失败: {exc}")) from exc


# ---------------------------------------------------------------------------
# 定投模拟
# ---------------------------------------------------------------------------

class DCASimulateRequest(BaseModel):
    """定投模拟请求体。"""

    initial_capital: Decimal = Field(default=Decimal("0"), ge=0, description="初始资金")
    periodic_amount: Decimal = Field(gt=0, description="每期投入金额")
    frequency: str = Field(default="MONTHLY", description="DAILY/WEEKLY/BIWEEKLY/MONTHLY")
    start_date: date = Field(description="开始日期")
    end_date: date | None = Field(default=None, description="结束日期（默认今天）")
    strategy: str = Field(default="FIXED", description="FIXED/DIP_BUY/DRAWDOWN_BUY/VALUATION_BUY/RISK_ADJUSTED/CUSTOM")
    fee_rate: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.05"), description="单边手续费率")
    dca_day: int = Field(default=1, ge=1, le=28, description="每月第 N 日 / 每周第 N 天")
    max_single_buy: Decimal | None = Field(default=None, gt=0, description="单次投入上限")
    cash_reserve: Decimal = Field(default=Decimal("0"), ge=0, description="现金储备下限")


@router.post("/simulate")
async def simulate_dca(
    req: DCASimulateRequest,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """定投策略模拟（内置 FIXED/DIP_BUY/DRAWDOWN_BUY/VALUATION_BUY/RISK_ADJUSTED）。"""
    try:
        freq = req.frequency.strip().upper()
        if freq not in _SIM_FREQUENCIES:
            raise HTTPException(
                status_code=400,
                detail=f"非法 frequency: {req.frequency}，可选: {sorted(_SIM_FREQUENCIES)}",
            )
        strategy = req.strategy.strip().upper()
        if strategy not in _SIM_STRATEGIES:
            raise HTTPException(
                status_code=400,
                detail=f"非法 strategy: {req.strategy}，可选: {sorted(_SIM_STRATEGIES)}",
            )
        end_date = req.end_date or date.today()
        if end_date < req.start_date:
            raise HTTPException(status_code=400, detail="end_date 不得早于 start_date")
        price_df = await _load_candles(
            session,
            datetime.combine(req.start_date, datetime.min.time(), tzinfo=timezone.utc)
            - timedelta(days=1),
            datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc),
        )
        if price_df.is_empty():
            raise HTTPException(
                status_code=400,
                detail="区间内无历史价格数据，无法模拟（请先采集 BTCUSDT 日线数据）",
            )
        config = DCAConfig(
            initial_capital=req.initial_capital,
            periodic_amount=req.periodic_amount,
            frequency=freq,
            start_date=req.start_date,
            end_date=end_date,
            strategy=strategy,
            fee_rate=req.fee_rate,
            dca_day=req.dca_day,
            max_single_buy=req.max_single_buy,
            cash_reserve=req.cash_reserve,
        )
        simulator = DCASimulator()
        result = await simulator.simulate(config, price_df)
        return _ok(_simulation_payload(result))
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("定投模拟失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"定投模拟失败: {exc}")) from exc


# ---------------------------------------------------------------------------
# 组合快照
# ---------------------------------------------------------------------------

@router.get("/snapshots")
async def list_snapshots(
    start: date | None = Query(default=None, description="起始日（含）"),
    end: date | None = Query(default=None, description="结束日（含）"),
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID = Depends(_get_user_id),
) -> dict[str, Any]:
    """组合净值快照历史（资产曲线数据点，观测时间升序）。"""
    try:
        if start is not None and end is not None and start > end:
            raise HTTPException(status_code=400, detail="start 不得晚于 end")
        svc = await _svc(session)
        rows = await svc.get_portfolio_history(user_id, start, end)
        items = [SnapshotResponse.model_validate(s).model_dump() for s in rows]
        return _ok(items, count=len(items), user_id=str(user_id))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询组合快照失败: {}", exc)
        raise HTTPException(status_code=503, detail=_unavailable(f"查询快照失败: {exc}")) from exc


__all__ = ["router"]
