"""统一 API 响应模型与构造辅助。

所有端点返回统一信封结构::

    成功: {"success": true, "data": ..., "meta": {...}}
    失败: {"success": false, "error": {"code": "...", "message": "..."}}

``ok`` / ``err`` 为路由层快捷构造函数，保证全站响应结构一致。
"""

from datetime import UTC, datetime
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class Meta(BaseModel):
    """响应元信息（数据来源、质量状态、缓存命中）。"""

    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    source: str | None = None
    quality_status: str | None = None
    cache_hit: bool = False


class ApiError(BaseModel):
    """错误信息体。"""

    code: str
    message: str


class ApiResponse(BaseModel):
    """统一响应信封。"""

    success: bool = True
    data: Any = None
    meta: Meta | None = None
    error: ApiError | None = None


def quality_str(status: Any) -> str | None:
    """将枚举/字符串质量状态统一转为字符串（None 安全）。"""
    if status is None:
        return None
    return getattr(status, "value", None) or str(status)


def ok(
    data: Any = None,
    *,
    source: str | None = None,
    quality_status: Any = None,
    cache_hit: bool = False,
) -> dict[str, Any]:
    """构造成功响应（dict，由 FastAPI 序列化，自动处理 datetime 等）。"""
    return {
        "success": True,
        "data": data,
        "meta": {
            "timestamp": datetime.now(UTC).isoformat(),
            "source": source,
            "quality_status": quality_str(quality_status),
            "cache_hit": cache_hit,
        },
        "error": None,
    }


def err(code: str, message: str, status: int = 400) -> JSONResponse:
    """构造错误响应（JSONResponse，携带 HTTP 状态码）。"""
    return JSONResponse(
        status_code=status,
        content={"success": False, "data": None, "error": {"code": code, "message": message}},
    )


def service_unavailable(message: str = "Service not available") -> JSONResponse:
    """构造 503 服务不可用响应（Service 层调用失败时的统一出口）。"""
    return err("SERVICE_UNAVAILABLE", message, status=503)


__all__ = [
    "ApiError",
    "ApiResponse",
    "Meta",
    "err",
    "ok",
    "quality_str",
    "service_unavailable",
]
