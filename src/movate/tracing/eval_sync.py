"""Edge glue: push eval results + datasets to Langfuse (ADR 031 D1).

This module lives in the tracing layer and is called from the *edges*
(``mdk eval`` in the CLI, the eval-dispatch path in the runtime) — never from
``core`` execution logic. Every function here is **best-effort and a no-op
when Langfuse isn't wired**: it reaches the Langfuse-specific extensions
(:meth:`LangfuseTracer.score_eval_summary` / ``sync_dataset``) by ``getattr``
on whatever ``Tracer`` the edge already built, so a Silent / Stdout / OTel
tracer simply has no such method and nothing happens. A raising Langfuse client
degrades to a logged warning; an eval never fails because Langfuse is down.

The functions take *duck-typed* inputs (objects exposing the attributes we
read) so this module doesn't import ``core`` types — keeping the dependency
arrow pointing inward (tracing → nothing in core).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger("movate.tracing.eval_sync")


def _pick_eval_trace_id(summary: Any) -> str | None:
    """Find a representative trace id to attach run-level eval scores to.

    Walks the summary's cases (newest cases last in the list) and returns the
    last non-empty ``response.metrics.trace_id`` (or ``response.trace_id``) —
    a real trace from this eval run that the aggregate scores can hang off so
    they render in Langfuse. ``None`` when tracing was off for every case
    (SilentTracer → empty trace ids) → caller no-ops.
    """
    trace_id: str | None = None
    for case in getattr(summary, "cases", []) or []:
        for run in getattr(case, "runs", []) or []:
            response = getattr(run, "response", None)
            if response is None:
                continue
            metrics = getattr(response, "metrics", None)
            candidate = (getattr(metrics, "trace_id", "") if metrics else "") or getattr(
                response, "trace_id", ""
            )
            if candidate:
                trace_id = candidate
    return trace_id


async def push_eval_scores(
    tracer: Any,
    summary: Any,
    *,
    drift_deltas: dict[str, float] | None = None,
) -> None:
    """Push an eval run's pass-rate + per-dimension means (+ drift) as scores.

    No-op unless ``tracer`` exposes ``score_eval_summary`` (i.e. it's a
    Langfuse tracer, or a composite containing one) AND the run produced a
    real trace id. Best-effort: any error is logged and swallowed so the eval
    outcome is unchanged.

    ``summary`` is duck-typed: we read ``.pass_rate`` / ``.mean_score`` and
    ``.dimensional_means.as_dict()`` (an :class:`EvalSummary`). ``drift_deltas``
    is the optional ``{metric: current - baseline}`` map from the drift path.
    """
    score_fn = getattr(tracer, "score_eval_summary", None)
    if not callable(score_fn):
        return  # non-Langfuse tracer → nothing to do
    trace_id = _pick_eval_trace_id(summary)
    if not trace_id:
        return  # tracing was off → no trace to attach to
    dim_means_obj = getattr(summary, "dimensional_means", None)
    as_dict = getattr(dim_means_obj, "as_dict", None)
    dimension_means: dict[str, float] = as_dict() if callable(as_dict) else {}
    try:
        await score_fn(
            trace_id=trace_id,
            pass_rate=float(getattr(summary, "pass_rate", 0.0)),
            mean_score=float(getattr(summary, "mean_score", 0.0)),
            dimension_means=dimension_means,
            drift_deltas=drift_deltas or None,
        )
    except Exception:
        logger.warning("langfuse eval-score push failed (eval result unchanged)", exc_info=True)


def _stable_case_id(agent: str, input_obj: Any, index: int) -> str:
    """Deterministic dataset-item id so re-sync upserts rather than dupes.

    Keyed on the agent name + the case input + its ordinal so two identical
    inputs in one dataset don't collide. Stable across runs → Langfuse treats
    the second sync as an update of the same item (idempotent)."""
    import json  # noqa: PLC0415

    try:
        payload = json.dumps(input_obj, sort_keys=True, default=str)
    except Exception:
        payload = repr(input_obj)
    digest = hashlib.sha256(f"{agent}\n{index}\n{payload}".encode()).hexdigest()
    return f"{agent}-{digest[:16]}"


def build_dataset_items(agent: str, cases: list[Any]) -> list[dict[str, Any]]:
    """Project eval ``cases`` into Langfuse dataset-item dicts.

    Each ``case`` is duck-typed (an :class:`EvalCase`): we read ``.input``,
    ``.expected`` and ``.objective`` / ``.tags`` for metadata. The item id is
    deterministic (see :func:`_stable_case_id`) so re-syncing the same dataset
    is idempotent.
    """
    items: list[dict[str, Any]] = []
    for index, case in enumerate(cases):
        input_obj = getattr(case, "input", {})
        expected = getattr(case, "expected", None)
        metadata: dict[str, Any] = {}
        objective = getattr(case, "objective", None)
        if objective:
            metadata["objective"] = objective
        tags = getattr(case, "tags", None)
        if tags:
            metadata["tags"] = list(tags)
        items.append(
            {
                "id": _stable_case_id(agent, input_obj, index),
                "input": input_obj,
                "expected_output": expected if expected else None,
                "metadata": metadata or None,
            }
        )
    return items


async def sync_eval_dataset(
    tracer: Any,
    *,
    agent: str,
    cases: list[Any],
) -> int:
    """Sync an agent's eval ``cases`` to a Langfuse dataset (idempotent).

    Dataset name is ``mdk-eval-<agent>``. No-op (returns 0) unless ``tracer``
    exposes ``sync_dataset`` (Langfuse, or a composite containing it) and
    there are cases to sync. Best-effort: errors are logged and swallowed.
    """
    sync_fn = getattr(tracer, "sync_dataset", None)
    if not callable(sync_fn) or not cases:
        return 0
    items = build_dataset_items(agent, cases)
    try:
        result = await sync_fn(
            name=f"mdk-eval-{agent}",
            items=items,
            description=f"mdk eval dataset for agent {agent!r}",
        )
        return int(result) if isinstance(result, int) else 0
    except Exception:
        logger.warning("langfuse dataset sync failed (eval result unchanged)", exc_info=True)
        return 0
