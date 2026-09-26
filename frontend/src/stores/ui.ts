/**
 * 全局 UI 状态（Zustand）。
 *
 * mode 持久化到 localStorage（zustand/persist）；
 * 运行时状态（侧边栏、数据状态灯、实时价格）不持久化。
 */

import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

/** 普通 / 专业模式（全站三级信息层级与术语展示密度开关） */
export type UIMode = "simple" | "pro";

/** 全局数据状态灯 */
export type GlobalDataStatus = "ok" | "stale" | "error";

/** 顶部价格胶囊 */
export interface LastPrice {
  price: number;
  change24h: number;
}

export interface UIStore {
  mode: UIMode;
  setMode: (mode: UIMode) => void;
  toggleMode: () => void;

  /** 移动端侧边栏抽屉开关 */
  sidebarOpen: boolean;
  setSidebarOpen: (open: boolean) => void;
  toggleSidebar: () => void;

  globalDataStatus: GlobalDataStatus;
  setGlobalDataStatus: (status: GlobalDataStatus) => void;

  lastPrice: LastPrice | null;
  setLastPrice: (price: LastPrice) => void;
}

export const useUIStore = create<UIStore>()(
  persist(
    (set, get) => ({
      mode: "simple",
      setMode: (mode) => set({ mode }),
      toggleMode: () => set({ mode: get().mode === "simple" ? "pro" : "simple" }),

      sidebarOpen: false,
      setSidebarOpen: (sidebarOpen) => set({ sidebarOpen }),
      toggleSidebar: () => set({ sidebarOpen: !get().sidebarOpen }),

      globalDataStatus: "ok",
      setGlobalDataStatus: (globalDataStatus) => set({ globalDataStatus }),

      lastPrice: null,
      setLastPrice: (lastPrice) => set({ lastPrice }),
    }),
    {
      name: "btc-ui-preferences",
      storage: createJSONStorage(() => localStorage),
      /** 仅持久化用户偏好（模式），运行时状态不落盘 */
      partialize: (state) => ({ mode: state.mode }),
    },
  ),
);
