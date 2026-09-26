import type { NextConfig } from "next";

/**
 * Next.js 基础配置（占位骨架）。
 * 后续可在此配置 API 反向代理 (rewrites)、图片域名、构建优化等。
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 示例：将 /api 请求代理到后端服务（开发阶段可启用）
  // async rewrites() {
  //   return [
  //     { source: "/api/:path*", destination: "http://localhost:8000/api/:path*" },
  //   ];
  // },
};

export default nextConfig;
