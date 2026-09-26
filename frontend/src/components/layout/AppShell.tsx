"use client";

/**
 * 应用外壳（客户端组件包装）：
 * 组装 Sidebar + TopBar + 主内容区，并在挂载时初始化 WebSocket 连接。
 */

import { type ReactNode, useEffect } from "react";

import { Sidebar } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { wsClient } from "@/lib/ws";

export function AppShell({ children }: { children: ReactNode }) {
  // 全站唯一的 WS 连接入口：价格胶囊等订阅方共享 wsClient 单例
  useEffect(() => {
    wsClient.connect();
  }, []);

  return (
    <div className="min-h-screen">
      <Sidebar />
      <div className="flex min-h-screen flex-col lg:pl-60">
        <TopBar />
        <main className="mx-auto w-full max-w-7xl flex-1 px-4 py-6 lg:px-8">{children}</main>
        <footer className="border-t border-line px-4 py-4 lg:px-8">
          <div className="mx-auto flex max-w-7xl flex-col gap-1 text-[11px] leading-relaxed text-muted sm:flex-row sm:items-center sm:justify-between">
            <p>
              数据来自公开第三方数据源，仅供研究参考，不构成任何投资建议。
            </p>
            <p className="font-mono tabular-nums">
              BTC 全市场智能研究平台 · v0.1
            </p>
          </div>
        </footer>
      </div>
    </div>
  );
}
