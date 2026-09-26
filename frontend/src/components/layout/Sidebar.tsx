"use client";

/**
 * 侧边栏导航：分组导航 + 当前路由高亮 + 分组折叠 + 移动端抽屉 + 底部模式切换。
 * 分组与路由对齐 docs/architecture/16-page-architecture.md 导航结构。
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { useHydrated } from "@/hooks/useHydrated";
import { useUIStore } from "@/stores/ui";

/* -------------------------------------------------------------------------- */
/* 导航配置                                                                    */
/* -------------------------------------------------------------------------- */

interface NavItem {
  label: string;
  href: string;
}

interface NavGroup {
  key: string;
  label: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    key: "overview",
    label: "总览",
    items: [{ label: "首页", href: "/" }],
  },
  {
    key: "market",
    label: "市场",
    items: [
      { label: "BTC行情", href: "/market" },
      { label: "市场周期", href: "/cycle" },
      { label: "估值", href: "/valuation" },
      { label: "链上", href: "/onchain" },
      { label: "资金流", href: "/exchange-flow" },
      { label: "ETF", href: "/etf" },
      { label: "衍生品", href: "/derivatives" },
      { label: "Options", href: "/options" },
      { label: "宏观", href: "/macro" },
      { label: "情绪", href: "/sentiment" },
      { label: "风险", href: "/risk" },
    ],
  },
  {
    key: "research",
    label: "研究",
    items: [
      { label: "历史回放", href: "/replay" },
      { label: "策略回测", href: "/backtest" },
      { label: "策略实验室", href: "/lab/strategies" },
      { label: "模型实验室", href: "/lab/models" },
      { label: "定投模拟", href: "/dca" },
    ],
  },
  {
    key: "mine",
    label: "我的",
    items: [
      { label: "我的计划", href: "/plans" },
      { label: "我的资产", href: "/portfolio" },
      { label: "智能预警", href: "/alerts" },
    ],
  },
  {
    key: "system",
    label: "系统",
    items: [
      { label: "数据源中心", href: "/providers" },
      { label: "数据质量", href: "/quality" },
      { label: "系统任务", href: "/jobs" },
      { label: "后台管理", href: "/admin" },
    ],
  },
];

/** 扁平路由 -> 标题映射（TopBar 复用） */
export const ROUTE_TITLES: Record<string, string> = Object.fromEntries(
  NAV_GROUPS.flatMap((g) => g.items.map((i) => [i.href, i.label])),
);

/* -------------------------------------------------------------------------- */
/* 分组图标（内联 SVG，几何风格）                                               */
/* -------------------------------------------------------------------------- */

function GroupIcon({ groupKey }: { groupKey: string }) {
  const common = {
    width: 12,
    height: 12,
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: 1.4,
    strokeLinecap: "round" as const,
    strokeLinejoin: "round" as const,
  };
  switch (groupKey) {
    case "overview":
      return (
        <svg {...common} aria-hidden>
          <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" />
          <rect x="9" y="1.5" width="5.5" height="5.5" rx="1" />
          <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" />
          <rect x="9" y="9" width="5.5" height="5.5" rx="1" />
        </svg>
      );
    case "market":
      return (
        <svg {...common} aria-hidden>
          <path d="M2 13.5 6 8l3 3 5-7" />
          <path d="M10.5 4H14v3.5" />
        </svg>
      );
    case "research":
      return (
        <svg {...common} aria-hidden>
          <path d="M6.5 1.5h3M7 1.5v4.5L2.8 12.6a1.6 1.6 0 0 0 1.4 2.4h7.6a1.6 1.6 0 0 0 1.4-2.4L9 6V1.5" />
          <path d="M4.5 10.5h7" />
        </svg>
      );
    case "mine":
      return (
        <svg {...common} aria-hidden>
          <rect x="1.5" y="4" width="13" height="9" rx="1.6" />
          <path d="M10.5 8.5h4M1.5 6.5h6" />
        </svg>
      );
    case "system":
      return (
        <svg {...common} aria-hidden>
          <rect x="1.5" y="2.5" width="13" height="4.5" rx="1.2" />
          <rect x="1.5" y="9" width="13" height="4.5" rx="1.2" />
          <circle cx="4.5" cy="4.75" r="0.6" fill="currentColor" stroke="none" />
          <circle cx="4.5" cy="11.25" r="0.6" fill="currentColor" stroke="none" />
        </svg>
      );
    default:
      return null;
  }
}

/* -------------------------------------------------------------------------- */
/* Sidebar                                                                    */
/* -------------------------------------------------------------------------- */

export function Sidebar() {
  const pathname = usePathname();
  const sidebarOpen = useUIStore((s) => s.sidebarOpen);
  const setSidebarOpen = useUIStore((s) => s.setSidebarOpen);
  const mode = useUIStore((s) => s.mode);
  const toggleMode = useUIStore((s) => s.toggleMode);

  // persist hydration 后才渲染模式开关，避免 SSR/CSR 不一致
  const mounted = useHydrated();

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const toggleGroup = (key: string) =>
    setCollapsed((prev) => ({ ...prev, [key]: !prev[key] }));

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname === href || pathname.startsWith(`${href}/`);

  const isGroupActive = (group: NavGroup) => group.items.some((i) => isActive(i.href));

  return (
    <>
      {/* 移动端遮罩 */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm lg:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-60 flex-col border-r border-line bg-bg transition-transform duration-300 ease-out lg:translate-x-0 ${
          sidebarOpen ? "translate-x-0" : "-translate-x-full"
        }`}
        aria-label="主导航"
      >
        {/* 品牌区 */}
        <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-line px-4">
          <span
            aria-hidden
            className="flex h-7 w-7 items-center justify-center rounded-lg border border-accent/40 bg-accent/10 font-mono text-sm font-bold text-accent"
          >
            ₿
          </span>
          <div className="min-w-0 leading-tight">
            <p className="truncate text-[13px] font-semibold tracking-wide text-[#e6edf3]">
              BTC 全市场研究
            </p>
            <p className="text-[10px] tracking-wider text-muted">SMART RESEARCH TERMINAL</p>
          </div>
        </div>

        {/* 导航分组 */}
        <nav className="flex-1 overflow-y-auto px-2.5 py-3 [scrollbar-width:thin]">
          {NAV_GROUPS.map((group) => {
            const isCollapsed = collapsed[group.key];
            const groupActive = isGroupActive(group);
            return (
              <section key={group.key} className="mb-1.5">
                <button
                  type="button"
                  onClick={() => toggleGroup(group.key)}
                  className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-[10px] font-semibold uppercase tracking-[0.14em] transition-colors ${
                    groupActive ? "text-accent" : "text-muted hover:text-[#c9d1d9]"
                  }`}
                  aria-expanded={!isCollapsed}
                >
                  <GroupIcon groupKey={group.key} />
                  <span className="flex-1 text-left">{group.label}</span>
                  <span
                    aria-hidden
                    className={`text-[9px] transition-transform duration-200 ${isCollapsed ? "-rotate-90" : ""}`}
                  >
                    ⌄
                  </span>
                </button>

                {!isCollapsed && (
                  <ul className="animate-fade-up mt-0.5 space-y-px">
                    {group.items.map((item) => {
                      const active = isActive(item.href);
                      return (
                        <li key={item.href}>
                          <Link
                            href={item.href}
                            onClick={() => setSidebarOpen(false)}
                            className={`relative flex items-center rounded-md py-[7px] pl-4 pr-2.5 text-[13px] transition-colors ${
                              active
                                ? "bg-accent/10 font-medium text-accent"
                                : "text-[#8b949e] hover:bg-bg-hover hover:text-[#c9d1d9]"
                            }`}
                          >
                            {/* active 左侧指示条 */}
                            {active && (
                              <span
                                aria-hidden
                                className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-r bg-accent"
                              />
                            )}
                            {item.label}
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            );
          })}
        </nav>

        {/* 底部：模式切换 */}
        <div className="shrink-0 border-t border-line px-4 py-3">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-[11px] font-medium text-[#c9d1d9]">
                {mounted ? (mode === "simple" ? "普通模式" : "专业模式") : "模式"}
              </p>
              <p className="text-[10px] text-muted">
                {mounted ? (mode === "simple" ? "结论优先 · 通俗易懂" : "全量数据 · 原始细节") : ""}
              </p>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={mounted ? mode === "pro" : false}
              aria-label="切换普通/专业模式"
              onClick={toggleMode}
              className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors duration-200 ${
                mounted && mode === "pro"
                  ? "border-accent/60 bg-accent/30"
                  : "border-line bg-bg-hover"
              }`}
            >
              <span
                aria-hidden
                className={`absolute top-1/2 h-3 w-3 -translate-y-1/2 rounded-full transition-all duration-200 ${
                  mounted && mode === "pro" ? "left-[calc(100%-15px)] bg-accent" : "left-0.5 bg-muted"
                }`}
              />
            </button>
          </div>
        </div>
      </aside>
    </>
  );
}
