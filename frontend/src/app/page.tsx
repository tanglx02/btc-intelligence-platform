"use client";

/**
 * 首页 Dashboard：
 * ① 价格横幅（WS 实时价 + 24H/7D/30D 涨跌 + 市值/ATH）
 * ② Market Regime 综合状态条（可展开证据）
 * ③ 9 大维度状态卡（3×3，点击展开证据）
 * ④ 个人区域（计划 + 资产概览，无数据时引导创建）
 *
 * 数据源：/engine/dashboard（聚合）、/market/price、/market/stats、
 * WS price 频道、/portfolio/plans、/portfolio/holdings。
 */

import Link from "next/link";
import { useState } from "react";

import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { EvidenceList, type EvidenceBundle } from "@/components/common/EvidenceList";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { StateCard } from "@/components/common/StateCard";
import { TrendIndicator } from "@/components/common/TrendIndicator";
import { useApi } from "@/hooks/useApi";
import { useHydrated } from "@/hooks/useHydrated";
import { useLivePrice } from "@/hooks/useWebSocket";
import {
  formatPct,
  formatPrice,
  formatUsd,
  fmtDateTime,
  pickNumber,
  planStatusLabel,
  relativeDays,
  semanticTone,
  stateLabel,
  type StateTone,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import { useUIStore } from "@/stores/ui";
import type {
  EngineDashboardData,
  EngineOutput,
  EngineOutputResponse,
  Holdings,
  MarketStats,
  PriceData,
  RegimeMetadata,
  RegimeStates,
  UserPlan,
} from "@/types/api";

/* -------------------------------------------------------------------------- */
/* 辅助                                                                        */
/* -------------------------------------------------------------------------- */

function outputOf(res?: EngineOutputResponse | null): EngineOutput | null {
  return res && typeof res === "object" ? (res.output ?? null) : null;
}

function bundleOf(out: EngineOutput | null): EvidenceBundle | null {
  if (!out) return null;
  const supporting = out.supporting_evidence ?? [];
  const opposing = out.opposing_evidence ?? [];
  if (supporting.length === 0 && opposing.length === 0) return null;
  return { supporting, opposing };
}

interface DimCardData {
  title: string;
  state: string;
  confidence: number | null;
  tone: StateTone;
  evidence: EvidenceBundle | null;
  explanation: string | null;
  raw?: string;
}

function engineCard(title: string, out: EngineOutput | null): DimCardData {
  return {
    title,
    state: out ? stateLabel(out.state) : "暂无数据",
    confidence: out?.confidence ?? null,
    tone: out ? semanticTone(out.state) : "muted",
    evidence: bundleOf(out),
    explanation: out?.explanation ?? null,
    raw: out?.state ?? undefined,
  };
}

function regimeDimCard(
  title: string,
  dim: RegimeStates[string],
  summary: string | null,
): DimCardData {
  return {
    title,
    state: dim ? stateLabel(dim.state) : "暂无数据",
    confidence: dim?.confidence ?? null,
    tone: dim ? semanticTone(dim.state) : "muted",
    evidence: null,
    explanation: summary,
    raw: dim?.state,
  };
}

function MiniChange({ label, value, size = "sm" }: { label: string; value?: number | null; size?: "sm" | "md" | "lg" }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wider text-muted">{label}</p>
      <TrendIndicator value={value ?? null} size={size} className="mt-0.5" />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function DashboardPage() {
  const hydrated = useHydrated();
  const mode = useUIStore((s) => s.mode);
  const isPro = hydrated && mode === "pro"; // mode 必须无条件调用（rules-of-hooks）

  const dashboard = useApi<EngineDashboardData>(
    () => apiClient.engine.getDashboard(),
    [],
    { refreshInterval: 60_000 },
  );
  const priceApi = useApi<PriceData>(() => apiClient.market.getPrice(), [], {
    refreshInterval: 30_000,
  });
  const statsApi = useApi<MarketStats>(() => apiClient.market.getStats(), [], {
    refreshInterval: 120_000,
  });
  const plansApi = useApi<UserPlan[]>(
    () => apiClient.portfolio.getPlans({ page: 1, page_size: 5 }),
    [],
    { refreshInterval: 120_000 },
  );
  const holdingsApi = useApi<Holdings>(() => apiClient.portfolio.getHoldings(), [], {
    refreshInterval: 120_000,
  });
  const live = useLivePrice();

  const [regimeOpen, setRegimeOpen] = useState(false);

  const db = dashboard.data;
  const regimeOut = outputOf(db?.regime);
  const cycleOut = outputOf(db?.cycle);
  const valOut = outputOf(db?.valuation);
  const riskOut = outputOf(db?.risk);

  const regimeMeta = (regimeOut?.metadata ?? null) as RegimeMetadata | null;
  const states: RegimeStates = regimeMeta?.states ?? {};
  const regimeLabel = stateLabel(regimeMeta?.regime ?? regimeOut?.state ?? "");
  const regimeSummary = regimeOut?.explanation ?? regimeMeta?.summary ?? null;
  const regimeEvidence = bundleOf(regimeOut);
  const regimeTone = semanticTone(regimeMeta?.regime ?? regimeOut?.state);

  const price = live.price ?? priceApi.data?.price ?? db?.price?.price ?? null;
  const c24 = live.change24h ?? priceApi.data?.change_24h_pct ?? db?.price?.change_24h_pct ?? null;
  const c7 = priceApi.data?.change_7d_pct ?? db?.price?.change_7d_pct ?? null;
  const c30 = priceApi.data?.change_30d_pct ?? db?.price?.change_30d_pct ?? null;

  const stats = statsApi.data;
  const marketCap = pickNumber(stats, ["market_cap"]);
  const ath = pickNumber(stats, ["ath"]);
  const drawdown =
    pickNumber(stats, ["drawdown_from_ath_pct"]) ??
    (ath && price ? (price / ath - 1) * 100 : null);
  const high24h = priceApi.data?.high_24h ?? pickNumber(stats, ["high_24h"]);
  const low24h = priceApi.data?.low_24h ?? pickNumber(stats, ["low_24h"]);
  const volume24h = pickNumber(stats, ["volume_24h"]);

  const plans = plansApi.data;
  const activePlan = plans?.find((p) => (p.status ?? "").toUpperCase() === "ACTIVE") ?? plans?.[0] ?? null;
  const holdings = holdingsApi.data;

  const toneDot: Record<StateTone, string> = {
    up: "bg-up",
    down: "bg-down",
    warn: "bg-warn",
    accent: "bg-accent",
    muted: "bg-muted",
  };

  const dimCards: DimCardData[] = [
    engineCard("市场阶段", cycleOut),
    regimeDimCard("趋势", states.trend, regimeSummary),
    engineCard("估值", valOut),
    regimeDimCard("资金", states.capital_flow, regimeSummary),
    regimeDimCard("链上", states.onchain, regimeSummary),
    regimeDimCard("杠杆", states.derivative, regimeSummary),
    regimeDimCard("宏观", states.macro, regimeSummary),
    regimeDimCard("情绪", states.sentiment, regimeSummary),
    engineCard("风险", riskOut),
  ];

  if (dashboard.loading && !db) {
    return (
      <div className="space-y-4">
        <LoadingSkeleton variant="card" />
        <LoadingSkeleton variant="lines" rows={2} />
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {dashboard.error && !db && (
        <ErrorState
          error={dashboard.error}
          lastUpdatedAt={dashboard.lastUpdatedAt}
          onRetry={dashboard.refresh}
        />
      )}

      {/* ① 价格横幅 */}
      <section className="relative overflow-hidden rounded-xl border border-line bg-bg-raised p-5 lg:p-6">
        <div
          aria-hidden
          className="pointer-events-none absolute -right-24 -top-28 h-64 w-64 rounded-full bg-accent/10 blur-3xl"
        />
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
          <div>
            <p className="flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.22em] text-muted">
              BTC / USDT
              {live.connected && (
                <span className="inline-flex items-center gap-1 text-up">
                  <span aria-hidden className="h-1.5 w-1.5 animate-pulse-soft rounded-full bg-up" />
                  实时
                </span>
              )}
            </p>
            <p className="mt-1.5 font-mono text-4xl font-bold tracking-tight tabular-nums text-[#e6edf3] lg:text-5xl">
              {price !== null ? `$${formatPrice(price)}` : "—"}
            </p>
          </div>
          <div className="flex items-end gap-5">
            <MiniChange label="24H" value={c24} size="md" />
            <MiniChange label="7D" value={c7} />
            <MiniChange label="30D" value={c30} />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-x-6 gap-y-1.5 font-mono text-xs tabular-nums text-muted">
          <span>市值 {formatUsd(marketCap)}</span>
          <span>ATH {ath !== null ? `$${formatPrice(ath)}` : "—"}</span>
          <span>距 ATH {formatPct(drawdown)}</span>
          {high24h !== null && low24h !== null && (
            <span>
              24H 区间 ${formatPrice(low24h)} ~ ${formatPrice(high24h)}
            </span>
          )}
          {volume24h !== null && <span>24H 量 {formatUsd(volume24h)}</span>}
        </div>

        {priceApi.error && !priceApi.data && (
          <ErrorState
            error={priceApi.error}
            lastUpdatedAt={priceApi.lastUpdatedAt}
            onRetry={priceApi.refresh}
            compact
            className="mt-3"
          />
        )}
      </section>

      {/* ② Market Regime 状态条 */}
      <section className="rounded-xl border border-line bg-gradient-to-r from-accent/[0.07] via-transparent to-transparent p-4 lg:p-5">
        <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3">
          <div className="flex items-center gap-3">
            <span
              aria-hidden
              className={`h-2.5 w-2.5 animate-pulse-soft rounded-full ${toneDot[regimeTone]}`}
            />
            <div>
              <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted">
                当前市场状态 · MARKET REGIME
              </p>
              <p className="mt-0.5 text-lg font-semibold leading-tight text-[#e6edf3]">
                {regimeOut || regimeMeta?.regime ? regimeLabel : "暂无数据"}
              </p>
            </div>
          </div>
          <div className="flex items-center gap-5 font-mono text-xs tabular-nums text-muted">
            {regimeOut?.confidence !== undefined && regimeOut?.confidence !== null && (
              <span>置信度 {Math.round(regimeOut.confidence * 100)}%</span>
            )}
            {hydrated && (regimeMeta?.changed_at ?? regimeOut?.observation_time) && (
              <span>
                状态更新 {fmtDateTime(regimeMeta?.changed_at ?? regimeOut?.observation_time ?? null)}
              </span>
            )}
            {(regimeEvidence || regimeSummary) && (
              <button
                type="button"
                onClick={() => setRegimeOpen((v) => !v)}
                aria-expanded={regimeOpen}
                className="rounded-md border border-accent/40 bg-accent/10 px-2.5 py-1 text-[11px] font-medium text-accent transition-colors hover:bg-accent/20"
              >
                {regimeOpen ? "收起" : "为什么？"}
              </button>
            )}
          </div>
        </div>

        {regimeSummary && (
          <p className="mt-2.5 text-xs leading-relaxed text-[#c9d1d9]">{regimeSummary}</p>
        )}

        {regimeOpen && (
          <div className="mt-3 space-y-3 border-t border-line pt-3">
            <EvidenceList evidence={regimeEvidence} />
            <Link
              href="/cycle"
              className="inline-block text-[11px] text-accent transition-colors hover:text-accent/80"
            >
              查看市场周期详情 →
            </Link>
          </div>
        )}

        {db?.errors && Object.keys(db.errors).length > 0 && (
          <p className="mt-3 text-[11px] text-warn/90">
            部分模块暂不可用：{Object.keys(db.errors).join("、")}
          </p>
        )}
      </section>

      {/* ③ 9 大维度状态卡 */}
      <section>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {dimCards.map((card) => (
            <StateCard
              key={card.title}
              title={card.title}
              state={card.state}
              confidence={card.confidence}
              color={card.tone}
              evidence={card.evidence}
              explanation={card.explanation}
              footnote={isPro && card.raw ? `原始状态: ${card.raw}` : undefined}
            />
          ))}
        </div>
      </section>

      {/* ④ 个人区域 */}
      <section className="grid gap-3 lg:grid-cols-2">
        <div className="rounded-xl border border-line bg-bg-raised p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-[#c9d1d9]">我的计划</h2>
            {activePlan?.status && (
              <span className="rounded border border-up/40 bg-up/10 px-1.5 py-0.5 text-[10px] text-up">
                {planStatusLabel(activePlan.status)}
              </span>
            )}
          </div>

          {plansApi.loading && !plans ? (
            <LoadingSkeleton variant="lines" rows={3} />
          ) : !activePlan ? (
            <EmptyState
              title="还没有定投计划"
              description="创建一个 BTC 定投计划，系统会结合市场状态给出投入建议。"
              action={
                <Link
                  href="/plans"
                  className="inline-block rounded-lg border border-accent/40 bg-accent/10 px-3 py-1.5 text-xs font-medium text-accent transition-colors hover:bg-accent/20"
                >
                  创建计划 →
                </Link>
              }
            />
          ) : (
            <dl className="space-y-2.5 text-xs">
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">计划名称</dt>
                <dd className="truncate font-medium text-[#e6edf3]">{activePlan.name}</dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">下次投入</dt>
                <dd className="font-mono tabular-nums text-[#e6edf3]">
                  {hydrated ? (relativeDays(activePlan.next_investment_at) ?? "—") : "—"}
                  {activePlan.next_investment_amount !== undefined &&
                    activePlan.next_investment_amount !== null &&
                    ` · ${formatUsd(activePlan.next_investment_amount)}`}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">累计投入</dt>
                <dd className="font-mono tabular-nums text-[#e6edf3]">
                  {formatUsd(activePlan.invested_total)}
                </dd>
              </div>
              <div className="pt-1">
                <Link href="/plans" className="text-[11px] text-accent hover:text-accent/80">
                  查看我的计划 →
                </Link>
              </div>
            </dl>
          )}
        </div>

        <div className="rounded-xl border border-line bg-bg-raised p-4">
          <h2 className="mb-3 text-sm font-semibold text-[#c9d1d9]">资产概览</h2>
          {holdingsApi.loading && !holdings ? (
            <LoadingSkeleton variant="lines" rows={3} />
          ) : !holdings || (holdings.btc_amount ?? 0) === 0 ? (
            <EmptyState
              title="暂无持仓记录"
              description="在「我的资产」手工记录买入后，这里会显示持仓市值与浮动盈亏。"
              action={
                <Link
                  href="/portfolio"
                  className="inline-block rounded-lg border border-line px-3 py-1.5 text-xs font-medium text-[#c9d1d9] transition-colors hover:border-accent/40 hover:text-accent"
                >
                  前往记录 →
                </Link>
              }
            />
          ) : (
            <dl className="space-y-2.5 text-xs">
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">持仓数量</dt>
                <dd className="font-mono tabular-nums text-[#e6edf3]">
                  {(holdings.btc_amount ?? 0).toFixed(4)} BTC
                </dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">市值</dt>
                <dd className="font-mono tabular-nums text-[#e6edf3]">
                  {formatUsd(holdings.market_value)}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">平均成本</dt>
                <dd className="font-mono tabular-nums text-[#e6edf3]">
                  {holdings.avg_cost != null ? `$${formatPrice(holdings.avg_cost)}` : "—"}
                </dd>
              </div>
              <div className="flex items-center justify-between gap-4">
                <dt className="text-muted">浮动盈亏</dt>
                <dd>
                  <TrendIndicator value={holdings.unrealized_pnl_pct ?? null} />
                  <span className="ml-2 font-mono tabular-nums text-muted">
                    {holdings.unrealized_pnl != null ? formatUsd(holdings.unrealized_pnl) : ""}
                  </span>
                </dd>
              </div>
              <div className="pt-1">
                <Link href="/portfolio" className="text-[11px] text-accent hover:text-accent/80">
                  查看我的资产 →
                </Link>
              </div>
            </dl>
          )}
        </div>
      </section>
    </div>
  );
}
