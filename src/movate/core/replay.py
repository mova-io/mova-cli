"""Trace replay — reconstruct an agent or workflow execution from local storage.

The high-leverage post-mortem tool: a developer pastes a ``run_id`` (or
``workflow_run_id``) from a Slack thread / Langfuse URL / cost report and
gets the full timeline of what happened, what each node saw, and what
came back. No need to keep Langfuse open.

This module is *pure* — it reads from a :class:`StorageProvider` and
returns rendered strings. CLI integration lives in
:mod:`movate.cli.trace`.

ID resolution
-------------

A v0.4 ``run_id`` and ``workflow_run_id`` are both UUIDs (no shape
distinction), so :func:`load_replay` tries the cheaper agent-run path
first and falls back to workflow lookup. Returns whichever matches; if
both somehow match (UUID collision — astronomically unlikely), the run
record wins because that's what most users paste.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from movate.core.models import RunRecord, WorkflowRunRecord
from movate.storage.base import StorageProvider


class ReplayNotFoundError(Exception):
    """Raised when neither a run nor a workflow_run matches the supplied id."""


@dataclass
class Replay:
    """A reconstructed view of either an agent run or a workflow run.

    Exactly one of ``run`` / ``workflow`` is populated. ``children`` is
    the per-node ``RunRecord`` list when ``workflow`` is set; empty for
    a single-agent replay.
    """

    kind: str  # "agent" or "workflow"
    run: RunRecord | None = None
    workflow: WorkflowRunRecord | None = None
    children: list[RunRecord] | None = None

    @property
    def total_cost_usd(self) -> float:
        if self.children:
            return round(sum(r.metrics.cost_usd for r in self.children), 6)
        if self.run:
            return self.run.metrics.cost_usd
        return 0.0

    @property
    def total_latency_ms(self) -> int:
        if self.children:
            return sum(r.metrics.latency_ms for r in self.children)
        if self.run:
            return self.run.metrics.latency_ms
        return 0


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


async def load_replay(
    storage: StorageProvider, identifier: str, *, tenant_id: str = "local"
) -> Replay:
    """Resolve ``identifier`` to a :class:`Replay`. Tries run then workflow.

    Raises :class:`ReplayNotFoundError` if neither matches.

    ``tenant_id`` defaults to ``"local"`` (the tenant the local CLI
    Executor stamps on every run via ``cli/_runtime.py``). A server-side
    caller — once we expose trace replay over HTTP — must pass the
    authenticated tenant so cross-tenant id probes return
    ``ReplayNotFoundError`` rather than someone else's run.
    """
    run = await storage.get_run(identifier, tenant_id=tenant_id)
    if run is not None:
        return Replay(kind="agent", run=run)

    wf = await storage.get_workflow_run(identifier, tenant_id=tenant_id)
    if wf is not None:
        children = await storage.list_runs(
            workflow_run_id=identifier, tenant_id=tenant_id, limit=1000
        )
        # Sort by created_at ascending for chronological order.
        children_sorted = sorted(children, key=lambda r: r.created_at)
        return Replay(kind="workflow", workflow=wf, children=children_sorted)

    raise ReplayNotFoundError(
        f"no run or workflow_run found for id {identifier!r}; "
        f"check `movate logs` or your storage path"
    )


# ---------------------------------------------------------------------------
# Renderers — text (Rich-friendly) and json
# ---------------------------------------------------------------------------


def render_replay_json(replay: Replay) -> str:
    """Single-document JSON dump suitable for piping or diffing."""
    if replay.kind == "agent":
        assert replay.run is not None
        payload: dict[str, object] = {
            "kind": "agent",
            "run": _run_to_dict(replay.run),
        }
    else:
        assert replay.workflow is not None
        payload = {
            "kind": "workflow",
            "workflow": _workflow_to_dict(replay.workflow),
            "nodes": [_run_to_dict(r) for r in (replay.children or [])],
            "total_cost_usd": replay.total_cost_usd,
            "total_latency_ms": replay.total_latency_ms,
        }
    return json.dumps(payload, indent=2, default=str)


def _run_to_dict(r: RunRecord) -> dict[str, object]:
    return {
        "run_id": r.run_id,
        "workflow_run_id": r.workflow_run_id,
        "node_id": r.node_id,
        "agent": r.agent,
        "agent_version": r.agent_version,
        "provider": r.provider,
        "status": r.status.value,
        "input": r.input,
        "output": r.output,
        "error": r.error.model_dump() if r.error else None,
        "metrics": {
            "latency_ms": r.metrics.latency_ms,
            "cost_usd": r.metrics.cost_usd,
            "tokens": r.metrics.tokens.model_dump(),
            "pricing_version": r.metrics.pricing_version,
        },
        "prompt_hash": r.prompt_hash,
        "created_at": r.created_at.isoformat(),
    }


def _workflow_to_dict(w: WorkflowRunRecord) -> dict[str, object]:
    return {
        "workflow_run_id": w.workflow_run_id,
        "workflow": w.workflow,
        "workflow_version": w.workflow_version,
        "status": w.status.value,
        "initial_state": w.initial_state,
        "final_state": w.final_state,
        "error_node_id": w.error_node_id,
        "error": w.error.model_dump() if w.error else None,
        "created_at": w.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Truncation helper — small but used in two places, lives here so the CLI
# doesn't reach into the storage layer just for it.
# ---------------------------------------------------------------------------


def truncate(value: object, *, max_chars: int = 200) -> str:
    """Render ``value`` as a single-line string and truncate with an ellipsis.

    Dicts / lists are JSON-encoded; everything else falls through ``str()``.
    The ``--verbose`` CLI flag bypasses this and prints full bodies.
    """
    if isinstance(value, (dict, list)):
        s = json.dumps(value, default=str)
    elif value is None:
        return "—"
    else:
        s = str(value)
    if len(s) <= max_chars:
        return s
    return s[: max_chars - 1] + "…"
