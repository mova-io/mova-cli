"""Trigger core primitives — job builder + HMAC auth (ADR 017 D2).

Asserts:

* ``build_triggered_job`` merges ``input_defaults`` UNDER the event body
  (event wins), and copies kind/target/tenant straight through — the same
  JobRecord shape ``POST /run`` + the scheduler produce.
* the HMAC-over-body signature scheme: a valid signature verifies, a bad one
  doesn't, an absent header doesn't, and the ``sha256=`` prefix is tolerated.
"""

from __future__ import annotations

import pytest

from movate.core.models import JobKind, JobStatus
from movate.core.triggers import (
    DELIVERY_ID_MAX_LEN,
    build_triggered_job,
    expected_signature,
    mint_trigger,
    resolve_body_delivery_id,
    signing_key,
    verify_signature,
    verify_token,
)


def _minted(kind: JobKind = JobKind.AGENT, **defaults):
    return mint_trigger(
        tenant_id="tenant-a",
        name="zendesk",
        kind=kind,
        target="triage-agent",
        input_defaults=defaults or None,
    )


# ---------------------------------------------------------------------------
# build_triggered_job
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_job_merges_event_over_defaults() -> None:
    minted = _minted(source="zendesk", priority="low")
    job = build_triggered_job(minted.record, {"priority": "high", "ticket": 42})
    # Event body overrides the default on a key collision; defaults fill the rest.
    assert job.input == {"source": "zendesk", "priority": "high", "ticket": 42}


@pytest.mark.unit
def test_build_job_empty_event_uses_defaults() -> None:
    minted = _minted(source="zendesk")
    job = build_triggered_job(minted.record, {})
    assert job.input == {"source": "zendesk"}


@pytest.mark.unit
def test_build_job_kind_target_tenant_passthrough() -> None:
    minted = _minted(kind=JobKind.WORKFLOW)
    job = build_triggered_job(minted.record, {"x": 1})
    assert job.kind == JobKind.WORKFLOW
    assert job.target == "triage-agent"
    assert job.tenant_id == "tenant-a"
    assert job.status == JobStatus.QUEUED
    assert job.job_id  # a fresh uuid


# ---------------------------------------------------------------------------
# HMAC-over-body auth
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_valid_signature_verifies() -> None:
    minted = _minted()
    body = b'{"ticket": 1}'
    key = signing_key(minted.secret, minted.salt)
    sig = expected_signature(key, body)  # "sha256=<hex>"
    assert verify_signature(minted.record, body, sig) is True


@pytest.mark.unit
def test_signature_tolerates_missing_prefix() -> None:
    minted = _minted()
    body = b'{"ticket": 1}'
    key = signing_key(minted.secret, minted.salt)
    bare_hex = expected_signature(key, body).removeprefix("sha256=")
    assert verify_signature(minted.record, body, bare_hex) is True


@pytest.mark.unit
def test_bad_signature_rejected() -> None:
    minted = _minted()
    body = b'{"ticket": 1}'
    assert verify_signature(minted.record, body, "sha256=deadbeef") is False


@pytest.mark.unit
def test_signature_is_body_bound() -> None:
    """A signature for one body must not validate a different body (replay guard)."""
    minted = _minted()
    key = signing_key(minted.secret, minted.salt)
    sig_for_a = expected_signature(key, b'{"a": 1}')
    assert verify_signature(minted.record, b'{"b": 2}', sig_for_a) is False


@pytest.mark.unit
def test_absent_header_rejected() -> None:
    minted = _minted()
    assert verify_signature(minted.record, b"{}", None) is False
    assert verify_signature(minted.record, b"{}", "") is False


# ---------------------------------------------------------------------------
# Event -> state mapping (ADR 100 D2)
# ---------------------------------------------------------------------------


def _mapped(**fields):
    """Mint a trigger with the ADR 100 mapping fields set."""
    return mint_trigger(
        tenant_id="tenant-a",
        name="ado-work-items",
        kind=JobKind.WORKFLOW,
        target="work-item-triage",
        **fields,
    )


@pytest.mark.unit
def test_build_job_event_key_nests_body() -> None:
    """event_key nests the WHOLE raw body under one state key — no top-level
    merge, no state-key collisions."""
    minted = _mapped(event_key="event", input_defaults={"source": "ado"})
    body = {"eventType": "workitem.created", "resource": {"id": 42}}
    job = build_triggered_job(minted.record, body)
    assert job.input == {"source": "ado", "event": body}


@pytest.mark.unit
def test_build_job_input_map_extracts_dotted_paths() -> None:
    minted = _mapped(input_map={"work_item_id": "resource.id", "event_type": "eventType"})
    job = build_triggered_job(
        minted.record,
        {"eventType": "workitem.created", "resource": {"id": 42, "url": "https://x"}},
    )
    # ONLY the mapped fields land (plus defaults) — never the raw body.
    assert job.input == {"work_item_id": 42, "event_type": "workitem.created"}


@pytest.mark.unit
def test_build_job_input_map_missing_path_omits_key() -> None:
    """Fail-soft (_read_field semantics): a missing path omits the key —
    the workflow's state schema then reports exactly what's missing."""
    minted = _mapped(input_map={"work_item_id": "resource.id", "missing": "no.such.path"})
    job = build_triggered_job(minted.record, {"resource": {"id": 7}})
    assert job.input == {"work_item_id": 7}
    assert "missing" not in job.input


@pytest.mark.unit
def test_build_job_composition_order_defaults_mapped_event_key() -> None:
    """Documented order: {**input_defaults, **mapped, **{event_key: body}} —
    mapped fields beat defaults; the event_key nesting beats both."""
    minted = _mapped(
        input_defaults={"source": "default", "event": "default-collides"},
        input_map={"source": "origin"},
        event_key="event",
    )
    body = {"origin": "mapped-wins", "x": 1}
    job = build_triggered_job(minted.record, body)
    # "source": mapped beat the default; "event": the nesting beat the
    # colliding default; NO top-level "x" (no verbatim merge in mapped mode).
    assert job.input == {"source": "mapped-wins", "event": body}


@pytest.mark.unit
def test_build_job_neither_set_preserves_verbatim_merge() -> None:
    """Back-compat: no event_key, no input_map -> the pre-ADR-100 verbatim
    merge, byte-for-byte (event body wins on key collisions)."""
    minted = _minted(source="zendesk", priority="low")
    assert minted.record.event_key is None and minted.record.input_map is None
    job = build_triggered_job(minted.record, {"priority": "high", "ticket": 42})
    assert job.input == {"source": "zendesk", "priority": "high", "ticket": 42}


# ---------------------------------------------------------------------------
# Body-sourced dedup id (ADR 100 D2)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_body_delivery_id_dotted_path() -> None:
    minted = _mapped(dedup_key="resource.id")
    assert resolve_body_delivery_id(minted.record, {"resource": {"id": 42}}) == "42"


@pytest.mark.unit
def test_resolve_body_delivery_id_top_level_ado_id() -> None:
    """The ADO Service Hooks case: the event id lives at body path `id`."""
    minted = _mapped(dedup_key="id")
    assert resolve_body_delivery_id(minted.record, {"id": "afa4a2af-7b21"}) == "afa4a2af-7b21"


@pytest.mark.unit
def test_resolve_body_delivery_id_unresolvable_is_none() -> None:
    """Missing path / null / empty -> None (no dedup, today's behavior)."""
    minted = _mapped(dedup_key="resource.id")
    assert resolve_body_delivery_id(minted.record, {}) is None
    assert resolve_body_delivery_id(minted.record, {"resource": {"id": None}}) is None
    assert resolve_body_delivery_id(minted.record, {"resource": {"id": "  "}}) is None


@pytest.mark.unit
def test_resolve_body_delivery_id_no_dedup_key_is_none() -> None:
    minted = _minted()
    assert resolve_body_delivery_id(minted.record, {"id": "x"}) is None


@pytest.mark.unit
def test_resolve_body_delivery_id_capped_at_max_len() -> None:
    """Over-long values truncate (deterministic per event, so retries still
    dedup) rather than silently disabling dedup."""
    minted = _mapped(dedup_key="id")
    resolved = resolve_body_delivery_id(minted.record, {"id": "x" * 500})
    assert resolved == "x" * DELIVERY_ID_MAX_LEN


# ---------------------------------------------------------------------------
# Static-token auth (ADR 100 D3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_verify_token_accepts_minted_secret() -> None:
    minted = _mapped(auth_mode="token")
    assert verify_token(minted.record, minted.secret) is True


@pytest.mark.unit
def test_verify_token_rejects_wrong_or_absent() -> None:
    minted = _mapped(auth_mode="token")
    assert verify_token(minted.record, "not-the-secret") is False
    assert verify_token(minted.record, None) is False
    assert verify_token(minted.record, "") is False


@pytest.mark.unit
def test_mint_trigger_mapping_fields_default_off() -> None:
    """mint_trigger without the new kwargs produces the pre-ADR-100 shape."""
    minted = _minted()
    assert minted.record.event_key is None
    assert minted.record.input_map is None
    assert minted.record.dedup_key is None
    assert minted.record.auth_mode == "hmac"
