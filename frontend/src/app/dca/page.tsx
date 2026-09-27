"use client";

/**
 * 定投模拟 —— 用真实历史数据模拟不同 DCA 策略（docs/architecture/15 §3、16 §8.16）。
 *
 * 6 种策略模板（固定 / 下跌加仓 / 回撤加仓 / 估值加仓 / 风险调整 / 自定义）
 * → POST /portfolio/simulate → 关键指标 + 资产曲线（投入 vs 市值）+ 逐笔明细。
 * 免责声明强制展示：历史模拟不代表未来收益。
 */

import { useMemo, useState } from "react";

import { EquityCurveChart, type CurvePoint } from "@/components/portfolio/charts";
import { Disclaimer } from "@/components/portfolio/Disclaimer";
import { DateInput, Field, NumberInput, SelectInput } from "@/components/portfolio/Form";
import { GhostButton, PrimaryButton } from "@/components/portfolio/Modal";
import {
  DEFAULT_MULTIPLIER_RULES,
  MultiplierEditor,
  type MultiplierRule,
} from "@/components/portfolio/MultiplierEditor";
import { PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import { cn, formatBtc, formatDate, formatMoney, formatPct, pickNum, strategyLabel, todayISO } from "@/components/portfolio/utils";
import { apiClient } from "@/lib/api";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

/* -------------------------------------------------------------------------- */
/* 策略定义                                                                    */
/* -------------------------------------------------------------------------- */

interface StrategyDef {
  code: string;
  name: string;
  desc: string;
  /** 是否显示倍数表编辑器 */
  editable: boolean;
  tone: string;
}

const STRATEGIES: StrategyDef[] = [
  {
    code: "fixed",
    name: "固定定投",
    desc: "每期固定金额，无任何条件规则 —— 纪律优先的基准策略。",
    editable: false,
    tone: "text-accent",
  },
  {
    code: "dip_ath",
    name: "下跌加仓",
    desc: "距历史高点回撤越深、投得越多：−20% ×1.2、−30% ×1.5、−40% ×2.0（梯度可调）。",
    editable: true,
    tone: "text-up",
  },
  {
    code: "dip_recent",
    name: "回撤加仓",
    desc: "距离近 90 日高点的回撤达到阈值时按梯度加仓，独立于 ATH 口径。",
    editable: true,
    tone: "text-[#39d2c0]",
  },
  {
    code: "value",
    name: "估值加仓",
    desc: "MVRV / NUPL 分位低估时加倍投入（<30% ×1.5、<15% ×2.0）。",
    editable: false,
    tone: "text-warn",
  },
  {
    code: "risk_adjust",
    name: "风险调整",
    desc: "按 Risk Engine 档位反向缩放：低风险 ×1.3、高风险 ×0.5、极端 ×0.2。",
    editable: false,
    tone: "text-orange",
  },
  {
    code: "custom",
    name: "自定义",
    desc: "自由组合回撤梯度倍数表，适合进阶玩家精确控制弹药节奏。",
    editable: true,
    tone: "text-muted",
  },
];

const FREQ_OPTIONS = [
  { value: "weekly", label: "每周" },
  { value: "biweekly", label: "双周" },
  { value: "monthly", label: "每月" },
  { value: "daily", label: "每日" },
];

/* -------------------------------------------------------------------------- */
/* 宽松结果解析（/portfolio/simulate 返回 Record<string, unknown>）             */
/* -------------------------------------------------------------------------- */

interface SimTrade {
  date: string;
  amount: number | null;
  multiplier: number | null;
  price: number | null;
  quantity: number | null;
}

interface SimResult {
  metrics: Record<string, unknown>;
  curve: CurvePoint[];
  trades: SimTrade[];
}

function parseSimResult(raw: Record<string, unknown> | null): SimResult {
  if (!raw) return { metrics: {}, curve: [], trades: [] };
  const nested = (raw.metrics ?? null) as Record<string, unknown> | null;
  const metrics = nested && typeof nested === "object" ? nested : raw;

  const curveRaw = (raw.equity_curve ?? raw.curve ?? null) as unknown;
  const curve: CurvePoint[] = Array.isArray(curveRaw)
    ? curveRaw
        .map((pt) => {
          const o = pt as Record<string, unknown>;
          const investedRaw = o.invested ?? o.invested_cum ?? o.total_invested;
          return {
            date: String(o.date ?? o.time ?? ""),
            value: Number(o.value ?? o.market_value ?? o.equity ?? 0),
            invested: typeof investedRaw === "number" ? investedRaw : undefined,
          };
        })
        .filter((p) => p.date)
    : [];

  const tradesRaw = (raw.trades ?? raw.transactions ?? null) as unknown;
  const trades: SimTrade[] = Array.isArray(tradesRaw)
    ? tradesRaw.map((t) => {
        const o = t as Record<string, unknown>;
        return {
          date: String(o.date ?? o.time ?? ""),
          amount: typeof o.amount === "number" ? o.amount : null,
          multiplier: typeof (o.multiplier ?? o.factor) === "number" ? Number(o.multiplier ?? o.factor) : null,
          price: typeof o.price === "number" ? o.price : null,
          quantity: typeof (o.quantity ?? o.btc_amount) === "number" ? Number(o.quantity ?? o.btc_amount) : null,
        };
      })
    : [];

  return { metrics, curve, trades };
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function DcaPage() {
  const toast = useToast();

  const [strategy, setStrategy] = useState("dip_ath");
  const [initialCapital, setInitialCapital] = useState("");
  const [baseAmount, setBaseAmount] = useState("5000");
  const [frequency, setFrequency] = useState("monthly");
  const [currency, setCurrency] = useState<"CNY" | "USD">("CNY");
  const [startDate, setStartDate] = useState("2020-01-01");
  const [endDate, setEndDate] = useState(todayISO());
  const [feePct, setFeePct] = useState("0.1");
  const [slippagePct, setSlippagePct] = useState("0.05");
  const [rules, setRules] = useState<MultiplierRule[]>(DEFAULT_MULTIPLIER_RULES);

  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [result, setResult] = useState<SimResult | null>(null);
  const [tradesOpen, setTradesOpen] = useState(false);

  const activeStrategy = STRATEGIES.find((s) => s.code === strategy) ?? STRATEGIES[0];

  const applyQuickRange = (yearsBack: number) => {
    const end = todayISO();
    const d = new Date();
    d.setFullYear(d.getFullYear() - yearsBack);
    setStartDate(formatDate(d));
    setEndDate(end);
  };

  const runSimulation = async () => {
    setRunning(true);
    setRunError(null);
    setResult(null);
    try {
      const res = await apiClient.portfolio.simulate({
        strategy,
        start_date: startDate,
        end_date: endDate,
        frequency,
        base_amount: Number(baseAmount) || 0,
        initial_capital: Number(initialCapital) || 0,
        currency,
        rules: activeStrategy.editable ? rules : [],
        fee_pct: (Number(feePct) || 0) / 100,
        slippage_pct: (Number(slippagePct) || 0) / 100,
      });
      const parsed = parseSimResult(res.data as Record<string, unknown> | null);
      if (!parsed.curve.length && !Object.keys(parsed.metrics).length) {
        throw new Error("模拟返回为空：请检查时间范围或策略参数");
      }
      setResult(parsed);
      toast.show("success", "模拟完成");
      setTradesOpen(false);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "模拟失败，请稍后重试";
      setRunError(msg);
      toast.show("error", msg);
    } finally {
      setRunning(false);
    }
  };

  const m = result?.metrics ?? null;
  const finalValue = pickNum(m, ["final_value", "final_asset"]);
  const totalInvested = pickNum(m, ["total_invested"]);
  const totalProfit = pickNum(m, ["total_profit", "total_return_amount"]);
  const returnPct = pickNum(m, ["total_return_pct", "return_pct"]);
  const cagr = pickNum(m, ["annualized_return_pct", "cagr", "cagr_pct"]);
  const maxDd = pickNum(m, ["max_drawdown_pct", "max_drawdown"]);
  const sharpe = pickNum(m, ["sharpe", "sharpe_ratio"]);
  const btcAmount = pickNum(m, ["btc_amount", "btc_total"]);
  const avgCost = pickNum(m, ["avg_cost", "average_cost"]);
  const yearSpan = useMemo(() => {
    if (!result?.curve.length) return null;
    const s = new Date(result.curve[0].date).getFullYear();
    const e = new Date(result.curve[result.curve.length - 1].date).getFullYear();
    return Number.isNaN(s) || Number.isNaN(e) ? null : e - s || 1;
  }, [result]);

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="03"
        kicker="Research · DCA Simulator"
        title="定投模拟"
        description="「过去 N 年如果这样做，结果会怎样？」—— 用真实历史价格与引擎状态，按策略规则逐期模拟投入。先选策略，再调参数，一键运行。"
      />

      <ToastHost toast={toast.toast} />

      {/* 策略选择 */}
      <SectionCard title="① 选择策略">
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {STRATEGIES.map((s) => {
            const active = strategy === s.code;
            return (
              <button
                key={s.code}
                type="button"
                onClick={() => setStrategy(s.code)}
                className={cn(
                  "rounded-xl border p-3.5 text-left transition-all",
                  active
                    ? "border-accent/60 bg-accent/[0.07] shadow-[0_0_0_1px_rgba(88,166,255,0.25)]"
                    : "border-line bg-bg-raised/60 hover:border-line",
                )}
                aria-pressed={active}
              >
                <div className="flex items-center justify-between">
                  <span className={cn("text-sm font-semibold", s.tone)}>{s.name}</span>
                  <span aria-hidden className={cn("text-xs", active ? "text-accent" : "text-muted")}>
                    {active ? "◉" : "○"}
                  </span>
                </div>
                <p className="mt-1.5 text-[11px] leading-relaxed text-muted">{s.desc}</p>
              </button>
            );
          })}
        </div>
      </SectionCard>

      {/* 参数 */}
      <SectionCard title="② 参数">
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="初始资金" hint="一次性投入">
              <NumberInput value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} placeholder="0" />
            </Field>
            <Field label="每期金额" required>
              <NumberInput value={baseAmount} onChange={(e) => setBaseAmount(e.target.value)} />
            </Field>
            <Field label="频率">
              <SelectInput value={frequency} onChange={(e) => setFrequency(e.target.value)} options={FREQ_OPTIONS} />
            </Field>
            <Field label="开始日期" required>
              <DateInput value={startDate} onChange={(e) => setStartDate(e.target.value)} />
            </Field>
            <Field label="结束日期" required>
              <DateInput value={endDate} onChange={(e) => setEndDate(e.target.value)} />
            </Field>
            <Field label="计价币种">
              <SelectInput
                value={currency}
                onChange={(e) => setCurrency(e.target.value as "CNY" | "USD")}
                options={[
                  { value: "CNY", label: "CNY" },
                  { value: "USD", label: "USD" },
                ]}
              />
            </Field>
            <Field label="手续费率">
              <div className="relative">
                <NumberInput value={feePct} onChange={(e) => setFeePct(e.target.value)} className="pr-7" />
                <span aria-hidden className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted">%</span>
              </div>
            </Field>
            <Field label="滑点">
              <div className="relative">
                <NumberInput value={slippagePct} onChange={(e) => setSlippagePct(e.target.value)} className="pr-7" />
                <span aria-hidden className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted">%</span>
              </div>
            </Field>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-muted">快捷区间</span>
            {[1, 3, 5, 8].map((y) => (
              <GhostButton key={y} onClick={() => applyQuickRange(y)}>
                近 {y} 年
              </GhostButton>
            ))}
            <span className="text-[10px] text-muted/70">当前 {formatDate(startDate)} ~ {formatDate(endDate)}</span>
          </div>

          {activeStrategy.editable && (
            <div>
              <p className="mb-2 text-[11px] font-medium text-muted">
                策略参数 · {strategyLabel(strategy)}倍数表
              </p>
              <MultiplierEditor
                rules={rules}
                onChange={setRules}
                thresholdLabel={strategy === "dip_recent" ? "距 90 日高点回撤达到（%）" : "距 ATH 回撤达到（%）"}
              />
            </div>
          )}

          <div className="flex items-center gap-3">
            <PrimaryButton loading={running} onClick={runSimulation}>
              ▶ 运行模拟
            </PrimaryButton>
            {running && <span className="text-[11px] text-muted">正在按 {frequencyLabelSafe(frequency)} 逐期回放历史…</span>}
          </div>
          {runError && (
            <ErrorState error={new Error(runError)} onRetry={runSimulation} compact />
          )}
        </div>
      </SectionCard>

      {/* 结果 */}
      {running && !result && (
        <SectionCard title="③ 模拟结果">
          <LoadingSkeleton variant="card" />
        </SectionCard>
      )}

      {result && (
        <SectionCard title="③ 模拟结果" extra={<span className="text-[10px] text-muted">{strategyLabel(strategy)} · {frequencyLabelSafe(frequency)}</span>}>
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <StatCard label="最终资产" value={formatMoney(finalValue, currency)} tone="accent" />
              <StatCard label="总投入" value={formatMoney(totalInvested, currency)} />
              <StatCard
                label="总收益"
                value={formatMoney(totalProfit, currency, { sign: true })}
                tone={totalProfit !== null && totalProfit > 0 ? "up" : totalProfit !== null && totalProfit < 0 ? "down" : "muted"}
                sub={returnPct !== null ? formatPct(returnPct) : undefined}
              />
              <StatCard
                label="年化收益（CAGR）"
                value={cagr !== null ? formatPct(cagr) : "—"}
                tone={cagr !== null && cagr > 0 ? "up" : cagr !== null && cagr < 0 ? "down" : "muted"}
                sub={yearSpan ? `约 ${yearSpan} 年区间` : undefined}
              />
              <StatCard
                label="最大回撤"
                value={maxDd !== null ? formatPct(maxDd, { sign: false }) : "—"}
                tone="warn"
              />
              <StatCard label="Sharpe" value={sharpe !== null ? sharpe.toFixed(2) : "—"} />
              <StatCard label="累计 BTC" value={formatBtc(btcAmount)} />
              <StatCard label="平均成本" value={formatMoney(avgCost, currency)} />
            </div>

            {result.curve.length ? (
              <EquityCurveChart data={result.curve} currency={currency} height={320} valueName="模拟市值" />
            ) : (
              <p className="py-6 text-center text-xs text-muted">该结果未包含资产曲线数据</p>
            )}

            {result.trades.length > 0 && (
              <div className="rounded-xl border border-line">
                <button
                  type="button"
                  onClick={() => setTradesOpen((v) => !v)}
                  aria-expanded={tradesOpen}
                  className="flex w-full items-center justify-between px-4 py-3 text-left"
                >
                  <span className="text-xs font-semibold uppercase tracking-wider text-muted">
                    逐笔投入明细（{result.trades.length} 笔）
                  </span>
                  <span aria-hidden className={cn("text-[11px] text-muted", tradesOpen && "rotate-180")}>
                    {tradesOpen ? "收起 ▴" : "展开 ▾"}
                  </span>
                </button>
                {tradesOpen && (
                  <div className="animate-fade-up max-h-80 overflow-auto border-t border-line">
                    <table className="w-full min-w-[520px] text-left text-xs">
                      <thead className="sticky top-0 bg-bg-raised">
                        <tr className="border-b border-line text-[10px] uppercase tracking-wider text-muted">
                          <th className="px-4 py-2 font-medium">日期</th>
                          <th className="px-3 py-2 text-right font-medium">投入金额</th>
                          <th className="px-3 py-2 text-right font-medium">倍数</th>
                          <th className="px-3 py-2 text-right font-medium">成交价</th>
                          <th className="px-4 py-2 text-right font-medium">买入 BTC</th>
                        </tr>
                      </thead>
                      <tbody>
                        {result.trades.map((t, i) => (
                          <tr key={`${t.date}-${i}`} className="border-b border-line/30 last:border-0">
                            <td className="px-4 py-2 font-mono tabular-nums text-muted">{formatDate(t.date)}</td>
                            <td className="px-3 py-2 text-right font-mono tabular-nums">{formatMoney(t.amount, currency)}</td>
                            <td className="px-3 py-2 text-right font-mono tabular-nums">
                              {t.multiplier !== null ? `${t.multiplier.toFixed(2)}×` : "—"}
                            </td>
                            <td className="px-3 py-2 text-right font-mono tabular-nums">{formatMoney(t.price, currency)}</td>
                            <td className="px-4 py-2 text-right font-mono tabular-nums">
                              {t.quantity !== null ? t.quantity.toFixed(8) : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}

            <Disclaimer />
          </div>
        </SectionCard>
      )}

      {!result && !running && <Disclaimer className="mx-auto max-w-5xl" />}

      <p className="text-center text-[10px] leading-relaxed text-muted/70">
        模拟引擎与真实计划共用同一规则引擎（docs/architecture/15 §3）；模拟结果与真实账本严格分列。
      </p>
    </div>
  );
}

function frequencyLabelSafe(f: string): string {
  switch (f) {
    case "daily":
      return "每日";
    case "weekly":
      return "每周";
    case "biweekly":
      return "双周";
    default:
      return "每月";
  }
}
