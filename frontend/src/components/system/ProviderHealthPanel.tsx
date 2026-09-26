"use client";

/**
 * Provider 健康详情面板（表格行展开区）。
 *
 * 点击行展开后按需拉取 GET /providers/{name}/health，
 * 展示最近成功/失败时间、连续失败次数、24h 成功率、最近故障原因、
 * 评分细项与最近健康快照历史，以及「测试」按钮的实时结果。
 */

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import {
  DataTable,
  InlineNotice,
  KeyValue,
  ScoreBar,
  StatusDot,
  btnGhost,
  btnPrimary,
  formatDateTime,
  formatMs,
  formatPct,
  formatRelative,
  tdClass,
  tdMutedClass,
} from "./ui";
import type { ProviderHealthDetail, ProviderRow, ProviderTestResult } from "./types";

import type { ApiResponse } from "@/types/api";

interface ProviderHealthPanelProps {
  row: ProviderRow;
  /** 「测试」按钮实时结果（父组件持有） */
  testResult: ProviderTestResult | null;
  testError: string | null;
  testing: boolean;
  onTest: () => void;
}

export function ProviderHealthPanel({
  row,
  testResult,
  testError,
  testing,
  onTest,
}: ProviderHealthPanelProps) {
  const { data, loading, error, lastUpdatedAt, refresh } = useApi<ProviderHealthDetail>(
    () =>
      apiClient.providers.getHealth(row.name) as unknown as Promise<
        ApiResponse<ProviderHealthDetail>
      >,
    [row.name],
  );

  const detail = data ?? null;
  const health = detail?.provider?.latest_health ?? row.latest_health ?? null;
  const score = detail?.latest_score ?? null;

  return (
    <tr className="border-b border-line/60 bg-bg/60">
      <td colSpan={8} className="px-3 py-4 sm:px-6">
        <div className="animate-fade-up space-y-4">
          {/* 头部：标识 + 测试按钮 */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <span className="font-mono text-xs font-semibold text-accent">{row.name}</span>
              <StatusDot status={row.status} />
              {row.base_url && (
                <span className="hidden truncate font-mono text-[10px] text-muted sm:inline">
                  {row.base_url}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={onTest}
              disabled={testing}
              className={testing ? btnGhost : btnPrimary}
            >
              {testing ? "测试中…" : "测试连通性"}
            </button>
          </div>

          {/* 实时测试结果 */}
          {testing && <LoadingSkeleton variant="lines" rows={2} />}
          {!testing && testError && <InlineNotice tone="error">连通性测试失败：{testError}</InlineNotice>}
          {!testing && testResult && (
            <div className="rounded-lg border border-line bg-bg p-3">
              <p className="mb-2 text-[11px] font-medium text-muted">
                实时测试结果 · 刚刚执行
              </p>
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <KeyValue label="状态" value={<StatusDot status={testResult.status} />} />
                <KeyValue
                  label="健康分"
                  value={testResult.health_score !== null && testResult.health_score !== undefined
                    ? `${Number(testResult.health_score).toFixed(1)}`
                    : "—"}
                  mono
                />
                <KeyValue label="平均延迟" value={formatMs(testResult.avg_latency_ms)} mono />
                <KeyValue label="24h 成功率" value={formatPct(testResult.success_rate_24h)} mono />
              </dl>
            </div>
          )}

          {/* 健康详情主体 */}
          {loading && !detail ? (
            <LoadingSkeleton variant="table" rows={3} />
          ) : error && !detail ? (
            <ErrorState error={error} lastUpdatedAt={lastUpdatedAt} onRetry={refresh} />
          ) : (
            <div className="space-y-4">
              <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
                <KeyValue
                  label="最近成功"
                  value={formatRelative(row.last_success_at ?? health?.check_time)}
                  mono
                />
                <KeyValue label="最近失败" value={formatRelative(row.last_failure_at)} mono />
                <KeyValue
                  label="连续失败"
                  value={`${health?.consecutive_failures ?? row.consecutive_failures ?? 0} 次`}
                  mono
                  tone={
                    (health?.consecutive_failures ?? row.consecutive_failures ?? 0) > 0
                      ? "down"
                      : undefined
                  }
                />
                <KeyValue
                  label="24h 成功率"
                  value={formatPct(health?.success_rate_24h)}
                  mono
                />
                <KeyValue label="1h 成功率" value={formatPct(health?.success_rate_1h)} mono />
                <KeyValue label="平均延迟" value={formatMs(health?.response_time_ms)} mono />
                <KeyValue
                  label="今日请求"
                  value={`${fmtNum(health?.today_requests)} / 失败 ${fmtNum(health?.today_failures)}`}
                  mono
                />
                <KeyValue
                  label="限流状态"
                  value={health?.is_rate_limited ? "限流中" : "正常"}
                  tone={health?.is_rate_limited ? "warn" : undefined}
                />
                <KeyValue label="HTTP 状态" value={health?.http_status ?? "—"} mono />
                <KeyValue
                  label="当前使用"
                  value={row.is_enabled === false ? "已禁用" : "参与主备链"}
                  tone={row.is_enabled === false ? "down" : "up"}
                />
                <KeyValue label="优先级" value={row.priority ?? "—"} mono />
                <KeyValue label="快照时间" value={formatRelative(health?.check_time)} mono />
              </dl>

              {/* 最近故障原因 */}
              {health?.last_error && (
                <div className="rounded-lg border border-down/30 bg-down/[0.06] px-3 py-2">
                  <p className="text-[10px] font-medium uppercase tracking-wider text-down">
                    最近故障原因{health.last_error_type ? ` · ${health.last_error_type}` : ""}
                  </p>
                  <p className="mt-1 break-all font-mono text-[11px] leading-relaxed text-[#e6edf3]">
                    {health.last_error}
                  </p>
                </div>
              )}

              {/* 评分细项 */}
              {score && (
                <div className="rounded-lg border border-line bg-bg p-3">
                  <div className="mb-2.5 flex items-center justify-between">
                    <p className="text-[11px] font-medium text-muted">Provider 评分细项</p>
                    {typeof score.rank === "number" && (
                      <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[10px] text-muted">
                        排名 #{score.rank}
                      </span>
                    )}
                  </div>
                  <div className="mb-3 flex items-center gap-3">
                    <span className="w-14 shrink-0 text-[11px] text-muted">综合</span>
                    <ScoreBar value={numOrNull(score.overall_score)} className="flex-1" />
                    <span className="w-12 shrink-0 text-right font-mono text-xs font-semibold tabular-nums text-[#e6edf3]">
                      {numOrNull(score.overall_score) !== null
                        ? Number(score.overall_score).toFixed(1)
                        : "—"}
                    </span>
                  </div>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-5">
                    <ScoreRow label="准确性" value={score.accuracy_score} />
                    <ScoreRow label="延迟" value={score.latency_score} />
                    <ScoreRow label="稳定性" value={score.stability_score} />
                    <ScoreRow label="完整性" value={score.completeness_score} />
                    <ScoreRow label="一致性" value={score.consistency_score} />
                  </div>
                  {score.scored_at && (
                    <p className="mt-2 text-right text-[10px] text-muted">
                      评分时间：{formatDateTime(score.scored_at)}
                    </p>
                  )}
                </div>
              )}

              {/* 最近健康快照历史 */}
              {detail?.history && detail.history.length > 0 && (
                <div>
                  <p className="mb-1.5 text-[11px] font-medium text-muted">
                    最近健康快照（{detail.history.length} 条中的前 8 条）
                  </p>
                  <DataTable
                    columns={["检查时间", "状态", "响应耗时", "1h 成功率", "连续失败", "错误"]}
                    minWidth={640}
                  >
                    {detail.history.slice(0, 8).map((h, i) => (
                      <tr key={`${h.check_time ?? i}-${i}`} className="hover:bg-bg-hover">
                        <td className={`${tdClass} font-mono tabular-nums text-muted`}>
                          {formatDateTime(h.check_time)}
                        </td>
                        <td className={tdClass}>
                          <StatusDot status={h.status} />
                        </td>
                        <td className={tdMutedClass}>{formatMs(h.response_time_ms)}</td>
                        <td className={tdMutedClass}>{formatPct(h.success_rate_1h)}</td>
                        <td className={tdMutedClass}>{h.consecutive_failures ?? 0}</td>
                        <td className={`${tdClass} max-w-[280px] truncate text-muted`} title={h.last_error ?? undefined}>
                          {h.last_error ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </DataTable>
                </div>
              )}
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

/* -------------------------------------------------------------------------- */
/* 内部小组件                                                                  */
/* -------------------------------------------------------------------------- */

function ScoreRow({ label, value }: { label: string; value?: number | null }) {
  const num = numOrNull(value);
  return (
    <div className="flex items-center gap-2">
      <span className="w-12 shrink-0 text-[11px] text-muted">{label}</span>
      <ScoreBar value={num} className="flex-1" />
      <span className="w-10 shrink-0 text-right font-mono text-[11px] tabular-nums text-[#e6edf3]">
        {num !== null ? num.toFixed(0) : "—"}
      </span>
    </div>
  );
}

function numOrNull(v?: number | null): number | null {
  return v === null || v === undefined || Number.isNaN(v) ? null : Number(v);
}

function fmtNum(v?: number | null): string {
  return v === null || v === undefined ? "0" : String(v);
}
