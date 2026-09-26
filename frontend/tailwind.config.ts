import type { Config } from "tailwindcss";

/**
 * Tailwind 设计 token —— 暗色金融终端主题。
 * 颜色对齐 docs/architecture/16-page-architecture.md 第 7 节状态视觉语言。
 */
const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/hooks/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        /* 背景层级 */
        bg: {
          DEFAULT: "#0d1117",
          raised: "#161b22",
          hover: "#1c2128",
          deep: "#010409",
        },
        /* 边框（避免与 border- 前缀冲突，命名为 line） */
        line: {
          DEFAULT: "#30363d",
          subtle: "#21262d",
        },
        /* 语义色 */
        accent: "#58a6ff",
        up: "#3fb950",
        down: "#f85149",
        warn: "#d29922",
        orange: "#db6d28",
        muted: "#8b949e",
      },
      fontFamily: {
        sans: [
          '"PingFang SC"',
          '"HarmonyOS Sans SC"',
          '"Source Han Sans SC"',
          '"Microsoft YaHei"',
          "system-ui",
          "sans-serif",
        ],
        mono: [
          '"JetBrains Mono"',
          '"SF Mono"',
          '"Cascadia Code"',
          '"Consolas"',
          '"ui-monospace"',
          "monospace",
        ],
      },
      keyframes: {
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(6px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "flash-up": {
          "0%": { color: "#3fb950" },
          "70%": { color: "#3fb950" },
          "100%": { color: "#e6edf3" },
        },
        "flash-down": {
          "0%": { color: "#f85149" },
          "70%": { color: "#f85149" },
          "100%": { color: "#e6edf3" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s infinite",
        "fade-up": "fade-up 0.35s cubic-bezier(0.22, 0.61, 0.36, 1) both",
        "flash-up": "flash-up 0.8s ease-out",
        "flash-down": "flash-down 0.8s ease-out",
        "pulse-soft": "pulse-soft 2.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};

export default config;
