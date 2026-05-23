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
    build_triggered_job,
    expected_signature,
    mint_trigger,
    signing_key,
    verify_signature,
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
