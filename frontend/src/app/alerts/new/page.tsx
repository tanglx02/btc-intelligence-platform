"use client";

/**
 * 创建预警规则（/alerts/new）：三步向导。
 *
 * Step 1 选择模板（12 个预设模板 + 从空白开始）
 * Step 2 配置规则（普通 / 高级双模式表单 + 实时预览）
 * Step 3 预览与保存（摘要 + 本地结构校验 + POST /alerts/rules）
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { apiClient } from "@/lib/api";
import { useApi } from "@/hooks/useApi";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

import { RuleForm, emptyRuleFormValue } from "@/components/alerts/RuleForm";
import { SeverityBadge5 } from "@/components/alerts/badges";
import {
  defaultSimple,
  extractSimple,
  firstLeaf,
  nodeToHumanText,
  validateTree,
} from "@/components/alerts/conditionUtils";
import { formatDuration } from "@/components/alerts/format";
import { Banner, PageHeader, cardCls } from "@/components/alerts/ui";
import { asAlertList } from "@/components/alerts/types";
import type {
  AlertTemplateRecord,
  RuleFormPayload,
  RuleFormValue,
  SimpleCondition,
} from "@/components/alerts/types";

/* -------------------------------------------------------------------------- */
/* 步骤条                                                                      */
/* -------------------------------------------------------------------------- */

const STEPS = [
  { n: 1, label: "选择模板" },
  { n: 2, label: "配置规则" },
  { n: 3, label: "预览保存" },
] as const;

function Stepper({ current }: { current: 1 | 2 | 3 }) {
  return (
    <ol className="flex items-center gap-0 text-xs">
      {STEPS.map((s, i) => (
        <li key={s.n} className="flex items-center">
          <div
            className={`flex items-center gap-2 rounded-full border px-3 py-1.5 transition-colors ${
              current === s.n
                ? "border-accent/60 bg-accent/10 text-accent"
                : current > s.n
                  ? "border-up/50 bg-up/10 text-up"
                  : "border-line bg-bg-raised text-muted"
            }`}
          >
            <span className="flex h-5 w-5 items-center justify-center rounded-full border border-current font-mono text-[10px]">
              {current > s.n ? "✓" : s.n}
            </span>
            {s.label}
          </div>
          {i < STEPS.length - 1 && (
            <span
              aria-hidden
              className={`mx-2 h-px w-8 sm:w-14 ${current > s.n ? "bg-up/50" : "bg-line"}`}
            />
          )}
        </li>
      ))}
    </ol>
  );
}

/* -------------------------------------------------------------------------- */
/* 模板预填充                                                                   */
/* -------------------------------------------------------------------------- */

function valueFromTemplate(t: AlertTemplateRecord): RuleFormValue {
  const tree = t.defaults?.condition_tree ?? null;
  let simple: SimpleCondition | null = tree ? extractSimple(tree) : null;
  if (!simple && tree) simple = extractSimple(firstLeaf(tree));
  const isComposite = tree !== null && simple === null;

  return {
    ...emptyRuleFormValue(),
    rule_name: t.name,
    description: t.description ?? "",
    simple: simple ?? defaultSimple(),
    tree,
    tab: isComposite ? "advanced" : "simple",
  };
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function NewAlertRulePage() {
  const router = useRouter();
  const templates = useApi<AlertTemplateRecord[]>(
    () => asAlertList<AlertTemplateRecord>(apiClient.alerts.getTemplates()),
    [],
  );

  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [formValue, setFormValue] = useState<RuleFormValue | null>(null);
  const [payload, setPayload] = useState<RuleFormPayload | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [checkResult, setCheckResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [activeTemplate, setActiveTemplate] = useState<AlertTemplateRecord | null>(null);

  const pickTemplate = (t: AlertTemplateRecord) => {
    setActiveTemplate(t);
    setFormValue(valueFromTemplate(t));
    setStep(2);
  };

  const startBlank = () => {
    setActiveTemplate(null);
    setFormValue(emptyRuleFormValue("我的预警规则"));
    setStep(2);
  };

  const handleFormSubmit = (p: RuleFormPayload) => {
    setPayload(p);
    setCheckResult(null);
    setSaveError(null);
    setStep(3);
  };

  const handleSave = async () => {
    if (!payload) return;
    setSaving(true);
    setSaveError(null);
    try {
      const res = await apiClient.alerts.createRule(payload as unknown as Record<string, unknown>);
      if (res.success) {
        router.push("/alerts");
        return;
      }
      setSaveError("保存失败，请检查表单后重试");
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  };

  const handleValidate = () => {
    if (!payload) return;
    const err = validateTree(payload.condition_tree);
    setCheckResult(
      err
        ? { ok: false, text: err }
        : { ok: true, text: "条件结构合法：指标、阈值与组合逻辑均通过校验。保存后可在规则列表用 ⚡ 测试实时命中。" },
    );
  };

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        eyebrow="New Alert Rule"
        title="创建预警规则"
        description="三步创建：选择模板 → 配置条件与提醒策略 → 预览保存。所有条件支持实时人类可读预览。"
        actions={
          <Link href="/alerts" className="text-xs text-muted transition-colors hover:text-accent">
            ← 返回预警中心
          </Link>
        }
      />

      <div className="animate-fade-up">
        <Stepper current={step} />
      </div>

      {/* ------------------------------ Step 1 ------------------------------ */}
      {step === 1 && (
        <section className="animate-fade-up">
          {templates.error && (
            <ErrorState error={templates.error} lastUpdatedAt={templates.lastUpdatedAt} onRetry={templates.refresh} />
          )}

          {templates.loading && !templates.data ? (
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, i) => (
                <LoadingSkeleton key={i} variant="card" />
              ))}
            </div>
          ) : (templates.data ?? []).length === 0 ? (
            <EmptyState
              title="模板加载失败"
              description="预设模板暂时不可用，你可以直接从空白开始创建。"
              action={
                <button type="button" className="text-xs text-accent hover:underline" onClick={startBlank}>
                  跳过，从空白开始 →
                </button>
              }
            />
          ) : (
            <>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {(templates.data ?? []).map((t, i) => (
                  <button
                    key={t.id}
                    type="button"
                    onClick={() => pickTemplate(t)}
                    className="animate-fade-up group flex flex-col items-start gap-2 rounded-xl border border-line bg-bg-raised p-4 text-left transition-all duration-200 hover:-translate-y-0.5 hover:border-accent/50 hover:shadow-[0_8px_28px_rgba(88,166,255,0.08)]"
                    style={{ animationDelay: `${Math.min(i * 45, 360)}ms` }}
                  >
                    <div className="flex w-full items-center justify-between">
                      <span aria-hidden className="text-xl">
                        {t.icon ?? "◇"}
                      </span>
                      {t.category && (
                        <span className="rounded border border-line px-1.5 py-0.5 font-mono text-[9px] uppercase tracking-wider text-muted">
                          {t.category}
                        </span>
                      )}
                    </div>
                    <div>
                      <p className="text-sm font-semibold text-[#e6edf3] group-hover:text-accent">{t.name}</p>
                      <p className="mt-1 text-[11px] leading-relaxed text-muted">{t.description}</p>
                    </div>
                    {t.defaults?.condition_tree && (
                      <p className="mt-auto w-full border-t border-line pt-2 text-[10px] leading-relaxed text-muted/80">
                        预设条件：{nodeToHumanText(t.defaults.condition_tree)}
                      </p>
                    )}
                  </button>
                ))}
              </div>

              <button
                type="button"
                onClick={startBlank}
                className="mt-4 w-full rounded-xl border border-dashed border-line px-4 py-3.5 text-xs text-muted transition-colors hover:border-accent/50 hover:text-accent"
              >
                跳过，从空白开始（构建自定义条件树）→
              </button>
            </>
          )}
        </section>
      )}

      {/* ------------------------------ Step 2 ------------------------------ */}
      {step === 2 && formValue && (
        <section className="animate-fade-up">
          {activeTemplate && (
            <div className={`${cardCls} mb-4 flex items-center gap-3 px-4 py-3`}>
              <span aria-hidden className="text-lg">{activeTemplate.icon}</span>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-medium text-[#e6edf3]">
                  基于模板「{activeTemplate.name}」，已预填默认参数，可自由调整
                </p>
              </div>
              <button
                type="button"
                onClick={() => setStep(1)}
                className="shrink-0 text-[11px] text-accent hover:underline"
              >
                重新选择
              </button>
            </div>
          )}

          <div className={`${cardCls} px-4 py-5 sm:px-6`}>
            <RuleForm
              value={formValue}
              onChange={setFormValue}
              onSubmit={handleFormSubmit}
              submitLabel="下一步：预览 →"
              notice={
                <p className="rounded-lg border border-accent/30 bg-accent/[0.06] px-3 py-2 text-[11px] leading-relaxed text-accent/90">
                  提示：每个监测数据项旁的 ℹ️ 图标可查看该指标的含义；底部的条件预览会随表单实时更新。
                </p>
              }
            />
          </div>
        </section>
      )}

      {/* ------------------------------ Step 3 ------------------------------ */}
      {step === 3 && payload && (
        <section className="animate-fade-up flex flex-col gap-4">
          <div className={cardCls}>
            <div className="border-b border-line px-4 py-3">
              <h2 className="text-sm font-semibold text-[#e6edf3]">规则摘要</h2>
            </div>
            <dl className="grid gap-x-6 gap-y-3 px-4 py-4 text-xs sm:grid-cols-2">
              <div>
                <dt className="text-muted">规则名称</dt>
                <dd className="mt-1 font-medium text-[#e6edf3]">{payload.rule_name}</dd>
              </div>
              <div>
                <dt className="text-muted">提醒等级</dt>
                <dd className="mt-1">
                  <SeverityBadge5 severity={payload.severity} />
                </dd>
              </div>
              <div className="sm:col-span-2">
                <dt className="text-muted">触发条件</dt>
                <dd className="mt-1 rounded-lg border border-accent/25 bg-accent/[0.04] px-3 py-2 leading-relaxed text-[#e6edf3]">
                  当 {nodeToHumanText(payload.condition_tree)}
                  {payload.duration_seconds ? ` 持续 ${formatDuration(payload.duration_seconds)}` : ""}
                  {payload.consecutive_count > 1 ? ` 且连续 ${payload.consecutive_count} 次满足` : ""}
                  {payload.channels.includes("EMAIL") ? "，通过 Email 通知" : "（仅记录事件）"}
                </dd>
              </div>
              <div>
                <dt className="text-muted">冷却时间</dt>
                <dd className="mt-1 font-mono tabular-nums">{formatDuration(payload.cooldown_seconds)}</dd>
              </div>
              <div>
                <dt className="text-muted">持续 / 连续</dt>
                <dd className="mt-1 font-mono tabular-nums">
                  {formatDuration(payload.duration_seconds)} · 连续 {payload.consecutive_count} 次
                </dd>
              </div>
            </dl>
          </div>

          {checkResult && (
            <Banner tone={checkResult.ok ? "success" : "error"}>{checkResult.text}</Banner>
          )}
          {saveError && <Banner tone="error">{saveError}</Banner>}

          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setStep(2)}
              className="rounded-lg border border-line bg-bg-raised px-3.5 py-1.5 text-xs text-[#e6edf3] transition-colors hover:bg-bg-hover"
              disabled={saving}
            >
              ← 返回编辑
            </button>
            <button
              type="button"
              onClick={handleValidate}
              className="rounded-lg border border-line bg-bg-raised px-3.5 py-1.5 text-xs text-[#e6edf3] transition-colors hover:border-accent/50 hover:text-accent"
              disabled={saving}
            >
              校验条件
            </button>
            <span className="mx-1 hidden text-[10px] text-muted sm:inline">
              保存后可在规则列表用 ⚡ 测试实时命中
            </span>
            <button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving}
              className="ml-auto rounded-lg border border-up/50 bg-up/15 px-4 py-1.5 text-xs font-medium text-up transition-colors hover:bg-up/25 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {saving ? "保存中…" : "✓ 保存规则"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
