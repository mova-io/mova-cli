"""Agent run replay — re-execute a recorded ``RunRecord`` against current code.

The high-leverage regression-debug tool: an engineer pastes a ``run_id``
(from a Slack thread, an eval baseline diff, a cost-spike alert) and
movate re-runs the *same input* through the *current* agent bundle. The
diff between recorded output and fresh output isolates whether a
behavior change comes from prompt edits, model swaps, schema tweaks, or
an upstream input that happened to drift.

Workflow replay is deliberately deferred. The 80% of debug requests are
single-agent regressions, and workflow replay needs node-by-node intermediate
state matching that's a follow-up land.

Exit-code semantics live in the CLI layer:

* output changed → still ``exit 0``. Surfacing the diff *is* the goal.
* current run errors → ``exit 1``. The agent regressed.
* run-id missing or agent-name mismatch → ``exit 2``. Operator error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from movate.core.executor import Executor
from movate.core.loader import AgentBundle
from movate.core.models import RunRecord, RunRequest, RunResponse
from movate.storage.base import StorageProvider


class ReplayMismatchError(Exception):
    """Raised when a replay can't proceed.

    Covers: unknown run_id, agent-name mismatch between bundle and
    record, or a record with no recorded input (shouldn't happen in
    v0.4+ but defensive against bad migrations).
    """


@dataclass
class AgentReplayDiff:
    """Side-by-side of a recorded run vs a fresh execution of the same input.

    The dataclass is intentionally narrow — diff math + JSON shape live
    on the :func:`render` helpers below so the dataclass stays a plain
    bag of facts.
    """

    original: RunRecord
    current: RunResponse

    @property
    def output_changed(self) -> bool:
        """True iff the response payload differs from the recorded output.

        On error runs we compare ``error.message`` instead — the original
        ``output`` is ``None`` and would always be "different" otherwise.
        """
        if self.original.status.value == "error" or self.current.status == "error":
            orig_msg = self.original.error.message if self.original.error else None
            cur_msg = self.current.error.message if self.current.error else None
            return orig_msg != cur_msg
        return self.original.output != self.current.data

    @property
    def cost_delta_usd(self) -> float:
        return round(self.current.metrics.cost_usd - self.original.metrics.cost_usd, 6)

    @property
    def latency_delta_ms(self) -> int:
        return self.current.metrics.latency_ms - self.original.metrics.latency_ms

    @property
    def status_changed(self) -> bool:
        return self.original.status.value != self.current.status

    @property
    def changed_keys(self) -> list[str]:
        """Keys whose top-level value differs between recorded and current.

        Returns an empty list when statuses differ or one side is None —
        in those cases the "diff" is the status itself, not a key set.
        Works on top-level keys only; nested diffs land if/when we add
        a richer ``--verbose`` view.
        """
        if self.original.output is None or self.current.status == "error":
            return []
        original = self.original.output
        current = self.current.data
        keys = set(original) | set(current)
        return sorted(k for k in keys if original.get(k) != current.get(k))


async def replay_agent_run(
    *,
    storage: StorageProvider,
    executor: Executor,
    bundle: AgentBundle,
    run_id: str,
    tenant_id: str = "local",
) -> AgentReplayDiff:
    """Look up ``run_id`` in storage and re-run its input through ``bundle``.

    The current agent's *bundle* is used end-to-end — the prompt
    template, model config, schemas, and pricing all come from disk
    (whatever the engineer last edited). Only the input is pinned.

    ``tenant_id`` defaults to ``"local"`` for local CLI use. Server-side
    callers must pass the authenticated tenant so cross-tenant
    ``run_id`` probes return ``ReplayMismatchError`` rather than
    leaking the existence of another tenant's run.
    """
    record = await storage.get_run(run_id, tenant_id=tenant_id)
    if record is None:
        raise ReplayMismatchError(
            f"no run found for id {run_id!r}; check `movate logs` or your storage path"
        )
    if bundle.spec.name != record.agent:
        raise ReplayMismatchError(
            f"agent mismatch: bundle is {bundle.spec.name!r}, recorded run was {record.agent!r}"
        )

    request = RunRequest(agent=bundle.spec.name, input=record.input)
    response = await executor.execute(bundle, request)
    return AgentReplayDiff(original=record, current=response)


# ---------------------------------------------------------------------------
# Renderers — JSON shape lives here so the CLI is just a thin pipe.
# ---------------------------------------------------------------------------


def render_replay_json(diff: AgentReplayDiff) -> dict[str, Any]:
    """Serialize an :class:`AgentReplayDiff` to a plain-dict JSON payload.

    Returned dict (not a JSON string) so the caller decides indent /
    output stream.
    """
    return {
        "run_id": diff.original.run_id,
        "agent": diff.original.agent,
        "agent_version_recorded": diff.original.agent_version,
        "agent_version_current": diff.current.metrics.provider,
        "input": diff.original.input,
        "recorded": {
            "status": diff.original.status.value,
            "output": diff.original.output,
            "error": diff.original.error.model_dump() if diff.original.error else None,
            "cost_usd": diff.original.metrics.cost_usd,
            "latency_ms": diff.original.metrics.latency_ms,
            "created_at": diff.original.created_at.isoformat(),
        },
        "current": {
            "status": diff.current.status,
            "output": diff.current.data,
            "error": diff.current.error.model_dump() if diff.current.error else None,
            "cost_usd": diff.current.metrics.cost_usd,
            "latency_ms": diff.current.metrics.latency_ms,
        },
        "diff": {
            "output_changed": diff.output_changed,
            "status_changed": diff.status_changed,
            "changed_keys": diff.changed_keys,
            "cost_delta_usd": diff.cost_delta_usd,
            "latency_delta_ms": diff.latency_delta_ms,
        },
    }


__all__ = [
    "AgentReplayDiff",
    "ReplayMismatchError",
    "render_replay_json",
    "replay_agent_run",
]
