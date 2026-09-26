"""BTC 全市场智能研究平台 - 后端应用包。

本包采用分层架构：
- api          : HTTP 路由层（FastAPI Router）
- core         : 配置、日志、安全等基础设施
- models       : SQLAlchemy ORM 数据模型
- schemas      : Pydantic 请求/响应模型
- services     : 业务服务编排层
- providers    : 外部数据源适配器（行情/链上/ETF/衍生品/期权/宏观/情绪）
- engines      : 分析引擎（周期/估值/风险/状态/质量）
- indicators   : 技术指标计算
- backtest     : 策略回测框架
- portfolio    : 个人资金计划与组合管理
- scheduler    : 定时任务调度
- utils        : 通用工具函数
"""

__version__ = "0.1.0"
