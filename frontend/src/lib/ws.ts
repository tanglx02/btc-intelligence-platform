/**
 * WebSocket 客户端（单例）。
 *
 * 协议对齐后端 /api/v1/ws/market：
 *   客户端 -> { "action": "subscribe",   "channels": ["price"] }
 *             { "action": "unsubscribe", "channels": ["price"] }
 *             { "action": "ping" }
 *   服务端 -> { "channel": "price", "data": {...}, "timestamp": "..." }
 *             { "channel": "pong",  "data": {...} }
 *
 * 特性：
 *   - 指数退避自动重连（1s/2s/4s...上限 30s）
 *   - 30s 心跳；60s 无任何消息判定死链主动重建
 *   - 页面不可见时暂停重连调度，恢复可见立即重连
 *   - 按频道订阅管理，首个订阅者触发 subscribe 帧
 */

import type { WsMessage } from "@/types/api";

const WS_URL =
  process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000/api/v1/ws/market";

const HEARTBEAT_INTERVAL_MS = 30_000;
/** 超过该时长未收到任何消息则判定连接已死 */
const DEAD_LINK_TIMEOUT_MS = 65_000;
const BASE_RECONNECT_DELAY_MS = 1_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

type WsDataHandler = (data: unknown, message: WsMessage) => void;
type StatusHandler = (connected: boolean) => void;

export class WSClient {
  private ws: WebSocket | null = null;
  private subscribers = new Map<string, Set<WsDataHandler>>();
  private statusListeners = new Set<StatusHandler>();

  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingReconnect = false;
  private userClosed = false;

  private heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  private lastActivityAt = 0;

  private visibilityBound = false;

  /** 当前是否处于已连接（OPEN）状态 */
  get connected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  /** 是否正在连接中 */
  get connecting(): boolean {
    return this.ws?.readyState === WebSocket.CONNECTING;
  }

  /** 建立连接（幂等；仅浏览器环境生效） */
  connect(): void {
    if (typeof window === "undefined") return;
    if (this.ws && (this.connected || this.connecting)) return;

    this.userClosed = false;
    this.bindVisibility();
    this.clearReconnectTimer();

    let ws: WebSocket;
    try {
      ws = new WebSocket(WS_URL);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      this.reconnectAttempts = 0;
      this.pendingReconnect = false;
      this.lastActivityAt = Date.now();
      this.startHeartbeat();
      // 重连后恢复全部订阅
      this.sendFrame({
        action: "subscribe",
        channels: [...this.subscribers.keys()],
      });
      this.notifyStatus(true);
    };

    ws.onmessage = (event: MessageEvent) => {
      this.lastActivityAt = Date.now();
      this.handleRawMessage(event.data);
    };

    ws.onclose = () => {
      this.stopHeartbeat();
      this.notifyStatus(false);
      if (!this.userClosed) this.scheduleReconnect();
    };

    ws.onerror = () => {
      // close 事件随后触发，此处仅防未处理异常
    };
  }

  /** 主动断开（不再自动重连） */
  disconnect(): void {
    this.userClosed = true;
    this.clearReconnectTimer();
    this.stopHeartbeat();
    this.ws?.close(1000, "client disconnect");
    this.ws = null;
    this.notifyStatus(false);
  }

  /**
   * 订阅频道；返回取消订阅函数。
   * 同频道多回调仅共享一次服务端订阅。
   */
  subscribe<T = unknown>(
    channel: string,
    handler: (data: T, message: WsMessage<T>) => void,
  ): () => void {
    let set = this.subscribers.get(channel);
    const isNewChannel = !set;
    if (!set) {
      set = new Set();
      this.subscribers.set(channel, set);
    }
    set.add(handler as WsDataHandler);

    if (isNewChannel && this.connected) {
      this.sendFrame({ action: "subscribe", channels: [channel] });
    }

    return () => {
      const current = this.subscribers.get(channel);
      if (!current) return;
      current.delete(handler as WsDataHandler);
      if (current.size === 0) {
        this.subscribers.delete(channel);
        if (this.connected) {
          this.sendFrame({ action: "unsubscribe", channels: [channel] });
        }
      }
    };
  }

  /** 连接状态变化监听；返回取消监听函数 */
  onStatusChange(handler: StatusHandler): () => void {
    this.statusListeners.add(handler);
    return () => this.statusListeners.delete(handler);
  }

  /** 发送任意 JSON 帧（连接不可用时静默丢弃） */
  private sendFrame(payload: Record<string, unknown>): void {
    if (!this.connected) return;
    try {
      this.ws?.send(JSON.stringify(payload));
    } catch {
      // 发送失败等待 onclose 触发重连
    }
  }

  /* ------------------------------------------------------------------ */
  /* 消息分发                                                            */
  /* ------------------------------------------------------------------ */

  private handleRawMessage(raw: unknown): void {
    if (typeof raw !== "string") return;
    let msg: WsMessage;
    try {
      msg = JSON.parse(raw) as WsMessage;
    } catch {
      return;
    }
    if (!msg || typeof msg.channel !== "string") return;

    // 心跳应答与系统帧不进入业务分发
    if (msg.channel === "pong" || msg.channel === "error") return;

    const set = this.subscribers.get(msg.channel);
    if (!set) return;
    for (const handler of set) {
      try {
        handler(msg.data, msg as WsMessage);
      } catch (err) {
        // 单个订阅者回调异常不影响其他订阅者
        console.error(`[ws] handler error on "${msg.channel}":`, err);
      }
    }
  }

  /* ------------------------------------------------------------------ */
  /* 心跳                                                                */
  /* ------------------------------------------------------------------ */

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.connected) return;
      // 超时未收到任何消息（含 pong）→ 判定死链，主动断开触发重连
      if (Date.now() - this.lastActivityAt > DEAD_LINK_TIMEOUT_MS) {
        this.ws?.close(4000, "heartbeat timeout");
        return;
      }
      this.sendFrame({ action: "ping" });
    }, HEARTBEAT_INTERVAL_MS);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /* ------------------------------------------------------------------ */
  /* 重连调度（指数退避 + 页面可见性感知）                                  */
  /* ------------------------------------------------------------------ */

  private scheduleReconnect(): void {
    if (this.userClosed) return;
    if (typeof document !== "undefined" && document.hidden) {
      // 页面不可见：暂缓调度，恢复可见时立即重连
      this.pendingReconnect = true;
      return;
    }

    const delay = Math.min(
      BASE_RECONNECT_DELAY_MS * 2 ** this.reconnectAttempts,
      MAX_RECONNECT_DELAY_MS,
    );
    this.reconnectAttempts += 1;

    this.clearReconnectTimer();
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private bindVisibility(): void {
    if (this.visibilityBound || typeof document === "undefined") return;
    this.visibilityBound = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) return;
      if (this.userClosed) return;
      if (this.connected || this.connecting) return;
      if (this.pendingReconnect || this.subscribers.size > 0) {
        this.pendingReconnect = false;
        this.connect();
      }
    });
  }

  private notifyStatus(connected: boolean): void {
    for (const listener of this.statusListeners) {
      try {
        listener(connected);
      } catch {
        // 状态回调异常忽略
      }
    }
  }
}

/** 全局单例 */
export const wsClient = new WSClient();
