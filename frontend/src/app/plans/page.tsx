"use client";

/**
 * 我的计划 —— 个人 BTC 资金计划管理（docs/architecture/15 §1、16 §8.14）。
 *
 * 计划列表（卡片 + 状态徽标 + 执行指标）→ 展开（交易记录 / 执行进度）
 * 创建 / 编辑（表单模态框，策略模板 + 倍数表）→ 暂停 / 恢复 / 删除（确认框）。
 * 只规划、不自动交易（需求第四十七节）。
 */

import { useMemo, useState } from "react";

import { ConfirmDialog, GhostButton, Modal, PrimaryButton } from "@/components/portfolio/Modal";
import {
  Field,
  FormError,
  NumberInput,
  SelectInput,
  TextInput,
  DateInput,
} from "@/components/portfolio/Form";
import { Badge, PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import { DEFAULT_MULTIPLIER_RULES, MultiplierEditor, type MultiplierRule } from "@/components/portfolio/MultiplierEditor";
import { useApi } from "@/hooks/useApi";
import { apiClient } from "@/lib/api";
import type { UserPlan } from "@/types/api";
import {
  cn,
  formatDate,
  formatDateTime,
  formatMoney,
  pickNum,
  pickStr,
  planStatusMeta,
  sideMeta,
  strategyLabel,
  frequencyLabel,
} from "@/components/portfolio/utils";
import type { ApiRequestError } from "@/lib/api";
import { ErrorState } from "@/components/common/ErrorState";
import { EmptyState } from "@/components/common/EmptyState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { TrendIndicator } from "@/components/common/TrendIndicator";

const FREQUENCY_OPTIONS = [
  { value: "daily", label: "每日投入" },
  { value: "weekly", label: "每周投入" },
  { value: "monthly", label: "每月投入" },
];

const STRATEGY_OPTIONS = [
  { value: "fixed", label: "固定定投（基准）" },
  { value: "dip_ath", label: "下跌加仓（距 ATH 回撤梯度）" },
  { value: "value", label: "估值加仓（低估多投）" },
];

function currencyOf(plan: UserPlan): "CNY" | "USD" {
  const c = pickStr(plan, ["currency", "quote_currency"]) ?? "CNY";
  return c.toUpperCase() === "USD" ? "USD" : "CNY";
}

/* -------------------------------------------------------------------------- */
/* 创建 / 编辑计划表单                                                         */
/* -------------------------------------------------------------------------- */

interface FormState {
  name: string;
  initial_capital: string;
  base_amount: string;
  frequency: string;
  currency: "CNY" | "USD";
  start_date: string;
  strategy: string;
  max_drawdown_tolerance_pct: string;
  rules: MultiplierRule[];
}

const EMPTY_FORM: FormState = {
  name: "",
  initial_capital: "",
  base_amount: "",
  frequency: "monthly",
  currency: "CNY",
  start_date: "",
  strategy: "fixed",
  max_drawdown_tolerance_pct: "50",
  rules: DEFAULT_MULTIPLIER_RULES,
};

function PlanFormModal({
  open,
  editing,
  onClose,
  onSaved,
}: {
  open: boolean;
  editing: UserPlan | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<FormState>(() =>
    editing
      ? {
          name: editing.name ?? "",
          initial_capital: String(pickNum(editing, ["initial_capital"]) ?? ""),
          base_amount: String(pickNum(editing, ["base_amount", "monthly_amount"]) ?? ""),
          frequency: pickStr(editing, ["frequency", "dca_period"]) ?? "monthly",
          currency: currencyOf(editing),
          start_date: formatDate(editing.start_date),
          strategy: pickStr(editing, ["strategy_template", "strategy", "strategy_code"]) ?? "fixed",
          max_drawdown_tolerance_pct: String(
            Math.abs(pickNum(editing, ["max_drawdown_tolerance"]) ?? -0.5) * 100,
          ),
          rules: (editing.rules as MultiplierRule[] | undefined)?.length
            ? (editing.rules as MultiplierRule[])
            : DEFAULT_MULTIPLIER_RULES,
        }
      : EMPTY_FORM,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const set = (patch: Partial<FormState>) => setForm((prev) => ({ ...prev, ...patch }));

  const validate = (): boolean => {
    const errs: Record<string, string> = {};
    if (!form.name.trim()) errs.name = "请填写计划名称";
    if (!form.base_amount || Number(form.base_amount) <= 0) errs.base_amount = "每期投入需大于 0";
    if (!form.start_date) errs.start_date = "请选择开始日期";
    setFieldErrors(errs);
    return Object.keys(errs).length === 0;
  };

  const submit = async () => {
    if (!validate()) return;
    setSubmitting(true);
    setError(null);
    const tolerancePct = Number(form.max_drawdown_tolerance_pct) || 50;
    const body: Record<string, unknown> = {
      name: form.name.trim(),
      initial_capital: form.initial_capital ? Number(form.initial_capital) : 0,
      base_amount: Number(form.base_amount),
      frequency: form.frequency,
      currency: form.currency,
      start_date: form.start_date,
      strategy_template: form.strategy,
      strategy: form.strategy,
      rules: form.strategy === "dip_ath" ? form.rules : [],
      max_drawdown_tolerance: -(tolerancePct / 100),
    };
    try {
      if (editing) {
        await apiClient.portfolio.updatePlan(editing.id, body);
        toast.show("success", "计划已更新");
      } else {
        await apiClient.portfolio.createPlan(body);
        toast.show("success", "计划已创建");
      }
      onSaved();
      onClose();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "保存失败，请稍后重试";
      setError(msg);
      toast.show("error", msg);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? "编辑计划" : "创建计划"}
      description="计划是「将来打算怎么做」的前瞻配置；实际交易请在「我的资产」手工记录。系统只提醒、只模拟，绝不自动交易。"
      footer={
        <>
          <GhostButton onClick={onClose}>取消</GhostButton>
          <PrimaryButton loading={submitting} onClick={submit}>
            {editing ? "保存修改" : "创建计划"}
          </PrimaryButton>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={error} />
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="计划名称" required error={fieldErrors.name} className="sm:col-span-2">
            <TextInput
              value={form.name}
              onChange={(e) => set({ name: e.target.value })}
              placeholder="如：十年 BTC 定投计划"
              maxLength={60}
            />
          </Field>
          <Field label="初始资金" hint="一次性投入，0 为纯定投">
            <NumberInput
              value={form.initial_capital}
              onChange={(e) => set({ initial_capital: e.target.value })}
              placeholder="0"
            />
          </Field>
          <Field label="计价币种">
            <SelectInput
              value={form.currency}
              onChange={(e) => set({ currency: e.target.value as "CNY" | "USD" })}
              options={[
                { value: "CNY", label: "CNY（人民币）" },
                { value: "USD", label: "USD（美元）" },
              ]}
            />
          </Field>
          <Field label="每期投入金额" required error={fieldErrors.base_amount}>
            <NumberInput
              value={form.base_amount}
              onChange={(e) => set({ base_amount: e.target.value })}
              placeholder="5000"
            />
          </Field>
          <Field label="定投频率">
            <SelectInput
              value={form.frequency}
              onChange={(e) => set({ frequency: e.target.value })}
              options={FREQUENCY_OPTIONS}
            />
          </Field>
          <Field label="开始日期" required error={fieldErrors.start_date}>
            <DateInput value={form.start_date} onChange={(e) => set({ start_date: e.target.value })} />
          </Field>
          <Field
            label="最大回撤容忍度"
            hint="触发提醒阈值，不自动卖出"
          >
            <div className="relative">
              <NumberInput
                value={form.max_drawdown_tolerance_pct}
                onChange={(e) => set({ max_drawdown_tolerance_pct: e.target.value })}
                className="pr-7"
              />
              <span aria-hidden className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted">
                %
              </span>
            </div>
          </Field>
          <Field label="策略选择" className="sm:col-span-2">
            <SelectInput
              value={form.strategy}
              onChange={(e) => set({ strategy: e.target.value })}
              options={STRATEGY_OPTIONS}
            />
          </Field>
        </div>

        {form.strategy === "dip_ath" && (
          <div>
            <p className="mb-2 text-[11px] font-medium text-muted">加仓倍数表（默认 = 需求示例，可调）</p>
            <MultiplierEditor rules={form.rules} onChange={(rules) => set({ rules })} disabled={submitting} />
          </div>
        )}
        {form.strategy === "value" && (
          <p className="rounded-lg border border-line bg-bg/60 px-3 py-2.5 text-[11px] leading-relaxed text-muted">
            估值加仓：MVRV / NUPL 分位低于阈值时按倍数加仓，具体阈值由规则引擎内置（MVRV 分位 &lt;30% ×1.5、&lt;15% ×2.0）。完整规则编辑将在策略实验室提供。
          </p>
        )}
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------------- */
/* 计划详情 · 交易记录                                                         */
/* -------------------------------------------------------------------------- */

function PlanTransactions({ planId }: { planId: string }) {
  const { data, loading, error, refresh } = useApi(
    () => apiClient.portfolio.getTransactions({ plan_id: planId, page_size: 20 }),
    [planId],
  );

  if (loading) return <LoadingSkeleton variant="table" rows={3} />;
  if (error && !data) {
    return <ErrorState error={error} onRetry={refresh} compact />;
  }
  const txs = data ?? [];
  if (!txs.length) {
    return <p className="py-3 text-center text-[11px] text-muted">该计划暂无交易记录 —— 实际成交请到「我的资产」记录</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[480px] text-left text-xs">
        <thead>
          <tr className="border-b border-line text-[10px] uppercase tracking-wider text-muted">
            <th className="py-2 pr-3 font-medium">时间</th>
            <th className="py-2 pr-3 font-medium">方向</th>
            <th className="py-2 pr-3 text-right font-medium">价格</th>
            <th className="py-2 pr-3 text-right font-medium">数量</th>
            <th className="py-2 text-right font-medium">手续费</th>
          </tr>
        </thead>
        <tbody>
          {txs.map((tx) => {
            const side = sideMeta(tx.side);
            return (
              <tr key={tx.id} className="border-b border-line/40 last:border-0">
                <td className="py-2 pr-3 font-mono tabular-nums text-muted">{formatDateTime(tx.traded_at)}</td>
                <td className="py-2 pr-3">
                  <Badge className={side.className}>{side.label}</Badge>
                </td>
                <td className="py-2 pr-3 text-right font-mono tabular-nums">{formatMoney(tx.price)}</td>
                <td className="py-2 pr-3 text-right font-mono tabular-nums">{tx.quantity?.toFixed(8)}</td>
                <td className="py-2 text-right font-mono tabular-nums text-muted">
                  {tx.fee ? formatMoney(tx.fee) : "—"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 计划卡片                                                                    */
/* -------------------------------------------------------------------------- */

function PlanCard({
  plan,
  expanded,
  onToggle,
  onEdit,
  onPauseToggle,
  onDelete,
  actionLoading,
}: {
  plan: UserPlan;
  expanded: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onPauseToggle: () => void;
  onDelete: () => void;
  actionLoading: boolean;
}) {
  const currency = currencyOf(plan);
  const status = planStatusMeta(plan.status);
  const initial = pickNum(plan, ["initial_capital"]);
  const baseAmount = pickNum(plan, ["base_amount", "monthly_amount", "weekly_amount"]);
  const invested = pickNum(plan, ["invested_total"]);
  const marketValue = pickNum(plan, ["market_value"]);
  const returnPct = pickNum(plan, ["return_pct", "total_return_pct", "unrealized_pnl_pct"]);
  const nextAt = pickStr(plan, ["next_investment_at"]);
  const nextAmount = pickNum(plan, ["next_investment_amount"]);
  const tolerance = pickNum(plan, ["max_drawdown_tolerance"]);

  return (
    <article
      className={cn(
        "animate-fade-up overflow-hidden rounded-xl border border-line border-l-2 bg-bg-raised/80 transition-colors",
        expanded ? "border-accent/40" : "hover:border-line",
      )}
    >
      <div className={cn("absolute", status.bar, "hidden")} aria-hidden />
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className="block w-full px-4 py-4 text-left"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span aria-hidden className={cn("h-1.5 w-1.5 rounded-full", status.bar)} />
          <h3 className="text-sm font-semibold text-[#e6edf3]">{plan.name}</h3>
          <Badge className={status.className}>{status.label}</Badge>
          <Badge className="border-line bg-bg-hover text-muted">{strategyLabel(pickStr(plan, ["strategy_template", "strategy"]))}</Badge>
          <span className="ml-auto text-[11px] text-muted">
            {frequencyLabel(pickStr(plan, ["frequency", "dca_period"]))}
            {plan.start_date ? ` · ${formatDate(plan.start_date)} 起` : ""}
          </span>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2.5 sm:grid-cols-5">
          <div>
            <p className="text-[10px] text-muted">初始资金</p>
            <p className="font-mono text-sm tabular-nums">{formatMoney(initial, currency)}</p>
          </div>
          <div>
            <p className="text-[10px] text-muted">每期投入</p>
            <p className="font-mono text-sm tabular-nums">{formatMoney(baseAmount, currency)}</p>
          </div>
          <div>
            <p className="text-[10px] text-muted">累计投入</p>
            <p className="font-mono text-sm tabular-nums">{formatMoney(invested, currency)}</p>
          </div>
          <div>
            <p className="text-[10px] text-muted">当前市值</p>
            <p className="font-mono text-sm tabular-nums">
              {marketValue !== null ? formatMoney(marketValue, currency) : <span className="text-muted">—</span>}
            </p>
          </div>
          <div>
            <p className="text-[10px] text-muted">收益率</p>
            <TrendIndicator value={returnPct} size="sm" />
          </div>
        </div>

        <div className="mt-3 flex items-center justify-between text-[11px]">
          <span className="text-muted">
            {nextAt
              ? `下次投入：${formatDateTime(nextAt)}${nextAmount !== null ? ` · 建议 ${formatMoney(nextAmount, currency)}` : ""}`
              : "暂无排程"}
            {tolerance !== null ? ` · 回撤容忍 ${(tolerance * 100).toFixed(0)}%` : ""}
          </span>
          <span className={cn("text-accent/80", expanded && "rotate-180 transition-transform")} aria-hidden>
            {expanded ? "收起 ▴" : "展开详情 ▾"}
          </span>
        </div>
      </button>

      {expanded && (
        <div className="animate-fade-up space-y-4 border-t border-line bg-bg/60 px-4 py-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard
              label="执行进度"
              value={nextAt ? "按排程执行" : plan.status === "PAUSED" ? "已暂停" : "待排程"}
              sub={plan.end_date ? `期限至 ${formatDate(plan.end_date)}` : "无限期"}
              tone={plan.status === "ACTIVE" ? "up" : "muted"}
            />
            <StatCard
              label="下次建议投入"
              value={nextAmount !== null ? formatMoney(nextAmount, currency) : "—"}
              sub="结合市场状态的倍数建议"
            />
            <StatCard
              label="计划状态"
              value={status.label}
              sub={plan.updated_at ? `更新于 ${formatDateTime(plan.updated_at)}` : undefined}
            />
          </div>

          <SectionCard title="该计划交易记录">
            <PlanTransactions planId={plan.id} />
          </SectionCard>

          <div className="flex flex-wrap items-center gap-2">
            <GhostButton onClick={onEdit}>编辑计划</GhostButton>
            {(plan.status ?? "ACTIVE").toUpperCase() === "ACTIVE" ? (
              <GhostButton disabled={actionLoading} onClick={onPauseToggle}>
                暂停
              </GhostButton>
            ) : (
              <GhostButton disabled={actionLoading} onClick={onPauseToggle}>
                恢复
              </GhostButton>
            )}
            <GhostButton danger disabled={actionLoading} onClick={onDelete}>
              删除
            </GhostButton>
          </div>
        </div>
      )}
    </article>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function PlansPage() {
  const toast = useToast();
  const { data, loading, error, refresh } = useApi(
    () => apiClient.portfolio.getPlans({ page_size: 100 }),
    [],
  );
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<UserPlan | null>(null);
  const [deleting, setDeleting] = useState<UserPlan | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const plans = useMemo(() => data ?? [], [data]);

  const togglePause = async (plan: UserPlan) => {
    setActionLoading(true);
    const toPaused = (plan.status ?? "ACTIVE").toUpperCase() === "ACTIVE";
    try {
      await apiClient.portfolio.updatePlan(plan.id, { status: toPaused ? "PAUSED" : "ACTIVE" });
      toast.show("success", toPaused ? `「${plan.name}」已暂停` : `「${plan.name}」已恢复`);
      refresh();
    } catch (err) {
      toast.show("error", err instanceof Error ? err.message : "操作失败");
    } finally {
      setActionLoading(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setActionLoading(true);
    try {
      await apiClient.portfolio.deletePlan(deleting.id);
      toast.show("success", `「${deleting.name}」已删除`);
      setDeleting(null);
      refresh();
    } catch (err) {
      toast.show("error", err instanceof Error ? err.message : "删除失败");
    } finally {
      setActionLoading(false);
    }
  };

  const totalInvested = plans.reduce((acc, p) => acc + (pickNum(p, ["invested_total"]) ?? 0), 0);
  const activeCount = plans.filter((p) => (p.status ?? "ACTIVE").toUpperCase() === "ACTIVE").length;

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="01"
        kicker="Personal Ledger · Plans"
        title="我的计划"
        description="创建与管理个人 BTC 资金计划：定投频率、加仓策略与回撤容忍度。计划只是「将来打算怎么做」，实际成交需要你手工记录 —— 系统只提醒、只模拟，绝不自动交易。"
        action={
          <>
            <GhostButton onClick={refresh}>刷新</GhostButton>
            <PrimaryButton
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
            >
              + 创建计划
            </PrimaryButton>
          </>
        }
      />

      <ToastHost toast={toast.toast} />

      {loading ? (
        <div className="space-y-3">
          {[0, 1, 2].map((i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : error && !data ? (
        <ErrorState error={error as ApiRequestError} onRetry={refresh}>
          <div className="rounded-xl border border-dashed border-line px-6 py-10 text-center text-xs text-muted">
            计划数据依赖本地数据库，后端不可达时无法展示。
          </div>
        </ErrorState>
      ) : !plans.length ? (
        <EmptyState
          title="还没有资金计划"
          description="创建第一个计划：设定每月投入与加仓策略，系统会在每个投入日提醒你「今天建议投多少、为什么」。"
          action={
            <PrimaryButton
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
            >
              创建第一个计划 →
            </PrimaryButton>
          }
        />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard label="计划总数" value={plans.length} sub={`${activeCount} 个进行中`} />
            <StatCard
              label="累计投入合计"
              value={formatMoney(totalInvested)}
              sub="跨计划聚合（含初始资金）"
            />
            <StatCard
              label="执行口径"
              value="手工记账"
              sub="交易所 API 同步为预留功能，默认关闭"
              tone="muted"
            />
          </div>

          <div className="space-y-3">
            {plans.map((plan) => (
              <PlanCard
                key={plan.id}
                plan={plan}
                expanded={expandedId === plan.id}
                onToggle={() => setExpandedId(expandedId === plan.id ? null : plan.id)}
                onEdit={() => {
                  setEditing(plan);
                  setFormOpen(true);
                }}
                onPauseToggle={() => togglePause(plan)}
                onDelete={() => setDeleting(plan)}
                actionLoading={actionLoading}
              />
            ))}
          </div>
        </>
      )}

      {formOpen && (
        <PlanFormModal
          open={formOpen}
          editing={editing}
          onClose={() => setFormOpen(false)}
          onSaved={refresh}
        />
      )}

      <ConfirmDialog
        open={deleting !== null}
        title="删除计划"
        description={`确定删除「${deleting?.name ?? ""}」？该计划的历史配置将一并移除（交易账本保留）。`}
        loading={actionLoading}
        onConfirm={confirmDelete}
        onCancel={() => setDeleting(null)}
      />

      <p className="text-center text-[10px] leading-relaxed text-muted/70">
        建议金额与倍数说明来自规则引擎（docs/architecture/15 §4），任何数据故障不会中断基础定投。
      </p>
    </div>
  );
}
