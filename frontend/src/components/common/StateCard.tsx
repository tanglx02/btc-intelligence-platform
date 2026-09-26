"use client";

/**
 * 9 大维度状态卡：维度名 + 状态结论 + 颜色指示 + 置信度。
 * 点击展开支持/反对证据（Level 2）。
 */

import { useState, type ReactNode } from "react";

import { EvidenceList, type EvidenceBundle } from "@/components/common/EvidenceList";

export type StateColor = "up" | "down" | "warn" | "accent" | "muted";

const COLOR_META: Record<StateColor, { dot: string; text: string; ring: string; bar: string }> = {
  up: { dot: "bg-up", text: "text-up", ring: "ring-up/30", bar: "from-up/60" },
  down: { dot: "bg-down", text: "text-down", ring: "ring-down/30", bar: "from-down/60" },
  warn: { dot: "bg-warn", text: "text-warn", ring: "ring-warn/30", bar: "from-warn/60" },
  accent: { dot: "bg-accent", text: "text-accent", ring: "ring-accent/30", bar: "from-accent/60" },
  muted: { dot: "bg-muted", text: "text-muted", ring: "ring-white/10", bar: "from-muted/40" },
};

interface StateCardProps {
  /** 维度名，如「估值」「市场阶段」 */
  title: string;
  /** 状态结论（描述性文案），如「偏高」「趋势上涨」 */
  state: string;
  /** 置信度 0~1 */
  confidence?: number | null;
  /** 支撑/反对证据（点击展开） */
  evidence?: EvidenceBundle | null;
  /** 结论解释（Level 2） */
  explanation?: string | null;
  /** 状态语义色 */
  color?: StateColor;
  /** 状态变化时间 / 附加脚注 */
  footnote?: ReactNode;
  className?: string;
}

export function StateCard({
  title,
  state,
  confidence,
  evidence,
  explanation,
  color = "muted",
  footnote,
  className = "",
}: StateCardProps) {
  const [expanded, setExpanded] = useState(false);
  const meta = COLOR_META[color] ?? COLOR_META.muted;
  const hasDetail = Boolean((evidence && (evidence.supporting.length || evidence.opposing.length)) || explanation);

  return (
    <div
      className={`group relative overflow-hidden rounded-xl border border-line bg-bg-raised transition-all duration-200 ${
        hasDetail ? "cursor-pointer hover:border-accent/40 hover:shadow-[0_0_24px_rgba(88,166,255,0.06)]" : ""
      } ${className}`}
      onClick={hasDetail ? () => setExpanded((v) => !v) : undefined}
      role={hasDetail ? "button" : undefined}
      aria-expanded={hasDetail ? expanded : undefined}
    >
      {/* 顶部语义色渐变条 */}
      <div
        aria-hidden
        className={`absolute inset-x-0 top-0 h-px bg-gradient-to-r ${meta.bar} to-transparent`}
      />

      <div className="p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="text-[11px] font-medium uppercase tracking-wider text-muted">
            {title}
          </span>
          {confidence !== undefined && confidence !== null && (
            <span className="font-mono text-[10px] tabular-nums text-muted">
              置信 {Math.round(confidence * 100)}%
            </span>
          )}
        </div>

        <div className="mt-2.5 flex items-center gap-2">
          <span aria-hidden className={`h-2 w-2 shrink-0 rounded-full ${meta.dot} animate-pulse-soft`} />
          <span className={`text-lg font-semibold leading-tight ${meta.text}`}>{state}</span>
        </div>

        {footnote && <div className="mt-2 text-[10px] text-muted">{footnote}</div>}

        {hasDetail && (
          <button
            type="button"
            className="mt-3 inline-flex items-center gap-1 text-[11px] text-accent/80 transition-colors hover:text-accent"
            onClick={(e) => {
              e.stopPropagation();
              setExpanded((v) => !v);
            }}
            aria-expanded={expanded}
          >
            {expanded ? "收起" : "为什么？"}
            <span aria-hidden className={`transition-transform duration-200 ${expanded ? "rotate-90" : ""}`}>
              ›
            </span>
          </button>
        )}
      </div>

      {/* Level 2 展开：解释 + 证据 */}
      {expanded && hasDetail && (
        <div className="animate-fade-up border-t border-line bg-bg px-4 py-3">
          {explanation && <p className="mb-3 text-xs leading-relaxed text-muted">{explanation}</p>}
          <EvidenceList evidence={evidence} />
        </div>
      )}
    </div>
  );
}
