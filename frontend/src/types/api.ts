/**
 * 统一 API 类型定义（与 docs/architecture/17-api-design.md 契约对齐）。
 *
 * 后端统一信封：{ success, data, meta, pagination?, error }
 * 所有 REST 响应经 lib/api.ts 解包后仍保持该结构。
 */

/* -------------------------------------------------------------------------- */
/* 统一信封                                                                    */
/* -------------------------------------------------------------------------- */

/** 数据质量状态（与数据质量引擎枚举一致） */
export type QualityStatus =
  | "VERIFIED"
  | "ESTIMATED"
  | "STALE"
  | "CONFLICT"
  | "INVALID";

/** 告警严重等级 */
export type Severity = "INFO" | "WARNING" | "HIGH" | "CRITICAL";

/** 故障切换信息（meta.failover） */
export interface FailoverInfo {
  from: string;
  reason: string;
  at: string;
}

/** 降级说明（503 场景 meta.degradation） */
export interface DegradationInfo {
  category: string;
  affected_providers: string[];
  last_good_data_at: string | null;
  retry_at: string | null;
}

/** 响应元数据：溯源 UI 的核心契约 */
export interface ApiMeta {
  /** 响应生成时间 */
  timestamp?: string;
  /** 实际提供数据的 Provider ID */
  source?: string | null;
  quality_status?: QualityStatus | null;
  cache_hit?: boolean;
  /** 数据本身的业务时间 */
  observation_time?: string | null;
  /** 数据抓取时间 */
  fetch_time?: string | null;
  failover?: FailoverInfo | null;
  /** 数据/模型置信度 0~1 */
  confidence?: number | null;
  /** STALE 降级标记 */
  degraded?: boolean;
  degradation?: DegradationInfo | null;
  [key: string]: unknown;
}

/** 错误详情 */
export interface ApiErrorDetail {
  field?: string;
  issue?: string;
  hint?: string;
}

/** 后端错误体 */
export interface ApiErrorBody {
  code: number | string;
  type?: string;
  message: string;
  details?: ApiErrorDetail[];
  request_id?: string;
}

/** 分页信息（cursor / offset 两种模式） */
export interface Pagination {
  cursor?: string | null;
  has_more?: boolean;
  total?: number;
  page?: number;
  page_size?: number;
}

/** 统一响应信封 */
export interface ApiResponse<T = unknown> {
  success: boolean;
  data: T | null;
  meta?: ApiMeta | null;
  pagination?: Pagination | null;
  error?: ApiErrorBody | null;
}

/* -------------------------------------------------------------------------- */
/* Market（市场行情）                                                          */
/* -------------------------------------------------------------------------- */

/** 交叉验证明细中的单源价格 */
export interface CrossValidationSource {
  provider: string;
  price: number;
  deviation_pct: number;
}

/** 多源交叉验证结果 */
export interface CrossValidation {
  status: QualityStatus;
  sources: CrossValidationSource[];
  median?: number;
  vwap?: number;
}

/** 当前价格（GET /market/price） */
export interface PriceData {
  symbol: string;
  price: number;
  change_24h_pct: number;
  change_7d_pct?: number;
  change_30d_pct?: number;
  high_24h?: number;
  low_24h?: number;
  cross_validation?: CrossValidation;
  [key: string]: unknown;
}

/** OHLCV K 线（GET /market/ohlcv） */
export interface Candle {
  /** ISO 字符串或 Unix 时间戳 */
  time: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  [key: string]: unknown;
}

/** OHLCV 查询参数 */
export interface OhlcvParams {
  symbol?: string;
  interval?: "1m" | "5m" | "15m" | "1h" | "4h" | "1d" | "1w";
  start?: string;
  end?: string;
  limit?: number;
  cursor?: string;
}

/** 市场统计（GET /market/stats） */
export interface MarketStats {
  market_cap?: number;
  ath?: number;
  drawdown_from_ath_pct?: number;
  volatility_24h?: number;
  atr?: number;
  vwap?: number;
  volume_24h?: number;
  [key: string]: unknown;
}

/** 市场总览（GET /market/overview）——聚合结构，保持宽松 */
export interface MarketOverview {
  price?: PriceData;
  stats?: MarketStats;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Engines（引擎输出）                                                         */
/* -------------------------------------------------------------------------- */

/** 引擎证据条目 */
export interface EngineEvidence {
  /** 因子名，如 "MVRV" */
  factor: string;
  value: unknown;
  /** 一句话解读 */
  interpretation?: string;
  weight?: number;
  /** true=支持结论 / false=反对结论 */
  supports: boolean;
  confidence?: number;
  data_source?: string;
  historical_percentile?: number;
  indicator_code?: string;
  quality_status?: QualityStatus;
}

/**
 * 引擎输出（cycle/valuation/risk/regime 通用结构）。
 * state 为描述性结论字符串（如 "UPTREND" / "HIGH" / "MEDIUM"）。
 */
export interface EngineOutput {
  state: string;
  score?: number | null;
  /** 0~1 */
  confidence: number;
  explanation?: string | null;
  data_quality?: string | null;
  observation_time?: string | null;
  calculated_at?: string | null;
  dimension_scores?: Record<string, number> | null;
  data_gaps?: string[];
  model_version?: string | null;
  supporting_evidence: EngineEvidence[];
  opposing_evidence: EngineEvidence[];
  historical_similar?: unknown;
  metadata?: Record<string, unknown> | null;
  [key: string]: unknown;
}

/** 单引擎端点响应体：{ engine, output } */
export interface EngineOutputResponse {
  engine: string;
  output: EngineOutput | null;
}

/* 各引擎状态别名（state 值由引擎动态定义，保留字符串扩展性） */

/** 周期引擎阶段（如 ACCUMULATION / UPTREND / DISTRIBUTION / TOP_RISK） */
export type CycleState = string;
/** 估值引擎五档（DEEP_UNDERVALUE / UNDERVALUE / FAIR / HIGH / EXTREME_HIGH） */
export type ValuationState = string;
/** 风险引擎档位（LOW / MEDIUM / HIGH / EXTREME） */
export type RiskState = string;
/** 综合市场状态（如 UPTREND_OVEREXTENDED） */
export type RegimeState = string;

/** Regime 九大维度单状态 */
export interface RegimeDimensionState {
  state: string;
  confidence: number;
}

/** Regime 九大维度状态集合（trend/valuation/capital_flow/onchain/derivative/macro/sentiment/risk/cycle） */
export interface RegimeStates {
  trend?: RegimeDimensionState;
  valuation?: RegimeDimensionState;
  capital_flow?: RegimeDimensionState;
  onchain?: RegimeDimensionState;
  derivative?: RegimeDimensionState;
  macro?: RegimeDimensionState;
  sentiment?: RegimeDimensionState;
  risk?: RegimeDimensionState;
  cycle?: RegimeDimensionState;
  [key: string]: RegimeDimensionState | undefined;
}

/** Regime 输出 metadata 结构（GET /engine/regime） */
export interface RegimeMetadata {
  regime?: RegimeState;
  summary?: string;
  confidence?: number;
  states?: RegimeStates;
  changed_at?: string;
  model_version?: string;
  [key: string]: unknown;
}

/** 首页聚合看板（GET /engine/dashboard） */
export interface EngineDashboardData {
  cycle?: EngineOutputResponse;
  valuation?: EngineOutputResponse;
  risk?: EngineOutputResponse;
  regime?: EngineOutputResponse;
  price?: PriceData | null;
  errors?: Record<string, string> | null;
}

/** 引擎历史序列（GET /engine/history） */
export interface EngineHistoryData {
  engine: string;
  start: string;
  end: string;
  count: number;
  outputs: EngineOutput[];
}

/* -------------------------------------------------------------------------- */
/* On-Chain（链上）                                                            */
/* -------------------------------------------------------------------------- */

/** 链上指标摘要（GET /onchain/metrics） */
export interface OnchainMetricSummary {
  name: string;
  label?: string;
  value?: number;
  percentile?: number;
  state?: string;
  category?: string;
  description?: string;
  updated_at?: string;
  [key: string]: unknown;
}

/** 链上指标历史点 */
export interface OnchainMetricPoint {
  date?: string;
  time?: string;
  value: number;
  [key: string]: unknown;
}

/** 交易所资金流（GET /onchain/exchange-flow） */
export interface ExchangeFlowData {
  inflow?: number;
  outflow?: number;
  netflow?: number;
  inflow_7d?: number;
  outflow_7d?: number;
  netflow_7d?: number;
  by_exchange?: { exchange: string; inflow?: number; outflow?: number; netflow?: number }[];
  history?: {
    date?: string;
    inflow?: number;
    outflow?: number;
    netflow?: number;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* ETF                                                                        */
/* -------------------------------------------------------------------------- */

/** ETF 每日资金流（GET /etf/flows） */
export interface ETFFlow {
  date?: string;
  ticker?: string;
  name?: string;
  net_flow?: number;
  inflow?: number;
  outflow?: number;
  total_holdings_btc?: number;
  [key: string]: unknown;
}

/** ETF 汇总（GET /etf/summary） */
export interface ETFSummary {
  latest_net_flow?: number;
  net_flow_7d?: number;
  net_flow_30d?: number;
  net_flow_90d?: number;
  cumulative_net_flow?: number;
  total_holdings_btc?: number;
  holdings_pct_of_supply?: number;
  updated_at?: string;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Derivatives（衍生品）与 Options（期权）                                       */
/* -------------------------------------------------------------------------- */

/** 资金费率（GET /derivatives/funding） */
export interface FundingData {
  symbol?: string;
  exchange?: string;
  funding_rate?: number;
  predicted_rate?: number;
  next_funding_time?: string;
  by_exchange?: { exchange: string; funding_rate?: number; [key: string]: unknown }[];
  history?: { date?: string; funding_rate?: number; [key: string]: unknown }[];
  [key: string]: unknown;
}

/** 未平仓合约（GET /derivatives/open-interest） */
export interface OpenInterest {
  total_oi?: number;
  oi_usd?: number;
  by_exchange?: { exchange: string; oi?: number; oi_usd?: number }[];
  history?: { date?: string; oi?: number; oi_usd?: number; [key: string]: unknown }[];
  [key: string]: unknown;
}

/** 清算数据（GET /derivatives/liquidations） */
export interface LiquidationData {
  long_liq_24h?: number;
  short_liq_24h?: number;
  total_liq_24h?: number;
  history?: { date?: string; long?: number; short?: number; [key: string]: unknown }[];
  events?: {
    time?: string;
    exchange?: string;
    side?: string;
    amount?: number;
    price?: number;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
}

/** 期权概览（GET /options/overview） */
export interface OptionsOverview {
  total_oi?: number;
  total_oi_usd?: number;
  volume_24h?: number;
  notional_value?: number;
  max_pain?: number;
  put_call_ratio?: number;
  expiries?: { date?: string; oi?: number; volume?: number; [key: string]: unknown }[];
  [key: string]: unknown;
}

/** 隐含波动率（GET /options/iv） */
export interface OptionsIV {
  dvol?: number;
  iv_percentile?: number;
  skew_25d?: number;
  term_structure?: { tenor?: string; iv?: number; [key: string]: unknown }[];
  [key: string]: unknown;
}

/** Put/Call 比率（GET /options/put-call） */
export interface PutCallData {
  volume_ratio?: number;
  oi_ratio?: number;
  history?: {
    date?: string;
    volume_ratio?: number;
    oi_ratio?: number;
    [key: string]: unknown;
  }[];
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Macro（宏观）与 Sentiment（情绪）                                            */
/* -------------------------------------------------------------------------- */

/** 宏观指标（GET /macro/indicators） */
export interface MacroIndicator {
  name: string;
  label?: string;
  value?: number;
  unit?: string;
  previous_value?: number;
  state?: string;
  updated_at?: string;
  description?: string;
  [key: string]: unknown;
}

/** 宏观事件（GET /macro/events） */
export interface MacroEvent {
  id?: string;
  title?: string;
  type?: string;
  event_time?: string;
  actual?: number;
  expected?: number;
  previous?: number;
  importance?: Severity;
  [key: string]: unknown;
}

/** 恐惧贪婪指数（GET /sentiment/fear-greed） */
export interface FearGreedIndex {
  value: number;
  classification?: string;
  previous_value?: number;
  updated_at?: string;
  history?: { date: string; value: number }[];
  factors?: { name: string; value?: number; weight?: number }[];
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Indicators（技术指标）                                                      */
/* -------------------------------------------------------------------------- */

/** 指标定义（GET /indicators/definitions/{name}） */
export interface IndicatorDefinition {
  name: string;
  label?: string;
  category?: string;
  description?: string;
  formula?: string;
  params?: Record<string, unknown>;
  status?: string;
  [key: string]: unknown;
}

/** 指标数据点 */
export interface IndicatorValue {
  name?: string;
  time?: string;
  date?: string;
  value: number;
  percentile?: number;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Portfolio（个人）                                                           */
/* -------------------------------------------------------------------------- */

/** 计划加仓规则条目 */
export interface PlanRule {
  drawdown_from_ath_pct?: number;
  multiplier?: number;
  [key: string]: unknown;
}

/** 我的计划（GET /portfolio/plans） */
export interface UserPlan {
  id: string;
  name: string;
  status?: string;
  initial_capital?: number;
  base_amount?: number;
  frequency?: string;
  currency?: string;
  start_date?: string;
  end_date?: string | null;
  rules?: PlanRule[];
  max_drawdown_tolerance?: number;
  next_investment_at?: string | null;
  next_investment_amount?: number;
  invested_total?: number;
  created_at?: string;
  updated_at?: string;
  [key: string]: unknown;
}

/** 交易方向 */
export type TransactionSide = "BUY" | "SELL";

/** 交易记录（GET /portfolio/transactions） */
export interface Transaction {
  id: string;
  plan_id?: string;
  side: TransactionSide;
  price: number;
  quantity: number;
  fee?: number;
  traded_at?: string;
  note?: string;
  created_at?: string;
  [key: string]: unknown;
}

/** 当前持仓（GET /portfolio/holdings） */
export interface Holdings {
  btc_amount?: number;
  avg_cost?: number;
  market_value?: number;
  invested_total?: number;
  unrealized_pnl?: number;
  unrealized_pnl_pct?: number;
  currency?: string;
  updated_at?: string;
  [key: string]: unknown;
}

/** 绩效报告（GET /portfolio/performance） */
export interface PerformanceReport {
  equity_curve?: { date: string; value: number; invested?: number }[];
  total_return_pct?: number;
  max_drawdown_pct?: number;
  sharpe?: number;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Backtest（回测）                                                            */
/* -------------------------------------------------------------------------- */

export type BacktestStatus = "PENDING" | "RUNNING" | "DONE" | "FAILED";

/** 回测指标汇总 */
export interface BacktestMetrics {
  final_value?: number;
  total_invested?: number;
  total_return_pct?: number;
  annualized_return_pct?: number;
  max_drawdown_pct?: number;
  max_drawdown_duration_days?: number;
  sharpe?: number;
  sortino?: number;
  btc_amount?: number;
  avg_cost?: number;
  total_fees?: number;
  win_rate?: number;
  best_year?: number;
  worst_year?: number;
  [key: string]: unknown;
}

/** 回测逐笔交易 */
export interface BacktestTrade {
  date: string;
  side?: string;
  price?: number;
  amount?: number;
  quantity?: number;
  multiplier?: number;
  [key: string]: unknown;
}

/** 回测结果（GET /backtest/runs/{id}） */
export interface BacktestResult {
  run_id: string;
  status: BacktestStatus;
  params?: Record<string, unknown>;
  metrics?: BacktestMetrics | null;
  equity_curve?: { date: string; value: number; invested?: number }[] | null;
  drawdown_curve?: { date: string; drawdown_pct: number }[] | null;
  trades?: BacktestTrade[] | null;
  audit_report?: Record<string, unknown> | null;
  model_version?: string | null;
  created_at?: string;
  finished_at?: string | null;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Providers（数据源）                                                         */
/* -------------------------------------------------------------------------- */

export type ProviderStatus = "ONLINE" | "DEGRADED" | "OFFLINE" | "DISABLED" | (string & {});

/** Provider 评分 */
export interface ProviderScore {
  stability?: number;
  latency?: number;
  consistency?: number;
  overall?: number;
  [key: string]: unknown;
}

/** Provider 信息（GET /providers） */
export interface ProviderInfo {
  name: string;
  display_name?: string;
  category?: string;
  status?: ProviderStatus;
  priority?: number;
  enabled?: boolean;
  description?: string;
  base_url?: string;
  success_rate_24h?: number;
  avg_latency_ms?: number;
  score?: ProviderScore;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  [key: string]: unknown;
}

/** Provider 健康详情（GET /providers/{id}/health） */
export interface ProviderHealth {
  name?: string;
  status?: ProviderStatus;
  latency_ms?: number;
  success_rate_1h?: number;
  success_rate_24h?: number;
  consecutive_failures?: number;
  failures_today?: number;
  rate_limited?: boolean;
  score?: ProviderScore;
  last_success_at?: string | null;
  last_failure_at?: string | null;
  last_error?: string | null;
  [key: string]: unknown;
}

/** 故障切换事件（GET /providers/failover-events） */
export interface FailoverEvent {
  id?: string;
  at?: string;
  from_provider?: string;
  to_provider?: string;
  reason?: string;
  recovered_at?: string | null;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* Alerts（智能预警）                                                          */
/* -------------------------------------------------------------------------- */

/** 告警规则 */
export interface AlertRule {
  id: string;
  name: string;
  enabled?: boolean;
  condition_type?: string;
  condition_config?: Record<string, unknown>;
  severity?: Severity;
  channels?: string[];
  cooldown_minutes?: number;
  last_triggered_at?: string | null;
  created_at?: string;
  [key: string]: unknown;
}

/** 告警事件 */
export interface AlertEvent {
  id: string;
  rule_id?: string;
  rule_name?: string;
  severity?: Severity;
  title?: string;
  message?: string;
  context?: Record<string, unknown>;
  triggered_at?: string;
  read?: boolean;
  [key: string]: unknown;
}

/** 告警模板 */
export interface AlertTemplate {
  key?: string;
  name: string;
  description?: string;
  default_config?: Record<string, unknown>;
  [key: string]: unknown;
}

/** SMTP 通知配置 */
export interface SmtpConfig {
  host?: string;
  port?: number;
  username?: string;
  password_set?: boolean;
  from_email?: string;
  use_tls?: boolean;
  enabled?: boolean;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* System（系统）                                                              */
/* -------------------------------------------------------------------------- */

/** 数据类别质量摘要 */
export interface DataQualityCategory {
  verified_ratio?: number;
  estimated_ratio?: number;
  stale_ratio?: number;
  conflict_ratio?: number;
  invalid_ratio?: number;
  last_update?: string | null;
  missing_count?: number;
  conflict_count?: number;
  failure_count?: number;
  failover_count?: number;
  current_provider?: string | null;
  [key: string]: unknown;
}

/** 数据质量报告（GET /system/data-quality） */
export interface DataQualityReport {
  generated_at?: string;
  overall_verified_ratio?: number;
  categories?: Record<string, DataQualityCategory>;
  total_conflicts?: number;
  total_missing?: number;
  [key: string]: unknown;
}

export type SystemJobStatus =
  | "IDLE"
  | "RUNNING"
  | "QUEUED"
  | "PAUSED"
  | "SUCCESS"
  | "FAILED"
  | (string & {});

/** 系统任务信息（GET /system/jobs） */
export interface SystemJobInfo {
  id: string;
  name: string;
  status: SystemJobStatus;
  cron?: string;
  priority?: number;
  last_run_at?: string | null;
  last_run_duration_ms?: number | null;
  last_result?: string | null;
  next_run_at?: string | null;
  checkpoint?: Record<string, unknown> | null;
  consecutive_failures?: number;
  [key: string]: unknown;
}

/** 系统健康检查（GET /system/health） */
export interface SystemHealth {
  status: "OK" | "DEGRADED" | "UNAVAILABLE" | (string & {});
  uptime_seconds?: number;
  components?: Record<string, { status: string; latency_ms?: number; [key: string]: unknown }>;
  modules?: Record<
    string,
    { status: string; source?: string; note?: string; [key: string]: unknown }
  >;
  [key: string]: unknown;
}

/* -------------------------------------------------------------------------- */
/* WebSocket 推送                                                             */
/* -------------------------------------------------------------------------- */

/** WS 推送消息通用结构：{ channel, data, timestamp } */
export interface WsMessage<T = unknown> {
  channel: string;
  data: T;
  timestamp?: string;
}

/** price 频道推送数据 */
export interface WsPriceData {
  symbol?: string;
  price: number;
  change_24h_pct?: number;
  source?: string;
  quality_status?: QualityStatus;
  [key: string]: unknown;
}

/** provider_status 频道推送数据 */
export interface WsProviderStatusData {
  total?: number;
  by_status?: Record<string, number>;
  items?: ProviderInfo[];
  [key: string]: unknown;
}

/** WS 支持的频道 */
export type WsChannel = "price" | "provider_status";
