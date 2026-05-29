"""NL query + troubleshoot over the insights store (ADR 047).

Two grounded, citation-bearing entry points:

* ``ask(question, ...)`` — answers from the append-only insights store (the
  fast path) and, when more detail is needed, runs ONE of a FIXED set of
  read-only, parameterized query templates over storage.
* ``troubleshoot(symptom, time_window, ...)`` — correlates recent failure
  clusters + anomalies + insights into a root-cause narrative.

SQL-SAFETY CONTRACT (the critical invariant of this module)
-----------------------------------------------------------
The detail path NEVER lets the LLM author SQL that we execute, and NEVER runs
an unbounded query. Instead:

1. We expose a CLOSED, named registry of read-only query "templates"
   (:data:`QUERY_TEMPLATES`). Each template is a Python function that calls
   *typed* :class:`StorageProvider` Protocol methods (e.g. ``list_runs``) with
   a hard row cap — there is NO raw-SQL string anywhere in this module, so
   there is nothing for an attacker (or a hallucinating model) to inject into.
2. The LLM's only influence on the detail path is to PICK a template name from
   the closed set and FILL its typed parameters (window, agent, …). Unknown
   names and out-of-range params are rejected before anything runs. This is
   text-to-PARAMETERIZED-QUERY, not text-to-arbitrary-SQL.
3. There is deliberately no ``execute_sql`` / ``raw_query`` helper. A test
   (``tests/test_observability_query.py``) asserts no such symbol exists.

Because every template goes through the Protocol's existing tenant-scoped,
row-capped read methods, the detail path is mutation-proof and bounded by
construction — the storage layer enforces both, not a string check.

Boundary discipline: depends only on the Protocols + core models.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from movate.core.models import RunRecord
from movate.core.observability.models import (
    Evidence,
    EvidenceKind,
    GroundedAnswer,
    ObservabilityInsight,
)
from movate.providers.base import BaseLLMProvider, CompletionRequest, Message
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Hard caps applied to EVERY template, independent of LLM-supplied params, so a
# detail query is bounded by construction (the SQL-safety contract's row cap).
MAX_ROWS = 500
MAX_WINDOW_DAYS = 90
_ANSWER_MAX_OUTPUT_TOKENS = 700


# ---------------------------------------------------------------------------
# Typed, read-only query templates (the CLOSED set — SQL-safety contract)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateResult:
    """The bounded result of running one named template."""

    name: str
    params: dict[str, Any]
    rows: list[dict[str, Any]]
    summary: str


def _window_bounds(window_days: int) -> tuple[datetime, datetime]:
    """Clamp + resolve ``window_days`` into a [start, now) UTC pair."""
    days = max(1, min(int(window_days), MAX_WINDOW_DAYS))
    now = datetime.now(UTC)
    return now - timedelta(days=days), now


def _coerce_utc(ts: datetime | None) -> datetime | None:
    if ts is None:
        return None
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _runs_in_window(runs: list[RunRecord], start: datetime, end: datetime) -> list[RunRecord]:
    out: list[RunRecord] = []
    for r in runs:
        ts = _coerce_utc(r.created_at)
        if ts is not None and start <= ts < end:
            out.append(r)
    return out


async def _tpl_cost_by_agent(
    storage: StorageProvider, *, tenant_id: str, window: int = 7, **_: Any
) -> TemplateResult:
    """Total cost + run count per agent over ``window`` days (read-only)."""
    start, end = _window_bounds(window)
    runs = _runs_in_window(await storage.list_runs(tenant_id=tenant_id, limit=MAX_ROWS), start, end)
    buckets: dict[str, dict[str, float]] = {}
    for r in runs:
        b = buckets.setdefault(r.agent or "(unknown)", {"runs": 0.0, "cost_usd": 0.0})
        b["runs"] += 1
        b["cost_usd"] += float(r.metrics.cost_usd or 0.0)
    rows = [
        {"agent": k, "runs": int(v["runs"]), "cost_usd": round(v["cost_usd"], 6)}
        for k, v in sorted(buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    ]
    return TemplateResult(
        name="cost_by_agent",
        params={"window": window},
        rows=rows,
        summary=f"cost by agent over {window}d ({len(runs)} runs)",
    )


async def _tpl_failed_runs(
    storage: StorageProvider,
    *,
    tenant_id: str,
    window: int = 7,
    agent: str | None = None,
    **_: Any,
) -> TemplateResult:
    """Failed/errored runs over ``window`` days, optionally for one agent."""
    start, end = _window_bounds(window)
    runs = _runs_in_window(
        await storage.list_runs(tenant_id=tenant_id, agent=agent, limit=MAX_ROWS), start, end
    )
    failed = [r for r in runs if r.status.value in ("error", "dead_letter", "safety_blocked")]
    rows = [
        {
            "run_id": r.run_id,
            "agent": r.agent,
            "status": r.status.value,
            "error_type": (r.error.type if r.error else ""),
            "created_at": (r.created_at.isoformat() if r.created_at else ""),
        }
        for r in failed[:MAX_ROWS]
    ]
    return TemplateResult(
        name="failed_runs",
        params={"window": window, "agent": agent},
        rows=rows,
        summary=f"{len(failed)} failed run(s) over {window}d"
        + (f" for agent {agent}" if agent else ""),
    )


async def _tpl_latency_percentiles(
    storage: StorageProvider,
    *,
    tenant_id: str,
    window: int = 7,
    agent: str | None = None,
    **_: Any,
) -> TemplateResult:
    """p50/p95/p99 latency (ms) over ``window`` days, optionally per agent."""
    import math  # noqa: PLC0415

    start, end = _window_bounds(window)
    runs = _runs_in_window(
        await storage.list_runs(tenant_id=tenant_id, agent=agent, limit=MAX_ROWS), start, end
    )
    lat = sorted(int(r.metrics.latency_ms or 0) for r in runs)

    def pct(p: int) -> float:
        if not lat:
            return 0.0
        rank = min(max(math.ceil((p / 100) * len(lat)), 1), len(lat))
        return float(lat[rank - 1])

    rows = [
        {
            "count": len(lat),
            "p50_ms": pct(50),
            "p95_ms": pct(95),
            "p99_ms": pct(99),
        }
    ]
    return TemplateResult(
        name="latency_percentiles",
        params={"window": window, "agent": agent},
        rows=rows,
        summary=f"latency percentiles over {window}d ({len(lat)} runs)",
    )


async def _tpl_usage_by_provider(
    storage: StorageProvider, *, tenant_id: str, window: int = 7, **_: Any
) -> TemplateResult:
    """Run count + cost per provider over ``window`` days (read-only)."""
    start, end = _window_bounds(window)
    runs = _runs_in_window(await storage.list_runs(tenant_id=tenant_id, limit=MAX_ROWS), start, end)
    buckets: dict[str, dict[str, float]] = {}
    for r in runs:
        b = buckets.setdefault(r.provider or "(unknown)", {"runs": 0.0, "cost_usd": 0.0})
        b["runs"] += 1
        b["cost_usd"] += float(r.metrics.cost_usd or 0.0)
    rows = [
        {"provider": k, "runs": int(v["runs"]), "cost_usd": round(v["cost_usd"], 6)}
        for k, v in sorted(buckets.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)
    ]
    return TemplateResult(
        name="usage_by_provider",
        params={"window": window},
        rows=rows,
        summary=f"usage by provider over {window}d ({len(runs)} runs)",
    )


# The CLOSED registry. The LLM may only pick a key from THIS dict; there is no
# code path that turns model output into raw SQL. Adding a template is a
# deliberate, reviewed change to this dict — never a runtime / LLM decision.
QUERY_TEMPLATES: dict[str, Callable[..., Awaitable[TemplateResult]]] = {
    "cost_by_agent": _tpl_cost_by_agent,
    "failed_runs": _tpl_failed_runs,
    "latency_percentiles": _tpl_latency_percentiles,
    "usage_by_provider": _tpl_usage_by_provider,
}


def _coerce_params(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + clamp LLM-supplied template params to typed, bounded values.

    Only ``window`` (int, clamped to [1, MAX_WINDOW_DAYS]) and ``agent`` (str
    or None) are accepted; anything else is dropped. This is the typed-param
    gate of the SQL-safety contract — params never reach a string-formatted
    query, only typed Protocol-method kwargs.
    """
    out: dict[str, Any] = {}
    if "window" in raw:
        try:
            out["window"] = max(1, min(int(raw["window"]), MAX_WINDOW_DAYS))
        except (TypeError, ValueError):
            out["window"] = 7
    agent = raw.get("agent")
    if isinstance(agent, str) and agent.strip():
        out["agent"] = agent.strip()
    return out


async def run_template(
    name: str, params: dict[str, Any], *, storage: StorageProvider, tenant_id: str
) -> TemplateResult | None:
    """Run one named template with validated params, or ``None`` if unknown.

    The single choke point: a name not in :data:`QUERY_TEMPLATES` returns
    ``None`` (no execution), and params pass through :func:`_coerce_params`
    first. Tenant scoping is forced server-side via the Protocol method's
    ``tenant_id`` kwarg.
    """
    fn = QUERY_TEMPLATES.get(name)
    if fn is None:
        logger.info("observability_unknown_template name=%s — ignored", name)
        return None
    safe = _coerce_params(params or {})
    return await fn(storage, tenant_id=tenant_id, **safe)


# ---------------------------------------------------------------------------
# Fast path: read the insights store
# ---------------------------------------------------------------------------


async def _recent_insights(
    storage: StorageProvider,
    *,
    tenant_id: str,
    project_id: str,
    days: int = 30,
) -> list[ObservabilityInsight]:
    until = datetime.now(UTC).date()
    since = until - timedelta(days=max(1, min(days, MAX_WINDOW_DAYS)))
    try:
        return await storage.list_insights(
            tenant_id, project_id=project_id, since=since, until=until, limit=days
        )
    except Exception:
        logger.warning("observability_insights_read_failed", exc_info=True)
        return []


def _insight_evidence(insight: ObservabilityInsight) -> Evidence:
    return Evidence(
        kind=EvidenceKind.INSIGHT,
        reference=insight.date.isoformat(),
        detail=(
            f"health {insight.health_score:.0f}, "
            f"{insight.usage_rollup.get('runs', 0)} runs, "
            f"${float(insight.usage_rollup.get('cost_usd', 0.0)):.4f} cost, "
            f"{len(insight.anomalies)} anomaly(ies)"
        ),
        data={
            "health_score": insight.health_score,
            "usage_rollup": insight.usage_rollup,
            "anomalies": insight.anomalies,
        },
    )


# ---------------------------------------------------------------------------
# LLM grounding (planning + synthesis)
# ---------------------------------------------------------------------------

_PLAN_SYSTEM = (
    "You route an observability question to AT MOST 2 read-only query "
    "templates. Reply with ONLY a JSON object: "
    '{"templates": [{"name": "<one of: %s>", "params": {"window": <int days>, '
    '"agent": "<optional>"}}]}. Pick templates whose data answers the '
    "question. If the daily insight summary already suffices, return an empty "
    "list. Never invent template names."
)

_ANSWER_SYSTEM = (
    "You answer an observability question from PROVIDED telemetry only. "
    "Ground every claim in the data given. Be concise. Reply with ONLY a JSON "
    'object: {"answer": "...", "confidence": <0..1>, "suggested_action": "..."}. '
    "Set confidence low if the data is thin or absent. Do NOT invent numbers."
)


async def _plan_templates(
    *, llm: BaseLLMProvider | None, model: str, question: str
) -> list[dict[str, Any]]:
    """Ask the LLM to PICK templates + params (closed set). Empty on failure."""
    if llm is None:
        return []
    system = _PLAN_SYSTEM % ", ".join(sorted(QUERY_TEMPLATES))
    request = CompletionRequest(
        provider=model,
        messages=[Message(role="system", content=system), Message(role="user", content=question)],
        params={"max_tokens": 200, "temperature": 0.0},
    )
    try:
        response = await llm.complete(request)
        parsed = _parse_json(response.text)
        templates = parsed.get("templates", []) if isinstance(parsed, dict) else []
        return [t for t in templates if isinstance(t, dict) and t.get("name") in QUERY_TEMPLATES][
            :2
        ]
    except Exception:
        logger.warning("observability_plan_failed — answering from insights only", exc_info=True)
        return []


def _parse_json(text: str) -> Any:
    """Best-effort JSON parse tolerant of ```json fences / surrounding prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```", 2)[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip().strip("`").strip()
    start, stop = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and stop != -1 and stop > start:
        cleaned = cleaned[start : stop + 1]
    return json.loads(cleaned)


@dataclass
class _Synthesis:
    answer: str
    confidence: float
    suggested_action: str
    cost_usd: float


def _estimate_cost(model: str, response: Any) -> float:
    try:
        from movate.providers.pricing import load_pricing  # noqa: PLC0415

        return float(load_pricing().cost_for(provider=model, tokens=response.tokens))
    except Exception:
        return 0.0


async def _synthesize(
    *,
    llm: BaseLLMProvider | None,
    model: str,
    question: str,
    context: dict[str, Any],
    budget_usd: float,
    fallback_answer: str,
) -> _Synthesis:
    """Run the answer-synthesis LLM call, budget-capped. Falls back cleanly."""
    if llm is None or budget_usd <= 0:
        return _Synthesis(answer=fallback_answer, confidence=0.3, suggested_action="", cost_usd=0.0)
    request = CompletionRequest(
        provider=model,
        messages=[
            Message(role="system", content=_ANSWER_SYSTEM),
            Message(
                role="user",
                content=json.dumps({"question": question, "telemetry": context}, default=str),
            ),
        ],
        params={"max_tokens": _ANSWER_MAX_OUTPUT_TOKENS, "temperature": 0.2},
    )
    try:
        response = await llm.complete(request)
    except Exception:
        logger.warning(
            "observability_synthesis_failed — returning structured fallback", exc_info=True
        )
        return _Synthesis(answer=fallback_answer, confidence=0.3, suggested_action="", cost_usd=0.0)

    cost = _estimate_cost(model, response)
    if cost > budget_usd:
        logger.warning(
            "observability_answer_over_budget cost=%.4f budget=%.4f model=%s",
            cost,
            budget_usd,
            model,
        )
    parsed = {}
    try:
        parsed = _parse_json(response.text)
    except Exception:
        parsed = {}
    return _Synthesis(
        answer=str(parsed.get("answer") or response.text.strip() or fallback_answer),
        confidence=float(parsed.get("confidence", 0.5) or 0.5),
        suggested_action=str(parsed.get("suggested_action", "")),
        cost_usd=cost,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def ask(
    question: str,
    *,
    tenant_id: str,
    project_id: str,
    storage: StorageProvider,
    llm: BaseLLMProvider | None = None,
    model: str = "openai/gpt-4o-mini",
    budget_usd: float = 0.05,
) -> GroundedAnswer:
    """Answer ``question`` from the insights store (+ bounded detail templates).

    Fast path: read recent insights. Detail path: the LLM PICKS up to two
    read-only templates from the closed set (:data:`QUERY_TEMPLATES`); we run
    them with row caps + tenant scoping and feed the rows back for synthesis.
    Every answer carries ``evidence[]`` citing its sources (insight dates +
    template names/params). Empty insights → a low-confidence fallback that
    still cites what it looked at.
    """
    insights = await _recent_insights(storage, tenant_id=tenant_id, project_id=project_id)
    evidence: list[Evidence] = [_insight_evidence(insights[0])] if insights else []

    plan = await _plan_templates(llm=llm, model=model, question=question)
    template_rows: list[dict[str, Any]] = []
    for spec in plan:
        result = await run_template(
            spec["name"], spec.get("params", {}), storage=storage, tenant_id=tenant_id
        )
        if result is None:
            continue
        evidence.append(
            Evidence(
                kind=EvidenceKind.QUERY,
                reference=result.name,
                detail=result.summary,
                data={"params": result.params, "rows": result.rows[:50]},
            )
        )
        template_rows.append(
            {"template": result.name, "summary": result.summary, "rows": result.rows[:50]}
        )

    context = {
        "latest_insight": insights[0].model_dump(mode="json") if insights else None,
        "insight_count": len(insights),
        "templates": template_rows,
    }
    fallback = _fallback_answer(insights, template_rows)
    synth = await _synthesize(
        llm=llm,
        model=model,
        question=question,
        context=context,
        budget_usd=budget_usd,
        fallback_answer=fallback,
    )
    # Thin data → cap confidence so callers don't over-trust a guess.
    confidence = synth.confidence if (insights or template_rows) else min(synth.confidence, 0.25)
    return GroundedAnswer(
        answer=synth.answer,
        evidence=evidence,
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        suggested_action=synth.suggested_action,
        cost_usd=synth.cost_usd,
    )


async def troubleshoot(
    symptom: str,
    time_window: int = 7,
    *,
    tenant_id: str,
    project_id: str,
    storage: StorageProvider,
    llm: BaseLLMProvider | None = None,
    model: str = "openai/gpt-4o-mini",
    budget_usd: float = 0.05,
) -> GroundedAnswer:
    """Correlate failures + anomalies + insights into a root-cause narrative.

    Pulls recent insights (for anomalies + failure clusters already computed)
    and runs the ``failed_runs`` template over ``time_window`` days, then asks
    the LLM to correlate them into a likely root cause with a remediation. Each
    contributing signal becomes an :class:`Evidence` citation (failure
    clusters, anomalies, the failed-runs query, insight dates).
    """
    window = max(1, min(int(time_window), MAX_WINDOW_DAYS))
    insights = await _recent_insights(
        storage, tenant_id=tenant_id, project_id=project_id, days=window
    )
    evidence: list[Evidence] = []

    # Anomalies + failure clusters from the latest insight (already computed).
    if insights:
        latest = insights[0]
        evidence.append(_insight_evidence(latest))
        for cluster in latest.top_failures[:3]:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.FAILURE,
                    reference=str(cluster.get("signature", "(unknown)")),
                    detail=f"{cluster.get('count', 0)} occurrence(s): "
                    f"{str(cluster.get('sample_message', ''))[:120]}",
                    data=cluster,
                )
            )
        for anom in latest.anomalies[:3]:
            evidence.append(
                Evidence(
                    kind=EvidenceKind.EVENT,
                    reference=str(anom.get("metric", "(metric)")),
                    detail=str(anom.get("note", "")),
                    data=anom,
                )
            )

    failed = await run_template(
        "failed_runs", {"window": window}, storage=storage, tenant_id=tenant_id
    )
    if failed is not None and failed.rows:
        evidence.append(
            Evidence(
                kind=EvidenceKind.QUERY,
                reference=failed.name,
                detail=failed.summary,
                data={"params": failed.params, "rows": failed.rows[:50]},
            )
        )

    context = {
        "symptom": symptom,
        "window_days": window,
        "latest_insight": insights[0].model_dump(mode="json") if insights else None,
        "failed_runs": (failed.rows[:50] if failed else []),
    }
    fallback = _troubleshoot_fallback(symptom, insights, failed)
    synth = await _synthesize(
        llm=llm,
        model=model,
        question=f"Troubleshoot this symptom and give a likely root cause: {symptom}",
        context=context,
        budget_usd=budget_usd,
        fallback_answer=fallback,
    )
    has_signal = bool(insights or (failed and failed.rows))
    confidence = synth.confidence if has_signal else min(synth.confidence, 0.25)
    return GroundedAnswer(
        answer=synth.answer,
        evidence=evidence,
        confidence=round(max(0.0, min(1.0, confidence)), 2),
        suggested_action=synth.suggested_action,
        cost_usd=synth.cost_usd,
    )


def _fallback_answer(
    insights: list[ObservabilityInsight], template_rows: list[dict[str, Any]]
) -> str:
    """Deterministic answer used when no LLM is configured / synthesis fails."""
    if not insights and not template_rows:
        return (
            "No observability insights are available yet for this project. Run the "
            "overnight analyst (mdk observability analyze) to populate the store."
        )
    parts: list[str] = []
    if insights:
        latest = insights[0]
        parts.append(
            f"Latest insight ({latest.date.isoformat()}): health {latest.health_score:.0f}/100, "
            f"{latest.usage_rollup.get('runs', 0)} runs, "
            f"${float(latest.usage_rollup.get('cost_usd', 0.0)):.4f} cost."
        )
        if latest.narrative_digest:
            parts.append(latest.narrative_digest)
    for tr in template_rows:
        parts.append(f"{tr['summary']}.")
    return " ".join(parts)


def _troubleshoot_fallback(
    symptom: str, insights: list[ObservabilityInsight], failed: TemplateResult | None
) -> str:
    parts = [f"Symptom: {symptom}."]
    if failed and failed.rows:
        parts.append(f"{len(failed.rows)} failed run(s) found in the window.")
    if insights and insights[0].top_failures:
        top = insights[0].top_failures[0]
        parts.append(
            f"Top failure cluster: {top.get('signature')} ({top.get('count', 0)} occurrences)."
        )
    if len(parts) == 1:
        parts.append("No correlated failures or anomalies found in the recent window.")
    return " ".join(parts)


__all__ = [
    "MAX_ROWS",
    "MAX_WINDOW_DAYS",
    "QUERY_TEMPLATES",
    "TemplateResult",
    "ask",
    "run_template",
    "troubleshoot",
]
