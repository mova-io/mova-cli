"""Audit record storage — save/get/list round-trip + tenant isolation.

Mirrors ``tests/test_bench_storage.py``: the same three backends in
scope via the shared ``storage`` fixture in conftest.py —
``InMemoryStorage``, ``SqliteProvider``, and ``PostgresProvider``
(skipped when ``MOVATE_PG_TEST_URL`` is unset).

Asserts the contract the audit endpoints rely on:

* ``save_audit`` then ``get_audit`` round-trips every field, including
  the nested ``findings`` list + the ``location`` sub-record.
* ``get_audit`` is tenant-scoped — a wrong-tenant id returns ``None``
  (404-not-403 semantics, no existence leak).
* ``list_audits`` filters by ``scope_id`` and tenant, returns
  newest-first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from movate.core.models import (
    AuditFinding,
    AuditFindingLocation,
    AuditFindingSeverity,
    AuditRecord,
)


def _make_audit(
    *,
    audit_id: str | None = None,
    tenant_id: str = "tenant-a",
    scope_kind: str = "agent",
    scope_id: str = "demo-agent",
    created_at: datetime | None = None,
    findings: list[AuditFinding] | None = None,
) -> AuditRecord:
    return AuditRecord(
        audit_id=audit_id or f"audit_{uuid4().hex[:24]}",
        tenant_id=tenant_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        categories=["ambiguous_prompts", "security_smells"],
        severity_floor=AuditFindingSeverity.WARN,
        model="openai/gpt-4o-mini",
        budget_usd=1.0,
        findings=findings
        if findings is not None
        else [
            AuditFinding(
                id="f1",
                category="ambiguous_prompts",
                severity=AuditFindingSeverity.WARN,
                agent_name=scope_id,
                location=AuditFindingLocation(kind="prompt_line", line=12),
                title="Vague directive",
                description="Line 12 is too short.",
                suggestion="Be specific.",
                confidence="high",
            ),
        ],
        partial=False,
        tokens_used=1234,
        cost_usd=0.012,
        created_at=created_at or datetime.now(UTC),
    )


@pytest.mark.unit
async def test_save_and_get_audit(storage) -> None:
    a = _make_audit()
    await storage.save_audit(a)
    got = await storage.get_audit(a.audit_id, tenant_id="tenant-a")
    assert got is not None
    assert got.audit_id == a.audit_id
    assert got.scope_id == "demo-agent"
    assert got.severity_floor == AuditFindingSeverity.WARN
    assert got.model == "openai/gpt-4o-mini"
    assert len(got.findings) == 1
    f = got.findings[0]
    assert f.title == "Vague directive"
    assert f.location is not None
    assert f.location.kind == "prompt_line"
    assert f.location.line == 12


@pytest.mark.unit
async def test_get_audit_tenant_isolated(storage) -> None:
    """A wrong-tenant lookup returns None (no leak)."""
    a = _make_audit(tenant_id="tenant-a")
    await storage.save_audit(a)
    got = await storage.get_audit(a.audit_id, tenant_id="tenant-b")
    assert got is None


@pytest.mark.unit
async def test_list_audits_filters_by_scope_id_and_tenant(storage) -> None:
    a1 = _make_audit(tenant_id="tenant-a", scope_id="agent-1")
    a2 = _make_audit(tenant_id="tenant-a", scope_id="agent-2")
    a3 = _make_audit(tenant_id="tenant-b", scope_id="agent-1")
    await storage.save_audit(a1)
    await storage.save_audit(a2)
    await storage.save_audit(a3)

    rows = await storage.list_audits(tenant_id="tenant-a")
    assert {r.scope_id for r in rows} == {"agent-1", "agent-2"}

    one_agent = await storage.list_audits(tenant_id="tenant-a", scope_id="agent-1")
    assert [r.audit_id for r in one_agent] == [a1.audit_id]

    cross = await storage.list_audits(tenant_id="tenant-b")
    assert [r.audit_id for r in cross] == [a3.audit_id]
