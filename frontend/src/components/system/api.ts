/**
 * 系统管理页面组补充 API（lib 目录约定不修改，故以 fetch 实现与 lib/api.ts 相同的信封语义）。
 *
 * 覆盖 apiClient 未包含的端点：
 * - SMTP 配置读写与测试邮件（GET/PUT /alerts/smtp/config、POST /alerts/smtp/test）
 * - 告警摘要配置（GET/PUT /alerts/digest/config）
 * - 数据覆盖范围（GET /system/data-coverage；后端暂未实现时以 ApiRequestError 降级）
 * - 任务暂停/恢复（PUT /system/jobs/{id}/pause|resume；后端暂未实现时以 ApiRequestError 降级）
 *
 * 错误处理与 lib/api.ts 一致：统一抛 ApiRequestError（业务码 + HTTP 状态），
 * 兼容 ok()/err() 信封（error: {code, message}）与 providers 路由的字符串 error。
 */

import { API_BASE_URL, ApiRequestError } from "@/lib/api";

import type {
  DataCoverageData,
  DigestConfigData,
  DigestConfigUpdate,
  SmtpConfigData,
  SmtpTestResult,
  SystemJobAction,
} from "./types";

import type { ApiResponse } from "@/types/api";

type Method = "GET" | "PUT" | "POST";

interface ErrorBodyLike {
  code?: unknown;
  message?: unknown;
}

/** 从信封 error 字段提取消息与业务码（兼容对象与字符串两种形态） */
function extractError(
  err: unknown,
  fallbackStatus: number,
): { message: string; code: string; status?: number } {
  const status = fallbackStatus > 0 ? fallbackStatus : undefined;
  if (typeof err === "string" && err.trim()) {
    return { message: err, code: `HTTP_${fallbackStatus}`, status };
  }
  if (err && typeof err === "object") {
    const body = err as ErrorBodyLike;
    const message = typeof body.message === "string" && body.message ? body.message : null;
    const code = typeof body.code === "string" && body.code ? body.code : null;
    if (message || code) {
      return {
        message: message ?? "请求失败",
        code: code ?? `HTTP_${fallbackStatus}`,
        status,
      };
    }
  }
  return { message: `请求失败（HTTP ${fallbackStatus}）`, code: `HTTP_${fallbackStatus}`, status };
}

async function request<T>(method: Method, url: string, body?: unknown): Promise<ApiResponse<T>> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE_URL}${url}`, {
      method,
      cache: "no-store",
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiRequestError("后端服务不可达", { code: "NETWORK_ERROR" });
  }

  let payload: ApiResponse<T> | null = null;
  try {
    payload = (await res.json()) as ApiResponse<T>;
  } catch {
    payload = null; // 非 JSON 响应（如 405 HTML）
  }

  if (!res.ok) {
    const info = extractError(payload?.error, res.status);
    throw new ApiRequestError(info.message, { code: info.code, status: info.status });
  }
  if (!payload) {
    throw new ApiRequestError("响应解析失败", { code: "HTTP_ERROR", status: res.status });
  }
  if (payload.success === false) {
    const info = extractError(payload.error, 0);
    throw new ApiRequestError(info.message, { code: info.code });
  }
  return payload;
}

export const systemApi = {
  /* ---- SMTP ---- */
  getSmtpConfig: () => request<SmtpConfigData>("GET", "/alerts/smtp/config"),
  updateSmtpConfig: (body: Record<string, unknown>) =>
    request<SmtpConfigData>("PUT", "/alerts/smtp/config", body),
  /** 发送测试邮件；recipient 缺省时由后端取已保存配置 */
  testSmtp: (recipient?: string) =>
    request<SmtpTestResult>("POST", "/alerts/smtp/test", recipient ? { recipient } : {}),

  /* ---- 告警摘要 ---- */
  getDigestConfig: () => request<DigestConfigData>("GET", "/alerts/digest/config"),
  updateDigestConfig: (body: DigestConfigUpdate) =>
    request<DigestConfigData>("PUT", "/alerts/digest/config", body),

  /* ---- 数据覆盖范围（后端暂未实现，失败时页面降级为质量记录推导） ---- */
  getDataCoverage: () => request<DataCoverageData>("GET", "/system/data-coverage"),

  /* ---- 任务控制（后端暂未实现 PUT 形态，失败时按钮区行内报错） ---- */
  pauseJob: (id: string) =>
    request<SystemJobAction>("PUT", `/system/jobs/${encodeURIComponent(id)}/pause`),
  resumeJob: (id: string) =>
    request<SystemJobAction>("PUT", `/system/jobs/${encodeURIComponent(id)}/resume`),
};
