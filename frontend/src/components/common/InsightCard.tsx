"use client";

/**
 * 核心通用卡片：统一三级信息层级（docs/architecture/16-page-architecture.md 第 5 节）。
 *
 * Level 1 摘要（默认）：一句话结论 + 状态颜色 + 值；
 * Level 2 展开（点击卡片）：解释、支持/反对证据、自定义内容；
 * Level 3 原始数据（点击「原始数据」）：JSON、数据源、更新时间。
 */

import { useState, type ReactNode } from "react";

import { DataSourceBadge } from "@/components/common/DataSourceBadge";
import { EvidenceList, normalizeEvidence, type EvidenceBundle } from "@/components/common/EvidenceList";
import { QualityBadge } from "@/components/common/QualityBadge";
import { SeverityBadge } from "@/components/common/SeverityBadge";
import type { ApiMeta, QualityStatus, Severity } from "@/types/api";

interface InsightCardProps {
  title: string;
  /** Level 1 值区（文案或数字），如「偏高」/「$108,240」 */
  value?: ReactNode;
  /** 语义色文字（配合 value） */
  valueTone?: "up" | "down" | "warn" | "accent" | "muted" | "default";
  /** 数据质量徽标 */
  quality?: QualityStatus | null;
  /** 告警等级徽标 */
  severity?: Severity | null;
  /** 一句话解释（Level 2） */
  explanation?: string | null;
  /** 证据（支持/反对），可传扁平数组或分组 */
  evidence?: EvidenceBundle | EngineEvidenceLike[] | null;
  /** Level 2 自定义内容（图表、关键数据表等） */
  children?: ReactNode;
  /** Level 3 原始数据（任意可 JSON 序列化对象） */
  rawData?: unknown;
  /** 数据源（Level 3 与徽标） */
  meta?: ApiMeta | null;
  source?: string | null;
  updatedAt?: string | null;
  /** 置信度 0~1 */
  confidence?: number | null;
  /** 点击卡片是否可展开（默认：有 Level 2/3 内容时自动判断） */
  expandable?: boolean;
  defaultOpen?: boolean;
  className?: string;
}

type EngineEvidenceLike = {
  factor: string;
  value?: unknown;
  interpretation?: string;
  weight?: number;
  supports: boolean;
  [key: string]: unknown;
};

const TONE_TEXT: Record<NonNullable<InsightCardProps["valueTone"]>, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  accent: "text-accent",
  muted: "text-muted",
  default: "text-[#e6edf3]",
};

export function InsightCard({
  title,
  value,
  valueTone = "default",
  quality,
  severity,
  explanation,
  evidence,
  children,
  rawData,
  meta,
  source,
  updatedAt,
  confidence,
  expandable,
  defaultOpen = false,
  className = "",
}: InsightCardProps) {
  const [open, setOpen] = useState(defaultOpen);
  const [showRaw, setShowRaw] = useState(false);

  const bundle: EvidenceBundle = normalizeEvidence(evidence as EvidenceBundle | null);
  const hasLevel2 = Boolean(explanation || bundle.supporting.length || bundle.opposing.length || children);
  const hasLevel3 = rawData !== undefined && rawData !== null;
  const canExpand = expandable ?? (hasLevel2 || hasLevel3);

  const borderTone =
    quality === "STALE"
      ? "border-t-warn/60"
      : quality === "CONFLICT"
        ? "border-t-down/60"
        : "border-t-transparent";

  return (
    <div
      className={`overflow-hidden rounded-xl border border-line border-t-2 bg-bg-raised transition-colors duration-200 ${borderTone} ${
        canExpand ? "cursor-pointer hover:border-accent/40" : ""
      } ${className}`}
      onClick={canExpand ? () => setOpen((v) => !v) : undefined}
      role={canExpand ? "button" : undefined}
      aria-expanded={canExpand ? open : undefined}
    >
      {/* Level 1 —— 摘要 */}
      <div className="p-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-[11px] font-medium uppercase tracking-wider text-muted">{title}</h3>
          <div className="flex items-center gap-1.5">
            <SeverityBadge severity={severity} />
            <QualityBadge status={quality} />
          </div>
        </div>

        <div className="mt-2 flex flex-wrap items-baseline gap-x-2.5 gap-y-1">
          <span className={`text-xl font-semibold leading-tight ${TONE_TEXT[valueTone]}`}>
            {value ?? "—"}
          </span>
          {confidence !== undefined && confidence !== null && (
            <span className="font-mono text-[10px] tabular-nums text-muted">
              置信 {Math.round(confidence * 100)}%
            </span>
          )}
        </div>

        {canExpand && (
          <div className="mt-2.5 flex items-center justify-between">
            <span className="text-[11px] text-accent/80 transition-colors group-hover:text-accent">
              {open ? "收起" : "展开详情"}
            </span>
            <span
              aria-hidden
              className={`text-[11px] text-muted transition-transform duration-200 ${open ? "rotate-180" : ""}`}
            >
              ⌄
            </span>
          </div>
        )}
      </div>

      {/* Level 2 —— 展开 */}
      {open && hasLevel2 && (
        <div className="animate-fade-up space-y-3 border-t border-line bg-bg px-4 py-3.5">
          {explanation && (
            <p className="text-xs leading-relaxed text-[#c9d1d9]">{explanation}</p>
          )}
          <EvidenceList evidence={bundle} />
          {children}
        </div>
      )}

      {/* Level 3 —— 原始数据 */}
      {open && hasLevel3 && (
        <div className="border-t border-line bg-bg px-4 py-3.5">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setShowRaw((v) => !v);
            }}
            className="mb-2 inline-flex items-center gap-1.5 text-[11px] text-accent transition-colors hover:text-accent/80"
          >
            <span aria-hidden className={`transition-transform duration-200 ${showRaw ? "rotate-90" : ""}`}>
              ▸
            </span>
            原始数据
          </button>
          {showRaw && (
            <pre className="animate-fade-up max-h-60 overflow-auto rounded-lg border border-line bg-bg-raised p-3 font-mono text-[10px] leading-relaxed text-muted">
              {JSON.stringify(rawData, null, 2)}
            </pre>
          )}
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            <DataSourceBadge
              source={source ?? meta?.source ?? null}
              updatedAt={updatedAt ?? meta?.observation_time ?? meta?.timestamp ?? null}
              qualityStatus={quality}
              meta={meta}
            />
          </div>
        </div>
      )}
    </div>
  );
}
