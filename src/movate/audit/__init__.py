"""Production-readiness audit (Sprint N Day 8-10).

Final member of the K-state cluster. Scans a snapshot (or current
project state) for issues that should block deployment:

* **Missing evals** — agents without a dataset
* **Missing metadata** — agents without owner / description
* **Exposed secrets** — regex scan of agent.yaml + prompt.md for
  patterns that look like leaked API keys / tokens
* **Policy violations** — re-uses :class:`ModelPolicy` check
* **Prompt linter findings** — re-uses :func:`lint_prompt`
* **Empty examples + empty dataset** — agent has neither, no test signal
* **Untestable agents** — failure modes that block CI eval gating

Each scanner is a **pure function** over an agent directory. The
audit orchestrator walks every agent, runs every scanner, and
produces an :class:`AuditReport` with severity-tagged findings.

Two modes:

* ``audit_current(project_root)`` — scans the live project state
* ``audit_snapshot(project_root, hash)`` — scans a captured snapshot

Both produce the same :class:`AuditReport` shape; the CLI renders
identically. This lets operators audit a snapshot **before** they
``promote`` it (Sprint O).

CI-friendly: `--strict` promotes warnings to errors, exit code
gates merges, `--json` emits annotations-shape output.
"""

from __future__ import annotations

from movate.audit.report import (
    AuditReport,
    Finding,
    Severity,
)
from movate.audit.scanners import (
    SCANNERS,
    audit_current,
    audit_snapshot,
)

__all__ = [
    "SCANNERS",
    "AuditReport",
    "Finding",
    "Severity",
    "audit_current",
    "audit_snapshot",
]
