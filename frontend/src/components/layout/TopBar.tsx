"use client";

/**
 * 顶部工具栏：页面标题 | BTC 实时价格胶囊（WS） | 全局数据状态灯 + 模式切换。
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { TrendIndicator } from "@/components/common/TrendIndicator";
import { ROUTE_TITLES } from "@/components/layout/Sidebar";
import { useApi } from "@/hooks/useApi";
import { useHydrated } from "@/hooks/useHydrated";
import { useLivePrice } from "@/hooks/useWebSocket";
import { apiClient } from "@/lib/api";
import { useUIStore } from "@/stores/ui";

/* -------------------------------------------------------------------------- */
/* BTC 价格胶囊                                                                */
/* -------------------------------------------------------------------------- */

function PriceCapsule() {
  const { price, change24h, connected } = useLivePrice("BTC/USDT");

  // 价格变化方向闪烁
  const prevRef = useRef<number | null>(null);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    if (price === null) return;
    const prev = prevRef.current;
    prevRef.current = price;
    if (prev === null || price === prev) return;
    const next = price > prev ? "up" : "down";
    setFlash(next);
    const t = setTimeout(() => setFlash(null), 800);
    return () => clearTimeout(t);
  }, [price]);

  return (
    <Link
      href="/market"
      title={connected ? "实时价格 · 点击查看行情" : "实时更新已暂停（WebSocket 断开）"}
      className="group flex items-center gap-2 rounded-full border border-line bg-bg-raised px-3 py-1.5 transition-colors hover:border-accent/50"
    >
      <span aria-hidden className="flex h-4 w-4 items-center justify-center rounded-full border border-orange/40 bg-orange/10 font-mono text-[9px] font-bold text-orange">
        ₿
      </span>
      <span
        key={flash ?? "static"}
        className={`font-mono text-[13px] font-semibold tabular-nums ${
          flash === "up"
            ? "animate-flash-up text-up"
            : flash === "down"
              ? "animate-flash-down text-down"
              : "text-[#e6edf3]"
        }`}
      >
        {price !== null
          ? `$${price.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
          : "—"}
      </span>
      <TrendIndicator value={change24h} size="xs" />
      <span
        aria-hidden
        className={`h-1.5 w-1.5 rounded-full ${connected ? "bg-up animate-pulse-soft" : "bg-warn"}`}
      />
    </Link>
  );
}

/* -------------------------------------------------------------------------- */
/* 全局数据状态灯                                                              */
/* -------------------------------------------------------------------------- */

const STATUS_DOT: Record<string, { className: string; label: string }> = {
  ok: { className: "bg-up shadow-[0_0_8px_rgba(63,185,80,0.6)]", label: "数据正常" },
  stale: { className: "bg-warn shadow-[0_0_8px_rgba(210,153,34,0.6)]", label: "部分数据降级" },
  error: { className: "bg-down shadow-[0_0_8px_rgba(248,81,73,0.6)]", label: "核心数据不可用" },
};

function resolveGlobalStatus(
  data: { status: string } | null,
  error: unknown,
  loading: boolean,
): "ok" | "stale" | "error" {
  if (error) return "error";
  if (!data && loading) return "ok"; // 首次加载中先按正常显示
  const backend = data?.status ?? "OK";
  if (backend === "OK") return "ok";
  if (backend === "DEGRADED") return "stale";
  return "error";
}

function DataStatusLight() {
  const setGlobalDataStatus = useUIStore((s) => s.setGlobalDataStatus);
  const { data, error, loading } = useApi(() => apiClient.system.getHealth(), [], {
    refreshInterval: 60_000,
  });

  const status = resolveGlobalStatus(data, error, loading);

  useEffect(() => {
    setGlobalDataStatus(status);
  }, [status, setGlobalDataStatus]);

  const dot = STATUS_DOT[status] ?? STATUS_DOT.ok;

  return (
    <Link
      href="/quality"
      title={`数据状态：${dot.label}${data?.status ? `（后端 ${data.status}）` : ""} · 点击查看数据质量`}
      className="flex items-center gap-2 rounded-full border border-line bg-bg-raised px-2.5 py-1.5 transition-colors hover:border-accent/50"
      aria-label={`全局数据状态：${dot.label}`}
    >
      <span aria-hidden className={`h-2 w-2 rounded-full ${dot.className} ${status !== "ok" ? "animate-pulse-soft" : ""}`} />
      <span className="hidden text-[11px] text-muted sm:inline">{dot.label}</span>
    </Link>
  );
}

/* -------------------------------------------------------------------------- */
/* TopBar                                                                    */
/* -------------------------------------------------------------------------- */

function resolveTitle(pathname: string): string {
  if (ROUTE_TITLES[pathname]) return ROUTE_TITLES[pathname];
  // 最长前缀匹配（子路由）
  const match = Object.keys(ROUTE_TITLES)
    .filter((href) => href !== "/" && pathname.startsWith(href))
    .sort((a, b) => b.length - a.length)[0];
  return match ? ROUTE_TITLES[match] : "BTC 全市场智能研究平台";
}

export function TopBar() {
  const pathname = usePathname();
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const mode = useUIStore((s) => s.mode);
  const toggleMode = useUIStore((s) => s.toggleMode);

  const mounted = useHydrated();

  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-line bg-bg/85 px-4 backdrop-blur-md lg:px-6">
      {/* 左：汉堡（移动端）+ 标题 */}
      <div className="flex min-w-0 flex-1 items-center gap-3">
        <button
          type="button"
          onClick={toggleSidebar}
          className="rounded-md border border-line p-1.5 text-muted transition-colors hover:text-[#e6edf3] lg:hidden"
          aria-label="打开导航菜单"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden>
            <path d="M2 4h12M2 8h12M2 12h12" />
          </svg>
        </button>
        <h1 className="truncate text-sm font-semibold tracking-wide text-[#e6edf3]">
          {resolveTitle(pathname)}
        </h1>
      </div>

      {/* 中：价格胶囊 */}
      <div className="flex items-center">
        <PriceCapsule />
      </div>

      {/* 右：状态灯 + 模式切换 */}
      <div className="flex flex-1 items-center justify-end gap-2">
        <DataStatusLight />
        <button
          type="button"
          onClick={toggleMode}
          title="切换普通/专业模式"
          className="rounded-full border border-line bg-bg-raised px-3 py-1.5 text-[11px] font-medium text-muted transition-colors hover:border-accent/50 hover:text-accent"
        >
          {mounted ? (mode === "simple" ? "普通" : "专业") : "模式"}
        </button>
      </div>
    </header>
  );
}
