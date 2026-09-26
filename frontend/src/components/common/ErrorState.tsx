/**
 * 降级错误显示。
 *
 * 原则（需求第十一节）：STALE/故障时继续显示最近一次可信数据，
 * 绝不清空为 ERROR，绝不伪造数字 —— 因此本组件支持同时渲染缓存值。
 */

import type { ReactNode } from "react";

import { ApiRequestError } from "@/lib/api";

interface ErrorStateProps {
  /** 捕获到的错误（可选） */
  error?: unknown;
  /** 最后一次成功更新时间（Date / 时间戳 ms / ISO 字符串） */
  lastUpdatedAt?: Date | number | string | null;
  /** 缓存值区域（如果有最近数据，作为 children 渲染在警告条下方） */
  children?: ReactNode;
  onRetry?: () => void;
  /** 紧凑模式（卡片内嵌） */
  compact?: boolean;
  className?: string;
}

function formatTime(t?: Date | number | string | null): string | null {
  if (t === null || t === undefined) return null;
  const d = t instanceof Date ? t : new Date(t);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function describeError(error?: unknown): string {
  if (error instanceof ApiRequestError) return error.message;
  if (error instanceof Error) return error.message;
  return "数据暂时无法更新";
}

export function ErrorState({
  error,
  lastUpdatedAt,
  children,
  onRetry,
  compact = false,
  className = "",
}: ErrorStateProps) {
  const message = describeError(error);
  const timeText = formatTime(lastUpdatedAt);

  return (
    <div className={className}>
      <div
        className={`flex items-start gap-2.5 rounded-lg border border-warn/30 bg-warn/[0.06] ${compact ? "px-3 py-2" : "px-4 py-3"}`}
        role="alert"
      >
        <span aria-hidden className="mt-0.5 text-warn">
          ⚠
        </span>
        <div className="min-w-0 flex-1 text-xs leading-relaxed">
          <p className="font-medium text-warn">{message}</p>
          {timeText && (
            <p className="mt-0.5 text-muted">
              最后成功更新：{timeText}
            </p>
          )}
        </div>
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="shrink-0 rounded border border-warn/40 px-2 py-1 text-[11px] text-warn transition-colors hover:bg-warn/10"
          >
            重试
          </button>
        )}
      </div>

      {children && (
        <div className="mt-3">
          {children}
          <p className="mt-1.5 text-right text-[10px] text-muted">↑ 缓存数据 · 仅供参考</p>
        </div>
      )}
    </div>
  );
}
