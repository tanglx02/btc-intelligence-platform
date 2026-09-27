"use client";

/**
 * 加仓倍数表编辑器（规则引擎 drawdown_from_ath 梯度表的可视化编辑）。
 *
 * 默认参数 = 需求文档第二十二节：距 ATH −10% 内 ×1.0 / −20% ×1.2 / −30% ×1.5 / −40% ×2.0。
 * 与 StrategyConfig.rules（15 号文档 §4 / 14 号文档 §6.1）同一格式。
 */

import { cn } from "./utils";

export interface MultiplierRule {
  /** 距 ATH 回撤阈值（负百分数，如 -20 表示 −20%） */
  drawdown_from_ath_pct?: number;
  multiplier: number;
  [key: string]: unknown;
}

export const DEFAULT_MULTIPLIER_RULES: MultiplierRule[] = [
  { drawdown_from_ath_pct: -10, multiplier: 1.0 },
  { drawdown_from_ath_pct: -20, multiplier: 1.2 },
  { drawdown_from_ath_pct: -30, multiplier: 1.5 },
  { drawdown_from_ath_pct: -40, multiplier: 2.0 },
];

interface MultiplierEditorProps {
  rules: MultiplierRule[];
  onChange: (rules: MultiplierRule[]) => void;
  disabled?: boolean;
  /** 阈值列说明文字 */
  thresholdLabel?: string;
}

const NUM_INPUT =
  "w-full rounded-md border border-line bg-bg px-2 py-1.5 text-right font-mono text-xs tabular-nums text-[#e6edf3] outline-none transition-colors focus:border-accent/60 disabled:opacity-50";

export function MultiplierEditor({
  rules,
  onChange,
  disabled = false,
  thresholdLabel = "距 ATH 回撤达到（%）",
}: MultiplierEditorProps) {
  const update = (i: number, patch: Partial<MultiplierRule>) => {
    const next = rules.map((r, idx) => (idx === i ? { ...r, ...patch } : r));
    onChange(next);
  };

  const addRow = () => {
    const last = rules[rules.length - 1];
    const threshold = last?.drawdown_from_ath_pct !== undefined ? last.drawdown_from_ath_pct - 10 : -50;
    onChange([...rules, { drawdown_from_ath_pct: threshold, multiplier: 1.0 }]);
  };

  const removeRow = (i: number) => {
    onChange(rules.filter((_, idx) => idx !== i));
  };

  return (
    <div className="rounded-lg border border-line bg-bg/60 p-3">
      <div className="mb-2 grid grid-cols-[1fr_1fr_auto] items-center gap-2 text-[10px] font-medium uppercase tracking-wider text-muted">
        <span>{thresholdLabel}</span>
        <span>投入倍数（×）</span>
        <span className="w-7" aria-hidden />
      </div>
      <div className="space-y-2">
        {rules.map((rule, i) => (
          <div key={i} className="grid grid-cols-[1fr_1fr_auto] items-center gap-2">
            <div className="relative">
              <input
                type="number"
                step="any"
                disabled={disabled}
                value={rule.drawdown_from_ath_pct ?? ""}
                onChange={(e) =>
                  update(i, { drawdown_from_ath_pct: e.target.value === "" ? undefined : Number(e.target.value) })
                }
                className={cn(NUM_INPUT, "pr-6")}
                aria-label={`第 ${i + 1} 档回撤阈值`}
              />
              <span aria-hidden className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted">
                %
              </span>
            </div>
            <div className="relative">
              <input
                type="number"
                step="0.1"
                min={0}
                disabled={disabled}
                value={rule.multiplier}
                onChange={(e) => update(i, { multiplier: Number(e.target.value) })}
                className={cn(NUM_INPUT, "pr-7")}
                aria-label={`第 ${i + 1} 档投入倍数`}
              />
              <span aria-hidden className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-muted">
                ×
              </span>
            </div>
            <button
              type="button"
              disabled={disabled || rules.length <= 1}
              onClick={() => removeRow(i)}
              aria-label="删除该档"
              className="h-7 w-7 rounded-md border border-line text-xs text-muted transition-colors hover:border-down/40 hover:text-down disabled:opacity-30"
            >
              ✕
            </button>
          </div>
        ))}
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={addRow}
        className="mt-2.5 rounded-md border border-dashed border-line px-2.5 py-1 text-[11px] text-muted transition-colors hover:border-accent/40 hover:text-accent disabled:opacity-40"
      >
        + 添加一档
      </button>
      <p className="mt-2 text-[10px] leading-relaxed text-muted/70">
        阈值按「回撤达到该档或更深」逐级匹配；投入金额 = 基础金额 × 倍数，多规则命中时连乘并受单次上限约束。
      </p>
    </div>
  );
}
