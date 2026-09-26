"use client";

/**
 * 数据质量（Data Quality）。
 *
 * - 六大数据类别质量评分卡（0-100 综合完整度 + 状态灯 + QualityBadge）
 * - 数据覆盖范围：优先使用 GET /system/data-coverage；后端未实现时
 *   由最近 24h 质量检查记录推导（平均完整度 / 检查时间窗 / 缺口总数）
 * - 缺失时间段列表与数据冲突列表（含自动修复 / 需人工处理状态）
 * - 最近更新时间表（GET /system/stats：各核心表行数与最新 observation_time）
 *
 * API：GET /system/data-quality（轮询 60s）· GET /system/data-coverage · GET /system/stats。
 */

import { useMemo } from "react";

import { apiClient } from "@/lib/api";
import { useApi, usePolling } from "@/hooks/useApi";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { QualityBadge } from "@/components/common/QualityBadge";
import { SeverityBadge } from "@/components/common/SeverityBadge";

import { systemApi } from "@/components/system/api";
import {
  DataTable,
  InlineNotice,
  PageHeader,
  ScoreBar,
  SectionCard,
  StatChip,
  StatusDot,
  categoryLabel,
  formatDateTime,
  formatPct,
  formatRelative,
  fmtInt,
  tdClass,
  tdMutedClass,
} from "@/components/system/ui";
import type {
  DataCoverageCategory,
  DataCoverageData,
  DataQualityPayload,
  QualityRecordRow,
  SystemStatsData,
} from "@/components/system/types";

import type { ApiRequestError } from "@/lib/api";
import type { ApiResponse } from "@/types/api";
import type { QualityStatus, Severity } from "@/types/api";

const POLL_MS = 60_000;

/** data-coverage 条目里可能出现的质量枚举（用于 QualityBadge 的合法渲染） */
const QUALITY_STATUSES: readonly QualityStatus[] = [
  "VERIFIED",
  "ESTIMATED",
  "STALE",
  "CONFLICT",
  "INVALID",
];

/** 质量检查状态严重度排序（用于取「最差状态」） */
const WORST_RANK: Record<string, number> = { OK: 0, WARNING: 1, ERROR: 2, CRITICAL: 3 };

export default function QualityPage() {
  const qualityRes = usePolling<DataQualityPayload>(
    () => apiClient.system.getDataQuality() as unknown as Promise<ApiResponse<DataQualityPayload>>,
    POLL_MS,
  );
  const coverageRes = useApi<DataCoverageData>(() => systemApi.getDataCoverage());
  const statsRes = useApi<SystemStatsData>(
    () => apiClient.system.getStats() as unknown as Promise<ApiResponse<SystemStatsData>>,
  );

  const records = useMemo(
    () => (Array.isArray(qualityRes.data?.records) ? qualityRes.data!.records! : []),
    [qualityRes.data],
  );

  /* 类别聚合：评分（平均完整度）+ 最差状态 + 缺口/冲突/失败计数 */
  const summaries = useMemo(() => buildSummaries(records), [records]);

  const totalGaps = summaries.reduce((acc, s) => acc + s.gaps, 0);
  const totalConflicts = summaries.reduce((acc, s) => acc + s.conflicts, 0);
  const avgScore = useMemo(() => {
    const scored = summaries.filter((s) => s.score !== null);
    if (scored.length === 0) return null;
    return scored.reduce((acc, s) => acc + (s.score ?? 0), 0) / scored.length;
  }, [summaries]);

  const coverage = coverageRes.data ?? null;
  const coverageCategories = coverage?.categories ?? null;

  return (
    <div className="space-y-5">
      <PageHeader
        kicker="System · Data Quality"
        title="数据质量"
        description="数据可靠吗？各数据类别的验证比例、缺失与冲突、覆盖范围与最近更新时间 —— 全站数据状态灯的完整解释。"
      >
        <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-[11px] text-muted">
          轮询 60s · 更新 {formatRelative(qualityRes.lastUpdatedAt)}
        </span>
      </PageHeader>

      {/* 概览条 */}
      <div className="animate-fade-up flex flex-wrap items-center gap-2" style={{ animationDelay: "60ms" }}>
        <StatChip
          label="综合完整度"
          value={avgScore !== null ? `${avgScore.toFixed(1)}%` : "—"}
          tone={avgScore === null ? "default" : avgScore >= 95 ? "up" : avgScore >= 80 ? "warn" : "down"}
        />
        <StatChip label="检查记录" value={fmtInt(qualityRes.data?.count ?? records.length)} />
        <StatChip label="缺失缺口" value={fmtInt(totalGaps)} tone={totalGaps > 0 ? "warn" : "up"} />
        <StatChip
          label="数据冲突"
          value={fmtInt(totalConflicts)}
          tone={totalConflicts > 0 ? "down" : "up"}
        />
        <StatChip
          label="检查窗口起点"
          value={formatRelative(qualityRes.data?.since ?? null)}
        />
      </div>

      {qualityRes.error && records.length > 0 && (
        <ErrorState error={qualityRes.error} lastUpdatedAt={qualityRes.lastUpdatedAt} />
      )}

      {/* 1. 类别质量评分卡 */}
      <SectionCard
        title="数据类别质量评分"
        subtitle="按数据类别聚合最近 24h 质量检查：完整度评分 0-100 · 状态取最差检查结果"
      >
        {qualityRes.loading && records.length === 0 ? (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }, (_, i) => (
              <LoadingSkeleton key={i} variant="card" />
            ))}
          </div>
        ) : summaries.length === 0 ? (
          <EmptyState
            title="暂无质量检查记录"
            description={
              qualityRes.error?.message ??
              "最近 24h 内没有质量检查产出。质量引擎按计划任务运行，启动后此处会展示各数据类别评分。"
            }
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {summaries.map((s, i) => (
              <div
                key={s.key}
                className="animate-fade-up rounded-xl border border-line bg-bg p-4 transition-colors hover:border-accent/30"
                style={{ animationDelay: `${120 + i * 60}ms` }}
              >
                <div className="flex items-center justify-between">
                  <h3 className="text-xs font-semibold text-[#e6edf3]">
                    {categoryLabel(s.key)}
                    <span className="ml-1.5 font-mono text-[10px] font-normal text-muted">
                      {s.key}
                    </span>
                  </h3>
                  <StatusDot status={s.worst} />
                </div>
                <div className="mt-2.5 flex items-end justify-between">
                  <span
                    className={`font-mono text-2xl font-bold tabular-nums ${
                      s.score === null
                        ? "text-muted"
                        : s.score >= 95
                          ? "text-up"
                          : s.score >= 80
                            ? "text-warn"
                            : s.score >= 60
                              ? "text-orange"
                              : "text-down"
                    }`}
                  >
                    {s.score !== null ? s.score.toFixed(1) : "—"}
                  </span>
                  {s.verifiedBadge && <QualityBadge status={s.verifiedBadge} />}
                </div>
                <ScoreBar value={s.score} className="mt-2" />
                <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                  <MiniStat label="检查记录" value={fmtInt(s.recordCount)} />
                  <MiniStat label="检查样本" value={fmtInt(s.checked)} />
                  <MiniStat
                    label="缺失缺口"
                    value={fmtInt(s.gaps)}
                    tone={s.gaps > 0 ? "warn" : undefined}
                  />
                  <MiniStat
                    label="冲突"
                    value={fmtInt(s.conflicts)}
                    tone={s.conflicts > 0 ? "down" : undefined}
                  />
                </dl>
                <p className="mt-2.5 border-t border-line/60 pt-2 text-[10px] text-muted">
                  最近检查：{formatRelative(s.lastCheck)}（{formatDateTime(s.lastCheck)}）
                </p>
              </div>
            ))}
          </div>
        )}
      </SectionCard>

      {/* 2. 数据覆盖范围 */}
      <SectionCard
        title="数据覆盖范围"
        subtitle="各数据类别的最早 / 最新数据时间与覆盖率"
        right={
          coverageRes.error ? undefined : (
            <span className="text-[11px] text-muted">更新 {formatRelative(coverageRes.lastUpdatedAt)}</span>
          )
        }
      >
        {coverageRes.error ? (
          <div className="space-y-3">
            <InlineNotice tone="info">
              覆盖率明细端点暂未启用（GET /system/data-coverage 返回 {coverageRes.error.code}），
              以下覆盖指标由最近 24h 质量检查记录推导。
            </InlineNotice>
            {summaries.length === 0 ? (
              <EmptyState title="暂无可推导的覆盖数据" description="质量检查记录为空，无法估算覆盖率。" />
            ) : (
              <DataTable
                columns={["数据类别", "检查记录", "检查样本", "平均完整度", "最早检查", "最新检查", "缺口"]}
                minWidth={820}
              >
                {summaries.map((s) => (
                  <tr key={s.key} className="hover:bg-bg-hover">
                    <td className={tdClass}>{categoryLabel(s.key)}</td>
                    <td className={tdMutedClass}>{fmtInt(s.recordCount)}</td>
                    <td className={tdMutedClass}>{fmtInt(s.checked)}</td>
                    <td className={tdClass}>
                      <div className="flex w-36 items-center gap-2">
                        <ScoreBar value={s.score} className="flex-1" />
                        <span className="w-14 shrink-0 text-right font-mono text-[11px] tabular-nums">
                          {s.score !== null ? `${s.score.toFixed(1)}%` : "—"}
                        </span>
                      </div>
                    </td>
                    <td className={tdMutedClass}>{formatDateTime(s.firstCheck)}</td>
                    <td className={tdMutedClass}>{formatDateTime(s.lastCheck)}</td>
                    <td className={tdMutedClass}>{fmtInt(s.gaps)}</td>
                  </tr>
                ))}
              </DataTable>
            )}
          </div>
        ) : coverageRes.loading && !coverage ? (
          <LoadingSkeleton variant="table" rows={4} />
        ) : coverageCategories && Object.keys(coverageCategories).length > 0 ? (
          <DataTable
            columns={["数据类别", "最早数据", "最新数据", "覆盖率", "质量", "数据量"]}
            minWidth={760}
          >
            {Object.entries(coverageCategories).map(([key, item]) => (
              <CoverageRow key={key} code={key} item={item} />
            ))}
          </DataTable>
        ) : (
          <EmptyState
            title="覆盖率明细暂未返回"
            description="端点可用但未返回类别数据，请稍后重试或检查后端 data-coverage 任务。"
          />
        )}
      </SectionCard>

      {/* 3. 缺失时间段 */}
      <MissingList records={records} loading={qualityRes.loading && records.length === 0} />

      {/* 4. 数据冲突 */}
      <ConflictList records={records} loading={qualityRes.loading && records.length === 0} />

      {/* 5. 最近更新时间表 */}
      <StatsTable
        stats={statsRes.data}
        loading={statsRes.loading && !statsRes.data}
        error={statsRes.error}
        lastUpdatedAt={statsRes.lastUpdatedAt}
        onRetry={statsRes.refresh}
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 类别聚合                                                                    */
/* -------------------------------------------------------------------------- */

interface CategorySummary {
  key: string;
  score: number | null;
  worst: string | null;
  recordCount: number;
  checked: number;
  gaps: number;
  conflicts: number;
  failed: number;
  firstCheck: string | null;
  lastCheck: string | null;
  /** data-coverage / 文档化结构下可用的质量徽标（防御性可选） */
  verifiedBadge: QualityStatus | null;
}

function buildSummaries(records: QualityRecordRow[]): CategorySummary[] {
  const map = new Map<string, CategorySummary & { scores: number[] }>();
  for (const r of records) {
    const key = (r.data_category ?? "").toUpperCase() || "OTHER";
    let entry = map.get(key);
    if (!entry) {
      entry = {
        key,
        score: null,
        worst: null,
        recordCount: 0,
        checked: 0,
        gaps: 0,
        conflicts: 0,
        failed: 0,
        firstCheck: null,
        lastCheck: null,
        verifiedBadge: null,
        scores: [],
      };
      map.set(key, entry);
    }
    entry.recordCount += 1;
    entry.checked += r.records_checked ?? 0;
    entry.failed += r.records_failed ?? 0;
    entry.gaps += Array.isArray(r.gaps_found) ? r.gaps_found.length : 0;
    entry.conflicts += Array.isArray(r.conflicts_found) ? r.conflicts_found.length : 0;
    if (typeof r.completeness_pct === "number" && !Number.isNaN(r.completeness_pct)) {
      entry.scores.push(r.completeness_pct);
    }
    const t = r.check_time ?? null;
    if (t) {
      if (entry.firstCheck === null || t < entry.firstCheck) entry.firstCheck = t;
      if (entry.lastCheck === null || t > entry.lastCheck) entry.lastCheck = t;
    }
    const st = (r.status ?? "").toUpperCase();
    if (st) {
      entry.worst =
        entry.worst === null
          ? st
          : (WORST_RANK[st] ?? 0) > (WORST_RANK[entry.worst] ?? 0)
            ? st
            : entry.worst;
    }
  }
  return Array.from(map.values())
    .map(({ scores, ...rest }) => ({
      ...rest,
      score: scores.length > 0 ? scores.reduce((a, b) => a + b, 0) / scores.length : null,
    }))
    .sort((a, b) => (a.key < b.key ? -1 : 1));
}

function MiniStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "warn" | "down";
}) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-muted">{label}</dt>
      <dd
        className={`font-mono tabular-nums ${
          tone === "warn" ? "text-warn" : tone === "down" ? "text-down" : "text-[#e6edf3]"
        }`}
      >
        {value}
      </dd>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* data-coverage 行                                                            */
/* -------------------------------------------------------------------------- */

function CoverageRow({ code, item }: { code: string; item: DataCoverageCategory }) {
  const quality =
    typeof item.quality_status === "string" &&
    QUALITY_STATUSES.includes(item.quality_status as QualityStatus)
      ? (item.quality_status as QualityStatus)
      : null;
  const coverage = typeof item.coverage_pct === "number" ? item.coverage_pct : null;
  return (
    <tr className="hover:bg-bg-hover">
      <td className={tdClass}>
        {categoryLabel(code)}
        <span className="ml-1.5 font-mono text-[10px] text-muted">{code}</span>
      </td>
      <td className={tdMutedClass}>{formatDateTime(str(item.earliest))}</td>
      <td className={tdMutedClass}>{formatDateTime(str(item.latest))}</td>
      <td className={tdClass}>
        <div className="flex w-32 items-center gap-2">
          <ScoreBar value={coverage} className="flex-1" />
          <span className="w-14 shrink-0 text-right font-mono text-[11px] tabular-nums">
            {formatPct(coverage)}
          </span>
        </div>
      </td>
      <td className={tdClass}>{quality ? <QualityBadge status={quality} raw /> : "—"}</td>
      <td className={tdMutedClass}>{item.rows !== undefined && item.rows !== null ? fmtInt(item.rows) : "—"}</td>
    </tr>
  );
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

/* -------------------------------------------------------------------------- */
/* 缺失时间段列表                                                              */
/* -------------------------------------------------------------------------- */

function MissingList({ records, loading }: { records: QualityRecordRow[]; loading: boolean }) {
  const entries = useMemo(() => {
    const out: { time: string | null; category: string; text: string }[] = [];
    for (const r of records) {
      if (!Array.isArray(r.gaps_found)) continue;
      for (const g of r.gaps_found) {
        out.push({
          time: r.check_time ?? null,
          category: (r.data_category ?? "").toUpperCase(),
          text: detailText(g),
        });
        if (out.length >= 30) return out;
      }
    }
    return out;
  }, [records]);

  return (
    <SectionCard title="缺失时间段" subtitle="最近质量检查发现的缺口（最多展示 30 条）">
      {loading ? (
        <LoadingSkeleton variant="table" rows={3} />
      ) : entries.length === 0 ? (
        <EmptyState
          title="未发现缺失时间段"
          description="最近 24h 的质量检查没有记录到数据缺口。缺口由质量引擎的连续性检查自动发现。"
        />
      ) : (
        <DataTable columns={["发现时间", "数据类别", "缺口描述"]} minWidth={680}>
          {entries.map((e, i) => (
            <tr key={`${e.category}-${e.time ?? i}-${i}`} className="hover:bg-bg-hover">
              <td className={`${tdClass} font-mono tabular-nums text-muted`}>
                {formatDateTime(e.time)}
              </td>
              <td className={tdClass}>{categoryLabel(e.category)}</td>
              <td className={`${tdClass} max-w-[520px] break-all font-mono text-[11px] text-muted`}>
                {e.text}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* 冲突列表                                                                    */
/* -------------------------------------------------------------------------- */

function ConflictList({ records, loading }: { records: QualityRecordRow[]; loading: boolean }) {
  const entries = useMemo(() => {
    const out: {
      time: string | null;
      category: string;
      text: string;
      autoFixed: boolean;
      requiresManual: boolean;
      severity: Severity | null;
    }[] = [];
    for (const r of records) {
      if (!Array.isArray(r.conflicts_found)) continue;
      for (const c of r.conflicts_found) {
        out.push({
          time: r.check_time ?? null,
          category: (r.data_category ?? "").toUpperCase(),
          text: detailText(c),
          autoFixed: r.auto_fixed === true,
          requiresManual: r.requires_manual === true,
          severity:
            r.severity === "INFO" || r.severity === "WARNING" || r.severity === "HIGH" || r.severity === "CRITICAL"
              ? r.severity
              : null,
        });
        if (out.length >= 30) return out;
      }
    }
    return out;
  }, [records]);

  return (
    <SectionCard title="数据冲突" subtitle="多源数值偏差超阈值的记录与处理状态（最多展示 30 条）">
      {loading ? (
        <LoadingSkeleton variant="table" rows={3} />
      ) : entries.length === 0 ? (
        <EmptyState
          title="未发现数据冲突"
          description="最近 24h 的质量检查没有记录到多源冲突。交叉验证通过时不会产生冲突记录。"
        />
      ) : (
        <DataTable columns={["发现时间", "数据类别", "冲突详情", "等级", "处理状态"]} minWidth={760}>
          {entries.map((e, i) => (
            <tr key={`${e.category}-${e.time ?? i}-${i}`} className="hover:bg-bg-hover">
              <td className={`${tdClass} font-mono tabular-nums text-muted`}>
                {formatDateTime(e.time)}
              </td>
              <td className={tdClass}>{categoryLabel(e.category)}</td>
              <td className={`${tdClass} max-w-[420px] break-all font-mono text-[11px] text-muted`}>
                {e.text}
              </td>
              <td className={tdClass}>
                <SeverityBadge severity={e.severity} />
                {!e.severity && <span className="text-muted">—</span>}
              </td>
              <td className={tdClass}>
                {e.autoFixed ? (
                  <span className="rounded border border-up/40 bg-up/10 px-1.5 py-0.5 text-[10px] text-up">
                    已自动处理
                  </span>
                ) : e.requiresManual ? (
                  <span className="rounded border border-down/40 bg-down/10 px-1.5 py-0.5 text-[10px] text-down">
                    需人工处理
                  </span>
                ) : (
                  <span className="rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn">
                    待处理
                  </span>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* 最近更新时间表（system/stats）                                               */
/* -------------------------------------------------------------------------- */

const TABLE_LABELS: Record<string, string> = {
  candles: "K 线（candles）",
  market_prices: "实时价格（market_prices）",
  onchain_metrics: "链上指标（onchain_metrics）",
  etf_flows: "ETF 资金流（etf_flows）",
  derivatives: "衍生品（derivatives）",
  macro_series: "宏观序列（macro_series）",
  sentiment: "情绪（sentiment）",
  indicator_values: "技术指标值（indicator_values）",
};

function StatsTable({
  stats,
  loading,
  error,
  lastUpdatedAt,
  onRetry,
}: {
  stats: SystemStatsData | null;
  loading: boolean;
  error: ApiRequestError | null;
  lastUpdatedAt: number | null;
  onRetry: () => void;
}) {
  const tables = stats?.tables ?? {};
  const rows = Object.entries(tables);
  return (
    <SectionCard
      title="最近更新时间表"
      subtitle="各核心数据表行数与最新数据时间"
      right={
        <div className="flex items-center gap-2">
          {typeof stats?.provider_count === "number" && (
            <StatChip label="Provider" value={stats.provider_count} />
          )}
          <span className="text-[11px] text-muted">更新 {formatRelative(lastUpdatedAt)}</span>
        </div>
      }
    >
      {error && !stats ? (
        <ErrorState error={error} lastUpdatedAt={lastUpdatedAt} onRetry={onRetry} />
      ) : loading ? (
        <LoadingSkeleton variant="table" rows={5} />
      ) : rows.length === 0 ? (
        <EmptyState title="暂无统计" description="系统统计端点未返回表数据。" />
      ) : (
        <DataTable columns={["数据表", "行数", "最近数据时间", "相对时间"]} minWidth={640}>
          {rows.map(([name, entry]) => (
            <tr key={name} className="hover:bg-bg-hover">
              <td className={tdClass}>{TABLE_LABELS[name] ?? name}</td>
              <td className={tdMutedClass}>{entry?.error ? "查询失败" : fmtInt(entry?.rows)}</td>
              <td className={tdMutedClass}>{formatDateTime(entry?.latest_observation_time)}</td>
              <td className={tdMutedClass}>{formatRelative(entry?.latest_observation_time)}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */

function detailText(item: unknown): string {
  if (typeof item === "string") return item;
  if (item && typeof item === "object") {
    try {
      return JSON.stringify(item);
    } catch {
      return String(item);
    }
  }
  return String(item);
}
