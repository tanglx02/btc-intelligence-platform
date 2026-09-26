"""预设预警规则模板。

前端「新建预警」页可直接基于模板填充表单：用户只需调整
``editable_fields`` 列出的字段（阈值/引擎名等），其余沿用 defaults。
condition_tree 与 :class:`app.alerts.schemas.ConditionNode` 格式严格一致，
可直接经 ``AlertRuleCreate`` 创建（model_post_init 自动校验并生成
condition_text）。
"""

from __future__ import annotations

ALERT_TEMPLATES: list[dict] = [
    {
        "id": "price_below",
        "name": "价格跌破提醒",
        "description": "当 BTC 价格跌破指定值时提醒",
        "icon": "📉",
        "category": "PRICE",
        "defaults": {
            "category": "MARKET",
            "severity": "HIGH",
            "condition_tree": {
                "type": "threshold", "metric": "price", "operator": "lt", "value": 90000,
            },
        },
        "editable_fields": ["value"],  # 用户可调整的字段
    },
    {
        "id": "price_above",
        "name": "价格突破提醒",
        "description": "当 BTC 价格突破指定值时提醒",
        "icon": "📈",
        "category": "PRICE",
        "defaults": {
            "category": "MARKET",
            "severity": "HIGH",
            "condition_tree": {
                "type": "threshold", "metric": "price", "operator": "gt", "value": 120000,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "major_drawdown",
        "name": "大幅回撤",
        "description": "BTC 从历史高点回撤超过指定百分比",
        "icon": "🪃",
        "category": "RISK",
        "defaults": {
            "category": "INDICATOR",
            "severity": "CRITICAL",
            "condition_tree": {
                "type": "threshold", "metric": "drawdown_pct", "operator": "lt", "value": -20,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "extreme_fear",
        "name": "极端恐慌",
        "description": "恐惧贪婪指数跌入极端恐慌区间",
        "icon": "😱",
        "category": "SENTIMENT",
        "defaults": {
            "category": "INDICATOR",
            "severity": "MEDIUM",
            "condition_tree": {
                "type": "threshold", "metric": "fear_greed", "operator": "lt", "value": 20,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "mvrv_low",
        "name": "估值低位",
        "description": "MVRV 低于指定值（历史底部区域参考）",
        "icon": "🟢",
        "category": "VALUATION",
        "defaults": {
            "category": "INDICATOR",
            "severity": "MEDIUM",
            "condition_tree": {
                "type": "threshold", "metric": "mvrv", "operator": "lt", "value": 1.0,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "mvrv_high",
        "name": "估值高位",
        "description": "MVRV 高于指定值（历史顶部区域参考）",
        "icon": "🔴",
        "category": "VALUATION",
        "defaults": {
            "category": "INDICATOR",
            "severity": "MEDIUM",
            "condition_tree": {
                "type": "threshold", "metric": "mvrv", "operator": "gt", "value": 3.0,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "leverage_risk",
        "name": "杠杆风险",
        "description": "资金费率过高且持仓量快速上升（杠杆堆积）",
        "icon": "⚡",
        "category": "DERIVATIVES",
        "defaults": {
            "category": "INDICATOR",
            "severity": "HIGH",
            "condition_tree": {
                "type": "AND",
                "children": [
                    {
                        "type": "threshold", "metric": "funding_rate",
                        "operator": "gt", "value": 0.0005,
                    },
                    {
                        "type": "threshold", "metric": "open_interest_change_24h",
                        "operator": "gt", "value": 15,
                    },
                ],
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "etf_anomaly",
        "name": "ETF资金异常",
        "description": "ETF 单日净流入/流出绝对值超过指定金额（亿美元）",
        "icon": "🏦",
        "category": "FLOW",
        "defaults": {
            "category": "PROVIDER",
            "severity": "MEDIUM",
            "condition_tree": {
                "type": "threshold", "metric": "etf_flow_1d", "operator": "gt", "value": 5,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "cycle_change",
        "name": "市场阶段变化",
        "description": "周期引擎判断的市场阶段发生变化时提醒",
        "icon": "🔄",
        "category": "ENGINE",
        "defaults": {
            "category": "ENGINE",
            "severity": "INFO",
            "condition_tree": {"type": "state_change", "engine": "cycle"},
        },
        "editable_fields": ["engine"],
    },
    {
        "id": "risk_high",
        "name": "综合风险",
        "description": "风险引擎综合评分超过指定值",
        "icon": "⚠️",
        "category": "RISK",
        "defaults": {
            "category": "ENGINE",
            "severity": "HIGH",
            "condition_tree": {
                "type": "threshold", "metric": "risk_score", "operator": "gt", "value": 70,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "rsi_oversold",
        "name": "RSI超卖",
        "description": "RSI 跌入超卖区间",
        "icon": "🧭",
        "category": "TECHNICAL",
        "defaults": {
            "category": "INDICATOR",
            "severity": "LOW",
            "condition_tree": {
                "type": "threshold", "metric": "rsi", "operator": "lt", "value": 30,
            },
        },
        "editable_fields": ["value"],
    },
    {
        "id": "portfolio_loss",
        "name": "持仓亏损提醒",
        "description": "组合累计收益率低于指定百分比",
        "icon": "🛡️",
        "category": "PORTFOLIO",
        "defaults": {
            "category": "PORTFOLIO",
            "severity": "HIGH",
            "condition_tree": {
                "type": "threshold", "metric": "portfolio_pnl_pct",
                "operator": "lt", "value": -20,
            },
        },
        "editable_fields": ["value"],
    },
]

_TEMPLATE_INDEX: dict[str, dict] = {t["id"]: t for t in ALERT_TEMPLATES}


def get_templates() -> list[dict]:
    """返回全部预设模板（浅拷贝列表，条目共享，调用方勿原地修改）。"""
    return list(ALERT_TEMPLATES)


def get_template(template_id: str) -> dict | None:
    """按 ID 取模板（未找到返回 None）。"""
    return _TEMPLATE_INDEX.get(template_id)
