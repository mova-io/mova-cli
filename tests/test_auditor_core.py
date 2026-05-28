"""Unit tests for the Claude-orchestrated audit pipeline core
(:mod:`movate.core.auditor`).

Coverage:

* Per-category sub-agent isolation: each category sees only its slice
  of context (not the whole agent + KB + runs payload).
* Budget cap: a budget that runs out short-circuits the remaining
  categories and marks the record ``partial=True``.
* Empty-findings case: the LLM returning ``{"findings": []}`` produces
  an empty :class:`AuditRecord`, not an error.
* Read-only invariant: ``test_audit_does_not_modify_agent`` verifies
  the InMemory storage's mutable lists are unchanged after an audit.
* Severity-floor: ``warn`` filter drops ``info`` findings.

Uses a tiny ``MockAuditProvider`` that records every prompt it was
called with — keeps assertions tight without a real LLM.
"""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")  # auditor imports loader → uses pydantic etc.

from movate.core.auditor import CATEGORIES, Auditor
from movate.core.loader import load_agent
from movate.core.models import AuditFindingSeverity
from movate.providers.base import (
    BaseLLMProvider,
    CompletionRequest,
    CompletionResponse,
    StreamChunk,
)
from movate.testing import InMemoryStorage, scaffold_agent


class MockAuditProvider(BaseLLMProvider):
    """Records every prompt; returns a per-category canned findings
    JSON so the auditor's flatten/filter logic gets exercised.
    """

    name = "mock_audit"
    version = "0.0.1"

    def __init__(
        self,
        *,
        findings_by_category: dict[str, list[dict[str, Any]]] | None = None,
        raise_for: set[str] | None = None,
    ) -> None:
        self._findings_by_category = findings_by_category or {}
        self._raise_for = raise_for or set()
        self.calls: list[tuple[str, str, str]] = []
        """(system_prompt_first_line, user_content, full_user_prompt)."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        sys_msg = request.messages[0].content if request.messages else ""
        user_msg = request.messages[1].content if len(request.messages) > 1 else ""
        first_line = sys_msg.split("\n")[0]
        self.calls.append((first_line, user_msg[:160], user_msg))

        # Detect which category this is by matching against the first
        # line of each canned prompt. Mirrors the structure in
        # _CATEGORY_PROMPTS without re-importing it.
        category = _classify_category(sys_msg)
        if category in self._raise_for:
            raise RuntimeError("mock provider raised for category " + category)
        findings = self._findings_by_category.get(category, [])
        return CompletionResponse(
            text=json.dumps({"findings": findings}),
        )

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        resp = await self.complete(request)
        yield StreamChunk(text=resp.text)
        yield StreamChunk(text="", tokens=resp.tokens)

    async def embed(self, text: str, *, model: str) -> list[float]:
        raise NotImplementedError


_CATEGORY_HINTS = {
    "ambiguous": "ambiguous_prompts",
    "eval-coverage": "missing_eval_coverage",
    "security": "security_smells",
    "cost": "cost_outliers",
    "kb": "kb_quality",
    "schema drift": "schema_drift",
    "model-choice": "model_choice",
}


def _classify_category(sys_msg: str) -> str | None:
    s = sys_msg.lower()
    if "ambigu" in s or "contradict" in s:
        return "ambiguous_prompts"
    if "eval-coverage" in s or "dataset row exercises" in s:
        return "missing_eval_coverage"
    if "security" in s and ("smell" in s or "pii" in s):
        return "security_smells"
    if "cost concerns" in s or "cost-per-run" in s:
        return "cost_outliers"
    if "knowledge base" in s or "kb chunk" in s or "knowledge" in s:
        return "kb_quality"
    if "schema drift" in s or "input/output schema" in s:
        return "schema_drift"
    if "model-choice" in s or "model too big" in s or "model fit" in s or "model-choice" in s:
        return "model_choice"
    return None


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def bundle(tmp_path: Path):
    agent_dir = scaffold_agent(tmp_path / "demo")
    return load_agent(agent_dir)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_audit_agent_returns_record_with_seven_categories_by_default(
    storage: InMemoryStorage, bundle
) -> None:
    """No category filter → all seven categories ran (the auditor
    called the provider once per category in the declared order)."""
    provider = MockAuditProvider()  # all-empty findings
    auditor = Auditor(
        provider=provider,
        storage=storage,
        model="openai/gpt-4o-mini",
        budget_usd=0.0,  # no cap
    )
    record = await auditor.audit_agent(bundle=bundle, tenant_id="t1")
    assert record.scope_kind == "agent"
    assert record.scope_id == "demo"
    assert record.tenant_id == "t1"
    assert record.findings == []
    assert record.partial is False
    # We called the provider once per category.
    assert len(provider.calls) == len(CATEGORIES)
    # AuditRecord.categories echoes the declared list.
    assert tuple(record.categories) == CATEGORIES


@pytest.mark.unit
async def test_audit_subagent_only_sees_its_own_slice(
    storage: InMemoryStorage, bundle
) -> None:
    """Per-category isolation: the cost_outliers sub-agent's user
    prompt MUST NOT carry the full prompt.md (that's an
    ambiguous_prompts-only slice), and the ambiguous_prompts user
    prompt MUST NOT carry KB chunks (that's a kb_quality slice).
    """
    provider = MockAuditProvider()
    auditor = Auditor(provider=provider, storage=storage, model="openai/gpt-4o-mini")
    await auditor.audit_agent(bundle=bundle, tenant_id="t1")

    by_category = {
        _classify_category(c[0] + "\n"): c[2] for c in provider.calls if _classify_category(c[0] + "\n")
    }
    # The cost_outliers slice carries agent.yaml summary + run cost stats,
    # NOT the full prompt.md banner.
    assert "=== prompt.md ===" not in by_category["cost_outliers"]
    assert "agent.yaml summary" in by_category["cost_outliers"]
    # The ambiguous_prompts slice DOES carry the prompt.md banner.
    assert "=== prompt.md ===" in by_category["ambiguous_prompts"]
    # The kb_quality slice carries KB chunk summary, NOT input/output schemas.
    assert "KB chunks" in by_category["kb_quality"]
    assert "input_schema" not in by_category["kb_quality"]


@pytest.mark.unit
async def test_audit_filters_below_severity_floor(
    storage: InMemoryStorage, bundle
) -> None:
    """severity_floor=warn drops info-level findings; warn+ keep."""
    findings = {
        "ambiguous_prompts": [
            {"severity": "info", "title": "info-1", "description": "x", "suggestion": "y"},
            {"severity": "warn", "title": "warn-1", "description": "x", "suggestion": "y"},
            {"severity": "critical", "title": "crit-1", "description": "x", "suggestion": "y"},
        ],
    }
    provider = MockAuditProvider(findings_by_category=findings)
    auditor = Auditor(
        provider=provider,
        storage=storage,
        model="openai/gpt-4o-mini",
        severity_floor=AuditFindingSeverity.WARN,
    )
    record = await auditor.audit_agent(
        bundle=bundle,
        tenant_id="t1",
        categories=["ambiguous_prompts"],
    )
    titles = {f.title for f in record.findings}
    assert titles == {"warn-1", "crit-1"}  # info-1 filtered


@pytest.mark.unit
async def test_audit_budget_cap_marks_partial_and_skips_remaining(
    storage: InMemoryStorage, bundle
) -> None:
    """A budget of $0.000001 exhausts after zero categories complete →
    every category should be skipped and the record marked partial."""
    provider = MockAuditProvider()
    auditor = Auditor(
        provider=provider,
        storage=storage,
        model="openai/gpt-4o-mini",
        budget_usd=0.000001,  # too small for even one category
    )
    record = await auditor.audit_agent(bundle=bundle, tenant_id="t1")
    assert record.partial is True
    assert record.findings == []
    # Provider should NOT have been called — budget pre-flight skipped
    # every category.
    assert len(provider.calls) == 0


@pytest.mark.unit
async def test_audit_provider_failure_marks_category_skipped(
    storage: InMemoryStorage, bundle
) -> None:
    """One sub-agent failing must not kill the audit — that category
    contributes zero findings and the record is partial."""
    provider = MockAuditProvider(raise_for={"security_smells"})
    auditor = Auditor(
        provider=provider,
        storage=storage,
        model="openai/gpt-4o-mini",
    )
    record = await auditor.audit_agent(bundle=bundle, tenant_id="t1")
    assert record.partial is True
    # Other six categories did run.
    assert len(provider.calls) == len(CATEGORIES)


@pytest.mark.unit
async def test_audit_does_not_modify_agent(
    storage: InMemoryStorage, bundle
) -> None:
    """Read-only invariant: every mutable storage list MUST be the same
    after an audit as before (modulo the new AuditRecord)."""
    # Capture the storage state BEFORE the audit (deep copy so we
    # detect in-place mutations as well as appends/deletes).
    before = {
        "runs": copy.deepcopy(storage.runs),
        "evals": copy.deepcopy(storage.evals),
        "bench": copy.deepcopy(storage.bench),
        "kb_chunks": copy.deepcopy(storage.kb_chunks),
        "agent_bundles": copy.deepcopy(storage.agent_bundles),
        "workflow_runs": copy.deepcopy(storage.workflow_runs),
        "jobs": copy.deepcopy(storage.jobs),
        "api_keys": copy.deepcopy(storage.api_keys),
        "feedback": copy.deepcopy(storage.feedback),
        "eval_schedules": copy.deepcopy(storage.eval_schedules),
        "job_schedules": copy.deepcopy(storage.job_schedules),
    }
    # And capture the bundle's files on disk.
    bundle_dir_snapshot = {
        p.relative_to(bundle.agent_dir): p.read_bytes()
        for p in bundle.agent_dir.rglob("*")
        if p.is_file()
    }

    provider = MockAuditProvider(
        findings_by_category={
            "ambiguous_prompts": [
                {"severity": "warn", "title": "t", "description": "d", "suggestion": "s"}
            ]
        }
    )
    auditor = Auditor(
        provider=provider, storage=storage, model="openai/gpt-4o-mini"
    )
    await auditor.audit_agent(bundle=bundle, tenant_id="t1")

    # The ONLY thing the audit may write durably is an AuditRecord (and
    # only when explicitly persisted by the dispatch; the Auditor itself
    # does not persist — that's the dispatch's job). Auditor here was
    # called directly, so no AuditRecord is in storage.audits either.
    assert storage.audits == []
    # Every other mutable list is BIT-FOR-BIT unchanged.
    for key, snap in before.items():
        actual = getattr(storage, key)
        assert actual == snap, f"audit mutated storage.{key}"
    # And the agent's files on disk are unchanged.
    after = {
        p.relative_to(bundle.agent_dir): p.read_bytes()
        for p in bundle.agent_dir.rglob("*")
        if p.is_file()
    }
    assert after == bundle_dir_snapshot, "audit mutated the agent directory"


@pytest.mark.unit
async def test_audit_project_aggregates_across_bundles(
    storage: InMemoryStorage, tmp_path: Path
) -> None:
    """Project audit fans out across multiple bundles; findings come
    back keyed by their agent_name."""
    b1 = load_agent(scaffold_agent(tmp_path / "a1", name="a1"))
    b2 = load_agent(scaffold_agent(tmp_path / "a2", name="a2"))
    provider = MockAuditProvider(
        findings_by_category={
            "ambiguous_prompts": [
                {"severity": "warn", "title": "t", "description": "d", "suggestion": "s"}
            ]
        }
    )
    auditor = Auditor(
        provider=provider, storage=storage, model="openai/gpt-4o-mini"
    )
    record = await auditor.audit_project(
        bundles=[b1, b2],
        project_id="proj-1",
        tenant_id="t1",
        categories=["ambiguous_prompts"],
    )
    assert record.scope_kind == "project"
    assert record.scope_id == "proj-1"
    # One finding per agent.
    agent_names = {f.agent_name for f in record.findings}
    assert agent_names == {"a1", "a2"}


@pytest.mark.unit
async def test_audit_emits_progress_events_via_on_event(
    storage: InMemoryStorage, bundle
) -> None:
    """The on_event callback receives one ``category_complete`` per
    category + one ``agent_complete`` at the end."""
    events: list[tuple[str, dict[str, Any]]] = []

    def on_event(name: str, payload: dict[str, Any]) -> None:
        events.append((name, payload))

    provider = MockAuditProvider()
    auditor = Auditor(
        provider=provider, storage=storage, model="openai/gpt-4o-mini"
    )
    await auditor.audit_agent(
        bundle=bundle,
        tenant_id="t1",
        categories=["ambiguous_prompts", "security_smells"],
        on_event=on_event,
    )
    event_names = [n for n, _ in events]
    assert event_names == [
        "category_complete",
        "category_complete",
        "agent_complete",
    ]


@pytest.mark.unit
async def test_audit_drops_unknown_categories_silently(
    storage: InMemoryStorage, bundle
) -> None:
    """Unknown categories are filtered (defensive). Mix of one valid
    + one typo'd → only the valid one runs."""
    provider = MockAuditProvider()
    auditor = Auditor(
        provider=provider, storage=storage, model="openai/gpt-4o-mini"
    )
    record = await auditor.audit_agent(
        bundle=bundle,
        tenant_id="t1",
        categories=["ambiguous_prompts", "totally-not-a-category"],
    )
    assert record.categories == ["ambiguous_prompts"]
    assert len(provider.calls) == 1


@pytest.mark.unit
async def test_audit_record_has_summary_counts_by_severity(
    storage: InMemoryStorage, bundle
) -> None:
    """Two warn + one critical findings → record's findings list
    carries the expected severities (the JobView wraps into the summary
    rollup; here we assert the source data)."""
    provider = MockAuditProvider(
        findings_by_category={
            "ambiguous_prompts": [
                {"severity": "warn", "title": "w1", "description": "x", "suggestion": "y"},
                {"severity": "warn", "title": "w2", "description": "x", "suggestion": "y"},
                {"severity": "critical", "title": "c1", "description": "x", "suggestion": "y"},
            ]
        }
    )
    auditor = Auditor(
        provider=provider, storage=storage, model="openai/gpt-4o-mini"
    )
    record = await auditor.audit_agent(
        bundle=bundle, tenant_id="t1", categories=["ambiguous_prompts"]
    )
    severities = [f.severity.value for f in record.findings]
    assert severities.count("warn") == 2
    assert severities.count("critical") == 1
    # IDs are renumbered globally.
    ids = [f.id for f in record.findings]
    assert ids == ["f1", "f2", "f3"]
