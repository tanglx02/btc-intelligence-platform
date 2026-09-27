/**
 * 条件树工具：普通模式 <-> ConditionNode 双向转换、
 * 校验、人类可读描述（与 backend/app/alerts/schemas.ConditionNode.to_human_text 对齐）。
 */

import { METRIC_MAP, metricLabel } from "./constants";
import { formatDuration, formatNumber } from "./format";
import type {
  ConditionDetail,
  ConditionNode,
  RuleFormPayload,
  RuleFormValue,
  Severity4,
  SimpleCondition,
} from "./types";

/** 条件树最大嵌套深度（与后端 MAX_CONDITION_DEPTH 一致） */
export const MAX_CONDITION_DEPTH = 5;

const OP_SYM: Record<string, string> = {
  gt: ">",
  lt: "<",
  gte: "≥",
  lte: "≤",
  eq: "=",
};

const COMPOSITE_TYPES = new Set(["AND", "OR", "NOT"]);

export function isComposite(node: ConditionNode): boolean {
  return COMPOSITE_TYPES.has(node.type);
}

function windowLabel(hours: number | null | undefined): string {
  const h = hours ?? 24;
  if (h >= 24 && h % 24 === 0) return `${h / 24}d`;
  return `${h}h`;
}

function fmtValue(metric: string, v: number | null | undefined): string {
  if (v === null || v === undefined) return "?";
  const unit = METRIC_MAP[metric]?.unit;
  return `${formatNumber(v, METRIC_MAP[metric]?.decimals)}${unit ?? ""}`;
}

/* -------------------------------------------------------------------------- */
/* 人类可读描述                                                                */
/* -------------------------------------------------------------------------- */

/** 条件树 -> 人类可读中文描述（metric 用中文标签） */
export function nodeToHumanText(node: ConditionNode | null | undefined): string {
  if (!node) return "（未配置条件）";

  if (node.type === "AND") {
    return (node.children ?? []).map((c) => `(${nodeToHumanText(c)})`).join(" 且 ");
  }
  if (node.type === "OR") {
    return (node.children ?? []).map((c) => `(${nodeToHumanText(c)})`).join(" 或 ");
  }
  if (node.type === "NOT") {
    return `非(${nodeToHumanText((node.children ?? [])[0])})`;
  }

  const metric = node.metric ?? "";
  const label = metricLabel(metric);
  const op = OP_SYM[node.operator ?? ""] ?? node.operator ?? "";

  switch (node.type) {
    case "threshold":
      return `${label} ${op} ${fmtValue(metric, node.value)}`;
    case "change_pct":
      return `${label} ${windowLabel(node.window_hours)}涨跌幅 ${op} ${formatNumber(node.value)}%`;
    case "range_enter":
      return `${label} 进入区间 [${fmtValue(metric, node.low)}, ${fmtValue(metric, node.high)}]`;
    case "range_exit":
      return `${label} 离开区间 [${fmtValue(metric, node.low)}, ${fmtValue(metric, node.high)}]`;
    case "cross_above":
      return `${label} 上穿 ${fmtValue(metric, node.value)}`;
    case "cross_below":
      return `${label} 下穿 ${fmtValue(metric, node.value)}`;
    case "percentile":
      return `${label} 历史分位 ${op || "≥"} ${formatNumber(node.percentile)}%`;
    case "state_change": {
      const engineLabel = METRIC_MAP[`engine:${node.engine}`]?.label ?? node.engine ?? "引擎";
      const from = node.from_state ? `由 ${node.from_state} ` : "";
      return `${engineLabel} 状态${from}变为 ${node.to_state || "其他状态"}`;
    }
    default:
      return `${node.type}(${label})`;
  }
}

/** 规则完整描述（含持续时间 / 连续次数 / 通知渠道） */
export function describeRuleText(value: RuleFormValue, tree: ConditionNode): string {
  const parts: string[] = [`当 ${nodeToHumanText(tree)}`];
  if (value.durationSeconds) parts.push(`持续 ${formatDuration(value.durationSeconds)}`);
  if (value.consecutiveCount > 1) parts.push(`且连续 ${value.consecutiveCount} 次求值满足`);
  let text = parts.join(" ");
  if (value.emailEnabled) {
    text += "，通过 Email 通知我";
  } else {
    text += "（未启用通知渠道，仅记录事件）";
  }
  return text;
}

/* -------------------------------------------------------------------------- */
/* 普通模式 <-> ConditionNode                                                   */
/* -------------------------------------------------------------------------- */

export function defaultSimple(): SimpleCondition {
  return {
    metric: "price",
    condition: "lt",
    value: "",
    low: "",
    high: "",
    windowHours: 24,
    toState: "",
    percentile: "",
  };
}

function num(input: string): number | null {
  const trimmed = input.trim();
  if (trimmed === "") return null;
  const v = Number(trimmed);
  return Number.isFinite(v) ? v : null;
}

/** 普通模式表单 -> 条件树（叶子节点） */
export function buildFromSimple(s: SimpleCondition): ConditionNode {
  const metric = s.metric;
  const metricOption = METRIC_MAP[metric];

  if (s.condition === "state_change") {
    const node: ConditionNode = {
      type: "state_change",
      engine: metricOption?.engine ?? "cycle",
    };
    if (s.toState.trim()) node.to_state = s.toState.trim();
    return node;
  }

  switch (s.condition) {
    case "gt":
    case "lt":
      return { type: "threshold", metric, operator: s.condition, value: num(s.value) };
    case "change_up":
    case "change_down":
      return {
        type: "change_pct",
        metric: "price",
        operator: s.condition === "change_up" ? "gt" : "lt",
        value: num(s.value),
        window_hours: s.windowHours,
      };
    case "range_enter":
    case "range_exit":
      return { type: s.condition, metric, low: num(s.low), high: num(s.high) };
    case "cross_above":
    case "cross_below":
      return { type: s.condition, metric, value: num(s.value) };
    case "percentile_above":
    case "percentile_below":
      return {
        type: "percentile",
        metric,
        operator: s.condition === "percentile_above" ? "gte" : "lte",
        percentile: num(s.percentile),
      };
    default:
      return { type: "threshold", metric, operator: "gt", value: num(s.value) };
  }
}

/** 条件树 -> 普通模式表单（非单叶子条件返回 null） */
export function extractSimple(node: ConditionNode | null | undefined): SimpleCondition | null {
  if (!node || isComposite(node)) return null;
  const base = defaultSimple();
  const metric = node.metric ?? "";

  switch (node.type) {
    case "threshold":
      if (node.operator !== "gt" && node.operator !== "lt") return null;
      return {
        ...base,
        metric,
        condition: node.operator,
        value: node.value !== null && node.value !== undefined ? String(node.value) : "",
      };
    case "change_pct": {
      if (node.operator !== "gt" && node.operator !== "lt") return null;
      const isPrice = metric === "price" || metric === "";
      if (!isPrice) return null;
      return {
        ...base,
        metric: "change",
        condition: node.operator === "gt" ? "change_up" : "change_down",
        value: node.value !== null && node.value !== undefined ? String(node.value) : "",
        windowHours: node.window_hours ?? 24,
      };
    }
    case "range_enter":
    case "range_exit":
      return {
        ...base,
        metric,
        condition: node.type,
        low: node.low !== null && node.low !== undefined ? String(node.low) : "",
        high: node.high !== null && node.high !== undefined ? String(node.high) : "",
      };
    case "cross_above":
    case "cross_below":
      return {
        ...base,
        metric,
        condition: node.type,
        value: node.value !== null && node.value !== undefined ? String(node.value) : "",
      };
    case "percentile": {
      if (node.operator !== "gte" && node.operator !== "lte") return null;
      return {
        ...base,
        metric,
        condition: node.operator === "gte" ? "percentile_above" : "percentile_below",
        percentile: node.percentile !== null && node.percentile !== undefined ? String(node.percentile) : "",
      };
    }
    case "state_change":
      return {
        ...base,
        metric: `engine:${node.engine ?? "cycle"}`,
        condition: "state_change",
        toState: node.to_state ?? "",
      };
    default:
      return null;
  }
}

/** 从树中取第一个叶子（模板预填充的 best-effort） */
export function firstLeaf(node: ConditionNode): ConditionNode {
  if (isComposite(node)) {
    for (const child of node.children ?? []) {
      const leaf = firstLeaf(child);
      if (leaf.type && !isComposite(leaf)) return leaf;
    }
  }
  return node;
}

/* -------------------------------------------------------------------------- */
/* 校验                                                                        */
/* -------------------------------------------------------------------------- */

/** 校验条件树，返回第一条错误信息；合法返回 null */
export function validateTree(node: ConditionNode, depth = 1): string | null {
  if (depth > MAX_CONDITION_DEPTH) {
    return `条件树嵌套深度超过上限 ${MAX_CONDITION_DEPTH}`;
  }
  if (isComposite(node)) {
    const children = node.children ?? [];
    if (children.length === 0) {
      return `${node.type} 组合条件必须包含至少一个子条件`;
    }
    if (node.type === "NOT" && children.length !== 1) {
      return "NOT 组合必须有且仅有一个子条件";
    }
    for (const child of children) {
      const err = validateTree(child, depth + 1);
      if (err) return err;
    }
    return null;
  }

  // 叶子条件字段校验
  if (node.type === "state_change") {
    if (!node.engine) return "状态变化条件必须选择引擎";
    return null;
  }
  if (!node.metric) return "叶子条件必须选择监测数据";

  switch (node.type) {
    case "threshold":
    case "cross_above":
    case "cross_below":
      if (node.value === null || node.value === undefined) return "请填写阈值";
      break;
    case "change_pct":
      if (node.value === null || node.value === undefined) return "请填写涨跌幅阈值";
      break;
    case "range_enter":
    case "range_exit":
      if (node.low === null || node.low === undefined || node.high === null || node.high === undefined) {
        return "请填写区间下限与上限";
      }
      if ((node.low as number) >= (node.high as number)) return "区间下限必须小于上限";
      break;
    case "percentile":
      if (node.percentile === null || node.percentile === undefined) return "请填写历史分位阈值";
      break;
    default:
      return `未知条件类型: ${node.type}`;
  }
  return null;
}

/** 统计叶子条件数量 */
export function countLeaves(node: ConditionNode): number {
  if (isComposite(node)) {
    return (node.children ?? []).reduce((acc, c) => acc + countLeaves(c), 0);
  }
  return 1;
}

/* -------------------------------------------------------------------------- */
/* 表单 -> 请求体                                                               */
/* -------------------------------------------------------------------------- */

function resolveCooldownSeconds(value: RuleFormValue): number {
  if (value.cooldownPreset === "custom") {
    const v = Number(value.cooldownCustom);
    return Number.isFinite(v) && v > 0 ? Math.floor(v) : 86400;
  }
  const preset = value.cooldownPreset;
  const seconds = (
    {
      "5m": 300,
      "15m": 900,
      "1h": 3600,
      "4h": 14400,
      "12h": 43200,
      "24h": 86400,
      "3d": 259200,
      "7d": 604800,
    } as Record<string, number>
  )[preset];
  return seconds ?? 86400;
}

/** 由表单值构建创建 / 更新请求体（condition 为当前激活 tab 的树） */
export function buildRulePayload(value: RuleFormValue, tree: ConditionNode): RuleFormPayload {
  return {
    rule_name: value.rule_name.trim(),
    description: value.description.trim(),
    severity: value.severity,
    category: "MARKET",
    target_symbol: "BTC",
    condition_tree: tree,
    duration_seconds: value.durationSeconds,
    consecutive_count: value.consecutiveCount,
    cooldown_seconds: resolveCooldownSeconds(value),
    channels: value.emailEnabled ? ["EMAIL"] : [],
  };
}

/* -------------------------------------------------------------------------- */
/* 枚举归一化                                                                   */
/* -------------------------------------------------------------------------- */

/** 后端五级 severity -> 前端四级（LOW->INFO，MEDIUM->WARNING） */
export function normalizeSeverity(severity?: string | null): Severity4 {
  switch (severity) {
    case "HIGH":
      return "HIGH";
    case "CRITICAL":
      return "CRITICAL";
    case "WARNING":
    case "MEDIUM":
      return "WARNING";
    case "INFO":
    case "LOW":
    default:
      return "INFO";
  }
}

/** 求值结果三态 -> 展示 */
export function evalResultTone(result?: string | null): { label: string; className: string } {
  switch (result) {
    case "TRUE":
      return { label: "满足", className: "text-up" };
    case "FALSE":
      return { label: "未满足", className: "text-muted" };
    case "UNKNOWN":
      return { label: "数据不足", className: "text-warn" };
    default:
      return { label: result ?? "—", className: "text-muted" };
  }
}

/** 从 condition_result / evidence 条目提取「值 vs 阈值」展示 */
export function detailValueText(d: ConditionDetail): string | null {
  const parts: string[] = [];
  if (d.value !== undefined && d.value !== null) parts.push(`值 ${formatNumber(Number(d.value))}`);
  if (d.threshold !== undefined && d.threshold !== null) {
    parts.push(`阈值 ${formatNumber(Number(d.threshold))}`);
  } else if (d.low !== undefined && d.low !== null && d.high !== undefined && d.high !== null) {
    parts.push(`区间 [${formatNumber(Number(d.low))}, ${formatNumber(Number(d.high))}]`);
  }
  return parts.length ? parts.join(" · ") : null;
}
