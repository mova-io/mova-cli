"""Shared DECISION-node evaluation (ADR 094).

A DECISION workflow node routes purely on a **deterministic comparison over
workflow state** — no LLM, no activity. It is the deterministic twin of the
``intent-router`` node: where intent-router calls a classifier agent
(``call_gate_activity`` on Temporal), a decision node compares a state field to a
literal with a closed-allowlist operator and picks the first matching route.

Because the logic is pure (no time/random/IO, no model call), it runs **inline**
on both backends. To guarantee the native runner and the Temporal-compiled
workflow reach byte-identical routing decisions — including the messy
``"5000"``-vs-``5000`` and missing-field cases — BOTH funnel through the one
:func:`evaluate_decision` here. The Temporal workflow imports this module through
``workflow.unsafe.imports_passed_through()``; that is safe precisely because this
module is dependency-free and side-effect-free. This mirrors :mod:`judge` as the
single semantic helper shared across backends (ADR 056 D2/D3): one shape, one
rule, no backend invents its own.

Rule surface (ADR 094 D1). A node carries an ordered list of ``cases`` and a
``default``::

    cases:
      - when: {field: amount, op: gt, value: 5000}
        to: director-approval
      - when: {field: amount, op: gt, value: 0}
        to: manager-approval
    default: auto-approve

First matching case wins; ``default`` when none match. ``field`` is a dotted path
into state (``expense.amount``). ``op`` is one of the closed allowlist below.

Semantics (ADR 094 D3, fail-soft + deterministic):

* Ordered comparisons (``gt/gte/lt/lte``) attempt numeric coercion of both sides
  (so ``"5000" > 0`` works); if either side can't coerce, the case is a
  **non-match** (fall through) — never an exception that wedges the workflow.
* A **missing field** is a non-match for comparisons/membership, and falsy for
  ``truthy``/``falsy``.
* ``in`` ⇒ ``field_value in value`` (``value`` is the collection); ``not_in`` is
  its negation; ``contains`` ⇒ ``value in field_value`` (``field`` is the
  collection/string). ``truthy``/``falsy`` test the field's Python truthiness and
  ignore ``value``.
"""

from __future__ import annotations

from typing import Any

# Closed operator allowlist (ADR 094 D1). The spec layer also pins ``op`` to this
# set via a ``Literal`` so an unknown operator fails at parse time; ``_apply_op``
# raising here is defense-in-depth for any caller bypassing the spec.
_OPS = frozenset(
    {"gt", "gte", "lt", "lte", "eq", "ne", "in", "not_in", "contains", "truthy", "falsy"}
)

# Sentinel for "field absent from state" — distinct from a stored ``None`` so a
# present ``None`` can still compare/equality-match while a truly missing key
# always falls through.
_MISSING = object()


def _read_field(state: dict[str, Any], dotted: str) -> Any:
    """Walk a dotted path (``expense.amount``) into ``state``; ``_MISSING`` if any
    segment is absent or a non-mapping is traversed."""
    cur: Any = state
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return _MISSING
    return cur


def _as_number(value: Any) -> float | None:
    """Best-effort numeric coercion for ordered comparisons; ``None`` if not
    numeric. Booleans are intentionally rejected (``True`` is not an amount)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _apply_op(op: str, left: Any, right: Any) -> bool:  # noqa: PLR0912 — flat operator dispatch
    """Evaluate one ``op`` against ``left`` (the state field) and ``right`` (the
    case's ``value``). Pure + total: returns a bool, never raises for in-allowlist
    operators (a type mismatch is a non-match, not an error)."""
    if op not in _OPS:
        raise ValueError(f"unknown decision operator {op!r} (allowed: {sorted(_OPS)})")

    if op == "truthy":
        return left is not _MISSING and bool(left)
    if op == "falsy":
        return left is _MISSING or not bool(left)

    # All remaining ops need a present field.
    if left is _MISSING:
        return False

    if op in ("gt", "gte", "lt", "lte"):
        ln, rn = _as_number(left), _as_number(right)
        if ln is None or rn is None:
            return False
        if op == "gt":
            return ln > rn
        if op == "gte":
            return ln >= rn
        if op == "lt":
            return ln < rn
        return ln <= rn  # lte

    if op == "eq":
        return bool(left == right)
    if op == "ne":
        return bool(left != right)

    if op in ("in", "not_in"):
        try:
            is_in = left in right  # right is the collection
        except TypeError:
            return op == "not_in"  # uniterable RHS ⇒ "not in" is vacuously true
        return is_in if op == "in" else not is_in

    # contains: field is the collection/string, value is the needle.
    try:
        return right in left
    except TypeError:
        return False


def _evaluate_decision_traced(
    cases: list[dict[str, Any]],
    default: str,
    state: dict[str, Any],
) -> tuple[str, int | None]:
    """Return ``(chosen_node_id, matched_case_index)``; index is ``None`` when the
    ``default`` route is taken. The index feeds the ``workflow.decision`` span so
    the branch is observable. First matching case wins."""
    for idx, case in enumerate(cases):
        cond = case.get("when", {})
        left = _read_field(state, str(cond.get("field", "")))
        if _apply_op(str(cond.get("op", "")), left, cond.get("value")):
            return str(case.get("to", default)), idx
    return default, None


def evaluate_decision(
    cases: list[dict[str, Any]],
    default: str,
    state: dict[str, Any],
) -> str:
    """Pick the route for a decision node: the ``to`` of the first matching case,
    else ``default``. The single source of truth shared by the native runner and
    the Temporal-compiled workflow (ADR 094 D3)."""
    return _evaluate_decision_traced(cases, default, state)[0]
