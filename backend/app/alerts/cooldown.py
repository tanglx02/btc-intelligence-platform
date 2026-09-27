"""冷却与速率限制管理。

触发抑制的三道闸门（按序检查）：
1. 规则处于「已触发未恢复」状态 -> 抑制（等待条件恢复，RECOVERY_PENDING）；
2. 冷却窗口内（last_triggered_at + cooldown_seconds > now）-> COOLDOWN 抑制；
3. 用户级每小时触发条数超限（Redis 计数器）-> RATE_LIMIT 抑制。

Redis 不可用时优雅降级到进程内 dict。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from uuid import UUID

from loguru import logger

#: 用户级每小时最多触发条数
USER_HOURLY_LIMIT = 20

#: 速率限制 Redis key 模板（按小时分桶）
_RATE_KEY = "alert:rate:{user_id}:{window}"


def _as_utc(dt: datetime) -> datetime:
    """naive datetime 视为 UTC 并附加时区。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class RuleTriggerState(str, Enum):
    """规则触发运行时状态（非 DB 字段，由本管理器维护）。"""

    NORMAL = "NORMAL"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "COOLDOWN"
    RECOVERED = "RECOVERED"


class CooldownManager:
    """规则触发冷却管理（恢复检查 + 冷却窗口 + 用户级速率限制）。"""

    def __init__(self, redis_client: Any = None, hourly_limit: int = USER_HOURLY_LIMIT) -> None:
        self._redis = redis_client
        self._hourly_limit = hourly_limit
        # 内存降级 / 补充状态
        self._triggered_at: dict[str, datetime] = {}
        self._rate_memory: dict[str, int] = {}
        self._rate_window: dict[str, str] = {}

    # ---- 恢复 + 冷却检查 ----

    async def can_trigger(self, rule: Any) -> tuple[bool, str | None]:
        """返回 (是否可触发, 抑制原因)。

        1. 规则状态：TRIGGERED 且未恢复 -> 抑制（等恢复）；
        2. 冷却：last_triggered_at + cooldown_seconds > now -> COOLDOWN 抑制。
        """
        rule_id = str(getattr(rule, "id", ""))

        # 1. 已触发未恢复（内存态或 DB 计数器任一命中）
        if self._triggered_at.get(rule_id) is not None:
            return False, "RECOVERY_PENDING"
        if int(getattr(rule, "consecutive_triggers", 0) or 0) > 0:
            return False, "RECOVERY_PENDING"

        # 2. 冷却窗口（DB 字段优先，内存补充）
        last_triggered = getattr(rule, "last_triggered_at", None)
        if last_triggered is None:
            last_triggered = self._triggered_at.get(rule_id)
        if last_triggered is not None:
            cooldown_seconds = int(getattr(rule, "cooldown_seconds", 0) or 0)
            cooldown_end = _as_utc(last_triggered) + timedelta(seconds=cooldown_seconds)
            if datetime.now(UTC) < cooldown_end:
                return False, "COOLDOWN"

        return True, None

    # ---- 状态标记 ----

    async def mark_triggered(self, rule_id: UUID | str, at: datetime) -> None:
        """标记规则已触发（进入等恢复状态）。"""
        self._triggered_at[str(rule_id)] = _as_utc(at)

    async def mark_recovered(self, rule_id: UUID | str, at: datetime) -> None:
        """标记规则已恢复（下次条件满足可再次触发，冷却窗口仍然生效）。"""
        self._triggered_at.pop(str(rule_id), None)

    def get_trigger_state(self, rule: Any) -> RuleTriggerState:
        """推断规则当前运行时状态（诊断/展示用途）。"""
        rule_id = str(getattr(rule, "id", ""))
        last_triggered = getattr(rule, "last_triggered_at", None) or self._triggered_at.get(rule_id)
        if self._triggered_at.get(rule_id) is not None or (
            int(getattr(rule, "consecutive_triggers", 0) or 0) > 0
        ):
            return RuleTriggerState.TRIGGERED
        if last_triggered is not None:
            cooldown_end = _as_utc(last_triggered) + timedelta(
                seconds=int(getattr(rule, "cooldown_seconds", 0) or 0)
            )
            if datetime.now(UTC) < cooldown_end:
                return RuleTriggerState.COOLDOWN
        return RuleTriggerState.NORMAL

    # ---- 用户级速率限制 ----

    def _effective_hourly_limit(self) -> int:
        """每小时限额：SettingsService(alert.max_per_hour) > 构造值（热生效）。"""
        try:
            from app.services.settings_service import get_settings_service

            value = get_settings_service().get_sync("alert.max_per_hour", None)
            if value is not None and int(value) > 0:
                return int(value)
        except Exception:  # noqa: BLE001 - 设置层不可用时用构造值
            pass
        return self._hourly_limit

    async def check_rate_limit(self, user_id: UUID | str | None) -> bool:
        """用户级每小时最多 N 条（Redis 计数器，INCR + 1h TTL）。

        user_id 为 None（系统级规则）不限流。
        """
        if user_id is None:
            return True

        limit = self._effective_hourly_limit()
        window = datetime.now(UTC).strftime("%Y%m%d%H")
        key = _RATE_KEY.format(user_id=user_id, window=window)

        if self._redis is not None:
            try:
                current = await self._redis.incr(key)
                await self._redis.expire(key, 3600)
                return int(current) <= limit
            except Exception as e:  # noqa: BLE001 - Redis 故障降级
                logger.warning(f"CooldownManager: Redis 限流计数失败，降级内存: {e}")
                self._redis = None

        # 内存降级（进程内窗口计数）
        if self._rate_window.get(key) != window:
            self._rate_window[key] = window
            self._rate_memory[key] = 0
        self._rate_memory[key] = self._rate_memory.get(key, 0) + 1
        return self._rate_memory[key] <= limit
