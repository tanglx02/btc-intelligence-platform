"use client";

/**
 * 编辑预警规则（/alerts/rules/[id]）：
 * 表单预填充（与创建页共用 RuleForm）+ 暂停/恢复/删除/测试
 * + 历史触发模拟（日期范围 → 每日快照式求值 → 触发后 7/30/90 天表现）。
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useMemo, useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { RuleForm, emptyRuleFormValue } from "@/components/alerts/RuleForm";
import { RuleStateBadge, SeverityBadge5 } from "@/components/alerts/badges";
import { COOLDOWN_PRESETS } from "@/components/alerts/constants";
import {
  defaultSimple,
  extractSimple,
  firstLeaf,
  nodeToHumanText,
  normalizeSeverity,
} from "@/components/alerts/conditionUtils";
import { formatDateInput, formatDateTime, formatNumber, formatSignedPct } from "@/components/alerts/format";
import { Banner, ConfirmDialog, PageHeader, btnGhost, btnIcon, btnPrimary, cardCls } from "@/components/alerts/ui";
import { TestResultModal } from "@/components/alerts/TestResultModal";
import { asAlert } from "@/components/alerts/types";
import type {
  AlertRuleRecord,
  RuleBacktestResult,
  RuleFormPayload,
  RuleFormValue,
  RuleTestResult,
  SimpleCondition,
} from "@/components/alerts/types";

/* -------------------------------------------------------------------------- */
/* 辅助                                                                        */
/* -------------------------------------------------------------------------- */

function cooldownPresetFor(seconds?: number | null): { key: string; custom: string } {
  const s = seconds ?? 86400;
  const hit = COOLDOWN_PRESETS.find((p) => p.seconds === s);
  if (hit) return { key: hit.key, custom: "" };
  return { key: "custom", custom: String(s) };
}

function valueFromRule(r: AlertRuleRecord): RuleFormValue {
  const tree = r.condition_tree ?? null;
  let simple: SimpleCondition | null = tree ? extractSimple(tree) : null;
  if (!simple && tree) simple = extractSimple(firstLeaf(tree));
  const isComposite = tree !== null && simple === null;
  const cd = cooldownPresetFor(r.cooldown_seconds);

  return {
    ...emptyRuleFormValue(),
    rule_name: r.rule_name,
    description: r.description ?? "",
    severity: normalizeSeverity(r.severity),
    simple: simple ?? defaultSimple(),
    tree,
    tab: isComposite ? "advanced" : "simple",
    durationSeconds: r.duration_seconds ?? null,
    consecutiveCount: r.consecutive_count ?? 1,
    cooldownPreset: cd.key,
    cooldownCustom: cd.custom,
    emailEnabled: (r.channels ?? []).includes("EMAIL"),
  };
}

const HORIZONS: { key: string; label: string }[] = [
  { key: "7", label: "7 天后" },
  { key: "30", label: "30 天后" },
  { key: "90", label: "90 天后" },
];

/**
 * 表单面板：以规则记录惰性初始化一次受控表单。
 * 父组件以 key={rule.id} 渲染，切换规则时整体重置；刷新不覆盖未保存编辑。
 * （避免在 effect 中同步 setState）
 */
function RuleFormPanel({
  rule,
  saving,
  onSubmit,
}: {
  rule: AlertRuleRecord;
  saving: boolean;
  onSubmit: (p: RuleFormPayload) => void;
}) {
  const [formValue, setFormValue] = useState<RuleFormValue>(() => valueFromRule(rule));
  return (
    <div className={`${cardCls} px-4 py-5 sm:px-6`}>
      <RuleForm
        value={formValue}
        onChange={setFormValue}
        onSubmit={onSubmit}
        submitting={saving}
        submitLabel="保存修改"
      />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function EditAlertRulePage() {
  const params = useParams<{ id: string }>();
  const router = useRouter();
  const ruleId = params.id;

  const ruleQ = useApi<AlertRuleRecord>(
    () => asAlert<AlertRuleRecord>(apiClient.alerts.getRule(ruleId)),
    [ruleId],
  );

  const [saving, setSaving] = useState(false);
  const [banner, setBanner] = useState<{ tone: "success" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const [testModal, setTestModal] = useState<{
    open: boolean;
    loading: boolean;
    error: string | null;
    result: RuleTestResult | null;
  }>({ open: false, loading: false, error: null, result: null });

  const [pendingDelete, setPendingDelete] = useState(false);

  // 历史触发模拟
  const [btStart, setBtStart] = useState(() => formatDateInput(new Date(Date.now() - 90 * 86_400_000)));
  const [btEnd, setBtEnd] = useState(() => formatDateInput(new Date()));
  const [btLoading, setBtLoading] = useState(false);
  const [btError, setBtError] = useState<string | null>(null);
  const [btResult, setBtResult] = useState<RuleBacktestResult | null>(null);

  const paused = ruleQ.data?.state === "PAUSED" || ruleQ.data?.is_enabled === false;

  const handleSave = async (payload: RuleFormPayload) => {
    setSaving(true);
    setBanner(null);
    try {
      const res = await apiClient.alerts.updateRule(ruleId, payload as unknown as Record<string, unknown>);
      if (res.success) {
        setBanner({ tone: "success", text: "规则已保存" });
        ruleQ.refresh();
      } else {
        setBanner({ tone: "error", text: "保存失败，请稍后重试" });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "保存失败" });
    } finally {
      setSaving(false);
    }
  };

  const handleTogglePause = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = paused
        ? await apiClient.alerts.resumeRule(ruleId)
        : await apiClient.alerts.pauseRule(ruleId);
      if (res.success) {
        setBanner({ tone: "success", text: paused ? "规则已恢复" : "规则已暂停" });
        ruleQ.refresh();
      } else {
        setBanner({ tone: "error", text: "操作失败，请稍后重试" });
      }
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "操作失败" });
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    setBusy(true);
    try {
      const res = await apiClient.alerts.deleteRule(ruleId);
      if (res.success) {
        router.push("/alerts");
        return;
      }
      setBanner({ tone: "error", text: "删除失败，请稍后重试" });
    } catch (e) {
      setBanner({ tone: "error", text: e instanceof Error ? e.message : "删除失败" });
    } finally {
      setBusy(false);
      setPendingDelete(false);
    }
  };

  const handleTest = async () => {
    setTestModal({ open: true, loading: true, error: null, result: null });
    try {
      const res = await apiClient.alerts.testRule(ruleId);
      setTestModal({
        open: true,
        loading: false,
        error: res.success ? null : "规则测试失败",
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

  const handleBacktest = async () => {
    setBtLoading(true);
    setBtError(null);
    setBtResult(null);
    try {
      const res = await apiClient.alerts.backtestRule(ruleId, { start: btStart, end: btEnd });
      if (res.success && res.data) {
        const data = res.data as unknown as RuleBacktestResult;
        if (!data.found) {
          setBtError("规则不存在");
        } else {
          setBtResult(data);
        }
      } else {
        setBtError("历史模拟失败，请稍后重试");
      }
    } catch (e) {
      setBtError(e instanceof Error ? e.message : "历史模拟失败");
    } finally {
      setBtLoading(false);
    }
  };

  const performanceSummary = useMemo(() => {
    if (!btResult?.performance) return [];
    return HORIZONS.map(({ key, label }) => {
      const rows = btResult.performance?.[key] ?? [];
      const changes = rows.map((r) => r.change_pct);
      const avg = changes.length ? changes.reduce((a, b) => a + b, 0) / changes.length : null;
      return {
        key,
        label,
        count: changes.length,
        avg,
        best: changes.length ? Math.max(...changes) : null,
        worst: changes.length ? Math.min(...changes) : null,
      };
    });
  }, [btResult]);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        eyebrow="Edit Rule"
        title={ruleQ.data?.rule_name ?? "编辑规则"}
        description={ruleQ.data?.condition_text ?? undefined}
        actions={
          <>
            <Link href="/alerts" className={btnGhost}>
              ← 预警中心
            </Link>
            <button type="button" className={btnIcon} title="测试：立即求值一次" onClick={() => void handleTest()}>
              ⚡
            </button>
            <button
              type="button"
              className={btnIcon}
              title={paused ? "恢复规则" : "暂停规则"}
              onClick={() => void handleTogglePause()}
              disabled={busy || !ruleQ.data}
            >
              {paused ? "▶ 恢复" : "⏸ 暂停"}
            </button>
            <button
              type="button"
              className={`${btnIcon} hover:!border-down/50 hover:!text-down`}
              title="删除规则"
              onClick={() => setPendingDelete(true)}
              disabled={busy || !ruleQ.data}
            >
              🗑
            </button>
          </>
        }
      />

      {ruleQ.data && (
        <div className="animate-fade-up flex flex-wrap items-center gap-3 text-xs text-muted">
          <SeverityBadge5 severity={ruleQ.data.severity} />
          <RuleStateBadge state={ruleQ.data.state} isEnabled={ruleQ.data.is_enabled} />
          <span className="font-mono tabular-nums">累计触发 {ruleQ.data.trigger_count ?? 0} 次</span>
          <span>最后触发：{formatDateTime(ruleQ.data.last_triggered_at)}</span>
          <span>创建于：{formatDateTime(ruleQ.data.created_at)}</span>
        </div>
      )}

      {banner && <Banner tone={banner.tone}>{banner.text}</Banner>}

      {ruleQ.error && (
        <ErrorState error={ruleQ.error} lastUpdatedAt={ruleQ.lastUpdatedAt} onRetry={ruleQ.refresh} />
      )}

      {/* 表单 */}
      {ruleQ.loading && !ruleQ.data ? (
        <LoadingSkeleton variant="card" className="h-96" />
      ) : ruleQ.data ? (
        <RuleFormPanel
          key={ruleQ.data.id}
          rule={ruleQ.data}
          saving={saving}
          onSubmit={handleSave}
        />
      ) : null}

      {/* 历史触发模拟 */}
      <section className="animate-fade-up" style={{ animationDelay: "120ms" }}>
        <div className={cardCls}>
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
            <div>
              <h2 className="text-sm font-semibold text-[#e6edf3]">历史触发模拟</h2>
              <p className="mt-0.5 text-[11px] text-muted">
                用历史数据逐日回放求值：规则在过去何时会触发、触发后行情表现如何。
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <label className="flex items-center gap-1.5 text-muted">
                开始
                <input
                  type="date"
                  value={btStart}
                  max={btEnd}
                  onChange={(e) => setBtStart(e.target.value)}
                  className="rounded-lg border border-line bg-bg px-2 py-1 font-mono text-xs text-[#e6edf3]"
                />
              </label>
              <label className="flex items-center gap-1.5 text-muted">
                结束
                <input
                  type="date"
                  value={btEnd}
                  min={btStart}
                  onChange={(e) => setBtEnd(e.target.value)}
                  className="rounded-lg border border-line bg-bg px-2 py-1 font-mono text-xs text-[#e6edf3]"
                />
              </label>
              <button
                type="button"
                className={btnPrimary}
                onClick={() => void handleBacktest()}
                disabled={btLoading}
              >
                {btLoading ? "回放中…" : "▶ 运行模拟"}
              </button>
            </div>
          </div>

          <div className="px-4 py-4">
            {btError && <Banner tone="error">{btError}</Banner>}

            {btLoading && <LoadingSkeleton variant="table" rows={4} />}

            {!btLoading && !btResult && !btError && (
              <p className="py-6 text-center text-xs text-muted">
                选择日期范围并运行模拟（默认回看 90 天；状态变化类条件因缺历史引擎输出不支持回放）
              </p>
            )}

            {btResult && (
              <div className="flex flex-col gap-4">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  <div className="rounded-lg border border-line bg-bg px-3 py-2.5">
                    <p className="text-[10px] uppercase tracking-wider text-muted">触发次数</p>
                    <p className="mt-1 font-mono text-xl font-semibold tabular-nums text-accent">
                      {btResult.trigger_count ?? 0}
                    </p>
                  </div>
                  <div className="rounded-lg border border-line bg-bg px-3 py-2.5">
                    <p className="text-[10px] uppercase tracking-wider text-muted">平均间隔</p>
                    <p className="mt-1 font-mono text-xl font-semibold tabular-nums text-[#e6edf3]">
                      {btResult.avg_interval_days !== null && btResult.avg_interval_days !== undefined
                        ? `${btResult.avg_interval_days} 天`
                        : "—"}
                    </p>
                  </div>
                  <div className="col-span-2 rounded-lg border border-line bg-bg px-3 py-2.5">
                    <p className="text-[10px] uppercase tracking-wider text-muted">
                      触发日期（{btResult.trigger_dates?.length ?? 0}）
                    </p>
                    <div className="mt-1.5 flex max-h-20 flex-wrap gap-1 overflow-y-auto">
                      {(btResult.trigger_dates ?? []).length === 0 ? (
                        <span className="text-[11px] text-muted">区间内未触发</span>
                      ) : (
                        (btResult.trigger_dates ?? []).map((d) => (
                          <span
                            key={d}
                            className="rounded border border-line bg-bg-raised px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-muted"
                          >
                            {d}
                          </span>
                        ))
                      )}
                    </div>
                  </div>
                </div>

                <div className="overflow-x-auto">
                  <table className="w-full min-w-[480px] text-left text-xs">
                    <thead>
                      <tr className="border-b border-line text-[10px] uppercase tracking-wider text-muted">
                        <th className="py-2 pr-4 font-medium">观察窗口</th>
                        <th className="py-2 pr-4 text-right font-medium">样本数</th>
                        <th className="py-2 pr-4 text-right font-medium">平均表现</th>
                        <th className="py-2 pr-4 text-right font-medium">最好</th>
                        <th className="py-2 text-right font-medium">最差</th>
                      </tr>
                    </thead>
                    <tbody className="font-mono tabular-nums">
                      {performanceSummary.map((row) => (
                        <tr key={row.key} className="border-b border-line-subtle last:border-0">
                          <td className="py-2 pr-4 text-[#e6edf3]">{row.label}</td>
                          <td className="py-2 pr-4 text-right text-muted">{row.count}</td>
                          <td
                            className={`py-2 pr-4 text-right ${
                              row.avg === null ? "text-muted" : row.avg >= 0 ? "text-up" : "text-down"
                            }`}
                          >
                            {row.avg === null ? "—" : formatSignedPct(row.avg)}
                          </td>
                          <td className="py-2 pr-4 text-right text-up/80">
                            {row.best === null ? "—" : formatSignedPct(row.best)}
                          </td>
                          <td className="py-2 text-right text-down/80">
                            {row.worst === null ? "—" : formatSignedPct(row.worst)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {btResult.note && <p className="text-[11px] leading-relaxed text-warn">备注：{btResult.note}</p>}

                <p className="text-[11px] leading-relaxed text-muted">
                  ⚠️ 以下为历史统计，不代表规则未来有效。历史回放为日线粒度（逐日收盘快照），
                  「表现」为触发日收盘到 N 天后收盘的价格涨跌幅。
                </p>
              </div>
            )}
          </div>
        </div>
      </section>

      {/* 条件摘要（原始 AST） */}
      {ruleQ.data?.condition_tree && (
        <p className="animate-fade-up font-mono text-[10px] leading-relaxed text-muted/70">
          条件树 AST：{nodeToHumanText(ruleQ.data.condition_tree)} ·
          {" "}{formatNumber(ruleQ.data.trigger_count ?? 0)} 次触发 · 最后求值 {formatDateTime(ruleQ.data.last_evaluated_at)}
        </p>
      )}

      <TestResultModal
        open={testModal.open}
        loading={testModal.loading}
        error={testModal.error}
        result={testModal.result}
        onClose={() => setTestModal({ open: false, loading: false, error: null, result: null })}
      />

      <ConfirmDialog
        open={pendingDelete}
        title="删除预警规则"
        message={
          <>
            确定删除「<span className="text-[#e6edf3]">{ruleQ.data?.rule_name}</span>」？
            规则将停止监控，历史触发记录会保留。
          </>
        }
        confirmLabel="删除"
        danger
        loading={busy}
        onConfirm={() => void handleDelete()}
        onCancel={() => setPendingDelete(false)}
      />
    </div>
  );
}
