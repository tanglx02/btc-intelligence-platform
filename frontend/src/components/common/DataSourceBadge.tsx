"use client";

/**
 * 数据来源徽标：provider 名 + 更新时间，点击弹出溯源详情。
 * 对应 docs/architecture/16-page-architecture.md 第 6 节「数据溯源 UI」。
 */

import { useEffect, useState } from "react";

import type { ApiMeta, QualityStatus } from "@/types/api";

interface DataSourceBadgeProps {
  source?: string | null;
  updatedAt?: string | null;
  qualityStatus?: QualityStatus | null;
  /** 弹窗中展示的完整 meta（含 failover / 抓取时间等） */
  meta?: ApiMeta | null;
  /** 徽标紧凑模式（只显示首字母圆标） */
  compact?: boolean;
  className?: string;
}

function formatTime(t?: string | null): string | null {
  if (!t) return null;
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString("zh-CN", { hour12: false });
}

function relativeTime(t?: string | null): string | null {
  if (!t) return null;
  const d = new Date(t);
  if (Number.isNaN(d.getTime())) return null;
  const diffMs = Date.now() - d.getTime();
  const diffSec = Math.floor(diffMs / 1000);
  if (diffSec < 60) return "刚刚";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  return `${Math.floor(diffSec / 86400)} 天前`;
}

export function DataSourceBadge({
  source,
  updatedAt,
  qualityStatus,
  meta,
  compact = false,
  className = "",
}: DataSourceBadgeProps) {
  const [open, setOpen] = useState(false);

  // Escape 关闭弹窗
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!source) return null;

  const initial = source.charAt(0).toUpperCase();
  const rel = relativeTime(updatedAt);

  const qualityColor =
    qualityStatus === "VERIFIED"
      ? "border-up/40 text-up"
      : qualityStatus === "STALE"
        ? "border-warn/40 text-warn"
        : qualityStatus === "CONFLICT"
          ? "border-down/40 text-down"
          : "border-line text-muted";

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={`数据来源：${source}${rel ? ` · ${rel}` : ""}`}
        className={`inline-flex max-w-[220px] items-center gap-1.5 rounded border border-line bg-bg px-1.5 py-0.5 text-[10px] leading-none text-muted transition-colors hover:border-accent/50 hover:text-accent ${className}`}
      >
        <span
          aria-hidden
          className={`flex h-3.5 w-3.5 items-center justify-center rounded-sm border ${qualityColor} text-[8px] font-bold`}
        >
          {initial}
        </span>
        {!compact && <span className="truncate">{source}</span>}
        {!compact && rel && <span aria-hidden>·</span>}
        {!compact && rel && <span className="truncate">{rel}</span>}
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm"
          role="dialog"
          aria-label="数据来源详情"
          onClick={() => setOpen(false)}
        >
          <div
            className="w-full max-w-md rounded-xl border border-line bg-bg-raised shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-line px-4 py-3">
              <h3 className="text-sm font-semibold text-[#e6edf3]">数据来源</h3>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded p-1 text-muted transition-colors hover:bg-bg-hover hover:text-[#e6edf3]"
                aria-label="关闭"
              >
                ✕
              </button>
            </div>
            <dl className="space-y-2 px-4 py-3 text-xs">
              <Row label="Provider" value={source} mono />
              {qualityStatus && <Row label="质量状态" value={qualityStatus} mono />}
              {meta?.observation_time && (
                <Row label="数据时间" value={formatTime(meta.observation_time) ?? "-"} mono />
              )}
              {meta?.fetch_time && (
                <Row label="抓取时间" value={formatTime(meta.fetch_time) ?? "-"} mono />
              )}
              {updatedAt && (
                <Row label="更新时间" value={formatTime(updatedAt) ?? "-"} mono />
              )}
              {meta?.cache_hit !== undefined && (
                <Row label="缓存命中" value={meta.cache_hit ? "是" : "否"} mono />
              )}
              {meta?.failover && (
                <Row
                  label="故障切换"
                  value={`${meta.failover.from} → ${source}（${meta.failover.reason}）`}
                  mono
                />
              )}
            </dl>
            {meta && (
              <div className="border-t border-line px-4 py-3">
                <p className="mb-1.5 text-[10px] uppercase tracking-wider text-muted">原始 Meta</p>
                <pre className="max-h-40 overflow-auto rounded-lg bg-bg p-2.5 font-mono text-[10px] leading-relaxed text-muted">
                  {JSON.stringify(meta, null, 2)}
                </pre>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd className={`text-right text-[#e6edf3] ${mono ? "font-mono tabular-nums" : ""}`}>{value}</dd>
    </div>
  );
}
