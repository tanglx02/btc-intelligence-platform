"""持续时间与连续次数跟踪（Redis 持久化，不可用时降级内存 dict）。

Redis key 约定：
- ``alert:duration:{rule_id}``     — 条件开始满足的时间戳（TTL 1 天）
- ``alert:consecutive:{rule_id}``  — 连续满足计数（TTL 1 天）

设计语义：
- check_duration：条件开始满足时记录时间；满足持续超过 duration_seconds
  才返回 True；条件不满足立即重置；
- check_consecutive：连续 N 次求值满足才返回 True；任一次不满足重置计数。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from loguru import logger

#: Redis key 模板
_DURATION_KEY = "alert:duration:{rule_id}"
_CONSECUTIVE_KEY = "alert:consecutive:{rule_id}"

#: 状态 TTL（秒，1 天）
_STATE_TTL = 86400


class DurationTracker:
    """跟踪条件持续满足时间 / 连续满足次数。

    优先使用 Redis（异步客户端，多进程共享）；Redis 不可用时自动降级
    到进程内 dict（单进程语义，重启后状态丢失但不影响正确性）。
    """

    def __init__(self, redis_client: Any = None) -> None:
        self._redis = redis_client
        # 内存降级存储：key -> (value, expires_at)
        self._memory: dict[str, tuple[float, float]] = {}

    # ---- 对外接口 ----

    async def check_duration(
        self,
        rule_id: UUID | str,
        condition_met: bool,
        duration_seconds: int | None,
    ) -> bool:
        """条件开始满足时记录时间；满足超过 duration_seconds 才返回 True。

        Args:
            rule_id: 规则 ID
            condition_met: 本轮条件是否满足
            duration_seconds: 需持续满足的秒数（None/<=0 视为不启用）
        """
        if duration_seconds is None or duration_seconds <= 0:
            return condition_met

        key = _DURATION_KEY.format(rule_id=rule_id)
        now = time.time()

        if not condition_met:
            await self._delete(key)
            return False

        started = await self._get(key)
        if started is None:
            await self._set(key, now)
            logger.debug(f"DurationTracker: 规则 {rule_id} 条件开始满足，开始计时")
            return False
        return (now - started) >= duration_seconds

    async def check_consecutive(
        self,
        rule_id: UUID | str,
        condition_met: bool,
        count: int,
    ) -> bool:
        """连续 N 次满足才返回 True；不满足重置计数。

        Args:
            rule_id: 规则 ID
            condition_met: 本轮条件是否满足
            count: 需连续满足的次数（<=1 视为不启用）
        """
        if count <= 1:
            return condition_met

        key = _CONSECUTIVE_KEY.format(rule_id=rule_id)

        if not condition_met:
            await self._delete(key)
            return False

        current = await self._incr(key)
        return current >= count

    # ---- Redis / 内存统一存取（全部容错，失败降级内存）----

    async def _get(self, key: str) -> float | None:
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                return float(raw) if raw is not None else None
            except Exception as e:  # noqa: BLE001 - Redis 故障降级
                logger.warning(f"DurationTracker: Redis GET 失败，降级内存: {e}")
                self._redis = None
        return self._mem_get(key)

    async def _set(self, key: str, value: float) -> None:
        if self._redis is not None:
            try:
                await self._redis.set(key, value, ex=_STATE_TTL)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DurationTracker: Redis SET 失败，降级内存: {e}")
                self._redis = None
        self._mem_set(key, value)

    async def _delete(self, key: str) -> None:
        self._memory.pop(key, None)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DurationTracker: Redis DELETE 失败: {e}")
                self._redis = None

    async def _incr(self, key: str) -> int:
        if self._redis is not None:
            try:
                current = await self._redis.incr(key)
                await self._redis.expire(key, _STATE_TTL)
                return int(current)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"DurationTracker: Redis INCR 失败，降级内存: {e}")
                self._redis = None
        # 内存降级：递增后必须写回，否则计数永远停在同一值
        current = int(self._mem_get(key) or 0) + 1
        self._mem_set(key, float(current))
        return current

    # ---- 内存降级实现（带 TTL）----

    def _mem_get(self, key: str) -> float | None:
        item = self._memory.get(key)
        if item is None:
            return None
        value, expires_at = item
        if time.time() > expires_at:
            self._memory.pop(key, None)
            return None
        return value

    def _mem_set(self, key: str, value: float) -> None:
        self._memory[key] = (value, time.time() + _STATE_TTL)
