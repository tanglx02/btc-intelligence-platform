"""预警条件树 DTO 与求值结果结构。

ConditionNode 为 JSON 持久化格式（存于 alert_rules.condition_tree JSONB），
由 :class:`app.alerts.evaluator.AlertEvaluator` 递归求值。

三态语义与 ``app.portfolio.rule_engine`` 保持一致：
TRUE / FALSE / UNKNOWN（数据缺失或过期 → UNKNOWN，规则不触发）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

#: 组合条件类型（与叶子条件区分）
COMPOSITE_TYPES: frozenset[str] = frozenset({"AND", "OR", "NOT"})

#: 支持的叶子条件类型
LEAF_TYPES: frozenset[str] = frozenset({
    "threshold", "change_pct", "range_enter", "range_exit",
    "cross_above", "cross_below", "percentile", "state_change",
})

#: 条件树最大嵌套深度（防配置失控）
MAX_CONDITION_DEPTH: int = 5

# 比较运算符 -> 人类可读符号
_OP_SYMBOLS: dict[str, str] = {
    "gt": ">",
    "lt": "<",
    "gte": "≥",
    "lte": "≤",
    "eq": "=",
}


def _window_label(window_hours: float | None) -> str:
    """窗口小时数 -> 标签（24 -> "24h"，168 -> "7d"）。"""
    if window_hours is None:
        return "24h"
    if window_hours >= 24 and window_hours % 24 == 0:
        return f"{int(window_hours // 24)}d"
    return f"{window_hours:g}h"


class ConditionNode(BaseModel):
    """条件树节点（JSON 持久化格式）。"""

    # 节点类型：threshold/change_pct/range_enter/range_exit/cross_above/
    # cross_below/percentile/state_change/AND/OR/NOT
    type: str
    metric: str | None = None       # 指标名（如 "price", "mvrv", "funding_rate"）
    operator: str | None = None     # gt/lt/gte/lte/eq
    value: float | None = None      # 阈值
    low: float | None = None        # 区间下限
    high: float | None = None       # 区间上限
    window_hours: float | None = None  # 变化率窗口（1h/24h/7d）
    metric_b: str | None = None     # 交叉条件第二指标
    engine: str | None = None       # 引擎名（cycle/valuation/risk/regime）
    from_state: str | None = None   # 状态变化起始
    to_state: str | None = None     # 状态变化目标
    percentile: float | None = None # 历史分位阈值（0-100）
    children: list[ConditionNode] | None = None  # 组合条件子节点

    # ---- 校验 ----

    def validate_tree(self, depth: int = 1) -> None:
        """递归校验条件树结构（类型合法、深度受限、子节点数量）。"""
        if depth > MAX_CONDITION_DEPTH:
            raise ValueError(f"条件树嵌套深度超过上限 {MAX_CONDITION_DEPTH}")
        if self.type in COMPOSITE_TYPES:
            if not self.children:
                raise ValueError(f"组合条件 {self.type} 必须包含子节点")
            if self.type == "NOT" and len(self.children) != 1:
                raise ValueError("NOT 组合必须有且仅有一个子节点")
            for child in self.children:
                child.validate_tree(depth + 1)
        elif self.type in LEAF_TYPES:
            if self.children:
                raise ValueError(f"叶子条件 {self.type} 不应包含子节点")
        else:
            raise ValueError(f"未知条件类型: {self.type!r}")

    # ---- 收集 ----

    def iter_nodes(self) -> list[ConditionNode]:
        """深度优先遍历返回全部节点（含自身）。"""
        nodes = [self]
        for child in self.children or []:
            nodes.extend(child.iter_nodes())
        return nodes

    def collect_metrics(self) -> set[str]:
        """收集条件树引用的全部指标名（含 metric / metric_b）。"""
        metrics: set[str] = set()
        for node in self.iter_nodes():
            if node.type in COMPOSITE_TYPES:
                continue
            if node.metric:
                metrics.add(node.metric)
            if node.metric_b:
                metrics.add(node.metric_b)
        return metrics

    def collect_engines(self) -> set[str]:
        """收集条件树引用的引擎名（state_change 条件）。"""
        return {
            node.engine
            for node in self.iter_nodes()
            if node.type == "state_change" and node.engine
        }

    # ---- 人类可读描述 ----

    def to_human_text(self) -> str:
        """转人类可读描述（中文）。"""
        if self.type == "AND":
            return " 且 ".join(
                f"({c.to_human_text()})" for c in (self.children or [])
            )
        if self.type == "OR":
            return " 或 ".join(
                f"({c.to_human_text()})" for c in (self.children or [])
            )
        if self.type == "NOT":
            child = (self.children or [None])[0]
            inner = child.to_human_text() if child else "?"
            return f"非({inner})"

        metric = self.metric or "?"
        if self.type == "threshold":
            op_sym = _OP_SYMBOLS.get(self.operator or "", self.operator or "")
            return f"{metric} {op_sym} {_fmt(self.value)}"
        if self.type == "change_pct":
            op_sym = _OP_SYMBOLS.get(self.operator or "", self.operator or "")
            return (
                f"{metric} {_window_label(self.window_hours)}涨跌幅 "
                f"{op_sym} {_fmt(self.value)}%"
            )
        if self.type == "range_enter":
            return f"{metric} 进入区间 [{_fmt(self.low)}, {_fmt(self.high)}]"
        if self.type == "range_exit":
            return f"{metric} 离开区间 [{_fmt(self.low)}, {_fmt(self.high)}]"
        if self.type == "cross_above":
            target = self.metric_b or _fmt(self.value)
            return f"{metric} 上穿 {target}"
        if self.type == "cross_below":
            target = self.metric_b or _fmt(self.value)
            return f"{metric} 下穿 {target}"
        if self.type == "percentile":
            op_sym = _OP_SYMBOLS.get(self.operator or "gte", "≥")
            return f"{metric} 历史分位 {op_sym} {_fmt(self.percentile)}%"
        if self.type == "state_change":
            frm = self.from_state or "任意状态"
            to = self.to_state or "其他状态"
            return f"{self.engine or 'engine'} 状态由 {frm} 变为 {to}"
        return f"{self.type}({metric})"


def _fmt(v: float | None) -> str:
    """数值格式化（整数不带小数点）。"""
    if v is None:
        return "?"
    return f"{v:g}"


class AlertRuleCreate(BaseModel):
    """创建预警规则请求体。"""

    rule_name: str = Field(min_length=1, max_length=100)
    description: str = ""
    severity: str = "MEDIUM"
    category: str = "MARKET"
    target_symbol: str = "BTC"
    condition_tree: ConditionNode
    condition_text: str | None = None
    duration_seconds: int | None = None
    consecutive_count: int = Field(default=1, ge=1)
    cooldown_seconds: int = Field(default=86400, ge=0)
    channels: list[str] = Field(default_factory=lambda: ["EMAIL"])
    channel_config: dict[str, Any] = Field(default_factory=dict)
    min_data_quality: str = "ESTIMATED"
    require_multi_source: bool = False
    priority: int = 50
    tags: list[str] = Field(default_factory=list)

    def model_post_init(self, __context: Any) -> None:
        """创建后自动校验条件树并填充人类可读描述。"""
        self.condition_tree.validate_tree()
        if not self.condition_text:
            self.condition_text = self.condition_tree.to_human_text()


class EvalResult(BaseModel):
    """单次求值结果。"""

    triggered: bool
    result: str  # TRUE/FALSE/UNKNOWN
    condition_result: dict[str, Any]  # 各子条件详情
    evidence: list[dict[str, Any]]

    @property
    def is_true(self) -> bool:
        return self.result == "TRUE"

    @property
    def is_unknown(self) -> bool:
        return self.result == "UNKNOWN"

    @property
    def is_false(self) -> bool:
        return self.result == "FALSE"

    def first_true_description(self) -> str | None:
        """返回第一条 TRUE 的人类可读描述（用于事件文案）。"""
        for ev in self.evidence:
            if ev.get("result") == "TRUE":
                return ev.get("description")
        return None
