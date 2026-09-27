"use client";

/**
 * 模型实验室 —— 引擎模型版本与验证结果查看（docs/architecture/14 §7、16 §8.19）。
 *
 * 四大引擎（cycle / valuation / risk / regime）+ 预测引擎：
 * 版本号 / 输出状态 / 置信度 → 详情（参数、适用条件、近期输出统计）。
 * 禁止任何「精准预测」话术；预测面板固定展示不确定性说明。
 */

import { useMemo, useState } from "react";

import { Badge, PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { GhostButton } from "@/components/portfolio/Modal";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import {
  cn,
  engineLabel,
  formatDate,
  formatDateTime,
  pickNum,
  pickStr,
  stateTone,
  translateState,
} from "@/components/portfolio/utils";
import { useApi } from "@/hooks/useApi";
import { apiClient, type ApiRequestError } from "@/lib/api";
import type { EngineOutput } from "@/types/api";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";

const ENGINES = ["cycle", "valuation", "risk", "regime"] as const;
type EngineKey = (typeof ENGINES)[number];

const TONE_TEXT: Record<string, string> = {
  up: "text-up",
  down: "text-down",
  warn: "text-warn",
  accent: "text-accent",
  muted: "text-muted",
};

/** 近期输出统计（In-Sample/Out-of-Sample 报告的本地近似口径） */
interface OutputStats {
  count: number;
  avgConfidence: number | null;
  stateDist: { state: string; count: number; pct: number }[];
  lastChange: string | null;
}

function computeStats(outputs: EngineOutput[]): OutputStats {
  if (!outputs.length) return { count: 0, avgConfidence: null, stateDist: [], lastChange: null };
  const confs = outputs.map((o) => o.confidence).filter((c): c is number => typeof c === "number");
  const counts = new Map<string, number>();
  for (const o of outputs) {
    const s = (o.state ?? "UNKNOWN").toUpperCase();
    counts.set(s, (counts.get(s) ?? 0) + 1);
  }
  const total = outputs.length;
  const stateDist = [...counts.entries()]
    .map(([state, count]) => ({ state, count, pct: (count / total) * 100 }))
    .sort((a, b) => b.count - a.count);

  let lastChange: string | null = null;
  for (let i = 1; i < outputs.length; i += 1) {
    if (outputs[i].state !== outputs[i - 1].state) {
      lastChange = outputs[i - 1].calculated_at ?? outputs[i - 1].observation_time ?? null;
      break;
    }
  }

  return {
    count: total,
    avgConfidence: confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : null,
    stateDist,
    lastChange,
  };
}

/* -------------------------------------------------------------------------- */
/* 引擎输出卡                                                                  */
/* -------------------------------------------------------------------------- */

function EngineOutputCard({
  engine,
  resp,
  selected,
  onSelect,
}: {
  engine: EngineKey;
  resp: { engine: string; output: EngineOutput | null } | undefined;
  selected: boolean;
  onSelect: () => void;
}) {
  const output = resp?.output ?? null;
  const tone = stateTone(output?.state);
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "rounded-xl border p-4 text-left transition-all",
        selected ? "border-accent/60 bg-accent/[0.06]" : "border-line bg-bg-raised/60 hover:border-line",
      )}
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-[#e6edf3]">{engineLabel(engine)}</h3>
        <span aria-hidden className={cn("text-[10px]", selected ? "text-accent" : "text-muted")}>
          {selected ? "◉" : "○"}
        </span>
      </div>
      {output ? (
        <>
          <p className={cn("mt-2 font-mono text-lg font-semibold leading-tight", TONE_TEXT[tone])}>
            {translateState(output.state)}
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted">
            <span className="font-mono tabular-nums">置信 {Math.round(output.confidence * 100)}%</span>
            {typeof output.score === "number" && (
              <span className="font-mono tabular-nums">评分 {output.score.toFixed(2)}</span>
            )}
            {output.model_version && (
              <Badge className="border-line bg-bg-hover text-muted">{output.model_version}</Badge>
            )}
          </div>
          <p className="mt-2 text-[10px] text-muted/70">
            {output.calculated_at ? `计算于 ${formatDateTime(output.calculated_at)}` : ""}
          </p>
        </>
      ) : (
        <p className="mt-2 text-xs text-muted">该引擎暂无输出（可能尚未运行或数据不足）</p>
      )}
    </button>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function ModelLabPage() {
  const toast = useToast();
  const [selected, setSelected] = useState<EngineKey>("cycle");

  const dashboard = useApi(() => apiClient.engine.getDashboard(false), []);
  const prediction = useApi(() => apiClient.engine.getPrediction(), []);
  const history = useApi(
    () => apiClient.engine.getHistory({ engine: selected, limit: 90 }),
    [selected],
  );

  const stats = useMemo(() => computeStats(history.data?.outputs ?? []), [history.data]);

  const selectedOutput = dashboard.data?.[selected]?.output ?? null;

  const predictionData = (prediction.data ?? null) as Record<string, unknown> | null;
  const predDirection = pickStr(predictionData ?? {}, ["direction", "signal", "outlook"]);
  const predConf = pickNum(predictionData ?? {}, ["confidence"]);
  const predHorizon = pickNum(predictionData ?? {}, ["horizon_days", "horizon"]);
  const predProbs = (predictionData?.probabilities ?? predictionData?.probability ?? null) as
    | Record<string, unknown>
    | null;

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="06"
        kicker="Research · Model Lab"
        title="模型实验室"
        description="引擎模型的版本管理、验证结果与输出追踪。任何模型升级都不可变存档；回测与回放消费的历史输出永不因模型升级而改变。"
        action={<GhostButton onClick={dashboard.refresh}>刷新</GhostButton>}
      />

      <ToastHost toast={toast.toast} />

      {/* 引擎卡网格 */}
      {dashboard.loading && !dashboard.data ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <LoadingSkeleton key={i} variant="card" />
          ))}
        </div>
      ) : dashboard.error && !dashboard.data ? (
        <ErrorState error={dashboard.error as ApiRequestError} onRetry={dashboard.refresh} />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {ENGINES.map((engine) => (
            <EngineOutputCard
              key={engine}
              engine={engine}
              resp={dashboard.data?.[engine]}
              selected={selected === engine}
              onSelect={() => setSelected(engine)}
            />
          ))}
        </div>
      )}

      {/* 详情 */}
      <SectionCard
        title={`${engineLabel(selected)} · 模型详情`}
        extra={
          selectedOutput?.model_version ? (
            <Badge className="border-accent/40 bg-accent/10 text-accent">
              版本 {selectedOutput.model_version}
            </Badge>
          ) : undefined
        }
      >
        {!selectedOutput ? (
          <EmptyState title="该引擎暂无输出" description="引擎尚未运行或历史区间内无判断记录，不会回补伪造。" />
        ) : (
          <div className="space-y-5">
            {/* 结论与适用条件 */}
            <div className="grid gap-3 sm:grid-cols-3">
              <StatCard
                label="当前输出"
                value={translateState(selectedOutput.state)}
                tone={stateTone(selectedOutput.state)}
                sub={typeof selectedOutput.score === "number" ? `综合评分 ${selectedOutput.score.toFixed(2)}` : undefined}
              />
              <StatCard
                label="置信度"
                value={`${Math.round(selectedOutput.confidence * 100)}%`}
                tone="accent"
                sub={selectedOutput.data_quality ? `数据质量 ${selectedOutput.data_quality}` : undefined}
              />
              <StatCard
                label="证据构成"
                value={`${selectedOutput.supporting_evidence.length} 支持 / ${selectedOutput.opposing_evidence.length} 反对`}
                sub="展开证据链请前往对应引擎页面"
              />
            </div>

            {selectedOutput.explanation && (
              <div className="rounded-lg border border-line bg-bg/60 px-4 py-3">
                <p className="text-[10px] font-medium uppercase tracking-wider text-muted">适用条件说明</p>
                <p className="mt-1.5 text-xs leading-relaxed text-[#c9d1d9]">{selectedOutput.explanation}</p>
              </div>
            )}

            {/* 参数（维度得分） */}
            {selectedOutput.dimension_scores && Object.keys(selectedOutput.dimension_scores).length > 0 && (
              <div>
                <p className="mb-2 text-[11px] font-medium text-muted">模型参数 · 维度得分</p>
                <div className="overflow-x-auto rounded-xl border border-line">
                  <table className="w-full min-w-[320px] text-left text-xs">
                    <thead>
                      <tr className="border-b border-line bg-bg-hover/40 text-[10px] uppercase tracking-wider text-muted">
                        <th className="px-4 py-2 font-medium">维度</th>
                        <th className="px-4 py-2 text-right font-medium">得分</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(selectedOutput.dimension_scores).map(([dim, score]) => (
                        <tr key={dim} className="border-b border-line/40 last:border-0">
                          <td className="px-4 py-2 font-mono text-muted">{dim}</td>
                          <td className="px-4 py-2 text-right font-mono tabular-nums">
                            {typeof score === "number" ? score.toFixed(3) : String(score)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* 近期输出统计（IS/OOS 近似口径） */}
            <div>
              <div className="mb-2 flex items-center justify-between">
                <p className="text-[11px] font-medium text-muted">验证结果摘要 · 近 90 次输出统计</p>
                <GhostButton onClick={history.refresh}>刷新统计</GhostButton>
              </div>
              {history.loading && !history.data ? (
                <LoadingSkeleton variant="lines" rows={3} />
              ) : history.error && !history.data ? (
                <ErrorState error={history.error as ApiRequestError} onRetry={history.refresh} compact />
              ) : stats.count === 0 ? (
                <p className="rounded-lg border border-dashed border-line px-4 py-4 text-center text-xs text-muted">
                  该引擎在本地尚无历史输出记录
                </p>
              ) : (
                <div className="space-y-3">
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <StatCard label="样本数" value={stats.count} sub="近 90 次输出（In-Sample 近似）" />
                    <StatCard
                      label="平均置信度"
                      value={stats.avgConfidence !== null ? `${Math.round(stats.avgConfidence * 100)}%` : "—"}
                      tone="accent"
                    />
                    <StatCard
                      label="最近状态变化"
                      value={stats.lastChange ? formatDate(stats.lastChange) : "区间内无变化"}
                      sub="as-run 历史输出"
                    />
                    <StatCard
                      label="主导状态"
                      value={translateState(stats.stateDist[0]?.state)}
                      sub={stats.stateDist[0] ? `${stats.stateDist[0].pct.toFixed(0)}% 时间占比` : undefined}
                    />
                  </div>
                  <div className="rounded-xl border border-line p-4">
                    <p className="mb-2.5 text-[10px] uppercase tracking-wider text-muted">状态分布（Out-of-Sample 窗口近似）</p>
                    <div className="space-y-2">
                      {stats.stateDist.slice(0, 5).map((s) => (
                        <div key={s.state} className="flex items-center gap-3">
                          <span className="w-24 shrink-0 truncate text-[11px] text-[#c9d1d9]">{translateState(s.state)}</span>
                          <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-bg-hover">
                            <div
                              className="h-full rounded-full bg-accent/70 transition-all"
                              style={{ width: `${s.pct}%` }}
                            />
                          </div>
                          <span className="w-12 shrink-0 text-right font-mono text-[10px] tabular-nums text-muted">
                            {s.pct.toFixed(0)}%
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        )}
      </SectionCard>

      {/* 预测引擎 */}
      <SectionCard title="预测引擎 · Prediction">
        {prediction.loading && !prediction.data ? (
          <LoadingSkeleton variant="card" />
        ) : prediction.error && !prediction.data ? (
          <ErrorState error={prediction.error as ApiRequestError} onRetry={prediction.refresh} compact />
        ) : !predictionData ? (
          <p className="text-xs text-muted">当前无法可靠预测 —— 样本或证据不足时，预测系统主动沉默。</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <StatCard label="方向判断" value={predDirection ?? "—"} tone="accent" />
            <StatCard
              label="置信度"
              value={predConf !== null ? `${Math.round(predConf * 100)}%` : "—"}
              sub={predHorizon !== null ? `展望 ${predHorizon} 天` : undefined}
            />
            {predProbs &&
              Object.entries(predProbs)
                .slice(0, 2)
                .map(([k, v]) => (
                  <StatCard
                    key={k}
                    label={`概率 · ${k}`}
                    value={typeof v === "number" ? `${(v * 100).toFixed(1)}%` : String(v ?? "—")}
                    tone="muted"
                  />
                ))}
          </div>
        )}
        <p className="mt-4 rounded-lg border border-dashed border-warn/35 bg-warn/[0.04] px-4 py-2.5 text-[11px] italic leading-relaxed text-warn/90">
          概率输出存在固有不确定性：样本数量有限、市场结构变化可能使历史规律失效。禁止将模型输出理解为「精准预测」，历史命中率不代表未来准确度。
        </p>
      </SectionCard>

      <p className="text-center text-[10px] leading-relaxed text-muted/70">
        完整 Walk-Forward 验证与 A/B 报告由模型管理端点（/models，Analyst+）提供；本页统计口径为本地 as-run 输出。
      </p>
    </div>
  );
}
