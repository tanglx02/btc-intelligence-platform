"use client";

/**
 * 预警规则表单（创建 / 编辑共用）。
 *
 * - 普通模式：单叶子条件表单（监测数据 + 条件 + 阈值）
 * - 高级模式：ConditionNodeEditor 递归条件树
 * - 底部实时人类可读预览（condition_text 风格）
 * - 提交前本地校验，通过后回调 onSubmit(构建后的请求体)
 */

import { useState, type ReactNode } from "react";

import { SeverityBadge } from "@/components/common/SeverityBadge";

import { ConditionNodeEditor } from "./ConditionNodeEditor";
import {
  CONDITION_MAP,
  CONDITION_OPTIONS,
  COOLDOWN_PRESETS,
  DURATION_PRESETS,
  METRIC_MAP,
  METRIC_OPTIONS,
  SEVERITY_OPTIONS,
} from "./constants";
import {
  buildFromSimple,
  buildRulePayload,
  countLeaves,
  defaultSimple,
  describeRuleText,
  extractSimple,
  nodeToHumanText,
  validateTree,
} from "./conditionUtils";
import { formatDuration } from "./format";
import type { ConditionNode, RuleFormPayload, RuleFormValue, Severity4, SimpleCondition } from "./types";
import { Field, InfoHint, btnPrimary, inputCls, selectCls } from "./ui";

interface RuleFormProps {
  value: RuleFormValue;
  onChange: (value: RuleFormValue) => void;
  onSubmit: (payload: RuleFormPayload) => void | Promise<void>;
  submitting?: boolean;
  /** 外部错误（API 返回） */
  error?: string | null;
  submitLabel: string;
  /** 编辑页附加操作（暂停 / 恢复 / 删除 / 测试） */
  extraActions?: ReactNode;
  /** 附加说明（如向导第二步提示） */
  notice?: ReactNode;
}

export function emptyRuleFormValue(name = ""): RuleFormValue {
  return {
    rule_name: name,
    description: "",
    severity: "WARNING",
    simple: defaultSimple(),
    tree: null,
    tab: "simple",
    durationSeconds: null,
    consecutiveCount: 1,
    cooldownPreset: "24h",
    cooldownCustom: "",
    emailEnabled: true,
  };
}

export function RuleForm({
  value,
  onChange,
  onSubmit,
  submitting = false,
  error,
  submitLabel,
  extraActions,
  notice,
}: RuleFormProps) {
  const [formError, setFormError] = useState<string | null>(null);
  const [tabWarning, setTabWarning] = useState<string | null>(null);

  const patch = (p: Partial<RuleFormValue>) => {
    setFormError(null);
    onChange({ ...value, ...p });
  };
  const patchSimple = (p: Partial<SimpleCondition>) => {
    setFormError(null);
    onChange({ ...value, simple: { ...value.simple, ...p } });
  };

  const activeTree: ConditionNode =
    value.tab === "advanced" ? (value.tree ?? buildFromSimple(value.simple)) : buildFromSimple(value.simple);

  const handleTab = (next: "simple" | "advanced") => {
    setTabWarning(null);
    if (next === value.tab) return;
    if (next === "advanced") {
      // 进入高级模式：以普通模式当前条件为种子
      onChange({ ...value, tab: "advanced", tree: buildFromSimple(value.simple) });
      return;
    }
    // 退回普通模式：仅当条件树是单叶子条件时可无损转换
    const simple = extractSimple(value.tree);
    if (simple) {
      onChange({ ...value, tab: "simple", simple });
    } else {
      setTabWarning("当前条件树包含 AND/OR/NOT 组合逻辑，普通模式无法完整表达。请继续在高级模式编辑。");
    }
  };

  const handleSubmit = () => {
    const err = validateTree(activeTree);
    if (err) {
      setFormError(err);
      return;
    }
    if (!value.rule_name.trim()) {
      setFormError("请填写规则名称");
      return;
    }
    void onSubmit(buildRulePayload(value, activeTree));
  };

  const metricMeta = METRIC_MAP[value.simple.metric];
  const conditionMeta = value.simple.condition ? CONDITION_MAP[value.simple.condition] : undefined;
  const isEngineMetric = Boolean(metricMeta?.engine);
  const kind = conditionMeta?.kind ?? "threshold";

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
    >
      {notice}

      {/* 模式切换 */}
      <div className="flex items-center gap-1 rounded-lg border border-line bg-bg-hover/50 p-1 text-xs">
        {(["simple", "advanced"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => handleTab(t)}
            aria-pressed={value.tab === t}
            className={`flex-1 rounded-md px-3 py-1.5 font-medium transition-colors ${
              value.tab === t ? "bg-accent/15 text-accent" : "text-muted hover:text-[#e6edf3]"
            }`}
          >
            {t === "simple" ? "普通模式" : "高级模式（条件树）"}
          </button>
        ))}
      </div>
      {tabWarning && (
        <p className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-xs leading-relaxed text-warn">
          {tabWarning}
        </p>
      )}

      {/* 基本信息 */}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="规则名称" required>
          <input
            className={inputCls}
            value={value.rule_name}
            onChange={(e) => patch({ rule_name: e.target.value })}
            placeholder="如：BTC 跌破关键支撑"
            maxLength={100}
          />
        </Field>
        <Field label="描述（可选）">
          <input
            className={inputCls}
            value={value.description}
            onChange={(e) => patch({ description: e.target.value })}
            placeholder="补充说明规则的用途"
            maxLength={200}
          />
        </Field>
      </div>

      {/* 条件编辑区 */}
      {value.tab === "simple" ? (
        <section className="flex flex-col gap-4 rounded-xl border border-line bg-bg-hover/20 p-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="监测数据">
              <div className="flex items-center gap-1.5">
                <select
                  className={selectCls}
                  value={value.simple.metric}
                  onChange={(e) => {
                    const meta = METRIC_MAP[e.target.value];
                    patchSimple({
                      metric: e.target.value,
                      condition: meta?.engine ? "state_change" : value.simple.condition === "state_change" ? "lt" : value.simple.condition,
                    });
                  }}
                >
                  {METRIC_OPTIONS.map((m) => (
                    <option key={m.value} value={m.value}>
                      {m.label}
                    </option>
                  ))}
                </select>
                {metricMeta && <InfoHint text={metricMeta.hint} />}
              </div>
            </Field>

            {!isEngineMetric && (
              <Field label="条件">
                <select
                  className={selectCls}
                  value={value.simple.condition}
                  onChange={(e) => patchSimple({ condition: e.target.value as SimpleCondition["condition"] })}
                >
                  {CONDITION_OPTIONS.filter((c) => c.kind !== "state").map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </Field>
            )}
          </div>

          {/* 数值输入 */}
          <div className="grid gap-4 sm:grid-cols-2">
            {value.simple.condition === "state_change" ? (
              <Field
                label="目标状态（可选）"
                hint="留空表示任意状态变化都会提醒；填写后仅在该目标状态出现时提醒，如 UPTREND。"
              >
                <input
                  className={`${inputCls} font-mono`}
                  value={value.simple.toState}
                  onChange={(e) => patchSimple({ toState: e.target.value })}
                  placeholder="如 UPTREND（留空 = 任意变化）"
                />
              </Field>
            ) : kind === "range" ? (
              <>
                <Field label="区间下限" required>
                  <input
                    type="number"
                    step="any"
                    className={`${inputCls} font-mono`}
                    value={value.simple.low}
                    onChange={(e) => patchSimple({ low: e.target.value })}
                  />
                </Field>
                <Field label="区间上限" required>
                  <input
                    type="number"
                    step="any"
                    className={`${inputCls} font-mono`}
                    value={value.simple.high}
                    onChange={(e) => patchSimple({ high: e.target.value })}
                  />
                </Field>
              </>
            ) : kind === "percentile" ? (
              <Field
                label="历史分位阈值"
                required
                hint="当前值在过去全部历史中的分位位置，如 95 表示仅高于历史上 95% 的时间。"
              >
                <input
                  type="number"
                  step="any"
                  min={0}
                  max={100}
                  className={`${inputCls} font-mono`}
                  value={value.simple.percentile}
                  onChange={(e) => patchSimple({ percentile: e.target.value })}
                  placeholder="0 ~ 100"
                />
              </Field>
            ) : (
              <>
                {kind === "change" && (
                  <Field label="变化窗口">
                    <select
                      className={selectCls}
                      value={value.simple.windowHours}
                      onChange={(e) => patchSimple({ windowHours: Number(e.target.value) })}
                    >
                      <option value={1}>1 小时</option>
                      <option value={24}>24 小时</option>
                      <option value={168}>7 天</option>
                    </select>
                  </Field>
                )}
                <Field label={kind === "change" ? "涨跌幅阈值" : "阈值"} required>
                  <div className="flex items-center gap-1.5">
                    <input
                      type="number"
                      step="any"
                      className={`${inputCls} font-mono`}
                      value={value.simple.value}
                      onChange={(e) => patchSimple({ value: e.target.value })}
                      placeholder={conditionMeta?.placeholder ?? "阈值"}
                    />
                    {metricMeta?.unit && <span className="text-[10px] text-muted">{metricMeta.unit}</span>}
                  </div>
                </Field>
              </>
            )}
          </div>
        </section>
      ) : (
        <section className="flex flex-col gap-3 rounded-xl border border-line bg-bg-hover/20 p-4">
          <p className="text-[11px] leading-relaxed text-muted">
            组合条件：AND（全部满足才触发）/ OR（任一满足即触发）/ NOT（取反）。最多嵌套 5 层。
          </p>
          <ConditionNodeEditor
            node={activeTree}
            onChange={(tree) => {
              setFormError(null);
              onChange({ ...value, tree });
            }}
            depth={1}
            path="root"
          />
        </section>
      )}

      {/* 触发策略 */}
      <section className="grid gap-4 rounded-xl border border-line bg-bg-hover/20 p-4 sm:grid-cols-2">
        <Field
          label="持续时间"
          hint="条件需要持续满足多久才提醒，用于过滤瞬时波动。"
        >
          <select
            className={selectCls}
            value={value.durationSeconds === null ? "none" : String(value.durationSeconds)}
            onChange={(e) =>
              patch({
                durationSeconds: e.target.value === "none" ? null : Number(e.target.value),
              })
            }
          >
            {DURATION_PRESETS.map((p) => (
              <option key={p.key} value={p.seconds === null ? "none" : String(p.seconds)}>
                {p.label}
              </option>
            ))}
          </select>
        </Field>
        <Field
          label="连续满足次数"
          hint="连续 N 次扫描周期都满足才触发，进一步降低误报（1 = 立即触发）。"
        >
          <input
            type="number"
            min={1}
            max={10}
            className={`${inputCls} font-mono`}
            value={value.consecutiveCount}
            onChange={(e) => {
              const n = Math.floor(Number(e.target.value));
              patch({ consecutiveCount: Math.min(10, Math.max(1, Number.isFinite(n) ? n : 1)) });
            }}
          />
        </Field>
      </section>

      {/* 提醒等级 */}
      <section>
        <p className="mb-2 text-[11px] font-medium text-muted">提醒等级</p>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          {SEVERITY_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => patch({ severity: opt.value as Severity4 })}
              aria-pressed={value.severity === opt.value}
              className={`flex flex-col items-start gap-1.5 rounded-lg border px-3 py-2.5 text-left transition-colors ${
                value.severity === opt.value
                  ? "border-accent/60 bg-accent/10"
                  : "border-line bg-bg-raised hover:border-muted/50"
              }`}
            >
              <SeverityBadge severity={opt.value} />
              <span className="text-[10px] leading-snug text-muted">{opt.desc}</span>
            </button>
          ))}
        </div>
      </section>

      {/* 通知渠道 */}
      <section className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-bg-hover/20 p-4">
        <p className="text-[11px] font-medium text-muted">通知方式</p>
        <label className="flex cursor-pointer items-center gap-2 text-xs text-[#e6edf3]">
          <input
            type="checkbox"
            checked={value.emailEnabled}
            onChange={(e) => patch({ emailEnabled: e.target.checked })}
            className="h-3.5 w-3.5 accent-[#58a6ff]"
          />
          Email 邮件
        </label>
        {!value.emailEnabled && (
          <span className="text-[11px] text-warn">未选择任何渠道时将仅记录事件、不发送通知</span>
        )}
        <span className="ml-auto text-[11px] text-muted">
          邮件发送配置在
          <a href="/alerts/channels" className="mx-1 text-accent hover:underline">
            通知渠道
          </a>
          页面管理
        </span>
      </section>

      {/* 冷却时间 */}
      <Field
        label="冷却时间"
        hint="触发一次后，在冷却期内即使条件再次满足也不会重复发送，避免骚扰。"
      >
        <div className="flex items-center gap-2">
          <select
            className={`${selectCls} sm:w-56`}
            value={value.cooldownPreset}
            onChange={(e) => patch({ cooldownPreset: e.target.value })}
          >
            {COOLDOWN_PRESETS.map((p) => (
              <option key={p.key} value={p.key}>
                {p.label}
              </option>
            ))}
          </select>
          {value.cooldownPreset === "custom" && (
            <div className="flex items-center gap-1.5">
              <input
                type="number"
                min={60}
                step={60}
                className={`${inputCls} w-32 font-mono`}
                value={value.cooldownCustom}
                onChange={(e) => patch({ cooldownCustom: e.target.value })}
                placeholder="秒数"
              />
              <span className="text-[10px] text-muted">
                {value.cooldownCustom ? `≈ ${formatDuration(Number(value.cooldownCustom))}` : "秒"}
              </span>
            </div>
          )}
        </div>
      </Field>

      {/* 实时预览 */}
      <div className="relative overflow-hidden rounded-xl border border-accent/25 bg-accent/[0.04]">
        <div aria-hidden className="absolute inset-y-0 left-0 w-0.5 bg-gradient-to-b from-accent/60 to-transparent" />
        <div className="px-4 py-3">
          <p className="flex items-center justify-between text-[10px] font-medium uppercase tracking-wider text-accent/80">
            条件预览
            <span className="font-mono normal-case tracking-normal text-muted">
              {countLeaves(activeTree)} 个叶子条件
            </span>
          </p>
          <p className="mt-1.5 text-sm leading-relaxed text-[#e6edf3]">
            {describeRuleText(value, activeTree)}
          </p>
          <p className="mt-1 font-mono text-[10px] leading-relaxed text-muted">
            AST: {nodeToHumanText(activeTree)}
          </p>
        </div>
      </div>

      {/* 错误与提交 */}
      {(formError || error) && (
        <p className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-xs text-down" role="alert">
          {formError ?? error}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
        {extraActions}
        <button type="submit" className={btnPrimary} disabled={submitting}>
          {submitting ? "处理中…" : submitLabel}
        </button>
      </div>
    </form>
  );
}

/** 「从空白开始」默认值的便捷导出（供向导跳过模板时使用） */
export const blankRuleFormValue = emptyRuleFormValue;
