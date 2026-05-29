"""Data contracts for the Observability Intelligence layer (ADR 047).

Kept in their own module (not folded into ``core/models.py``) to minimize
collision surface with other in-flight PRs — this whole feature is a new,
self-contained package plus one append-only storage table.

The store is **append-only**: a re-run of the analyst for a given
``(tenant_id, project_id, date)`` inserts a NEW row rather than mutating the
prior one, so the daily insight history is itself an audit trail. Reads take
the *latest* row per ``(tenant, project, date)`` (newest ``created_at`` wins).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


class AnomalySeverity(StrEnum):
    """How far a metric has drifted from its trailing baseline.

    Mapped from the absolute z-score in
    :func:`movate.core.observability.analyst.detect_anomalies`:
    ``info`` (z >= 2), ``warning`` (z >= 3), ``critical`` (z >= 4).
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Anomaly(BaseModel):
    """One typed anomaly record: a metric that drifted from its baseline.

    Pure-Python z-score detection produces these (no LLM). ``value`` is the
    observed value for the window, ``baseline`` is the trailing mean, and
    ``z`` is ``(value - mean) / std`` (0.0 when std is 0 — a flat baseline
    can't produce a meaningful z-score).
    """

    model_config = ConfigDict(extra="forbid")

    metric: str
    """Which signal drifted: ``cost``, ``latency``, ``error_rate`` or ``volume``."""
    severity: AnomalySeverity
    value: float
    baseline: float
    z: float
    """Standard scores from the trailing baseline; sign carries direction."""
    note: str = ""
    """Optional human-readable hint rendered in the narrative / CLI."""


class ObservabilityInsight(BaseModel):
    """One day's preprocessed telemetry summary for a (tenant, project).

    The analyst computes one of these per night per project and appends it
    via :meth:`StorageProvider.save_insight`. The NL-query fast path reads
    these instead of re-scanning raw runs, so ``ask`` / ``health`` answer in
    a single indexed lookup.

    The four ``dict`` / ``list`` fields are persisted as JSON:

    * ``anomalies`` — list of :class:`Anomaly` dumps (typed records).
    * ``top_failures`` — list of failure clusters
      ``{signature, count, sample_message, agent}`` (clustered by the #542
      diagnoser when present, else un-clustered raw failures).
    * ``usage_rollup`` — aggregate counters
      ``{runs, errors, error_rate, cost_usd, tokens_in, tokens_out,
      mean_latency_ms, p95_latency_ms, eval_pass_rate, by_agent, by_provider}``.
    * ``trends`` — trailing baselines + deltas the anomaly detector used
      ``{<metric>: {value, baseline, delta_pct}}`` so a reader can see the
      direction of travel without recomputing.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: uuid4().hex)
    tenant_id: str
    """Tenant owner — NOT NULL. Every read is tenant-scoped at the SQL layer."""
    project_id: str
    date: date
    """The UTC calendar day this insight summarizes."""
    health_score: float
    """Composite 0-100. See
    :func:`movate.core.observability.analyst.compute_health_score`."""
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    top_failures: list[dict[str, Any]] = Field(default_factory=list)
    usage_rollup: dict[str, Any] = Field(default_factory=dict)
    trends: dict[str, Any] = Field(default_factory=dict)
    narrative_digest: str = ""
    """The single budget-capped LLM output: a short markdown digest. Empty
    when the analyst ran with no LLM or hit its budget cap before the call."""
    created_at: datetime = Field(default_factory=_now)

    def typed_anomalies(self) -> list[Anomaly]:
        """Re-hydrate the JSON ``anomalies`` blobs into :class:`Anomaly`."""
        return [Anomaly.model_validate(a) for a in self.anomalies]


class EvidenceKind(StrEnum):
    """What a piece of grounding evidence points at.

    Drives the ``reference`` field's meaning in :class:`Evidence`:

    * ``insight`` — an insight date (the fast-path source).
    * ``query`` — a named, parameterized read-only template + its params.
    * ``run`` — a specific run id.
    * ``event`` — a deploy / drift marker.
    * ``failure`` — a failure cluster signature.
    """

    INSIGHT = "insight"
    QUERY = "query"
    RUN = "run"
    EVENT = "event"
    FAILURE = "failure"


class Evidence(BaseModel):
    """One citation backing a grounded answer.

    Citations are MANDATORY (ADR 047): every ``ask`` / ``troubleshoot``
    answer carries at least one of these when there is data, so an operator
    can trace the model's claim back to a concrete telemetry source.
    """

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind
    reference: str
    """The source pointer — an insight date, a template name, a run id, etc."""
    detail: str = ""
    """Short human-readable summary of what this evidence shows."""
    data: dict[str, Any] = Field(default_factory=dict)
    """Structured payload (e.g. the template's rows, the anomaly record)."""


class GroundedAnswer(BaseModel):
    """The return shape of ``ask`` / ``troubleshoot``.

    A natural-language ``answer`` plus the ``evidence`` that grounds it, a
    ``confidence`` in [0, 1], and an optional ``suggested_action``. The
    ``budget_usd`` / ``cost_usd`` pair lets callers see the LLM spend the
    query incurred against its cap.
    """

    model_config = ConfigDict(extra="forbid")

    answer: str
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.0
    suggested_action: str = ""
    cost_usd: float = 0.0
    """Estimated LLM cost this answer incurred (0.0 when no LLM was called)."""


__all__ = [
    "Anomaly",
    "AnomalySeverity",
    "Evidence",
    "EvidenceKind",
    "GroundedAnswer",
    "ObservabilityInsight",
]
