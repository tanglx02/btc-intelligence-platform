"use client";

/**
 * 数据源中心（Provider Health Center）。
 *
 * - Provider 按数据类别分节展示：状态灯 / 健康评分 / 平均延迟 / 成功率 / 优先级 / 锁定
 * - 行展开健康详情（最近成功失败、连续失败、24h 成功率、故障原因、评分细项、快照历史）
 * - 「测试」按钮：POST /providers/{name}/test 实时连通性探测
 * - 配置后台化：「配置」编辑模态框（凭据掩码/优先级/锁定/限流，PUT 保存即热重载）、
 *   列表直接切换启用开关、「从 YAML 导入」同步配置、「新增数据源」引导
 * - Failover 事件流（最近 20 条）与 Provider 评分排名
 *
 * API：GET /providers · PUT /providers/{name} · POST /providers/{name}/test|reload ·
 * POST /providers/sync-from-yaml · GET /providers/failover-events|scores；轮询 30s。
 */

import { useMemo, useState } from "react";

import { apiClient } from "@/lib/api";
import { usePolling } from "@/hooks/useApi";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { ToastHost, useToast } from "@/components/portfolio/toast";
import { ProviderEditModal } from "@/components/system/ProviderEditModal";
import { ProviderHealthPanel } from "@/components/system/ProviderHealthPanel";
import { ProviderImportDialog } from "@/components/system/ProviderImportDialog";
import {
  CATEGORY_ORDER,
  DataTable,
  PageHeader,
  ScoreBar,
  SectionCard,
  StatChip,
  StatusDot,
  ToggleSwitch,
  btnGhost,
  btnPrimary,
  categoryLabel,
  formatDateTime,
  formatMs,
  formatPct,
  formatRelative,
  rowClass,
  tdClass,
  tdMutedClass,
} from "@/components/system/ui";
import type {
  FailoverEventRow,
  ProviderRow,
  ProviderScoreRow,
  ProviderTestResult,
} from "@/components/system/types";

import type { ApiResponse } from "@/types/api";

const POLL_MS = 30_000;

export default function ProvidersPage() {
  const providersRes = usePolling<ProviderRow[]>(
    () => apiClient.providers.getList() as unknown as Promise<ApiResponse<ProviderRow[]>>,
    POLL_MS,
  );
  const eventsRes = usePolling<FailoverEventRow[]>(
    () =>
      apiClient.providers.getFailoverEvents() as unknown as Promise<
        ApiResponse<FailoverEventRow[]>
      >,
    POLL_MS,
  );
  const scoresRes = usePolling<ProviderScoreRow[]>(
    () => apiClient.providers.getScores() as unknown as Promise<ApiResponse<ProviderScoreRow[]>>,
    POLL_MS,
  );

  const providers = useMemo(() => providersRes.data ?? [], [providersRes.data]);

  /* 展开行与测试状态（单行展开 + 单 Provider 测试结果） */
  const [expanded, setExpanded] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  /* 配置后台化：编辑模态框 / 导入对话框 / 启用开关切换中 / toast */
  const { toast, show } = useToast();
  const [editing, setEditing] = useState<ProviderRow | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [importDialog, setImportDialog] = useState<{ open: boolean; mode: "create" | "import" }>({
    open: false,
    mode: "import",
  });
  const [toggling, setToggling] = useState<string | null>(null);

  function openConfig(row: ProviderRow) {
    setEditing(row);
    setEditOpen(true);
  }

  /** 列表直接切换启用/禁用（PUT /providers/{name}，保存即生效） */
  async function toggleEnabled(row: ProviderRow, next: boolean) {
    setToggling(row.name);
    try {
      const res = await apiClient.providers.updateProvider(row.name, { is_enabled: next });
      if (res.success === false) throw new Error(res.error?.message ?? "操作失败");
      show("success", `${row.name} 已${next ? "启用" : "禁用"}，配置即时生效`);
      providersRes.refresh();
    } catch (err) {
      show("error", err instanceof Error ? err.message : "操作失败");
    } finally {
      setToggling(null);
    }
  }

  async function runTest(name: string) {
    setExpanded(name);
    setTesting(name);
    setTestError(null);
    setTestResult(null);
    try {
      const res = await apiClient.providers.testProvider(name);
      if (res.success === false || res.data === null) {
        setTestError(res.error?.message ?? "测试返回异常");
      } else {
        setTestResult(res.data as ProviderTestResult);
      }
    } catch (err) {
      setTestError(err instanceof Error ? err.message : "测试请求失败");
    } finally {
      setTesting(null);
    }
  }

  /* 概览统计 */
  const summary = useMemo(() => {
    let online = 0;
    let warning = 0;
    let down = 0;
    let disabled = 0;
    let scoreSum = 0;
    let scoreCount = 0;
    for (const p of providers) {
      const s = (p.status ?? "").toUpperCase();
      if (s === "ONLINE") online += 1;
      else if (["DEGRADED", "SLOW", "RATE_LIMITED"].includes(s)) warning += 1;
      else if (["OFFLINE", "AUTH_ERROR", "NETWORK_ERROR", "DATA_ERROR"].includes(s)) down += 1;
      else if (s === "DISABLED" || p.is_enabled === false) disabled += 1;
      if (typeof p.health_score === "number") {
        scoreSum += p.health_score;
        scoreCount += 1;
      }
    }
    return {
      total: providers.length,
      online,
      warning,
      down,
      disabled,
      avgScore: scoreCount > 0 ? scoreSum / scoreCount : null,
    };
  }, [providers]);

  /* 类别分组 */
  const groups = useMemo(() => {
    const map = new Map<string, ProviderRow[]>();
    for (const p of providers) {
      const key = (p.category ?? "").toUpperCase() || "OTHER";
      const list = map.get(key) ?? [];
      list.push(p);
      map.set(key, list);
    }
    const ordered: [string, ProviderRow[]][] = [];
    for (const key of CATEGORY_ORDER) {
      const list = map.get(key);
      if (list) ordered.push([key, list]);
    }
    for (const [key, list] of map) {
      if (!CATEGORY_ORDER.includes(key as (typeof CATEGORY_ORDER)[number])) {
        ordered.push([key, list]);
      }
    }
    return ordered;
  }, [providers]);

  const loading = providersRes.loading && providers.length === 0;

  return (
    <div className="space-y-5">
      <PageHeader
        kicker="System · Provider Center"
        title="数据源中心"
        description="平台数据从哪里来、当前由哪个源提供服务、主备切换与健康状况 —— 所有原始数据的来路在这里一目了然。配置修改保存后立即生效，无需重启。"
      >
        <button
          type="button"
          onClick={() => setImportDialog({ open: true, mode: "create" })}
          className={btnGhost}
          title="界面仅支持配置已实现的数据源；新数据源需从 YAML 导入"
        >
          新增数据源
        </button>
        <button
          type="button"
          onClick={() => setImportDialog({ open: true, mode: "import" })}
          className={btnGhost}
        >
          从 YAML 导入
        </button>
        <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-[11px] text-muted">
          轮询 30s · 更新 {formatRelative(providersRes.lastUpdatedAt)}
        </span>
      </PageHeader>

      {/* 概览条 */}
      <div className="animate-fade-up flex flex-wrap items-center gap-2" style={{ animationDelay: "60ms" }}>
        <StatChip label="Provider 总数" value={summary.total} />
        <StatChip label="在线" value={summary.online} tone="up" />
        <StatChip label="降级/缓慢" value={summary.warning} tone="warn" />
        <StatChip label="异常/离线" value={summary.down} tone="down" />
        <StatChip label="已禁用" value={summary.disabled} />
        <StatChip
          label="平均健康分"
          value={summary.avgScore !== null ? summary.avgScore.toFixed(1) : "—"}
        />
      </div>

      {providersRes.error && providers.length > 0 && (
        <ErrorState error={providersRes.error} lastUpdatedAt={providersRes.lastUpdatedAt} />
      )}

      {/* 分节 Provider 表 */}
      {loading ? (
        <div className="space-y-4">
          {CATEGORY_ORDER.slice(0, 3).map((c) => (
            <SectionCard key={c} title={categoryLabel(c)}>
              <LoadingSkeleton variant="table" rows={3} />
            </SectionCard>
          ))}
        </div>
      ) : groups.length === 0 ? (
        <EmptyState
          title="暂无 Provider 数据"
          description={
            providersRes.error?.message ??
            "后端未返回任何数据源，请确认 Provider 服务已启动并完成注册。"
          }
        />
      ) : (
        groups.map(([key, list]) => (
          <SectionCard
            key={key}
            title={categoryLabel(key)}
            subtitle={`主备优先级链 · 共 ${list.length} 个数据源`}
          >
            <DataTable
              columns={[
                "Provider",
                "状态",
                "健康评分",
                "平均延迟",
                "24h 成功率",
                "优先级",
                "锁定/限流",
                "启用",
                "操作",
              ]}
              minWidth={1000}
            >
              {list.map((p) => {
                  const isExpanded = expanded === p.name;
                  const rateLimited = p.latest_health?.is_rate_limited === true;
                  const disabled = p.is_enabled === false;
                  return (
                    <ProviderRowGroup
                      key={p.id || p.name}
                      row={p}
                      isExpanded={isExpanded}
                      rateLimited={rateLimited}
                      disabled={disabled}
                      toggling={toggling === p.name}
                      testing={testing === p.name}
                      testResult={expanded === p.name ? testResult : null}
                      testError={expanded === p.name ? testError : null}
                      onToggle={() => setExpanded(isExpanded ? null : p.name)}
                      onTest={() => void runTest(p.name)}
                      onConfig={() => openConfig(p)}
                      onToggleEnabled={(next) => void toggleEnabled(p, next)}
                    />
                  );
                })}
              </DataTable>
          </SectionCard>
        ))
      )}

      {/* Failover 事件流 */}
      <FailoverEvents
        events={eventsRes.data ?? []}
        loading={eventsRes.loading && (eventsRes.data ?? []).length === 0}
        error={eventsRes.error}
        lastUpdatedAt={eventsRes.lastUpdatedAt}
        onRetry={eventsRes.refresh}
      />

      {/* 评分排名 */}
      <ScoreBoard
        scores={scoresRes.data ?? []}
        loading={scoresRes.loading && (scoresRes.data ?? []).length === 0}
        error={scoresRes.error}
        lastUpdatedAt={scoresRes.lastUpdatedAt}
        onRetry={scoresRes.refresh}
      />

      {/* 配置编辑模态框（保存即热重载生效） */}
      <ProviderEditModal
        open={editOpen}
        provider={editing}
        onClose={() => setEditOpen(false)}
        onSaved={(msg) => {
          show("success", msg);
          providersRes.refresh();
        }}
      />

      {/* 新增数据源引导 / 从 YAML 导入确认框 */}
      <ProviderImportDialog
        open={importDialog.open}
        mode={importDialog.mode}
        onClose={() => setImportDialog((prev) => ({ ...prev, open: false }))}
        onImported={(imported, overwrite) => {
          show("success", `导入完成：${imported} 个数据源已同步（${overwrite ? "覆盖模式" : "仅新增"}）`);
          providersRes.refresh();
        }}
      />

      <ToastHost toast={toast} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 单行 + 展开详情                                                             */
/* -------------------------------------------------------------------------- */

function ProviderRowGroup({
  row,
  isExpanded,
  rateLimited,
  disabled,
  toggling,
  testing,
  testResult,
  testError,
  onToggle,
  onTest,
  onConfig,
  onToggleEnabled,
}: {
  row: ProviderRow;
  isExpanded: boolean;
  rateLimited: boolean;
  disabled: boolean;
  toggling: boolean;
  testing: boolean;
  testResult: ProviderTestResult | null;
  testError: string | null;
  onToggle: () => void;
  onTest: () => void;
  onConfig: () => void;
  onToggleEnabled: (next: boolean) => void;
}) {
  const score = typeof row.health_score === "number" ? row.health_score : null;
  return (
    <>
      <tr className={rowClass} onClick={onToggle} aria-expanded={isExpanded}>
        <td className={tdClass}>
          <div className="flex items-center gap-2">
            <span
              aria-hidden
              className={`text-muted transition-transform ${isExpanded ? "rotate-90" : ""}`}
            >
              ▸
            </span>
            <div className="min-w-0">
              <p className="font-mono text-xs font-semibold text-[#e6edf3]">{row.name}</p>
              {row.description && (
                <p className="max-w-[220px] truncate text-[10px] text-muted" title={row.description}>
                  {row.description}
                </p>
              )}
            </div>
          </div>
        </td>
        <td className={tdClass}>
          <StatusDot status={row.status} />
        </td>
        <td className={tdClass}>
          <div className="flex w-32 items-center gap-2">
            <ScoreBar value={score} className="flex-1" />
            <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums text-[#e6edf3]">
              {score !== null ? score.toFixed(0) : "—"}
            </span>
          </div>
        </td>
        <td className={tdMutedClass}>{formatMs(row.latest_health?.response_time_ms)}</td>
        <td className={tdMutedClass}>{formatPct(row.latest_health?.success_rate_24h)}</td>
        <td className={tdMutedClass}>{row.priority ?? "—"}</td>
        <td className={tdClass}>
          <div className="flex items-center gap-1.5">
            {row.config?.is_locked === true && (
              <span
                className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[10px] text-accent"
                title="优先级已锁定，不参与健康评分自动调整"
              >
                锁定
              </span>
            )}
            {rateLimited && (
              <span className="rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn">
                限流中
              </span>
            )}
            {row.config?.is_locked !== true && !rateLimited && (
              <span className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">正常</span>
            )}
          </div>
        </td>
        <td className={tdClass} onClick={(e) => e.stopPropagation()}>
          <ToggleSwitch
            checked={!disabled}
            disabled={toggling}
            title={disabled ? "启用该数据源（立即生效）" : "禁用该数据源（立即生效）"}
            onChange={onToggleEnabled}
          />
        </td>
        <td className={tdClass}>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onTest();
              }}
              disabled={testing}
              className={testing ? btnGhost : btnPrimary}
            >
              {testing ? "测试中…" : "测试"}
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onConfig();
              }}
              className={btnGhost}
              title="编辑连接 / 凭据 / 限流 / 优先级配置"
            >
              配置
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggle();
              }}
              className={btnGhost}
            >
              {isExpanded ? "收起" : "详情"}
            </button>
          </div>
        </td>
      </tr>
      {isExpanded && (
        <ProviderHealthPanel
          row={row}
          testResult={testResult}
          testError={testError}
          testing={testing}
          onTest={onTest}
        />
      )}
    </>
  );
}

/* -------------------------------------------------------------------------- */
/* Failover 事件流                                                             */
/* -------------------------------------------------------------------------- */

function FailoverEvents({
  events,
  loading,
  error,
  lastUpdatedAt,
  onRetry,
}: {
  events: FailoverEventRow[];
  loading: boolean;
  error: ProvidersPageError;
  lastUpdatedAt: number | null;
  onRetry: () => void;
}) {
  const recent = events.slice(0, 20);
  return (
    <SectionCard
      title="故障切换事件"
      subtitle="最近 20 条 · 原主源 → 新源与触发原因"
      right={
        <span className="text-[11px] text-muted">更新 {formatRelative(lastUpdatedAt)}</span>
      }
    >
      {error && events.length === 0 ? (
        <ErrorState error={error} lastUpdatedAt={lastUpdatedAt} onRetry={onRetry} />
      ) : loading ? (
        <LoadingSkeleton variant="table" rows={4} />
      ) : recent.length === 0 ? (
        <EmptyState
          title="近期无故障切换"
          description="最近 30 天内没有记录到 Provider 主备切换事件，数据链路稳定。"
        />
      ) : (
        <DataTable
          columns={["时间", "数据类别", "切换", "触发原因", "状态", "持续"]}
          minWidth={820}
        >
          {recent.map((ev, i) => (
            <tr key={ev.id ?? i} className="hover:bg-bg-hover">
              <td className={`${tdClass} font-mono tabular-nums text-muted`}>
                <span title={formatDateTime(ev.occurred_at)}>{formatRelative(ev.occurred_at)}</span>
              </td>
              <td className={tdClass}>{categoryLabel(ev.data_category)}</td>
              <td className={tdClass}>
                <span className="inline-flex items-center gap-1.5 font-mono text-[11px]">
                  <span className="text-down">{ev.from_provider ?? "—"}</span>
                  <span aria-hidden className="text-muted">
                    →
                  </span>
                  <span className="text-up">{ev.to_provider ?? "—"}</span>
                </span>
              </td>
              <td className={`${tdClass} max-w-[320px] truncate text-muted`} title={ev.error_message ?? ev.trigger_reason ?? undefined}>
                {ev.trigger_reason ?? "—"}
                {ev.symbol ? ` · ${ev.symbol}` : ""}
              </td>
              <td className={tdClass}>
                {ev.resolved_at ? (
                  <span className="rounded border border-up/40 bg-up/10 px-1.5 py-0.5 text-[10px] text-up">
                    已恢复
                  </span>
                ) : (
                  <span className="rounded border border-warn/40 bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn">
                    切换中
                  </span>
                )}
              </td>
              <td className={tdMutedClass}>
                {ev.duration_seconds !== null && ev.duration_seconds !== undefined
                  ? `${Math.round(ev.duration_seconds)}s`
                  : "—"}
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */
/* 评分排名                                                                    */
/* -------------------------------------------------------------------------- */

function ScoreBoard({
  scores,
  loading,
  error,
  lastUpdatedAt,
  onRetry,
}: {
  scores: ProviderScoreRow[];
  loading: boolean;
  error: ProvidersPageError;
  lastUpdatedAt: number | null;
  onRetry: () => void;
}) {
  const top = scores.slice(0, 10);
  return (
    <SectionCard
      title="Provider 评分排名"
      subtitle="按综合评分降序 · 稳定性 / 延迟 / 完整性 / 一致性加权"
      right={<span className="text-[11px] text-muted">更新 {formatRelative(lastUpdatedAt)}</span>}
    >
      {error && scores.length === 0 ? (
        <ErrorState error={error} lastUpdatedAt={lastUpdatedAt} onRetry={onRetry} />
      ) : loading ? (
        <LoadingSkeleton variant="table" rows={4} />
      ) : top.length === 0 ? (
        <EmptyState
          title="暂无评分数据"
          description="评分任务尚未生成结果，Provider 需要积累一段请求记录后才会参与排名。"
        />
      ) : (
        <DataTable
          columns={["排名", "Provider", "类别", "综合评分", "稳定性", "延迟", "一致性", "评分时间"]}
          minWidth={820}
        >
          {top.map((s, i) => (
            <tr key={`${s.provider ?? i}-${i}`} className="hover:bg-bg-hover">
              <td className={`${tdClass} font-mono tabular-nums`}>
                <span className={i < 3 ? "font-semibold text-accent" : "text-muted"}>
                  #{s.rank ?? i + 1}
                </span>
              </td>
              <td className={`${tdClass} font-mono text-xs font-semibold`}>{s.provider ?? "—"}</td>
              <td className={tdClass}>{categoryLabel(s.category)}</td>
              <td className={tdClass}>
                <div className="flex w-32 items-center gap-2">
                  <ScoreBar value={numOrNull(s.overall_score)} className="flex-1" />
                  <span className="w-9 shrink-0 text-right font-mono text-[11px] tabular-nums">
                    {numOrNull(s.overall_score) !== null
                      ? Number(s.overall_score).toFixed(0)
                      : "—"}
                  </span>
                </div>
              </td>
              <td className={tdMutedClass}>{fmtScore(s.stability_score)}</td>
              <td className={tdMutedClass}>{fmtScore(s.latency_score)}</td>
              <td className={tdMutedClass}>{fmtScore(s.consistency_score)}</td>
              <td className={tdMutedClass}>{formatDateTime(s.scored_at)}</td>
            </tr>
          ))}
        </DataTable>
      )}
    </SectionCard>
  );
}

/* -------------------------------------------------------------------------- */

function numOrNull(v?: number | null): number | null {
  return v === null || v === undefined || Number.isNaN(v) ? null : Number(v);
}

function fmtScore(v?: number | null): string {
  const num = numOrNull(v);
  return num !== null ? num.toFixed(0) : "—";
}

type ProvidersPageError = ReturnType<typeof usePolling>["error"];
