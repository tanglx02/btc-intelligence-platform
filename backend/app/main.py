"""BTC 全市场智能研究平台 - Backend API 入口。

职责：
1. 应用生命周期编排（ProviderService / Scheduler / AlertEngine 的启停）
2. 中间件装配（RequestID 访问日志、Redis 限流、CORS）
3. 全局异常兜底（统一错误信封，绝不向客户端泄漏堆栈）
4. 挂载 /api/v1 业务路由与 /health 探活端点

启动::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

import inspect
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.middleware import RateLimitMiddleware, RequestIDMiddleware


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #
async def _start_optional_module(
    *,
    import_path: str,
    factory: str,
    start_method: str = "start",
    label: str,
) -> None:
    """尝试启动可选模块（scheduler / alerts），缺失或失败均不阻断主流程。"""
    try:
        module = __import__(import_path, fromlist=[factory])
        instance = getattr(module, factory)()
        if instance is None:
            logger.info(f"{label} not initialized yet")
            return
        started = getattr(instance, start_method)()
        if inspect.isawaitable(started):
            await started
        logger.info(f"{label} started")
    except ImportError:
        logger.info(f"{label} module not available yet")
    except Exception as e:  # noqa: BLE001 - 可选模块启动失败不阻断应用
        logger.error(f"{label} startup failed: {e}")


async def _stop_optional_module(
    *,
    import_path: str,
    factory: str,
    stop_method: str = "stop",
    label: str,
) -> None:
    """尝试停止可选模块（shutdown 阶段，反序执行，全部容错）。"""
    try:
        module = __import__(import_path, fromlist=[factory])
        instance = getattr(module, factory)()
        if instance is None:
            return
        stopped = getattr(instance, stop_method)()
        if inspect.isawaitable(stopped):
            await stopped
        logger.info(f"{label} stopped")
    except Exception as e:  # noqa: BLE001 - 关闭阶段任何异常都不应阻断
        logger.warning(f"{label} stop failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：startup 启动数据/调度/告警子系统，shutdown 反序释放。"""
    # ---- startup ----
    setup_logging()
    logger.info("Starting BTC Intelligence Platform...")

    # 1. ProviderService（Redis 连接、Provider 注册与健康监控）
    try:
        from app.services.provider_service import get_provider_service

        await get_provider_service().startup()
        logger.info("ProviderService started")
    except Exception as e:  # noqa: BLE001
        logger.error(f"ProviderService startup failed: {e}")

    # 2. Scheduler（可选模块；SCHEDULER_ENABLED=false 时跳过 —— 生产环境中
    #    由独立 scheduler 容器专职运行，避免多 worker 重复执行任务）
    if settings.scheduler_enabled:
        await _start_optional_module(
            import_path="app.scheduler",
            factory="get_scheduler",
            label="Scheduler",
        )
    else:
        logger.info("Scheduler disabled by config (SCHEDULER_ENABLED=false)")

    # 3. AlertEngine（可选模块；ALERT_ENABLED=false 时跳过 —— 生产环境中由
    #    scheduler 容器内运行，避免多 worker 双发告警）
    if settings.alert_enabled:
        await _start_optional_module(
            import_path="app.alerts",
            factory="get_alert_engine",
            label="Alert engine",
        )
    else:
        logger.info("Alert engine disabled by config (ALERT_ENABLED=false)")

    yield

    # ---- shutdown（反序）----
    await _stop_optional_module(import_path="app.alerts", factory="get_alert_engine", label="Alert engine")
    await _stop_optional_module(import_path="app.scheduler", factory="get_scheduler", label="Scheduler")

    try:
        from app.api.v1.ws import shutdown_broadcaster

        await shutdown_broadcaster()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"WS broadcaster stop failed: {e}")

    try:
        from app.services.provider_service import get_provider_service

        await get_provider_service().shutdown()
        logger.info("ProviderService stopped")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"ProviderService stop failed: {e}")

    try:
        from app.core.database import close_engine

        await close_engine()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"DB engine close failed: {e}")

    logger.info("Platform stopped")


# --------------------------------------------------------------------------- #
# 应用实例
# --------------------------------------------------------------------------- #
app = FastAPI(
    title="BTC Intelligence Platform",
    description=(
        "BTC 全市场智能研究、历史数据、周期分析、个人资金计划、"
        "策略回测与风险监测平台"
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# ---- CORS（config.cors_origins 为逗号分隔字符串；未配置时使用开发默认值）----
_cors_raw = getattr(settings, "cors_origins", None) or "http://localhost:3000,http://localhost:8080"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_raw.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- 中间件（add 顺序与执行顺序相反：RequestID 最外层，429 也会被记录）----
app.add_middleware(RateLimitMiddleware, requests_per_minute=120)
app.add_middleware(RequestIDMiddleware)


# --------------------------------------------------------------------------- #
# 全局异常兜底
# --------------------------------------------------------------------------- #
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """未捕获异常兜底：记录完整堆栈，返回统一 500 信封（不泄漏内部信息）。"""
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "data": None,
            "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"},
        },
    )


# --------------------------------------------------------------------------- #
# 探活端点（供容器编排 / 负载均衡使用，不走业务路由前缀）
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["System"])
async def health_check():
    """基础健康检查（进程级，不检查外部依赖）。"""
    return {"status": "ok", "service": "btc-platform-api", "version": "0.1.0"}


# --------------------------------------------------------------------------- #
# 业务路由
# --------------------------------------------------------------------------- #
from app.api.v1 import api_router  # noqa: E402

app.include_router(api_router, prefix=settings.api_v1_prefix)
