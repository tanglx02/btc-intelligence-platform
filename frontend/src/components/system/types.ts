/**
 * 系统管理页面组（/providers /quality /jobs /admin）本地类型。
 *
 * 与后端实际响应逐字段对齐（backend/app/api/v1/providers.py、system.py、alerts.py），
 * 独立于 types/api.ts 的前瞻性契约；接口字段一律按「可缺失」处理，页面侧必须运行时防御。
 */

/* -------------------------------------------------------------------------- */
/* Providers（/providers）                                                     */
/* -------------------------------------------------------------------------- */

/** Provider 最新健康快照（providers 表 LEFT JOIN provider_health 最新一条） */
export interface ProviderLatestHealth {
  check_time?: string | null;
  status?: string | null;
  response_time_ms?: number | null;
  success_rate_1h?: number | null;
  success_rate_24h?: number | null;
  consecutive_failures?: number | null;
  today_failures?: number | null;
  today_requests?: number | null;
  data_latency_ms?: number | null;
  last_error?: string | null;
  last_error_type?: string | null;
  http_status?: number | null;
  is_rate_limited?: boolean | null;
}

/** GET /providers 列表项 */
export interface ProviderRow {
  id: string;
  name: string;
  category?: string | null;
  base_url?: string | null;
  priority?: number | null;
  is_enabled?: boolean | null;
  status?: string | null;
  health_score?: number | null;
  consecutive_failures?: number | null;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  description?: string | null;
  latest_health?: ProviderLatestHealth | null;
}

/** GET /providers/failover-events 列表项 */
export interface FailoverEventRow {
  id?: string;
  from_provider?: string | null;
  to_provider?: string | null;
  data_category?: string | null;
  symbol?: string | null;
  trigger_reason?: string | null;
  error_message?: string | null;
  occurred_at?: string | null;
  resolved_at?: string | null;
  resolution_type?: string | null;
  duration_seconds?: number | null;
  requests_affected?: number | null;
}

/** GET /providers/scores 列表项 */
export interface ProviderScoreRow {
  provider?: string;
  category?: string | null;
  scored_at?: string | null;
  accuracy_score?: number | null;
  latency_score?: number | null;
  stability_score?: number | null;
  completeness_score?: number | null;
  consistency_score?: number | null;
  overall_score?: number | null;
  rank?: number | null;
}

/** GET /providers/{name}/health 响应体 */
export interface ProviderHealthDetail {
  provider?: ProviderRow | null;
  latest_score?: ProviderScoreRow | null;
  history?: ProviderLatestHealth[];
}

/** POST /providers/{name}/test 响应体（实时健康快照） */
export interface ProviderTestResult {
  name?: string;
  category?: string | null;
  status?: string | null;
  health_score?: number | null;
  avg_latency_ms?: number | null;
  success_rate_24h?: number | null;
}

/* -------------------------------------------------------------------------- */
/* System（/system/health · /system/stats · /system/jobs）                     */
/* -------------------------------------------------------------------------- */

/** GET /system/health 响应体 */
export interface SystemHealthData {
  status?: string | null;
  database?: { ok?: boolean; latency_ms?: number; error?: string } | null;
  redis?: { ok?: boolean; latency_ms?: number; error?: string } | null;
  providers?: {
    source?: string | null;
    total?: number;
    by_status?: Record<string, number> | null;
    items?: {
      name?: string;
      category?: string | null;
      status?: string | null;
      is_enabled?: boolean | null;
      is_available?: boolean | null;
    }[];
  } | null;
  time?: string | null;
}

/** GET /system/stats 响应体 */
export interface SystemStatsData {
  tables?: Record<
    string,
    { rows?: number; latest_observation_time?: string | null; error?: string } | undefined
  >;
  provider_count?: number;
  time?: string | null;
}

/** GET /system/jobs 任务行 */
export interface JobRow {
  id: string;
  job_name?: string | null;
  job_type?: string | null;
  job_group?: string | null;
  description?: string | null;
  schedule_cron?: string | null;
  schedule_interval_seconds?: number | null;
  priority?: number | null;
  status?: string | null;
  is_enabled?: boolean | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_duration_ms?: number | null;
  avg_duration_ms?: number | null;
  retry_count?: number | null;
  consecutive_failures?: number | null;
  last_error?: string | null;
  /** 后端暂未返回执行历史；保留字段做防御性渲染 */
  history?: unknown;
  [key: string]: unknown;
}

/** GET /system/jobs 响应体（data = { count, jobs }） */
export interface JobsPayload {
  count?: number;
  jobs?: JobRow[];
}

/** 任务手动执行 / 暂停 / 恢复的响应体（宽松） */
export interface SystemJobAction {
  job_id?: string;
  job_name?: string;
  queued?: boolean;
  paused?: boolean;
  resumed?: boolean;
  note?: string;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Data Quality（/system/data-quality · /system/data-coverage）                */
/* -------------------------------------------------------------------------- */

/** GET /system/data-quality 检查记录行 */
export interface QualityRecordRow {
  check_time?: string | null;
  data_category?: string | null;
  table_name?: string | null;
  check_type?: string | null;
  status?: string | null;
  severity?: string | null;
  records_checked?: number | null;
  records_passed?: number | null;
  records_failed?: number | null;
  completeness_pct?: number | null;
  gaps_found?: unknown[] | null;
  conflicts_found?: unknown[] | null;
  anomalies_found?: unknown[] | null;
  auto_fixed?: boolean | null;
  requires_manual?: boolean | null;
}

/**
 * GET /system/data-quality 响应体。
 * 当前后端返回 { count, since, records }；同时兼容文档化前瞻结构
 * （generated_at / overall_verified_ratio / categories，见 types/api.ts DataQualityReport）。
 */
export interface DataQualityPayload {
  count?: number;
  since?: string | null;
  records?: QualityRecordRow[];

  generated_at?: string | null;
  overall_verified_ratio?: number | null;
  categories?: Record<string, Record<string, unknown>> | null;
  total_conflicts?: number | null;
  total_missing?: number | null;
  [key: string]: unknown;
}

/** GET /system/data-coverage 单类别覆盖条目（端点暂未上线，字段按宽松处理） */
export interface DataCoverageCategory {
  earliest?: string | null;
  latest?: string | null;
  coverage_pct?: number | null;
  rows?: number | null;
  quality_status?: string | null;
  [key: string]: unknown;
}

/** GET /system/data-coverage 响应体（宽松） */
export interface DataCoverageData {
  generated_at?: string | null;
  categories?: Record<string, DataCoverageCategory> | null;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Alerts（/alerts/smtp/* · /alerts/digest/config）                            */
/* -------------------------------------------------------------------------- */

/** GET/PUT /alerts/smtp/config 响应体（password 恒为掩码 "***" 或空串） */
export interface SmtpConfigData {
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
  [key: string]: unknown;
}

/** POST /alerts/smtp/test 响应体（success=false 时 error 携带原因） */
export interface SmtpTestResult {
  recipient?: string;
  success?: boolean;
  message_id?: string | null;
  error?: string | null;
  response?: string | null;
  [key: string]: unknown;
}

/** GET/PUT /alerts/digest/config 响应体 */
export interface DigestConfigData {
  enabled?: boolean | null;
  daily_hour?: number | null;
  weekly_day?: number | null;
  weekly_hour?: number | null;
  recipients?: string[] | null;
  [key: string]: unknown;
}

/** PUT /alerts/digest/config 请求体 */
export interface DigestConfigUpdate {
  enabled?: boolean;
  daily_hour?: number;
  weekly_day?: number;
  weekly_hour?: number;
  recipients?: string[];
}
