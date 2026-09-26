"""API v1 路由汇总。

核心路由（market/system/engines/indicators/ws）由本任务维护，直接导入；
其余业务路由（onchain/etf/derivatives/...）由并行任务创建，
通过 :func:`_try_include` 容错导入 —— 模块尚未就绪时跳过，不阻塞启动。
"""

import importlib

from fastapi import APIRouter
from loguru import logger

api_router = APIRouter()

#: 本任务创建的核心路由（缺失视为错误，直接导入失败即抛出）
_CORE_ROUTES = ("market", "system", "engines", "indicators")

#: 并行任务创建的路由（缺失时静默跳过）
_PARALLEL_ROUTES = (
    "onchain",
    "etf",
    "derivatives",
    "options",
    "macro",
    "sentiment",
    "portfolio",
    "backtest",
    "providers",
    "alerts",
)


def _try_include(module_name: str, *, required: bool = False) -> bool:
    """尝试导入 ``app.api.v1.<module_name>`` 并挂载其 ``router``。

    Args:
        module_name: 路由模块名
        required: True 时导入失败抛出异常（核心路由），False 时静默跳过

    Returns:
        是否成功挂载
    """
    try:
        module = importlib.import_module(f"app.api.v1.{module_name}")
        router_obj = getattr(module, "router", None)
        if router_obj is None:
            raise AttributeError(f"module 'app.api.v1.{module_name}' has no attribute 'router'")
        api_router.include_router(router_obj)
        return True
    except ImportError as e:
        if required:
            raise
        logger.info(f"Route module 'app.api.v1.{module_name}' not available yet, skipped ({e})")
        return False
    except Exception as e:  # noqa: BLE001 - 路由模块损坏时不应拖垮整个应用
        if required:
            raise
        logger.warning(f"Failed to include route 'app.api.v1.{module_name}': {e}")
        return False


# ---- 核心路由（本任务维护）----
for _core in _CORE_ROUTES:
    _try_include(_core, required=True)

# ---- 并行任务路由（容错导入）----
for _name in _PARALLEL_ROUTES:
    _try_include(_name)

# ---- WebSocket ----
from .ws import router as ws_router  # noqa: E402

api_router.include_router(ws_router)

__all__ = ["api_router"]
