"""Audit report — :class:`Finding` + :class:`AuditReport` aggregator.

Each scanner produces zero or more :class:`Finding` objects, each
carrying a severity, a category (which scanner ran), the offending
target (agent name or path), and an operator-facing message + hint.

:class:`AuditReport` is the aggregated, sortable, JSON-serialisable
result. Two views: the Rich-friendly table view (CLI) and the
machine-readable JSON view (CI).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """Audit-finding severity.

    * ``error``: blocks deployment. CI gates exit non-zero.
    * ``warning``: should be addressed but won't block by default.
      ``--strict`` mode promotes warnings to errors.
    * ``info``: informational signal (e.g. "agent has no examples but
      has a dataset, which is fine"). Never blocks.
    """

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class Finding:
    """One audit finding.

    ``category`` is the scanner's stable identifier (``"missing-evals"``,
    ``"exposed-secret"``, etc.) — CI tooling filters on this to
    suppress specific scanners during transitions. ``target`` is the
    agent name when scoped to one agent, the file path when scoped
    to a file, ``"project"`` when project-level.

    ``hint`` is the operator-facing remediation pointer. Always
    actionable: "add `description:` to agent.yaml" not "missing
    metadata."
    """

    category: str
    severity: Severity
    target: str
    message: str
    hint: str = ""


@dataclass(frozen=True)
class AuditReport:
    """Aggregated audit result.

    Findings are stored in registration order (the order scanners
    surfaced them). The CLI re-sorts for display: errors first,
    then warnings, then info; within each severity, alphabetical
    by target then category.
    """

    findings: tuple[Finding, ...] = field(default_factory=tuple)
    scanned_agents: int = 0

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == Severity.WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == Severity.INFO)

    @property
    def is_clean(self) -> bool:
        """True when no errors AND no warnings. Pure-info reports are clean."""
        return not self.errors and not self.warnings

    def gate_fails(self, *, strict: bool) -> bool:
        """Return True when the audit blocks deploy.

        Default semantics: errors always block. With ``--strict``,
        warnings also block — used in CI to require a clean bill of
        health before merging.
        """
        if self.errors:
            return True
        return bool(strict and self.warnings)

    def to_json(self) -> str:
        """Serialise to JSON for CI annotations / piping to jq.

        Stable key order so the output diff is signal-only.
        """
        payload = {
            "scanned_agents": self.scanned_agents,
            "summary": {
                "errors": len(self.errors),
                "warnings": len(self.warnings),
                "infos": len(self.infos),
                "is_clean": self.is_clean,
            },
            "findings": [
                {
                    "severity": f.severity.value,
                    "category": f.category,
                    "target": f.target,
                    "message": f.message,
                    "hint": f.hint,
                }
                for f in self.findings
            ],
        }
        return json.dumps(payload, indent=2, sort_keys=False)


# ---------------------------------------------------------------------------
# Sorted iteration for rendering
# ---------------------------------------------------------------------------


_SEVERITY_ORDER: dict[Severity, int] = {
    Severity.ERROR: 0,
    Severity.WARNING: 1,
    Severity.INFO: 2,
}


def sorted_findings(report: AuditReport) -> list[Finding]:
    """Sort findings for display: severity desc, target asc, category asc.

    Same shape both the Rich table renderer and any future Markdown
    reporter consume. Pulled out so the sort key lives in one place.
    """
    return sorted(
        report.findings,
        key=lambda f: (_SEVERITY_ORDER[f.severity], f.target, f.category),
    )
