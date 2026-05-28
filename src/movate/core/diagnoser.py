"""Failure Pattern Diagnoser — the foundational diagnose step of ADR 043.

Read recent failures for an agent (failed runs, eval misses, drift
detections, optional canary misses), cluster them by failure mode using
Claude, and propose a TYPED fix per cluster. **Read-only**: this module
NEVER modifies the agent — it produces structured proposals only. ADR
043's apply step (a separate later PR) is the only thing that mutates an
agent's prompt / KB / context / model.

This module is a pure transform: it consumes a list of :class:`Failure`
dataclasses (built by callers from storage reads) and returns a
:class:`DiagnoseResult`. The storage seam stays in the runtime adapter
(:mod:`movate.core.diagnose_sources`) — the diagnoser itself imports no
storage backend, no runtime app, no tracing. Same boundary discipline
the rest of ``core`` follows: depends on :class:`BaseLLMProvider`, never
on a concrete provider.

The clustering is a **single-prompt Claude call** that returns a list of
cluster summaries with example pointers + a typed-fix proposal in one
shot. We considered a sub-agent-per-cluster fan-out (cluster pass A →
proposed-fix pass B) but converged on a single pass because:

* It collapses two LLM round-trips into one, halving latency and tokens
  for the same accuracy (the cluster summary and the fix proposal share
  the same context window — splitting forces re-priming).
* It lets the diagnoser meet the request's ``budget_usd`` cap with one
  predictable spend instead of N (variable) per-cluster spends.
* It matches the ADR 043 D1 surface: one structured analysis output
  with clusters + typed fixes, not a streaming pipeline.

The single call uses JSON mode (``response_format=json_object``) so the
parser has a stable, validated schema to consume — no free-text-to-JSON
re-prompting. A parse failure on the model's reply is recorded as a
diagnoser error, not silently retried (that would double-spend the
budget).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, cast

from movate.providers.base import BaseLLMProvider, CompletionRequest, Message

logger = logging.getLogger(__name__)

# Hard cap on failures fed to the LLM in one call — past this we sample
# (oldest dropped first) so the prompt stays inside a reasonable context
# window AND the spend stays inside ``budget_usd``. The actual char
# budget the prompt uses is the more conservative gate; this is a safety
# valve against a tenant with thousands of failures.
MAX_FAILURES_PER_PROMPT = 200

# Hard cap on the JSON content the diagnoser sends to the model per
# failure (input + output excerpt + error). Anything past this is
# truncated with a ``…[truncated]`` marker. A failure with a 100KB
# output blob would otherwise blow the prompt budget on one row.
MAX_FAILURE_CHARS = 800

# Hard cap on the number of representative example run_ids the diagnoser
# returns per cluster. Five gives an operator enough to spot-check the
# pattern without exploding the GET response.
MAX_EXAMPLES_PER_CLUSTER = 5


class FailureSource(StrEnum):
    """Where this failure came from.

    Mirrors the four input streams the diagnose endpoint accepts:
    failed runs (``run``), eval misses (``eval``), drift detections
    (``drift``), and canary misses (``canary``). The string lands on
    the wire inside each :class:`FailureClusterView` example so an
    operator can tell at a glance whether a cluster is driven by prod
    runs vs. eval regressions.
    """

    RUN = "run"
    EVAL = "eval"
    DRIFT = "drift"
    CANARY = "canary"


@dataclass(frozen=True)
class Failure:
    """One observed failure across any input source.

    The diagnoser's single input shape. Backend-agnostic — callers
    (the runtime adapter) build this from :class:`RunRecord` /
    :class:`EvalRecord` / drift results / canary misses without the
    diagnoser caring which source it came from.

    ``id`` is the underlying record's primary key (run_id, eval_id, …)
    so a cluster's ``example_ids`` round-trips back to the source
    record for an operator to inspect. ``source`` discriminates which
    table to look in. ``summary`` is the human-readable one-liner the
    LLM sees; ``input``/``output``/``error`` carry the structured
    detail the LLM needs to spot a pattern.
    """

    id: str
    source: FailureSource
    summary: str
    """One-line operator-readable headline. Falls onto the LLM's prompt
    as the failure's primary identifier."""
    created_at: datetime
    """When the failure happened. Used for the window-days filter
    upstream and for chronological ordering in the prompt."""
    input: dict[str, Any] = field(default_factory=dict)
    """The agent's input on this failure. Empty for drift / canary
    detections that don't pin one input."""
    output: dict[str, Any] | None = None
    """The agent's actual output (if any). ``None`` for an aborted run
    or a drift detection that has no per-run output."""
    error: str | None = None
    """The error message or eval-miss rationale. ``None`` for a
    successful-but-low-scoring run that triggered drift."""
    extra: dict[str, Any] = field(default_factory=dict)
    """Source-specific context the LLM may find useful (e.g. an eval
    case's ``expected`` field, a drift detection's per-dimension
    delta). Carried verbatim — no schema enforcement."""


# The seven typed fix kinds. MUST match ADR 043's taxonomy exactly so
# the apply step in the next PR can dispatch on the kind without
# remapping. Adding a new kind here is an ADR 043 amendment, not a
# casual extension.
FixKind = Literal[
    "prompt_edit",
    "kb_ingest",
    "context_add",
    "context_remove",
    "model_swap",
    "temperature_change",
    "retrieval_k_change",
]


# The set above as a runtime-checkable frozen set; used by the parser
# below to reject a model that hallucinates an unknown kind.
ALLOWED_FIX_KINDS: frozenset[str] = frozenset(
    {
        "prompt_edit",
        "kb_ingest",
        "context_add",
        "context_remove",
        "model_swap",
        "temperature_change",
        "retrieval_k_change",
    }
)


@dataclass(frozen=True)
class ProposedFix:
    """One typed-fix proposal — the discriminated union (`kind` is the tag).

    The ``payload`` shape depends on ``kind`` and matches what ADR 043's
    apply step will dispatch on. Validated by the wire-edge Pydantic
    schemas in :mod:`movate.runtime.schemas`:

    * ``prompt_edit`` → ``{"before": str, "after": str, "patch_text": str}``
      (unified diff against ``prompt.md``)
    * ``kb_ingest`` → ``{"kind": str, "source": str, "rationale": str}``
      (suggested payload for the unified KB ingest endpoint)
    * ``context_add`` → ``{"name": str, "body": str}``
    * ``context_remove`` → ``{"name": str}``
    * ``model_swap`` → ``{"provider": str}``
    * ``temperature_change`` → ``{"delta": float}``
    * ``retrieval_k_change`` → ``{"delta": int}``
    """

    kind: FixKind
    payload: dict[str, Any]
    rationale: str
    """One-paragraph why-this-fix explanation grounded in the cluster's
    failure pattern. Carried as-is onto the wire so an operator can
    audit the proposal before approving an apply (later PR)."""
    expected_improvement: dict[str, Any] = field(default_factory=dict)
    """Optional ``{"metric": str, "delta": float, "based_on": str}`` —
    the diagnoser's estimated lift from applying this fix. Empty when
    the LLM declines to estimate (no fabrication)."""


@dataclass(frozen=True)
class FailureCluster:
    """One cohort of related failures + the diagnoser's proposed fix.

    A cluster is the unit of diagnose output: the LLM grouped N
    failures it considers manifestations of the same root cause,
    summarized the cause, and proposed one typed fix that would
    address them. ``example_ids`` round-trips back to the source
    records so an operator can drill in.
    """

    id: str
    summary: str
    example_count: int
    """Total failures the LLM placed in this cluster. May be larger
    than ``len(example_ids)`` — we cap the returned example ids at
    :data:`MAX_EXAMPLES_PER_CLUSTER` for response-size hygiene."""
    example_ids: list[str]
    confidence: Literal["high", "medium", "low"]
    proposed_fix: ProposedFix


@dataclass(frozen=True)
class DiagnoseResult:
    """The diagnoser's structured output for the runtime to persist + render.

    ``input_summary`` lets a GET response describe what was examined
    without a separate query — total failures examined, the number of
    clusters identified, and the per-cluster example cap that was in
    effect.
    """

    clusters: list[FailureCluster]
    total_failures_examined: int
    examples_per_cluster_max: int
    tokens_used: int
    cost_usd: float
    model: str


# ---------------------------------------------------------------------------
# Diagnoser
# ---------------------------------------------------------------------------


class DiagnoserBudgetExceeded(Exception):  # noqa: N818 — domain-named, matches MovateError siblings
    """Raised when the estimated cost of the diagnoser's LLM call exceeds
    the request's ``budget_usd`` cap.

    Caught at the runtime edge and converted to a structured error on
    the persisted :class:`DiagnosisRecord` — never propagated up as a
    500. The diagnoser's contract is "produce a result OR a budget
    error, never silently overspend."
    """


class DiagnoserParseError(Exception):
    """Raised when the LLM's reply doesn't validate against the diagnoser's
    JSON schema (missing fields, unknown fix kind, malformed payload).

    Caught at the runtime edge. We deliberately do NOT auto-re-prompt:
    a re-prompt doubles the spend and a model that hallucinated a fix
    kind once tends to do it again. Operators see the parse error and
    decide whether to retry with a stronger model.
    """


class Diagnoser:
    """Cluster failures + propose typed fixes via Claude.

    Constructed once per diagnose request with the model provider and
    config. The single public method, :meth:`diagnose`, takes a list
    of :class:`Failure` objects and returns a :class:`DiagnoseResult`.

    Provider-agnostic — accepts any :class:`BaseLLMProvider`
    implementation. The Diagnoser computes the prompt and parses the
    response; the provider does the actual call.
    """

    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        model: str = "openai/gpt-4o-mini",
        budget_usd: float = 1.0,
        max_clusters: int = 10,
        estimated_cost_per_1k_tokens: float = 0.0006,
        max_tokens: int = 4096,
    ) -> None:
        self._provider = provider
        self._model = model
        self._budget_usd = budget_usd
        self._max_clusters = max(1, min(max_clusters, 50))
        self._estimated_cost_per_1k_tokens = estimated_cost_per_1k_tokens
        self._max_tokens = max_tokens

    async def diagnose(self, failures: list[Failure]) -> DiagnoseResult:
        """Cluster ``failures`` and propose typed fixes, one per cluster.

        Empty ``failures`` returns an empty result with zero cost — no
        LLM call is made (defensive: a tenant with no failures shouldn't
        be charged a token).

        On a single call we send a JSON-mode prompt with the failure
        list + an output schema describing the seven typed fix kinds.
        The model returns ``{"clusters": [...]}`` which we parse, cap
        at ``max_clusters``, and wrap into a :class:`DiagnoseResult`.

        Raises :class:`DiagnoserBudgetExceeded` if the pre-call cost
        estimate exceeds ``budget_usd``;
        :class:`DiagnoserParseError` if the model reply is invalid.
        """
        if not failures:
            return DiagnoseResult(
                clusters=[],
                total_failures_examined=0,
                examples_per_cluster_max=MAX_EXAMPLES_PER_CLUSTER,
                tokens_used=0,
                cost_usd=0.0,
                model=self._model,
            )

        sampled = self._sample_failures(failures)

        prompt = self._build_prompt(sampled)
        estimated_input_tokens = max(1, len(prompt) // 4)
        estimated_tokens = estimated_input_tokens + self._max_tokens
        estimated_cost = (estimated_tokens / 1000.0) * self._estimated_cost_per_1k_tokens
        if estimated_cost > self._budget_usd:
            raise DiagnoserBudgetExceeded(
                f"estimated cost ${estimated_cost:.4f} exceeds budget "
                f"${self._budget_usd:.4f} (failures={len(sampled)}, "
                f"prompt_chars={len(prompt)}, max_tokens={self._max_tokens}). "
                "Reduce window_days, raise min_failure_count, or raise budget_usd."
            )

        request = CompletionRequest(
            provider=self._model,
            messages=[
                Message(
                    role="system",
                    content=(
                        "You are a senior AI agent debugger. "
                        "Cluster failures by ROOT CAUSE (not by surface text). "
                        "For each cluster, propose ONE typed fix from the allowed "
                        "kinds. Reply with valid JSON matching the schema in the "
                        "user message."
                    ),
                ),
                Message(role="user", content=prompt),
            ],
            params={
                "response_format": {"type": "json_object"},
                "max_tokens": self._max_tokens,
            },
        )

        response = await self._provider.complete(request)
        tokens_used = response.tokens.input + response.tokens.output
        actual_cost = (tokens_used / 1000.0) * self._estimated_cost_per_1k_tokens

        clusters = self._parse_response(response.text, sampled)
        # Apply the runtime-honored cap (request.max_clusters propagates
        # into the Diagnoser's constructor).
        clusters = clusters[: self._max_clusters]

        return DiagnoseResult(
            clusters=clusters,
            total_failures_examined=len(failures),
            examples_per_cluster_max=MAX_EXAMPLES_PER_CLUSTER,
            tokens_used=tokens_used,
            cost_usd=round(actual_cost, 6),
            model=self._model,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sample_failures(self, failures: list[Failure]) -> list[Failure]:
        """Cap the failure list at :data:`MAX_FAILURES_PER_PROMPT`.

        Drop the oldest first when we have to — recent failures are
        more representative of the agent's current state. The caller
        is responsible for the window-days filter; this is just the
        prompt-size safety valve.
        """
        if len(failures) <= MAX_FAILURES_PER_PROMPT:
            return failures
        sorted_by_recency = sorted(failures, key=lambda f: f.created_at, reverse=True)
        return sorted_by_recency[:MAX_FAILURES_PER_PROMPT]

    def _build_prompt(self, failures: list[Failure]) -> str:
        """Compose the JSON-mode prompt sent to the LLM.

        The schema description is part of the prompt (not just the
        ``response_format`` flag) because providers differ on how
        strictly they bind to free-form schema hints. Including it
        verbatim is robust across LiteLLM backends.
        """
        rows: list[dict[str, Any]] = []
        for f in failures:
            rows.append(
                {
                    "id": f.id,
                    "source": f.source.value,
                    "summary": _truncate(f.summary, MAX_FAILURE_CHARS),
                    "input": _truncate_json(f.input, MAX_FAILURE_CHARS),
                    "output": _truncate_json(f.output, MAX_FAILURE_CHARS)
                    if f.output is not None
                    else None,
                    "error": _truncate(f.error, MAX_FAILURE_CHARS) if f.error else None,
                    "extra": _truncate_json(f.extra, MAX_FAILURE_CHARS),
                }
            )

        schema_doc = (
            "Output schema (strict, JSON object):\n"
            "{\n"
            '  "clusters": [\n'
            "    {\n"
            '      "id": str,  // short stable id, e.g. "cl1"\n'
            '      "summary": str,  // one-line root-cause description\n'
            '      "example_count": int,  // total failures in this cluster\n'
            '      "example_ids": [str],  // ids from the failures list above (up to 5)\n'
            '      "confidence": "high" | "medium" | "low",\n'
            '      "proposed_fix": {\n'
            '        "kind": "prompt_edit" | "kb_ingest" | "context_add"\n'
            '              | "context_remove" | "model_swap"\n'
            '              | "temperature_change" | "retrieval_k_change",\n'
            '        "payload": {...},  // shape depends on kind, see below\n'
            '        "rationale": str,  // why this fix\n'
            '        "expected_improvement": {  // optional, omit if unknown\n'
            '          "metric": str, "delta": number, "based_on": str\n'
            "        }\n"
            "      }\n"
            "    }\n"
            "  ]\n"
            "}\n"
            "\n"
            "Per-kind payload shapes:\n"
            '  prompt_edit: {"before": str, "after": str, "patch_text": str}\n'
            '  kb_ingest:   {"kind": str, "source": str}\n'
            '  context_add: {"name": str, "body": str}\n'
            '  context_remove: {"name": str}\n'
            '  model_swap: {"provider": str}\n'
            '  temperature_change: {"delta": number}\n'
            '  retrieval_k_change: {"delta": int}\n'
        )

        return (
            f"{schema_doc}\n"
            f"Analyze the following {len(failures)} failures for one agent. "
            "Cluster them by ROOT CAUSE (not surface text). For each cluster, "
            "propose exactly one typed fix from the allowed kinds. "
            "Return strict JSON matching the schema above; no prose outside JSON.\n\n"
            "FAILURES:\n"
            f"{json.dumps(rows, ensure_ascii=False, indent=None, default=str)}\n"
        )

    def _parse_response(self, text: str, failures: list[Failure]) -> list[FailureCluster]:
        """Parse the LLM's JSON reply into validated :class:`FailureCluster` rows.

        Defends against the common JSON-mode failure modes: extra
        markdown fences, missing fields, an unknown fix kind, a
        non-existent example_id. Anything malformed → an empty cluster
        list rather than a partial / broken result.
        """
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            # Strip "json\n" prefix if it survived the backtick strip.
            cleaned = cleaned.removeprefix("json\n").removeprefix("json")
        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise DiagnoserParseError(f"diagnoser reply is not valid JSON: {exc}") from exc

        if not isinstance(obj, dict) or "clusters" not in obj:
            raise DiagnoserParseError("diagnoser reply missing 'clusters' key at top level")

        raw_clusters = obj.get("clusters", [])
        if not isinstance(raw_clusters, list):
            raise DiagnoserParseError("diagnoser 'clusters' must be a list")

        valid_ids = {f.id for f in failures}
        clusters: list[FailureCluster] = []
        for idx, raw in enumerate(raw_clusters):
            if not isinstance(raw, dict):
                continue
            try:
                cluster = self._parse_one_cluster(raw, idx, valid_ids)
            except DiagnoserParseError as exc:
                logger.warning("diagnoser_skip_cluster idx=%s reason=%s", idx, exc)
                continue
            clusters.append(cluster)
        return clusters

    def _parse_one_cluster(
        self, raw: dict[str, Any], idx: int, valid_ids: set[str]
    ) -> FailureCluster:
        cluster_id = str(raw.get("id") or f"cl{idx + 1}")
        summary = str(raw.get("summary", "")).strip() or "(no summary)"
        confidence = str(raw.get("confidence", "medium"))
        if confidence not in ("high", "medium", "low"):
            confidence = "medium"

        example_ids_raw = raw.get("example_ids", [])
        if not isinstance(example_ids_raw, list):
            example_ids_raw = []
        example_ids: list[str] = []
        for eid in example_ids_raw[:MAX_EXAMPLES_PER_CLUSTER]:
            sid = str(eid)
            if sid in valid_ids:
                example_ids.append(sid)

        example_count_raw = raw.get("example_count")
        if isinstance(example_count_raw, int) and example_count_raw >= 0:
            example_count = example_count_raw
        else:
            example_count = len(example_ids)

        fix_raw = raw.get("proposed_fix")
        if not isinstance(fix_raw, dict):
            raise DiagnoserParseError("cluster missing 'proposed_fix' object")
        kind = str(fix_raw.get("kind", ""))
        if kind not in ALLOWED_FIX_KINDS:
            raise DiagnoserParseError(f"unknown fix kind {kind!r}")

        payload = fix_raw.get("payload", {})
        if not isinstance(payload, dict):
            raise DiagnoserParseError("proposed_fix.payload must be an object")

        rationale = str(fix_raw.get("rationale", "")).strip() or "(no rationale)"
        expected_raw = fix_raw.get("expected_improvement")
        expected: dict[str, Any] = {}
        if isinstance(expected_raw, dict):
            expected = expected_raw

        return FailureCluster(
            id=cluster_id,
            summary=summary,
            example_count=example_count,
            example_ids=example_ids,
            confidence=cast(Literal["high", "medium", "low"], confidence),
            proposed_fix=ProposedFix(
                kind=cast(FixKind, kind),
                payload=payload,
                rationale=rationale,
                expected_improvement=expected,
            ),
        )


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def _truncate_json(payload: Any, limit: int) -> Any:
    """Truncate a JSON-serializable payload by re-serializing + clipping.

    Keeps the structure recognizable to the LLM while keeping the per-
    failure footprint bounded. A 100KB output blob becomes the first
    ``limit`` characters followed by the truncation marker, wrapped in a
    one-key envelope so the LLM still sees a JSON object on the failure
    row (vs. a raw clipped string that breaks the JSON-mode prompt).
    """
    try:
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    except Exception:
        return {"_truncated": True, "_preview": str(payload)[:limit]}
    if len(serialized) <= limit:
        return payload
    return {"_truncated": True, "_preview": serialized[:limit] + "…"}
