"""Decision-chain serialization for a stored run.

The pure ``RunRecord -> dict`` logic behind ``mdk explain --json`` and the
read-only ``GET /api/v1/runs/{run_id}/explain`` endpoint. Lives in ``core``
so BOTH the control plane (:mod:`movate.cli.explain`) and the execution plane
(the runtime endpoint) import it — the runtime never imports from ``cli``.

The shape is the machine-readable decision chain: the run's identity +
status, its input, the single LLM-call summary (model, tokens, latency,
cost), the output (or error), and either the full per-step ``skill_calls``
(when ``steps=True``) or a one-line hint about how many there are.
"""

from __future__ import annotations

from typing import Any

from movate.core.models import RunRecord


def explain_run(record: RunRecord, *, steps: bool = False) -> dict[str, Any]:
    """Build the machine-readable decision chain for *record*.

    With ``steps=True`` the full per-skill-call breakdown is embedded under
    ``skill_calls``; otherwise a ``skill_calls_hint`` string summarises the
    count (mirroring ``mdk explain``'s default vs ``--steps`` behaviour).
    """
    m = record.metrics
    chain: dict[str, Any] = {
        "run_id": record.run_id,
        "agent": record.agent,
        "agent_version": record.agent_version,
        "status": record.status,
        "input": record.input,
        "llm_call": {
            "model": m.provider,
            "tokens_in": m.tokens.input,
            "tokens_out": m.tokens.output,
            "tokens_cached": m.tokens.cached_input,
            "latency_ms": m.latency_ms,
            "cost_usd": m.cost_usd,
        },
        "output": record.output,
        "error": record.error.model_dump() if record.error else None,
    }
    if steps:
        chain["skill_calls"] = [s.model_dump() for s in (record.skill_calls or [])]
    else:
        chain["skill_calls_hint"] = (
            f"{len(record.skill_calls)} skill call(s) — add --steps to include details"
            if record.skill_calls
            else "no skill calls (single-shot agent)"
        )
    return chain


__all__ = ["explain_run"]
