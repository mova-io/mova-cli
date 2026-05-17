"""Safe arithmetic evaluator for the `calculator` skill.

Uses Python's ``ast`` module to parse expressions — never ``eval()``.
Supports +, -, *, /, unary minus, parentheses, and integer/float
literals. Any other construct (function calls, names, subscripts) is
rejected with a clear error message.
"""

from __future__ import annotations

import ast
import operator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from movate.core.skill_backend import SkillExecutionContext

_OPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _eval_node(node: ast.expr, steps: list[str]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op_fn = _OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        left = _eval_node(node.left, steps)
        right = _eval_node(node.right, steps)
        result = op_fn(left, right)
        steps.append(f"{left} {type(node.op).__name__} {right} = {result}")
        return result
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        val = _eval_node(node.operand, steps)
        result = -val
        steps.append(f"-({val}) = {result}")
        return result
    raise ValueError(f"unsupported expression node: {type(node).__name__}")


async def run(input: dict[str, Any], ctx: SkillExecutionContext) -> dict[str, Any]:
    expression = str(input["expression"]).strip()
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        return {"result": 0.0, "steps": [f"parse error: {exc}"]}

    steps: list[str] = []
    try:
        result = _eval_node(tree.body, steps)
    except (ValueError, ZeroDivisionError) as exc:
        return {"result": 0.0, "steps": [str(exc)]}

    if not steps:
        steps = [f"{expression} = {result}"]

    return {"result": result, "steps": steps}
