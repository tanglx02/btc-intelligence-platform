"""HTTP 网络客户端封装。

基于 httpx.AsyncClient，提供：
- 每个 Provider 独立的连接池、代理、超时配置
- 指数退避重试（带 jitter）
- 请求/响应日志记录
- 异常到 ErrorType 的统一分类映射
"""

import asyncio
import random
import socket
import time
from typing import Any

import httpx
from loguru import logger

from app.providers.base.config import ProviderConfig
from app.providers.base.types import ErrorType, FetchResult
from app.utils.datetime_utils import utcnow


class NetworkErrorClassifier:
    """网络错误分类器 — 将底层异常映射为统一的 ErrorType。"""

    @staticmethod
    def classify(error: Exception) -> ErrorType:
        """根据异常类型返回统一的 ErrorType。

        Args:
            error: 捕获的异常实例

        Returns:
            对应的 ErrorType 枚举值
        """
        if isinstance(error, httpx.ConnectTimeout):
            return ErrorType.TIMEOUT
        if isinstance(error, httpx.ReadTimeout):
            return ErrorType.TIMEOUT
        if isinstance(error, httpx.WriteTimeout):
            return ErrorType.TIMEOUT
        if isinstance(error, httpx.PoolTimeout):
            return ErrorType.TIMEOUT
        if isinstance(error, httpx.ConnectError):
            # 区分 DNS 错误和连接拒绝
            cause = error.__cause__
            if isinstance(cause, (socket.gaierror, OSError)):
                if isinstance(cause, socket.gaierror):
                    return ErrorType.DNS_ERROR
            return ErrorType.CONNECTION_REFUSED
        if isinstance(error, httpx.TooManyRedirects):
            return ErrorType.NETWORK_ERROR
        if isinstance(error, httpx.DecodingError):
            return ErrorType.DATA_FORMAT
        if isinstance(error, httpx.InvalidURL):
            return ErrorType.NETWORK_ERROR
        if isinstance(error, ssl_errors_tuple()):
            return ErrorType.TLS_ERROR
        if isinstance(error, socket.gaierror):
            return ErrorType.DNS_ERROR
        if isinstance(error, httpx.HTTPStatusError):
            status = error.response.status_code
            return _classify_http_status(status)
        if isinstance(error, httpx.TimeoutException):
            return ErrorType.TIMEOUT
        if isinstance(error, httpx.NetworkError):
            return ErrorType.NETWORK_ERROR
        return ErrorType.UNKNOWN

    @staticmethod
    def classify_status_code(status_code: int) -> ErrorType:
        """根据 HTTP 状态码分类错误类型。"""
        return _classify_http_status(status_code)


def ssl_errors_tuple() -> tuple:
    """返回 SSL 相关异常类型元组（延迟导入避免循环）。"""
    import ssl

    return (ssl.SSLError, ssl.SSLCertVerificationError)


def _classify_http_status(status_code: int) -> ErrorType:
    """根据 HTTP 状态码映射 ErrorType。"""
    if status_code == 401:
        return ErrorType.AUTH_ERROR
    if status_code == 403:
        return ErrorType.AUTH_ERROR
    if status_code == 429:
        return ErrorType.RATE_LIMIT
    if 500 <= status_code < 600:
        return ErrorType.SERVER_ERROR
    return ErrorType.NETWORK_ERROR


class HTTPClient:
    """异步 HTTP 客户端封装。

    为每个 Provider 提供独立的 httpx.AsyncClient 实例，
    内置重试、超时、代理、连接池管理等功能。

    Usage:
        client = HTTPClient(config)
        await client.start()
        result = await client.get("/api/v1/price", params={"symbol": "BTCUSDT"})
        await client.close()
    """

    def __init__(self, config: ProviderConfig):
        """初始化 HTTP 客户端。

        Args:
            config: Provider 配置实例
        """
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._started = False

    @property
    def is_started(self) -> bool:
        """客户端是否已启动。"""
        return self._started

    @property
    def config(self) -> ProviderConfig:
        """获取关联的 Provider 配置。"""
        return self._config

    async def start(self) -> None:
        """创建并启动 httpx AsyncClient。"""
        if self._started:
            return

        timeout = httpx.Timeout(
            connect=self._config.timeout.connect,
            read=self._config.timeout.read,
            write=self._config.timeout.write,
            pool=self._config.timeout.pool,
        )

        limits = httpx.Limits(
            max_connections=self._config.max_connections,
            max_keepalive_connections=self._config.max_connections // 2,
            keepalive_expiry=30.0 if self._config.keep_alive else 0.0,
        )

        headers = {
            "User-Agent": "BTC-Platform/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            **self._config.headers,
        }

        # 注入 API Key 到 Header（如果有且未在 headers 中手动指定）
        if self._config.api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        self._client = httpx.AsyncClient(
            base_url=self._config.base_url,
            timeout=timeout,
            limits=limits,
            proxy=self._config.proxy,
            headers=headers,
            verify=self._config.verify_ssl,
            follow_redirects=True,
        )

        self._started = True
        logger.debug(
            f"[{self._config.name}] HTTP client started | "
            f"base_url={self._config.base_url} | proxy={self._config.proxy or 'direct'}"
        )

    async def close(self) -> None:
        """关闭 HTTP 客户端并释放连接池。"""
        if self._client and self._started:
            await self._client.aclose()
            self._started = False
            logger.debug(f"[{self._config.name}] HTTP client closed")

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """发送 GET 请求（含重试逻辑）。

        Args:
            path: 请求路径（相对于 base_url）
            params: 查询参数
            headers: 额外请求头
            **kwargs: 传递给 httpx 的其他参数

        Returns:
            FetchResult 封装的响应结果
        """
        return await self._request("GET", path, params=params, headers=headers, **kwargs)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """发送 POST 请求（含重试逻辑）。"""
        return await self._request(
            "POST", path, json=json, data=data, headers=headers, **kwargs
        )

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> FetchResult:
        """底层请求方法，包含重试逻辑。

        实现指数退避重试策略：
        - 对可重试的状态码（429, 500, 502, 503, 504）进行重试
        - 对超时和连接错误进行重试
        - 对认证错误（401, 403）不重试
        - 每次重试间隔指数增长并加入随机 jitter

        Returns:
            FetchResult 包含成功数据或错误信息
        """
        if not self._client or not self._started:
            return FetchResult(
                success=False,
                error="HTTP client not started",
                error_type=ErrorType.NETWORK_ERROR,
                provider_name=self._config.name,
            )

        max_retries = self._config.retry.count
        backoff_factor = self._config.retry.backoff_factor
        max_delay = self._config.retry.max_delay
        use_jitter = self._config.retry.jitter

        last_error: str | None = None
        last_error_type: ErrorType | None = None
        last_status_code: int | None = None

        for attempt in range(max_retries + 1):
            start_time = time.monotonic()

            try:
                response = await self._client.request(
                    method=method,
                    url=path,
                    params=params,
                    headers=headers,
                    json=json,
                    data=data,
                    **kwargs,
                )

                elapsed_ms = (time.monotonic() - start_time) * 1000

                # 处理 HTTP 错误状态码
                if response.status_code >= 400:
                    error_type = NetworkErrorClassifier.classify_status_code(
                        response.status_code
                    )
                    last_status_code = response.status_code
                    last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                    last_error_type = error_type

                    # 判断是否可重试
                    if self._is_retryable_status(response.status_code) and attempt < max_retries:
                        delay = self._calculate_delay(
                            attempt, backoff_factor, max_delay, use_jitter
                        )
                        # 429 响应优先使用 Retry-After header
                        if response.status_code == 429:
                            retry_after = response.headers.get("Retry-After")
                            if retry_after:
                                try:
                                    delay = max(delay, float(retry_after))
                                except (ValueError, TypeError):
                                    pass

                        logger.warning(
                            f"[{self._config.name}] {method} {path} -> "
                            f"HTTP {response.status_code}, retry {attempt + 1}/{max_retries} "
                            f"in {delay:.1f}s"
                        )
                        await asyncio.sleep(delay)
                        continue

                    # 不可重试或已达最大重试次数
                    logger.error(
                        f"[{self._config.name}] {method} {path} -> "
                        f"HTTP {response.status_code} (final attempt)"
                    )
                    return FetchResult(
                        success=False,
                        error=last_error,
                        error_type=last_error_type,
                        status_code=response.status_code,
                        response_time_ms=elapsed_ms,
                        provider_name=self._config.name,
                        fetch_time=utcnow(),
                    )

                # 请求成功
                try:
                    response_data = response.json()
                except Exception:
                    response_data = response.text

                logger.debug(
                    f"[{self._config.name}] {method} {path} -> "
                    f"200 OK ({elapsed_ms:.0f}ms)"
                )

                return FetchResult(
                    success=True,
                    data=response_data,
                    status_code=response.status_code,
                    response_time_ms=elapsed_ms,
                    provider_name=self._config.name,
                    fetch_time=utcnow(),
                    raw_response=response_data,
                )

            except httpx.TimeoutException as e:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                last_error = f"Timeout: {type(e).__name__}"
                last_error_type = ErrorType.TIMEOUT

                if attempt < max_retries:
                    delay = self._calculate_delay(attempt, backoff_factor, max_delay, use_jitter)
                    logger.warning(
                        f"[{self._config.name}] {method} {path} -> Timeout, "
                        f"retry {attempt + 1}/{max_retries} in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

            except httpx.HTTPError as e:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                error_type = NetworkErrorClassifier.classify(e)
                last_error = f"{type(e).__name__}: {str(e)[:200]}"
                last_error_type = error_type

                # DNS 错误和 TLS 错误不重试
                if error_type in (ErrorType.DNS_ERROR, ErrorType.TLS_ERROR):
                    break

                if attempt < max_retries:
                    delay = self._calculate_delay(attempt, backoff_factor, max_delay, use_jitter)
                    logger.warning(
                        f"[{self._config.name}] {method} {path} -> "
                        f"{type(e).__name__}, retry {attempt + 1}/{max_retries} in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue

            except Exception as e:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                last_error = f"Unexpected: {type(e).__name__}: {str(e)[:200]}"
                last_error_type = ErrorType.UNKNOWN
                logger.exception(f"[{self._config.name}] {method} {path} -> Unexpected error")
                break

        # 所有重试耗尽
        return FetchResult(
            success=False,
            error=last_error,
            error_type=last_error_type,
            status_code=last_status_code,
            response_time_ms=0.0,
            provider_name=self._config.name,
            fetch_time=utcnow(),
        )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """判断 HTTP 状态码是否可重试。"""
        return status_code in {429, 500, 502, 503, 504}

    @staticmethod
    def _calculate_delay(
        attempt: int, backoff_factor: float, max_delay: float, jitter: bool
    ) -> float:
        """计算重试延迟（指数退避 + 可选 jitter）。

        公式：min(max_delay, backoff_factor * 2^attempt) + random_jitter
        """
        delay = min(max_delay, backoff_factor * (2**attempt))
        if jitter:
            delay += random.uniform(0, delay * 0.1)
        return delay


__all__ = ["HTTPClient", "NetworkErrorClassifier"]
