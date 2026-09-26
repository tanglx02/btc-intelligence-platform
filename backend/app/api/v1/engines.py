"""分析引擎路由（/api/v1/engine）。

提供四大引擎（cycle/valuation/risk/regime）的最新状态、首页聚合看板
与历史查询。引擎实例惰性构建并复用；数据从数据库读取（get_latest），
数据库不可用时返回 null 而非 500（引擎层内部已容错）。

红线：引擎只输出描述性结论与证据链，本路由不做任何二次解读。
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from app.core.database import get_db_session_ctx
from app.schemas import err, ok

router = APIRouter(prefix="/engine", tags=["Engines"])

# --------------------------------------------------------------------------- #
# 引擎实例管理
# --------------------------------------------------------------------------- #
_ENGINE_CLASSES: dict[str, str] = {
    "cycle": "CycleEngine",
    "valuation": "ValuationEngine",
    "risk": "RiskEngine",
    "regime": "MarketRegimeEngine",
}

_engine_instances: dict[str, Any] = {}


def _get_engine(name: str) -> Any:
    """按名称惰性构建并复用引擎实例（未知名称抛 KeyError）。"""
    if name in _engine_instances:
        return _engine_instances[name]
    import app.engines as engines_pkg

    cls_name = _ENGINE_CLASSES.get(name)
    if cls_name is None or not hasattr(engines_pkg, cls_name):
        raise KeyError(name)
    engine = getattr(engines_pkg, cls_name)()
    _engine_instances[name] = engine
    return engine


# --------------------------------------------------------------------------- #
# EngineOutput 序列化
# --------------------------------------------------------------------------- #
def _jsonable(v: Any) -> Any:
    """证据值 -> JSON 安全值。"""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x) for x in v]
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "item"):
        try:
            return _jsonable(v.item())
        except Exception:  # noqa: BLE001
            pass
    return str(v)


def _evidence_to_dict(ev: Any) -> dict[str, Any]:
    """EngineEvidence -> dict。"""
    return {
        "factor": ev.factor,
        "value": _jsonable(ev.value),
        "interpretation": ev.interpretation,
        "weight": ev.weight,
        "supports": ev.supports,
        "confidence": ev.confidence,
        "data_source": ev.data_source,
        "historical_percentile": ev.historical_percentile,
        "indicator_code": ev.indicator_code,
        "quality_status": ev.quality_status,
    }


def engine_output_to_dict(output: Any) -> dict[str, Any] | None:
    """EngineOutput -> JSON 友好 dict（None 安全）。"""
    if output is None:
        return None
    return {
        "state": output.state,
        "score": output.score,
        "confidence": output.confidence,
        "explanation": output.explanation,
        "data_quality": output.data_quality,
        "observation_time": output.observation_time.isoformat() if output.observation_time else None,
        "calculated_at": output.calculated_at.isoformat() if output.calculated_at else None,
        "dimension_scores": _jsonable(output.dimension_scores),
        "data_gaps": output.data_gaps,
        "model_version": output.model_version,
        "supporting_evidence": [_evidence_to_dict(e) for e in output.supporting_evidence],
        "opposing_evidence": [_evidence_to_dict(e) for e in output.opposing_evidence],
        "historical_similar": _jsonable(output.historical_similar),
        "metadata": _jsonable(output.metadata),
    }


async def _latest_payload(name: str) -> dict[str, Any] | None:
    """读取引擎最新状态并序列化（无数据返回 None）。"""
    engine = _get_engine(name)
    output = await engine.get_latest()
    return engine_output_to_dict(output)


# --------------------------------------------------------------------------- #
# 单引擎端点
# --------------------------------------------------------------------------- #
def _register_engine_endpoint(route_name: str, engine_name: str) -> None:
    """注册单引擎最新状态端点（/cycle /valuation /risk /regime）。"""

    async def _endpoint():  # noqa: ANN202
        """查询该引擎最新落库状态（含证据链），无数据时返回 null。"""
        try:
            payload = await _latest_payload(engine_name)
            return ok({"engine": engine_name, "output": payload})
        except KeyError:
            return err("NOT_FOUND", f"未知引擎: {engine_name}", status=404)
        except Exception as e:  # noqa: BLE001
            return err("ENGINE_ERROR", f"查询 {engine_name} 引擎状态失败: {e}", status=503)

    _endpoint.__name__ = f"get_{engine_name}_latest"
    _endpoint.__doc__ = f"获取 {engine_name} 引擎最新分析状态（state + 置信度 + 证据链）。"
    router.add_api_route(
        f"/{route_name}",
        _endpoint,
        methods=["GET"],
        summary=f"{engine_name} 引擎最新状态",
    )


for _name in ("cycle", "valuation", "risk", "regime"):
    _register_engine_endpoint(_name, _name)


# --------------------------------------------------------------------------- #
# 聚合看板
# --------------------------------------------------------------------------- #
@router.get("/dashboard")
async def engine_dashboard(
    include_price: bool = Query(default=True, description="是否附带当前 BTC 价格"),
):
    """聚合四大引擎最新状态（首页 9 大维度看板数据源），逐引擎容错。"""
    names = ("cycle", "valuation", "risk", "regime")

    async def _safe_latest(nm: str) -> dict[str, Any] | str | None:
        try:
            return (await _latest_payload(nm)) or None
        except Exception as e:  # noqa: BLE001 - 单引擎失败不阻断看板
            return f"error: {e}"

    results = await asyncio.gather(*(_safe_latest(nm) for nm in names))

    data: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for nm, res in zip(names, results):
        if isinstance(res, str):
            errors[nm] = res
        else:
            data[nm] = res

    if include_price:
        try:
            from app.services.market_service import MarketService
            from app.services.provider_service import get_provider_service

            service = MarketService(get_provider_service().manager)
            price_res = await service.get_current_price("BTCUSDT", cross_validate=False)
            data["price"] = (
                {"symbol": "BTCUSDT", **price_res.data} if price_res.success else None
            )
        except Exception as e:  # noqa: BLE001 - 价格失败不阻断看板
            errors["price"] = f"error: {e}"

    return ok({**data, "errors": errors or None})


# --------------------------------------------------------------------------- #
# 历史查询
# --------------------------------------------------------------------------- #
@router.get("/history")
async def engine_history(
    engine: str = Query(default="cycle", description="引擎: cycle/valuation/risk/regime"),
    start: datetime | None = Query(default=None, description="起始时间（ISO8601，默认30天前）"),
    end: datetime | None = Query(default=None, description="结束时间（ISO8601，默认现在）"),
    limit: int = Query(default=1000, ge=1, le=5000, description="返回条数上限"),
):
    """查询引擎历史落库状态序列（按观测时间升序）。

    直接读取引擎对应 ORM 表（cycle_states / valuation_states /
    risk_scores / market_regimes），不触发实时计算。
    """
    engine_name = engine.lower()
    try:
        eng = _get_engine(engine_name)
    except KeyError:
        return err(
            "INVALID_ENGINE",
            f"未知引擎: {engine}（可用: {sorted(_ENGINE_CLASSES)}）",
            status=422,
        )

    end_dt = end or datetime.now(UTC)
    start_dt = start or (end_dt - timedelta(days=30))

    try:
        model = eng._orm_model  # noqa: SLF001 - 引擎契约内的 ORM 映射
        time_col = getattr(model, eng._time_column)
        async with get_db_session_ctx() as session:
            rows = (
                (
                    await session.execute(
                        select(model)
                        .where(time_col >= start_dt, time_col <= end_dt)
                        .order_by(time_col)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )

        outputs: list[dict[str, Any]] = []
        for row in rows:
            try:
                outputs.append(engine_output_to_dict(eng._row_to_output(row)))
            except Exception:  # noqa: BLE001 - 单行反序列化失败跳过
                continue

        return ok(
            {
                "engine": engine_name,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "count": len(outputs),
                "outputs": outputs,
            }
        )
    except Exception as e:  # noqa: BLE001
        return err("DB_ERROR", f"查询 {engine_name} 引擎历史失败: {e}", status=503)


__all__ = ["engine_output_to_dict", "router"]
