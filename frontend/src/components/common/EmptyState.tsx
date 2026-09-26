/**
 * 空状态占位。
 */

import type { ReactNode } from "react";

interface EmptyStateProps {
  title: string;
  description?: string;
  /** 底部操作（如引导按钮） */
  action?: ReactNode;
  className?: string;
}

export function EmptyState({ title, description, action, className = "" }: EmptyStateProps) {
  return (
    <div
      className={`flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-line px-6 py-10 text-center ${className}`}
    >
      <div aria-hidden className="flex h-10 w-10 items-center justify-center rounded-full border border-line bg-bg-raised text-muted">
        ◇
      </div>
      <p className="text-sm font-medium text-[#e6edf3]">{title}</p>
      {description && <p className="max-w-sm text-xs leading-relaxed text-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
