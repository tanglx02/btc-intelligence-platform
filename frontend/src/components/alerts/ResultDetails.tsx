/**
 * 求值结果详情渲染：condition_result 树 / 证据链 / 上下文快照。
 * 供「规则测试」弹窗与「触发历史」展开详情共用。
 */

import { formatNumber } from "./format";
import { metricLabel } from "./constants";
import { detailValueText } from "./conditionUtils";
import type { ConditionDetail } from "./types";

/* -------------------------------------------------------------------------- */
/* condition_result 树                                                         */
/* -------------------------------------------------------------------------- */

const RESULT_COLOR: Record<string, string> = {
  TRUE: "border-up/50 text-up",
  FALSE: "border-line text-muted",
  UNKNOWN: "border-warn/50 text-warn",
};

/** path 按自然排序（root.0 < root.1 < root.10），并按层级缩进 */
function sortPaths(paths: string[]): string[] {
  return [...paths].sort((a, b) => {
    const pa = a.split(".");
    const pb = b.split(".");
    const len = Math.min(pa.length, pb.length);
    for (let i = 0; i < len; i++) {
      const na = Number(pa[i]);
      const nb = Number(pb[i]);
      if (!Number.isNaN(na) && !Number.isNaN(nb) && na !== nb) return na - nb;
      if (pa[i] !== pb[i]) return pa[i] < pb[i] ? -1 : 1;
    }
    return pa.length - pb.length;
  });
}

const COMPOSITE_LABEL: Record<string, string> = {
  AND: "且（全部满足）",
  OR: "或（任一满足）",
  NOT: "非（取反）",
};

export function ConditionResultTree({
  details,
  className = "",
}: {
  details?: Record<string, ConditionDetail> | null;
  className?: string;
}) {
  const entries = details ?? {};
  const paths = Object.keys(entries);
  if (paths.length === 0) {
    return <p className={`text-xs text-muted ${className}`}>暂无求值详情</p>;
  }

  return (
    <ul className={`flex flex-col gap-1 ${className}`}>
      {sortPaths(paths).map((path) => {
        const d = entries[path];
        const depth = path.split(".").length - 1;
        const color = RESULT_COLOR[d.result ?? ""] ?? RESULT_COLOR.FALSE;
        const valueText = detailValueText(d);
        const isComposite = d.type === "AND" || d.type === "OR" || d.type === "NOT";
        return (
          <li
            key={path}
            className="flex items-start gap-2 rounded-md bg-bg-hover/40 px-2.5 py-1.5"
            style={{ marginLeft: `${depth * 18}px` }}
          >
            <span
              className={`mt-px inline-flex shrink-0 items-center rounded border px-1.5 py-0.5 text-[10px] font-medium leading-none ${color}`}
            >
              {d.result === "TRUE" ? "✓" : d.result === "UNKNOWN" ? "?" : "○"} {d.result ?? "—"}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-xs leading-relaxed text-[#e6edf3]">
                {isComposite && d.type && (
                  <span className="mr-1.5 rounded bg-bg px-1 py-0.5 font-mono text-[10px] text-accent">
                    {d.type} · {COMPOSITE_LABEL[d.type] ?? ""}
                  </span>
                )}
                {d.description ?? (d.reason ? <span className="text-warn">{d.reason}</span> : d.metric ? metricLabel(d.metric) : d.type)}
              </p>
              {valueText && (
                <p className="mt-0.5 font-mono text-[10px] tabular-nums text-muted">{valueText}</p>
              )}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/* 证据链（扁平叶子详情数组）                                                     */
/* -------------------------------------------------------------------------- */

export function EvidenceDetailList({
  evidence,
  className = "",
}: {
  evidence?: ConditionDetail[] | null;
  className?: string;
}) {
  const items = evidence ?? [];
  if (items.length === 0) {
    return <p className={`text-xs text-muted ${className}`}>暂无证据数据</p>;
  }

  return (
    <ul className={`flex flex-col gap-1.5 ${className}`}>
      {items.map((d, i) => {
        const valueText = detailValueText(d);
        return (
          <li key={i} className="flex items-start gap-2 rounded-lg bg-bg-hover/50 px-2.5 py-2">
            <span
              aria-hidden
              className={`mt-0.5 font-mono text-[10px] ${
                d.result === "TRUE" ? "text-up" : d.result === "UNKNOWN" ? "text-warn" : "text-muted"
              }`}
            >
              {d.result === "TRUE" ? "✓" : d.result === "UNKNOWN" ? "?" : "·"}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-xs leading-relaxed text-[#e6edf3]">
                {d.description ?? metricLabel(d.metric)}
                {valueText && (
                  <span className="ml-1.5 font-mono text-[10px] tabular-nums text-accent">{valueText}</span>
                )}
              </p>
              {d.reason && <p className="mt-0.5 text-[11px] leading-relaxed text-warn">{d.reason}</p>}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/* -------------------------------------------------------------------------- */
/* 市场上下文快照                                                               */
/* -------------------------------------------------------------------------- */

export function ContextSnapshot({
  snapshot,
  className = "",
}: {
  snapshot?: {
    price?: number | null;
    indicators?: Record<string, number> | null;
    stale_fields?: string[] | null;
    engine_states?: Record<string, unknown> | null;
    data_quality?: Record<string, unknown> | null;
  } | null;
  className?: string;
}) {
  if (!snapshot) return null;
  const indicators = Object.entries(snapshot.indicators ?? {}).slice(0, 8);
  const engines = Object.entries(snapshot.engine_states ?? {});
  const stale = snapshot.stale_fields ?? [];

  return (
    <div className={`flex flex-col gap-2 rounded-lg border border-line bg-bg px-3 py-2.5 ${className}`}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
        <span className="text-muted">
          当时价格
          <span className="ml-1.5 font-mono tabular-nums text-accent">
            {formatNumber(snapshot.price)} USDT
          </span>
        </span>
        {engines.length > 0 && (
          <span className="text-muted">
            引擎状态：
            {engines.map(([k, v]) => (
              <span key={k} className="ml-1 font-mono text-[10px] text-[#e6edf3]">
                {k}={String(v)}
              </span>
            ))}
          </span>
        )}
      </div>
      {indicators.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {indicators.map(([k, v]) => (
            <span
              key={k}
              className="rounded border border-line bg-bg-raised px-1.5 py-0.5 font-mono text-[10px] tabular-nums text-muted"
            >
              {k} {formatNumber(v)}
            </span>
          ))}
        </div>
      )}
      {stale.length > 0 && (
        <p className="text-[10px] leading-relaxed text-warn">
          数据缺失字段：{stale.join("、")}（相关条件按 UNKNOWN 处理，不触发）
        </p>
      )}
    </div>
  );
}
