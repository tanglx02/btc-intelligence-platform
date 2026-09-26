import type { Config } from "tailwindcss";

/**
 * Tailwind CSS 配置（占位骨架）。
 * 主题色板、字体、动画等设计系统 token 将在前端设计阶段补充。
 */
const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      // colors / fontFamily / animation 等设计 token 待补充
    },
  },
  plugins: [],
};

export default config;
