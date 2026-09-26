"""请求限流器实现。

提供两种限流算法：
- TokenBucketRateLimiter: 令牌桶算法，允许突发流量，平滑长期速率
- SlidingWindowRateLimiter: 滑动窗口算法，精确限制固定时间窗口内的请求数

每个 Provider 持有独立的限流器实例。
"""

import asyncio
import time

from loguru import logger


class TokenBucketRateLimiter:
    """令牌桶限流器。

    特点：
    - 允许突发流量（桶满时可一次性消耗多个令牌）
    - 长期请求速率平滑（令牌以固定速率补充）
    - 异步友好（使用 asyncio.Lock 保护状态）

    Attributes:
        rate: 窗口内允许的最大请求数（即桶容量）
        window: 时间窗口（秒）
    """

    def __init__(self, rate: int, window: int, burst: int | None = None):
        """初始化令牌桶限流器。

        Args:
            rate: 每个窗口允许的最大请求数
            window: 时间窗口（秒）
            burst: 突发容量（默认等于 rate）
        """
        self.rate = rate
        self.window = window
        self.burst = burst or rate
        self.tokens = float(self.burst)
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()
        self._total_waited: float = 0.0
        self._total_requests: int = 0

    async def acquire(self, tokens: float = 1.0) -> float:
        """获取令牌。如果令牌不足则等待。

        Args:
            tokens: 需要消耗的令牌数量（默认 1）

        Returns:
            实际等待的时间（秒），0 表示无需等待
        """
        async with self._lock:
            self._refill()
            self._total_requests += 1

            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0

            # 计算需要等待的时间
            deficit = tokens - self.tokens
            refill_rate = self.rate / self.window  # 令牌/秒
            wait_time = deficit / refill_rate

            logger.debug(
                f"RateLimiter: waiting {wait_time:.2f}s "
                f"(tokens={self.tokens:.1f}, need={tokens})"
            )

        # 在锁外等待（允许其他协程检查状态）
        await asyncio.sleep(wait_time)

        async with self._lock:
            self._refill()
            self.tokens = max(0.0, self.tokens - tokens)
            self._total_waited += wait_time

        return wait_time

    def _refill(self) -> None:
        """根据流逝时间补充令牌。"""
        now = time.monotonic()
        elapsed = now - self.last_refill
        refill_amount = elapsed * (self.rate / self.window)
        self.tokens = min(float(self.burst), self.tokens + refill_amount)
        self.last_refill = now

    @property
    def available_tokens(self) -> float:
        """当前可用令牌数（近似值，非线程安全读取）。"""
        self._refill()
        return self.tokens

    @property
    def stats(self) -> dict:
        """限流器统计信息。"""
        return {
            "total_requests": self._total_requests,
            "total_waited_seconds": round(self._total_waited, 2),
            "available_tokens": round(self.tokens, 1),
            "rate": self.rate,
            "window": self.window,
        }

    def adjust_rate(self, new_rate: int) -> None:
        """动态调整速率（用于 429 响应后自动降速）。

        Args:
            new_rate: 新的请求速率上限
        """
        old_rate = self.rate
        self.rate = new_rate
        # 调整令牌上限
        self.burst = min(self.burst, new_rate)
        logger.info(f"RateLimiter: rate adjusted {old_rate} -> {new_rate}")

    def backoff(self, factor: float = 0.5) -> None:
        """降速 — 将速率乘以系数（用于触发 429 后）。

        Args:
            factor: 降速系数（0-1），默认减半
        """
        new_rate = max(1, int(self.rate * factor))
        self.adjust_rate(new_rate)

    def restore(self, original_rate: int) -> None:
        """恢复原始速率（限流解除后调用）。

        Args:
            original_rate: 原始配置的速率
        """
        self.adjust_rate(original_rate)


class SlidingWindowRateLimiter:
    """滑动窗口限流器。

    精确限制固定时间窗口内的请求数量，适用于严格限流的 API
    （如 Glassnode 免费版 30 请求/分钟）。

    与令牌桶的区别：
    - 令牌桶允许突发（桶满时）
    - 滑动窗口严格限制任何连续 N 秒内的请求数
    """

    def __init__(self, max_requests: int, window_seconds: int):
        """初始化滑动窗口限流器。

        Args:
            max_requests: 窗口内最大请求数
            window_seconds: 窗口时长（秒）
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: list[float] = []
        self._lock = asyncio.Lock()
        self._total_waited: float = 0.0
        self._total_requests: int = 0

    async def acquire(self) -> float:
        """等待直到可以发送请求。

        Returns:
            实际等待的时间（秒），0 表示无需等待
        """
        wait_time = 0.0

        async with self._lock:
            self._total_requests += 1
            now = time.monotonic()
            self._cleanup(now)

            if len(self._requests) >= self.max_requests:
                # 计算需要等待的时间：最早的请求过期
                oldest = self._requests[0]
                wait_time = self.window_seconds - (now - oldest)
                if wait_time > 0:
                    logger.debug(
                        f"SlidingWindow: waiting {wait_time:.2f}s "
                        f"({len(self._requests)}/{self.max_requests} used)"
                    )

        if wait_time > 0:
            await asyncio.sleep(wait_time)
            self._total_waited += wait_time

        async with self._lock:
            now = time.monotonic()
            self._cleanup(now)
            self._requests.append(now)

        return wait_time

    def _cleanup(self, now: float) -> None:
        """清理窗口外的过期请求记录。"""
        cutoff = now - self.window_seconds
        self._requests = [t for t in self._requests if t > cutoff]

    @property
    def current_usage(self) -> int:
        """当前窗口内的请求数（近似值）。"""
        now = time.monotonic()
        cutoff = now - self.window_seconds
        return len([t for t in self._requests if t > cutoff])

    @property
    def stats(self) -> dict:
        """限流器统计信息。"""
        return {
            "total_requests": self._total_requests,
            "total_waited_seconds": round(self._total_waited, 2),
            "current_usage": self.current_usage,
            "max_requests": self.max_requests,
            "window_seconds": self.window_seconds,
        }

    def adjust_limit(self, new_max: int) -> None:
        """动态调整限制（用于 429 响应后降速）。"""
        old_max = self.max_requests
        self.max_requests = new_max
        logger.info(f"SlidingWindow: limit adjusted {old_max} -> {new_max}")


class RateLimiterFactory:
    """限流器工厂 — 根据 Provider 配置创建合适的限流器实例。"""

    @staticmethod
    def create(
        rate: int,
        window: int,
        algorithm: str = "token_bucket",
        burst: int | None = None,
    ) -> TokenBucketRateLimiter | SlidingWindowRateLimiter:
        """创建限流器实例。

        Args:
            rate: 每窗口最大请求数
            window: 窗口秒数
            algorithm: 算法类型 ("token_bucket" 或 "sliding_window")
            burst: 突发容量（仅 token_bucket 有效）

        Returns:
            限流器实例
        """
        if algorithm == "sliding_window":
            return SlidingWindowRateLimiter(max_requests=rate, window_seconds=window)
        return TokenBucketRateLimiter(rate=rate, window=window, burst=burst)


__all__ = [
    "RateLimiterFactory",
    "SlidingWindowRateLimiter",
    "TokenBucketRateLimiter",
]
