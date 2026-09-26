/**
 * 证据列表：支持 / 反对 证据的统一渲染（Level 2 展开）。
 */

import type { EngineEvidence } from "@/types/api";

export interface EvidenceBundle {
  supporting: EngineEvidence[];
  opposing: EngineEvidence[];
}

interface EvidenceListProps {
  evidence?: EvidenceBundle | null;
  /** 空列表提示文案 */
  emptyText?: string;
  className?: string;
}

/** 将扁平证据数组或分组结构归一化为 { supporting, opposing } */
export function normalizeEvidence(
  input?: EvidenceBundle | EngineEvidence[] | null,
): EvidenceBundle {
  if (!input) return { supporting: [], opposing: [] };
  if (Array.isArray(input)) {
    return {
      supporting: input.filter((e) => e.supports),
      opposing: input.filter((e) => !e.supports),
    };
  }
  return input;
}

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    // 大数缩写与小数精度
    if (Math.abs(v) >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
    if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
    return Number.isInteger(v) ? String(v) : v.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function EvidenceItem({ item, tone }: { item: EngineEvidence; tone: "up" | "down" }) {
  return (
    <li className="group/ev flex items-start gap-2 rounded-lg bg-bg-hover/50 px-2.5 py-2">
      <span
        aria-hidden
        className={`mt-0.5 shrink-0 font-mono text-[10px] ${tone === "up" ? "text-up" : "text-down"}`}
      >
        {tone === "up" ? "+" : "−"}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-xs leading-relaxed text-[#e6edf3]">
          <span className="font-medium">{item.factor}</span>
          {item.value !== undefined && item.value !== null && (
            <span className="ml-1.5 font-mono tabular-nums text-accent">
              {formatValue(item.value)}
            </span>
          )}
          {item.historical_percentile !== undefined &&
            item.historical_percentile !== null && (
              <span className="ml-1.5 text-[10px] text-muted">
                分位 {Math.round(item.historical_percentile * 100)}%
              </span>
            )}
        </p>
        {item.interpretation && (
          <p className="mt-0.5 text-[11px] leading-relaxed text-muted">{item.interpretation}</p>
        )}
        {(item.data_source || item.weight !== undefined) && (
          <p className="mt-1 flex items-center gap-2 text-[10px] text-muted">
            {item.data_source && <span>源: {item.data_source}</span>}
            {item.weight !== undefined && item.weight !== null && (
              <span>权重 {Math.round(item.weight * 100)}%</span>
            )}
          </p>
        )}
      </div>
    </li>
  );
}

export function EvidenceList({ evidence, emptyText = "暂无证据", className = "" }: EvidenceListProps) {
  const { supporting, opposing } = normalizeEvidence(evidence);
  const isEmpty = supporting.length === 0 && opposing.length === 0;

  if (isEmpty) {
    return <p className={`text-xs text-muted ${className}`}>{emptyText}</p>;
  }

  return (
    <div className={`flex flex-col gap-3 ${className}`}>
      {supporting.length > 0 && (
        <section>
          <h4 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-up">
            <span aria-hidden>▸</span> 支持证据 ({supporting.length})
          </h4>
          <ul className="flex flex-col gap-1.5">
            {supporting.map((ev, i) => (
              <EvidenceItem key={`${ev.factor}-${i}`} item={ev} tone="up" />
            ))}
          </ul>
        </section>
      )}
      {opposing.length > 0 && (
        <section>
          <h4 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-down">
            <span aria-hidden>▸</span> 反对证据 ({opposing.length})
          </h4>
          <ul className="flex flex-col gap-1.5">
            {opposing.map((ev, i) => (
              <EvidenceItem key={`${ev.factor}-${i}`} item={ev} tone="down" />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
