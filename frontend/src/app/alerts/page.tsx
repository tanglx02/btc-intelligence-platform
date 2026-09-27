"use client";

/**
 * 智能预警中心（/alerts）：
 * 统计卡行 + 规则列表（测试 / 暂停 / 恢复 / 编辑 / 删除）+ 最近事件快捷入口。
 */

import Link from "next/link";
import { useMemo, useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { EventStatusBadge, NotifBadge, RuleStateBadge, SeverityBadge5 } from "@/components/alerts/badges";
import { normalizeSeverity, nodeToHumanText } from "@/components/alerts/conditionUtils";
import { formatDateTime, formatDuration, formatRelative } from "@/components/alerts/format";
import { ConfirmDialog, PageHeader, StatCard, btnGhost, btnIcon, btnPrimary, cardCls } from "@/components/alerts/ui";
import { TestResultModal } from "@/components/alerts/TestResultModal";
import { asAlert } from "@/components/alerts/types";
import type { AlertEventRecord, AlertRuleRecord, AlertStats, Paginated, RuleTestResult } from "@/components/alerts/types";

/* -------------------------------------------------------------------------- */
/* 辅助                                                                        */
/* -------------------------------------------------------------------------- */

/** 冷却状态：根据 cooldown_seconds 与 last_triggered_at 推算 */
function cooldownInfo(rule: AlertRuleRecord): { label: string; className: string } {
  const cooldown = rule.cooldown_seconds ?? 0;
  if (!rule.last_triggered_at || cooldown <= 0) {
    return { label: "可触发", className: "text-muted" };
  }
  const elapsedMs = Date.now() - new Date(rule.last_triggered_at).getTime();
  const remainSec = Math.floor(cooldown - elapsedMs / 1000);
  if (remainSec > 0) {
    return { label: `冷却中 · 剩余 ${formatDuration(remainSec)}`, className: "text-warn" };
  }
  return { label: "可触发", className: "text-muted" };
}

/** 最近一次触发事件的状态 -> 通知状态推断（投递明细 alert_deliveries 暂未开放查询） */
function notifStatusOfEvent(status?: string | null): "SENT" | "FAILED" | "PENDING" | "SUPPRESSED" | null {
  switch (status) {
    case "TRIGGERED":
      return "PENDING";
    case "SUPPRESSED":
      return "SUPPRESSED";
    case "ACKED":
    case "RESOLVED":
      return "SENT";
    default:
      return null;
  }
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function AlertsPage() {
  const stats = useApi<AlertStats>(
    () => asAlert<AlertStats>(apiClient.alerts.getStats()),
    [],
    { refreshInterval: 30_000 },
  );
  const rules = useApi<Paginated<AlertRuleRecord>>(
    () => asAlert<Paginated<AlertRuleRecord>>(apiClient.alerts.getRules({ page: 1, size: 100 })),
    [],
    { refreshInterval: 30_000 },
  );
  const recentEvents = useApi<Paginated<AlertEventRecord>>(
    () => asAlert<Paginated<AlertEventRecord>>(apiClient.alerts.getEvents({ page: 1, size: 5 })),
    [],
    { refreshInterval: 30_000 },
  );

  const [testModal, setTestModal] = useState<{
    open: boolean;
    loading: boolean;
    error: string | null;
    result: RuleTestResult | null;
  }>({ open: false, loading: false, error: null, result: null });
  const [pendingDelete, setPendingDelete] = useState<AlertRuleRecord | null>(null);
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [busyRuleId, setBusyRuleId] = useState<string | null>(null);
  const [banner, setBanner] = useState<{ tone: "success" | "error"; text: string } | null>(null);

  const ruleList = rules.data?.items ?? [];

  /** rule_id -> 最近一次事件（用于「最近通知」列） */
  const lastEventByRule = useMemo(() => {
    const map = new Map<string, AlertEventRecord>();
    for (const ev of recentEvents.data?.items ?? []) {
      if (!map.has(ev.rule_id)) map.set(ev.rule_id, ev);
    }
    return map;
  }, [recentEvents.data]);

  /** severity 分布 -> 四级汇总（顺序展示） */
  const severitySummary = useMemo(() => {
    const dist = stats.data?.severity_distribution ?? {};
    const acc: Record<string, number> = { INFO: 0, WARNING: 0, HIGH: 0, CRITICAL: 0 };
    for (const [k, v] of Object.entries(dist)) {
      acc[normalizeSeverity(k)] += v;
    }
    return acc;
  }, [stats.data]);

  const handleTest = async (rule: AlertRuleRecord) => {
    setTestModal({ open: true, loading: true, error: null, result: null });
    try {
      const res = await apiClient.alerts.testRule(rule.id);
      setTestModal({
        open: true,
        loading: false,
        error: res.success ? null : "规则测试失败，请稍后重试",
        result: (res.data as unknown as RuleTestResult) ?? null,
      });
    } catch (e) {
      setTestModal({
        open: true,
        loading: false,
        error: e instanceof Error ? e.message : "规则测试失败",
        result: null,
      });
    }
  };

  const handleTogglePause = async (rule: AlertRuleRecord) => {
    const paused = rule.state === "PAUSED" || rule.is_enabled === false;
    setBusyRuleId(rule.id);
    setBanner(null);
    try {
      const res = paused
        ? await apiClient.alerts.resumeRule(rule.id)
        : await apiClient.alerts.pauseRule(rule.id);
      if (res.success) {
        setBanner({ tone: "success", text: paused ? `已恢复「${rule.rule_name}」` : `已暂停「${rule.rule_name}」` });
        rules.refresh();
        stats.refresh();
      } else {
        setBanner({ tone: "error", text: "操作失败，请稍后重试" });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "操作失败" });
    } finally {
      setBusyRuleId(null);
    }
  };

  const handleDelete = async () => {
    if (!pendingDelete) return;
    setDeleteLoading(true);
    try {
      const res = await apiClient.alerts.deleteRule(pendingDelete.id);
      if (res.success) {
        setBanner({ tone: "success", text: `已删除「${pendingDelete.rule_name}」（历史事件保留）` });
        rules.refresh();
        stats.refresh();
      } else {
        setBanner({ tone: "error", text: "删除失败，请稍后重试" });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "删除失败" });
    } finally {
      setDeleteLoading(false);
      setPendingDelete(null);
    }
  };

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        eyebrow="Alert Center"
        title="智能预警中心"
        description="基于条件树的智能预警：价格、链上指标、衍生品、引擎状态与个人持仓的全维度监控，满足条件即通过 Email 提醒。"
        actions={
          <>
            <Link href="/alerts/events" className={btnGhost}>
              触发历史
            </Link>
            <Link href="/alerts/channels" className={btnGhost}>
              通知渠道
            </Link>
            <Link href="/alerts/new" className={btnPrimary}>
              ＋ 新增规则
            </Link>
          </>
        }
      />

      {/* 全局操作反馈 */}
      {banner && (
        <div
          role="status"
          className={`flex items-center justify-between rounded-lg border px-3 py-2 text-xs ${
            banner.tone === "success"
              ? "border-up/40 bg-up/10 text-up"
              : "border-down/40 bg-down/10 text-down"
          }`}
        >
          <span>{banner.text}</span>
          <button type="button" onClick={() => setBanner(null)} aria-label="关闭" className="opacity-60 hover:opacity-100">
            ✕
          </button>
        </div>
      )}

      {/* 统计卡行 */}
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {stats.loading && !stats.data ? (
          <>
            <LoadingSkeleton variant="card" />
            <LoadingSkeleton variant="card" />
            <LoadingSkeleton variant="card" />
            <LoadingSkeleton variant="card" />
          </>
        ) : (
          <>
            <StatCard
              label="总规则数"
              value={stats.data?.total_rules ?? "—"}
              sub={`其中 ${stats.data?.paused_rules ?? 0} 条已暂停`}
              tone="accent"
              delay={0}
            />
            <StatCard
              label="活跃规则"
              value={stats.data?.active_rules ?? "—"}
              sub={
                <span className="flex flex-wrap gap-x-2">
                  {(
                    [
                      ["INFO", "text-accent"],
                      ["WARNING", "text-warn"],
                      ["HIGH", "text-orange"],
                      ["CRITICAL", "text-down"],
                    ] as const
                  ).map(([k, cls]) => (
                    <span key={k} className={cls}>
                      {severitySummary[k]} {k === "INFO" ? "提示" : k === "WARNING" ? "警告" : k === "HIGH" ? "重要" : "严重"}
                    </span>
                  ))}
                </span>
              }
              tone="up"
              delay={60}
            />
            <StatCard
              label="今日触发"
              value={stats.data?.today_triggered ?? "—"}
              sub="全部规则合计"
              tone="warn"
              delay={120}
            />
            <StatCard
              label="待确认事件"
              value={stats.data?.unacked_events ?? "—"}
              sub={
                <Link href="/alerts/events" className="text-accent hover:underline">
                  查看触发历史 →
                </Link>
              }
              tone="down"
              delay={180}
            />
          </>
        )}
      </section>

      {stats.error && (
        <ErrorState
          error={stats.error}
          lastUpdatedAt={stats.lastUpdatedAt}
          onRetry={stats.refresh}
        />
      )}

      {/* 规则列表 */}
      <section className="animate-fade-up" style={{ animationDelay: "120ms" }}>
        <div className={`${cardCls} overflow-hidden`}>
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <h2 className="text-sm font-semibold text-[#e6edf3]">预警规则</h2>
            <button
              type="button"
              onClick={() => rules.refresh()}
              className="text-[11px] text-muted transition-colors hover:text-accent"
            >
              刷新 ↻
            </button>
          </div>

          {rules.loading && !rules.data ? (
            <div className="p-4">
              <LoadingSkeleton variant="table" rows={5} />
            </div>
          ) : ruleList.length === 0 ? (
            <div className="p-4">
              <EmptyState
                title="还没有预警规则"
                description="从 12 个预设模板快速创建（价格突破、极端恐慌、杠杆风险…），或从空白构建条件树。"
                action={
                  <Link href="/alerts/new" className={btnPrimary}>
                    ＋ 创建第一条规则
                  </Link>
                }
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[900px] text-left text-xs">
                <thead>
                  <tr className="border-b border-line text-[10px] uppercase tracking-wider text-muted">
                    <th className="px-4 py-2.5 font-medium">规则</th>
                    <th className="px-3 py-2.5 font-medium">等级</th>
                    <th className="px-3 py-2.5 font-medium">状态</th>
                    <th className="px-3 py-2.5 font-medium">冷却状态</th>
                    <th className="px-3 py-2.5 text-right font-medium">触发次数</th>
                    <th className="px-3 py-2.5 font-medium">最后触发</th>
                    <th className="px-3 py-2.5 font-medium">最近通知</th>
                    <th className="px-4 py-2.5 text-right font-medium">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {ruleList.map((rule, i) => {
                    const cd = cooldownInfo(rule);
                    const paused = rule.state === "PAUSED" || rule.is_enabled === false;
                    const lastEvent = lastEventByRule.get(rule.id);
                    const conditionText = rule.condition_text || nodeToHumanText(rule.condition_tree);
                    return (
                      <tr
                        key={rule.id}
                        className="animate-fade-up border-b border-line-subtle transition-colors last:border-0 hover:bg-bg-hover/40"
                        style={{ animationDelay: `${Math.min(i * 40, 240)}ms` }}
                      >
                        <td className="max-w-[280px] px-4 py-3">
                          <p className="truncate font-medium text-[#e6edf3]">{rule.rule_name}</p>
                          <p className="mt-0.5 truncate text-[11px] text-muted" title={conditionText}>
                            {conditionText}
                          </p>
                        </td>
                        <td className="px-3 py-3">
                          <SeverityBadge5 severity={rule.severity} />
                        </td>
                        <td className="px-3 py-3">
                          <RuleStateBadge state={rule.state} isEnabled={rule.is_enabled} />
                        </td>
                        <td className={`px-3 py-3 ${cd.className}`}>{cd.label}</td>
                        <td className="px-3 py-3 text-right font-mono tabular-nums">
                          {rule.trigger_count ?? 0}
                        </td>
                        <td className="px-3 py-3" title={formatDateTime(rule.last_triggered_at, true)}>
                          {formatRelative(rule.last_triggered_at)}
                        </td>
                        <td className="px-3 py-3">
                          <span title="基于最近一次触发事件推断（邮件投递明细暂未开放查询）">
                            <NotifBadge status={notifStatusOfEvent(lastEvent?.status)} />
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              type="button"
                              className={btnIcon}
                              title="测试：立即求值一次"
                              onClick={() => void handleTest(rule)}
                              disabled={busyRuleId === rule.id}
                            >
                              ⚡
                            </button>
                            <button
                              type="button"
                              className={btnIcon}
                              title={paused ? "恢复规则" : "暂停规则"}
                              onClick={() => void handleTogglePause(rule)}
                              disabled={busyRuleId === rule.id}
                            >
                              {paused ? "▶" : "⏸"}
                            </button>
                            <Link href={`/alerts/rules/${rule.id}`} className={btnIcon} title="编辑规则">
                              ✎
                            </Link>
                            <button
                              type="button"
                              className={`${btnIcon} hover:!border-down/50 hover:!text-down`}
                              title="删除规则"
                              onClick={() => setPendingDelete(rule)}
                              disabled={busyRuleId === rule.id}
                            >
                              🗑
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* 最近事件快捷入口 */}
      <section className="animate-fade-up" style={{ animationDelay: "200ms" }}>
        <div className={cardCls}>
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <h2 className="text-sm font-semibold text-[#e6edf3]">最近触发事件</h2>
            <Link href="/alerts/events" className="text-[11px] text-accent hover:underline">
              全部历史 →
            </Link>
          </div>
          <div className="divide-y divide-line-subtle">
            {recentEvents.loading && !recentEvents.data ? (
              <div className="p-4">
                <LoadingSkeleton variant="lines" rows={4} />
              </div>
            ) : (recentEvents.data?.items ?? []).length === 0 ? (
              <p className="px-4 py-6 text-center text-xs text-muted">
                暂无触发记录 —— 条件满足时事件会出现在这里
              </p>
            ) : (
              (recentEvents.data?.items ?? []).map((ev) => (
                <Link
                  key={ev.id}
                  href="/alerts/events"
                  className="flex items-center gap-3 px-4 py-2.5 transition-colors hover:bg-bg-hover/40"
                >
                  <span className="w-36 shrink-0 font-mono text-[11px] tabular-nums text-muted">
                    {formatDateTime(ev.triggered_at)}
                  </span>
                  <span className="min-w-0 flex-1 truncate text-xs text-[#e6edf3]">
                    {ev.rule_name ?? ev.title ?? "未命名规则"}
                    {ev.is_suppressed && (
                      <span className="ml-2 text-[10px] text-muted">
                        （{ev.suppress_reason === "COOLDOWN" ? "冷却期抑制" : "已抑制"}）
                      </span>
                    )}
                  </span>
                  <SeverityBadge5 severity={ev.severity} />
                  <EventStatusBadge status={ev.status} />
                </Link>
              ))
            )}
          </div>
        </div>
      </section>

      {/* 测试结果弹窗 */}
      <TestResultModal
        open={testModal.open}
        loading={testModal.loading}
        error={testModal.error}
        result={testModal.result}
        onClose={() => setTestModal({ open: false, loading: false, error: null, result: null })}
      />

      {/* 删除确认 */}
      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除预警规则"
        message={
          <>
            确定删除「<span className="text-[#e6edf3]">{pendingDelete?.rule_name}</span>」？
            规则将停止监控，历史触发记录会保留。
          </>
        }
        confirmLabel="删除"
        danger
        loading={deleteLoading}
        onConfirm={() => void handleDelete()}
        onCancel={() => setPendingDelete(null)}
      />
    </div>
  );
}
