"use client";

/**
 * 风险页：
 * - 综合风险等级（大号 + 颜色 + 四档仪表条）
 * - 7 维雷达图（dimension_scores 归一化）
 * - 风险因子明细列表（支持/反对合并，每项可展开）
 * - 支持证据 / 反向证据两列
 */

import { useMemo, useState } from "react";

import { ErrorState } from "@/components/common/ErrorState";
import { EvidenceList } from "@/components/common/EvidenceList";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { EChart, PageHeader, SectionTitle, SegmentBar } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import { useHydrated } from "@/hooks/useHydrated";
import {
  baseTooltip,
  fmtDateTime,
  labelDimension,
  RISK_LEVELS,
  riskLevelIndex,
  scoreToPct,
  semanticTone,
  stateLabel,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import { useUIStore } from "@/stores/ui";
import type { EngineEvidence, EngineOutput, EngineOutputResponse } from "@/types/api";
import type { EChartsOption } from "echarts";

/** 雷达维度：主键 + 备选键（不同引擎命名兼容） */
const RADAR_DEFS: { keys: string[]; label: string }[] = [
  { keys: ["trend"], label: "趋势" },
  { keys: ["valuation"], label: "估值" },
  { keys: ["leverage", "derivative", "derivatives"], label: "杠杆" },
  { keys: ["liquidity", "capital_flow"], label: "流动性" },
  { keys: ["macro"], label: "宏观" },
  { keys: ["onchain"], label: "链上" },
  { keys: ["overall", "total", "composite"], label: "综合" },
];

function radarValues(ds: Record<string, number> | null | undefined): { label: string; value: number }[] {
  if (!ds) return [];
  const lower: Record<string, number> = {};
  for (const [k, v] of Object.entries(ds)) {
    const n = typeof v === "number" ? v : Number(v);
    if (Number.isFinite(n)) lower[k.toLowerCase()] = n;
  }
  const out: { label: string; value: number }[] = [];
  for (const def of RADAR_DEFS) {
    for (const k of def.keys) {
      if (lower[k] !== undefined) {
        out.push({ label: def.label, value: scoreToPct(lower[k]) });
        break;
      }
    }
  }
  return out;
}

function fmtEvidenceValue(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (typeof v === "number") return Number.isFinite(v) ? String(Math.round(v * 10000) / 10000) : "—";
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "是" : "否";
  return JSON.stringify(v);
}

const TONE_HEX: Record<string, string> = {
  up: "#3fb950",
  down: "#f85149",
  warn: "#d29922",
  accent: "#58a6ff",
  muted: "#8b949e",
};

export default function RiskPage() {
  const hydrated = useHydrated();
  const mode = useUIStore((s) => s.mode);
  const isPro = hydrated && mode === "pro"; // mode 必须无条件调用（rules-of-hooks）

  const riskApi = useApi<EngineOutputResponse>(() => apiClient.engine.getRisk(), [], {
    refreshInterval: 120_000,
  });

  const out: EngineOutput | null = riskApi.data?.output ?? null;
  const levelIndex = riskLevelIndex(out?.state);
  const tone = out ? semanticTone(out.state) : "muted";
  const score = out?.score !== undefined && out.score !== null ? scoreToPct(out.score) : null;

  const radar = useMemo(() => radarValues(out?.dimension_scores), [out?.dimension_scores]);

  const radarOption: EChartsOption = useMemo(() => {
    if (radar.length < 3) return {};
    return {
      tooltip: { ...baseTooltip() },
      radar: {
        indicator: radar.map((r) => ({ name: r.label, max: 100 })),
        radius: "68%",
        center: ["50%", "54%"],
        axisName: { color: "#8b949e", fontSize: 11 },
        splitLine: { lineStyle: { color: "#21262d" } },
        splitArea: { areaStyle: { color: ["transparent"] } },
        axisLine: { lineStyle: { color: "#30363d" } },
      },
      series: [
        {
          type: "radar",
          data: [
            {
              value: radar.map((r) => r.value),
              name: "风险维度",
              areaStyle: { color: "rgba(248,81,73,0.18)" },
              lineStyle: { color: "#f85149", width: 1.8 },
              itemStyle: { color: "#f85149" },
              symbolSize: 4,
            },
          ],
        },
      ],
    };
  }, [radar]);

  /** 支持 + 反对证据合并为因子明细 */
  const factors = useMemo<EngineEvidence[]>(() => {
    if (!out) return [];
    return [...out.supporting_evidence, ...out.opposing_evidence];
  }, [out]);

  const [openIdx, setOpenIdx] = useState<number | null>(null);

  return (
    <div>
      <PageHeader
        title="风险监测"
        subtitle="当前市场的综合风险水平 · 由 Risk Engine 基于估值、杠杆、流动性、宏观等维度合成"
        right={
          out?.calculated_at && hydrated ? (
            <span className="font-mono text-[11px] tabular-nums text-muted">
              计算于 {fmtDateTime(out.calculated_at)}
            </span>
          ) : undefined
        }
      />

      {riskApi.error && !out && (
        <ErrorState
          error={riskApi.error}
          lastUpdatedAt={riskApi.lastUpdatedAt}
          onRetry={riskApi.refresh}
          className="mb-4"
        />
      )}

      {riskApi.loading && !out ? (
        <LoadingSkeleton variant="card" />
      ) : !out ? (
        <ErrorState error={riskApi.error ?? undefined} lastUpdatedAt={riskApi.lastUpdatedAt} onRetry={riskApi.refresh}>
          <p className="text-xs text-muted">
            风险引擎暂无输出（可能尚未完成首轮计算）。数据落库后此页会自动展示。
          </p>
        </ErrorState>
      ) : (
        <div className="space-y-5">
          {/* 综合风险等级 */}
          <section className="rounded-xl border border-line bg-bg-raised p-5 lg:p-8">
            <div className="mx-auto max-w-2xl">
              <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
                <div>
                  <p className="font-mono text-[10px] uppercase tracking-[0.24em] text-muted">
                    综合风险等级
                  </p>
                  <p
                    className="mt-1.5 text-4xl font-bold tracking-tight"
                    style={{ color: TONE_HEX[tone] }}
                  >
                    {stateLabel(out.state)}
                  </p>
                  <p className="mt-1 font-mono text-xs tabular-nums text-muted">
                    置信度 {Math.round(out.confidence * 100)}%
                    {score !== null ? ` · 风险分 ${score}/100` : ""}
                  </p>
                </div>
                {isPro && (
                  <div className="text-right">
                    <p className="font-mono text-[10px] uppercase tracking-wider text-muted">原始状态</p>
                    <p className="font-mono text-sm text-[#e6edf3]">{out.state}</p>
                  </div>
                )}
              </div>

              <SegmentBar
                levels={RISK_LEVELS}
                activeIndex={levelIndex}
                caption="风险等级高不代表马上下跌，而是要求更保守的仓位与更严格的止损纪律。"
              />
            </div>
          </section>

          {/* 雷达图 + 阶段解释 */}
          <section className="grid gap-4 lg:grid-cols-2">
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle right={<span className="text-[10px] text-muted">数值越高风险越高</span>}>
                7 维风险雷达
              </SectionTitle>
              {radar.length >= 3 ? (
                <EChart option={radarOption} height={320} />
              ) : (
                <p className="py-16 text-center text-xs text-muted">
                  引擎未返回足够的维度得分（{radar.length}/7）
                </p>
              )}
            </div>

            <div className="flex flex-col justify-center rounded-xl border border-line bg-bg-raised p-5">
              <p className="font-mono text-[10px] uppercase tracking-[0.24em] text-muted">阶段解释</p>
              <p className="mt-3 text-sm leading-relaxed text-[#c9d1d9]">
                {out.explanation ?? "系统暂未给出该风险状态的通俗解释。"}
              </p>
              {out.data_gaps && out.data_gaps.length > 0 && (
                <div className="mt-4 rounded-lg border border-warn/30 bg-warn/5 p-3">
                  <p className="text-[11px] font-medium text-warn">数据缺口</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-muted">
                    {out.data_gaps.join("、")}（相关维度得分置信度可能下降）
                  </p>
                </div>
              )}
              {isPro && out.model_version && (
                <p className="mt-4 font-mono text-[11px] text-muted">模型版本：{out.model_version}</p>
              )}
            </div>
          </section>

          {/* 因子明细 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4">
            <SectionTitle right={<span className="text-[10px] text-muted">点击行展开明细</span>}>
              风险因子明细（{factors.length}）
            </SectionTitle>

            {factors.length === 0 ? (
              <p className="py-8 text-center text-xs text-muted">引擎暂未输出风险因子明细。</p>
            ) : (
              <ul className="divide-y divide-line">
                {factors.map((e, i) => {
                  const open = openIdx === i;
                  const supports = e.supports !== false;
                  return (
                    <li key={`${e.factor}-${i}`}>
                      <button
                        type="button"
                        onClick={() => setOpenIdx(open ? null : i)}
                        aria-expanded={open}
                        className="flex w-full items-center gap-3 py-2.5 text-left transition-colors hover:bg-bg-hover/40"
                      >
                        <span
                          aria-hidden
                          className={`h-1.5 w-1.5 shrink-0 rounded-full ${supports ? "bg-up" : "bg-down"}`}
                        />
                        <span className="min-w-0 flex-1 truncate text-xs font-medium text-[#e6edf3]">
                          {e.factor}
                        </span>
                        <span className="hidden shrink-0 font-mono text-[11px] tabular-nums text-muted sm:block">
                          {fmtEvidenceValue(e.value)}
                        </span>
                        {typeof e.weight === "number" && Number.isFinite(e.weight) && (
                          <span className="hidden w-16 shrink-0 text-right font-mono text-[11px] tabular-nums text-muted md:block">
                            权重 {Math.round(e.weight <= 1 ? e.weight * 100 : e.weight)}%
                          </span>
                        )}
                        <span aria-hidden className={`shrink-0 text-[10px] text-muted transition-transform ${open ? "rotate-180" : ""}`}>
                          ▼
                        </span>
                      </button>

                      {open && (
                        <dl className="grid gap-x-8 gap-y-2 rounded-lg border border-line bg-bg px-4 py-3 text-[11px] sm:grid-cols-2">
                          <div className="flex justify-between gap-4">
                            <dt className="shrink-0 text-muted">当前值</dt>
                            <dd className="text-right font-mono tabular-nums text-[#e6edf3]">
                              {fmtEvidenceValue(e.value)}
                            </dd>
                          </div>
                          {typeof e.weight === "number" && (
                            <div className="flex justify-between gap-4">
                              <dt className="shrink-0 text-muted">权重</dt>
                              <dd className="text-right font-mono tabular-nums text-[#e6edf3]">
                                {e.weight <= 1 ? (e.weight * 100).toFixed(1) + "%" : String(e.weight)}
                              </dd>
                            </div>
                          )}
                          {typeof e.confidence === "number" && (
                            <div className="flex justify-between gap-4">
                              <dt className="shrink-0 text-muted">置信度</dt>
                              <dd className="text-right font-mono tabular-nums text-[#e6edf3]">
                                {Math.round(e.confidence <= 1 ? e.confidence * 100 : e.confidence)}%
                              </dd>
                            </div>
                          )}
                          {typeof e.historical_percentile === "number" && (
                            <div className="flex justify-between gap-4">
                              <dt className="shrink-0 text-muted">历史分位</dt>
                              <dd className="text-right font-mono tabular-nums text-[#e6edf3]">
                                {Math.round(
                                  e.historical_percentile <= 1 ? e.historical_percentile * 100 : e.historical_percentile,
                                )}
                                %
                              </dd>
                            </div>
                          )}
                          {e.interpretation && (
                            <div className="flex justify-between gap-4 sm:col-span-2">
                              <dt className="shrink-0 text-muted">解读</dt>
                              <dd className="text-right leading-relaxed text-[#c9d1d9]">{e.interpretation}</dd>
                            </div>
                          )}
                          {e.data_source && (
                            <div className="flex justify-between gap-4">
                              <dt className="shrink-0 text-muted">数据源</dt>
                              <dd className="text-right font-mono text-[#e6edf3]">{e.data_source}</dd>
                            </div>
                          )}
                          <div className="flex justify-between gap-4">
                            <dt className="shrink-0 text-muted">方向</dt>
                            <dd className={`text-right ${supports ? "text-up" : "text-down"}`}>
                              {supports ? "支持当前结论" : "反对当前结论"}
                            </dd>
                          </div>
                        </dl>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>

          {/* 证据两列 */}
          <section className="grid gap-4 md:grid-cols-2">
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting: out.supporting_evidence, opposing: [] }}
                emptyText="暂无支持证据"
              />
            </div>
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting: [], opposing: out.opposing_evidence }}
                emptyText="暂无反向证据"
              />
            </div>
          </section>

          {/* 专业模式：维度原始分 */}
          {isPro && out.dimension_scores && Object.keys(out.dimension_scores).length > 0 && (
            <section className="rounded-xl border border-line bg-bg-raised p-4 text-[11px]">
              <SectionTitle>维度原始得分（专业模式）</SectionTitle>
              <div className="grid gap-x-8 gap-y-1.5 sm:grid-cols-2 lg:grid-cols-3">
                {Object.entries(out.dimension_scores).map(([key, v]) => (
                  <div key={key} className="flex justify-between gap-4">
                    <span className="text-muted">{labelDimension(key)}</span>
                    <span className="font-mono tabular-nums text-[#e6edf3]">
                      {typeof v === "number" ? v.toFixed(4) : String(v)}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
