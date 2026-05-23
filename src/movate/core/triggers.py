"""Event/webhook trigger primitives (ADR 017 D2).

A :class:`movate.core.models.Trigger` is the trigger sibling of
:class:`movate.core.models.JobSchedule`: both register a *standing* way to
enqueue an agent/workflow job. The scheduler fires on a cron cadence; a
trigger fires on an **inbound event** that an external system POSTs to a
stable movate URL.

This module is **pure** — no DB, no HTTP. It holds the reusable pieces the
runtime composes:

* :func:`mint_trigger` — generate a new per-trigger secret + the persisted
  :class:`Trigger` (hash-at-rest, plaintext shown once), mirroring
  :func:`movate.core.auth.mint_api_key`.
* :func:`build_triggered_job` — turn a trigger + an inbound event body into a
  :class:`JobRecord` of the right :class:`JobKind`, the **same** shape
  ``POST /run`` and :func:`movate.core.scheduler.build_scheduled_job`
  produce, so the enqueued job flows through the existing
  ``_execute_agent`` / ``_execute_workflow`` dispatch with no new branch.
* :func:`signing_key` / :func:`expected_signature` / :func:`verify_signature`
  — the HMAC-SHA256-over-body authentication of an inbound fire request.

Authentication design (the security model)
-------------------------------------------
The external caller has **no** ``mvt_*`` API key. It authenticates with the
**per-trigger secret**: at creation it is handed the plaintext ``secret`` plus
the (non-sensitive) ``salt`` exactly once. To fire, it sends the
``X-Movate-Signature: sha256=<hex>`` header, where ``<hex>`` is
``HMAC-SHA256(key, raw_request_body)`` and ``key`` is the
:func:`signing_key` derived as ``hash_secret(secret, salt)``.

Two properties this buys:

* **Hashed at rest (no plaintext secret stored).** We persist only
  ``secret_hash = hash_secret(secret, salt)`` and the ``salt`` — the plaintext
  ``secret`` is shown once and never stored (``hash_secret`` / SHA-256 is
  one-way, so the persisted row can't be turned back into the secret the
  operator copied). This reuses the exact API-key hashing primitives
  (:func:`movate.core.auth.hash_secret`).
* **Body-bound, secret never on the wire.** The signature is over the *raw
  body*, so a captured request can't be replayed against a different payload,
  and the secret itself never travels on the wire. The server verifies by
  recomputing the HMAC with the stored ``secret_hash`` (which equals the
  caller's ``signing_key``) and :func:`hmac.compare_digest`-comparing.

Note that, as with every HMAC-over-body webhook scheme, the value the server
must hold to *verify* (here ``secret_hash``) is also sufficient to *forge* a
signature — the hashing buys "the operator's copied secret is not recoverable
from the DB", not "a DB read can't forge". A stronger at-rest posture
(encrypting the verification key) is a documented follow-up, as is replay
suppression (a delivery-id / nonce store to drop duplicate events).
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from movate.core.auth import _urlsafe_b64, hash_secret
from movate.core.models import JobKind, JobRecord, Trigger

# Entropy for the per-trigger secret. 256 bits — same as an API key secret;
# brute-forcing it (or the HMAC keyed by its hash) is economically infeasible.
TRIGGER_SECRET_BYTES = 32
TRIGGER_SALT_BYTES = 16

# The header an external caller sends, and the algorithm prefix we emit /
# tolerate on it (``sha256=<hex>``) — GitHub-webhook-compatible.
SIGNATURE_HEADER = "X-Movate-Signature"
_SIG_PREFIX = "sha256="


@dataclass(frozen=True)
class MintedTrigger:
    """Output of :func:`mint_trigger` — the parts shown once + the row stored.

    ``secret`` + ``salt`` are handed to the operator **once** at creation; the
    caller derives the HMAC :func:`signing_key` from them. Only
    ``record.secret_hash`` + ``record.salt`` persist — the plaintext
    ``secret`` is never stored, exactly like a minted API key.
    """

    secret: str
    salt: str
    record: Trigger


def mint_trigger(
    *,
    tenant_id: str,
    name: str,
    kind: JobKind,
    target: str,
    input_defaults: dict[str, Any] | None = None,
    enabled: bool = True,
    created_by: str | None = None,
) -> MintedTrigger:
    """Generate a new trigger + its one-time secret (ADR 017 D2).

    Mints a fresh 256-bit secret + salt, hashes the secret at rest
    (:func:`movate.core.auth.hash_secret`), and assembles the persisted
    :class:`Trigger`. The caller persists ``minted.record`` and shows
    ``minted.secret`` exactly once — it is irrecoverable afterward, exactly
    like a minted API key.
    """
    secret = _urlsafe_b64(TRIGGER_SECRET_BYTES)
    salt = _urlsafe_b64(TRIGGER_SALT_BYTES)
    record = Trigger(
        tenant_id=tenant_id,
        name=name,
        trigger_id=uuid4().hex,
        kind=kind,
        target=target,
        secret_hash=hash_secret(secret, salt),
        salt=salt,
        input_defaults=input_defaults or {},
        enabled=enabled,
        created_by=created_by,
    )
    return MintedTrigger(secret=secret, salt=salt, record=record)


def build_triggered_job(trigger: Trigger, event_body: dict[str, Any]) -> JobRecord:
    """Construct the ``JobKind.AGENT``/``WORKFLOW`` :class:`JobRecord` for a fire.

    Merges the trigger's ``input_defaults`` then the inbound ``event_body``
    (the event body wins on key collisions) into the job ``input``, and
    copies ``kind`` + ``target`` straight through. The result is the SAME
    shape ``POST /run`` (``runtime.app.v1_agent_run``) and
    :func:`movate.core.scheduler.build_scheduled_job` produce, so the
    worker's existing ``_execute_agent`` / ``_execute_workflow`` dispatch
    runs it with no new branch. The enqueued job is scoped to the trigger's
    own ``tenant_id`` (the external caller is otherwise tenant-less).
    """
    merged: dict[str, Any] = {**trigger.input_defaults, **event_body}
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=trigger.tenant_id,
        kind=trigger.kind,
        target=trigger.target,
        input=merged,
    )


def signing_key(secret: str, salt: str) -> str:
    """The HMAC key a caller signs with — ``hash_secret(secret, salt)``.

    Equal to the stored ``Trigger.secret_hash``, so the server can verify the
    HMAC by recomputing it from what it already persists. The caller derives
    this from the one-time ``secret`` + ``salt`` it was handed at creation.
    """
    return hash_secret(secret, salt)


def expected_signature(key: str, raw_body: bytes) -> str:
    """The ``sha256=<hex>`` HMAC for ``raw_body`` under ``key``.

    ``key`` is the :func:`signing_key` (== ``Trigger.secret_hash``).
    Returned with the ``sha256=`` prefix so it matches the header shape the
    caller sends (the GitHub-webhook convention). The test suite signs
    requests with this exact function, mirroring a real caller.
    """
    digest = hmac.new(key.encode("ascii"), raw_body, hashlib.sha256).hexdigest()
    return f"{_SIG_PREFIX}{digest}"


def verify_signature(trigger: Trigger, raw_body: bytes, presented: str | None) -> bool:
    """Constant-time check of a presented ``X-Movate-Signature`` for a fire.

    Recomputes the expected HMAC over ``raw_body`` keyed by the trigger's
    stored ``secret_hash`` and compares it (timing-safe) to ``presented``.
    Tolerates the optional ``sha256=`` prefix on the presented value, and
    returns ``False`` (never raises) when the header is absent/empty — the
    fire endpoint maps ``False`` → 401.
    """
    if not presented:
        return False
    expected = expected_signature(trigger.secret_hash, raw_body)
    expected_hex = expected[len(_SIG_PREFIX) :]
    presented = presented.strip()
    presented_hex = (
        presented[len(_SIG_PREFIX) :] if presented.startswith(_SIG_PREFIX) else presented
    )
    return hmac.compare_digest(presented_hex, expected_hex)


__all__ = [
    "SIGNATURE_HEADER",
    "TRIGGER_SALT_BYTES",
    "TRIGGER_SECRET_BYTES",
    "MintedTrigger",
    "build_triggered_job",
    "expected_signature",
    "mint_trigger",
    "signing_key",
    "verify_signature",
]
