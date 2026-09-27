"use client";

/**
 * 市场页面组共享 UI 小组件 + 图表动态导入入口。
 *
 * EChart / KLineChart 均为 ssr:false 动态导入，页面直接从本模块引用，
 * 避免 ECharts 与 Lightweight Charts 在服务端被加载。
 */

import dynamic from "next/dynamic";
import type { ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* 动态图表                                                                    */
/* -------------------------------------------------------------------------- */

export function ChartLoading({ height = 300 }: { height?: number }) {
  return (
    <div
      style={{ height }}
      role="status"
      aria-label="图表加载中"
      className="w-full animate-pulse rounded-xl border border-line bg-bg-raised/50"
    />
  );
}

export const EChart = dynamic(
  () => import("@/components/market/EChart").then((m) => m.EChart),
  { ssr: false, loading: () => <ChartLoading /> },
);

export const KLineChart = dynamic(
  () => import("@/components/market/KLineChart").then((m) => m.KLineChart),
  { ssr: false, loading: () => <ChartLoading height={440} /> },
);

/* -------------------------------------------------------------------------- */
/* 布局小组件                                                                  */
/* -------------------------------------------------------------------------- */

export function PageHeader({
  title,
  subtitle,
  right,
}: {
  title: string;
  subtitle?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-bold tracking-tight text-[#e6edf3]">{title}</h1>
        {subtitle && <p className="mt-1 text-xs leading-relaxed text-muted">{subtitle}</p>}
      </div>
      {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
    </header>
  );
}

export function SectionTitle({ children, right }: { children: ReactNode; right?: ReactNode }) {
  return (
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 className="text-sm font-semibold text-[#c9d1d9]">{children}</h2>
      {right}
    </div>
  );
}

const TILE_TONE: Record<string, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  accent: "text-accent",
  muted: "text-muted",
  default: "text-[#e6edf3]",
};

export function StatTile({
  label,
  value,
  sub,
  tone = "default",
  className = "",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: "up" | "down" | "warn" | "accent" | "muted" | "default";
  className?: string;
}) {
  return (
    <div className={`rounded-xl border border-line bg-bg-raised p-4 ${className}`}>
      <p className="text-[11px] font-medium uppercase tracking-wider text-muted">{label}</p>
      <p
        className={`mt-1.5 font-mono text-lg font-semibold leading-tight tabular-nums ${TILE_TONE[tone]}`}
      >
        {value ?? "—"}
      </p>
      {sub && <p className="mt-1 text-[11px] leading-relaxed text-muted">{sub}</p>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 分段仪表条（估值五档 / 风险四档）                                             */
/* -------------------------------------------------------------------------- */

export interface GaugeLevelView {
  label: string;
  color: string;
}

export function SegmentBar({
  levels,
  activeIndex,
  caption,
  className = "",
}: {
  levels: GaugeLevelView[];
  /** 当前命中档位索引，-1 表示未知 */
  activeIndex: number;
  caption?: ReactNode;
  className?: string;
}) {
  const valid = activeIndex >= 0 && activeIndex < levels.length;
  return (
    <div className={className}>
      <div className="relative pt-2">
        {valid && (
          <span
            aria-hidden
            className="absolute top-0 z-10 -translate-x-1/2 text-[11px] leading-none transition-all duration-500"
            style={{
              left: `${((activeIndex + 0.5) / levels.length) * 100}%`,
              color: levels[activeIndex]?.color,
            }}
          >
            ▼
          </span>
        )}
        <div className="flex h-3 overflow-hidden rounded-full border border-line">
          {levels.map((level, i) => (
            <div
              key={level.label}
              className="flex-1 transition-opacity duration-500"
              style={{
                backgroundColor: level.color,
                opacity: !valid || activeIndex === i ? 1 : 0.22,
              }}
              title={level.label}
            />
          ))}
        </div>
      </div>
      <div className="mt-2 flex" aria-hidden={false}>
        {levels.map((level, i) => (
          <span
            key={level.label}
            className={`flex-1 text-center text-[10px] leading-tight ${
              i === activeIndex ? "font-semibold text-[#e6edf3]" : "text-muted"
            }`}
          >
            {level.label}
          </span>
        ))}
      </div>
      {caption && <p className="mt-2 text-center text-[11px] leading-relaxed text-muted">{caption}</p>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 双色占比条（多空比等）                                                       */
/* -------------------------------------------------------------------------- */

export function SplitBar({
  leftLabel,
  rightLabel,
  leftPct,
  leftColor = "#3fb950",
  rightColor = "#f85149",
}: {
  leftLabel: string;
  rightLabel: string;
  /** 0-100 */
  leftPct: number;
  leftColor?: string;
  rightColor?: string;
}) {
  const left = Math.max(0, Math.min(100, leftPct));
  return (
    <div>
      <div className="flex h-2.5 overflow-hidden rounded-full border border-line">
        <div style={{ width: `${left}%`, backgroundColor: leftColor }} className="transition-all duration-500" />
        <div style={{ width: `${100 - left}%`, backgroundColor: rightColor }} className="transition-all duration-500" />
      </div>
      <div className="mt-1.5 flex justify-between text-[11px]">
        <span className="font-mono tabular-nums text-up">{leftLabel}</span>
        <span className="font-mono tabular-nums text-down">{rightLabel}</span>
      </div>
    </div>
  );
}
