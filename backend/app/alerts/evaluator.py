"""预警条件树求值器（纯计算，无 IO）。

三态语义与 ``app.portfolio.rule_engine`` 的 ConditionResult 保持一致：
TRUE / FALSE / UNKNOWN。

UNKNOWN 语义（数据缺失不触发）：叶子条件引用的字段位于
``AlertContext.stale_fields`` 或取值为 None 时，该条件求值为 UNKNOWN；
组合条件按 Kleene 三值逻辑传播（AND 遇 UNKNOWN 不确定、OR 遇 TRUE
直接成立、NOT 保持 UNKNOWN）。最终 result != TRUE 的规则一律不触发。
"""

from __future__ import annotations

from typing import Any

from app.alerts.context_builder import AlertContext
from app.alerts.schemas import ConditionNode, EvalResult
from app.portfolio.rule_engine import ConditionResult

#: 数值相等比较的相对容差
_EQ_TOLERANCE = 1e-9


def _cmp(
    value: float | None, operator: str | None, threshold: float | None
) -> ConditionResult:
    """单值阈值比较（值或阈值为 None、运算符未知 -> UNKNOWN）。"""
    if value is None or threshold is None:
        return ConditionResult.UNKNOWN
    op = operator or "gt"
    if op == "gt":
        ok = value > threshold
    elif op == "gte":
        ok = value >= threshold
    elif op == "lt":
        ok = value < threshold
    elif op == "lte":
        ok = value <= threshold
    elif op == "eq":
        ok = abs(value - threshold) <= _EQ_TOLERANCE * max(1.0, abs(threshold))
    else:
        return ConditionResult.UNKNOWN
    return ConditionResult.TRUE if ok else ConditionResult.FALSE


def _enum_value(v: Any) -> str:
    """枚举/字符串统一取值。"""
    return v.value if hasattr(v, "value") else str(v)


class AlertEvaluator:
    """规则求值器：递归求值条件树，产出三态结果 + 证据链。"""

    #: 条件树最大递归深度（防御性上限，超出按 UNKNOWN 处理）
    MAX_DEPTH = 5

    def evaluate(self, condition_tree: dict, context: AlertContext) -> EvalResult:
        """递归求值条件树。"""
        node = ConditionNode.model_validate(condition_tree)
        details: dict[str, Any] = {}
        evidence: list[dict[str, Any]] = []
        result = self._eval_node(
            node, context, path="root", details=details, evidence=evidence, depth=0
        )
        return EvalResult(
            triggered=(result == ConditionResult.TRUE),
            result=result.value,
            condition_result=details,
            evidence=evidence,
        )

    # ------------------------------------------------------------------
    # 内部：节点分发
    # ------------------------------------------------------------------

    def _eval_node(
        self,
        node: ConditionNode,
        context: AlertContext,
        *,
        path: str,
        details: dict[str, Any],
        evidence: list[dict[str, Any]],
        depth: int,
    ) -> ConditionResult:
        """求值单个节点（组合条件递归，叶子条件分发）。"""
        if depth > self.MAX_DEPTH:
            details[path] = {"type": node.type, "result": "UNKNOWN",
                             "reason": "嵌套深度超限"}
            return ConditionResult.UNKNOWN

        if node.type in ("AND", "OR", "NOT"):
            return self._eval_composite(
                node, context, path=path, details=details,
                evidence=evidence, depth=depth,
            )
        return self._eval_leaf(
            node, context, path=path, details=details, evidence=evidence
        )

    # ------------------------------------------------------------------
    # 内部：组合条件（Kleene 三值逻辑）
    # ------------------------------------------------------------------

    def _eval_composite(
        self,
        node: ConditionNode,
        context: AlertContext,
        *,
        path: str,
        details: dict[str, Any],
        evidence: list[dict[str, Any]],
        depth: int,
    ) -> ConditionResult:
        results: list[ConditionResult] = []
        child_paths: list[str] = []
        for i, child in enumerate(node.children or []):
            child_path = f"{path}.{i}"
            child_paths.append(child_path)
            results.append(
                self._eval_node(
                    child, context, path=child_path, details=details,
                    evidence=evidence, depth=depth + 1,
                )
            )

        if node.type == "NOT":
            inner = results[0] if results else ConditionResult.UNKNOWN
            if inner == ConditionResult.TRUE:
                outcome = ConditionResult.FALSE
            elif inner == ConditionResult.FALSE:
                outcome = ConditionResult.TRUE
            else:
                outcome = ConditionResult.UNKNOWN
        elif node.type == "AND":
            if any(r == ConditionResult.FALSE for r in results):
                outcome = ConditionResult.FALSE
            elif any(r == ConditionResult.UNKNOWN for r in results):
                outcome = ConditionResult.UNKNOWN
            else:
                outcome = ConditionResult.TRUE
        else:  # OR
            if any(r == ConditionResult.TRUE for r in results):
                outcome = ConditionResult.TRUE
            elif any(r == ConditionResult.UNKNOWN for r in results):
                outcome = ConditionResult.UNKNOWN
            else:
                outcome = ConditionResult.FALSE

        details[path] = {
            "type": node.type,
            "result": outcome.value,
            "children": child_paths,
        }
        return outcome

    # ------------------------------------------------------------------
    # 内部：叶子条件
    # ------------------------------------------------------------------

    def _eval_leaf(
        self,
        node: ConditionNode,
        context: AlertContext,
        *,
        path: str,
        details: dict[str, Any],
        evidence: list[dict[str, Any]],
    ) -> ConditionResult:
        result, detail = self._dispatch_leaf(node, context)
        detail.update({
            "type": node.type,
            "metric": node.metric,
            "result": result.value,
            "description": node.to_human_text(),
        })
        details[path] = detail
        evidence.append(detail)
        return result

    def _dispatch_leaf(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """分发到具体条件检查（返回 求值结果, 详情）。"""
        handler = {
            "threshold": self._check_threshold,
            "change_pct": self._check_change_pct,
            "range_enter": self._check_range_enter,
            "range_exit": self._check_range_exit,
            "cross_above": self._check_cross_above,
            "cross_below": self._check_cross_below,
            "percentile": self._check_percentile,
            "state_change": self._check_state_change,
        }.get(node.type)
        if handler is None:
            return ConditionResult.UNKNOWN, {"reason": f"未知条件类型: {node.type}"}
        return handler(node, context)

    def _check_threshold(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """threshold: prices/indicators 值 vs 阈值（gt/lt/gte/lte/eq）。"""
        value = context.resolve_value(node.metric)
        result = _cmp(value, node.operator, node.value)
        return result, {"value": value, "threshold": node.value}

    def _check_change_pct(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """change_pct: 指定窗口的价格涨跌幅 vs 阈值。"""
        value = context.resolve_price_change(node.metric, node.window_hours)
        result = _cmp(value, node.operator, node.value)
        return result, {
            "value": value,
            "threshold": node.value,
            "window_hours": node.window_hours,
        }

    def _check_range(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any], bool]:
        """range 公共逻辑：返回 (结果, 详情, 是否在区间内)。"""
        value = context.resolve_value(node.metric)
        if value is None or (node.low is None and node.high is None):
            return ConditionResult.UNKNOWN, {"value": value}, False
        in_range = True
        if node.low is not None:
            in_range = in_range and value >= node.low
        if node.high is not None:
            in_range = in_range and value <= node.high
        detail = {"value": value, "low": node.low, "high": node.high}
        return ConditionResult.TRUE, detail, in_range

    def _check_range_enter(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """range_enter: 值进入 [low, high] 区间。"""
        result, detail, in_range = self._check_range(node, context)
        if result == ConditionResult.TRUE:
            result = ConditionResult.TRUE if in_range else ConditionResult.FALSE
        return result, detail

    def _check_range_exit(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """range_exit: 值离开 [low, high] 区间。"""
        result, detail, in_range = self._check_range(node, context)
        if result == ConditionResult.TRUE:
            result = ConditionResult.FALSE if in_range else ConditionResult.TRUE
        return result, detail

    def _check_cross(
        self, node: ConditionNode, context: AlertContext, *, above: bool
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """cross_above/cross_below: 序列从一侧穿越参考线（metric_b 或固定阈值）。"""
        if node.metric and node.metric in context.stale_fields:
            return ConditionResult.UNKNOWN, {"reason": f"{node.metric} 数据不可用"}
        history = context.indicator_history.get(node.metric or "")
        if not history or len(history) < 2:
            return ConditionResult.UNKNOWN, {"reason": "历史序列不足（需≥2点）"}

        if node.metric_b:
            if node.metric_b in context.stale_fields:
                return ConditionResult.UNKNOWN, {
                    "reason": f"{node.metric_b} 数据不可用"
                }
            threshold = context.resolve_value(node.metric_b)
            if threshold is None:
                return ConditionResult.UNKNOWN, {"reason": f"{node.metric_b} 无可用值"}
        else:
            threshold = node.value

        if threshold is None:
            return ConditionResult.UNKNOWN, {"reason": "缺少穿越参考线"}

        prev_ts, prev = history[-2]
        curr_ts, curr = history[-1]
        if above:
            crossed = prev <= threshold < curr
        else:
            crossed = prev >= threshold > curr
        result = ConditionResult.TRUE if crossed else ConditionResult.FALSE
        return result, {
            "prev": prev,
            "prev_time": prev_ts.isoformat(),
            "current": curr,
            "current_time": curr_ts.isoformat(),
            "threshold": threshold,
            "reference": node.metric_b or "fixed",
        }

    def _check_cross_above(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """cross_above: 值从下方穿越参考线。"""
        return self._check_cross(node, context, above=True)

    def _check_cross_below(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """cross_below: 值从上方穿越参考线。"""
        return self._check_cross(node, context, above=False)

    def _check_percentile(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """percentile: 指标历史分位 vs 阈值（默认 ≥）。"""
        if node.metric and node.metric in context.stale_fields:
            return ConditionResult.UNKNOWN, {"reason": f"{node.metric} 数据不可用"}
        value = context.indicator_percentiles.get(node.metric or "")
        result = _cmp(value, node.operator or "gte", node.percentile)
        return result, {"value": value, "threshold": node.percentile}

    def _check_state_change(
        self, node: ConditionNode, context: AlertContext
    ) -> tuple[ConditionResult, dict[str, Any]]:
        """state_change: 引擎状态变化（可选限定 from_state / to_state）。"""
        engine = node.engine or ""
        field_key = f"engine.{engine}"
        if not engine or field_key in context.stale_fields:
            return ConditionResult.UNKNOWN, {"reason": f"引擎 {engine} 状态不可用"}
        current = context.engine_states.get(engine)
        prev = context.engine_prev_states.get(engine)
        if current is None or prev is None:
            return ConditionResult.UNKNOWN, {
                "reason": "引擎状态历史不足（需两期）",
                "current": current,
            }
        changed = current != prev
        if node.from_state is not None:
            changed = changed and prev == node.from_state
        if node.to_state is not None:
            changed = changed and current == node.to_state
        result = ConditionResult.TRUE if changed else ConditionResult.FALSE
        return result, {
            "engine": engine,
            "from": prev,
            "to": current,
            "expected_from": node.from_state,
            "expected_to": node.to_state,
        }
