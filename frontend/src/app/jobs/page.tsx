"use client";

/**
 * 系统任务（System Jobs）。
 *
 * - 任务看板统计（总数 / 运行中 / 暂停 / 失败）与任务列表
 *   （任务名 / 描述 / 调度间隔 / 优先级 / 状态 / 最后运行 / 最后结果 / 连续失败 / 平均耗时）
 * - 操作：手动执行（POST /system/jobs/{id}/run）、暂停/恢复（PUT，后端未实现时行内报错降级）
 * - 行展开：任务详情（类型 / 分组 / 下次运行 / 重试计数 / 最近错误 / 执行历史如有）
 *
 * API：GET /system/jobs（轮询 10s）· POST /system/jobs/{id}/run。
 */

import { useMemo, useState } from "react";

import { apiClient } from "@/lib/api";
import { usePolling } from "@/hooks/useApi";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { systemApi } from "@/components/system/api";
import {
  DataTable,
  InlineNotice,
  PageHeader,
  SectionCard,
  StatChip,
  StatusDot,
  btnDanger,
  btnGhost,
  btnPrimary,
  btnSuccess,
  formatDateTime,
  formatMs,
  formatRelative,
  formatSchedule,
  fmtInt,
  rowClass,
  tdClass,
  tdMutedClass,
} from "@/components/system/ui";
import type { JobRow, JobsPayload } from "@/components/system/types";

import type { ApiRequestError } from "@/lib/api";
import type { ApiResponse } from "@/types/api";

const POLL_MS = 10_000;

/** 行内操作提示（按 job id 记录，避免互相覆盖） */
interface JobNotice {
  tone: "success" | "error" | "info";
  text: string;
}

export default function JobsPage() {
  const jobsRes = usePolling<JobsPayload>(
    () => apiClient.system.getJobs() as unknown as Promise<ApiResponse<JobsPayload>>,
    POLL_MS,
  );

  const jobs = useMemo<JobRow[]>(() => {
    const data = jobsRes.data as unknown;
    if (Array.isArray(data)) return data as JobRow[]; // 防御：直接数组形态
    if (data && typeof data === "object" && Array.isArray((data as JobsPayload).jobs)) {
      return (data as JobsPayload).jobs ?? [];
    }
    return [];
  }, [jobsRes.data]);

  /* 展开行 + 每行操作状态 */
  const [expanded, setExpanded] = useState<string | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [notices, setNotices] = useState<Record<string, JobNotice>>({});

  function setNotice(id: string, notice: JobNotice | null) {
    setNotices((prev) => {
      const next = { ...prev };
      if (notice === null) delete next[id];
      else next[id] = notice;
      return next;
    });
  }

  async function act(id: string, action: "run" | "pause" | "resume") {
    setActingId(id);
    setNotice(id, null);
    try {
      const res =
        action === "run"
          ? await apiClient.system.runJob(id)
          : action === "pause"
            ? await systemApi.pauseJob(id)
            : await systemApi.resumeJob(id);
      if (res.success === false) {
        setNotice(id, { tone: "error", text: res.error?.message ?? "操作返回异常" });
      } else {
        const data = res.data as Record<string, unknown> | null;
        const note =
          action === "run"
            ? (data?.note as string | undefined) ?? "已登记立即执行，等待 Scheduler 调度"
            : action === "pause"
              ? "任务已请求暂停"
              : "任务已请求恢复";
        setNotice(id, { tone: "success", text: note });
        jobsRes.refresh();
      }
    } catch (err) {
      const e = err as ApiRequestError;
      const hint =
        e.code === "HTTP_404" || e.code === "HTTP_405" || e.code === "NETWORK_ERROR"
          ? "（后端暂未提供该操作端点或服务不可达）"
          : "";
      setNotice(id, { tone: "error", text: `${e.message}${hint}` });
    } finally {
      setActingId(null);
    }
  }

  /* 看板统计 */
  const summary = useMemo(() => {
    let running = 0;
    let paused = 0;
    let failed = 0;
    for (const j of jobs) {
      const s = (j.status ?? "").toUpperCase();
      if (s === "RUNNING") running += 1;
      if (s === "PAUSED" || j.is_enabled === false) paused += 1;
      if (s === "FAILED" || (j.consecutive_failures ?? 0) > 0) failed += 1;
    }
    return { total: jobs.length, running, paused, failed };
  }, [jobs]);

  const loading = jobsRes.loading && jobs.length === 0;

  return (
    <div className="space-y-5">
      <PageHeader
        kicker="System · Jobs"
        title="系统任务"
        description="采集 / 计算 / 质量检查等后台调度任务的运行状态与手动控制 —— 谁在什么时候跑了、跑了多久、失败了没有。"
      >
        <span className="rounded-lg border border-line bg-bg px-2.5 py-1 text-[11px] text-muted">
          轮询 10s · 更新 {formatRelative(jobsRes.lastUpdatedAt)}
        </span>
      </PageHeader>

      {/* 看板统计 */}
      <div className="animate-fade-up flex flex-wrap items-center gap-2" style={{ animationDelay: "60ms" }}>
        <StatChip label="任务总数" value={summary.total} />
        <StatChip label="运行中" value={summary.running} tone="up" />
        <StatChip label="已暂停/禁用" value={summary.paused} tone={summary.paused > 0 ? "warn" : "default"} />
        <StatChip label="失败/连续失败" value={summary.failed} tone={summary.failed > 0 ? "down" : "up"} />
      </div>

      {jobsRes.error && jobs.length > 0 && (
        <ErrorState error={jobsRes.error} lastUpdatedAt={jobsRes.lastUpdatedAt} />
      )}

      <SectionCard
        title="任务列表"
        subtitle="点击行展开任务详情与最近错误 · 操作立即生效需等待下个调度周期"
      >
        {loading ? (
          <LoadingSkeleton variant="table" rows={6} />
        ) : jobs.length === 0 ? (
          <EmptyState
            title="暂无任务数据"
            description={
              jobsRes.error?.message ??
              "后端未返回任何调度任务。请确认 Scheduler 已启动且 system_jobs 表已初始化。"
            }
          />
        ) : (
          <DataTable
            columns={["任务", "描述", "调度间隔", "优先级", "状态", "最后运行", "最后结果", "连续失败", "平均耗时", "操作"]}
            minWidth={1080}
          >
            {jobs.map((job) => (
              <JobRowGroup
                key={job.id}
                job={job}
                isExpanded={expanded === job.id}
                acting={actingId === job.id}
                notice={notices[job.id] ?? null}
                onToggle={() => setExpanded(expanded === job.id ? null : job.id)}
                onAct={(action) => void act(job.id, action)}
              />
            ))}
          </DataTable>
        )}
      </SectionCard>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 单行 + 展开详情                                                             */
/* -------------------------------------------------------------------------- */

function JobRowGroup({
  job,
  isExpanded,
  acting,
  notice,
  onToggle,
  onAct,
}: {
  job: JobRow;
  isExpanded: boolean;
  acting: boolean;
  notice: JobNotice | null;
  onToggle: () => void;
  onAct: (action: "run" | "pause" | "resume") => void;
}) {
  const enabled = job.is_enabled !== false;
  const lastFailed =
    Boolean(job.last_error) || (job.consecutive_failures ?? 0) > 0 || job.status === "FAILED";
  const successRate = typeof job.success_rate === "number" ? job.success_rate : null;

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
              <p className="font-mono text-xs font-semibold text-[#e6edf3]">
                {job.job_name ?? job.id}
              </p>
              {job.job_group && (
                <span className="mt-0.5 inline-block rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
                  {job.job_group}
                </span>
              )}
            </div>
          </div>
        </td>
        <td className={`${tdClass} max-w-[240px] truncate text-muted`} title={job.description ?? undefined}>
          {job.description ?? "—"}
        </td>
        <td className={tdMutedClass}>{formatSchedule(job.schedule_cron, job.schedule_interval_seconds)}</td>
        <td className={tdMutedClass}>{job.priority ?? "—"}</td>
        <td className={tdClass}>
          <div className="flex items-center gap-1.5">
            <StatusDot status={job.status} />
            {!enabled && (
              <span className="rounded border border-down/40 bg-down/10 px-1.5 py-0.5 text-[10px] text-down">
                已禁用
              </span>
            )}
          </div>
        </td>
        <td className={tdMutedClass}>
          <span title={formatDateTime(job.last_run_at)}>{formatRelative(job.last_run_at)}</span>
        </td>
        <td className={tdClass}>
          {lastFailed ? (
            <span className="rounded border border-down/40 bg-down/10 px-1.5 py-0.5 text-[10px] text-down">
              失败
            </span>
          ) : job.last_run_at ? (
            <span className="rounded border border-up/40 bg-up/10 px-1.5 py-0.5 text-[10px] text-up">
              成功
            </span>
          ) : (
            <span className="text-[10px] text-muted">未运行</span>
          )}
        </td>
        <td className={tdMutedClass}>
          {(job.consecutive_failures ?? 0) > 0 ? (
            <span className="text-down">{job.consecutive_failures}</span>
          ) : successRate !== null ? (
            `${(successRate * 100).toFixed(0)}%`
          ) : (
            "0"
          )}
        </td>
        <td className={tdMutedClass}>{formatMs(job.avg_duration_ms)}</td>
        <td className={tdClass}>
          <div className="flex items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
            <button
              type="button"
              disabled={acting || !enabled}
              onClick={() => onAct("run")}
              className={btnPrimary}
              title={enabled ? "登记立即执行" : "任务已禁用"}
            >
              {acting ? "…" : "执行"}
            </button>
            {enabled ? (
              <button type="button" disabled={acting} onClick={() => onAct("pause")} className={btnDanger}>
                暂停
              </button>
            ) : (
              <button type="button" disabled={acting} onClick={() => onAct("resume")} className={btnSuccess}>
                恢复
              </button>
            )}
          </div>
        </td>
      </tr>

      {/* 行内操作提示 */}
      {notice && (
        <tr className="border-b border-line/60 bg-bg/40">
          <td colSpan={10} className="px-3 py-2 sm:px-6">
            <InlineNotice tone={notice.tone} className="max-w-2xl">
              {job.job_name ?? job.id}：{notice.text}
            </InlineNotice>
          </td>
        </tr>
      )}

      {/* 展开详情 */}
      {isExpanded && (
        <tr className="border-b border-line/60 bg-bg/60">
          <td colSpan={10} className="px-3 py-4 sm:px-6">
            <div className="animate-fade-up grid grid-cols-2 gap-x-6 gap-y-3 text-xs sm:grid-cols-4">
              <Detail label="任务 ID" value={job.id} mono />
              <Detail label="任务类型" value={job.job_type ?? "—"} mono />
              <Detail label="任务分组" value={job.job_group ?? "—"} />
              <Detail label="Cron 表达式" value={job.schedule_cron ?? "—"} mono />
              <Detail label="调度间隔" value={formatSchedule(job.schedule_cron, job.schedule_interval_seconds)} />
              <Detail label="下次计划运行" value={formatDateTime(job.next_run_at)} mono />
              <Detail label="重试计数" value={fmtInt(job.retry_count)} mono />
              <Detail
                label="启用状态"
                value={enabled ? "已启用" : "已禁用"}
                tone={enabled ? "up" : "down"}
              />
            </div>
            {job.last_error && (
              <div className="mt-3 rounded-lg border border-down/30 bg-down/[0.06] px-3 py-2">
                <p className="text-[10px] font-medium uppercase tracking-wider text-down">
                  最近错误
                </p>
                <p className="mt-1 break-all font-mono text-[11px] leading-relaxed text-[#e6edf3]">
                  {job.last_error}
                </p>
              </div>
            )}
            <JobHistory job={job} />
          </td>
        </tr>
      )}
    </>
  );
}

/** 任务执行历史：后端暂未在 /system/jobs 中返回，若未来附加 history 字段则渲染 */
function JobHistory({ job }: { job: JobRow }) {
  const history = job.history;
  if (!Array.isArray(history) || history.length === 0) {
    return (
      <div className="mt-3">
        <button type="button" className={btnGhost} disabled>
          执行历史（后端暂未提供）
        </button>
      </div>
    );
  }
  return (
    <div className="mt-3">
      <p className="mb-1.5 text-[11px] font-medium text-muted">执行历史</p>
      <DataTable columns={["时间", "结果"]} minWidth={420}>
        {history.slice(0, 8).map((h, i) => {
          const item = (h ?? {}) as { time?: string; status?: string; duration_ms?: number };
          return (
            <tr key={i} className="hover:bg-bg-hover">
              <td className={`${tdClass} font-mono tabular-nums text-muted`}>
                {formatDateTime(item.time ?? null)}
              </td>
              <td className={tdClass}>
                <StatusDot status={item.status ?? null} />
                {item.duration_ms !== undefined && (
                  <span className="ml-2 font-mono text-[11px] text-muted">
                    {formatMs(item.duration_ms)}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </DataTable>
    </div>
  );
}

/* -------------------------------------------------------------------------- */

function Detail({
  label,
  value,
  mono = false,
  tone,
}: {
  label: string;
  value: string;
  mono?: boolean;
  tone?: "up" | "down";
}) {
  return (
    <div className="min-w-0">
      <p className="text-[10px] uppercase tracking-wider text-muted">{label}</p>
      <p
        className={`mt-0.5 truncate ${mono ? "font-mono tabular-nums" : ""} ${
          tone === "up" ? "text-up" : tone === "down" ? "text-down" : "text-[#e6edf3]"
        }`}
        title={value}
      >
        {value}
      </p>
    </div>
  );
}
