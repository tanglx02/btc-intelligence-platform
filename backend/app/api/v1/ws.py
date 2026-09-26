"""WebSocket 实时推送路由（/api/v1/ws/market）。

协议（JSON 文本帧）::

    客户端 -> 服务端:
        {"action": "subscribe",   "channels": ["price", "provider_status"]}
        {"action": "unsubscribe", "channels": ["price"]}
        {"action": "ping"}

    服务端 -> 客户端:
        {"channel": "price", "data": {...}, "timestamp": "..."}   # 每 5 秒
        {"channel": "provider_status", "data": {...}, "timestamp": "..."}  # 每 30 秒
        {"channel": "pong", "data": {...}}                        # 心跳应答
        {"channel": "system", "data": {"message": "connected", ...}}  # 连接成功

MVP 实现：全局单广播循环（首次连接时启动），每 5 秒拉取一次 BTC 现货价格
并推送给 ``price`` 频道订阅者；数据获取失败时静默跳过，不影响连接。
"""

import asyncio
import contextlib
import json
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter(tags=["WebSocket"])

#: 支持的频道
CHANNELS = ("price", "provider_status")
#: 价格推送间隔（秒）
PRICE_INTERVAL_SECONDS = 5
#: Provider 状态推送间隔（秒，需为价格间隔的整数倍）
PROVIDER_STATUS_INTERVAL_SECONDS = 30


def _utcnow_iso() -> str:
    """当前 UTC 时间 ISO 字符串。"""
    return datetime.now(UTC).isoformat()


# --------------------------------------------------------------------------- #
# 连接管理器
# --------------------------------------------------------------------------- #
class ConnectionManager:
    """按频道管理的 WebSocket 连接池。"""

    def __init__(self) -> None:
        self._channels: dict[str, set[WebSocket]] = {ch: set() for ch in CHANNELS}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        """当前活跃连接数（去重）。"""
        return len({ws for conns in self._channels.values() for ws in conns})

    def channel_stats(self) -> dict[str, int]:
        """各频道订阅数。"""
        return {ch: len(conns) for ch, conns in self._channels.items()}

    async def subscribe(self, websocket: WebSocket, channel: str) -> None:
        """将连接加入频道（未知频道忽略）。"""
        if channel not in self._channels:
            return
        async with self._lock:
            self._channels[channel].add(websocket)

    async def unsubscribe(self, websocket: WebSocket, channel: str) -> None:
        """将连接移出频道。"""
        if channel not in self._channels:
            return
        async with self._lock:
            self._channels[channel].discard(websocket)

    async def unsubscribe_all(self, websocket: WebSocket) -> None:
        """连接断开时清理全部订阅。"""
        async with self._lock:
            for conns in self._channels.values():
                conns.discard(websocket)

    async def send_personal(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        """向单个连接发送 JSON（失败时抛异常，由调用方处理）。"""
        await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

    async def broadcast(self, channel: str, data: Any) -> None:
        """向频道内全部订阅者广播（发送失败的连接被移除）。"""
        payload = json.dumps(
            {"channel": channel, "data": data, "timestamp": _utcnow_iso()},
            ensure_ascii=False,
            default=str,
        )
        async with self._lock:
            conns = list(self._channels.get(channel, ()))
        if not conns:
            return

        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001 - 发送失败视为断开
                dead.append(ws)
        for ws in dead:
            await self.unsubscribe_all(ws)


manager = ConnectionManager()


# --------------------------------------------------------------------------- #
# 数据获取（广播内容）
# --------------------------------------------------------------------------- #
def _normalize(data: Any) -> Any:
    """dataclass / 其他对象 -> JSON 友好结构。"""
    if data is None or isinstance(data, (bool, int, float, str)):
        return data
    if is_dataclass(data) and not isinstance(data, dict):
        return asdict(data)
    if isinstance(data, dict):
        return {str(k): _normalize(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_normalize(v) for v in data]
    return str(data)


async def _fetch_price_payload() -> dict[str, Any] | None:
    """获取 BTC 现货价格（MarketService 优先，降级 ProviderService）。"""
    # 1) MarketService（带交叉验证与缓存）
    try:
        from app.services.market_service import MarketService
        from app.services.provider_service import get_provider_service

        service = MarketService(get_provider_service().manager)
        result = await service.get_current_price("BTCUSDT", cross_validate=False)
        if result.success:
            return _normalize(result.data)
    except Exception as e:  # noqa: BLE001 - 降级到 ProviderService
        logger.debug(f"WS price via MarketService failed: {e}")

    # 2) ProviderService 直取
    try:
        from app.services.provider_service import get_provider_service

        result = await get_provider_service().get_current_price("BTC/USDT")
        if result.success:
            return _normalize(result.data)
    except Exception as e:  # noqa: BLE001 - 全部失败时静默
        logger.debug(f"WS price via ProviderService failed: {e}")
    return None


async def _fetch_provider_status_payload() -> dict[str, Any] | None:
    """获取 Provider 状态摘要（数量 + 按状态分布 + 明细）。"""
    try:
        from app.services.provider_service import get_provider_service

        entries = get_provider_service().list_providers()
        by_status: dict[str, int] = {}
        for e in entries:
            status = str(e.get("status") or "UNKNOWN")
            by_status[status] = by_status.get(status, 0) + 1
        return {"total": len(entries), "by_status": by_status, "items": entries}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"WS provider status failed: {e}")
        return None


# --------------------------------------------------------------------------- #
# 广播循环
# --------------------------------------------------------------------------- #
_broadcaster_task: asyncio.Task | None = None


async def _broadcast_loop() -> None:
    """全局广播循环：价格每 5s，Provider 状态每 30s。"""
    logger.info("WS broadcaster loop started")
    tick = 0
    while True:
        try:
            tick += 1
            if manager.channel_stats().get("price"):
                price = await _fetch_price_payload()
                if price is not None:
                    await manager.broadcast("price", price)

            if (
                tick * PRICE_INTERVAL_SECONDS >= PROVIDER_STATUS_INTERVAL_SECONDS
                and manager.channel_stats().get("provider_status")
            ):
                status = await _fetch_provider_status_payload()
                if status is not None:
                    await manager.broadcast("provider_status", status)
                tick = 0
        except asyncio.CancelledError:
            logger.info("WS broadcaster loop cancelled")
            raise
        except Exception as e:  # noqa: BLE001 - 广播循环永不退出
            logger.warning(f"WS broadcast loop error: {e}")
        await asyncio.sleep(PRICE_INTERVAL_SECONDS)


def _ensure_broadcaster() -> None:
    """首次使用时启动全局广播循环（幂等）。"""
    global _broadcaster_task
    if _broadcaster_task is None or _broadcaster_task.done():
        _broadcaster_task = asyncio.create_task(_broadcast_loop())


# --------------------------------------------------------------------------- #
# WebSocket 端点
# --------------------------------------------------------------------------- #
@router.websocket("/ws/market")
async def ws_market(websocket: WebSocket):
    """实时行情推送连接（订阅 price / provider_status 频道，30s 心跳）。"""
    await websocket.accept()
    _ensure_broadcaster()

    try:
        await manager.send_personal(
            websocket,
            {
                "channel": "system",
                "data": {
                    "message": "connected",
                    "channels": list(CHANNELS),
                    "protocol": {
                        "subscribe": '{"action": "subscribe", "channels": ["price"]}',
                        "ping": '{"action": "ping"}',
                    },
                    "server_time": _utcnow_iso(),
                },
            },
        )

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await manager.send_personal(
                    websocket,
                    {"channel": "error", "data": {"message": "invalid JSON"}},
                )
                continue

            action = str(msg.get("action", "")).lower()
            channels = msg.get("channels") or []

            if action == "ping":
                await manager.send_personal(
                    websocket, {"channel": "pong", "data": {"server_time": _utcnow_iso()}}
                )
            elif action == "subscribe":
                for ch in channels:
                    await manager.subscribe(websocket, str(ch))
                await manager.send_personal(
                    websocket,
                    {
                        "channel": "system",
                        "data": {
                            "subscribed": channels,
                            "stats": manager.channel_stats(),
                        },
                    },
                )
            elif action == "unsubscribe":
                for ch in channels:
                    await manager.unsubscribe(websocket, str(ch))
                await manager.send_personal(
                    websocket,
                    {"channel": "system", "data": {"unsubscribed": channels}},
                )
            else:
                await manager.send_personal(
                    websocket,
                    {"channel": "error", "data": {"message": f"unknown action: {action}"}},
                )
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001 - 其余异常记录后清理
        logger.debug(f"WS connection error: {e}")
    finally:
        await manager.unsubscribe_all(websocket)


async def shutdown_broadcaster() -> None:
    """应用关闭时取消广播循环（供 lifespan 调用）。"""
    global _broadcaster_task
    if _broadcaster_task is not None and not _broadcaster_task.done():
        _broadcaster_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _broadcaster_task
    _broadcaster_task = None


__all__ = ["ConnectionManager", "manager", "router", "shutdown_broadcaster"]
