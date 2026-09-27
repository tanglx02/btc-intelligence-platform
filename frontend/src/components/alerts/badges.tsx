/**
 * 智能预警模块徽标：规则状态点 / 事件状态 / 通知状态。
 * 严重程度复用 components/common/SeverityBadge（五级归一化到四级契约）。
 */

import { SeverityBadge } from "@/components/common/SeverityBadge";
import type { Severity } from "@/types/api";

import { normalizeSeverity } from "./conditionUtils";

/* -------------------------------------------------------------------------- */
/* 严重程度（五级 -> SeverityBadge）                                             */
/* -------------------------------------------------------------------------- */

export function SeverityBadge5({
  severity,
  className = "",
}: {
  severity?: string | null;
  className?: string;
}) {
  const mapped: Severity = normalizeSeverity(severity);
  return <SeverityBadge severity={mapped} className={className} />;
}

/* -------------------------------------------------------------------------- */
/* 规则状态点（启用绿点 / 暂停灰点 / 错误红点 / 归档淡点）                          */
/* -------------------------------------------------------------------------- */

const RULE_STATE_META: Record<string, { label: string; dot: string; text: string }> = {
  ACTIVE: { label: "启用中", dot: "bg-up animate-pulse-soft", text: "text-up" },
  PAUSED: { label: "已暂停", dot: "bg-muted", text: "text-muted" },
  ERROR: { label: "异常", dot: "bg-down", text: "text-down" },
  ARCHIVED: { label: "已归档", dot: "bg-muted/50", text: "text-muted/70" },
};

export function RuleStateBadge({
  state,
  isEnabled,
  className = "",
}: {
  state?: string | null;
  isEnabled?: boolean | null;
  className?: string;
}) {
  const key = state ?? (isEnabled === false ? "PAUSED" : "ACTIVE");
  const meta = RULE_STATE_META[key] ?? RULE_STATE_META.ACTIVE;
  return (
    <span
      title={isEnabled === false ? "未启用" : undefined}
      className={`inline-flex items-center gap-1.5 text-[11px] ${meta.text} ${className}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${meta.dot}`} />
      {meta.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 事件状态徽标：TRIGGERED 橙 / ACKED 蓝 / RESOLVED 绿 / SUPPRESSED 灰            */
/* -------------------------------------------------------------------------- */

const EVENT_STATUS_META: Record<string, { label: string; className: string }> = {
  TRIGGERED: { label: "待确认", className: "border-orange/40 bg-orange/10 text-orange" },
  ACKED: { label: "已确认", className: "border-accent/40 bg-accent/10 text-accent" },
  RESOLVED: { label: "已恢复", className: "border-up/40 bg-up/10 text-up" },
  SUPPRESSED: { label: "已抑制", className: "border-muted/40 bg-muted/10 text-muted" },
};

export function EventStatusBadge({
  status,
  className = "",
}: {
  status?: string | null;
  className?: string;
}) {
  if (!status) return null;
  const meta = EVENT_STATUS_META[status];
  if (!meta) return null;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide ${meta.className} ${className}`}
    >
      <span aria-hidden className="h-1 w-1 rounded-full bg-current" />
      {meta.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 通知（投递）状态徽标：SENT 绿 / FAILED 红 / PENDING 黄                         */
/* -------------------------------------------------------------------------- */

const NOTIF_META: Record<string, { label: string; className: string }> = {
  SENT: { label: "已发送", className: "border-up/40 bg-up/10 text-up" },
  FAILED: { label: "发送失败", className: "border-down/50 bg-down/10 text-down" },
  PENDING: { label: "待发送", className: "border-warn/40 bg-warn/10 text-warn" },
  SUPPRESSED: { label: "已抑制", className: "border-muted/40 bg-muted/10 text-muted" },
};

export function NotifBadge({
  status,
  className = "",
}: {
  status?: string | null;
  className?: string;
}) {
  if (!status) return <span className="text-[11px] text-muted">—</span>;
  const meta = NOTIF_META[status];
  if (!meta) return null;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none tracking-wide ${meta.className} ${className}`}
    >
      <span aria-hidden className="h-1 w-1 rounded-full bg-current" />
      {meta.label}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 求值结果三态点                                                                */
/* -------------------------------------------------------------------------- */

const EVAL_META: Record<string, { label: string; className: string }> = {
  TRUE: { label: "满足", className: "text-up border-up/40 bg-up/10" },
  FALSE: { label: "未满足", className: "text-muted border-line bg-bg-hover" },
  UNKNOWN: { label: "数据不足", className: "text-warn border-warn/40 bg-warn/10" },
};

export function EvalResultBadge({ result }: { result?: string | null }) {
  const meta = EVAL_META[result ?? ""] ?? EVAL_META.FALSE;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none ${meta.className}`}
    >
      <span aria-hidden className="h-1 w-1 rounded-full bg-current" />
      {meta.label}
    </span>
  );
}
