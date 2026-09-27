"use client";

/**
 * 系统管理页面组共享 UI 原语：
 * 页头 / 分区卡 / 状态灯（Provider 9 态 + 任务 7 态 + 质量 4 态）/ 评分条 /
 * 行内提示 / 键值对 / 表格骨架，以及时间与百分比格式化工具。
 *
 * 视觉语言对齐 docs/architecture/16-page-architecture.md 第 7 节状态颜色。
 */

import type { ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* 格式化工具                                                                  */
/* -------------------------------------------------------------------------- */

/** ISO 时间 → 本地化展示；无效值返回 "—" */
export function formatDateTime(t?: string | number | null): string {
  if (t === null || t === undefined || t === "") return "—";
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("zh-CN", { hour12: false });
}

/** 相对时间（刚刚 / N 分钟前 / N 小时前 / N 天前）；超过 30 天回退绝对日期 */
export function formatRelative(t?: string | number | null): string {
  if (t === null || t === undefined || t === "") return "—";
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return "—";
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 0) return formatDateTime(t);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  if (diffSec < 86400 * 30) return `${Math.floor(diffSec / 86400)} 天前`;
  return d.toLocaleDateString("zh-CN");
}

/**
 * 百分比归一：后端 success_rate 等字段为 0~1 比例，completeness_pct 等为 0~100，
 * 以 ≤1 视为比例（成功率为 1.0 表示 100%）统一转成 0~100 展示。
 */
export function toPct(v?: number | null): number | null {
  if (v === null || v === undefined || Number.isNaN(v)) return null;
  const pct = v <= 1 ? v * 100 : v;
  return Math.round(pct * 10) / 10;
}

/** 百分比文本（带 %）；null 显示 "—" */
export function formatPct(v?: number | null, digits = 1): string {
  const pct = toPct(v);
  if (pct === null) return "—";
  return `${pct.toFixed(digits)}%`;
}

/** 毫秒 → 可读文本（823 ms / 1.2 s） */
export function formatMs(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (v >= 1000) return `${(v / 1000).toFixed(1)} s`;
  return `${Math.round(v)} ms`;
}

/** 整数千分位 */
export function fmtInt(v?: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString("zh-CN");
}

/** 任务调度间隔（cron 优先，其次秒数）→ 可读文本 */
export function formatSchedule(
  cron?: string | null,
  intervalSeconds?: number | null,
): string {
  if (cron) return `cron ${cron}`;
  if (intervalSeconds !== null && intervalSeconds !== undefined) {
    if (intervalSeconds < 60) return `每 ${intervalSeconds} 秒`;
    if (intervalSeconds < 3600) return `每 ${Math.round(intervalSeconds / 60)} 分钟`;
    if (intervalSeconds < 86400) return `每 ${Math.round(intervalSeconds / 3600)} 小时`;
    return `每 ${Math.round(intervalSeconds / 86400)} 天`;
  }
  return "—";
}

/* -------------------------------------------------------------------------- */
/* 数据类别                                                                   */
/* -------------------------------------------------------------------------- */

const CATEGORY_LABELS: Record<string, string> = {
  MARKET: "市场行情",
  ONCHAIN: "链上数据",
  EXCHANGE_FLOW: "交易所资金流",
  ETF: "ETF 资金流",
  DERIVATIVES: "衍生品",
  OPTIONS: "期权",
  MACRO: "宏观",
  SENTIMENT: "情绪",
  NEWS: "新闻资讯",
};

export function categoryLabel(c?: string | null): string {
  if (!c) return "其他";
  return CATEGORY_LABELS[c.toUpperCase()] ?? c;
}

/** Provider 分节固定顺序（未列出的类别追加在末尾） */
export const CATEGORY_ORDER = [
  "MARKET",
  "ONCHAIN",
  "EXCHANGE_FLOW",
  "ETF",
  "DERIVATIVES",
  "OPTIONS",
  "MACRO",
  "SENTIMENT",
  "NEWS",
] as const;

/** Provider 9 大数据类别（配置表单下拉选项，与后端 ProviderCategory 枚举对齐） */
export const PROVIDER_CATEGORY_OPTIONS = [
  "MARKET",
  "ONCHAIN",
  "EXCHANGE_FLOW",
  "ETF",
  "DERIVATIVES",
  "OPTIONS",
  "MACRO",
  "SENTIMENT",
  "NEWS",
] as const;

/* -------------------------------------------------------------------------- */
/* 状态灯（圆点 + 颜色 + 中文标签）                                             */
/* -------------------------------------------------------------------------- */

interface StatusMeta {
  label: string;
  dot: string;
  text: string;
  /** 是否呼吸闪烁（运行/在线态） */
  pulse?: boolean;
}

/** Provider 9 态 + Job 7 态 + 质量 4 态 + 系统健康 3 态 统一映射 */
const STATUS_META: Record<string, StatusMeta> = {
  /* Provider 状态（backend/app/models/enums.py ProviderStatus） */
  ONLINE: { label: "在线", dot: "bg-up", text: "text-up", pulse: true },
  DEGRADED: { label: "降级", dot: "bg-warn", text: "text-warn" },
  SLOW: { label: "缓慢", dot: "bg-orange", text: "text-orange" },
  RATE_LIMITED: { label: "限流", dot: "bg-orange", text: "text-orange" },
  AUTH_ERROR: { label: "鉴权失败", dot: "bg-down", text: "text-down" },
  NETWORK_ERROR: { label: "网络错误", dot: "bg-down", text: "text-down" },
  DATA_ERROR: { label: "数据异常", dot: "bg-down", text: "text-down" },
  OFFLINE: { label: "离线", dot: "bg-down", text: "text-down" },
  DISABLED: { label: "已禁用", dot: "bg-muted", text: "text-muted" },
  /* 任务状态（JobStatus） */
  RUNNING: { label: "运行中", dot: "bg-up", text: "text-up", pulse: true },
  PENDING: { label: "排队中", dot: "bg-accent", text: "text-accent" },
  QUEUED: { label: "排队中", dot: "bg-accent", text: "text-accent" },
  PAUSED: { label: "已暂停", dot: "bg-warn", text: "text-warn" },
  COMPLETED: { label: "已完成", dot: "bg-up", text: "text-up" },
  SUCCESS: { label: "成功", dot: "bg-up", text: "text-up" },
  FAILED: { label: "失败", dot: "bg-down", text: "text-down" },
  CANCELLED: { label: "已取消", dot: "bg-muted", text: "text-muted" },
  SKIPPED: { label: "已跳过", dot: "bg-muted", text: "text-muted" },
  IDLE: { label: "空闲", dot: "bg-muted", text: "text-muted" },
  /* 质量检查状态（data_quality.status） */
  OK: { label: "正常", dot: "bg-up", text: "text-up" },
  WARNING: { label: "警告", dot: "bg-warn", text: "text-warn" },
  ERROR: { label: "错误", dot: "bg-down", text: "text-down" },
  CRITICAL: { label: "严重", dot: "bg-down", text: "text-down" },
  /* 系统健康（system/health.status，小写） */
  ok: { label: "健康", dot: "bg-up", text: "text-up" },
  degraded: { label: "降级", dot: "bg-warn", text: "text-warn" },
  critical: { label: "故障", dot: "bg-down", text: "text-down" },
};

export function statusMeta(status?: string | null): StatusMeta {
  if (!status) return { label: "未知", dot: "bg-muted", text: "text-muted" };
  return (
    STATUS_META[status] ?? { label: status, dot: "bg-muted", text: "text-muted" }
  );
}

interface StatusDotProps {
  status?: string | null;
  /** 覆盖默认中文标签 */
  label?: string;
  className?: string;
}

/** 状态灯：呼吸圆点 + 标签 */
export function StatusDot({ status, label, className = "" }: StatusDotProps) {
  const meta = statusMeta(status);
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap ${className}`}>
      <span
        aria-hidden
        className={`h-2 w-2 shrink-0 rounded-full ${meta.dot} ${
          meta.pulse ? "animate-pulse-soft" : ""
        }`}
      />
      <span className={`${meta.text} font-medium`}>{label ?? meta.label}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 评分条（0-100）                                                            */
/* -------------------------------------------------------------------------- */

/** 分值 → 颜色：≥80 绿 / ≥60 黄 / ≥40 橙 / 其余红 */
export function scoreColorClass(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "bg-muted";
  if (value >= 80) return "bg-up";
  if (value >= 60) return "bg-warn";
  if (value >= 40) return "bg-orange";
  return "bg-down";
}

interface ScoreBarProps {
  /** 0-100 */
  value?: number | null;
  className?: string;
}

export function ScoreBar({ value, className = "" }: ScoreBarProps) {
  const pct = value === null || value === undefined ? null : Math.max(0, Math.min(100, value));
  return (
    <div
      className={`h-1.5 w-full overflow-hidden rounded-full bg-line/60 ${className}`}
      role="meter"
      aria-valuenow={pct ?? undefined}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="评分"
    >
      {pct !== null && (
        <div
          className={`h-full rounded-full transition-all duration-500 ${scoreColorClass(value)}`}
          style={{ width: `${pct}%` }}
        />
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 布局原语                                                                    */
/* -------------------------------------------------------------------------- */

interface PageHeaderProps {
  kicker: string;
  title: string;
  description: string;
  /** 右侧元信息区（统计胶囊等） */
  children?: ReactNode;
}

export function PageHeader({ kicker, title, description, children }: PageHeaderProps) {
  return (
    <header className="animate-fade-up flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <p className="font-mono text-[11px] uppercase tracking-[0.22em] text-accent">{kicker}</p>
        <h1 className="mt-1.5 text-xl font-bold tracking-tight text-[#e6edf3] lg:text-2xl">
          {title}
        </h1>
        <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-muted">{description}</p>
      </div>
      {children && <div className="flex shrink-0 items-center gap-2">{children}</div>}
    </header>
  );
}

interface SectionCardProps {
  title: string;
  subtitle?: string;
  /** 标题行右侧操作区 */
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function SectionCard({ title, subtitle, right, children, className = "" }: SectionCardProps) {
  return (
    <section
      className={`animate-fade-up overflow-hidden rounded-xl border border-line bg-bg-raised ${className}`}
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-[#e6edf3]">{title}</h2>
          {subtitle && <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{subtitle}</p>}
        </div>
        {right && <div className="flex shrink-0 items-center gap-2">{right}</div>}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

/** 统计胶囊（页头右侧 / 概览条） */
export function StatChip({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  tone?: "default" | "up" | "warn" | "down";
}) {
  const toneClass =
    tone === "up"
      ? "border-up/40 text-up"
      : tone === "warn"
        ? "border-warn/40 text-warn"
        : tone === "down"
          ? "border-down/40 text-down"
          : "border-line text-[#e6edf3]";
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-lg border bg-bg px-2.5 py-1 text-[11px] leading-none ${toneClass}`}
    >
      <span className="text-muted">{label}</span>
      <span className="font-mono font-semibold tabular-nums">{value}</span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 行内提示（表单提交结果等）                                                   */
/* -------------------------------------------------------------------------- */

interface InlineNoticeProps {
  tone: "success" | "error" | "info";
  children: ReactNode;
  className?: string;
}

export function InlineNotice({ tone, children, className = "" }: InlineNoticeProps) {
  const meta =
    tone === "success"
      ? { border: "border-up/40", bg: "bg-up/[0.08]", text: "text-up", icon: "✓" }
      : tone === "error"
        ? { border: "border-down/40", bg: "bg-down/[0.08]", text: "text-down", icon: "✕" }
        : { border: "border-accent/40", bg: "bg-accent/[0.08]", text: "text-accent", icon: "ℹ" };
  return (
    <div
      role="status"
      className={`flex items-start gap-2 rounded-lg border ${meta.border} ${meta.bg} px-3 py-2 text-xs leading-relaxed ${meta.text} ${className}`}
    >
      <span aria-hidden className="mt-px shrink-0 font-bold">
        {meta.icon}
      </span>
      <div className="min-w-0 break-all">{children}</div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 键值对（展开详情）                                                          */
/* -------------------------------------------------------------------------- */

export function KeyValue({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: ReactNode;
  mono?: boolean;
  tone?: "up" | "warn" | "down";
}) {
  const toneClass =
    tone === "up" ? "text-up" : tone === "warn" ? "text-warn" : tone === "down" ? "text-down" : "";
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase tracking-wider text-muted">{label}</dt>
      <dd
        className={`mt-0.5 truncate text-xs text-[#e6edf3] ${mono ? "font-mono tabular-nums" : ""} ${toneClass}`}
        title={typeof value === "string" ? value : undefined}
      >
        {value}
      </dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 表格骨架                                                                    */
/* -------------------------------------------------------------------------- */

/** 统一表格外壳：横向滚动 + 表头样式 */
export function DataTable({
  columns,
  children,
  minWidth = 760,
}: {
  columns: string[];
  children: ReactNode;
  minWidth?: number;
}) {
  return (
    <div className="-mx-4 overflow-x-auto px-4 sm:mx-0 sm:px-0">
      <table className="w-full text-left text-xs" style={{ minWidth }}>
        <thead>
          <tr className="border-b border-line">
            {columns.map((c) => (
              <th key={c} className="whitespace-nowrap px-3 py-2 font-medium text-muted">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-line/60">{children}</tbody>
      </table>
    </div>
  );
}

export const tdClass = "px-3 py-2.5 align-middle text-[#e6edf3]";
export const tdMutedClass = "px-3 py-2.5 align-middle font-mono tabular-nums text-muted";
export const rowClass =
  "cursor-pointer transition-colors hover:bg-bg-hover [&_td]:whitespace-nowrap";

/* -------------------------------------------------------------------------- */
/* 按钮样式常量                                                                */
/* -------------------------------------------------------------------------- */

export const btnBase =
  "inline-flex items-center justify-center gap-1 rounded-lg border px-2.5 py-1 text-[11px] font-medium leading-none transition-colors disabled:cursor-not-allowed disabled:opacity-50";
export const btnPrimary = `${btnBase} border-accent/50 bg-accent/10 text-accent hover:bg-accent/20`;
export const btnGhost = `${btnBase} border-line bg-transparent text-muted hover:border-accent/40 hover:text-accent`;
export const btnDanger = `${btnBase} border-down/40 bg-transparent text-down hover:bg-down/10`;
export const btnSuccess = `${btnBase} border-up/40 bg-transparent text-up hover:bg-up/10`;

/* -------------------------------------------------------------------------- */
/* 表单控件                                                                    */
/* -------------------------------------------------------------------------- */

export const inputClass =
  "w-full rounded-lg border border-line bg-bg px-3 py-2 text-xs text-[#e6edf3] placeholder:text-muted/60 focus:border-accent/60 focus:outline-none disabled:opacity-50";

export const labelClass = "mb-1.5 block text-[11px] font-medium text-muted";

/** 表单字段外壳 */
export function Field({
  label,
  hint,
  children,
  className = "",
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <label className={labelClass}>
        {label}
        {hint && (
          <span className="ml-1.5 font-normal text-muted/70" title={hint}>
            <span
              aria-hidden
              className="mr-0.5 inline-block cursor-help rounded-full border border-line px-1 text-[9px] leading-[14px] text-muted"
              title={hint}
            >
              i
            </span>
            {hint}
          </span>
        )}
      </label>
      {children}
    </div>
  );
}

/** 启用/禁用开关（受控；点击不会冒泡到父级行点击事件） */
export function ToggleSwitch({
  checked,
  disabled = false,
  title,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  title?: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      title={title}
      disabled={disabled}
      onClick={(e) => {
        e.stopPropagation();
        if (!disabled) onChange(!checked);
      }}
      className={`relative inline-flex h-[18px] w-8 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        checked ? "border-up/60 bg-up/30" : "border-line bg-bg"
      }`}
    >
      <span
        aria-hidden
        className={`inline-block h-3 w-3 transform rounded-full transition-transform ${
          checked ? "translate-x-4 bg-up" : "translate-x-0.5 bg-muted"
        }`}
      />
    </button>
  );
}
