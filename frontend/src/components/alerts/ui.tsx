"use client";

/**
 * 智能预警模块通用 UI 元件：
 * 页头 / 表单字段 / 内联提示条 / 弹窗 / 确认框 / 统计卡。
 */

import { useEffect, type ReactNode } from "react";

/* -------------------------------------------------------------------------- */
/* 样式常量                                                                    */
/* -------------------------------------------------------------------------- */

export const btnPrimary =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-accent/50 bg-accent/15 px-3.5 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-50";

export const btnGhost =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-line bg-bg-raised px-3 py-1.5 text-xs text-[#e6edf3] transition-colors hover:border-muted/50 hover:bg-bg-hover disabled:cursor-not-allowed disabled:opacity-50";

export const btnDanger =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-down/40 bg-down/10 px-3 py-1.5 text-xs text-down transition-colors hover:bg-down/20 disabled:cursor-not-allowed disabled:opacity-50";

export const btnIcon =
  "inline-flex h-7 min-w-7 items-center justify-center rounded-md border border-line bg-bg-raised px-1.5 text-[11px] text-muted transition-colors hover:border-accent/50 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40";

export const inputCls =
  "w-full rounded-lg border border-line bg-bg px-2.5 py-1.5 text-xs text-[#e6edf3] placeholder:text-muted/50 focus:border-accent/60 focus:outline-none disabled:opacity-60";

export const selectCls = `${inputCls} appearance-none bg-bg-hover/60`;

export const cardCls = "rounded-xl border border-line bg-bg-raised";

/* -------------------------------------------------------------------------- */
/* 页头                                                                        */
/* -------------------------------------------------------------------------- */

interface PageHeaderProps {
  eyebrow: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
}

export function PageHeader({ eyebrow, title, description, actions }: PageHeaderProps) {
  return (
    <header className="animate-fade-up flex flex-col gap-3 pb-1 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p className="font-mono text-[10px] uppercase tracking-[0.22em] text-accent">{eyebrow}</p>
        <h1 className="mt-1.5 text-xl font-bold tracking-tight text-[#e6edf3] lg:text-2xl">
          {title}
        </h1>
        {description && <p className="mt-1.5 max-w-2xl text-xs leading-relaxed text-muted">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

/* -------------------------------------------------------------------------- */
/* 表单字段                                                                    */
/* -------------------------------------------------------------------------- */

interface FieldProps {
  label: string;
  hint?: string;
  required?: boolean;
  children: ReactNode;
  className?: string;
}

export function Field({ label, hint, required, children, className = "" }: FieldProps) {
  return (
    <label className={`flex flex-col gap-1.5 ${className}`}>
      <span className="flex items-center gap-1 text-[11px] font-medium text-muted">
        {label}
        {required && <span className="text-down">*</span>}
        {hint && <InfoHint text={hint} />}
      </span>
      {children}
    </label>
  );
}

/** ℹ️ hover 提示图标 */
export function InfoHint({ text }: { text: string }) {
  return (
    <span className="group/hint relative inline-flex" tabIndex={0} aria-label={text}>
      <span
        aria-hidden
        className="flex h-3.5 w-3.5 cursor-help items-center justify-center rounded-full border border-line text-[9px] leading-none text-muted transition-colors group-hover/hint:border-accent/60 group-hover/hint:text-accent"
      >
        i
      </span>
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full left-1/2 z-40 mb-1.5 w-56 -translate-x-1/2 rounded-lg border border-line bg-bg-raised px-2.5 py-2 text-[11px] leading-relaxed text-[#e6edf3] opacity-0 shadow-xl transition-opacity duration-150 group-hover/hint:opacity-100 group-focus-within/hint:opacity-100"
      >
        {text}
      </span>
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* 内联提示条                                                                   */
/* -------------------------------------------------------------------------- */

const BANNER_TONES = {
  success: "border-up/40 bg-up/10 text-up",
  error: "border-down/40 bg-down/10 text-down",
  info: "border-accent/40 bg-accent/10 text-accent",
} as const;

interface BannerProps {
  tone: keyof typeof BANNER_TONES;
  children: ReactNode;
  onClose?: () => void;
  className?: string;
}

export function Banner({ tone, children, onClose, className = "" }: BannerProps) {
  const icon = tone === "success" ? "✓" : tone === "error" ? "✕" : "›";
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={`animate-fade-up flex items-start gap-2 rounded-lg border px-3 py-2 text-xs leading-relaxed ${BANNER_TONES[tone]} ${className}`}
    >
      <span aria-hidden className="mt-px font-bold">{icon}</span>
      <div className="min-w-0 flex-1">{children}</div>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          aria-label="关闭提示"
          className="shrink-0 opacity-60 transition-opacity hover:opacity-100"
        >
          ✕
        </button>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 弹窗                                                                        */
/* -------------------------------------------------------------------------- */

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  /** 最大宽度 class（默认 max-w-lg） */
  widthClass?: string;
}

export function Modal({ open, onClose, title, children, widthClass = "max-w-lg" }: ModalProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
    >
      <button
        type="button"
        aria-label="关闭弹窗"
        className="absolute inset-0 cursor-default bg-bg-deep/70 backdrop-blur-sm"
        onClick={onClose}
      />
      <div
        className={`animate-fade-up relative w-full ${widthClass} max-h-[85vh] overflow-y-auto rounded-xl border border-line bg-bg-raised shadow-2xl`}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between gap-3 border-b border-line bg-bg-raised/95 px-4 py-3 backdrop-blur">
          <h2 className="text-sm font-semibold text-[#e6edf3]">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭"
            className="rounded-md border border-line px-2 py-0.5 text-xs text-muted transition-colors hover:border-down/50 hover:text-down"
          >
            ✕
          </button>
        </div>
        <div className="px-4 py-4">{children}</div>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 确认框                                                                      */
/* -------------------------------------------------------------------------- */

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "确认",
  danger = false,
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal open={open} onClose={onCancel} title={title} widthClass="max-w-sm">
      <div className="text-xs leading-relaxed text-muted">{message}</div>
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" className={btnGhost} onClick={onCancel} disabled={loading}>
          取消
        </button>
        <button
          type="button"
          className={danger ? btnDanger : btnPrimary}
          onClick={onConfirm}
          disabled={loading}
        >
          {loading ? "处理中…" : confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 统计卡                                                                      */
/* -------------------------------------------------------------------------- */

const STAT_TONES = {
  accent: "from-accent/60",
  up: "from-up/60",
  warn: "from-warn/60",
  down: "from-down/60",
  muted: "from-muted/40",
} as const;

interface StatCardProps {
  label: string;
  value: ReactNode;
  sub?: ReactNode;
  tone?: keyof typeof STAT_TONES;
  /** 入场动画延迟（ms） */
  delay?: number;
}

export function StatCard({ label, value, sub, tone = "accent", delay = 0 }: StatCardProps) {
  return (
    <div
      className="animate-fade-up relative overflow-hidden rounded-xl border border-line bg-bg-raised p-4 transition-colors hover:border-muted/40"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div
        aria-hidden
        className={`absolute inset-x-0 top-0 h-px bg-gradient-to-r ${STAT_TONES[tone]} to-transparent`}
      />
      <p className="text-[11px] font-medium uppercase tracking-wider text-muted">{label}</p>
      <p className="mt-2 font-mono text-2xl font-semibold tabular-nums text-[#e6edf3]">{value}</p>
      {sub && <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{sub}</p>}
    </div>
  );
}
