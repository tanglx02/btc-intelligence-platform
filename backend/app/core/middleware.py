"""通用 HTTP 中间件。

- :class:`RequestIDMiddleware` — 为每个请求生成 X-Request-ID 并记录访问日志
- :class:`RateLimitMiddleware` — 基于 Redis 固定窗口的简单限流（每 IP 每分钟 N 次），
  Redis 不可用时自动降级为进程内计数器
"""

import time
import uuid
from collections import defaultdict
from typing import Any

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response


class RequestIDMiddleware(BaseHTTPMiddleware):
    """请求 ID + 访问日志中间件。

    - 生成 8 位短 request_id，写入 ``request.state.request_id`` 与响应头 ``X-Request-ID``
    - 记录 ``method path -> status (耗时ms)`` 访问日志
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex[:8]
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                f"[{request_id}] {request.method} {request.url.path} -> 500 ({duration_ms:.1f}ms)"
            )
            raise

        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info(
            f"[{request_id}] {request.method} {request.url.path} -> "
            f"{response.status_code} ({duration_ms:.1f}ms)"
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """简单固定窗口限流（每客户端 IP 每分钟最多 N 次请求）。

    - 优先使用 Redis（``SETEX`` 固定窗口计数），跨进程生效
    - Redis 不可用时降级为进程内字典计数（单进程开发环境够用）
    - 健康检查 / 文档路径豁免；WebSocket 升级请求不经过本中间件
    """

    #: 默认豁免路径
    DEFAULT_EXEMPT_PATHS = ("/health", "/docs", "/redoc", "/openapi.json")

    def __init__(
        self,
        app: Any,
        *,
        requests_per_minute: int = 120,
        exempt_paths: tuple[str, ...] = DEFAULT_EXEMPT_PATHS,
    ) -> None:
        super().__init__(app)
        self.limit = max(1, int(requests_per_minute))
        self.exempt_paths = exempt_paths
        # 进程内降级计数器 {key: count}
        self._local: dict[str, int] = defaultdict(int)
        self._local_cleanup_at = 0.0
        # 惰性创建的 Redis 客户端
        self._redis: Any = None
        self._redis_failed = False

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in self.exempt_paths or request.method == "OPTIONS":
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time() // 60)
        key = f"ratelimit:{client_ip}:{window}"

        count = await self._increment(key)
        if count > self.limit:
            return JSONResponse(
                status_code=429,
                content={
                    "success": False,
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": f"Too many requests (limit: {self.limit}/min)",
                    },
                },
                headers={"Retry-After": "60"},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(self.limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, self.limit - count))
        return response

    async def _increment(self, key: str) -> int:
        """对当前窗口计数 +1，返回累计次数。Redis 失败自动降级进程内计数。"""
        redis = await self._get_redis()
        if redis is not None:
            try:
                pipe = redis.pipeline()
                pipe.incr(key)
                pipe.expire(key, 65)
                count = (await pipe.execute())[0]
                return int(count)
            except Exception as e:  # noqa: BLE001 - 限流器故障不应阻断请求
                logger.warning(f"Rate limit Redis error, fallback to in-memory: {e}")
                self._redis = None
                self._redis_failed = True

        # 进程内降级
        self._local[key] += 1
        self._local_cleanup()
        return self._local[key]

    def _local_cleanup(self) -> None:
        """进程内计数器定期清理（每 5 分钟，只保留当前窗口）。"""
        now = time.time()
        if now - self._local_cleanup_at < 300:
            return
        self._local_cleanup_at = now
        current_window = int(now // 60)
        stale = [k for k in self._local if not k.endswith(f":{current_window}")]
        for k in stale:
            self._local.pop(k, None)

    async def _get_redis(self) -> Any | None:
        """惰性获取 Redis 客户端；不可用时返回 None（只警告一次）。"""
        if self._redis is not None:
            return self._redis
        if self._redis_failed:
            return None
        try:
            from redis.asyncio import Redis

            from app.core.config import settings

            self._redis = Redis.from_url(
                settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.0,
                socket_timeout=1.0,
            )
            await self._redis.ping()
            return self._redis
        except Exception as e:  # noqa: BLE001 - Redis 不可用降级进程内限流
            logger.info(f"Rate limiter: Redis unavailable, using in-memory fallback ({e})")
            self._redis = None
            self._redis_failed = True
            return None


__all__ = ["RateLimitMiddleware", "RequestIDMiddleware"]
