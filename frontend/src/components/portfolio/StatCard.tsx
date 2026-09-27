/**
 * 页面骨架件：PageHeader / SectionCard / StatCard / Badge。
 * 个人页面组统一视觉语言 —— mono kicker + 大标题、卡片分区、等宽指标。
 */

import type { ReactNode } from "react";

import { cn } from "./utils";

/* -------------------------------------------------------------------------- */
/* 页头                                                                        */
/* -------------------------------------------------------------------------- */

export function PageHeader({
  index,
  kicker,
  title,
  description,
  action,
}: {
  index?: string;
  kicker: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="animate-fade-up flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <p className="font-mono text-[10px] uppercase tracking-[0.28em] text-accent/80">
          {index ? `${index} · ` : ""}
          {kicker}
        </p>
        <h1 className="mt-1.5 text-xl font-bold tracking-tight text-[#e6edf3] lg:text-2xl">
          {title}
        </h1>
        <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-muted">{description}</p>
      </div>
      {action && <div className="flex shrink-0 items-center gap-2">{action}</div>}
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* 分区卡片                                                                    */
/* -------------------------------------------------------------------------- */

export function SectionCard({
  title,
  extra,
  children,
  className = "",
  bodyClassName = "",
}: {
  title?: string;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section
      className={cn(
        "overflow-hidden rounded-xl border border-line bg-bg-raised/80 backdrop-blur-sm",
        className,
      )}
    >
      {(title || extra) && (
        <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <h2 className="text-xs font-semibold uppercase tracking-wider text-muted">{title}</h2>
          {extra}
        </div>
      )}
      <div className={cn("p-4", bodyClassName)}>{children}</div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* 指标卡                                                                      */
/* -------------------------------------------------------------------------- */

const TONE_TEXT: Record<string, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  accent: "text-accent",
  muted: "text-muted",
  default: "text-[#e6edf3]",
};

export function StatCard({
  label,
  value,
  sub,
  tone = "default",
  className = "",
}: {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: keyof typeof TONE_TEXT;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-line bg-bg-raised/80 px-4 py-3.5 transition-colors hover:border-line/70",
        className,
      )}
    >
      <p className="text-[11px] font-medium tracking-wide text-muted">{label}</p>
      <p
        className={cn(
          "mt-1.5 font-mono text-lg font-semibold tabular-nums leading-tight",
          TONE_TEXT[tone],
        )}
      >
        {value}
      </p>
      {sub && <p className="mt-1 text-[11px] leading-snug text-muted">{sub}</p>}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 徽标                                                                        */
/* -------------------------------------------------------------------------- */

export function Badge({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide",
        className,
      )}
    >
      {children}
    </span>
  );
}

/** 空点占位 */
export function Dash() {
  return <span className="text-muted">—</span>;
}
