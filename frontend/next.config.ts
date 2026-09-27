import type { NextConfig } from "next";

/**
 * Next.js 配置。
 * 开发阶段将 /api/* 反向代理到后端服务（前端可用相对路径请求），
 * 生产环境由 Nginx 承担（docs/architecture/21-deployment.md）。
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  // 生产镜像以 standalone 模式运行（frontend/Dockerfile 复制 .next/standalone）
  output: "standalone",
  async rewrites() {
    const apiOrigin = process.env.API_PROXY_ORIGIN || "http://localhost:8000";
    return [
      {
        source: "/api/:path*",
        destination: `${apiOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
