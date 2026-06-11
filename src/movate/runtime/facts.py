"""Observability fact builders — project records into facts (ADR 096).

Pure mapping helpers: each builder flattens one authoritative record
(:class:`RunRecord` / :class:`WorkflowRunRecord`) into the denormalized
:class:`ObservabilityFact` row the mova-io platform reads. No I/O here —
the edge writers (``runtime/dispatch.py``; the Temporal persist/pause
activities for workflow facts and the agent/gate/judge activities for
per-node run facts) call :func:`write_fact_failsoft`, which wraps the one
storage call in the ADR 096 D3 contract: a fact-write failure logs a
warning and NEVER fails the run.

Facts are DERIVED (ADR 096 D4): ``fact_id = "<kind>:<source_id>"`` makes
every write an idempotent upsert, so re-deriving from the same record
(retry, pause→terminal transition, backfill) updates in place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from movate.core.models import ObservabilityFact, RunRecord, WorkflowRunRecord

if TYPE_CHECKING:
    from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)


def fact_from_run_record(
    record: RunRecord, *, governance_effect: str | None = None, runtime: str = "native"
) -> ObservabilityFact:
    """Flatten one agent :class:`RunRecord` into its fact row.

    The nested ``record.metrics`` blob (the thing the platform must never
    couple to — ADR 096 Context) is flattened into the stable scalar
    columns; provider / model / pricing_version land in ``attributes``
    (the bounded escape hatch). A record persisted before metrics were
    populated simply carries the ``Metrics`` defaults (zeros, empty
    trace_id) — never an error.

    ``governance_effect`` is supplied by the *edge* (the most severe effect
    a :func:`movate.governance.effects.governance_effect_scope` collected
    around the run) because ``runs`` stays untouched by design — the effect
    is a facts-only projection, never a column on the authoritative record.
    ``None`` ⇒ no gate evaluated (the column's honest NULL).

    ``runtime`` names the backend that owned the execution: the dispatch
    edge keeps the ``"native"`` default (byte-for-byte the prior behavior —
    rule 5, additive keyword); the Temporal activities pass ``"temporal"``.

    A workflow-spawned run (``record.workflow_run_id`` set) surfaces that id
    in ``attributes["workflow_run_id"]`` so readers can join per-node spend
    back to the parent ``workflow_run`` fact — the ADR 096 reader-side
    rollup ("summing is the reader's join"). Standalone runs omit the key.
    """
    metrics = record.metrics
    attributes: dict[str, object] = {
        "provider": record.provider,
        "pricing_version": record.pricing_version,
    }
    # The concrete model string lives on the per-turn records (the chosen
    # fallback may differ from the spec's preference); the last turn is the
    # one that produced the final answer. Legacy/no-turn records omit it.
    if record.turns:
        attributes["model"] = record.turns[-1].model
    if record.workflow_run_id:
        attributes["workflow_run_id"] = record.workflow_run_id
    return ObservabilityFact(
        fact_id=f"run:{record.run_id}",
        kind="run",
        source_id=record.run_id,
        trace_id=metrics.trace_id,
        tenant_id=record.tenant_id,
        workflow=None,
        agent=record.agent,
        node_id=record.node_id,
        status=record.status.value,
        runtime=runtime,
        route=None,
        cost_usd=metrics.cost_usd,
        tokens_in=metrics.tokens.input,
        tokens_out=metrics.tokens.output,
        latency_ms=metrics.latency_ms,
        governance_effect=governance_effect,
        error_type=record.error.type if record.error else None,
        created_at=record.created_at,
        attributes=attributes,
    )


def fact_from_workflow_run(
    record: WorkflowRunRecord, *, governance_effect: str | None = None
) -> ObservabilityFact:
    """Flatten one :class:`WorkflowRunRecord` into its fact row.

    ``route`` surfaces a decision/router outcome when the workflow state
    carries one under ``tier`` / ``route`` (ADR 094 decision nodes write
    there) — ``None`` otherwise. ``runtime`` mirrors the record's backend
    owner (``None`` ⇒ native, the ADR 062 D2 convention). Cost/token/
    latency stay at their zero defaults: workflow-level rollups are a
    deliberate non-goal here (the per-node ``run`` facts carry the spend;
    summing is the reader's join, not a second source of truth).

    ``governance_effect`` follows the same edge-supplied contract as
    :func:`fact_from_run_record`: ``workflow_runs`` stays untouched, the
    effect is a facts-only projection. ``None`` ⇒ no gate observed at this
    edge — the storage upsert keeps any effect a *different* edge already
    recorded for the same fact (NULL never overwrites non-NULL).
    """
    state = record.final_state or record.paused_state or {}
    raw_route = state.get("tier", state.get("route"))
    return ObservabilityFact(
        fact_id=f"workflow_run:{record.workflow_run_id}",
        kind="workflow_run",
        source_id=record.workflow_run_id,
        tenant_id=record.tenant_id,
        workflow=record.workflow,
        agent=None,
        node_id=record.paused_node_id or record.error_node_id,
        status=record.status.value,
        runtime=record.runtime or "native",
        route=str(raw_route) if raw_route is not None else None,
        governance_effect=governance_effect,
        error_type=record.error.type if record.error else None,
        created_at=record.created_at,
    )


async def write_fact_failsoft(storage: StorageProvider, fact: ObservabilityFact) -> None:
    """Persist one fact, fail-soft (ADR 096 D3).

    Same posture as the metrics the facts sit next to: the authoritative
    record is already persisted by the time this runs, so a fact-write
    failure (storage blip, missing migration) logs a warning and never
    raises — the run/workflow outcome is byte-for-byte unaffected.
    """
    try:
        await storage.save_observability_fact(fact)
    except Exception:
        logger.warning("observability_fact_write_failed fact_id=%s", fact.fact_id, exc_info=True)


__all__ = ["fact_from_run_record", "fact_from_workflow_run", "write_fact_failsoft"]
