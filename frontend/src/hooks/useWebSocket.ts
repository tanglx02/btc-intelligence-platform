"use client";

/**
 * WebSocket 相关 hooks。
 *
 * useLivePrice：订阅 price 频道，返回实时 BTC 价格；
 * 同时同步到全局 store（供 TopBar 价格胶囊等消费）。
 */

import { useEffect, useState } from "react";

import { wsClient } from "@/lib/ws";
import { useUIStore } from "@/stores/ui";
import type { WsPriceData } from "@/types/api";

export interface LivePriceState {
  price: number | null;
  /** 24h 涨跌幅（%） */
  change24h: number | null;
  source: string | null;
  qualityStatus: string | null;
  connected: boolean;
  /** 最后一次推送的本地时间戳（ms） */
  updatedAt: number | null;
}

/** 标准化交易对符号用于比较：BTC/USDT、BTC-USDT、BTCUSDT -> BTCUSDT */
function normalizeSymbol(s?: string | null): string {
  return (s ?? "").replace(/[/\-_]/g, "").toUpperCase();
}

/**
 * 订阅 WS price 频道，返回实时价格。
 * 组件卸载时自动取消订阅；连接由 wsClient 单例管理（多组件共享一条连接）。
 */
export function useLivePrice(symbol = "BTC/USDT"): LivePriceState {
  const setLastPrice = useUIStore((s) => s.setLastPrice);

  const [state, setState] = useState<LivePriceState>({
    price: null,
    change24h: null,
    source: null,
    qualityStatus: null,
    connected: false,
    updatedAt: null,
  });

  useEffect(() => {
    const wantSymbol = normalizeSymbol(symbol);

    const unsubscribe = wsClient.subscribe<WsPriceData>("price", (data) => {
      if (!data || typeof data.price !== "number") return;
      // 只处理匹配的交易对（平台以 BTC 为主，宽松匹配）
      if (wantSymbol && data.symbol && normalizeSymbol(data.symbol) !== wantSymbol) {
        return;
      }

      const change = typeof data.change_24h_pct === "number" ? data.change_24h_pct : null;
      setState((prev) => ({
        price: data.price,
        change24h: change ?? prev.change24h,
        source: data.source ?? prev.source,
        qualityStatus: data.quality_status ?? prev.qualityStatus,
        connected: true,
        updatedAt: Date.now(),
      }));

      if (typeof data.change_24h_pct === "number") {
        setLastPrice({ price: data.price, change24h: data.change_24h_pct });
      } else {
        setLastPrice({ price: data.price, change24h: 0 });
      }
    });

    const unsubscribeStatus = wsClient.onStatusChange((connected) => {
      setState((prev) => ({ ...prev, connected }));
    });

    // 确保连接建立（幂等；重连后 wsClient 自动恢复订阅）
    wsClient.connect();

    return () => {
      unsubscribe();
      unsubscribeStatus();
    };
  }, [symbol, setLastPrice]);

  return state;
}
