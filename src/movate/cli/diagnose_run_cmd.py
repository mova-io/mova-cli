"""``mdk diagnose run <workflow-run-id>`` — evidence-cited post-mortem for ONE run.

Answers "what happened and why" for a single workflow run, in two cleanly
separated phases:

**Phase A — deterministic evidence collectors** (pure data, no LLM). Each is
fail-soft and individually reported present/absent:

1. **Facts** — the terminal ``workflow_run`` fact + the per-node ``kind=run``
   facts, read over the runtime API (``GET /api/v1/observability/facts``,
   ADR 096). Same reader-side rollup the certification driver uses: run facts
   carry the correlation in ``attributes.workflow_run_id``, joined client-side.
2. **Temporal history** — reuses ``mdk workflow history``'s underlying fetch
   (:func:`movate.cli.workflow_cmd._fetch_history`, imported not subprocessed)
   and condenses it: close status, failure message/stack, per-activity attempt
   counts (**retry storms**), timer/signal events (HITL pauses), duration.
3. **Langfuse trace** — the run's ``trace_id`` deep-links to Langfuse. When the
   ``langfuse`` extra is installed AND keys are configured, the v3 SDK's read
   API (``Langfuse().api.trace.get``) fetches the trace's observations
   (model, truncated prompt/completion, token counts, errors). Otherwise the
   evidence is marked absent with the dashboard URL still cited
   (:func:`movate.tracing.langfuse_link.langfuse_trace_url`).
4. **Sim-ledger rows** (certification runs) — ``sim_side_effects`` rows via
   ``certification.harness.sim_systems`` (repo-level package, lazily imported;
   gated on ``MOVATE_PG_URL``/``MOVATE_DB_URL`` like the driver's side-effects
   capability).
5. **Workflow spec** — if ``workflows/<name>/workflow.yaml`` exists locally,
   its topology (node ids/types/routes) so the narrative can name the graph
   position where execution diverged.

The collectors also derive **deterministic signals** (retry storms, duplicate
side-effect rows, governance denials) so ``--no-llm`` output is genuinely
useful on its own.

**Phase B — one optional LLM call** over the structured evidence digest. Every
claim must cite evidence ids (E1..En); the prompt instructs the model to answer
"insufficient evidence" rather than speculate. ``--no-llm`` skips it; ``--json``
emits the full ``{evidence, diagnosis}`` object.

Surface: ``mdk diagnose <workflow-run-id>``. The existing ``mdk diagnose``
group (the agent failure-pattern diagnoser, ADR 043) dispatches here from its
callback when the positional argument is UUID-shaped — a workflow run id is
``str(uuid4())`` (ADR 054 D6), while agent names are human slugs — so the
established ``mdk diagnose <agent>`` contract is untouched. (A ``run``
subcommand is not possible: a Click group whose callback declares a positional
argument consumes the first token itself, so subcommands with positionals
never resolve.)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from movate.cli._console import error, get_global_target, hint
from movate.core.user_config import (
    UserConfigError,
    resolve_bearer_token,
    resolve_target,
)
from movate.tracing.langfuse_link import langfuse_trace_url

stdout = Console()
err = Console(stderr=True)

# Default model for the diagnosis pass — cheap, structured-output-reliable.
DEFAULT_DIAGNOSIS_MODEL = "openai/gpt-4o-mini"

# Truncation budgets. Prompts/completions from Langfuse are clipped hard so a
# verbose run can't blow the digest; the digest itself is bounded so the LLM
# call stays one cheap request.
_TEXT_TRUNC = 500
_STACK_TRUNC = 1500
_FAILURE_MSG_TRUNC = 300
_DIGEST_MAX_CHARS = 24_000
_MAX_OBSERVATIONS = 20
_MAX_LEDGER_ROWS = 50

# An activity reaching this many attempts is flagged as a retry storm.
_RETRY_STORM_ATTEMPTS = 3

# Temporal connect+fetch budget — an unreachable TEMPORAL_HOST must degrade to
# "evidence absent" quickly, not hang the diagnosis.
_TEMPORAL_FETCH_TIMEOUT_S = 20.0

_EVIDENCE_SOURCES = ("facts", "temporal_history", "langfuse", "sim_ledger", "workflow_spec")


# ---------------------------------------------------------------------------
# Target / credential resolution
# ---------------------------------------------------------------------------


def _default_dev_api_url() -> str | None:
    """The dev runtime URL the certification suite defaults to.

    Lazy: ``certification`` is repo-level (not in the wheel) — absent on an
    installed-only mdk, in which case only ``MDK_DEV_API_URL`` can supply it.
    """
    try:
        from certification.run_suite import DEFAULT_DEV_API_URL  # noqa: PLC0415

        return str(DEFAULT_DEV_API_URL)
    except ImportError:
        return None


def _resolve_api(target: str | None) -> tuple[str, str, str]:
    """Resolve ``(target_name, base_url, bearer_token)`` for the runtime API.

    Primary path: the user-config target registry (``resolve_target`` +
    ``resolve_bearer_token``) — same as the sibling ``mdk diagnose <agent>``.
    Fallback for the common ``--target dev`` case without a configured target:
    ``mdk certify``'s credential resolution (env ``MDK_DEV_KEY``, then
    ``~/.movate/credentials``) + ``MDK_DEV_API_URL`` / the suite's default.
    """
    name = target or get_global_target()
    try:
        target_name, cfg = resolve_target(name)
        return target_name, cfg.url, resolve_bearer_token(cfg)
    except UserConfigError as exc:
        if name in (None, "dev"):
            from movate.cli.certify_cmd import _resolve_dev_key  # noqa: PLC0415

            key = _resolve_dev_key()
            base = os.environ.get("MDK_DEV_API_URL", "").strip() or _default_dev_api_url()
            if key and base:
                return "dev", base, key
        error(str(exc), context="diagnose run")
        raise typer.Exit(code=2) from None


# ---------------------------------------------------------------------------
# Phase A — evidence collectors. Each returns {"present": bool, "detail": str,
# ...source-specific data} and NEVER raises (fail-soft, individually absent).
# ---------------------------------------------------------------------------


def _collect_facts(base_url: str, token: str, workflow_run_id: str) -> dict[str, Any]:
    """Terminal ``workflow_run`` fact + per-node ``run`` facts over the API.

    Mirrors the certification driver's ADR 096 reader-side rollup: the facts
    API has no ``workflow_run_id`` filter for ``kind=run``, so node facts join
    client-side on ``attributes.workflow_run_id``.
    """
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=30.0) as client:
            resp = client.get(
                "/api/v1/observability/facts",
                params={"kind": "workflow_run", "limit": 200},
            )
            resp.raise_for_status()
            wf_facts = [
                f for f in resp.json().get("facts", []) if f.get("source_id") == workflow_run_id
            ]
            resp = client.get("/api/v1/observability/facts", params={"kind": "run", "limit": 500})
            resp.raise_for_status()
            node_facts = [
                f
                for f in resp.json().get("facts", [])
                if (f.get("attributes") or {}).get("workflow_run_id") == workflow_run_id
            ]
    except httpx.HTTPError as exc:
        return {"present": False, "detail": f"facts API unreachable: {exc}"}
    terminal = next((f for f in wf_facts if f.get("status") != "paused"), None) or (
        wf_facts[0] if wf_facts else None
    )
    return {
        "present": True,
        "detail": f"{len(wf_facts)} workflow_run fact(s), {len(node_facts)} node run fact(s)",
        "terminal_fact": terminal,
        "node_facts": sorted(node_facts, key=lambda f: str(f.get("created_at", ""))),
    }


def _norm_event_type(evt: dict[str, Any]) -> str:
    """Normalize a Temporal event type to ``ACTIVITY_TASK_SCHEDULED`` form."""
    raw = str(evt.get("eventType", evt.get("event_type", "")))
    return raw.removeprefix("EVENT_TYPE_").upper()


_ACTIVITY_CLOSE_ATTR_KEYS = {
    "ACTIVITY_TASK_COMPLETED": "activityTaskCompletedEventAttributes",
    "ACTIVITY_TASK_FAILED": "activityTaskFailedEventAttributes",
    "ACTIVITY_TASK_TIMED_OUT": "activityTaskTimedOutEventAttributes",
}

_WORKFLOW_CLOSE_STATUSES = {
    "WORKFLOW_EXECUTION_COMPLETED": "completed",
    "WORKFLOW_EXECUTION_TIMED_OUT": "timed_out",
    "WORKFLOW_EXECUTION_CANCELED": "canceled",
    "WORKFLOW_EXECUTION_TERMINATED": "terminated",
}


def _apply_activity_event(
    etype: str, evt: dict[str, Any], activities: dict[str, dict[str, Any]]
) -> None:
    """Fold one ``ACTIVITY_TASK_*`` event into the per-activity rollup."""
    eid = str(evt.get("eventId", ""))
    ts = str(evt.get("eventTime", ""))
    if etype == "ACTIVITY_TASK_SCHEDULED":
        attrs = evt.get("activityTaskScheduledEventAttributes") or {}
        activities[eid] = {
            "activity_id": attrs.get("activityId"),
            "activity_type": (attrs.get("activityType") or {}).get("name"),
            "scheduled_at": ts,
            "attempts": 0,
            "failures": [],
            "outcome": "scheduled",
        }
    elif etype == "ACTIVITY_TASK_STARTED":
        attrs = evt.get("activityTaskStartedEventAttributes") or {}
        info = activities.get(str(attrs.get("scheduledEventId", "")))
        if info is not None:
            with contextlib.suppress(TypeError, ValueError):
                info["attempts"] = max(info["attempts"], int(attrs.get("attempt", 1) or 1))
            last_failure = (attrs.get("lastFailure") or {}).get("message")
            if last_failure:
                info["failures"].append(str(last_failure)[:_FAILURE_MSG_TRUNC])
    elif etype in _ACTIVITY_CLOSE_ATTR_KEYS:
        attrs = evt.get(_ACTIVITY_CLOSE_ATTR_KEYS[etype]) or {}
        info = activities.get(str(attrs.get("scheduledEventId", "")))
        if info is not None:
            info["outcome"] = etype.removeprefix("ACTIVITY_TASK_").lower()
            info["closed_at"] = ts
            msg = (attrs.get("failure") or {}).get("message")
            if msg:
                info["failures"].append(str(msg)[:_FAILURE_MSG_TRUNC])


def _apply_pause_event(
    etype: str,
    evt: dict[str, Any],
    timer_events: list[dict[str, Any]],
    signal_events: list[dict[str, Any]],
) -> None:
    """Fold one timer / signal event (the HITL-pause signal) into the rollup."""
    eid = str(evt.get("eventId", ""))
    ts = str(evt.get("eventTime", ""))
    if etype == "TIMER_STARTED":
        attrs = evt.get("timerStartedEventAttributes") or {}
        timer_events.append(
            {
                "event_id": eid,
                "started_at": ts,
                "timeout": attrs.get("startToFireTimeout"),
                "fired": False,
            }
        )
    elif etype == "TIMER_FIRED":
        attrs = evt.get("timerFiredEventAttributes") or {}
        started = str(attrs.get("startedEventId", ""))
        for timer in timer_events:
            if timer["event_id"] == started:
                timer["fired"] = True
                timer["fired_at"] = ts
    elif etype == "WORKFLOW_EXECUTION_SIGNALED":
        attrs = evt.get("workflowExecutionSignaledEventAttributes") or {}
        signal_events.append({"signal_name": attrs.get("signalName"), "at": ts})


def _summarize_history(history: dict[str, Any]) -> dict[str, Any]:
    """Condense a Temporal event history (proto-JSON form) into evidence.

    Extracts: close status + failure, per-activity attempt counts (the retry
    storm signal), timer/signal events (HITL pauses), and start/close times.
    Defensive throughout — a malformed event is skipped, never fatal.
    """
    events = history.get("events", []) or []
    activities: dict[str, dict[str, Any]] = {}  # scheduled eventId -> info
    timer_events: list[dict[str, Any]] = []
    signal_events: list[dict[str, Any]] = []
    status = "unknown"
    failure: dict[str, Any] | None = None

    for evt in events:
        etype = _norm_event_type(evt)
        if etype.startswith("ACTIVITY_TASK_"):
            _apply_activity_event(etype, evt, activities)
        elif etype.startswith("TIMER_") or etype == "WORKFLOW_EXECUTION_SIGNALED":
            _apply_pause_event(etype, evt, timer_events, signal_events)
        elif etype == "WORKFLOW_EXECUTION_FAILED":
            status = "failed"
            f = (evt.get("workflowExecutionFailedEventAttributes") or {}).get("failure") or {}
            failure = {
                "message": f.get("message"),
                "stack": str(f.get("stackTrace") or "")[:_STACK_TRUNC] or None,
                "cause": (f.get("cause") or {}).get("message"),
            }
        elif etype in _WORKFLOW_CLOSE_STATUSES:
            status = _WORKFLOW_CLOSE_STATUSES[etype]

    return {
        "event_count": len(events),
        "workflow_status": status,
        "workflow_failure": failure,
        "activities": list(activities.values()),
        "timer_events": timer_events,
        "signal_events": signal_events,
        "started_at": str(events[0].get("eventTime", "")) if events else None,
        "closed_at": str(events[-1].get("eventTime", "")) if events else None,
    }


def _collect_temporal_history(workflow_run_id: str) -> dict[str, Any]:
    """Fetch + condense the run's Temporal event history. Absent on any miss.

    Reuses ``mdk workflow history``'s fetch (import, not subprocess). The
    preconditions are checked here first so an absent [temporal] extra or
    unresolvable connection degrades silently to absent evidence instead of
    the command-style error print ``_fetch_history`` emits.
    """
    try:
        import temporalio  # noqa: F401, PLC0415
    except ImportError:
        return {"present": False, "detail": "[temporal] extra not installed"}
    from movate.runtime.workflow_backend import (  # noqa: PLC0415
        WorkflowBackendError,
        _resolve_temporal_connection,
    )

    try:
        _resolve_temporal_connection()
    except WorkflowBackendError as exc:
        return {"present": False, "detail": f"Temporal connection unresolved: {exc}"}

    from movate.cli.workflow_cmd import _fetch_history  # noqa: PLC0415

    async def _fetch() -> dict[str, Any]:
        return await asyncio.wait_for(
            _fetch_history(run_id=workflow_run_id, suppress=True),
            timeout=_TEMPORAL_FETCH_TIMEOUT_S,
        )

    try:
        history = asyncio.run(_fetch())
    except typer.Exit:
        return {"present": False, "detail": "Temporal not configured for this environment"}
    except TimeoutError:
        return {
            "present": False,
            "detail": f"Temporal unreachable (timed out after {_TEMPORAL_FETCH_TIMEOUT_S:.0f}s)",
        }
    except Exception as exc:
        return {"present": False, "detail": f"Temporal history fetch failed: {exc}"}
    summary = _summarize_history(history)
    return {
        "present": True,
        "detail": (f"{summary['event_count']} event(s), close status {summary['workflow_status']}"),
        **summary,
    }


def _trunc(value: Any, limit: int = _TEXT_TRUNC) -> str | None:
    """Stringify + clip a value for the digest; ``None`` passes through."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [+{len(text) - limit} chars truncated]"


def _collect_langfuse(trace_id: str | None) -> dict[str, Any]:
    """Fetch the run's Langfuse trace observations via the v3 SDK read API.

    The ``langfuse`` extra ships the Fern read client (``Langfuse().api``), so
    when keys are configured we can fetch real observations. Any miss (no
    trace_id, no keys, SDK absent, fetch error) is absent evidence — with the
    dashboard URL still cited when it can be constructed.
    """
    if not trace_id:
        return {
            "present": False,
            "detail": "no trace_id on the run's facts — tracing off or facts absent",
        }
    trace_url = langfuse_trace_url(trace_id)
    have_keys = bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        and os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    )
    if not have_keys:
        return {
            "present": False,
            "detail": "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY not set — cannot read the trace",
            "trace_url": trace_url,
        }
    try:
        from langfuse import Langfuse  # noqa: PLC0415
    except ImportError:
        return {
            "present": False,
            "detail": "langfuse extra not installed (uv sync --extra langfuse)",
            "trace_url": trace_url,
        }
    client = None
    try:
        client = Langfuse()
        trace = client.api.trace.get(trace_id)
        data: dict[str, Any] = trace.dict() if hasattr(trace, "dict") else dict(trace)
    except Exception as exc:
        return {
            "present": False,
            "detail": f"Langfuse trace fetch failed: {exc}",
            "trace_url": trace_url,
        }
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.shutdown()
    observations: list[dict[str, Any]] = []
    for obs in (data.get("observations") or [])[:_MAX_OBSERVATIONS]:
        usage = obs.get("usage") or {}
        observations.append(
            {
                "type": obs.get("type"),
                "name": obs.get("name"),
                "model": obs.get("model"),
                "level": obs.get("level"),
                "status_message": obs.get("statusMessage") or obs.get("status_message"),
                "input": _trunc(obs.get("input")),
                "output": _trunc(obs.get("output")),
                "tokens": {
                    "input": usage.get("input"),
                    "output": usage.get("output"),
                    "total": usage.get("total"),
                },
            }
        )
    return {
        "present": True,
        "detail": f"{len(observations)} observation(s)",
        "trace_url": trace_url,
        "observations": observations,
    }


def _collect_sim_ledger(workflow_run_id: str) -> dict[str, Any]:
    """``sim_side_effects`` rows for the run (certification runs only).

    Reuses the harness's row reader. ``certification`` is repo-level (not in
    the wheel) → lazy import; the read is gated on the same DSNs the driver's
    side-effects capability requires (the deployed runtime's shared Postgres).
    """
    try:
        from certification.harness import sim_systems  # noqa: PLC0415
        from certification.harness.driver import side_effects_db_configured  # noqa: PLC0415
    except ImportError:
        return {
            "present": False,
            "detail": "certification package not importable (run from a movate-cli checkout)",
        }
    if not side_effects_db_configured():
        return {
            "present": False,
            "detail": "MOVATE_PG_URL/MOVATE_DB_URL not set — sim-ledger DB unreachable from here",
        }
    try:
        rows = sim_systems.read(workflow_run_id)
    except Exception as exc:
        return {"present": False, "detail": f"sim-ledger read failed: {exc}"}
    return {"present": True, "detail": f"{len(rows)} side-effect row(s)", "rows": rows}


def _collect_workflow_spec(workflow_name: str | None) -> dict[str, Any]:
    """Topology of ``workflows/<name>/workflow.yaml`` when present locally."""
    if not workflow_name:
        return {"present": False, "detail": "workflow name unknown (no terminal fact)"}
    path = Path("workflows") / workflow_name / "workflow.yaml"
    if not path.is_file():
        return {"present": False, "detail": f"no local spec at {path}"}
    try:
        import yaml  # noqa: PLC0415

        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return {"present": False, "detail": f"could not parse {path}: {exc}"}
    nodes = [
        {
            "id": n.get("id"),
            "type": n.get("type"),
            **({"routes": n.get("routes")} if n.get("routes") else {}),
            **({"cases": n.get("cases")} if n.get("cases") else {}),
            **({"default": n.get("default")} if n.get("default") else {}),
            **({"fallback": n.get("fallback")} if n.get("fallback") else {}),
        }
        for n in (spec.get("nodes") or [])
        if isinstance(n, dict)
    ]
    return {
        "present": True,
        "detail": f"{len(nodes)} node(s) from {path}",
        "path": str(path),
        "runtime": spec.get("runtime"),
        "entrypoint": spec.get("entrypoint"),
        "nodes": nodes,
        "edges": spec.get("edges") or [],
    }


# ---------------------------------------------------------------------------
# Deterministic signals — the pre-LLM analysis (what makes --no-llm useful).
# ---------------------------------------------------------------------------


# Two or more byte-identical ledger rows = a re-executed side-effect.
_DUPLICATE_ROW_THRESHOLD = 2


def _facts_signals(facts: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    if not facts.get("present"):
        return signals
    terminal = facts.get("terminal_fact")
    if terminal:
        parts = [f"status={terminal.get('status')}"]
        for field in ("error_type", "route", "governance_effect"):
            if terminal.get(field):
                parts.append(f"{field}={terminal[field]}")
        signals.append(f"terminal fact: {' '.join(parts)}")
    error_nodes = [
        nf.get("node_id")
        for nf in facts.get("node_facts", [])
        if str(nf.get("status", "")).lower() == "error"
    ]
    if error_nodes:
        signals.append(f"node fact(s) with status=error: {', '.join(map(str, error_nodes))}")
    return signals


def _temporal_signals(temporal: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    if not temporal.get("present"):
        return signals
    for act in temporal.get("activities", []):
        attempts = int(act.get("attempts") or 0)
        label = f"activity {act.get('activity_type')!r} (id {act.get('activity_id')})"
        last_failure = (act.get("failures") or [None])[-1]
        suffix = f", last failure: {last_failure}" if last_failure else ""
        if attempts >= _RETRY_STORM_ATTEMPTS:
            signals.append(
                f"retry storm: {label} made {attempts} attempts, "
                f"outcome={act.get('outcome')}{suffix}"
            )
        elif act.get("outcome") == "failed":
            signals.append(
                f"failed activity: {label} outcome=failed after {attempts} attempt(s){suffix}"
            )
    if temporal.get("workflow_failure"):
        signals.append(
            "workflow closed FAILED on Temporal: "
            f"{(temporal['workflow_failure'] or {}).get('message')}"
        )
    n_timers = len(temporal.get("timer_events", []))
    n_signals = len(temporal.get("signal_events", []))
    if n_timers or n_signals:
        signals.append(
            f"{n_timers} timer(s) / {n_signals} signal(s) in history — "
            "HITL pause/resume or durable timers were involved"
        )
    return signals


def _ledger_signals(ledger: dict[str, Any]) -> list[str]:
    signals: list[str] = []
    if not ledger.get("present"):
        return signals
    counts: Counter[tuple[str, str, str]] = Counter(
        (
            str(r.get("system")),
            str(r.get("action")),
            json.dumps(r.get("payload"), sort_keys=True, default=str),
        )
        for r in ledger.get("rows", [])
    )
    for (system, action, _payload), n in counts.items():
        if n >= _DUPLICATE_ROW_THRESHOLD:
            signals.append(
                f"duplicate side-effects: {n} identical {system}.{action} ledger rows — "
                "a retried activity re-executed its side-effect"
            )
    return signals


def _derive_signals(sources: dict[str, dict[str, Any]]) -> list[str]:
    """Deterministic findings derived from the collected evidence.

    These encode the operational failure modes we have actually seen — e.g. a
    terminal ``temporal_workflow_error`` + N attempts of one activity + N
    identical ledger rows IS a retry storm re-executing a side-effect, and the
    diagnosis must name the failing activity rather than say "the workflow
    failed".
    """
    return (
        _facts_signals(sources.get("facts", {}))
        + _temporal_signals(sources.get("temporal_history", {}))
        + _ledger_signals(sources.get("sim_ledger", {}))
    )


# ---------------------------------------------------------------------------
# Evidence assembly — the cited digest both output modes share.
# ---------------------------------------------------------------------------


def _build_links(workflow_run_id: str, trace_id: str | None) -> dict[str, str | None]:
    """Client-side deep links (never stored — ADR 096 D2)."""
    from movate.cli.workflow_cmd import (  # noqa: PLC0415
        _resolve_temporal_ui_base,
        _temporal_web_url,
    )

    temporal_ui: str | None = None
    ui_base = _resolve_temporal_ui_base()
    if ui_base:
        namespace = "default"
        try:
            from movate.runtime.workflow_backend import (  # noqa: PLC0415
                _resolve_temporal_connection,
            )

            namespace = _resolve_temporal_connection().namespace
        except Exception:  # link is best-effort
            pass
        temporal_ui = _temporal_web_url(ui_base, namespace, workflow_run_id)
    return {"temporal_ui": temporal_ui, "langfuse_trace": langfuse_trace_url(trace_id)}


def _assemble_evidence(
    workflow_run_id: str,
    sources: dict[str, dict[str, Any]],
    signals: list[str],
    links: dict[str, str | None],
) -> dict[str, Any]:
    """Build the evidence object: per-source presence + cited items E1..En."""
    items: list[dict[str, Any]] = []

    def add(source: str, kind: str, summary: str, data: Any) -> None:
        items.append(
            {
                "id": f"E{len(items) + 1}",
                "source": source,
                "kind": kind,
                "summary": summary,
                "data": data,
            }
        )

    facts = sources["facts"]
    if facts.get("present"):
        terminal = facts.get("terminal_fact")
        if terminal:
            add(
                "facts",
                "terminal_workflow_run_fact",
                f"workflow={terminal.get('workflow')} status={terminal.get('status')} "
                f"error_type={terminal.get('error_type')} route={terminal.get('route')} "
                f"governance_effect={terminal.get('governance_effect')} "
                f"cost_usd={terminal.get('cost_usd')} latency_ms={terminal.get('latency_ms')} "
                f"trace_id={terminal.get('trace_id')} created_at={terminal.get('created_at')}",
                terminal,
            )
        for nf in facts.get("node_facts", []):
            add(
                "facts",
                "node_run_fact",
                f"node {nf.get('node_id')}: status={nf.get('status')} "
                f"agent={nf.get('agent')} cost_usd={nf.get('cost_usd')} "
                f"tokens={nf.get('tokens_in')}/{nf.get('tokens_out')} "
                f"latency_ms={nf.get('latency_ms')} created_at={nf.get('created_at')}",
                nf,
            )

    temporal = sources["temporal_history"]
    if temporal.get("present"):
        add(
            "temporal",
            "workflow_close",
            f"Temporal close status={temporal.get('workflow_status')} over "
            f"{temporal.get('event_count')} event(s), "
            f"{temporal.get('started_at')} → {temporal.get('closed_at')}",
            {
                "workflow_status": temporal.get("workflow_status"),
                "workflow_failure": temporal.get("workflow_failure"),
                "event_count": temporal.get("event_count"),
                "started_at": temporal.get("started_at"),
                "closed_at": temporal.get("closed_at"),
            },
        )
        for act in temporal.get("activities", []):
            add(
                "temporal",
                "activity",
                f"activity {act.get('activity_type')!r} (id {act.get('activity_id')}): "
                f"{act.get('attempts')} attempt(s), outcome={act.get('outcome')}",
                act,
            )
        if temporal.get("timer_events"):
            add(
                "temporal",
                "timers",
                f"{len(temporal['timer_events'])} timer event(s)",
                temporal["timer_events"],
            )
        if temporal.get("signal_events"):
            add(
                "temporal",
                "signals",
                f"{len(temporal['signal_events'])} signal event(s)",
                temporal["signal_events"],
            )

    langfuse = sources["langfuse"]
    if langfuse.get("present"):
        add(
            "langfuse",
            "trace",
            f"{len(langfuse.get('observations', []))} observation(s) fetched",
            {"trace_url": langfuse.get("trace_url")},
        )
        for obs in langfuse.get("observations", []):
            add(
                "langfuse",
                "observation",
                f"{obs.get('type')} {obs.get('name')!r} model={obs.get('model')} "
                f"level={obs.get('level')}",
                obs,
            )

    ledger = sources["sim_ledger"]
    if ledger.get("present"):
        rows = ledger.get("rows", [])
        add(
            "sim_ledger",
            "side_effects",
            f"{len(rows)} sim_side_effects row(s): "
            + ", ".join(f"{r.get('system')}.{r.get('action')}" for r in rows[:10]),
            rows[:_MAX_LEDGER_ROWS],
        )

    spec = sources["workflow_spec"]
    if spec.get("present"):
        add(
            "workflow_spec",
            "topology",
            f"entrypoint={spec.get('entrypoint')}, "
            f"nodes: {', '.join(str(n.get('id')) for n in spec.get('nodes', []))}",
            {
                "path": spec.get("path"),
                "runtime": spec.get("runtime"),
                "entrypoint": spec.get("entrypoint"),
                "nodes": spec.get("nodes"),
                "edges": spec.get("edges"),
            },
        )

    # Derived signals are evidence items too, so the diagnosis can cite them
    # directly (they are deterministic restatements of the items above).
    for signal in signals:
        add("derived", "signal", signal, None)

    return {
        "workflow_run_id": workflow_run_id,
        "sources": {
            name: {"present": bool(src.get("present")), "detail": src.get("detail", "")}
            for name, src in sources.items()
        },
        "items": items,
        "links": links,
    }


# ---------------------------------------------------------------------------
# Phase B — the one LLM call (optional, mocked in tests).
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an SRE writing a post-mortem for ONE workflow run of an AI-agent platform.
You are given an EVIDENCE DIGEST: a JSON object whose "items" array contains evidence
records, each with a unique id (E1, E2, ...).

Hard rules:
- Use ONLY the digest. Do not rely on outside knowledge of what "probably" happened.
- EVERY claim MUST cite the evidence ids it rests on, in square brackets,
  e.g. "the post-erp activity failed 3 times [E4,E7]".
- A claim you cannot tie to at least one evidence id must not be made. If the
  evidence is insufficient to determine something, write exactly
  "insufficient evidence" for that field and add what is missing to missing_evidence.
- Never invent node names, error messages, counts, or timestamps that are not in
  the digest.
- Some evidence sources may be listed as ABSENT (unreachable from the operator's
  machine while diagnosing, AFTER the run). Absence of a source is never a cause
  of the run's behavior and must not appear in summary/probable_root_cause — it
  belongs only in missing_evidence.
- Prefer the specific over the generic: name the failing node/activity and the
  failure mechanism (e.g. a retry storm) when the evidence shows one — never just
  "the workflow failed".

Respond with EXACTLY one JSON object (no markdown fences):
{
  "summary": "<2-4 sentences, every sentence cited>",
  "timeline": [{"at": "<timestamp or event order>", "what": "<cited event>", "evidence": ["E1"]}],
  "divergence_point": "<graph position / activity where execution left the happy path,
                       cited — or 'insufficient evidence'>",
  "probable_root_cause": "<cited — or 'insufficient evidence'>",
  "confidence": "high|medium|low",
  "missing_evidence": ["<absent evidence that would settle open questions>"]
}"""


def _build_user_prompt(workflow_run_id: str, evidence: dict[str, Any]) -> str:
    digest = json.dumps(evidence, indent=2, default=str)
    if len(digest) > _DIGEST_MAX_CHARS:
        digest = digest[:_DIGEST_MAX_CHARS] + "\n… [digest truncated]"
    absent = [
        f"- {name}: {src.get('detail', '')}"
        for name, src in evidence.get("sources", {}).items()
        if not src.get("present")
    ]
    absent_block = (
        "\n\nAbsent evidence sources — these describe what the DIAGNOSTIC TOOL could "
        "not reach from the operator's machine. They are NOT evidence about the run "
        "itself and MUST NOT be cited as a cause of the run's behavior; they only "
        "limit visibility (factor them into missing_evidence):\n" + "\n".join(absent)
        if absent
        else ""
    )
    return f"EVIDENCE DIGEST for workflow run {workflow_run_id}:\n{digest}{absent_block}"


def _run_llm(system_prompt: str, user_prompt: str, *, model: str) -> str:
    """One-shot diagnosis completion via the project's provider plumbing.

    Same minimal path as ``mdk keys test`` / ``mdk plan``: a direct
    ``LiteLLMProvider().complete``. Module-level so tests monkeypatch it.
    """
    from movate.providers.base import CompletionRequest, Message  # noqa: PLC0415
    from movate.providers.litellm import (  # noqa: PLC0415
        LiteLLMProvider,
        reset_logging_worker_for_new_event_loop,
    )

    reset_logging_worker_for_new_event_loop()
    request = CompletionRequest(
        provider=model,
        messages=[
            Message(role="system", content=system_prompt),
            Message(role="user", content=user_prompt),
        ],
        params={"temperature": 0.2, "max_tokens": 1800},
    )
    response = asyncio.run(LiteLLMProvider().complete(request))
    return response.text or ""


def _parse_diagnosis(raw: str) -> dict[str, Any] | None:
    """Parse the model's JSON object, tolerating markdown fences. None = unparseable."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_timeline(sources: dict[str, dict[str, Any]]) -> None:
    """The deterministic (collected) timeline: node facts + workflow open/close."""
    timeline_rows: list[tuple[str, str, str]] = []
    temporal = sources["temporal_history"]
    if temporal.get("present") and temporal.get("started_at"):
        timeline_rows.append((str(temporal["started_at"]), "temporal", "workflow started"))
    if sources["facts"].get("present"):
        for nf in sources["facts"].get("node_facts", []):
            timeline_rows.append(
                (
                    str(nf.get("created_at", "")),
                    "facts",
                    f"node {nf.get('node_id')}: {nf.get('status')} "
                    f"(cost=${nf.get('cost_usd', 0)}, {nf.get('latency_ms', 0)}ms)",
                )
            )
    if temporal.get("present") and temporal.get("closed_at"):
        timeline_rows.append(
            (
                str(temporal["closed_at"]),
                "temporal",
                f"workflow closed: {temporal.get('workflow_status')}",
            )
        )
    if not timeline_rows:
        return
    tl_table = Table(title="timeline (collected)", show_header=True)
    tl_table.add_column("at", style="dim")
    tl_table.add_column("source", style="dim")
    tl_table.add_column("what", overflow="fold")
    for at, source, what in sorted(timeline_rows, key=lambda r: r[0]):
        tl_table.add_row(at[:19], source, what)
    stdout.print(tl_table)


def _render_diagnosis(diagnosis: dict[str, Any] | None, *, no_llm: bool) -> None:
    if no_llm:
        hint("[dim]--no-llm: skipped the diagnosis pass — evidence report only.[/dim]")
        return
    if diagnosis is None or diagnosis.get("error"):
        detail = (diagnosis or {}).get("error", "no diagnosis produced")
        err.print(f"[yellow]![/yellow] diagnosis unavailable: {detail}")
        if diagnosis and diagnosis.get("raw"):
            err.print(f"[dim]{str(diagnosis['raw'])[:500]}[/dim]")
        return
    lines = [f"[bold]{diagnosis.get('summary', '')}[/bold]", ""]
    for entry in diagnosis.get("timeline", []) or []:
        cited = ",".join(entry.get("evidence", []) or [])
        lines.append(f"  {entry.get('at', '?')}  {entry.get('what', '')}  [dim][{cited}][/dim]")
    if diagnosis.get("timeline"):
        lines.append("")
    lines += [
        f"divergence point: {diagnosis.get('divergence_point', 'insufficient evidence')}",
        f"probable root cause: {diagnosis.get('probable_root_cause', 'insufficient evidence')}",
        f"confidence: {diagnosis.get('confidence', '?')}",
    ]
    missing = diagnosis.get("missing_evidence") or []
    if missing:
        lines.append("missing evidence:")
        lines += [f"  - {m}" for m in missing]
    stdout.print(Panel("\n".join(lines), title="diagnosis (claims cite E# evidence ids)"))


def _render_links(evidence: dict[str, Any], sources: dict[str, dict[str, Any]]) -> None:
    links = evidence.get("links", {})
    shown = {k: v for k, v in links.items() if v}
    if shown:
        stdout.print("\n[bold]links[/bold]")
        for name, url in shown.items():
            stdout.print(f"  {name}: [link={url}]{url}[/link]")
    # Even when the Langfuse READ was absent, cite the dashboard URL if we
    # could construct one (honest-absent: here's where the detail lives).
    lf_src = sources["langfuse"]
    if not lf_src.get("present") and lf_src.get("trace_url") and not links.get("langfuse_trace"):
        stdout.print(f"  langfuse_trace (detail-fetch absent): {lf_src['trace_url']}")


def _render_report(
    workflow_run_id: str,
    evidence: dict[str, Any],
    sources: dict[str, dict[str, Any]],
    signals: list[str],
    diagnosis: dict[str, Any] | None,
    *,
    no_llm: bool,
) -> None:
    facts = sources["facts"]
    terminal = (facts.get("terminal_fact") or {}) if facts.get("present") else {}
    header_bits = [f"run [bold]{workflow_run_id}[/bold]"]
    if terminal.get("workflow"):
        header_bits.append(f"workflow={terminal['workflow']}")
    if terminal.get("status"):
        header_bits.append(f"status={terminal['status']}")
    stdout.print(Panel.fit("  ".join(header_bits), title="mdk diagnose run"))

    ev_table = Table(title="evidence collected", show_header=True)
    ev_table.add_column("source")
    ev_table.add_column("status", justify="center")
    ev_table.add_column("detail", overflow="fold")
    for name in _EVIDENCE_SOURCES:
        src = sources.get(name, {})
        mark = "[green]✓[/green]" if src.get("present") else "[red]✗[/red]"
        ev_table.add_row(name, mark, str(src.get("detail", "")))
    stdout.print(ev_table)

    if signals:
        stdout.print("\n[bold]signals[/bold] [dim](deterministic, pre-LLM)[/dim]")
        for signal in signals:
            stdout.print(f"  • {signal}")

    _render_timeline(sources)
    _render_diagnosis(diagnosis, no_llm=no_llm)
    _render_links(evidence, sources)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run_postmortem(
    workflow_run_id: str,
    *,
    target: str | None = None,
    no_llm: bool = False,
    json_output: bool = False,
    model: str | None = None,
) -> None:
    """Evidence-cited post-mortem for ONE workflow run.

    Collects deterministic evidence from the observability surfaces — the
    facts API (terminal + per-node facts), Temporal event history (retry
    storms, HITL pauses, failure stacks), the Langfuse trace, the
    certification sim-ledger, and the local workflow spec — each fail-soft
    and reported present/absent. Then (unless ``no_llm``) runs ONE LLM pass
    over the evidence digest; every claim must cite evidence ids (E1..En)
    and insufficient evidence is stated, never guessed.

    Invoked from the ``mdk diagnose`` callback when the positional argument
    is UUID-shaped (a workflow run id is ``str(uuid4())`` — ADR 054 D6: it
    doubles as the Temporal workflow id). It is a plain function, not a
    Typer subcommand, because a Click group whose callback declares a
    positional argument cannot also route positionals to subcommands.
    """
    model = model or DEFAULT_DIAGNOSIS_MODEL
    target_name, base_url, token = _resolve_api(target)

    sources: dict[str, dict[str, Any]] = {}
    sources["facts"] = _collect_facts(base_url, token, workflow_run_id)
    terminal = sources["facts"].get("terminal_fact") if sources["facts"].get("present") else None
    node_facts = sources["facts"].get("node_facts", []) if sources["facts"].get("present") else []
    trace_id = (terminal or {}).get("trace_id") or next(
        (nf.get("trace_id") for nf in node_facts if nf.get("trace_id")), None
    )
    workflow_name = (terminal or {}).get("workflow")

    sources["temporal_history"] = _collect_temporal_history(workflow_run_id)
    sources["langfuse"] = _collect_langfuse(trace_id)
    sources["sim_ledger"] = _collect_sim_ledger(workflow_run_id)
    sources["workflow_spec"] = _collect_workflow_spec(workflow_name)

    # Unknown run id: the facts API answered and has NOTHING for this id, and
    # Temporal has no history for it either — a positive "no such run".
    facts_present = bool(sources["facts"].get("present"))
    facts_empty = facts_present and not terminal and not node_facts
    if facts_empty and not sources["temporal_history"].get("present"):
        error(
            f"unknown workflow run id {workflow_run_id!r}: the facts API on "
            f"{target_name!r} has no workflow_run/run fact for it, and no Temporal "
            "history was found. Check the id (mdk workflow runs lists recent runs).",
            context="diagnose run",
        )
        raise typer.Exit(code=2)

    if not any(src.get("present") for src in sources.values()):
        error(
            "no evidence source is reachable (facts API, Temporal, Langfuse, "
            "sim-ledger, local spec all absent) — cannot diagnose. Check the "
            "--target and your network/credentials.",
            context="diagnose run",
        )
        raise typer.Exit(code=1)

    signals = _derive_signals(sources)
    links = _build_links(workflow_run_id, trace_id)
    evidence = _assemble_evidence(workflow_run_id, sources, signals, links)

    diagnosis: dict[str, Any] | None = None
    if not no_llm:
        user_prompt = _build_user_prompt(workflow_run_id, evidence)
        try:
            raw = _run_llm(_SYSTEM_PROMPT, user_prompt, model=model)
        except Exception as exc:  # degrade to evidence-only, never crash
            diagnosis = {"error": f"LLM call failed ({type(exc).__name__}): {exc}"}
        else:
            parsed = _parse_diagnosis(raw)
            diagnosis = (
                parsed
                if parsed is not None
                else {"error": "LLM returned non-JSON output", "raw": raw[:2000]}
            )
        diagnosis["links"] = links

    if json_output:
        stdout.print(
            json.dumps(
                {
                    "workflow_run_id": workflow_run_id,
                    "evidence": evidence,
                    "diagnosis": diagnosis,
                },
                indent=2,
                default=str,
            ),
            soft_wrap=True,
            highlight=False,
        )
        return

    _render_report(workflow_run_id, evidence, sources, signals, diagnosis, no_llm=no_llm)


__all__ = ["run_postmortem"]
