/**
 * 智能预警领域类型（与 backend/app/alerts、app/services/alert_service.py 对齐）。
 *
 * 注意：types/api.ts 中旧版 AlertRule/AlertEvent 与后端实际响应不一致
 * （后端为 condition_tree / condition_text / cooldown_seconds / trigger_count
 * 及 { items, total, page, size } 分页包装），本文件以后端真实结构为准，
 * 仅供 alerts 模块内部使用。
 */

import type { ApiResponse } from "@/types/api";

/* -------------------------------------------------------------------------- */
/* 基础枚举                                                                    */
/* -------------------------------------------------------------------------- */

/** 后端 AlertSeverity 完整枚举（含 LOW / MEDIUM） */
export type SeverityLevel = "INFO" | "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";

/** 前端四级严重程度（SeverityBadge 契约） */
export type Severity4 = "INFO" | "WARNING" | "HIGH" | "CRITICAL";

export type AlertEventStatus = "TRIGGERED" | "ACKED" | "RESOLVED" | "SUPPRESSED";
export type AlertRuleStateValue = "ACTIVE" | "PAUSED" | "ARCHIVED" | "ERROR";

/** 后端叶子条件类型 */
export type LeafConditionType =
  | "threshold"
  | "change_pct"
  | "range_enter"
  | "range_exit"
  | "cross_above"
  | "cross_below"
  | "percentile"
  | "state_change";

/* -------------------------------------------------------------------------- */
/* 条件树（ConditionNode JSON 持久化格式）                                       */
/* -------------------------------------------------------------------------- */

export interface ConditionNode {
  type: string;
  metric?: string | null;
  operator?: string | null;
  value?: number | null;
  low?: number | null;
  high?: number | null;
  window_hours?: number | null;
  metric_b?: string | null;
  engine?: string | null;
  from_state?: string | null;
  to_state?: string | null;
  percentile?: number | null;
  children?: ConditionNode[] | null;
}

/** 普通模式（单叶子条件）的受控表单状态 */
export interface SimpleCondition {
  /** 监测数据（引擎类为 "engine:cycle" 等前缀形式） */
  metric: string;
  condition: ConditionId;
  /** 以下数值均为受控字符串（空串 = 未填写） */
  value: string;
  low: string;
  high: string;
  /** change_pct 窗口（小时） */
  windowHours: number;
  toState: string;
  percentile: string;
}

/** 普通模式条件 ID（CONDITION_OPTIONS 的 value） */
export type ConditionId =
  | "gt"
  | "lt"
  | "change_up"
  | "change_down"
  | "range_enter"
  | "range_exit"
  | "cross_above"
  | "cross_below"
  | "percentile_above"
  | "percentile_below"
  | "state_change";

/* -------------------------------------------------------------------------- */
/* 规则表单                                                                    */
/* -------------------------------------------------------------------------- */

export interface RuleFormValue {
  rule_name: string;
  description: string;
  severity: Severity4;
  simple: SimpleCondition;
  /** 高级模式条件树（null = 尚未从普通模式同步） */
  tree: ConditionNode | null;
  tab: "simple" | "advanced";
  /** 持续时间秒数（null = 不限） */
  durationSeconds: number | null;
  consecutiveCount: number;
  /** 冷却档位 key（"custom" = 使用 cooldownCustom） */
  cooldownPreset: string;
  cooldownCustom: string;
  emailEnabled: boolean;
}

/** 创建 / 更新规则请求体（AlertRuleCreate / RuleUpdateRequest 子集） */
export interface RuleFormPayload {
  rule_name: string;
  description: string;
  severity: string;
  category: string;
  target_symbol: string;
  condition_tree: ConditionNode;
  duration_seconds: number | null;
  consecutive_count: number;
  cooldown_seconds: number;
  channels: string[];
}

/* -------------------------------------------------------------------------- */
/* API 响应记录                                                                */
/* -------------------------------------------------------------------------- */

/** { items, total, page, size } 分页包装 */
export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

/** 规则记录（rule_to_dict） */
export interface AlertRuleRecord {
  id: string;
  rule_name: string;
  description?: string | null;
  severity?: string | null;
  category?: string | null;
  target_symbol?: string | null;
  condition_tree?: ConditionNode | null;
  condition_text?: string | null;
  duration_seconds?: number | null;
  consecutive_count?: number | null;
  cooldown_seconds?: number | null;
  channels?: string[] | null;
  state?: string | null;
  is_enabled?: boolean | null;
  priority?: number | null;
  last_evaluated_at?: string | null;
  last_triggered_at?: string | null;
  trigger_count?: number | null;
  consecutive_triggers?: number | null;
  tags?: string[] | null;
  created_at?: string | null;
  updated_at?: string | null;
}

/** 事件记录（event_to_dict） */
export interface AlertEventRecord {
  id: string;
  rule_id: string;
  rule_name?: string | null;
  triggered_at?: string | null;
  resolved_at?: string | null;
  severity?: string | null;
  status?: string | null;
  trigger_value?: number | null;
  trigger_threshold?: number | null;
  condition_result?: Record<string, ConditionDetail> | null;
  evidence?: ConditionDetail[] | null;
  market_context?: Record<string, unknown> | null;
  data_quality?: string | null;
  data_source?: string | null;
  title?: string | null;
  description_cn?: string | null;
  is_suppressed?: boolean | null;
  suppress_reason?: string | null;
  acked_at?: string | null;
  created_at?: string | null;
}

/** 统计（get_stats） */
export interface AlertStats {
  total_rules: number;
  active_rules: number;
  paused_rules: number;
  today_triggered: number;
  unacked_events: number;
  severity_distribution?: Record<string, number> | null;
}

/** 单节点求值详情（AlertEvaluator details/evidence 条目） */
export interface ConditionDetail {
  type?: string;
  metric?: string | null;
  result?: string;
  description?: string;
  value?: unknown;
  threshold?: unknown;
  low?: unknown;
  high?: unknown;
  reason?: string;
  children?: string[];
  window_hours?: unknown;
  engine?: string;
  from?: unknown;
  to?: unknown;
  prev?: unknown;
  current?: unknown;
  [key: string]: unknown;
}

/** 规则测试结果（test_rule） */
export interface RuleTestResult {
  found: boolean;
  rule_id?: string;
  rule_name?: string;
  triggered?: boolean;
  /** TRUE / FALSE / UNKNOWN */
  result?: string;
  evidence?: ConditionDetail[] | null;
  condition_result?: Record<string, ConditionDetail> | null;
  context_snapshot?: {
    price?: number | null;
    indicators?: Record<string, number> | null;
    stale_fields?: string[] | null;
    engine_states?: Record<string, unknown> | null;
    data_quality?: Record<string, unknown> | null;
  } | null;
}

/** 历史触发模拟（backtest_rule） */
export interface RuleBacktestResult {
  found: boolean;
  rule_id?: string;
  rule_name?: string;
  start?: string;
  end?: string;
  trigger_dates?: string[];
  trigger_count?: number;
  avg_interval_days?: number | null;
  /** "7" / "30" / "90" -> [{ date, change_pct }] */
  performance?: Record<string, { date: string; change_pct: number }[]> | null;
  note?: string | null;
}

/** SMTP 配置（password 仅返回掩码 ***） */
export interface SmtpConfigRecord {
  enabled?: boolean | null;
  host?: string | null;
  port?: number | null;
  user?: string | null;
  password?: string | null;
  from_email?: string | null;
  from_name?: string | null;
  use_tls?: boolean | null;
  use_ssl?: boolean | null;
  timeout_seconds?: number | null;
  recipient?: string | null;
}

/** SMTP / 渠道测试结果 */
export interface ChannelTestResult {
  channel_type?: string;
  recipient?: string;
  success: boolean;
  message_id?: string | null;
  error?: string | null;
  response?: unknown;
}

/** 摘要配置 */
export interface DigestConfigRecord {
  enabled?: boolean | null;
  daily_hour?: number | null;
  weekly_day?: number | null;
  weekly_hour?: number | null;
  recipients?: string[] | null;
  last_daily_sent?: string | null;
  last_weekly_sent?: string | null;
}

/** 渠道配置记录 */
export interface ChannelRecord {
  id: string;
  channel_type: string;
  channel_name: string;
  config?: Record<string, unknown> | null;
  is_primary?: boolean | null;
  is_verified?: boolean | null;
  is_enabled?: boolean | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface ChannelListPayload {
  available?: string[];
  items?: ChannelRecord[];
}

/** 预设模板（rule_templates.py） */
export interface AlertTemplateRecord {
  id: string;
  name: string;
  description?: string;
  icon?: string;
  category?: string;
  defaults?: {
    category?: string;
    severity?: string;
    condition_tree?: ConditionNode;
  };
  editable_fields?: string[];
}

/* -------------------------------------------------------------------------- */
/* 类型窄化辅助                                                                */
/* -------------------------------------------------------------------------- */

/**
 * 将 apiClient.alerts 的宽泛 Record 载荷窄化为精确类型。
 * （lib/api.ts 不依赖本模块类型，避免反向耦合。）
 */
export function asAlert<T>(p: Promise<ApiResponse<Record<string, unknown>>>): Promise<ApiResponse<T>> {
  return p as unknown as Promise<ApiResponse<T>>;
}

/** 数组载荷版本 */
export function asAlertList<T>(
  p: Promise<ApiResponse<Record<string, unknown>[]>>,
): Promise<ApiResponse<T[]>> {
  return p as unknown as Promise<ApiResponse<T[]>>;
}
