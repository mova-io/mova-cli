"""``Trigger`` model — kind validation, extra=forbid, secret hashed (ADR 017 D2).

Mirrors tests/test_job_schedule_model.py. Asserts the trigger model:

* accepts ``kind=agent`` / ``kind=workflow`` and rejects ``eval`` / ``bench``
  with a clear validator error (eval has its own scheduler; bench isn't a
  trigger target).
* rejects unknown fields (``extra="forbid"``).
* defaults: enabled True, input_defaults {}, last_fired_at None.
* the minted secret is hashed at rest — the plaintext never appears on the
  persisted record, and the stored hash verifies against the plaintext.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from movate.core.auth import verify_secret
from movate.core.models import JobKind, Trigger
from movate.core.triggers import mint_trigger, signing_key


def _kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tenant_id": "tenant-a",
        "name": "zendesk",
        "kind": JobKind.AGENT,
        "target": "triage-agent",
        "secret_hash": "deadbeef",
        "salt": "saltsalt",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_accepts_agent_and_workflow() -> None:
    a = Trigger(**_kwargs(kind=JobKind.AGENT))  # type: ignore[arg-type]
    w = Trigger(**_kwargs(kind=JobKind.WORKFLOW))  # type: ignore[arg-type]
    assert a.kind == JobKind.AGENT
    assert w.kind == JobKind.WORKFLOW


@pytest.mark.unit
@pytest.mark.parametrize("bad_kind", [JobKind.EVAL, JobKind.BENCH])
def test_rejects_eval_and_bench_kind(bad_kind: JobKind) -> None:
    with pytest.raises(ValidationError) as exc:
        Trigger(**_kwargs(kind=bad_kind))  # type: ignore[arg-type]
    assert "agent" in str(exc.value) and "workflow" in str(exc.value)


@pytest.mark.unit
def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Trigger(**_kwargs(surprise="nope"))


@pytest.mark.unit
def test_defaults() -> None:
    t = Trigger(**_kwargs())
    assert t.enabled is True
    assert t.input_defaults == {}
    assert t.created_by is None
    assert t.last_fired_at is None
    # trigger_id is auto-assigned (uuid hex) and unique-ish.
    assert t.trigger_id and len(t.trigger_id) >= 16


@pytest.mark.unit
def test_minted_secret_is_hashed_not_plaintext() -> None:
    minted = mint_trigger(
        tenant_id="tenant-a",
        name="zendesk",
        kind=JobKind.AGENT,
        target="triage-agent",
    )
    rec = minted.record
    # Plaintext secret must NOT appear on the persisted record.
    assert minted.secret != rec.secret_hash
    assert minted.secret not in rec.model_dump_json()
    # The stored hash verifies against the plaintext (reuses the API-key path).
    assert verify_secret(minted.secret, rec.secret_hash, rec.salt) is True
    assert verify_secret("wrong", rec.secret_hash, rec.salt) is False
    # The HMAC signing key the caller derives equals the stored hash.
    assert signing_key(minted.secret, minted.salt) == rec.secret_hash


@pytest.mark.unit
def test_mint_rejects_eval_kind() -> None:
    with pytest.raises(ValidationError):
        mint_trigger(
            tenant_id="tenant-a",
            name="x",
            kind=JobKind.EVAL,
            target="agent",
        )


# ---------------------------------------------------------------------------
# ADR 100 D2/D3 — event mapping + auth-mode fields
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_adr100_fields_default_off() -> None:
    """Back-compat: every pre-ADR-100 trigger reads back with the defaults
    (verbatim merge / header-only dedup / hmac)."""
    t = Trigger(**_kwargs())
    assert t.event_key is None
    assert t.input_map is None
    assert t.dedup_key is None
    assert t.auth_mode == "hmac"


@pytest.mark.unit
def test_adr100_fields_round_trip() -> None:
    t = Trigger(
        **_kwargs(),
        event_key="event",
        input_map={"work_item_id": "resource.id"},
        dedup_key="id",
        auth_mode="token",
    )
    assert t.event_key == "event"
    assert t.input_map == {"work_item_id": "resource.id"}
    assert t.dedup_key == "id"
    assert t.auth_mode == "token"


@pytest.mark.unit
def test_auth_mode_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        Trigger(**_kwargs(), auth_mode="basic")
