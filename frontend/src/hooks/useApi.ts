"use client";

/**
 * SWR 风格数据获取 hooks。
 *
 * useApi：loading / error / data 状态 + 可选轮询 + 错误降级（保留上次数据）；
 * usePolling：定时的 useApi 便捷封装。
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiRequestError } from "@/lib/api";
import type { ApiResponse } from "@/types/api";

export interface UseApiOptions {
  /** 自动刷新间隔（毫秒），0 或不传则不轮询；页面隐藏时暂停 */
  refreshInterval?: number;
  /** 为 false 时跳过首次请求 */
  enabled?: boolean;
}

export interface UseApiResult<T> {
  data: T | null;
  meta: ApiResponse<T>["meta"] | null;
  loading: boolean;
  error: ApiRequestError | null;
  /** 最后一次成功更新的本地时间 */
  lastUpdatedAt: number | null;
  /** 手动刷新（不清空现有数据） */
  refresh: () => void;
}

interface UseApiState<T> {
  data: T | null;
  meta: ApiResponse<T>["meta"] | null;
  loading: boolean;
  error: ApiRequestError | null;
  lastUpdatedAt: number | null;
}

export function useApi<T>(
  fetcher: () => Promise<ApiResponse<T>>,
  deps: readonly unknown[] = [],
  options: UseApiOptions = {},
): UseApiResult<T> {
  const { refreshInterval = 0, enabled = true } = options;

  // fetcher 通过 ref 透传，避免调用方未 memo 导致重复请求
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const inFlightRef = useRef(false);
  const mountedRef = useRef(true);

  const [state, setState] = useState<UseApiState<T>>({
    data: null,
    meta: null,
    loading: enabled,
    error: null,
    lastUpdatedAt: null,
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const execute = useCallback(async (silent = false) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    if (!silent) setState((prev) => ({ ...prev, loading: true }));

    try {
      const res = await fetcherRef.current();
      if (!mountedRef.current) return;
      setState({
        data: res.data,
        meta: res.meta ?? null,
        loading: false,
        // 成功后清除错误，即使返回降级数据（success=false 时 data 可能为 null）
        error: res.success
          ? null
          : new ApiRequestError(res.error?.message || "请求返回异常", {
              code: String(res.error?.code ?? "API_ERROR"),
            }),
        lastUpdatedAt: Date.now(),
      });
    } catch (err) {
      if (!mountedRef.current) return;
      // 降级：保留上次成功数据，仅标记错误
      setState((prev) => ({
        ...prev,
        loading: false,
        error:
          err instanceof ApiRequestError
            ? err
            : new ApiRequestError(err instanceof Error ? err.message : "未知错误"),
      }));
    } finally {
      inFlightRef.current = false;
    }
  }, []);

  // deps 变化触发重新请求（保留旧数据用于降级显示）
  useEffect(() => {
    if (!enabled) return;
    void execute(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, execute, ...deps]);

  // 轮询：页面不可见时暂停，避免无谓请求
  useEffect(() => {
    if (!enabled || refreshInterval <= 0) return;
    const id = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      void execute(true);
    }, refreshInterval);
    return () => clearInterval(id);
  }, [enabled, refreshInterval, execute]);

  const refresh = useCallback(() => void execute(true), [execute]);

  return { ...state, refresh };
}

/**
 * 定时轮询 hook（useApi 的轮询封装）。
 * intervalMs <= 0 时等价于普通 useApi。
 */
export function usePolling<T>(
  fetcher: () => Promise<ApiResponse<T>>,
  intervalMs: number,
  deps: readonly unknown[] = [],
): UseApiResult<T> {
  return useApi<T>(fetcher, deps, { refreshInterval: intervalMs });
}
