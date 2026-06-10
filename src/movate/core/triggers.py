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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from movate.core.auth import _urlsafe_b64, hash_secret
from movate.core.models import JobKind, JobRecord, Trigger

# ADR 100 D2: ``input_map``/``dedup_key`` dotted-path extraction deliberately
# REUSES the decision node's reader (ADR 094) so "dotted path into a dict,
# fail-soft on a missing segment" has exactly ONE semantics in the codebase.
from movate.core.workflow.decision import _MISSING, _read_field

# Entropy for the per-trigger secret. 256 bits — same as an API key secret;
# brute-forcing it (or the HMAC keyed by its hash) is economically infeasible.
TRIGGER_SECRET_BYTES = 32
TRIGGER_SALT_BYTES = 16

# The header an external caller sends, and the algorithm prefix we emit /
# tolerate on it (``sha256=<hex>``) — GitHub-webhook-compatible.
SIGNATURE_HEADER = "X-Movate-Signature"
_SIG_PREFIX = "sha256="

# ADR 100 D3: GitHub sends the same sha256-HMAC-over-body under its own
# header name. Accepted as an alias ONLY when ``X-Movate-Signature`` is
# absent (the operator pastes the minted signing key into GitHub as the
# webhook secret). Zero new crypto.
GITHUB_SIGNATURE_HEADER = "X-Hub-Signature-256"

# ADR 100 D3: the static-token header for ``auth_mode: "token"`` triggers —
# senders that cannot compute a per-body HMAC (ADO Service Hooks support
# static headers only). The presented value is the plaintext per-trigger
# secret; the server recomputes ``hash_secret(token, salt)`` and
# constant-time-compares against the stored ``secret_hash``. Explicitly
# weaker than HMAC (replayable until rotation) — pair with ``dedup_key``.
TOKEN_HEADER = "X-Movate-Trigger-Token"

# item 23 (ADR 017 D2 follow-up): the optional per-delivery idempotency key an
# external caller may send to suppress at-least-once retries — the GitHub
# ``X-GitHub-Delivery`` convention. When present, the fire endpoint dedups on
# ``(trigger_id, delivery_id)`` so a repeated delivery returns the SAME job
# without re-enqueuing. Absent → today's behavior (always enqueue). Capped to
# bound storage; an empty value is treated as absent.
DELIVERY_ID_HEADER = "X-Movate-Delivery-Id"
DELIVERY_ID_MAX_LEN = 200


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
    event_key: str | None = None,
    input_map: dict[str, str] | None = None,
    dedup_key: str | None = None,
    auth_mode: str = "hmac",
    enabled: bool = True,
    created_by: str | None = None,
) -> MintedTrigger:
    """Generate a new trigger + its one-time secret (ADR 017 D2).

    Mints a fresh 256-bit secret + salt, hashes the secret at rest
    (:func:`movate.core.auth.hash_secret`), and assembles the persisted
    :class:`Trigger`. The caller persists ``minted.record`` and shows
    ``minted.secret`` exactly once — it is irrecoverable afterward, exactly
    like a minted API key.

    ``event_key`` / ``input_map`` / ``dedup_key`` / ``auth_mode`` are the
    ADR 100 D2/D3 event-mapping + auth fields; all default to the
    pre-ADR-100 behavior (verbatim merge, header-only dedup, HMAC).
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
        event_key=event_key,
        input_map=input_map,
        dedup_key=dedup_key,
        auth_mode=auth_mode,  # type: ignore[arg-type]  # Literal validated by pydantic
        enabled=enabled,
        created_by=created_by,
    )
    return MintedTrigger(secret=secret, salt=salt, record=record)


def build_triggered_job(trigger: Trigger, event_body: dict[str, Any]) -> JobRecord:
    """Construct the ``JobKind.AGENT``/``WORKFLOW`` :class:`JobRecord` for a fire.

    Builds the job ``input`` from the trigger's mapping declaration
    (ADR 100 D2), in a deterministic, documented order:

    * **Neither ``event_key`` nor ``input_map`` set** (every pre-ADR-100
      trigger): the existing verbatim merge ``{**input_defaults,
      **event_body}`` — the event body wins on key collisions — preserved
      byte-for-byte.
    * **Otherwise**: ``{**input_defaults, **mapped_fields, **({event_key:
      event_body} if event_key else {})}`` — ``input_map`` extractions land
      over the defaults, and the whole raw body is nested under
      ``event_key`` (when set) so the workflow can still read unmapped
      fields without top-level state-key collisions. A missing ``input_map``
      path means the key is **omitted** (fail-soft, the decision node's
      ``_read_field`` semantics), never an exception.

    ``kind`` + ``target`` copy straight through. The result is the SAME
    shape ``POST /run`` (``runtime.app.v1_agent_run``) and
    :func:`movate.core.scheduler.build_scheduled_job` produce, so the
    worker's existing ``_execute_agent`` / ``_execute_workflow`` dispatch
    runs it with no new branch. The enqueued job is scoped to the trigger's
    own ``tenant_id`` (the external caller is otherwise tenant-less).
    """
    if trigger.event_key is None and trigger.input_map is None:
        merged: dict[str, Any] = {**trigger.input_defaults, **event_body}
    else:
        mapped: dict[str, Any] = {}
        for out_key, dotted in (trigger.input_map or {}).items():
            value = _read_field(event_body, dotted)
            if value is not _MISSING:
                mapped[out_key] = value
        merged = {**trigger.input_defaults, **mapped}
        if trigger.event_key is not None:
            merged[trigger.event_key] = event_body
    return JobRecord(
        job_id=str(uuid4()),
        tenant_id=trigger.tenant_id,
        kind=trigger.kind,
        target=trigger.target,
        input=merged,
    )


def resolve_body_delivery_id(trigger: Trigger, event_body: dict[str, Any]) -> str | None:
    """Resolve the body-sourced delivery id for dedup (ADR 100 D2).

    Used by the fire endpoint when the ``X-Movate-Delivery-Id`` header is
    absent and the trigger declares a ``dedup_key`` dotted path. The
    resolved value is stringified and capped at ``DELIVERY_ID_MAX_LEN``
    (truncated — still deterministic per event, so retries keep deduping).
    Unresolvable path / empty value / no ``dedup_key`` → ``None`` (no
    dedup — today's behavior), never an exception.
    """
    if trigger.dedup_key is None:
        return None
    value = _read_field(event_body, trigger.dedup_key)
    if value is _MISSING or value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:DELIVERY_ID_MAX_LEN]


def verify_fire_auth(trigger: Trigger, raw_body: bytes, headers: Mapping[str, str]) -> bool:
    """Authenticate one fire request per the trigger's ``auth_mode`` (ADR 100 D3).

    * ``"hmac"`` (default — today's behavior): body-bound HMAC via
      ``X-Movate-Signature``, with GitHub's ``X-Hub-Signature-256`` accepted
      as an alias ONLY when the movate header is absent. The static token
      header is NOT accepted in this mode.
    * ``"token"``: static ``X-Movate-Trigger-Token`` compared constant-time
      against the stored hash. The HMAC headers are NOT accepted in this
      mode (one declared auth path per trigger — no silent fallbacks).

    ``headers`` is any case-insensitive mapping (e.g. ``request.headers``).
    Returns ``False`` (never raises) on any missing/invalid credential.
    """
    if trigger.auth_mode == "token":
        return verify_token(trigger, headers.get(TOKEN_HEADER))
    presented = headers.get(SIGNATURE_HEADER) or headers.get(GITHUB_SIGNATURE_HEADER)
    return verify_signature(trigger, raw_body, presented)


def verify_token(trigger: Trigger, presented: str | None) -> bool:
    """Constant-time check of a presented ``X-Movate-Trigger-Token`` (ADR 100 D3).

    Recomputes ``hash_secret(presented, salt)`` and compares (timing-safe)
    against the stored ``secret_hash`` — the exact at-rest posture of HMAC
    mode, but the plaintext secret travels on the wire instead of a
    body-bound signature, so a captured request is replayable until
    rotation (pair ``auth_mode: "token"`` with ``dedup_key``). Returns
    ``False`` (never raises) when the header is absent/empty.
    """
    if not presented:
        return False
    presented_hash = hash_secret(presented.strip(), trigger.salt)
    return hmac.compare_digest(presented_hash, trigger.secret_hash)


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
    "DELIVERY_ID_HEADER",
    "DELIVERY_ID_MAX_LEN",
    "GITHUB_SIGNATURE_HEADER",
    "SIGNATURE_HEADER",
    "TOKEN_HEADER",
    "TRIGGER_SALT_BYTES",
    "TRIGGER_SECRET_BYTES",
    "MintedTrigger",
    "build_triggered_job",
    "expected_signature",
    "mint_trigger",
    "resolve_body_delivery_id",
    "signing_key",
    "verify_fire_auth",
    "verify_signature",
    "verify_token",
]
