"""Backtest API 路由 — 回测执行/历史记录/策略对比/策略列表/历史回放。

所有路由统一返回 ``{"success": bool, "data": ..., "meta": {...}}`` 结构；
引擎执行失败或依赖不可用时返回 503，参数错误返回 400，记录不存在返回 404。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.engine import BacktestEngine
from app.backtest.replay import HistoryReplay
from app.backtest.strategy import STRATEGY_REGISTRY
from app.backtest.types import BacktestConfig, BacktestResult as EngineResult, FillMode
from app.core.database import get_db_session, get_session_factory
from app.models.backtest import (
    BacktestResult as BacktestResultORM,
)
from app.models.backtest import (
    BacktestRun,
    BacktestTrade,
    Strategy,
    StrategyVersion,
)
from app.models.enums import BacktestStatus, TradeSide

router = APIRouter(prefix="/backtest", tags=["Backtest"])

# 对比回测并行上限
_MAX_COMPARE_RUNS = 5


# ---------------------------------------------------------------------------
# 请求模型与辅助
# ---------------------------------------------------------------------------

def _ok(data: Any, **meta: Any) -> dict[str, Any]:
    """统一成功响应。"""
    return {"success": True, "data": data, "meta": meta}


def _get_user_id(
    request: Request,
    x_user_id: str | None = Header(default=None, alias="x-user-id"),
) -> UUID | None:
    """从请求中提取用户 ID（中间件注入 → 请求头 → None 匿名）。"""
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
    return None


def _jsonify(obj: Any) -> Any:
    """递归把 Decimal/date/datetime/UUID 转成 JSON 可序列化类型。"""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonify(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_jsonify(v) for v in obj]
    return obj


class RunRequest(BaseModel):
    """单次回测请求体。"""

    strategy_name: str = Field(default="fixed_dca", description="策略名（见 /strategies）")
    strategy_params: dict[str, Any] = Field(default_factory=dict, description="策略参数")
    initial_capital: Decimal = Field(default=Decimal("0"), ge=0, description="初始资金")
    periodic_investment: Decimal | None = Field(default=None, gt=0, description="每期定投金额")
    frequency: str = Field(default="MONTHLY", description="DAILY/WEEKLY/BIWEEKLY/MONTHLY")
    dca_day: int = Field(default=1, ge=1, le=28)
    start_date: date = Field(description="回测开始日期")
    end_date: date = Field(description="回测结束日期")
    fee_rate: Decimal = Field(default=Decimal("0.001"), ge=0, le=Decimal("0.005"))
    slippage: Decimal = Field(default=Decimal("0.0005"), ge=0, le=Decimal("0.01"))
    symbol: str = Field(default="BTCUSDT", max_length=20)
    interval: str = Field(default="1d", max_length=10)
    fill_mode: str = Field(default="NEXT_OPEN", description="NEXT_OPEN / SAME_CLOSE")
    cash_reserve: Decimal = Field(default=Decimal("0"), ge=0)
    max_single_buy: Decimal | None = Field(default=None, gt=0)
    risk_free_rate: float = Field(default=0.02, ge=0, le=1)
    label: str | None = Field(default=None, max_length=100, description="对比展示标签")
    persist: bool = Field(default=True, description="是否持久化回测记录")


class CompareRequest(BaseModel):
    """策略对比请求体。"""

    runs: list[RunRequest] = Field(
        min_length=2, max_length=_MAX_COMPARE_RUNS, description="2~5 组回测配置"
    )


def _build_config(req: RunRequest) -> BacktestConfig:
    """RunRequest → BacktestConfig（参数错误抛 400）。"""
    try:
        fill_mode = FillMode(req.fill_mode.strip().upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"非法 fill_mode: {req.fill_mode}，可选: {[m.value for m in FillMode]}",
        ) from exc
    if req.start_date > req.end_date:
        raise HTTPException(status_code=400, detail="start_date 不得晚于 end_date")
    if req.initial_capital == 0 and (req.periodic_investment is None or req.periodic_investment == 0):
        raise HTTPException(status_code=400, detail="initial_capital 与 periodic_investment 不得同时为 0")
    try:
        return BacktestConfig(
            strategy_name=req.strategy_name.strip().lower(),
            strategy_params=dict(req.strategy_params),
            initial_capital=req.initial_capital,
            periodic_investment=req.periodic_investment,
            frequency=req.frequency.strip().upper(),
            dca_day=req.dca_day,
            start_date=req.start_date,
            end_date=req.end_date,
            fee_rate=req.fee_rate,
            slippage=req.slippage,
            symbol=req.symbol.strip().upper(),
            interval=req.interval,
            fill_mode=fill_mode,
            max_single_buy=req.max_single_buy,
            cash_reserve=req.cash_reserve,
            risk_free_rate=req.risk_free_rate,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _run_single(req: RunRequest, session: AsyncSession) -> EngineResult:
    """执行单次回测（引擎异常包装为 FAILED 结果再抛 503）。"""
    config = _build_config(req)
    start_dt = datetime.combine(req.start_date, datetime.min.time(), tzinfo=UTC)
    feed = _make_feed(start_dt, session)
    engine = BacktestEngine(feed=feed)
    result = await engine.run(config)
    if result.status != "COMPLETED":
        raise HTTPException(
            status_code=503,
            detail=f"回测执行失败: {result.error_message or 'unknown error'}",
        )
    return result


def _make_feed(start_dt: datetime, session: AsyncSession):
    from app.backtest.data_feed import PointInTimeDataFeed

    return PointInTimeDataFeed(current_time=start_dt, session=session)


def _jsonable(obj: Any) -> Any:
    """经 JSON round-trip 转成 JSONB 兼容结构（date/Decimal → str）。"""
    return json.loads(json.dumps(obj, default=str))


async def _persist_run(
    session: AsyncSession,
    req: RunRequest,
    user_id: UUID | None,
    result: EngineResult,
) -> UUID:
    """回测结果持久化：Strategy/StrategyVersion get-or-create → BacktestRun → 明细。"""
    config = result.config
    strategy = (
        await session.execute(select(Strategy).where(Strategy.name == config.strategy_name))
    ).scalars().first()
    if strategy is None:
        strategy = Strategy(
            name=config.strategy_name,
            category="DCA",
            description=f"Auto-registered strategy: {config.strategy_name}",
            is_system=True,
            is_active=True,
        )
        session.add(strategy)
        await session.flush()

    version = StrategyVersion(
        strategy_id=strategy.id,
        version="v1",
        parameters=_jsonable(config.strategy_params),
        is_current=True,
    )
    session.add(version)
    await session.flush()

    metrics = _jsonable(result.metrics)
    started = result.started_at or datetime.now(UTC)
    completed = result.completed_at or datetime.now(UTC)
    run = BacktestRun(
        strategy_version_id=version.id,
        user_id=user_id,
        status=BacktestStatus.COMPLETED,
        started_at=started,
        completed_at=completed,
        duration_seconds=result.duration_seconds,
        period_start=config.start_date,
        period_end=config.end_date,
        run_params=_jsonable(
            {
                "strategy_name": config.strategy_name,
                "strategy_params": config.strategy_params,
                "frequency": config.frequency,
                "dca_day": config.dca_day,
                "fee_rate": str(config.fee_rate),
                "slippage": str(config.slippage),
                "symbol": config.symbol,
                "interval": config.interval,
                "fill_mode": config.fill_mode.value,
                "label": req.label,
            }
        ),
        initial_capital=config.initial_capital,
        total_invested=result.total_invested,
        final_value=result.final_value,
        total_fees=result.total_fees,
        btc_accumulated=result.btc_accumulated,
        avg_cost_basis=result.avg_cost_basis,
        total_trades=len(result.trades),
        summary_metrics=metrics,
        benchmark_return=result.benchmark_return,
        data_snapshot_time=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()

    for p in result.equity_points:
        session.add(
            BacktestResultORM(
                backtest_run_id=run.id,
                observation_time=p.time,
                portfolio_value=p.portfolio_value,
                cash_balance=p.cash_balance,
                btc_holdings=p.btc_holdings,
                btc_price=p.btc_price,
                avg_cost=p.avg_cost,
                unrealized_pnl=p.unrealized_pnl,
                realized_pnl=p.realized_pnl,
                cumulative_invested=p.cumulative_invested,
                drawdown=p.drawdown,
                daily_return=p.daily_return,
                cumulative_return=p.cumulative_return,
            )
        )

    for t in result.trades:
        session.add(
            BacktestTrade(
                backtest_run_id=run.id,
                trade_time=t.time,
                observation_time=t.time,
                trade_number=t.trade_number,
                side=TradeSide(t.side.value),
                price=t.price,
                quantity_btc=t.quantity_btc,
                amount=t.amount,
                fee=t.fee,
                slippage=t.slippage,
                trigger_reason=(t.trigger_reason or "UNKNOWN")[:100],
                trigger_details=_jsonable(t.trigger_details or {}),
                pnl=t.pnl,
                cumulative_btc=t.cumulative_btc,
                cumulative_invested=t.cumulative_invested,
                avg_cost_after=t.avg_cost_after,
                portfolio_after=t.portfolio_after,
            )
        )
    await session.flush()
    return run.id


# ---------------------------------------------------------------------------
# 路由：执行 / 记录 / 对比
# ---------------------------------------------------------------------------

@router.post("/run")
async def run_backtest(
    req: RunRequest,
    session: AsyncSession = Depends(get_db_session),
    user_id: UUID | None = Depends(_get_user_id),
) -> dict[str, Any]:
    """运行回测（事件驱动引擎，PIT 防泄漏），默认持久化。"""
    try:
        result = await _run_single(req, session)
        run_id: UUID | None = None
        if req.persist:
            run_id = await _persist_run(session, req, user_id, result)
        return _ok(
            {"run_id": str(run_id) if run_id else None, "summary": _jsonify(result.summary())}
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("回测执行异常: {}", exc)
        raise HTTPException(status_code=503, detail=f"回测服务不可用: {exc}") from exc


def _run_row(run: BacktestRun, strategy_name: str) -> dict[str, Any]:
    """BacktestRun ORM → 摘要 dict。"""
    return {
        "run_id": str(run.id),
        "strategy_name": strategy_name,
        "status": run.status.value if isinstance(run.status, BacktestStatus) else str(run.status),
        "period_start": run.period_start.isoformat(),
        "period_end": run.period_end.isoformat(),
        "initial_capital": float(run.initial_capital) if run.initial_capital is not None else None,
        "total_invested": float(run.total_invested) if run.total_invested is not None else None,
        "final_value": float(run.final_value) if run.final_value is not None else None,
        "total_return": float(run.total_return) if run.total_return is not None else None,
        "annual_return": float(run.annual_return) if run.annual_return is not None else None,
        "max_drawdown": float(run.max_drawdown) if run.max_drawdown is not None else None,
        "sharpe_ratio": float(run.sharpe_ratio) if run.sharpe_ratio is not None else None,
        "win_rate": float(run.win_rate) if run.win_rate is not None else None,
        "total_trades": run.total_trades,
        "btc_accumulated": (
            float(run.btc_accumulated) if run.btc_accumulated is not None else None
        ),
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/runs")
async def list_runs(
    status: str | None = Query(default=None, description="状态过滤: PENDING/RUNNING/COMPLETED/FAILED/CANCELLED"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """回测记录列表（按创建时间降序，含策略名）。"""
    try:
        status_filter: BacktestStatus | None = None
        if status:
            try:
                status_filter = BacktestStatus(status.strip().upper())
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"非法 status: {status}，可选: {[s.value for s in BacktestStatus]}",
                ) from exc
        stmt = (
            select(BacktestRun, Strategy.name)
            .join(StrategyVersion, BacktestRun.strategy_version_id == StrategyVersion.id)
            .join(Strategy, StrategyVersion.strategy_id == Strategy.id)
            .order_by(BacktestRun.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if status_filter is not None:
            stmt = stmt.where(BacktestRun.status == status_filter)
        rows = (await session.execute(stmt)).all()
        items = [_run_row(run, name) for run, name in rows]
        return _ok(items, count=len(items), limit=limit, offset=offset)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询回测列表失败: {}", exc)
        raise HTTPException(status_code=503, detail=f"回测记录查询不可用: {exc}") from exc


@router.get("/runs/{run_id}")
async def get_run_detail(
    run_id: UUID,
    result_limit: int = Query(default=1000, ge=1, le=5000, description="净值点上限"),
    trade_limit: int = Query(default=500, ge=1, le=2000, description="交易明细上限"),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """回测详情：运行记录 + 净值曲线（backtest_results）+ 交易明细（backtest_trades）。"""
    try:
        run = await session.get(BacktestRun, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=f"回测记录 {run_id} 不存在")
        strategy_name = "unknown"
        if run.strategy_version_id is not None:
            version = await session.get(StrategyVersion, run.strategy_version_id)
            if version is not None:
                strategy = await session.get(Strategy, version.strategy_id)
                if strategy is not None:
                    strategy_name = strategy.name
        results = (
            (
                await session.execute(
                    select(BacktestResultORM)
                    .where(BacktestResultORM.backtest_run_id == run_id)
                    .order_by(BacktestResultORM.observation_time.asc())
                    .limit(result_limit)
                )
            )
            .scalars()
            .all()
        )
        trades = (
            (
                await session.execute(
                    select(BacktestTrade)
                    .where(BacktestTrade.backtest_run_id == run_id)
                    .order_by(BacktestTrade.trade_number.asc())
                    .limit(trade_limit)
                )
            )
            .scalars()
            .all()
        )
        result_items = [
            {
                "time": r.observation_time.isoformat(),
                "portfolio_value": float(r.portfolio_value),
                "cash_balance": float(r.cash_balance),
                "btc_holdings": float(r.btc_holdings),
                "btc_price": float(r.btc_price) if r.btc_price is not None else None,
                "avg_cost": float(r.avg_cost) if r.avg_cost is not None else None,
                "unrealized_pnl": (
                    float(r.unrealized_pnl) if r.unrealized_pnl is not None else None
                ),
                "realized_pnl": float(r.realized_pnl) if r.realized_pnl is not None else None,
                "cumulative_invested": (
                    float(r.cumulative_invested) if r.cumulative_invested is not None else None
                ),
                "drawdown": float(r.drawdown) if r.drawdown is not None else None,
                "daily_return": float(r.daily_return) if r.daily_return is not None else None,
                "cumulative_return": (
                    float(r.cumulative_return) if r.cumulative_return is not None else None
                ),
            }
            for r in results
        ]
        trade_items = [
            {
                "trade_number": t.trade_number,
                "time": t.trade_time.isoformat() if t.trade_time else None,
                "side": t.side.value if isinstance(t.side, TradeSide) else str(t.side),
                "price": float(t.price),
                "quantity_btc": float(t.quantity_btc),
                "amount": float(t.amount),
                "fee": float(t.fee),
                "slippage": float(t.slippage),
                "trigger_reason": t.trigger_reason,
                "pnl": float(t.pnl) if t.pnl is not None else None,
                "cumulative_btc": (
                    float(t.cumulative_btc) if t.cumulative_btc is not None else None
                ),
                "cumulative_invested": (
                    float(t.cumulative_invested) if t.cumulative_invested is not None else None
                ),
                "avg_cost_after": (
                    float(t.avg_cost_after) if t.avg_cost_after is not None else None
                ),
                "portfolio_after": (
                    float(t.portfolio_after) if t.portfolio_after is not None else None
                ),
            }
            for t in trades
        ]
        return _ok(
            {
                "run": _run_row(run, strategy_name),
                "error_message": run.error_message,
                "run_params": run.run_params,
                "summary_metrics": run.summary_metrics,
                "results": result_items,
                "trades": trade_items,
            },
            result_count=len(result_items),
            trade_count=len(trade_items),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("查询回测详情 {} 失败: {}", run_id, exc)
        raise HTTPException(status_code=503, detail=f"回测详情查询不可用: {exc}") from exc


@router.post("/compare")
async def compare_strategies(
    req: CompareRequest,
    user_id: UUID | None = Depends(_get_user_id),
) -> dict[str, Any]:
    """策略对比：多组回测并行执行（每组独立 session，互不影响）。"""
    try:
        factory = get_session_factory()

        async def _one(item: RunRequest) -> dict[str, Any]:
            try:
                async with factory() as s:
                    result = await _run_single(item, s)
                    run_id = None
                    if item.persist:
                        run_id = await _persist_run(s, item, user_id, result)
                    return {
                        "label": item.label or item.strategy_name,
                        "ok": True,
                        "run_id": str(run_id) if run_id else None,
                        "summary": _jsonify(result.summary()),
                    }
            except HTTPException as exc:
                return {"label": item.label or item.strategy_name, "ok": False, "error": str(exc.detail)}
            except Exception as exc:  # noqa: BLE001
                logger.exception("对比回测子任务失败（{}）: {}", item.label or item.strategy_name, exc)
                return {"label": item.label or item.strategy_name, "ok": False, "error": str(exc)}

        outcomes = await asyncio.gather(*(_one(item) for item in req.runs))
        succeeded = [o for o in outcomes if o.get("ok")]
        if not succeeded:
            raise HTTPException(status_code=503, detail="全部对比回测执行失败")
        return _ok(
            {"comparisons": outcomes, "best": _pick_best(succeeded)},
            total=len(outcomes),
            succeeded=len(succeeded),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("策略对比失败: {}", exc)
        raise HTTPException(status_code=503, detail=f"策略对比服务不可用: {exc}") from exc


def _pick_best(succeeded: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按 total_return 选取最优组（缺指标时忽略）。"""
    best: tuple[float, dict[str, Any]] | None = None
    for item in succeeded:
        ret = (item.get("summary") or {}).get("total_return")
        try:
            value = float(ret)
        except (TypeError, ValueError):
            continue
        if best is None or value > best[0]:
            best = (value, item)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# 路由：策略列表 / 历史回放
# ---------------------------------------------------------------------------

@router.get("/strategies")
async def list_strategies() -> dict[str, Any]:
    """可用策略列表（backtest/strategy.py 注册表，含别名）。"""
    items: list[dict[str, Any]] = []
    for key, cls in STRATEGY_REGISTRY.items():
        items.append(
            {
                "key": key,
                "name": getattr(cls, "name", key),
                "class": cls.__name__,
                "description": getattr(cls, "description", "") or "",
            }
        )
    return _ok(items, count=len(items))


_PERSPECTIVE_ALIASES = {"current": "current", "then": "current", "hindsight": "hindsight", "after": "hindsight"}


def _serialize_candle(bar: Any) -> dict[str, Any] | None:
    if bar is None:
        return None
    return {
        "time": bar.time.isoformat() if bar.time else None,
        "open": float(bar.open),
        "high": float(bar.high),
        "low": float(bar.low),
        "close": float(bar.close),
        "volume": float(bar.volume) if bar.volume is not None else None,
    }


@router.get("/replay/{target_date}")
async def replay(
    target_date: str,
    perspective: str = Query(
        default="current", description="current=当时视角 / hindsight=事后视角（含未来走势）"
    ),
    symbol: str = Query(default="BTCUSDT", max_length=20),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """历史回放：指定日期的市场状态（PIT），可选事后视角附未来 30/90/180/365 天走势。"""
    try:
        try:
            target = datetime.fromisoformat(target_date.strip())
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"非法日期: {target_date!r}，须为 ISO 格式（YYYY-MM-DD）"
            ) from exc
        persp_key = perspective.strip().lower()
        persp = _PERSPECTIVE_ALIASES.get(persp_key)
        if persp is None:
            raise HTTPException(
                status_code=400,
                detail=f"非法 perspective: {perspective}，可选: current / hindsight",
            )
        replay_service = HistoryReplay(session=session, symbol=symbol.strip().upper())
        snap = await replay_service.get_snapshot(target, perspective=persp)
        ms = snap.market_state
        payload: dict[str, Any] = {
            "date": snap.date.isoformat() if snap.date else None,
            "perspective": snap.perspective,
            "market_state": None,
            "future_outcome": None,
        }
        if ms is not None:
            payload["market_state"] = {
                "date": ms.date.isoformat() if ms.date else None,
                "price": float(ms.price) if ms.price is not None else None,
                "candle": _serialize_candle(ms.candle),
                "indicators": _jsonify(ms.indicators),
                "indicator_percentiles": _jsonify(ms.indicator_percentiles),
                "cycle_phase": ms.cycle_phase,
                "valuation_level": ms.valuation_level,
                "risk_level": ms.risk_level,
                "risk_score": ms.risk_score,
                "regime": ms.regime,
                "macro": _jsonify(ms.macro),
                "sentiment": _jsonify(ms.sentiment),
                "recomputed": ms.recomputed,
                "data_availability": _jsonify(ms.data_availability),
            }
        if snap.future_outcome is not None:
            fo = snap.future_outcome
            payload["future_outcome"] = {
                "base_date": fo.base_date.isoformat() if fo.base_date else None,
                "horizons": {
                    str(days): _jsonify(detail) for days, detail in fo.horizons.items()
                },
            }
        return _ok(payload, symbol=symbol, perspective=persp)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("历史回放 {} 失败: {}", target_date, exc)
        raise HTTPException(status_code=503, detail=f"历史回放服务不可用: {exc}") from exc


__all__ = ["router"]
