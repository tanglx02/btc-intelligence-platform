"use client";

/**
 * 客户端 hydration 检测：SSR 渲染阶段返回 false，客户端水合完成后返回 true。
 * 用于持久化状态（localStorage）回填前后避免 SSR/CSR 不一致。
 */

import { useSyncExternalStore } from "react";

const emptySubscribe = () => () => {};
const getClientSnapshot = () => true;
const getServerSnapshot = () => false;

export function useHydrated(): boolean {
  return useSyncExternalStore(emptySubscribe, getClientSnapshot, getServerSnapshot);
}
