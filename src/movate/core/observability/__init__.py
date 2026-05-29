"""Observability Intelligence layer v1 (ADR 047).

Three facets of one feature, all behind the existing adapter Protocols:

* **Insights store** (:mod:`movate.core.observability.models`) — an
  append-only ``ObservabilityInsight`` row per (tenant, project, date),
  persisted through three additive :class:`StorageProvider` methods.
* **Overnight analyst** (:mod:`movate.core.observability.analyst`) — a
  scheduled MDK agent that preprocesses telemetry (runs / evals / failures)
  into anomalies + a health score + a budget-capped narrative digest. This
  is MDK dogfooding itself: the platform's own observability is produced by
  a scheduled MDK job.
* **NL query + troubleshoot** (:mod:`movate.core.observability.query`) — a
  grounded, citation-bearing question interface over the insights store plus
  a FIXED set of read-only, parameterized query templates (text-to-
  PARAMETERIZED-QUERY, never text-to-arbitrary-SQL).

Boundary discipline (CLAUDE.md rules 6-7): this package depends only on the
:class:`StorageProvider` + :class:`BaseLLMProvider` Protocols and the core
data models — never a concrete storage backend, never ``cli`` or ``runtime``.
"""

from __future__ import annotations

from movate.core.observability.models import (
    Anomaly,
    AnomalySeverity,
    Evidence,
    EvidenceKind,
    GroundedAnswer,
    ObservabilityInsight,
)

__all__ = [
    "Anomaly",
    "AnomalySeverity",
    "Evidence",
    "EvidenceKind",
    "GroundedAnswer",
    "ObservabilityInsight",
]
