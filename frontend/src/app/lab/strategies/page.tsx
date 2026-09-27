"use client";

/**
 * 策略实验室 —— 策略模板浏览、参数微调与多策略对比回测
 * （docs/architecture/14 §6、16 §8.18）。
 *
 * 内置策略（可复制调整）+ 参数表单 → 多选（≤5）→ 逐个 POST /backtest/run
 * → 轮询完成 → POST /backtest/compare（尽力）→ 指标对比表 + 净值叠加图。
 * 对比端点不可用时自动降级为本地结果对比。
 */

import { useMemo, useState } from "react";

import { MultiLineChart, SERIES_PALETTE, type MultiSeries } from "@/components/portfolio/charts";
import { Disclaimer } from "@/components/portfolio/Disclaimer";
import { Field, NumberInput } from "@/components/portfolio/Form";
import { GhostButton, PrimaryButton } from "@/components/portfolio/Modal";
import {
  DEFAULT_MULTIPLIER_RULES,
  MultiplierEditor,
  type MultiplierRule,
} from "@/components/portfolio/MultiplierEditor";
import { Badge, PageHeader, SectionCard } from "@/components/portfolio/StatCard";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import {
  cn,
  formatBtc,
  formatDate,
  formatMoney,
  formatPct,
  pickNum,
  pickStr,
  strategyLabel,
  todayISO,
} from "@/components/portfolio/utils";
import { useApi } from "@/hooks/useApi";
import { apiClient, type ApiRequestError } from "@/lib/api";
import type { BacktestResult } from "@/types/api";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

/* -------------------------------------------------------------------------- */
/* 内置策略模板（14 号文档 §6.2；后端模板列表加载失败时兜底）                    */
/* -------------------------------------------------------------------------- */

interface StrategyCard {
  code: string;
  name: string;
  desc: string;
  editable: boolean;
  builtin: boolean;
}

const BUILTIN_STRATEGIES: StrategyCard[] = [
  { code: "fixed", name: "固定定投", desc: "纯 DCA 基准策略：每期固定金额，无任何条件规则。对比其他策略的锚点。", editable: false, builtin: true },
  { code: "dip_ath", name: "逢跌加仓", desc: "ATH 回撤梯度加仓：−10% ×1.0 / −20% ×1.2 / −30% ×1.5 / −40% ×2.0，倍数表可调。", editable: true, builtin: true },
  { code: "dip_recent", name: "回撤加仓", desc: "距近 90 日高点回撤达阈值加仓，独立于 ATH 口径，更贴近中期波段。", editable: true, builtin: true },
  { code: "value", name: "估值定投", desc: "MVRV/NUPL 分位低估加倍、高估减半，基于链上估值状态的逆向投入。", editable: false, builtin: true },
  { code: "risk_adjust", name: "风险平价", desc: "按 Risk Overall 反向缩放投入：VERY_LOW ×1.3 → EXTREME ×0.2。", editable: false, builtin: true },
  { code: "cycle_adjust", name: "周期调整", desc: "按周期阶段映射倍数：底部构筑 ×1.5 / 加速 ×0.6 / 顶部风险 ×0.2。", editable: false, builtin: true },
];

const COMPARE_LIMIT = 5;
const POLL_INTERVAL_MS = 2500;

function mergeStrategies(remote: Record<string, unknown>[] | null): StrategyCard[] {
  if (!remote?.length) return BUILTIN_STRATEGIES;
  const builtinCodes = new Set(BUILTIN_STRATEGIES.map((s) => s.code));
  const extras: StrategyCard[] = [];
  for (const s of remote) {
    const code = pickStr(s, ["code", "name", "strategy", "id"]);
    if (!code || builtinCodes.has(code)) continue;
    extras.push({
      code,
      name: strategyLabel(code),
      desc: pickStr(s, ["description", "desc"]) ?? "来自回测服务的策略模板。",
      editable: Array.isArray(s.rules),
      builtin: false,
    });
  }
  return [...BUILTIN_STRATEGIES, ...extras];
}

/* -------------------------------------------------------------------------- */
/* 轮询单个回测任务                                                            */
/* -------------------------------------------------------------------------- */

function pollRun(runId: string, maxAttempts = 120): Promise<BacktestResult | null> {
  return new Promise((resolve) => {
    let attempts = 0;
    const tick = async () => {
      attempts += 1;
      try {
        const res = await apiClient.backtest.getRun(runId);
        const run = res.data;
        const status = (run?.status ?? "").toUpperCase();
        if (status === "DONE" || status === "COMPLETED") {
          resolve(run);
          return;
        }
        if (status === "FAILED" || status === "CANCELLED") {
          resolve(null);
          return;
        }
      } catch {
        /* 网络抖动：计入 attempts 后继续 */
      }
      if (attempts >= maxAttempts) {
        resolve(null);
        return;
      }
      setTimeout(tick, POLL_INTERVAL_MS);
    };
    setTimeout(tick, POLL_INTERVAL_MS);
  });
}

/* -------------------------------------------------------------------------- */
/* 对比指标行定义                                                              */
/* -------------------------------------------------------------------------- */

interface MetricRow {
  label: string;
  get: (m: Record<string, unknown> | null) => string;
  /** 数值越大越好的方向标记（用于排序高亮） */
  higherBetter?: boolean;
  raw?: (m: Record<string, unknown> | null) => number | null;
}

const METRIC_ROWS: MetricRow[] = [
  {
    label: "最终资产",
    get: (m) => formatMoney(pickNum(m, ["final_value"])),
    higherBetter: true,
    raw: (m) => pickNum(m, ["final_value"]),
  },
  {
    label: "总收益率",
    get: (m) => (pickNum(m, ["total_return_pct"]) !== null ? formatPct(pickNum(m, ["total_return_pct"])) : "—"),
    higherBetter: true,
    raw: (m) => pickNum(m, ["total_return_pct"]),
  },
  {
    label: "年化（CAGR）",
    get: (m) => (pickNum(m, ["annualized_return_pct", "cagr_pct"]) !== null ? formatPct(pickNum(m, ["annualized_return_pct", "cagr_pct"])) : "—"),
    higherBetter: true,
    raw: (m) => pickNum(m, ["annualized_return_pct", "cagr_pct"]),
  },
  {
    label: "最大回撤",
    get: (m) => (pickNum(m, ["max_drawdown_pct"]) !== null ? formatPct(pickNum(m, ["max_drawdown_pct"]), { sign: false }) : "—"),
    // 回撤为负百分数：数值更大（更接近 0）代表回撤更小，即更优
    higherBetter: true,
    raw: (m) => pickNum(m, ["max_drawdown_pct"]),
  },
  { label: "Sharpe", get: (m) => (pickNum(m, ["sharpe"]) !== null ? pickNum(m, ["sharpe"])!.toFixed(2) : "—"), higherBetter: true, raw: (m) => pickNum(m, ["sharpe"]) },
  { label: "Sortino", get: (m) => (pickNum(m, ["sortino"]) !== null ? pickNum(m, ["sortino"])!.toFixed(2) : "—"), higherBetter: true, raw: (m) => pickNum(m, ["sortino"]) },
  {
    label: "胜率",
    get: (m) => {
      const w = pickNum(m, ["win_rate"]);
      if (w === null) return "—";
      return `${(w <= 1 ? w * 100 : w).toFixed(1)}%`;
    },
    higherBetter: true,
  },
  { label: "累计手续费", get: (m) => formatMoney(pickNum(m, ["total_fees"])) },
  { label: "期末 BTC", get: (m) => formatBtc(pickNum(m, ["btc_amount"])), higherBetter: true, raw: (m) => pickNum(m, ["btc_amount"]) },
  { label: "平均成本", get: (m) => formatMoney(pickNum(m, ["avg_cost"])) },
];

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function StrategyLabPage() {
  const toast = useToast();
  const strategiesApi = useApi(() => apiClient.backtest.getStrategies(), []);
  const strategies = useMemo(() => mergeStrategies(strategiesApi.data), [strategiesApi.data]);

  /* 对比选择与参数 */
  const [selected, setSelected] = useState<string[]>(["fixed", "dip_ath"]);
  const [rules, setRules] = useState<MultiplierRule[]>(DEFAULT_MULTIPLIER_RULES);
  const [initialCapital, setInitialCapital] = useState("0");
  const [periodAmount, setPeriodAmount] = useState("1000");
  const [startDate, setStartDate] = useState("2019-01-01");
  const [endDate, setEndDate] = useState(todayISO());

  const [comparing, setComparing] = useState(false);
  const [progress, setProgress] = useState("");
  const [compareError, setCompareError] = useState<string | null>(null);
  const [results, setResults] = useState<BacktestResult[]>([]);
  const [serverCompared, setServerCompared] = useState(false);

  const hasEditableSelected = selected.some((code) => strategies.find((s) => s.code === code)?.editable);

  const toggleSelect = (code: string) => {
    setSelected((prev) => {
      if (prev.includes(code)) return prev.filter((c) => c !== code);
      if (prev.length >= COMPARE_LIMIT) {
        toast.show("error", `最多同时对比 ${COMPARE_LIMIT} 个策略`);
        return prev;
      }
      return [...prev, code];
    });
  };

  const runCompare = async () => {
    if (!selected.length) {
      toast.show("error", "请先勾选至少一个策略");
      return;
    }
    setComparing(true);
    setCompareError(null);
    setResults([]);
    const collected: BacktestResult[] = [];
    try {
      for (const code of selected) {
        setProgress(`提交「${strategyLabel(code)}」回测…`);
        const res = await apiClient.backtest.run({
          strategy: code,
          initial_capital: Number(initialCapital) || 0,
          monthly_amount: Number(periodAmount) || 0,
          frequency: "monthly",
          start_date: startDate,
          end_date: endDate,
          currency: "CNY",
          fee_rate: 0.001,
          slippage_bps: 5,
          rules: strategies.find((s) => s.code === code)?.editable ? rules : [],
        });
        const runId = res.data?.run_id;
        if (!runId) throw new Error(`「${strategyLabel(code)}」未返回 run_id`);
        setProgress(`运行「${strategyLabel(code)}」回测…`);
        const done = await pollRun(runId);
        if (done) collected.push(done);
      }
      if (!collected.length) throw new Error("所有回测均失败或超时，请检查参数与后端服务");
      setResults(collected);

      /* 对比端点尽力而为：失败时本地已有完整结果，直接本地对比 */
      setProgress("生成对比报告…");
      try {
        await apiClient.backtest.compare(collected.map((r) => r.run_id));
        setServerCompared(true);
      } catch {
        setServerCompared(false);
      }
      toast.show("success", `对比完成（${collected.length} 个策略）`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "对比回测失败";
      setCompareError(msg);
      toast.show("error", msg);
    } finally {
      setComparing(false);
      setProgress("");
    }
  };

  /* 对比曲线与最优标注 */
  const series: MultiSeries[] = useMemo(
    () =>
      results.map((r, i) => ({
        name: strategyLabel(
          pickStr((r.params ?? {}) as Record<string, unknown>, ["strategy", "strategy_template"]) ?? `run${i}`,
        ),
        points: (r.equity_curve ?? []).map((p) => ({ date: p.date, value: p.value })),
        color: SERIES_PALETTE[i % SERIES_PALETTE.length],
      })),
    [results],
  );

  const bestBy = (row: MetricRow): string | null => {
    if (!row.higherBetter || !row.raw) return null;
    let bestIdx = -1;
    let bestVal: number | null = null;
    results.forEach((r, i) => {
      const v = row.raw?.((r.metrics ?? null) as Record<string, unknown> | null) ?? null;
      if (v === null) return;
      if (bestVal === null || v > bestVal) {
        bestVal = v;
        bestIdx = i;
      }
    });
    return bestIdx >= 0 ? `#${bestIdx + 1}` : null;
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="05"
        kicker="Research · Strategy Lab"
        title="策略实验室"
        description="浏览内置策略模板、微调参数（每次保存生成不可变新版本），勾选多个策略做同区间对比回测 —— 参数扫描的初步筛选用向量化引擎，最终结论以事件驱动引擎复核。"
        action={<GhostButton onClick={strategiesApi.refresh}>刷新模板</GhostButton>}
      />

      <ToastHost toast={toast.toast} />

      {/* 策略列表 */}
      {strategiesApi.loading && !strategiesApi.data ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : strategiesApi.error && !strategiesApi.data ? (
        <ErrorState error={strategiesApi.error as ApiRequestError} onRetry={strategiesApi.refresh}>
          <p className="text-[11px] text-muted">已展示内置策略模板（离线兜底），仍可发起对比回测。</p>
        </ErrorState>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {strategies.map((s, i) => {
            const checked = selected.includes(s.code);
            return (
              <div
                key={s.code}
                className={cn(
                  "animate-fade-up flex flex-col rounded-xl border p-4 transition-all",
                  checked ? "border-accent/60 bg-accent/[0.06]" : "border-line bg-bg-raised/60 hover:border-line",
                )}
                style={{ animationDelay: `${i * 60}ms` }}
              >
                <div className="flex items-start justify-between gap-2">
                  <label className="flex cursor-pointer items-start gap-2.5">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleSelect(s.code)}
                      className="mt-0.5 h-3.5 w-3.5 accent-[#58a6ff]"
                      aria-label={`选择 ${s.name} 参与对比`}
                    />
                    <span>
                      <span className="block text-sm font-semibold text-[#e6edf3]">{s.name}</span>
                      <span className="mt-1 block text-[11px] leading-relaxed text-muted">{s.desc}</span>
                    </span>
                  </label>
                </div>
                <div className="mt-3 flex items-center gap-1.5">
                  <Badge className={s.builtin ? "border-accent/40 bg-accent/10 text-accent" : "border-up/40 bg-up/10 text-up"}>
                    {s.builtin ? "内置" : "自定义"}
                  </Badge>
                  {s.editable && <Badge className="border-warn/40 bg-warn/10 text-warn">参数可调</Badge>}
                  <span className="ml-auto font-mono text-[10px] text-muted/70">{s.code}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* 参数微调 + 对比配置 */}
      <SectionCard title="参数微调与对比配置" extra={<span className="text-[10px] text-muted">同一区间并行回测，口径一致才有意义</span>}>
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="初始资金">
              <NumberInput value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
            </Field>
            <Field label="每月投入" required>
              <NumberInput value={periodAmount} onChange={(e) => setPeriodAmount(e.target.value)} />
            </Field>
            <Field label="开始日期" required>
              <input
                type="date"
                value={startDate}
                min="2015-01-01"
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-[#e6edf3] outline-none transition-colors focus:border-accent/60"
              />
            </Field>
            <Field label="结束日期" required>
              <input
                type="date"
                value={endDate}
                max={todayISO()}
                onChange={(e) => setEndDate(e.target.value)}
                className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-[#e6edf3] outline-none transition-colors focus:border-accent/60"
              />
            </Field>
          </div>

          {hasEditableSelected && (
            <div>
              <p className="mb-2 text-[11px] font-medium text-muted">梯度倍数表（应用于选中的加仓类策略）</p>
              <MultiplierEditor rules={rules} onChange={setRules} />
            </div>
          )}

          <div className="flex flex-wrap items-center gap-3">
            <PrimaryButton loading={comparing} onClick={runCompare} disabled={!selected.length}>
              ⇄ 对比回测（{selected.length}）
            </PrimaryButton>
            {comparing ? (
              <span className="animate-pulse-soft text-[11px] text-accent">{progress || "处理中…"}</span>
            ) : (
              <span className="text-[11px] text-muted">已选 {selected.length} / {COMPARE_LIMIT} · 手续费 0.1% · 滑点 5bps 固定假设</span>
            )}
          </div>
          {compareError && <ErrorState error={new Error(compareError)} onRetry={runCompare} compact />}
        </div>
      </SectionCard>

      {/* 对比结果 */}
      {results.length > 0 && (
        <SectionCard
          title="对比结果"
          extra={
            <Badge className={serverCompared ? "border-up/40 bg-up/10 text-up" : "border-warn/40 bg-warn/10 text-warn"}>
              {serverCompared ? "服务端对比报告" : "本地结果对比"}
            </Badge>
          }
        >
          <div className="space-y-5">
            <div className="overflow-x-auto rounded-xl border border-line">
              <table className="w-full min-w-[560px] text-left text-xs">
                <thead>
                  <tr className="border-b border-line bg-bg-hover/40 text-[10px] uppercase tracking-wider text-muted">
                    <th className="px-4 py-2.5 font-medium">指标</th>
                    {results.map((r, i) => {
                      const name = strategyLabel(
                        pickStr((r.params ?? {}) as Record<string, unknown>, ["strategy", "strategy_template"]) ?? `run${i}`,
                      );
                      return (
                        <th key={r.run_id} className="px-3 py-2.5 text-right font-medium">
                          <span className="inline-flex items-center gap-1.5">
                            <span aria-hidden className="h-1.5 w-1.5 rounded-full" style={{ background: SERIES_PALETTE[i % SERIES_PALETTE.length] }} />
                            {name}
                          </span>
                        </th>
                      );
                    })}
                  </tr>
                </thead>
                <tbody>
                  {METRIC_ROWS.map((row) => {
                    const best = bestBy(row);
                    return (
                      <tr key={row.label} className="border-b border-line/40 last:border-0">
                        <td className="px-4 py-2.5 text-muted">{row.label}</td>
                        {results.map((r, i) => (
                          <td key={r.run_id} className="px-3 py-2.5 text-right font-mono tabular-nums">
                            <span className={best === `#${i + 1}` ? "font-semibold text-up" : undefined}>
                              {row.get((r.metrics ?? null) as Record<string, unknown> | null)}
                            </span>
                          </td>
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            {series.some((s) => s.points.length > 0) ? (
              <MultiLineChart series={series} height={340} />
            ) : (
              <p className="py-4 text-center text-xs text-muted">各策略结果未包含资产曲线，无法叠加展示</p>
            )}

            <div className="flex flex-wrap gap-2 text-[10px] text-muted">
              {results.map((r, i) => (
                <span key={r.run_id} className="font-mono">
                  {i + 1}. {r.run_id.slice(0, 10)}…
                </span>
              ))}
              <span>· 对比区间 {formatDate(startDate)} ~ {formatDate(endDate)}</span>
            </div>

            <Disclaimer />
          </div>
        </SectionCard>
      )}

      {results.length === 0 && !comparing && (
        <EmptyState
          title="尚未生成对比"
          description="勾选 2 个以上策略（最多 5 个），设置统一区间后点击「对比回测」。任务将逐个提交并轮询，完成后自动生成对比表与净值叠加图。"
        />
      )}
    </div>
  );
}
