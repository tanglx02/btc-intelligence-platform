"use client";

/**
 * 触发历史（/alerts/events）：垂直时间线。
 *
 * - 每条事件：时间 / 规则 / 等级 / 状态 / 触发值 vs 阈值 / 数据质量 / 抑制标记
 * - 点击展开：condition_result 子条件详情、evidence 证据、market_context 快照
 * - 过滤：时间范围（服务端）、规则（服务端）、severity（本页过滤）
 * - 待确认（TRIGGERED）事件可一键确认（POST /events/{id}/ack）
 */

import Link from "next/link";
import { useMemo, useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { QualityBadge } from "@/components/common/QualityBadge";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import type { QualityStatus } from "@/types/api";

import { EventStatusBadge, SeverityBadge5 } from "@/components/alerts/badges";
import { ConditionResultTree, ContextSnapshot, EvidenceDetailList } from "@/components/alerts/ResultDetails";
import { formatDateTime, formatNumber } from "@/components/alerts/format";
import { Banner, PageHeader, btnGhost, cardCls, selectCls } from "@/components/alerts/ui";
import { asAlert } from "@/components/alerts/types";
import type { AlertEventRecord, AlertRuleRecord, Paginated } from "@/components/alerts/types";

/* -------------------------------------------------------------------------- */
/* 常量与辅助                                                                   */
/* -------------------------------------------------------------------------- */

const TIME_RANGES = [
  { days: 7, label: "近 7 天" },
  { days: 30, label: "近 30 天" },
  { days: 90, label: "近 90 天" },
  { days: 0, label: "全部" },
] as const;

const PAGE_SIZE = 20;

const DOT_CLS: Record<string, string> = {
  TRIGGERED: "bg-orange shadow-[0_0_8px_rgba(219,109,40,0.6)]",
  ACKED: "bg-accent shadow-[0_0_8px_rgba(88,166,255,0.5)]",
  RESOLVED: "bg-up shadow-[0_0_8px_rgba(63,185,80,0.5)]",
  SUPPRESSED: "bg-muted",
};

function asNumber(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

function asString(v: unknown): string | null {
  return typeof v === "string" && v ? v : null;
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function AlertEventsPage() {
  const [rangeDays, setRangeDays] = useState(30);
  const [ruleFilter, setRuleFilter] = useState("");
  const [severityFilter, setSeverityFilter] = useState("");
  const [page, setPage] = useState(1);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [ackingId, setAckingId] = useState<string | null>(null);
  const [ackBanner, setAckBanner] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  /** 页面加载时刻（避免在 render 中调用 Date.now()，保证渲染纯度） */
  const [loadedAt] = useState(() => Date.now());

  const startIso =
    rangeDays > 0 ? new Date(loadedAt - rangeDays * 86_400_000).toISOString() : undefined;

  const rules = useApi<Paginated<AlertRuleRecord>>(
    () => asAlert<Paginated<AlertRuleRecord>>(apiClient.alerts.getRules({ page: 1, size: 100 })),
    [],
  );

  const events = useApi<Paginated<AlertEventRecord>>(
    () =>
      asAlert<Paginated<AlertEventRecord>>(
        apiClient.alerts.getEvents({
          page,
          size: PAGE_SIZE,
          ...(ruleFilter ? { rule_id: ruleFilter } : {}),
          ...(startIso ? { start: startIso } : {}),
        }),
      ),
    [page, ruleFilter, startIso],
  );

  const items = useMemo(() => events.data?.items ?? [], [events.data]);
  const filtered = useMemo(
    () => (severityFilter ? items.filter((ev) => ev.severity === severityFilter) : items),
    [items, severityFilter],
  );
  const total = events.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const handleAck = async (ev: AlertEventRecord) => {
    setAckingId(ev.id);
    setAckBanner(null);
    try {
      const res = await apiClient.alerts.ackEvent(ev.id);
      if (res.success) {
        setAckBanner({ tone: "success", text: "事件已确认" });
        events.refresh();
      } else {
        setAckBanner({ tone: "error", text: "确认失败，请稍后重试" });
      }
    } catch (e) {
      setAckBanner({ tone: "error", text: e instanceof Error ? e.message : "确认失败" });
    } finally {
      setAckingId(null);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader
        eyebrow="Trigger History"
        title="触发历史"
        description="所有预警触发的完整时间线：命中详情、证据链与触发时的市场快照，支持确认（ACK）操作。"
        actions={
          <Link href="/alerts" className={btnGhost}>
            ← 预警中心
          </Link>
        }
      />

      {/* 过滤器 */}
      <section className={`${cardCls} animate-fade-up flex flex-wrap items-center gap-x-5 gap-y-3 px-4 py-3`}>
        <label className="flex items-center gap-2 text-[11px] text-muted">
          时间范围
          <select
            className={`${selectCls} w-28`}
            value={rangeDays}
            onChange={(e) => {
              setRangeDays(Number(e.target.value));
              setPage(1);
            }}
          >
            {TIME_RANGES.map((r) => (
              <option key={r.days} value={r.days}>
                {r.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 text-[11px] text-muted">
          规则
          <select
            className={`${selectCls} w-48`}
            value={ruleFilter}
            onChange={(e) => {
              setRuleFilter(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部规则</option>
            {(rules.data?.items ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.rule_name}
              </option>
            ))}
          </select>
        </label>

        <div className="flex items-center gap-1.5 text-[11px] text-muted">
          等级（本页过滤）
          <div className="flex items-center gap-1">
            {["", "INFO", "WARNING", "HIGH", "CRITICAL"].map((s) => (
              <button
                key={s || "all"}
                type="button"
                onClick={() => setSeverityFilter(s)}
                aria-pressed={severityFilter === s}
                className={`rounded border px-1.5 py-0.5 text-[10px] transition-colors ${
                  severityFilter === s
                    ? "border-accent/60 bg-accent/10 text-accent"
                    : "border-line text-muted hover:text-[#e6edf3]"
                }`}
              >
                {s || "全部"}
              </button>
            ))}
          </div>
        </div>

        <span className="ml-auto font-mono text-[11px] tabular-nums text-muted">
          共 {total} 条
        </span>
      </section>

      {ackBanner && <Banner tone={ackBanner.tone}>{ackBanner.text}</Banner>}

      {events.error && (
        <ErrorState error={events.error} lastUpdatedAt={events.lastUpdatedAt} onRetry={events.refresh} />
      )}

      {/* 时间线 */}
      {events.loading && !events.data ? (
        <div className={cardCls}>
          <div className="p-5">
            <LoadingSkeleton variant="lines" rows={6} />
          </div>
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          title="暂无触发记录"
          description="该过滤条件下没有事件。调整时间范围，或创建新规则开始监控。"
          action={
            <Link href="/alerts/new" className={btnGhost}>
              ＋ 新增规则
            </Link>
          }
        />
      ) : (
        <section className="animate-fade-up relative flex flex-col gap-4 pl-7 before:absolute before:inset-y-1 before:left-[5px] before:w-px before:bg-gradient-to-b before:from-accent/40 before:via-line before:to-transparent">
          {filtered.map((ev, i) => {
            const ctx = asRecord(ev.market_context);
            const expanded = expandedId === ev.id;
            return (
              <article
                key={ev.id}
                className="animate-fade-up relative"
                style={{ animationDelay: `${Math.min(i * 40, 200)}ms` }}
              >
                <span
                  aria-hidden
                  className={`absolute -left-7 top-4 h-3 w-3 rounded-full border border-bg ${
                    DOT_CLS[ev.status ?? ""] ?? "bg-muted"
                  }`}
                />
                <div className={`${cardCls} overflow-hidden transition-colors hover:border-muted/40`}>
                  {/* 头部 */}
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-3">
                    <span className="font-mono text-[11px] tabular-nums text-muted">
                      {formatDateTime(ev.triggered_at, true)}
                    </span>
                    <span className="text-xs font-medium text-[#e6edf3]">
                      {ev.rule_name ?? ev.title ?? "未命名规则"}
                    </span>
                    <SeverityBadge5 severity={ev.severity} />
                    <EventStatusBadge status={ev.status} />
                    {ev.is_suppressed && (
                      <span
                        className="rounded border border-muted/40 bg-muted/10 px-1.5 py-0.5 text-[10px] text-muted"
                        title={ev.suppress_reason ?? undefined}
                      >
                        {ev.suppress_reason === "COOLDOWN" ? "冷却期抑制" : "已抑制"}
                      </span>
                    )}

                    <div className="ml-auto flex items-center gap-2">
                      {ev.data_quality && <QualityBadge status={ev.data_quality as QualityStatus} />}
                      {ev.status === "TRIGGERED" && (
                        <button
                          type="button"
                          className="rounded border border-accent/40 bg-accent/10 px-2 py-1 text-[11px] text-accent transition-colors hover:bg-accent/20 disabled:opacity-50"
                          onClick={() => void handleAck(ev)}
                          disabled={ackingId === ev.id}
                        >
                          {ackingId === ev.id ? "确认中…" : "✓ 确认"}
                        </button>
                      )}
                      <button
                        type="button"
                        aria-expanded={expanded}
                        className="rounded border border-line px-2 py-1 text-[11px] text-muted transition-colors hover:text-accent"
                        onClick={() => setExpandedId(expanded ? null : ev.id)}
                      >
                        {expanded ? "收起 ▲" : "详情 ▼"}
                      </button>
                    </div>
                  </div>

                  {/* 摘要行 */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line-subtle bg-bg-hover/20 px-4 py-2 text-[11px] text-muted">
                    {ev.trigger_value !== null && ev.trigger_value !== undefined && (
                      <span>
                        触发值
                        <span className="ml-1.5 font-mono tabular-nums text-accent">
                          {formatNumber(ev.trigger_value)}
                        </span>
                      </span>
                    )}
                    {ev.trigger_threshold !== null && ev.trigger_threshold !== undefined && (
                      <span>
                        阈值
                        <span className="ml-1.5 font-mono tabular-nums text-[#e6edf3]">
                          {formatNumber(ev.trigger_threshold)}
                        </span>
                      </span>
                    )}
                    {asString(ev.data_source) && <span>来源 {ev.data_source}</span>}
                    {asString(ev.description_cn) && (
                      <span className="min-w-0 flex-1 truncate text-[#e6edf3]/80">{ev.description_cn}</span>
                    )}
                  </div>

                  {/* 展开详情 */}
                  {expanded && (
                    <div className="animate-fade-up flex flex-col gap-4 border-t border-line px-4 py-4">
                      <section>
                        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">
                          子条件求值详情
                        </h3>
                        <ConditionResultTree details={ev.condition_result} />
                      </section>
                      <section>
                        <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">证据链</h3>
                        <EvidenceDetailList evidence={ev.evidence} />
                      </section>
                      {Object.keys(asRecord(ev.market_context)).length > 0 && (
                        <section>
                          <h3 className="mb-2 text-[11px] font-medium uppercase tracking-wider text-muted">
                            触发时市场快照
                          </h3>
                          <ContextSnapshot
                            snapshot={{
                              price: asNumber(ctx.price),
                              indicators: asRecord(ctx.indicators) as Record<string, number>,
                              stale_fields: Array.isArray(ctx.stale_fields)
                                ? (ctx.stale_fields as string[])
                                : null,
                              engine_states: asRecord(ctx.engine_states),
                              data_quality: asRecord(ctx.data_quality),
                            }}
                          />
                        </section>
                      )}
                      {ev.acked_at && (
                        <p className="text-[11px] text-muted">确认时间：{formatDateTime(ev.acked_at, true)}</p>
                      )}
                    </div>
                  )}
                </div>
              </article>
            );
          })}
        </section>
      )}

      {/* 分页 */}
      {total > 0 && (
        <div className="flex items-center justify-between text-[11px] text-muted">
          <button
            type="button"
            className={btnGhost}
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1 || events.loading}
          >
            ← 上一页
          </button>
          <span className="font-mono tabular-nums">
            第 {page} / {totalPages} 页
          </span>
          <button
            type="button"
            className={btnGhost}
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages || events.loading}
          >
            下一页 →
          </button>
        </div>
      )}
    </div>
  );
}
