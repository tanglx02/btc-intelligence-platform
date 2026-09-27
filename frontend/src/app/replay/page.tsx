"use client";

/**
 * 历史回放 —— 回到任意历史日期，以「当时视角 / 事后视角」查看系统状态
 * （docs/architecture/14 §5、16 §8.13）。
 *
 * - 当时视角（默认）：严格只显示该日期前已知信息（PIT / as-run 引擎输出），
 *   若该日期无判断记录则明示，不回补伪造；
 * - 事后视角：追加未来 30/90/180/365 天实际走势，固定醒目提示「仅供学习」。
 */

import { useMemo, useState } from "react";

import { PricePathChart } from "@/components/portfolio/charts";
import { Disclaimer } from "@/components/portfolio/Disclaimer";
import { Field } from "@/components/portfolio/Form";
import { Badge, PageHeader, SectionCard, StatCard } from "@/components/portfolio/StatCard";
import { GhostButton } from "@/components/portfolio/Modal";
import { ToastHost, useToast } from "@/components/portfolio/toast";
import {
  cn,
  engineLabel,
  formatDate,
  formatMoney,
  formatPct,
  stateTone,
  translateState,
  todayISO,
  addDaysISO,
} from "@/components/portfolio/utils";
import { useApi } from "@/hooks/useApi";
import { apiClient, type ApiRequestError } from "@/lib/api";
import type { Candle, EngineOutput } from "@/types/api";
import { EmptyState } from "@/components/common/EmptyState";
import { ErrorState } from "@/components/common/ErrorState";
import { LoadingSkeleton } from "@/components/common/LoadingSkeleton";
import { TrendIndicator } from "@/components/common/TrendIndicator";

const MIN_DATE = "2015-01-01";

/** 快捷历史事件 */
const QUICK_EVENTS: { date: string; label: string }[] = [
  { date: "2020-03-12", label: "3·12 崩盘" },
  { date: "2020-10-20", label: "突破前高" },
  { date: "2021-01-08", label: "Tesla 买入" },
  { date: "2021-11-10", label: "周期顶" },
  { date: "2022-06-18", label: "LUNA 低点" },
  { date: "2024-04-20", label: "第四次减半" },
];

type View = "then" | "after";

function candleDate(c: Candle): string {
  if (typeof c.time === "number") {
    return formatDate(new Date(c.time * (c.time > 1e11 ? 1 : 1000)));
  }
  return formatDate(c.time);
}

/* -------------------------------------------------------------------------- */
/* 当日价格快照                                                                */
/* -------------------------------------------------------------------------- */

function PriceSnapshot({ date }: { date: string }) {
  const { data, loading, error, refresh } = useApi(
    () => apiClient.market.getOhlcv({ interval: "1d", start: addDaysISO(date, -1), end: addDaysISO(date, 1), limit: 10 }),
    [date],
  );

  const parsed = useMemo(() => {
    const candles = data ?? [];
    const day = candles.find((c) => candleDate(c) === date);
    const prev = candles.find((c) => candleDate(c) === addDaysISO(date, -1));
    if (!day) return null;
    const prevClose = prev?.close ?? day.open;
    const changePct = prevClose ? ((day.close - prevClose) / prevClose) * 100 : null;
    return { day, changePct };
  }, [data, date]);

  if (loading && !data) return <LoadingSkeleton variant="card" />;
  if (error && !data) return <ErrorState error={error as ApiRequestError} onRetry={refresh} compact />;
  if (!parsed) {
    return (
      <EmptyState
        title="该日期无行情数据"
        description="早于数据覆盖起点或为非交易日，系统不会伪造数字。"
      />
    );
  }

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatCard label="当日收盘" value={formatMoney(parsed.day.close, "USD")} tone="accent" sub={`开盘 ${formatMoney(parsed.day.open, "USD")}`} />
      <StatCard
        label="相对前日"
        value={<TrendIndicator value={parsed.changePct} size="sm" />}
        sub="24h 变化（收盘口径）"
      />
      <StatCard label="当日最高" value={formatMoney(parsed.day.high, "USD")} />
      <StatCard label="当日最低" value={formatMoney(parsed.day.low, "USD")} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 当日引擎状态（as-run）                                                      */
/* -------------------------------------------------------------------------- */

function ReplayEngineCard({ engine, date }: { engine: string; date: string }) {
  const { data, loading, error, refresh } = useApi(
    () => apiClient.engine.getHistory({ engine, date, limit: 1 }),
    [engine, date],
  );
  const output: EngineOutput | null = data?.outputs?.[0] ?? null;

  return (
    <div className="rounded-xl border border-line bg-bg-raised/80 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-[#e6edf3]">{engineLabel(engine)}</h3>
        {output?.model_version && (
          <Badge className="border-line bg-bg-hover text-muted">{output.model_version}</Badge>
        )}
      </div>

      {loading && !data ? (
        <div className="mt-3">
          <LoadingSkeleton variant="lines" rows={2} />
        </div>
      ) : error && !data ? (
        <div className="mt-3">
          <ErrorState error={error as ApiRequestError} onRetry={refresh} compact />
        </div>
      ) : !output ? (
        <p className="mt-3 text-[11px] leading-relaxed text-muted">
          该日期系统尚无判断记录（引擎上线前的历史不回补伪造）。
        </p>
      ) : (
        <>
          <p className={cn("mt-2 font-mono text-lg font-semibold leading-tight", `text-${stateTone(output.state)}`)}>
            {translateState(output.state)}
          </p>
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted">
            <span className="font-mono tabular-nums">置信 {Math.round(output.confidence * 100)}%</span>
            {typeof output.score === "number" && (
              <span className="font-mono tabular-nums">评分 {output.score.toFixed(2)}</span>
            )}
          </div>
          {output.supporting_evidence.length > 0 && (
            <div className="mt-3 border-t border-line/60 pt-2.5">
              <p className="mb-1.5 text-[10px] uppercase tracking-wider text-muted">当日关键指标</p>
              <ul className="space-y-1.5">
                {output.supporting_evidence.slice(0, 4).map((ev, i) => (
                  <li key={`${ev.factor}-${i}`} className="flex items-baseline gap-2 text-[11px]">
                    <span className="w-20 shrink-0 truncate font-mono text-accent/90">{ev.factor}</span>
                    <span className="shrink-0 font-mono tabular-nums text-[#e6edf3]">
                      {typeof ev.value === "number" ? ev.value.toFixed(2) : String(ev.value ?? "—")}
                    </span>
                    {ev.interpretation && (
                      <span className="truncate text-muted" title={ev.interpretation}>
                        {ev.interpretation}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 事后视角                                                                    */
/* -------------------------------------------------------------------------- */

interface WindowStat {
  days: number;
  reached: boolean;
  elapsedDays: number;
  endPrice: number | null;
  returnPct: number | null;
  high: number | null;
  low: number | null;
  maxDrawdown: number | null;
}

const HINDSIGHT_WINDOWS = [30, 90, 180, 365];

function HindsightPanel({ date }: { date: string }) {
  const endBound = addDaysISO(date, 366);
  const { data, loading, error, refresh } = useApi(
    () => apiClient.market.getOhlcv({ interval: "1d", start: date, end: endBound, limit: 400 }),
    [date],
  );

  const points = useMemo(() => {
    const today = todayISO();
    return (data ?? [])
      .map((c) => ({ date: candleDate(c), close: c.close }))
      .filter((p) => p.date && p.date <= today);
  }, [data]);

  const stats = useMemo<WindowStat[]>(() => {
    const base = points[0]?.close ?? null;
    if (base === null || base === 0) return [];
    return HINDSIGHT_WINDOWS.map((days) => {
      const elapsedDays = Math.max(0, points.length - 1);
      const reached = elapsedDays >= days;
      const seg = points.slice(0, Math.min(days + 1, points.length));
      if (seg.length < 2) {
        return { days, reached: false, elapsedDays, endPrice: null, returnPct: null, high: null, low: null, maxDrawdown: null };
      }
      const last = seg[seg.length - 1];
      const high = Math.max(...seg.map((p) => p.close));
      const low = Math.min(...seg.map((p) => p.close));
      let peak = -Infinity;
      let maxDD = 0;
      for (const p of seg) {
        peak = Math.max(peak, p.close);
        maxDD = Math.min(maxDD, ((p.close - peak) / peak) * 100);
      }
      return {
        days,
        reached,
        elapsedDays,
        endPrice: last.close,
        returnPct: ((last.close - base) / base) * 100,
        high,
        low,
        maxDrawdown: maxDD,
      };
    });
  }, [points]);

  if (loading && !data) return <LoadingSkeleton variant="card" />;
  if (error && !data) return <ErrorState error={error as ApiRequestError} onRetry={refresh} />;

  return (
    <div className="space-y-4">
      {/* 醒目事后提示 */}
      <div
        className="flex items-start gap-2.5 rounded-xl border border-down/45 bg-down/[0.07] px-4 py-3"
        role="alert"
      >
        <span aria-hidden className="mt-0.5 text-down">
          ⏳
        </span>
        <p className="text-xs font-medium leading-relaxed text-down">
          以下为事后信息，仅供学习 —— 你正在以「上帝视角」查看该日期之后的真实走势。这在当时（{formatDate(date)}）是任何人都不可知的未来。
        </p>
      </div>

      {points.length < 2 ? (
        <EmptyState
          title="该日期之后暂无足够行情数据"
          description="可能太接近今天或超出数据覆盖范围。"
        />
      ) : (
        <>
          <PricePathChart
            points={points}
            height={300}
            windowMarks={HINDSIGHT_WINDOWS.filter((d) => points.length - 1 >= d * 0.05)}
          />

          <div className="overflow-x-auto rounded-xl border border-line">
            <table className="w-full min-w-[560px] text-left text-xs">
              <thead>
                <tr className="border-b border-line bg-bg-hover/40 text-[10px] uppercase tracking-wider text-muted">
                  <th className="px-4 py-2.5 font-medium">未来窗口</th>
                  <th className="px-3 py-2.5 font-medium">状态</th>
                  <th className="px-3 py-2.5 text-right font-medium">期末价</th>
                  <th className="px-3 py-2.5 text-right font-medium">事后收益率</th>
                  <th className="px-3 py-2.5 text-right font-medium">区间最高</th>
                  <th className="px-3 py-2.5 text-right font-medium">区间最低</th>
                  <th className="px-4 py-2.5 text-right font-medium">区间最大回撤</th>
                </tr>
              </thead>
              <tbody>
                {stats.map((s) => (
                  <tr key={s.days} className="border-b border-line/40 last:border-0">
                    <td className="px-4 py-2.5 font-mono tabular-nums">{s.days} 天</td>
                    <td className="px-3 py-2.5">
                      {s.reached ? (
                        <Badge className="border-up/40 bg-up/10 text-up">已到达</Badge>
                      ) : (
                        <Badge className="border-line bg-bg-hover text-muted">
                          未到期（已 {s.elapsedDays} 天）
                        </Badge>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums">{formatMoney(s.endPrice, "USD")}</td>
                    <td className="px-3 py-2.5 text-right">
                      <TrendIndicator value={s.returnPct} size="xs" />
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-muted">{formatMoney(s.high, "USD")}</td>
                    <td className="px-3 py-2.5 text-right font-mono tabular-nums text-muted">{formatMoney(s.low, "USD")}</td>
                    <td className="px-4 py-2.5 text-right font-mono tabular-nums text-down">
                      {s.maxDrawdown !== null ? formatPct(s.maxDrawdown, { sign: false }) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <p className="text-[10px] leading-relaxed text-muted/70">
            事后收益率统计基于日线收盘价（UTC），区间最大回撤按区间内滚动峰值计算；当日系统判断 vs 实际结果的命中率统计将随预测成绩单上线。
          </p>
        </>
      )}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* 页面                                                                        */
/* -------------------------------------------------------------------------- */

export default function ReplayPage() {
  const toast = useToast();
  const [date, setDate] = useState("2024-04-20");
  const [view, setView] = useState<View>("then");

  const changeDate = (d: string) => {
    if (!d) return;
    const clamped = d > todayISO() ? todayISO() : d < MIN_DATE ? MIN_DATE : d;
    setDate(clamped);
    if (d > todayISO() || d < MIN_DATE) {
      toast.show("error", `日期已限定在 ${MIN_DATE} ~ 今天范围内`);
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-6 py-6 lg:py-8">
      <PageHeader
        index="07"
        kicker="Research · Time Machine"
        title="历史回放"
        description="选择 2015 年至今的任意日期，恢复当日系统的全部状态：当时视角严格只使用该日期前已知的信息（PIT + as-run），杜绝未来数据泄漏。"
      />

      <ToastHost toast={toast.toast} />

      {/* 日期与视角 */}
      <SectionCard>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
          <div className="space-y-3">
            <Field label="回放日期" hint={`可选范围 ${MIN_DATE} ~ 今天`}>
              <input
                type="date"
                value={date}
                min={MIN_DATE}
                max={todayISO()}
                onChange={(e) => changeDate(e.target.value)}
                className="w-full rounded-lg border border-line bg-bg px-3 py-2 font-mono text-sm text-[#e6edf3] outline-none transition-colors focus:border-accent/60 sm:w-52"
              />
            </Field>
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-[10px] uppercase tracking-wider text-muted">快捷事件</span>
              {QUICK_EVENTS.map((ev) => (
                <GhostButton
                  key={ev.date}
                  onClick={() => changeDate(ev.date)}
                  className={cn(date === ev.date && "border-accent/50 text-accent")}
                >
                  {ev.label}
                </GhostButton>
              ))}
            </div>
          </div>

          <div>
            <p className="mb-1.5 text-[11px] font-medium text-muted">视角</p>
            <div className="inline-flex rounded-lg border border-line p-1" role="tablist" aria-label="回放视角">
              {(
                [
                  { key: "then", label: "当时视角" },
                  { key: "after", label: "事后视角" },
                ] as { key: View; label: string }[]
              ).map((t) => (
                <button
                  key={t.key}
                  type="button"
                  role="tab"
                  aria-selected={view === t.key}
                  onClick={() => setView(t.key)}
                  className={cn(
                    "rounded-md px-4 py-1.5 text-xs font-medium transition-colors",
                    view === t.key
                      ? "bg-accent/15 text-accent"
                      : "text-muted hover:text-[#e6edf3]",
                  )}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>
      </SectionCard>

      {/* 回放主体（带日期水印） */}
      <div className="relative">
        <div
          aria-hidden
          className="pointer-events-none absolute right-4 top-2 z-0 select-none font-mono text-5xl font-bold tracking-tight text-[#58a6ff]/[0.05] lg:text-7xl"
        >
          {formatDate(date)}
        </div>

        <div className="relative z-10 space-y-6">
          {view === "then" ? (
            <>
              <SectionCard
                title={`当时视角 · ${formatDate(date)}`}
                extra={<Badge className="border-accent/40 bg-accent/10 text-accent">模拟当时信息环境</Badge>}
              >
                <PriceSnapshot date={date} />
              </SectionCard>

              <SectionCard title="当日市场状态（as-run 引擎输出）">
                <div className="grid gap-3 sm:grid-cols-2">
                  {["cycle", "valuation", "risk", "regime"].map((engine) => (
                    <ReplayEngineCard key={engine} engine={engine} date={date} />
                  ))}
                </div>
              </SectionCard>

              <Disclaimer>
                当时视角严格使用 Point-in-Time 数据：仅显示该日期前已发布/已抓取的信息；宏观数据按官方发布时间过滤，引擎输出取 as-run 历史记录，缺失即明示缺失。
              </Disclaimer>
            </>
          ) : (
            <SectionCard title={`事后视角 · ${formatDate(date)} 之后的真实走势`}>
              <HindsightPanel date={date} />
            </SectionCard>
          )}
        </div>
      </div>

      <p className="text-center text-[10px] leading-relaxed text-muted/70">
        历史回放不构成投资建议；ETF 等数据在该历史日期不存在时将明确标注「当时不存在」。
      </p>
    </div>
  );
}
