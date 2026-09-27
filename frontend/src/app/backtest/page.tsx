"use client";

/**
 * 策略回测 —— Backtest Engine 前端（docs/architecture/14 §3-4、16 §8.17）。
 *
 * 配置 → POST /backtest/run（异步任务）→ 轮询 GET /backtest/runs/{id} 至 DONE/FAILED
 * → 绩效指标网格 + 资产曲线 + 回撤水下图 + 逐笔交易。
 * 历史回测列表可点击回看；免责声明强制展示。
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { DrawdownChart, EquityCurveChart, type CurvePoint } from "@/components/portfolio/charts";
import { Disclaimer } from "@/components/portfolio/Disclaimer";
import { DateInput, Field, NumberInput, SelectInput } from "@/components/portfolio/Form";
import { GhostButton, PrimaryButton } from "@/components/portfolio/Modal";
import { Badge, PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import {
  cn,
  formatBtc,
  formatDate,
  formatDateTime,
  formatMoney,
  formatPct,
  pickNum,
  pickStr,
  strategyLabel,
  todayISO,
} from "@/components/portfolio/utils";
import { useApi } from "@/hooks/useApi";
import { apiClient, type ApiRequestError } from "@/lib/api";
import type { BacktestResult, BacktestTrade } from "@/types/api";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

/* 内置策略候选（GET /backtest/strategies 失败时的兜底） */
const FALLBACK_STRATEGIES = [
  { value: "fixed", label: "固定定投（基准）" },
  { value: "dip_ath", label: "下跌加仓" },
  { value: "dip_recent", label: "回撤加仓" },
  { value: "value", label: "估值加仓" },
  { value: "risk_adjust", label: "风险调整" },
  { value: "cycle_adjust", label: "周期调整" },
];

const FREQ_OPTIONS = [
  { value: "monthly", label: "每月" },
  { value: "weekly", label: "每周" },
  { value: "biweekly", label: "双周" },
];

const POLL_INTERVAL_MS = 2500;
const POLL_MAX_ERRORS = 4;

function normalizeStrategyOptions(raw: Record<string, unknown>[] | null) {
  if (!raw?.length) return FALLBACK_STRATEGIES;
  const options = raw
    .map((s) => {
      const code = pickStr(s, ["code", "name", "strategy", "id"]);
      const label = pickStr(s, ["label", "display_name", "title", "description"]) ?? code;
      return code ? { value: code, label: strategyLabel(code) === code && label ? label : strategyLabel(code) } : null;
    })
    .filter((o): o is { value: string; label: string } => o !== null);
  return options.length ? options : FALLBACK_STRATEGIES;
}

function runTitle(run: BacktestResult): string {
  const params = (run.params ?? {}) as Record<string, unknown>;
  const strategy = pickStr(params, ["strategy", "strategy_template"]) ?? "strategy";
  return `${strategyLabel(strategy)} · ${formatDate(pickStr(params, ["start_date"]) ?? run.created_at)} ~ ${formatDate(pickStr(params, ["end_date"]) ?? run.finished_at)}`;
}

export default function BacktestPage() {
  const toast = useToast();

  /* 策略模板 */
  const strategies = useApi(() => apiClient.backtest.getStrategies(), []);
  const strategyOptions = useMemo(
    () => normalizeStrategyOptions(strategies.data),
    [strategies.data],
  );

  /* 配置 */
  const [strategy, setStrategy] = useState("fixed");
  const [initialCapital, setInitialCapital] = useState("0");
  const [periodAmount, setPeriodAmount] = useState("1000");
  const [frequency, setFrequency] = useState("monthly");
  const [startDate, setStartDate] = useState("2018-01-01");
  const [endDate, setEndDate] = useState(todayISO());
  const [feePct, setFeePct] = useState("0.1");
  const [slippageBps, setSlippageBps] = useState("5");

  /* 运行状态 */
  const [submitting, setSubmitting] = useState(false);
  const [runningId, setRunningId] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [tradesOpen, setTradesOpen] = useState(false);
  const pollFailures = useRef(0);

  /* 历史列表 */
  const runs = useApi(() => apiClient.backtest.getRuns({ page_size: 20 }), []);
  const [viewing, setViewing] = useState<BacktestResult | null>(null);
  const [viewLoading, setViewLoading] = useState(false);

  /* 计时 */
  useEffect(() => {
    if (!runningId) return;
    const start = Date.now();
    /* 首帧用 0ms 宏任务异步归零，避免在 effect 体内同步 setState（react-hooks/set-state-in-effect） */
    const immediate = setTimeout(() => setElapsed(0), 0);
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - start) / 1000)), 1000);
    return () => {
      clearTimeout(immediate);
      clearInterval(id);
    };
  }, [runningId]);

  /* 轮询回测任务直到完成 */
  useEffect(() => {
    if (!runningId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const res = await apiClient.backtest.getRun(runningId);
        if (cancelled) return;
        const run = res.data;
        pollFailures.current = 0;
        if (run) {
          const status = (run.status ?? "").toUpperCase();
          if (status === "DONE" || status === "COMPLETED") {
            setResult(run);
            setRunningId(null);
            toast.show("success", "回测完成");
            runs.refresh();
            return;
          }
          if (status === "FAILED" || status === "CANCELLED") {
            setRunningId(null);
            const msg = "回测任务失败（策略参数或数据区间可能不可用）";
            setSubmitError(msg);
            toast.show("error", msg);
            runs.refresh();
            return;
          }
        }
        timer = setTimeout(tick, POLL_INTERVAL_MS);
      } catch {
        if (cancelled) return;
        pollFailures.current += 1;
        if (pollFailures.current >= POLL_MAX_ERRORS) {
          setRunningId(null);
          const msg = "轮询回测状态失败：后端服务可能不可达";
          setSubmitError(msg);
          toast.show("error", msg);
          return;
        }
        timer = setTimeout(tick, POLL_INTERVAL_MS);
      }
    };

    timer = setTimeout(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runningId]);

  const submitRun = async () => {
    setSubmitting(true);
    setSubmitError(null);
    setResult(null);
    try {
      const res = await apiClient.backtest.run({
        strategy,
        initial_capital: Number(initialCapital) || 0,
        monthly_amount: frequency === "monthly" ? Number(periodAmount) || 0 : 0,
        weekly_amount: frequency === "weekly" ? Number(periodAmount) || 0 : 0,
        frequency,
        start_date: startDate,
        end_date: endDate,
        currency: "CNY",
        fee_rate: (Number(feePct) || 0) / 100,
        slippage_bps: Number(slippageBps) || 0,
      });
      const runId = res.data?.run_id;
      if (!runId) throw new Error("后端未返回 run_id");
      toast.show("success", "回测任务已提交");
      setRunningId(runId);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "提交回测失败";
      setSubmitError(msg);
      toast.show("error", msg);
    } finally {
      setSubmitting(false);
    }
  };

  const viewRun = async (runId: string) => {
    setViewLoading(true);
    try {
      const res = await apiClient.backtest.getRun(runId);
      setViewing(res.data);
      setResult(null);
      setRunningId(null);
    } catch (err) {
      toast.show("error", err instanceof Error ? err.message : "加载回测详情失败");
    } finally {
      setViewLoading(false);
    }
  };

  /* 展示目标：手动运行的结果优先，其次是历史详情 */
  const shown = result ?? viewing;
  const metrics = (shown?.metrics ?? null) as Record<string, unknown> | null;
  const curve: CurvePoint[] = useMemo(
    () =>
      (shown?.equity_curve ?? []).map((p) => ({
        date: p.date,
        value: p.value,
        invested: p.invested,
      })),
    [shown],
  );
  const ddCurve = useMemo(() => shown?.drawdown_curve ?? [], [shown]);
  const trades: BacktestTrade[] = useMemo(() => (shown?.trades ?? []).slice(0, 200), [shown]);

  const totalReturn = pickNum(metrics, ["total_return_pct"]);
  const cagr = pickNum(metrics, ["annualized_return_pct", "cagr_pct"]);
  const maxDd = pickNum(metrics, ["max_drawdown_pct"]);
  const maxDdDays = pickNum(metrics, ["max_drawdown_duration_days"]);
  const sharpe = pickNum(metrics, ["sharpe"]);
  const sortino = pickNum(metrics, ["sortino"]);
  const winRate = pickNum(metrics, ["win_rate"]);
  const winRatePct = winRate !== null && winRate <= 1 ? winRate * 100 : winRate;
  const plRatio = pickNum(metrics, ["profit_loss_ratio", "pl_ratio", "profit_factor"]);
  const totalFees = pickNum(metrics, ["total_fees"]);
  const bestYear = pickNum(metrics, ["best_year"]);
  const worstYear = pickNum(metrics, ["worst_year"]);
  const btcAmount = pickNum(metrics, ["btc_amount"]);
  const avgCost = pickNum(metrics, ["avg_cost"]);
  const finalValue = pickNum(metrics, ["final_value"]);
  const totalInvested = pickNum(metrics, ["total_invested"]);

  const statusBadge = (status?: string) => {
    const s = (status ?? "").toUpperCase();
    if (s === "DONE" || s === "COMPLETED") return { label: "已完成", cls: "border-up/40 bg-up/10 text-up" };
    if (s === "RUNNING") return { label: "运行中", cls: "border-accent/40 bg-accent/10 text-accent" };
    if (s === "FAILED") return { label: "失败", cls: "border-down/50 bg-down/10 text-down" };
    if (s === "PENDING") return { label: "排队中", cls: "border-warn/40 bg-warn/10 text-warn" };
    return { label: s || "未知", cls: "border-line bg-bg-hover text-muted" };
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="04"
        kicker="Research · Backtest Engine"
        title="策略回测"
        description="在真实历史数据上严格验证定投策略：PIT 数据防未来泄漏、次 Bar 开盘成交、逐笔可查。回测结果是异步任务，提交后自动轮询进度。"
      />

      <ToastHost toast={toast.toast} />

      {/* 配置 */}
      <SectionCard title="回测配置" extra={<span className="text-[10px] text-muted">数据区间越早，链上/ETF 覆盖越稀疏</span>}>
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="策略" required>
              <SelectInput value={strategy} onChange={(e) => setStrategy(e.target.value)} options={strategyOptions} />
            </Field>
            <Field label="初始资金" hint="0 = 纯定投">
              <NumberInput value={initialCapital} onChange={(e) => setInitialCapital(e.target.value)} />
            </Field>
            <Field label="每期定投金额" required>
              <NumberInput value={periodAmount} onChange={(e) => setPeriodAmount(e.target.value)} />
            </Field>
            <Field label="定投频率">
              <SelectInput value={frequency} onChange={(e) => setFrequency(e.target.value)} options={FREQ_OPTIONS} />
            </Field>
            <Field label="开始日期" required>
              <DateInput value={startDate} onChange={(e) => setStartDate(e.target.value)} min="2015-01-01" />
            </Field>
            <Field label="结束日期" required>
              <DateInput value={endDate} onChange={(e) => setEndDate(e.target.value)} max={todayISO()} />
            </Field>
            <Field label="手续费率" hint="现货默认 0.1%">
              <div className="relative">
                <NumberInput value={feePct} onChange={(e) => setFeePct(e.target.value)} className="pr-7" />
                <span aria-hidden className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted">%</span>
              </div>
            </Field>
            <Field label="滑点" hint="固定 bps">
              <NumberInput value={slippageBps} onChange={(e) => setSlippageBps(e.target.value)} />
            </Field>
          </div>

          <div className="flex items-center gap-3">
            <PrimaryButton loading={submitting || runningId !== null} onClick={submitRun}>
              {runningId ? "回测运行中…" : submitting ? "提交中…" : "▶ 运行回测"}
            </PrimaryButton>
            {runningId && (
              <span className="animate-pulse-soft text-[11px] text-accent">
                run_id {runningId.slice(0, 8)}… · 已运行 {elapsed}s（长回测 {">"}30s 属正常）
              </span>
            )}
          </div>

          {submitError && <ErrorState error={new Error(submitError)} onRetry={submitRun} compact />}
        </div>
      </SectionCard>

      {/* 结果 */}
      {(shown || viewLoading) && (
        <SectionCard
          title={result ? "回测结果（本次运行）" : "回测详情"}
          extra={
            shown ? (
              <span className="text-[10px] text-muted">
                {shown.run_id.slice(0, 8)}… · {formatDateTime(shown.created_at)}
                {shown.model_version ? ` · 模型 ${shown.model_version}` : ""}
              </span>
            ) : undefined
          }
        >
          {viewLoading && !shown ? (
            <LoadingSkeleton variant="card" />
          ) : (
            <div className="space-y-5">
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatCard label="最终资产" value={formatMoney(finalValue)} tone="accent" sub={`总投入 ${formatMoney(totalInvested)}`} />
                <StatCard
                  label="总收益率"
                  value={<TrendValue value={totalReturn} />}
                  sub={cagr !== null ? `年化 ${formatPct(cagr)}` : undefined}
                />
                <StatCard label="最大回撤" value={maxDd !== null ? formatPct(maxDd, { sign: false }) : "—"} tone="warn" sub={maxDdDays !== null ? `最长持续 ${maxDdDays} 天` : undefined} />
                <StatCard label="Sharpe / Sortino" value={sharpe !== null ? sharpe.toFixed(2) : "—"} sub={sortino !== null ? `Sortino ${sortino.toFixed(2)}` : undefined} />
                <StatCard label="胜率" value={winRatePct !== null ? `${winRatePct.toFixed(1)}%` : "—"} sub={plRatio !== null ? `盈亏比 ${plRatio.toFixed(2)}` : undefined} />
                <StatCard label="累计手续费" value={formatMoney(totalFees)} tone="muted" sub={`滑点 ${slippageBps}bps 假设`} />
                <StatCard
                  label="最佳 / 最差年份"
                  value={
                    bestYear !== null || worstYear !== null
                      ? `${bestYear !== null ? formatPct(bestYear, { sign: false }) : "—"} / ${worstYear !== null ? formatPct(worstYear, { sign: false }) : "—"}`
                      : "—"
                  }
                  tone={bestYear !== null && bestYear > 0 ? "up" : "muted"}
                  sub="自然年收益率极值"
                />
                <StatCard
                  label="期末持仓"
                  value={formatBtc(btcAmount)}
                  sub={avgCost !== null ? `平均成本 ${formatMoney(avgCost)}` : undefined}
                />
              </div>

              {curve.length ? (
                <EquityCurveChart data={curve} height={320} valueName="组合净值" investedName="累计投入" />
              ) : (
                <p className="py-4 text-center text-xs text-muted">该回测未包含资产曲线</p>
              )}

              {ddCurve.length > 0 && (
                <div>
                  <p className="mb-2 text-[11px] font-medium text-muted">回撤水下图</p>
                  <DrawdownChart data={ddCurve} height={180} />
                </div>
              )}

              {trades.length > 0 && (
                <div className="rounded-xl border border-line">
                  <button
                    type="button"
                    onClick={() => setTradesOpen((v) => !v)}
                    aria-expanded={tradesOpen}
                    className="flex w-full items-center justify-between px-4 py-3 text-left"
                  >
                    <span className="text-xs font-semibold uppercase tracking-wider text-muted">
                      逐笔交易（{shown?.trades?.length ?? trades.length} 笔，最多展示 200）
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
                            <th className="px-3 py-2 font-medium">方向</th>
                            <th className="px-3 py-2 text-right font-medium">成交价</th>
                            <th className="px-3 py-2 text-right font-medium">金额</th>
                            <th className="px-4 py-2 text-right font-medium">倍数</th>
                          </tr>
                        </thead>
                        <tbody>
                          {trades.map((t, i) => (
                            <tr key={`${t.date}-${i}`} className="border-b border-line/30 last:border-0">
                              <td className="px-4 py-2 font-mono tabular-nums text-muted">{formatDate(t.date)}</td>
                              <td className="px-3 py-2">
                                <Badge className={(t.side ?? "BUY").toUpperCase() === "SELL" ? "border-down/50 bg-down/10 text-down" : "border-up/40 bg-up/10 text-up"}>
                                  {(t.side ?? "BUY").toUpperCase() === "SELL" ? "卖出" : "买入"}
                                </Badge>
                              </td>
                              <td className="px-3 py-2 text-right font-mono tabular-nums">{formatMoney(t.price ?? null)}</td>
                              <td className="px-3 py-2 text-right font-mono tabular-nums">{formatMoney(t.amount ?? null)}</td>
                              <td className="px-4 py-2 text-right font-mono tabular-nums">{t.multiplier ? `${t.multiplier.toFixed(2)}×` : "—"}</td>
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
          )}
        </SectionCard>
      )}

      {/* 历史回测列表 */}
      <SectionCard title="历史回测" extra={<GhostButton onClick={runs.refresh}>刷新</GhostButton>} bodyClassName="p-0">
        {runs.loading && !runs.data ? (
          <div className="p-4">
            <LoadingSkeleton variant="table" rows={3} />
          </div>
        ) : runs.error && !runs.data ? (
          <div className="p-4">
            <ErrorState error={runs.error as ApiRequestError} onRetry={runs.refresh} compact />
          </div>
        ) : !(runs.data ?? []).length ? (
          <div className="p-4">
            <EmptyState title="暂无历史回测" description="运行第一次回测后，任务记录将出现在这里，可随时回看。" />
          </div>
        ) : (
          <ul className="divide-y divide-line/50">
            {(runs.data ?? []).map((run) => {
              const badge = statusBadge(run.status);
              const m = run.metrics as unknown as Record<string, unknown> | null;
              return (
                <li key={run.run_id}>
                  <button
                    type="button"
                    onClick={() => viewRun(run.run_id)}
                    className="flex w-full flex-wrap items-center gap-2.5 px-4 py-3 text-left transition-colors hover:bg-bg-hover/50"
                  >
                    <Badge className={badge.cls}>{badge.label}</Badge>
                    <span className="text-xs font-medium text-[#e6edf3]">{runTitle(run)}</span>
                    <span className="font-mono text-[10px] text-muted">{run.run_id.slice(0, 12)}…</span>
                    <span className="ml-auto flex items-center gap-3 text-[11px]">
                      {pickNum(m, ["total_return_pct"]) !== null && (
                        <span
                          className={cn(
                            "font-mono tabular-nums",
                            (pickNum(m, ["total_return_pct"]) ?? 0) >= 0 ? "text-up" : "text-down",
                          )}
                        >
                          {formatPct(pickNum(m, ["total_return_pct"]))}
                        </span>
                      )}
                      <span className="text-muted">{formatDateTime(run.created_at)}</span>
                      <span aria-hidden className="text-muted">›</span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </SectionCard>

      <Disclaimer className="mx-auto max-w-5xl">
        回测基于 PIT 历史数据与简化成交假设（次 Bar 开盘 + 固定滑点），历史表现不代表未来收益；结果不可变绑定模型版本与数据指纹，仅供参考研究。
      </Disclaimer>
    </div>
  );
}

/** 内联涨跌色文字 */
function TrendValue({ value }: { value: number | null }) {
  if (value === null) return <span className="text-muted">—</span>;
  return (
    <span className={value >= 0 ? "text-up" : "text-down"}>
      {formatPct(value)}
    </span>
  );
}
