"""BTC 全市场智能研究平台 - Backend API 入口。

这是 FastAPI 应用的最小占位骨架，仅包含应用实例与健康检查端点。
业务路由将在 app.api.v1 中逐步实现后挂载到此处。
"""

from fastapi import FastAPI

app = FastAPI(
    title="BTC Intelligence Platform",
    description="BTC 全市场智能研究、历史数据、周期分析、个人资金计划、策略回测与风险监测平台",
    version="0.1.0",
)


@app.get("/health")
async def health_check():
    """健康检查端点，供容器编排与负载均衡探活使用。"""
    return {"status": "ok", "service": "btc-platform-api"}
