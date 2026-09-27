"use client";

/**
 * 条件树递归编辑器（高级模式）。
 *
 * - 组合节点：AND / OR / NOT（NOT 仅允许一个子节点）
 * - 叶子节点：与普通模式同款字段（监测数据 / 条件 / 阈值）
 * - 层级缩进 + 左侧色条可视化嵌套深度，最大深度与后端一致（5）
 */

import { CONDITION_MAP, CONDITION_OPTIONS, ENGINE_STATE_SUGGESTIONS, METRIC_MAP, METRIC_OPTIONS } from "./constants";
import { MAX_CONDITION_DEPTH } from "./conditionUtils";
import { InfoHint, inputCls, selectCls } from "./ui";
import type { ConditionNode } from "./types";

/* -------------------------------------------------------------------------- */
/* 默认节点                                                                    */
/* -------------------------------------------------------------------------- */

function defaultLeaf(): ConditionNode {
  return { type: "threshold", metric: "price", operator: "gt", value: undefined };
}

const DEPTH_BAR = [
  "border-l-accent/60",
  "border-l-up/60",
  "border-l-warn/60",
  "border-l-orange/60",
  "border-l-down/60",
];

const DEPTH_TEXT = [
  "text-accent",
  "text-up",
  "text-warn",
  "text-orange",
  "text-down",
];

function depthCls(depth: number): string {
  return DEPTH_BAR[(depth - 1) % DEPTH_BAR.length];
}

function depthTextCls(depth: number): string {
  return DEPTH_TEXT[(depth - 1) % DEPTH_TEXT.length];
}

/* -------------------------------------------------------------------------- */
/* 叶子字段（普通模式与高级模式共用）                                              */
/* -------------------------------------------------------------------------- */

interface LeafFieldsProps {
  node: ConditionNode;
  onChange: (next: ConditionNode) => void;
}

/** 单个叶子条件的编辑字段（监测数据 / 条件 / 阈值 / 区间 / 窗口 / 状态） */
export function LeafFields({ node, onChange }: LeafFieldsProps) {
  const metricKey = node.type === "state_change" ? `engine:${node.engine ?? "cycle"}` : (node.metric ?? "price");
  const metricMeta = METRIC_MAP[metricKey];
  const conditionId = leafConditionId(node);
  const kind = conditionId ? CONDITION_MAP[conditionId]?.kind : "threshold";

  const onMetricChange = (next: string) => {
    const meta = METRIC_MAP[next];
    if (meta?.engine) {
      onChange({ ...node, type: "state_change", engine: meta.engine, metric: undefined, operator: undefined, value: undefined, low: undefined, high: undefined, percentile: undefined });
      return;
    }
    // 从状态条件切回数值条件：重置为 threshold
    if (node.type === "state_change") {
      onChange({ ...node, type: "threshold", engine: undefined, metric: next, operator: "gt", to_state: undefined });
      return;
    }
    onChange({ ...node, metric: next, window_hours: node.window_hours ?? 24 });
  };

  const onConditionChange = (nextId: string) => {
    const opt = CONDITION_MAP[nextId];
    if (!opt) return;
    switch (opt.kind) {
      case "threshold":
        onChange({ ...node, type: "threshold", operator: nextId, percentile: undefined });
        break;
      case "change":
        onChange({
          ...node,
          type: "change_pct",
          metric: "price",
          operator: nextId === "change_up" ? "gt" : "lt",
          window_hours: node.window_hours ?? 24,
          percentile: undefined,
        });
        break;
      case "range":
        onChange({ ...node, type: nextId, operator: undefined, value: undefined, percentile: undefined });
        break;
      case "cross":
        onChange({ ...node, type: nextId, operator: undefined, percentile: undefined });
        break;
      case "percentile":
        onChange({ ...node, type: "percentile", operator: nextId === "percentile_above" ? "gte" : "lte", value: undefined });
        break;
      default:
        break;
    }
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* 监测数据 */}
      <div className="flex items-center gap-1.5">
        <select
          aria-label="监测数据"
          className={`${selectCls} w-44`}
          value={metricKey}
          onChange={(e) => onMetricChange(e.target.value)}
        >
          {METRIC_OPTIONS.map((m) => (
            <option key={m.value} value={m.value}>
              {m.label}
            </option>
          ))}
        </select>
        {metricMeta && <InfoHint text={metricMeta.hint} />}
      </div>

      {/* 条件（引擎类指标为状态变化，隐藏条件选择） */}
      {!metricMeta?.engine && (
        <select
          aria-label="条件"
          className={`${selectCls} w-40`}
          value={conditionId ?? "gt"}
          onChange={(e) => onConditionChange(e.target.value)}
        >
          {CONDITION_OPTIONS.filter((c) => c.kind !== "state").map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
      )}

      {/* 状态变化：目标状态（可选） */}
      {node.type === "state_change" && (
        <>
          <input
            aria-label="目标状态（可留空表示任意变化）"
            className={`${inputCls} w-48`}
            placeholder="目标状态（留空 = 任意变化）"
            value={node.to_state ?? ""}
            onChange={(e) => onChange({ ...node, to_state: e.target.value })}
          />
          {metricMeta?.engine && ENGINE_STATE_SUGGESTIONS[metricMeta.engine] && (
            <select
              aria-label="常用状态"
              className={`${selectCls} w-44`}
              value=""
              onChange={(e) => {
                if (e.target.value) onChange({ ...node, to_state: e.target.value });
              }}
            >
              <option value="">常用状态…</option>
              {ENGINE_STATE_SUGGESTIONS[metricMeta.engine].map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          )}
        </>
      )}

      {/* 数值输入（按条件类型） */}
      {kind === "change" && node.type === "change_pct" && (
        <select
          aria-label="变化窗口"
          className={`${selectCls} w-28`}
          value={node.window_hours ?? 24}
          onChange={(e) => onChange({ ...node, window_hours: Number(e.target.value) })}
        >
          <option value={1}>1 小时</option>
          <option value={24}>24 小时</option>
          <option value={168}>7 天</option>
        </select>
      )}

      {(kind === "threshold" || kind === "cross" || kind === "change") && node.type !== "state_change" && (
        <div className="flex items-center gap-1">
          <input
            aria-label="阈值"
            type="number"
            step="any"
            className={`${inputCls} w-32 font-mono`}
            placeholder={conditionId ? (CONDITION_MAP[conditionId]?.placeholder ?? "阈值") : "阈值"}
            value={node.value ?? ""}
            onChange={(e) => onChange({ ...node, value: e.target.value === "" ? null : Number(e.target.value) })}
          />
          {metricMeta?.unit && <span className="text-[10px] text-muted">{metricMeta.unit}</span>}
        </div>
      )}

      {kind === "range" && (
        <div className="flex items-center gap-1">
          <input
            aria-label="区间下限"
            type="number"
            step="any"
            className={`${inputCls} w-28 font-mono`}
            placeholder="下限"
            value={node.low ?? ""}
            onChange={(e) => onChange({ ...node, low: e.target.value === "" ? null : Number(e.target.value) })}
          />
          <span className="text-muted">~</span>
          <input
            aria-label="区间上限"
            type="number"
            step="any"
            className={`${inputCls} w-28 font-mono`}
            placeholder="上限"
            value={node.high ?? ""}
            onChange={(e) => onChange({ ...node, high: e.target.value === "" ? null : Number(e.target.value) })}
          />
          {metricMeta?.unit && <span className="text-[10px] text-muted">{metricMeta.unit}</span>}
        </div>
      )}

      {kind === "percentile" && (
        <div className="flex items-center gap-1">
          <input
            aria-label="历史分位"
            type="number"
            step="any"
            min={0}
            max={100}
            className={`${inputCls} w-24 font-mono`}
            placeholder="分位 %"
            value={node.percentile ?? ""}
            onChange={(e) => onChange({ ...node, percentile: e.target.value === "" ? null : Number(e.target.value) })}
          />
          <span className="text-[10px] text-muted">%</span>
        </div>
      )}
    </div>
  );
}

/** ConditionNode -> 普通模式条件 ID（用于回显） */
function leafConditionId(node: ConditionNode): string | null {
  switch (node.type) {
    case "threshold":
      return node.operator === "lt" ? "lt" : "gt";
    case "change_pct":
      return node.operator === "lt" ? "change_down" : "change_up";
    case "range_enter":
      return "range_enter";
    case "range_exit":
      return "range_exit";
    case "cross_above":
      return "cross_above";
    case "cross_below":
      return "cross_below";
    case "percentile":
      return node.operator === "lte" ? "percentile_below" : "percentile_above";
    case "state_change":
      return "state_change";
    default:
      return null;
  }
}

/* -------------------------------------------------------------------------- */
/* 递归编辑器                                                                   */
/* -------------------------------------------------------------------------- */

interface ConditionNodeEditorProps {
  node: ConditionNode;
  onChange: (next: ConditionNode) => void;
  /** 传入则显示删除按钮（根节点不传） */
  onRemove?: () => void;
  depth: number;
  /** 唯一 key 前缀 */
  path: string;
}

export function ConditionNodeEditor({ node, onChange, onRemove, depth, path }: ConditionNodeEditorProps) {
  const composite = node.type === "AND" || node.type === "OR" || node.type === "NOT";
  const children = node.children ?? [];
  const canAddChild =
    composite && depth < MAX_CONDITION_DEPTH && !(node.type === "NOT" && children.length >= 1);

  const setChildren = (next: ConditionNode[]) => onChange({ ...node, children: next });

  return (
    <div
      className={`rounded-lg border border-line bg-bg-hover/30 border-l-2 py-2.5 pl-3 pr-2.5 ${depthCls(depth)}`}
    >
      {/* 节点头：类型选择 + 删除 */}
      <div className="flex flex-wrap items-center gap-2">
        {composite ? (
          <>
            <span className={`font-mono text-[10px] font-bold uppercase ${depthTextCls(depth)}`}>
              组合
            </span>
            <select
              aria-label="组合类型"
              className={`${selectCls} w-28`}
              value={node.type}
              onChange={(e) => {
                const nextType = e.target.value;
                const nextChildren = nextType === "NOT" ? children.slice(0, 1) : children;
                onChange({ ...node, type: nextType, children: nextChildren });
              }}
            >
              <option value="AND">且 AND</option>
              <option value="OR">或 OR</option>
              <option value="NOT">非 NOT</option>
            </select>
          </>
        ) : (
          <span className={`font-mono text-[10px] font-bold uppercase ${depthTextCls(depth)}`}>
            条件
          </span>
        )}

        {onRemove && (
          <button
            type="button"
            onClick={onRemove}
            aria-label="删除该条件"
            className="ml-auto rounded-md border border-line px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-down/50 hover:text-down"
          >
            删除
          </button>
        )}
      </div>

      {/* 字体区：叶子字段 / 子节点 */}
      {composite ? (
        <div className="mt-2.5 flex flex-col gap-2">
          {children.length === 0 && (
            <p className="text-[11px] text-warn">组合条件为空，请添加至少一个子条件</p>
          )}
          {children.map((child, i) => (
            <ConditionNodeEditor
              key={`${path}.${i}`}
              node={child}
              path={`${path}.${i}`}
              depth={depth + 1}
              onChange={(next) => {
                const nextChildren = [...children];
                nextChildren[i] = next;
                setChildren(nextChildren);
              }}
              onRemove={() => setChildren(children.filter((_, j) => j !== i))}
            />
          ))}

          {canAddChild && (
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded-md border border-dashed border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
                onClick={() => setChildren([...children, defaultLeaf()])}
              >
                ＋ 子条件
              </button>
              <button
                type="button"
                className="rounded-md border border-dashed border-line px-2 py-1 text-[11px] text-muted transition-colors hover:border-accent/60 hover:text-accent"
                onClick={() =>
                  setChildren([...children, { type: "AND", children: [defaultLeaf()] }])
                }
              >
                ＋ 组合
              </button>
              {depth + 1 >= MAX_CONDITION_DEPTH && (
                <span className="text-[10px] text-warn">已达最大嵌套深度 {MAX_CONDITION_DEPTH}</span>
              )}
            </div>
          )}
        </div>
      ) : (
        <div className="mt-2">
          <LeafFields node={node} onChange={onChange} />
        </div>
      )}
    </div>
  );
}
