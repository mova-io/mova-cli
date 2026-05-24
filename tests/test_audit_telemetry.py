"""Control-plane audit telemetry (item 35).

:func:`movate.tracing.record_audit_event` records security/ops-relevant
control-plane mutations to two channels:

1. the reliable structured ``movate.audit`` log (→ Azure Log Analytics), and
2. best-effort, an event on the active OTel span (→ App Insights correlation).

These tests pin three contracts:

* the helper emits a ``movate.audit`` INFO record carrying action/actor/
  tenant/target — and NEVER a secret-shaped value;
* it never raises whether or not OTel is installed / a span is active (the
  span-event path is best-effort);
* the five control-plane endpoints emit the matching audit event on success,
  and a non-admin caller is rejected (403) BEFORE any audit event fires.

caplog (not capsys) is the right capture here — movate emits through stdlib
``logging`` (see tests/conftest.py).
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest

from movate.tracing import record_audit_event

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from movate.core.auth import ALL_SCOPES, SCOPE_READ, ApiKeyEnv, mint_api_key
from movate.core.models import AgentBundleRecord
from movate.runtime import build_app
from movate.runtime.agent_resolver import content_hash
from movate.testing import InMemoryStorage

_AUDIT_LOGGER = "movate.audit"


def _otel_installed() -> bool:
    """Is the OTel API importable here? Mirrors test_tracing_propagation.py —
    in dev/CI (`uv sync --all-extras`) it's installed and we run the real
    span-event path; in a minimal build it isn't and we run the no-op path."""
    try:
        import opentelemetry.trace  # noqa: F401, PLC0415

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Unit: record_audit_event — the structured log channel
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_emits_movate_audit_record(caplog: pytest.LogCaptureFixture) -> None:
    """A single ``movate.audit`` INFO record carries the structured payload."""
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        record_audit_event(
            "api_key.mint",
            actor="key_actor_123",
            tenant_id="tenant_abc",
            target="key_target_456",
        )

    records = [r for r in caplog.records if r.name == _AUDIT_LOGGER]
    assert len(records) == 1
    rec = records[0]
    assert rec.levelno == logging.INFO
    # Structured payload rides on ``extra={"audit": ...}``.
    audit = rec.audit  # type: ignore[attr-defined]
    assert audit["action"] == "api_key.mint"
    assert audit["actor"] == "key_actor_123"
    assert audit["tenant_id"] == "tenant_abc"
    assert audit["target"] == "key_target_456"
    # The human-readable message keeps the repo's key=value style + is grep-able.
    assert "action=api_key.mint" in rec.getMessage()
    assert "actor=key_actor_123" in rec.getMessage()


@pytest.mark.unit
def test_no_secret_shaped_value_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """The no-secret guarantee: a real API key value (``mvt_...``) is never an
    argument we pass, so it must never appear in the audit trail. We assert the
    full captured text carries no ``mvt_`` token even when ids are present."""
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        record_audit_event(
            "api_key.rotate",
            actor="key_caller",
            tenant_id="tenant_xyz",
            target="key_old",
            successor_key_id="key_new",
        )
    # No plaintext key prefix anywhere in the emitted text.
    assert "mvt_" not in caplog.text
    rec = next(r for r in caplog.records if r.name == _AUDIT_LOGGER)
    audit = rec.audit  # type: ignore[attr-defined]
    assert audit["successor_key_id"] == "key_new"
    assert not any(isinstance(v, str) and v.startswith("mvt_") for v in audit.values())


@pytest.mark.unit
def test_optional_tenant_and_target_default_to_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """tenant_id / target are optional — omitting them logs ``None`` cleanly."""
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        record_audit_event("canary.promote", actor="key_a")
    rec = next(r for r in caplog.records if r.name == _AUDIT_LOGGER)
    audit = rec.audit  # type: ignore[attr-defined]
    assert audit["tenant_id"] is None
    assert audit["target"] is None


# ---------------------------------------------------------------------------
# Unit: best-effort span-event path never raises
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_does_not_raise_with_no_active_span() -> None:
    """No span active (and possibly no otel extra) → the span-event path is a
    no-op and the call returns cleanly."""
    record_audit_event("api_key.revoke", actor="key_a", tenant_id="t", target="k")


@pytest.mark.unit
@pytest.mark.skipif(not _otel_installed(), reason="needs OTel SDK")
def test_adds_event_to_active_span_when_recording() -> None:
    """With a real recording span, the audit fields land as a span event and
    the call still never raises.

    Uses a *local* TracerProvider via ``start_as_current_span`` — which sets
    the span on the current OTel **context** that ``get_current_span()`` reads
    — rather than the global ``set_tracer_provider`` (a one-time, process-wide
    mutation that would leak into other tests). Mirrors the in-memory-exporter
    convention in test_tracing_otel.py.
    """
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import (  # noqa: PLC0415
        SimpleSpanProcessor,
    )
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: PLC0415
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    # No global set_tracer_provider — start_as_current_span attaches the span
    # to the current context, which record_audit_event reads via
    # get_current_span(). No process-wide state mutated.
    with tracer.start_as_current_span("mint"):
        record_audit_event(
            "api_key.mint",
            actor="key_caller",
            tenant_id="tenant_abc",
            target="key_new",
        )

    finished: tuple[ReadableSpan, ...] = exporter.get_finished_spans()
    assert len(finished) == 1
    events = list(finished[0].events)
    assert any(e.name == "audit" for e in events)
    audit_event = next(e for e in events if e.name == "audit")
    assert audit_event.attributes["action"] == "api_key.mint"
    assert audit_event.attributes["actor"] == "key_caller"
    assert audit_event.attributes["target"] == "key_new"
    # Best-effort path must not have logged a secret either.
    assert all(
        not (isinstance(v, str) and v.startswith("mvt_")) for v in audit_event.attributes.values()
    )


# ---------------------------------------------------------------------------
# Endpoint-level: the 5 control-plane mutations emit the matching event
# ---------------------------------------------------------------------------


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


@pytest.fixture
def client(storage: InMemoryStorage) -> TestClient:
    return TestClient(build_app(storage))


@pytest.fixture
async def admin(storage: InMemoryStorage):
    tenant_id = uuid4().hex
    minted = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="audit-tests", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(minted.record)
    return {"Authorization": f"Bearer {minted.full_key}"}, tenant_id, minted.record.key_id


def _bundle(*, tenant_id: str, version: str, name: str = "bot") -> AgentBundleRecord:
    files = {
        "agent.yaml": (
            "api_version: movate/v1\n"
            "kind: Agent\n"
            f"name: {name}\n"
            f"version: {version}\n"
            "model:\n  provider: openai/gpt-4o-mini-2024-07-18\n"
            "prompt: ./prompt.md\n"
            "schema:\n  input:\n    text: string\n  output:\n    message: string\n"
        ),
        "prompt.md": "Reply to {{ input.text }}\n",
    }
    return AgentBundleRecord(
        name=name,
        tenant_id=tenant_id,
        version=version,
        created_by="tester",
        content_hash=content_hash(files),
        files=files,
    )


def _audit_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _AUDIT_LOGGER]


@pytest.mark.unit
def test_mint_endpoint_emits_audit_event(
    client: TestClient, admin, caplog: pytest.LogCaptureFixture
) -> None:
    header, tenant_id, caller_key_id = admin
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post(
            "/api/v1/auth/keys",
            json={"label": "ci-bot", "ttl_days": 90},
            headers=header,
        )
    assert resp.status_code == 201
    minted_key_id = resp.json()["key_id"]
    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "api_key.mint"
    assert audit["actor"] == caller_key_id
    assert audit["tenant_id"] == tenant_id
    assert audit["target"] == minted_key_id
    # The one-time full_key is never in the audit trail.
    assert resp.json()["full_key"] not in caplog.text
    assert "mvt_" not in caplog.text


@pytest.mark.unit
def test_revoke_endpoint_emits_audit_event(
    client: TestClient, admin, caplog: pytest.LogCaptureFixture
) -> None:
    header, _tenant_id, _ = admin
    minted = client.post("/api/v1/auth/keys", json={"label": "to-revoke"}, headers=header).json()
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.delete(f"/api/v1/auth/keys/{minted['key_id']}", headers=header)
    assert resp.status_code == 200
    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "api_key.revoke"
    assert audit["target"] == minted["key_id"]


@pytest.mark.unit
def test_rotate_endpoint_emits_audit_event(
    client: TestClient, admin, caplog: pytest.LogCaptureFixture
) -> None:
    header, _tenant_id, _ = admin
    minted = client.post("/api/v1/auth/keys", json={"label": "to-rotate"}, headers=header).json()
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post(f"/api/v1/auth/keys/{minted['key_id']}/rotate", json={}, headers=header)
    assert resp.status_code == 201, resp.text
    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "api_key.rotate"
    assert audit["target"] == minted["key_id"]
    assert audit["successor_key_id"] == resp.json()["key_id"]
    # Neither the old nor successor full_key leaks into the trail.
    assert resp.json()["full_key"] not in caplog.text


@pytest.mark.unit
async def test_promote_endpoint_emits_audit_event(
    client: TestClient, admin, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    header, tenant_id, _ = admin
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="1.0.0"))
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="2.0.0"))
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "champion_version": "1.0.0", "weight": 50},
        headers=header,
    )
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post("/api/v1/agents/bot/canary/promote", json={}, headers=header)
    assert resp.status_code == 200, resp.text
    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "canary.promote"
    assert audit["target"] == "bot@2.0.0"
    assert audit["mode"] == "assisted"


@pytest.mark.unit
async def test_rollback_endpoint_emits_audit_event(
    client: TestClient, admin, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    header, tenant_id, _ = admin
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="1.0.0"))
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="2.0.0"))
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "champion_version": "1.0.0", "weight": 80},
        headers=header,
    )
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post("/api/v1/agents/bot/canary/rollback", headers=header)
    assert resp.status_code == 200, resp.text
    recs = _audit_records(caplog)
    assert len(recs) == 1
    audit = recs[0].audit  # type: ignore[attr-defined]
    assert audit["action"] == "canary.rollback"
    assert audit["target"] == "bot@1.0.0"


# ---------------------------------------------------------------------------
# Scope guard unchanged: a non-admin caller is rejected BEFORE any audit event
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_non_admin_mint_is_403_with_no_audit_event(
    client: TestClient, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    tenant_id = uuid4().hex
    ro = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    ro_header = {"Authorization": f"Bearer {ro.full_key}"}
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post("/api/v1/auth/keys", json={"label": "nope"}, headers=ro_header)
    assert resp.status_code == 403
    # Scope guard fires before the mutation → no audit trail for a denied call.
    assert _audit_records(caplog) == []


@pytest.mark.unit
async def test_non_admin_promote_is_403_with_no_audit_event(
    client: TestClient, storage: InMemoryStorage, caplog: pytest.LogCaptureFixture
) -> None:
    tenant_id = uuid4().hex
    admin_key = mint_api_key(
        tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="adm", scopes=list(ALL_SCOPES)
    )
    await storage.save_api_key(admin_key.record)
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="1.0.0"))
    await storage.save_agent_bundle(_bundle(tenant_id=tenant_id, version="2.0.0"))
    client.post(
        "/api/v1/agents/bot/canary",
        json={"challenger_version": "2.0.0", "weight": 50},
        headers={"Authorization": f"Bearer {admin_key.full_key}"},
    )
    ro = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="ro", scopes=[SCOPE_READ])
    await storage.save_api_key(ro.record)
    with caplog.at_level(logging.INFO, logger=_AUDIT_LOGGER):
        resp = client.post(
            "/api/v1/agents/bot/canary/promote",
            json={},
            headers={"Authorization": f"Bearer {ro.full_key}"},
        )
    assert resp.status_code == 403
    assert _audit_records(caplog) == []
