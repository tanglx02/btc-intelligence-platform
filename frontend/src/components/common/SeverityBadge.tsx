/**
 * 等级徽标：INFO 蓝 / WARNING 黄 / HIGH 橙 / CRITICAL 红。
 */

import type { Severity } from "@/types/api";

const SEVERITY_META: Record<Severity, { label: string; className: string }> = {
  INFO: { label: "提示", className: "border-accent/40 bg-accent/10 text-accent" },
  WARNING: { label: "警告", className: "border-warn/40 bg-warn/10 text-warn" },
  HIGH: { label: "高", className: "border-orange/40 bg-orange/10 text-orange" },
  CRITICAL: { label: "严重", className: "border-down/50 bg-down/10 text-down" },
};

interface SeverityBadgeProps {
  severity?: Severity | null;
  /** 覆盖默认中文文案 */
  label?: string;
  className?: string;
}

export function SeverityBadge({ severity, label, className = "" }: SeverityBadgeProps) {
  if (!severity) return null;
  const meta = SEVERITY_META[severity];
  if (!meta) return null;

  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide ${meta.className} ${className}`}
    >
      <span aria-hidden className="h-1 w-1 rounded-full bg-current" />
      {label ?? meta.label}
    </span>
  );
}
