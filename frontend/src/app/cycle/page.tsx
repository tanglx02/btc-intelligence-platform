"use client";

/**
 * 市场周期页：
 * - 9 阶段水平时间轴（当前阶段高亮）
 * - 置信度 + 阶段解释
 * - 支持证据 vs 反对证据（两列）
 * - 历史相似阶段（后端返回 historical_similar 时渲染）
 * - 普通模式：阶段名 + 解释；专业模式：各因子得分 / 模型版本 / 数据缺口
 */

import { ErrorState } from "@/components/common/ErrorState";
import { EvidenceList } from "@/components/common/EvidenceList";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { PageHeader, SectionTitle } from "@/components/market/kit";
import { useApi } from "@/hooks/useApi";
import { useHydrated } from "@/hooks/useHydrated";
import {
  CYCLE_STAGES,
  fmtDateTime,
  formatPct,
  labelDimension,
  matchCycleStageIndex,
  pickNumber,
  pickString,
  scoreToPct,
  semanticTone,
  stateLabel,
} from "@/components/market/utils";
import { apiClient } from "@/lib/api";
import { useUIStore } from "@/stores/ui";
import type { EngineEvidence, EngineOutput, EngineOutputResponse } from "@/types/api";

const STAGE_TONE_HEX: Record<string, string> = {
  up: "#3fb950",
  down: "#f85149",
  warn: "#d29922",
  accent: "#58a6ff",
  muted: "#8b949e",
};

/** 阶段固有语义色（时间轴底色）：熊市红、底部灰、上涨绿、顶部红 */
const STAGE_BASE_HEX = [
  "#f85149",
  "#f85149",
  "#8b949e",
  "#58a6ff",
  "#3fb950",
  "#3fb950",
  "#d29922",
  "#f85149",
  "#f85149",
];

/** 历史相似阶段的宽松渲染（后端字段不固定） */
function HistoricalSimilar({ raw }: { raw: unknown }) {
  if (raw === null || raw === undefined) return null;

  const items: unknown[] = Array.isArray(raw)
    ? raw
    : typeof raw === "object" && Array.isArray((raw as Record<string, unknown>).items)
      ? ((raw as Record<string, unknown>).items as unknown[])
      : [];

  if (items.length > 0) {
    return (
      <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
        {items.slice(0, 6).map((item, i) => {
          const date = pickString(item, ["date", "period", "time", "start_date", "label"]) ?? `#${i + 1}`;
          const similarity = pickNumber(item, ["similarity", "similarity_pct", "score", "similar_pct"]);
          return (
            <div key={`${date}-${i}`} className="rounded-lg border border-line bg-bg p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="font-mono text-xs font-semibold tabular-nums text-[#e6edf3]">{date}</span>
                {similarity !== null && (
                  <span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-accent">
                    相似度 {Math.round(similarity <= 1 ? similarity * 100 : similarity)}%
                  </span>
                )}
              </div>
              <p className="mt-1.5 text-[11px] leading-relaxed text-muted">
                {pickString(item, ["outcome", "summary", "note", "description"]) ??
                  (pickNumber(item, ["subsequent_90d_pct", "return_90d", "forward_return_pct"]) !== null
                    ? `后续 90 天 ${formatPct(pickNumber(item, ["subsequent_90d_pct", "return_90d", "forward_return_pct"]))}`
                    : null) ??
                  "历史上该阶段与当前特征相似。"}
              </p>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <p className="text-xs leading-relaxed text-muted">
      {typeof raw === "string" ? raw : JSON.stringify(raw)}
    </p>
  );
}

export default function CyclePage() {
  const hydrated = useHydrated();
  const mode = useUIStore((s) => s.mode);
  const isPro = hydrated && mode === "pro"; // mode 必须无条件调用（rules-of-hooks）

  const cycleApi = useApi<EngineOutputResponse>(() => apiClient.engine.getCycle(), [], {
    refreshInterval: 120_000,
  });

  const out: EngineOutput | null = cycleApi.data?.output ?? null;
  const stageIndex = matchCycleStageIndex(out?.state);
  const tone = out ? semanticTone(out.state) : "muted";
  const explanation =
    out?.explanation ??
    (stageIndex >= 0 ? CYCLE_STAGES[stageIndex].desc : "系统暂未给出该阶段的通俗解释。");
  const supporting = out?.supporting_evidence ?? [];
  const opposing = out?.opposing_evidence ?? [];
  const dimensionScores = out?.dimension_scores ?? null;
  const similar = out?.historical_similar ?? null;

  const stageIdxForPro =
    stageIndex >= 0 ? `第 ${stageIndex + 1}/${CYCLE_STAGES.length} 阶段` : null;

  return (
    <div>
      <PageHeader
        title="市场周期"
        subtitle="BTC 当前处于历史周期的哪个阶段 · 结论由 Cycle Engine 基于链上与市场指标推断"
        right={
          out?.calculated_at && hydrated ? (
            <span className="font-mono text-[11px] tabular-nums text-muted">
              计算于 {fmtDateTime(out.calculated_at)}
            </span>
          ) : undefined
        }
      />

      {cycleApi.error && !out && (
        <ErrorState
          error={cycleApi.error}
          lastUpdatedAt={cycleApi.lastUpdatedAt}
          onRetry={cycleApi.refresh}
          className="mb-4"
        />
      )}

      {cycleApi.loading && !out ? (
        <LoadingSkeleton variant="card" />
      ) : !out ? (
        <ErrorState
          error={cycleApi.error ?? undefined}
          lastUpdatedAt={cycleApi.lastUpdatedAt}
          onRetry={cycleApi.refresh}
        >
          <p className="text-xs text-muted">
            周期引擎暂无输出（可能尚未完成首轮计算）。数据落库后此页会自动展示。
          </p>
        </ErrorState>
      ) : (
        <div className="space-y-5">
          {/* 9 阶段时间轴 */}
          <section className="rounded-xl border border-line bg-bg-raised p-4 lg:p-6">
            <div className="relative">
              <div aria-hidden className="absolute left-0 right-0 top-[9px] h-px bg-line" />
              <ol className="relative flex justify-between gap-1 overflow-x-auto">
                {CYCLE_STAGES.map((stage, i) => {
                  const active = i === stageIndex;
                  const passed = stageIndex >= 0 && i < stageIndex;
                  return (
                    <li key={stage.label} className="flex min-w-[64px] flex-1 flex-col items-center gap-2">
                      <span
                        aria-hidden
                        className={`h-[19px] w-[19px] rounded-full border-2 transition-all duration-300 ${
                          active ? "scale-125 shadow-[0_0_12px_rgba(88,166,255,0.4)]" : ""
                        }`}
                        style={{
                          borderColor: active ? "#58a6ff" : STAGE_BASE_HEX[i],
                          backgroundColor: active ? "#58a6ff" : passed ? `${STAGE_BASE_HEX[i]}55` : "transparent",
                        }}
                      />
                      <span
                        className={`text-center text-[11px] leading-tight ${
                          active ? "font-semibold text-accent" : passed ? "text-[#8b949e]" : "text-muted/70"
                        }`}
                      >
                        {stage.label}
                      </span>
                      {active && (
                        <span className="rounded border border-accent/40 bg-accent/10 px-1.5 py-0.5 text-[9px] font-medium text-accent">
                          当前
                        </span>
                      )}
                    </li>
                  );
                })}
              </ol>
            </div>

            <div className="mt-6 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-4">
              <div>
                <p className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted">
                  当前阶段
                </p>
                <p className="mt-1 flex items-baseline gap-3">
                  <span
                    className="text-2xl font-bold"
                    style={{ color: STAGE_TONE_HEX[tone] }}
                  >
                    {stateLabel(out.state)}
                  </span>
                  <span className="font-mono text-xs tabular-nums text-muted">
                    置信度 {Math.round(out.confidence * 100)}%
                    {isPro && stageIdxForPro ? ` · ${stageIdxForPro}` : ""}
                  </span>
                </p>
              </div>
              {out.score !== undefined && out.score !== null && (
                <div className="text-right">
                  <p className="font-mono text-[10px] uppercase tracking-wider text-muted">周期评分</p>
                  <p className="font-mono text-xl font-semibold tabular-nums text-[#e6edf3]">
                    {scoreToPct(out.score)}
                    <span className="text-xs text-muted">/100</span>
                  </p>
                </div>
              )}
            </div>

            <p className="mt-3 text-xs leading-relaxed text-[#c9d1d9]">{explanation}</p>
          </section>

          {/* 历史相似阶段 */}
          {similar !== null && (
            <section>
              <SectionTitle right={<span className="text-[10px] text-muted">历史结果不代表未来</span>}>
                历史相似阶段
              </SectionTitle>
              <HistoricalSimilar raw={similar} />
            </section>
          )}

          {/* 证据两列 */}
          <section className="grid gap-4 md:grid-cols-2">
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting, opposing: [] }}
                emptyText="暂无支持证据"
              />
            </div>
            <div className="rounded-xl border border-line bg-bg-raised p-4">
              <EvidenceList
                evidence={{ supporting: [], opposing }}
                emptyText="暂无反对证据"
              />
            </div>
          </section>

          {/* 专业模式 */}
          {isPro && (
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle>因子得分（专业模式）</SectionTitle>
              {dimensionScores && Object.keys(dimensionScores).length > 0 ? (
                <div className="space-y-2.5">
                  {Object.entries(dimensionScores).map(([key, value]) => {
                    const pct = scoreToPct(value);
                    return (
                      <div key={key}>
                        <div className="mb-1 flex items-center justify-between text-[11px]">
                          <span className="text-[#c9d1d9]">{labelDimension(key)}</span>
                          <span className="font-mono tabular-nums text-muted">
                            {pct}
                            {typeof value === "number" && value <= 1
                              ? `（原始 ${value.toFixed(3)}）`
                              : ""}
                          </span>
                        </div>
                        <div className="h-1.5 overflow-hidden rounded-full bg-bg-hover">
                          <div
                            className="h-full rounded-full bg-accent/70 transition-all duration-500"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : (
                <p className="text-xs text-muted">引擎未返回分维度得分。</p>
              )}

              <dl className="mt-4 grid gap-x-8 gap-y-2 border-t border-line pt-3 text-[11px] sm:grid-cols-2">
                {out.model_version && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-muted">模型版本</dt>
                    <dd className="font-mono text-[#e6edf3]">{out.model_version}</dd>
                  </div>
                )}
                {out.observation_time && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-muted">观测时间</dt>
                    <dd className="font-mono tabular-nums text-[#e6edf3]">
                      {hydrated ? fmtDateTime(out.observation_time) : out.observation_time.slice(0, 10)}
                    </dd>
                  </div>
                )}
                {out.data_quality && (
                  <div className="flex justify-between gap-4">
                    <dt className="text-muted">数据质量</dt>
                    <dd className="font-mono text-[#e6edf3]">{out.data_quality}</dd>
                  </div>
                )}
                {out.data_gaps && out.data_gaps.length > 0 && (
                  <div className="flex justify-between gap-4 sm:col-span-2">
                    <dt className="shrink-0 text-muted">数据缺口</dt>
                    <dd className="text-right font-mono text-warn">{out.data_gaps.join("、")}</dd>
                  </div>
                )}
              </dl>
            </section>
          )}

          {/* 证据明细（专业模式下补充权重） */}
          {isPro && supporting.length + opposing.length > 0 && (
            <section className="rounded-xl border border-line bg-bg-raised p-4">
              <SectionTitle>证据权重明细（专业模式）</SectionTitle>
              <ul className="space-y-1.5">
                {[...supporting, ...opposing].map((ev: EngineEvidence, i: number) => (
                  <li
                    key={`${ev.factor}-${i}`}
                    className="flex items-center justify-between gap-4 rounded-lg bg-bg-hover/50 px-3 py-2 text-xs"
                  >
                    <span className="font-medium text-[#e6edf3]">{ev.factor}</span>
                    <span className="flex items-center gap-3 font-mono text-[11px] tabular-nums text-muted">
                      {ev.historical_percentile != null && (
                        <span>分位 {Math.round(ev.historical_percentile * 100)}%</span>
                      )}
                      {ev.weight != null && <span>权重 {Math.round(ev.weight * 100)}%</span>}
                      <span className={ev.supports ? "text-up" : "text-down"}>
                        {ev.supports ? "支持" : "反对"}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
