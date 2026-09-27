/**
 * API 客户端：axios 实例 + 统一错误处理 + 类型化端点分组。
 *
 * 响应拦截器解包 AxiosResponse -> ApiResponse 信封；
 * 所有错误统一转换为 ApiRequestError（含业务码与 request_id）。
 */

import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
} from "axios";

import type { ApiResponse } from "@/types/api";

/* -------------------------------------------------------------------------- */
/* 配置                                                                       */
/* -------------------------------------------------------------------------- */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";

/* -------------------------------------------------------------------------- */
/* 统一错误                                                                    */
/* -------------------------------------------------------------------------- */

/** 业务错误（UI 据此展示降级信息，而非原始 axios 错误） */
export class ApiRequestError extends Error {
  /** 后端 6 位业务码或 "NETWORK_ERROR" / "HTTP_ERROR" */
  readonly code: string;
  readonly status?: number;
  readonly requestId?: string;
  readonly details?: unknown;

  constructor(
    message: string,
    opts: {
      code?: string;
      status?: number;
      requestId?: string;
      details?: unknown;
    } = {},
  ) {
    super(message);
    this.name = "ApiRequestError";
    this.code = opts.code ?? "UNKNOWN";
    this.status = opts.status;
    this.requestId = opts.requestId;
    this.details = opts.details;
  }

  /** 网络层错误（后端不可达 / 超时），UI 可提示「后端服务不可达」 */
  get isNetworkError(): boolean {
    return this.code === "NETWORK_ERROR";
  }
}

function normalizeError(err: unknown): ApiRequestError {
  if (err instanceof ApiRequestError) return err;

  const axiosErr = err as AxiosError<ApiResponse<unknown>>;
  const status = axiosErr?.response?.status;
  const body = axiosErr?.response?.data;

  if (body && typeof body === "object" && body.error) {
    return new ApiRequestError(body.error.message || "请求失败", {
      code: String(body.error.code ?? `HTTP_${status ?? 0}`),
      status,
      requestId: body.error.request_id,
      details: body.error.details,
    });
  }

  if (axiosErr?.code === "ECONNABORTED") {
    return new ApiRequestError("请求超时，请稍后重试", {
      code: "NETWORK_ERROR",
      status,
    });
  }
  if (!axiosErr?.response) {
    return new ApiRequestError("后端服务不可达", { code: "NETWORK_ERROR" });
  }
  return new ApiRequestError(`请求失败（HTTP ${status}）`, {
    code: `HTTP_${status}`,
    status,
  });
}

/* -------------------------------------------------------------------------- */
/* 实例                                                                       */
/* -------------------------------------------------------------------------- */

const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: { "Content-Type": "application/json" },
});

/** 解包信封：调用方直接拿到 ApiResponse */
api.interceptors.response.use(
  (res) => res.data,
  (error) => Promise.reject(normalizeError(error)),
);

/* 类型化辅助：借助 axios 第二个泛型参数声明解包后的返回类型 */
const get = <T>(url: string, config?: AxiosRequestConfig) =>
  api.get<T, ApiResponse<T>>(url, config);

const post = <T>(url: string, body?: unknown, config?: AxiosRequestConfig) =>
  api.post<T, ApiResponse<T>>(url, body, config);

const put = <T>(url: string, body?: unknown, config?: AxiosRequestConfig) =>
  api.put<T, ApiResponse<T>>(url, body, config);

const del = <T>(url: string, config?: AxiosRequestConfig) =>
  api.delete<T, ApiResponse<T>>(url, config);

/* -------------------------------------------------------------------------- */
/* 查询参数类型                                                                */
/* -------------------------------------------------------------------------- */

export interface HistoryQuery {
  engine?: string;
  start?: string;
  end?: string;
  limit?: number;
  date?: string;
}

export interface SeriesQuery {
  start?: string;
  end?: string;
  interval?: string;
  limit?: number;
  as_of?: string;
}

export interface ListQuery {
  page?: number;
  page_size?: number;
  plan_id?: string;
  status?: string;
}

/* -------------------------------------------------------------------------- */
/* 类型化 API 分组                                                             */
/* -------------------------------------------------------------------------- */

import type {
  BacktestResult,
  Candle,
  ETFFlow,
  ETFSummary,
  EngineDashboardData,
  EngineHistoryData,
  EngineOutputResponse,
  ExchangeFlowData,
  FailoverEvent,
  FearGreedIndex,
  FundingData,
  Holdings,
  IndicatorDefinition,
  IndicatorValue,
  JobConfigUpdateBody,
  JobUpdateResult,
  LiquidationData,
  MacroEvent,
  MacroIndicator,
  MarketOverview,
  MarketStats,
  OhlcvParams,
  OnchainMetricPoint,
  OnchainMetricSummary,
  OpenInterest,
  OptionsIV,
  OptionsOverview,
  PerformanceReport,
  PriceData,
  ProviderConfigUpdateBody,
  ProviderHealth,
  ProviderInfo,
  ProviderReloadResult,
  ProviderSyncResult,
  ProviderUpdateResult,
  PutCallData,
  SystemHealth,
  SystemJobInfo,
  SystemSettingsPayload,
  Transaction,
  UserPlan,
} from "@/types/api";

export const apiClient = {
  /** 市场行情 */
  market: {
    getPrice: (symbol?: string) =>
      get<PriceData>("/market/price", { params: symbol ? { symbol } : undefined }),
    getOhlcv: (params: OhlcvParams) =>
      get<Candle[]>("/market/ohlcv", { params }),
    getStats: (symbol?: string) =>
      get<MarketStats>("/market/stats", { params: symbol ? { symbol } : undefined }),
    getOverview: (symbol?: string) =>
      get<MarketOverview>("/market/overview", { params: symbol ? { symbol } : undefined }),
    getOrderbook: (symbol?: string, depth?: number) =>
      get<Record<string, unknown>>("/market/orderbook", {
        params: { ...(symbol ? { symbol } : {}), ...(depth ? { depth } : {}) },
      }),
  },

  /** 分析引擎 */
  engine: {
    getCycle: () => get<EngineOutputResponse>("/engine/cycle"),
    getValuation: () => get<EngineOutputResponse>("/engine/valuation"),
    getRisk: () => get<EngineOutputResponse>("/engine/risk"),
    getRegime: () => get<EngineOutputResponse>("/engine/regime"),
    getDashboard: (includePrice = true) =>
      get<EngineDashboardData>("/engine/dashboard", {
        params: { include_price: includePrice },
      }),
    getPrediction: () => get<Record<string, unknown>>("/engine/prediction"),
    getHistory: (query: HistoryQuery = {}) =>
      get<EngineHistoryData>("/engine/history", { params: query }),
    getExplain: (target?: string, date?: string) =>
      get<Record<string, unknown>>("/engine/explain", {
        params: { ...(target ? { target } : {}), ...(date ? { date } : {}) },
      }),
  },

  /** 链上数据 */
  onchain: {
    getMetrics: () => get<OnchainMetricSummary[]>("/onchain/metrics"),
    getMetricHistory: (name: string, query: SeriesQuery = {}) =>
      get<OnchainMetricPoint[]>(`/onchain/metrics/${encodeURIComponent(name)}`, {
        params: query,
      }),
    getExchangeFlow: (query: SeriesQuery = {}) =>
      get<ExchangeFlowData>("/onchain/exchange-flow", { params: query }),
    getSupply: () => get<Record<string, unknown>>("/onchain/supply"),
    getMiners: () => get<Record<string, unknown>>("/onchain/miners"),
    getWhaleActivity: () => get<Record<string, unknown>>("/onchain/whale-activity"),
  },

  /** ETF */
  etf: {
    getFlows: (query: SeriesQuery & { ticker?: string } = {}) =>
      get<ETFFlow[]>("/etf/flows", { params: query }),
    getHoldings: () => get<Record<string, unknown>>("/etf/holdings"),
    getSummary: () => get<ETFSummary>("/etf/summary"),
  },

  /** 衍生品 */
  derivatives: {
    getFunding: (query: SeriesQuery = {}) =>
      get<FundingData>("/derivatives/funding", { params: query }),
    getOpenInterest: (query: SeriesQuery = {}) =>
      get<OpenInterest>("/derivatives/open-interest", { params: query }),
    getLiquidations: (query: SeriesQuery = {}) =>
      get<LiquidationData>("/derivatives/liquidations", { params: query }),
    getLongShort: (query: SeriesQuery = {}) =>
      get<Record<string, unknown>>("/derivatives/long-short", { params: query }),
    getBasis: (query: SeriesQuery = {}) =>
      get<Record<string, unknown>>("/derivatives/basis", { params: query }),
    getOverview: () => get<Record<string, unknown>>("/derivatives/overview"),
  },

  /** 期权 */
  options: {
    getOverview: () => get<OptionsOverview>("/options/overview"),
    getIV: () => get<OptionsIV>("/options/iv"),
    getPutCall: () => get<PutCallData>("/options/put-call"),
  },

  /** 宏观 */
  macro: {
    getIndicators: () => get<MacroIndicator[]>("/macro/indicators"),
    getEvents: (query: Record<string, unknown> = {}) =>
      get<MacroEvent[]>("/macro/events", { params: query }),
    getSeries: (name: string, query: SeriesQuery = {}) =>
      get<Record<string, unknown>>(`/macro/series/${encodeURIComponent(name)}`, {
        params: query,
      }),
  },

  /** 情绪 */
  sentiment: {
    getFearGreed: (query: SeriesQuery = {}) =>
      get<FearGreedIndex>("/sentiment/fear-greed", { params: query }),
    getSocial: () => get<Record<string, unknown>>("/sentiment/social"),
    getNews: () => get<Record<string, unknown>>("/sentiment/news"),
    getOverview: () => get<Record<string, unknown>>("/sentiment/overview"),
  },

  /** 技术指标 */
  indicators: {
    getList: () => get<IndicatorDefinition[]>("/indicators/list"),
    getSeries: (name: string, query: SeriesQuery = {}) =>
      get<IndicatorValue[]>(`/indicators/${encodeURIComponent(name)}`, {
        params: query,
      }),
    getDefinition: (name: string) =>
      get<IndicatorDefinition>(`/indicators/definitions/${encodeURIComponent(name)}`),
  },

  /** 我的计划 / 资产（登录后） */
  portfolio: {
    getPlans: (query: ListQuery = {}) => get<UserPlan[]>("/portfolio/plans", { params: query }),
    getPlan: (id: string) => get<UserPlan>(`/portfolio/plans/${id}`),
    createPlan: (body: Record<string, unknown>) => post<UserPlan>("/portfolio/plans", body),
    updatePlan: (id: string, body: Record<string, unknown>) =>
      put<UserPlan>(`/portfolio/plans/${id}`, body),
    deletePlan: (id: string) => del<{ deleted: boolean }>(`/portfolio/plans/${id}`),
    getTransactions: (query: ListQuery = {}) =>
      get<Transaction[]>("/portfolio/transactions", { params: query }),
    createTransaction: (body: Record<string, unknown>) =>
      post<Transaction>("/portfolio/transactions", body),
    deleteTransaction: (id: string) =>
      del<{ deleted: boolean }>(`/portfolio/transactions/${id}`),
    getHoldings: () => get<Holdings>("/portfolio/holdings"),
    getPerformance: (query: SeriesQuery = {}) =>
      get<PerformanceReport>("/portfolio/performance", { params: query }),
    simulate: (body: Record<string, unknown>) =>
      post<Record<string, unknown>>("/portfolio/simulate", body),
  },

  /** 回测 */
  backtest: {
    run: (body: Record<string, unknown>) =>
      post<{ run_id: string }>("/backtest/run", body),
    getRuns: (query: ListQuery = {}) =>
      get<BacktestResult[]>("/backtest/runs", { params: query }),
    getRun: (id: string) => get<BacktestResult>(`/backtest/runs/${id}`),
    deleteRun: (id: string) => del<{ deleted: boolean }>(`/backtest/runs/${id}`),
    compare: (runIds: string[]) =>
      post<Record<string, unknown>>("/backtest/compare", { run_ids: runIds }),
    getStrategies: () => get<Record<string, unknown>[]>("/backtest/strategies"),
  },

  /** 数据源 */
  providers: {
    getList: () => get<ProviderInfo[]>("/providers"),
    getDetail: (id: string) => get<ProviderInfo>(`/providers/${encodeURIComponent(id)}`),
    getHealth: (id: string) =>
      get<ProviderHealth>(`/providers/${encodeURIComponent(id)}/health`),
    testProvider: (id: string) =>
      post<Record<string, unknown>>(`/providers/${encodeURIComponent(id)}/test`),
    getFailoverEvents: () => get<FailoverEvent[]>("/providers/failover-events"),
    getScores: () => get<Record<string, unknown>>("/providers/scores"),
    /** 更新 Provider 配置（落库 + 热重载生效；凭据空串=保留旧值） */
    updateProvider: (name: string, data: ProviderConfigUpdateBody) =>
      put<ProviderUpdateResult>(`/providers/${encodeURIComponent(name)}`, data),
    /** 手动热重载指定 Provider（重建实例 + 重建优先级队列） */
    reloadProvider: (name: string) =>
      post<ProviderReloadResult>(`/providers/${encodeURIComponent(name)}/reload`),
    /** 从 providers.yaml 导入配置到 DB（overwrite=true 覆盖已有行） */
    syncFromYaml: (overwrite = false) =>
      post<ProviderSyncResult>("/providers/sync-from-yaml", { overwrite }),
  },

  /** 系统 */
  system: {
    getHealth: () => get<SystemHealth>("/system/health"),
    getJobs: () => get<SystemJobInfo[]>("/system/jobs"),
    getJob: (id: string) => get<SystemJobInfo>(`/system/jobs/${id}`),
    runJob: (id: string, params?: Record<string, unknown>) =>
      post<Record<string, unknown>>(`/system/jobs/${id}/run`, params),
    pauseJob: (id: string) => post<Record<string, unknown>>(`/system/jobs/${id}/pause`),
    resumeJob: (id: string) => post<Record<string, unknown>>(`/system/jobs/${id}/resume`),
    /** 更新调度任务配置（interval_seconds/enabled，即时热生效，无需重启） */
    updateJob: (name: string, data: JobConfigUpdateBody) =>
      put<JobUpdateResult>(`/system/jobs/${encodeURIComponent(name)}`, data),
    getDataQuality: () => get<Record<string, unknown>>("/system/data-quality"),
    getStats: () => get<Record<string, unknown>>("/system/stats"),
  },

  /** 系统设置（配置后台化；命名空间 alert/provider/platform） */
  settings: {
    /** 全部/分组设置（is_secret 值掩码 "***"） */
    getAll: (namespace?: string) =>
      get<SystemSettingsPayload>("/settings", {
        params: namespace ? { namespace } : undefined,
      }),
    /** 更新单项设置（upsert + 审计 + 缓存即时失效 → 热生效） */
    update: (key: string, value: unknown) =>
      put<{ key: string; updated: boolean }>(`/settings/${encodeURIComponent(key)}`, { value }),
  },

  /**
   * 智能预警（后端响应多为 { items, total, page, size } 分页 dict，
   * 具体结构见 app/services/alert_service.py；调用方以本文件
   * ApiResponse<Record<string, unknown>> 接收后自行窄化类型）。
   */
  alerts: {
    // ---- 规则 CRUD / 操作 ----
    getRules: (query: { status?: string; severity?: string; page?: number; size?: number } = {}) =>
      get<Record<string, unknown>>("/alerts/rules", { params: query }),
    getRule: (id: string) => get<Record<string, unknown>>(`/alerts/rules/${id}`),
    createRule: (body: Record<string, unknown>) =>
      post<Record<string, unknown>>("/alerts/rules", body),
    updateRule: (id: string, body: Record<string, unknown>) =>
      put<Record<string, unknown>>(`/alerts/rules/${id}`, body),
    deleteRule: (id: string) =>
      del<{ deleted: boolean; rule_id: string }>(`/alerts/rules/${id}`),
    testRule: (id: string) =>
      post<Record<string, unknown>>(`/alerts/rules/${id}/test`),
    pauseRule: (id: string) =>
      post<Record<string, unknown>>(`/alerts/rules/${id}/pause`),
    resumeRule: (id: string) =>
      post<Record<string, unknown>>(`/alerts/rules/${id}/resume`),
    backtestRule: (id: string, query: { start?: string; end?: string } = {}) =>
      post<Record<string, unknown>>(`/alerts/rules/${id}/backtest`, undefined, {
        params: query,
      }),
    // ---- 事件 ----
    getEvents: (
      query: { rule_id?: string; start?: string; end?: string; page?: number; size?: number } = {},
    ) => get<Record<string, unknown>>("/alerts/events", { params: query }),
    ackEvent: (id: string) =>
      post<Record<string, unknown>>(`/alerts/events/${id}/ack`),
    // ---- 渠道 ----
    getChannels: () => get<Record<string, unknown>>("/alerts/channels"),
    createChannel: (body: Record<string, unknown>) =>
      post<Record<string, unknown>>("/alerts/channels", body),
    testChannel: (id: string) =>
      post<Record<string, unknown>>(`/alerts/channels/${id}/test`),
    // ---- SMTP（密码仅返回掩码 ***，更新时空密码表示保留旧值）----
    getSmtpConfig: () => get<Record<string, unknown>>("/alerts/smtp/config"),
    updateSmtpConfig: (body: Record<string, unknown>) =>
      put<Record<string, unknown>>("/alerts/smtp/config", body),
    testSmtp: (body: Record<string, unknown> = {}) =>
      post<Record<string, unknown>>("/alerts/smtp/test", body),
    // ---- 摘要 ----
    getDigestConfig: () => get<Record<string, unknown>>("/alerts/digest/config"),
    updateDigestConfig: (body: Record<string, unknown>) =>
      put<Record<string, unknown>>("/alerts/digest/config", body),
    sendDigest: (body: { type: "daily" | "weekly"; recipients?: string[] | null }) =>
      post<Record<string, unknown>>("/alerts/digest/send", body),
    // ---- 统计与模板 ----
    getStats: () => get<Record<string, unknown>>("/alerts/stats"),
    getTemplates: () => get<Record<string, unknown>[]>("/alerts/templates"),
  },

  /** 认证 */
  auth: {
    login: (body: { username: string; password: string }) =>
      post<Record<string, unknown>>("/auth/login", body),
    register: (body: { username: string; email: string; password: string }) =>
      post<Record<string, unknown>>("/auth/register", body),
    me: () => get<Record<string, unknown>>("/auth/me"),
    refresh: (refreshToken: string) =>
      post<Record<string, unknown>>("/auth/refresh", { refresh_token: refreshToken }),
  },
} as const;

export type ApiClient = typeof apiClient;
