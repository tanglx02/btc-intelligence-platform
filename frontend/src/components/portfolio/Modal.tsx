"use client";

/**
 * 模态框 + 危险操作确认框。
 *
 * 视觉：暗色面板浮层，backdrop 模糊，fade-up 入场；Esc / 点击遮罩关闭。
 */

import { useEffect, type ReactNode } from "react";

import { cn } from "./utils";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children?: ReactNode;
  footer?: ReactNode;
  /** 宽面板（表单网格用） */
  wide?: boolean;
}

export function Modal({ open, onClose, title, description, children, footer, wide }: ModalProps) {
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
      className="fixed inset-0 z-50 flex items-end justify-center bg-bg-deep/70 p-4 backdrop-blur-sm sm:items-center"
      role="dialog"
      aria-modal="true"
      aria-label={title}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className={cn(
          "animate-fade-up flex max-h-[88vh] w-full flex-col overflow-hidden rounded-xl border border-line bg-bg-raised shadow-[0_24px_64px_-16px_rgba(0,0,0,0.7)]",
          wide ? "max-w-2xl" : "max-w-md",
        )}
      >
        <div className="border-b border-line px-5 py-4">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-sm font-semibold text-[#e6edf3]">{title}</h2>
              {description && (
                <p className="mt-1 text-xs leading-relaxed text-muted">{description}</p>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="关闭"
              className="rounded p-1 text-muted transition-colors hover:bg-bg-hover hover:text-[#e6edf3]"
            >
              ✕
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">{children}</div>

        {footer && (
          <div className="flex items-center justify-end gap-2 border-t border-line bg-bg px-5 py-3.5">
            {footer}
          </div>
        )}
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
  description: string;
  confirmText?: string;
  loading?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmText = "确认删除",
  loading = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={onCancel}
      title={title}
      footer={
        <>
          <button
            type="button"
            onClick={onCancel}
            disabled={loading}
            className="rounded-lg border border-line px-3 py-1.5 text-xs text-muted transition-colors hover:bg-bg-hover hover:text-[#e6edf3] disabled:opacity-50"
          >
            取消
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={loading}
            className="rounded-lg border border-down/50 bg-down/15 px-3 py-1.5 text-xs font-medium text-down transition-colors hover:bg-down/25 disabled:opacity-50"
          >
            {loading ? "处理中…" : confirmText}
          </button>
        </>
      }
    >
      <p className="text-xs leading-relaxed text-[#c9d1d9]">{description}</p>
      <p className="mt-2 text-[11px] text-muted">该操作会记录审计日志，删除后历史数据不可恢复。</p>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 按钮（页面组内统一风格）                                                     */
/* -------------------------------------------------------------------------- */

export function PrimaryButton({
  children,
  loading = false,
  disabled = false,
  onClick,
  type = "button",
  className = "",
}: {
  children: ReactNode;
  loading?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  type?: "button" | "submit";
  className?: string;
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={loading || disabled}
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-lg border border-accent/50 bg-accent/15 px-3.5 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/25 disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
    >
      {loading && (
        <span
          aria-hidden
          className="h-3 w-3 animate-spin rounded-full border border-accent/40 border-t-accent"
        />
      )}
      {children}
    </button>
  );
}

export function GhostButton({
  children,
  onClick,
  disabled = false,
  danger = false,
  className = "",
  type = "button",
}: {
  children: ReactNode;
  onClick?: () => void;
  disabled?: boolean;
  danger?: boolean;
  className?: string;
  type?: "button" | "submit";
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "inline-flex items-center justify-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        danger
          ? "border-down/40 text-down hover:bg-down/10"
          : "border-line text-muted hover:bg-bg-hover hover:text-[#e6edf3]",
        className,
      )}
    >
      {children}
    </button>
  );
}
