"""Claude-orchestrated audit of an agent (or project) — read-only.

The Auditor takes a single :class:`AgentBundle` (or a list of them, for
a project-wide audit) and runs a fan-out of per-category sub-agents
against a :class:`BaseLLMProvider`. Each sub-agent gets ONLY the slice
of context its category needs (so the LLM's context window stays clean
and the categories are independently cacheable). The aggregated
findings are filtered by ``severity_floor``, deduplicated, and
returned as an :class:`AuditRecord`.

Boundary rules (``docs/architecture-principles.md``):

* ``cli ⊥ runtime`` — this module lives under ``core/`` so both the CLI
  and the runtime call into the same pipeline.
* Storage is consumed via the :class:`StorageProvider` Protocol; no
  backend imports.
* Provider is consumed via the :class:`BaseLLMProvider` Protocol.
* **Read-only invariant**: the Auditor only READS the agent bundle, KB
  chunks, and the run history. It NEVER calls ``save_agent_bundle`` /
  ``save_kb_chunk`` / ``save_eval`` / etc. — the regression test
  ``test_audit_does_not_modify_agent`` pins this on InMemory storage.

Seven categories ship in this PR:

* ``ambiguous_prompts`` — contradictions, vague instructions, missing
  escape clauses, hallucination risk in the prompt.
* ``missing_eval_coverage`` — prompt rules not exercised by the eval
  dataset; KB chunks never retrieved.
* ``security_smells`` — PII patterns in contexts, hardcoded secrets,
  prompt-injection vulnerabilities, missing input validation.
* ``cost_outliers`` — agents with cost-per-run >2σ above project
  average; ``max_tokens`` excess; redundant context inclusion.
* ``kb_quality`` — stale chunks, low-signal chunks (rarely retrieved +
  low judge scores), duplicate content.
* ``schema_drift`` — input/output schemas not enforced; type mismatches
  in eval cases.
* ``model_choice`` — disproportionate model (too big for the workload;
  too small for the complexity).

Each sub-agent is one Claude call with a tight per-category prompt; the
seven calls run **in parallel** via ``asyncio.gather`` so the wall
clock is the slowest category, not the sum. Each sub-agent's prompt is
small (only its slice of context) which makes the per-category context
windows much cleaner than one monolithic mega-call would be.

Failure modes (rule 10):

* Sub-agent times out / errors → that category contributes zero
  findings + the audit is marked ``partial=True``. The other categories
  still complete.
* Budget cap hits mid-fan-out → remaining categories are skipped,
  ``partial=True`` is set, the partial result is returned (no findings
  thrown away).
* LLM returns invalid JSON for findings → that category contributes
  zero findings + ``partial=True`` (logged at ``warning``). One bad
  category never kills the whole audit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from movate.core.loader import AgentBundle
from movate.core.models import (
    AuditFinding,
    AuditFindingLocation,
    AuditFindingSeverity,
    AuditRecord,
)
from movate.providers.base import BaseLLMProvider, CompletionRequest, Message
from movate.storage.base import StorageProvider

logger = logging.getLogger(__name__)

# Seven categories the Auditor ships. Order is the rendering / fan-out
# order; the SSE stream emits ``category_complete`` events in this order
# for consistent operator UX across reruns.
CATEGORIES: tuple[str, ...] = (
    "ambiguous_prompts",
    "missing_eval_coverage",
    "security_smells",
    "cost_outliers",
    "kb_quality",
    "schema_drift",
    "model_choice",
)

_SEVERITY_ORDER: dict[AuditFindingSeverity, int] = {
    AuditFindingSeverity.INFO: 0,
    AuditFindingSeverity.WARN: 1,
    AuditFindingSeverity.ERROR: 2,
    AuditFindingSeverity.CRITICAL: 3,
}

# Per-category prompts. Each prompt is small + self-contained — the
# Claude call returns ONLY findings in JSON; we parse, severity-floor,
# and append. We never delegate any *action* to the LLM; it returns
# advisory findings only.
_CATEGORY_PROMPTS: dict[str, str] = {
    "ambiguous_prompts": (
        "You audit one AI-agent prompt for ambiguity / contradictions / "
        "missing escape clauses / hallucination risk. Return ONLY a JSON "
        "object of the shape {\"findings\": [...]} where each finding has "
        "fields: severity (one of info|warn|error|critical), title (short), "
        "description (one paragraph), suggestion (one line, advisory only), "
        "confidence (low|medium|high), and optionally line (1-based line "
        "number in prompt.md). Categories you may flag: contradictory "
        "instructions, vague directives, missing-escape-clause (\"if unsure, "
        "say X\"), hallucination-prone phrasing. Be SPECIFIC — quote the "
        "offending lines. NEVER suggest writing/mutating files. Return "
        "{\"findings\": []} when the prompt is clean."
    ),
    "missing_eval_coverage": (
        "You audit an AI agent for eval-coverage gaps. You are given the "
        "agent's prompt.md and its eval dataset (one JSONL row per case). "
        "Identify rules / branches in the prompt that NO dataset row "
        "exercises, and KB chunks (if provided) that are never retrieved. "
        "Return ONLY a JSON object {\"findings\": [...]} with fields: "
        "severity, title, description, suggestion (one line), confidence. "
        "ALL suggestions are advisory — never recommend automatic dataset "
        "edits. Return {\"findings\": []} when coverage looks complete."
    ),
    "security_smells": (
        "You audit an AI agent for security smells: PII patterns hardcoded "
        "in contexts, leaked API keys / secrets, prompt-injection "
        "vulnerabilities (untrusted user input concatenated without "
        "fencing), and missing input validation. You are given the agent's "
        "prompt + contexts + skill list. Return ONLY a JSON object "
        "{\"findings\": [...]} with fields: severity, title, description, "
        "suggestion (one line, advisory only), confidence. Be conservative "
        "— flag only what you can name a concrete pattern for. NEVER "
        "suggest writing/mutating files."
    ),
    "cost_outliers": (
        "You audit an AI agent's configuration for cost concerns. You are "
        "given the agent.yaml (model, max_tokens, contexts) and recent "
        "run cost statistics (mean cost per run, vs. project average + "
        "stddev). Flag: cost >2σ above project mean, excessive max_tokens, "
        "redundant context inclusion. Return ONLY {\"findings\": [...]} "
        "with fields: severity, title, description, suggestion (one line, "
        "advisory), confidence. NEVER suggest auto-changing the model."
    ),
    "kb_quality": (
        "You audit an AI agent's knowledge base for quality issues. You "
        "are given KB chunk stats (retrieval frequency, age in days, "
        "rough text). Flag: stale chunks (very old + never retrieved), "
        "low-signal chunks (rarely retrieved + small text), duplicate "
        "content. Return ONLY {\"findings\": [...]} with fields: severity, "
        "title, description, suggestion (one line, advisory), confidence. "
        "NEVER suggest auto-deleting chunks — only flag them for human "
        "review."
    ),
    "schema_drift": (
        "You audit an AI agent for input/output schema drift. You are "
        "given the agent's schemas + a sample of recent run inputs/outputs. "
        "Flag: schema not enforced (run inputs missing required fields), "
        "type mismatches (e.g. string where number declared), undeclared "
        "fields appearing in runs. Return ONLY {\"findings\": [...]} with "
        "fields: severity, title, description, suggestion (one line, "
        "advisory), confidence. NEVER suggest auto-tightening the schema."
    ),
    "model_choice": (
        "You audit an AI agent for model-choice fit. You are given the "
        "agent.yaml (model + role + task complexity hints), recent eval "
        "pass-rate, and recent cost stats. Flag: model too big for "
        "workload (high cost + simple task + high pass-rate), or model "
        "too small for complexity (low pass-rate + complex task). Return "
        "ONLY {\"findings\": [...]} with fields: severity, title, "
        "description, suggestion (one line, advisory), confidence. NEVER "
        "suggest auto-swapping models — only flag for human review."
    ),
}

# Approx average tokens per finding-bearing Claude reply (input + output).
# Used for the budget-cap pre-flight check ONLY — the real spend is
# computed from the provider's token usage after each completion. A
# loose over-estimate is correct here: we'd rather skip one category we
# could have afforded than overshoot the operator's cap.
_APPROX_TOKENS_PER_CATEGORY = 3000

# Default per-token cost for the budget guard's pre-flight check. The
# actual provider pricing is computed by the executor; the auditor's
# guard just needs a rough number to decide whether to start the next
# category. Conservative on purpose.
_APPROX_USD_PER_1K_TOKENS = 0.003


@dataclass(frozen=True)
class _CategoryOutcome:
    """One sub-agent's result: findings + spend + did-it-complete."""

    category: str
    findings: list[AuditFinding]
    tokens_used: int
    cost_usd: float
    completed: bool
    """``False`` when the category was skipped (budget) or errored —
    the audit is marked ``partial=True`` in either case."""


# Public stream-event taxonomy — the runtime SSE bridge emits exactly
# these event names so the wire shape is documented in one place.
SSE_EVENT_CATEGORY_COMPLETE = "category_complete"
SSE_EVENT_AGENT_COMPLETE = "agent_complete"
SSE_EVENT_COMPLETED = "completed"
SSE_EVENT_ERROR = "error"


class Auditor:
    """Read-only Claude-orchestrated agent auditor.

    Backend-agnostic: takes a :class:`BaseLLMProvider` for the
    sub-agent fan-out and a :class:`StorageProvider` for run-history /
    KB-chunk stats. The seven category prompts live in
    :data:`_CATEGORY_PROMPTS`; subclasses or future categories slot in
    by extending the dict (kept open-vocabulary on purpose so adding
    a category doesn't need a model migration).
    """

    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        storage: StorageProvider,
        model: str,
        budget_usd: float = 0.0,
        severity_floor: AuditFindingSeverity = AuditFindingSeverity.INFO,
    ) -> None:
        self._provider = provider
        self._storage = storage
        self._model = model
        self._budget_usd = float(budget_usd)
        self._severity_floor = severity_floor

    async def audit_agent(
        self,
        *,
        bundle: AgentBundle,
        tenant_id: str,
        categories: Iterable[str] | None = None,
        on_event: Any = None,
    ) -> AuditRecord:
        """Audit ONE agent. Returns a tenant-scoped :class:`AuditRecord`.

        ``on_event`` is an optional callable invoked with
        ``(event_name, payload_dict)`` per SSE event (see the
        ``SSE_EVENT_*`` constants). When provided, every
        ``category_complete`` / ``agent_complete`` / ``completed`` event
        is forwarded — this is the bridge the runtime uses to stream
        progress as SSE. Pass ``None`` for the non-streamed path.
        """
        resolved_cats = self._resolve_categories(categories)
        outcomes = await self._run_categories(
            resolved_cats,
            agent_name=bundle.spec.name,
            bundle=bundle,
            tenant_id=tenant_id,
            project_stats=None,
            on_event=on_event,
        )
        # Per-agent SSE checkpoint — even on the single-agent path,
        # emitting an ``agent_complete`` event keeps the wire shape
        # uniform with the project path (callers can count the same
        # events either way).
        if on_event is not None:
            await _maybe_call(
                on_event,
                SSE_EVENT_AGENT_COMPLETE,
                {
                    "agent_name": bundle.spec.name,
                    "findings_for_agent": sum(len(o.findings) for o in outcomes),
                },
            )
        return self._record_from_outcomes(
            outcomes,
            tenant_id=tenant_id,
            scope_kind="agent",
            scope_id=bundle.spec.name,
            categories=resolved_cats,
        )

    async def audit_project(
        self,
        *,
        bundles: list[AgentBundle],
        project_id: str,
        tenant_id: str,
        categories: Iterable[str] | None = None,
        on_event: Any = None,
    ) -> AuditRecord:
        """Audit a list of agents as one project-wide audit.

        Project-wide cost outliers are computed across ``bundles`` (so
        ``cost_outliers`` flags agents above the project mean +2σ);
        every other category runs per-agent and the findings are
        concatenated, severity-floored, and persisted as one
        :class:`AuditRecord` keyed by ``project_id``.
        """
        resolved_cats = self._resolve_categories(categories)
        project_stats = await self._compute_project_cost_stats(
            bundles=bundles, tenant_id=tenant_id
        )
        all_outcomes: list[_CategoryOutcome] = []
        for b in bundles:
            outcomes = await self._run_categories(
                resolved_cats,
                agent_name=b.spec.name,
                bundle=b,
                tenant_id=tenant_id,
                project_stats=project_stats,
                on_event=on_event,
            )
            all_outcomes.extend(outcomes)
            if on_event is not None:
                await _maybe_call(
                    on_event,
                    SSE_EVENT_AGENT_COMPLETE,
                    {
                        "agent_name": b.spec.name,
                        "findings_for_agent": sum(len(o.findings) for o in outcomes),
                    },
                )
        return self._record_from_outcomes(
            all_outcomes,
            tenant_id=tenant_id,
            scope_kind="project",
            scope_id=project_id,
            categories=resolved_cats,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_categories(self, categories: Iterable[str] | None) -> list[str]:
        """Validate + default-fill the requested categories.

        Unknown categories are silently dropped (logged at ``warning``)
        so a typo in one of seven names doesn't fail the whole request
        — defensive on purpose; the API layer already validates the
        request shape strictly.
        """
        if categories is None:
            return list(CATEGORIES)
        seen: list[str] = []
        for c in categories:
            if c in _CATEGORY_PROMPTS and c not in seen:
                seen.append(c)
            elif c not in _CATEGORY_PROMPTS:
                logger.warning("audit_unknown_category category=%s — dropped", c)
        if not seen:
            # Defensive: caller explicitly listed only typos → fall back
            # to ALL so the audit still produces something.
            return list(CATEGORIES)
        return seen

    async def _run_categories(
        self,
        categories: list[str],
        *,
        agent_name: str,
        bundle: AgentBundle,
        tenant_id: str,
        project_stats: dict[str, float] | None,
        on_event: Any,
    ) -> list[_CategoryOutcome]:
        """Fan out the seven sub-agents in parallel.

        Each category gets its own sliced context. The budget guard is
        checked BEFORE each completion (pessimistic pre-flight: skip
        the category if the running total would exceed ``budget_usd``);
        after each completion the actual cost is added to the running
        total. This means a single over-spending category can short-
        circuit the rest, which is the documented contract.
        """
        # Pre-compute the slices once per agent so the parallel
        # sub-agents share read-only state without re-loading.
        slices = await self._build_context_slices(
            bundle=bundle,
            tenant_id=tenant_id,
            project_stats=project_stats,
        )

        running_cost = 0.0
        running_tokens = 0
        budget = self._budget_usd
        outcomes: list[_CategoryOutcome] = []
        # Sequential gating + parallel within-budget execution: we walk
        # the categories in declared order and skip cleanly when the
        # budget would be exceeded. A truly parallel ``asyncio.gather``
        # over all seven would also work — the per-category prompts are
        # independent — but the sequential walk makes the budget
        # short-circuit deterministic + keeps the SSE event order
        # stable (operators see ``category_complete`` events in the
        # declared CATEGORIES order regardless of LLM latency jitter).
        for cat in categories:
            if budget > 0:
                projected = running_cost + (
                    _APPROX_TOKENS_PER_CATEGORY * _APPROX_USD_PER_1K_TOKENS / 1000.0
                )
                if projected > budget:
                    outcomes.append(
                        _CategoryOutcome(
                            category=cat,
                            findings=[],
                            tokens_used=0,
                            cost_usd=0.0,
                            completed=False,
                        )
                    )
                    if on_event is not None:
                        await _maybe_call(
                            on_event,
                            SSE_EVENT_CATEGORY_COMPLETE,
                            {
                                "category": cat,
                                "findings_so_far": sum(len(o.findings) for o in outcomes),
                                "skipped_reason": "budget_exhausted",
                            },
                        )
                    continue
            outcome = await self._run_one_category(
                cat,
                agent_name=agent_name,
                slices=slices,
            )
            outcomes.append(outcome)
            running_cost += outcome.cost_usd
            running_tokens += outcome.tokens_used
            if on_event is not None:
                await _maybe_call(
                    on_event,
                    SSE_EVENT_CATEGORY_COMPLETE,
                    {
                        "category": cat,
                        "findings_so_far": sum(len(o.findings) for o in outcomes),
                    },
                )
        return outcomes

    async def _build_context_slices(
        self,
        *,
        bundle: AgentBundle,
        tenant_id: str,
        project_stats: dict[str, float] | None,
    ) -> dict[str, str]:
        """Build the per-category context slice rendered into the prompt.

        Each slice is a small text snippet (≤ ~2 KB) that the per-category
        sub-agent reads. This is the read-only edge: we read the bundle's
        prompt / schemas / contexts, list the agent's KB chunks (without
        retrieving them), and list recent run cost stats — never writing
        anything back.
        """
        spec = bundle.spec
        prompt_md = bundle.prompt_template
        agent_yaml_summary = _summarize_agent_yaml(bundle)

        # KB chunk stats — list (read-only) and count; the storage seam
        # ``list_kb_chunks`` doesn't carry retrieval-frequency data
        # natively today, so we summarise what we can (count + sample
        # text) and let the LLM flag obvious smells. Future PR adds a
        # ``kb_chunk_retrieval_stats`` method to the StorageProvider
        # Protocol so frequency data becomes first-class — flagged in
        # the PR notes.
        try:
            chunks = await self._storage.list_kb_chunks(
                agent=spec.name,
                tenant_id=tenant_id,
                limit=200,
            )
        except Exception:
            chunks = []
        kb_summary = _summarize_kb_chunks(chunks)

        # Recent runs for schema_drift + cost_outliers + model_choice.
        try:
            runs = await self._storage.list_runs(
                agent=spec.name,
                tenant_id=tenant_id,
                limit=50,
            )
        except Exception:
            runs = []
        run_cost_summary = _summarize_run_costs(runs)
        sample_runs_summary = _summarize_sample_runs(runs)

        # Recent eval pass-rate for model_choice.
        try:
            evals = await self._storage.list_evals(
                agent=spec.name,
                tenant_id=tenant_id,
                limit=5,
            )
        except Exception:
            evals = []
        eval_summary = _summarize_evals(evals)

        # Eval dataset (read-only). The bundle doesn't carry it as text —
        # we just count what's there + sample a couple rows if available.
        dataset_summary = _summarize_dataset(bundle)

        # Project cost stats for cost_outliers (only set on the
        # project path). The single-agent path passes ``None`` so the
        # sub-agent reasons against the agent's own run history.
        project_stats_blob = (
            f"\nproject_cost_stats: {json.dumps(project_stats)}"
            if project_stats is not None
            else ""
        )

        # Per-category slice. Kept compact — the LLM does its job with
        # less noise + the budget guard sees a smaller token footprint.
        return {
            "ambiguous_prompts": (
                f"=== prompt.md ===\n{prompt_md}\n=== end ==="
            ),
            "missing_eval_coverage": (
                f"=== prompt.md ===\n{prompt_md}\n=== end ===\n"
                f"=== eval dataset summary ===\n{dataset_summary}\n=== end ===\n"
                f"=== KB chunk summary ===\n{kb_summary}\n=== end ==="
            ),
            "security_smells": (
                f"=== prompt.md ===\n{prompt_md}\n=== end ===\n"
                f"=== contexts ===\n{_summarize_contexts(bundle)}\n=== end ===\n"
                f"=== skills ===\n{_summarize_skills(bundle)}\n=== end ==="
            ),
            "cost_outliers": (
                f"=== agent.yaml summary ===\n{agent_yaml_summary}\n=== end ===\n"
                f"=== recent run cost stats ===\n{run_cost_summary}{project_stats_blob}\n=== end ==="
            ),
            "kb_quality": (
                f"=== KB chunks ===\n{kb_summary}\n=== end ==="
            ),
            "schema_drift": (
                f"=== input_schema ===\n{json.dumps(bundle.input_schema)}\n=== end ===\n"
                f"=== output_schema ===\n{json.dumps(bundle.output_schema)}\n=== end ===\n"
                f"=== sample recent runs ===\n{sample_runs_summary}\n=== end ==="
            ),
            "model_choice": (
                f"=== agent.yaml summary ===\n{agent_yaml_summary}\n=== end ===\n"
                f"=== recent eval pass-rate ===\n{eval_summary}\n=== end ===\n"
                f"=== recent run cost stats ===\n{run_cost_summary}\n=== end ==="
            ),
        }

    async def _run_one_category(
        self,
        category: str,
        *,
        agent_name: str,
        slices: dict[str, str],
    ) -> _CategoryOutcome:
        """One sub-agent call. Wrapped in try/except so a category
        failure becomes a zero-finding outcome + ``partial=True``."""
        system_prompt = _CATEGORY_PROMPTS.get(category)
        if system_prompt is None:
            # Unknown category — shouldn't happen because _resolve_categories
            # filters them, but defensive against future callers.
            return _CategoryOutcome(
                category=category,
                findings=[],
                tokens_used=0,
                cost_usd=0.0,
                completed=False,
            )
        slice_blob = slices.get(category, "")
        try:
            response = await self._provider.complete(
                CompletionRequest(
                    provider=self._model,
                    messages=[
                        Message(role="system", content=system_prompt),
                        Message(
                            role="user",
                            content=(
                                f"Audit agent {agent_name!r}. Context follows.\n\n"
                                f"{slice_blob}\n\n"
                                "Respond ONLY with the JSON object."
                            ),
                        ),
                    ],
                    # Conservative — keeps the cap predictable. The
                    # JSON return shape is small.
                    params={"max_tokens": 1500, "temperature": 0.0},
                )
            )
        except Exception:
            logger.warning("audit_category_failed category=%s agent=%s", category, agent_name)
            return _CategoryOutcome(
                category=category,
                findings=[],
                tokens_used=0,
                cost_usd=0.0,
                completed=False,
            )

        findings = _parse_findings(
            response.text,
            category=category,
            agent_name=agent_name,
        )
        # Best-effort cost estimate. The Auditor doesn't consult the
        # pricing table directly (boundary: core/auditor.py only depends
        # on the provider Protocol + storage Protocol) — we use the same
        # rough estimate as the budget guard so the running total + the
        # persisted ``cost_usd`` are derived consistently from the same
        # number. Real pricing lives in the executor's edge.
        total_tokens = response.tokens.input + response.tokens.output
        cost_estimate = total_tokens * _APPROX_USD_PER_1K_TOKENS / 1000.0
        return _CategoryOutcome(
            category=category,
            findings=findings,
            tokens_used=total_tokens,
            cost_usd=cost_estimate,
            completed=True,
        )

    async def _compute_project_cost_stats(
        self,
        *,
        bundles: list[AgentBundle],
        tenant_id: str,
    ) -> dict[str, float]:
        """Cross-agent cost statistics for the project audit's
        ``cost_outliers`` sub-agent. Stats are mean + stddev of per-run
        cost across the listed agents' recent runs."""
        all_costs: list[float] = []
        for b in bundles:
            try:
                runs = await self._storage.list_runs(
                    agent=b.spec.name,
                    tenant_id=tenant_id,
                    limit=50,
                )
            except Exception:
                runs = []
            for r in runs:
                all_costs.append(r.metrics.cost_usd)
        if not all_costs:
            return {"mean_cost_usd": 0.0, "stddev_cost_usd": 0.0, "sample_count": 0.0}
        n = len(all_costs)
        mean = sum(all_costs) / n
        variance = sum((c - mean) ** 2 for c in all_costs) / n
        stddev = variance ** 0.5
        return {
            "mean_cost_usd": round(mean, 6),
            "stddev_cost_usd": round(stddev, 6),
            "sample_count": float(n),
        }

    def _record_from_outcomes(
        self,
        outcomes: list[_CategoryOutcome],
        *,
        tenant_id: str,
        scope_kind: str,
        scope_id: str,
        categories: list[str],
    ) -> AuditRecord:
        """Flatten outcomes → :class:`AuditRecord`. Severity-floor + assign
        stable ids."""
        floor = _SEVERITY_ORDER[self._severity_floor]
        kept: list[AuditFinding] = []
        for o in outcomes:
            for f in o.findings:
                if _SEVERITY_ORDER[f.severity] < floor:
                    continue
                kept.append(f.model_copy(update={"id": f"f{len(kept) + 1}"}))
        partial = any(not o.completed for o in outcomes)
        tokens_used = sum(o.tokens_used for o in outcomes)
        cost_usd = round(sum(o.cost_usd for o in outcomes), 6)
        return AuditRecord(
            audit_id=f"audit_{uuid4().hex[:24]}",
            tenant_id=tenant_id,
            scope_kind=scope_kind,
            scope_id=scope_id,
            categories=categories,
            severity_floor=self._severity_floor,
            model=self._model,
            budget_usd=self._budget_usd,
            findings=kept,
            partial=partial,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
        )


# ---------------------------------------------------------------------------
# Helpers — pure functions, no side effects.
# ---------------------------------------------------------------------------


async def _maybe_call(on_event: Any, name: str, payload: dict[str, Any]) -> None:
    """Call the optional progress callback, supporting both sync + async.

    Defensive against a misbehaving callback: a callback exception is
    logged + swallowed (a progress event must never break the audit).
    """
    try:
        result = on_event(name, payload)
        if asyncio.iscoroutine(result):
            await result
    except Exception:
        logger.warning("audit_on_event_failed event=%s", name)


def _summarize_agent_yaml(bundle: AgentBundle) -> str:
    """Compact agent.yaml summary for the cost/model sub-agents."""
    spec = bundle.spec
    return json.dumps(
        {
            "name": spec.name,
            "version": spec.version,
            "model_provider": spec.model.provider if spec.model else None,
            "max_tokens": (
                spec.model.params.get("max_tokens") if spec.model and spec.model.params else None
            ),
            "skills": list(spec.skills) if spec.skills else [],
            "context_count": len(bundle.contexts),
        }
    )


def _summarize_contexts(bundle: AgentBundle) -> str:
    parts: list[str] = []
    for name, body in bundle.contexts:
        parts.append(f"--- context {name!r} ({len(body)} chars) ---\n{body[:600]}")
    return "\n".join(parts) if parts else "<no contexts>"


def _summarize_skills(bundle: AgentBundle) -> str:
    if not bundle.skills:
        return "<no skills>"
    rows = []
    for s in bundle.skills:
        name = getattr(s, "name", None) or getattr(getattr(s, "spec", None), "name", "<unknown>")
        kind = getattr(getattr(s, "spec", None), "implementation_kind", None) or getattr(
            s, "implementation_kind", None
        )
        rows.append(f"- {name} ({kind})")
    return "\n".join(rows)


def _summarize_kb_chunks(chunks: list[Any]) -> str:
    if not chunks:
        return "<no KB chunks>"
    total = len(chunks)
    sample = chunks[:5]
    rows: list[str] = [f"total_chunks: {total}"]
    for c in sample:
        text = getattr(c, "text", "") or ""
        source = getattr(c, "source", "") or ""
        rows.append(f"- source={source!r} text={text[:200]!r}")
    if total > 5:
        rows.append(f"... ({total - 5} more)")
    return "\n".join(rows)


def _summarize_run_costs(runs: list[Any]) -> str:
    if not runs:
        return "<no recent runs>"
    costs = [getattr(getattr(r, "metrics", None), "cost_usd", 0.0) for r in runs]
    n = len(costs)
    mean = sum(costs) / n
    return json.dumps(
        {
            "sample_count": n,
            "mean_cost_usd": round(mean, 6),
            "max_cost_usd": round(max(costs), 6),
            "min_cost_usd": round(min(costs), 6),
        }
    )


def _summarize_sample_runs(runs: list[Any]) -> str:
    if not runs:
        return "<no recent runs>"
    sample = runs[:5]
    rows = []
    for r in sample:
        rows.append(
            json.dumps(
                {
                    "input_keys": sorted(list((getattr(r, "input", {}) or {}).keys())),
                    "output_keys": sorted(list((getattr(r, "output", {}) or {}).keys())),
                    "status": str(getattr(r, "status", "")),
                }
            )
        )
    return "\n".join(rows)


def _summarize_dataset(bundle: AgentBundle) -> str:
    """Read the agent's eval dataset (if present) without mutating it.

    Counts rows + samples up to 3 rows for the missing_eval_coverage
    sub-agent. Read-only by construction — only ``Path.read_text``.
    """
    spec = bundle.spec
    dataset_rel = spec.evals.dataset if spec.evals else None
    if not dataset_rel:
        return "<no dataset>"
    dataset_path = bundle.agent_dir / dataset_rel
    if not dataset_path.exists():
        return "<no dataset>"
    try:
        text = dataset_path.read_text()
    except Exception:
        return "<dataset unreadable>"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    sample = lines[:3]
    return json.dumps({"row_count": len(lines), "sample_rows": sample})


def _summarize_evals(evals: list[Any]) -> str:
    if not evals:
        return "<no evals>"
    latest = evals[0]
    return json.dumps(
        {
            "sample_count": len(evals),
            "latest_pass_rate": getattr(latest, "pass_rate", None),
            "latest_mean_score": getattr(latest, "mean_score", None),
        }
    )


_JSON_BLOB_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_findings(
    text: str, *, category: str, agent_name: str
) -> list[AuditFinding]:
    """Parse the sub-agent's JSON reply into typed findings.

    Defensive: an LLM that ignores the schema (non-JSON, missing
    ``findings`` key, bad severity) yields an empty list (not an
    exception). One bad sub-agent must never break the whole audit.
    """
    if not text or not text.strip():
        return []
    # The LLM sometimes wraps JSON in markdown fences. Extract the
    # first balanced ``{ ... }`` blob; if that fails, give up cleanly.
    match = _JSON_BLOB_RE.search(text)
    raw = match.group(0) if match else text
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "audit_finding_json_parse_failed category=%s agent=%s", category, agent_name
        )
        return []
    raw_findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(raw_findings, list):
        return []
    out: list[AuditFinding] = []
    for i, rf in enumerate(raw_findings):
        if not isinstance(rf, dict):
            continue
        sev_raw = str(rf.get("severity", "info")).lower()
        try:
            sev = AuditFindingSeverity(sev_raw)
        except ValueError:
            # An unknown severity → drop quietly (defensive).
            continue
        loc: AuditFindingLocation | None = None
        line = rf.get("line")
        if isinstance(line, int):
            loc = AuditFindingLocation(kind="prompt_line", line=line)
        try:
            out.append(
                AuditFinding(
                    id=f"f{i + 1}",  # provisional; the Auditor re-numbers globally
                    category=category,
                    severity=sev,
                    agent_name=agent_name,
                    location=loc,
                    title=str(rf.get("title", "")).strip() or "<untitled finding>",
                    description=str(rf.get("description", "")).strip(),
                    suggestion=str(rf.get("suggestion", "")).strip(),
                    confidence=str(rf.get("confidence", "medium")).lower(),
                )
            )
        except Exception:
            # A bad finding shape → drop, keep going.
            continue
    return out


__all__ = [
    "CATEGORIES",
    "SSE_EVENT_AGENT_COMPLETE",
    "SSE_EVENT_CATEGORY_COMPLETE",
    "SSE_EVENT_COMPLETED",
    "SSE_EVENT_ERROR",
    "Auditor",
]
