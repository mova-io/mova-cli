"""API key crypto: mint, parse, verify.

Key format (decision locked in :doc:`/docs/v0.5-design`):

    mvt_<env>_<tenant_id_prefix>_<key_id>_<secret>

* ``mvt`` — literal prefix (grep-able, collision-resistant in dumps)
* ``env`` — ``live`` | ``test`` (hard separation of prod vs CI)
* ``tenant_id_prefix`` — first 8 chars of the tenant's UUID. Lets a
  human eyeball which tenant a key belongs to without DB lookup.
* ``key_id`` — 12 chars random base32 (per-key revocation handle,
  doubles as the table primary key).
* ``secret`` — 32 bytes (256 bits) URL-safe base64 = 43 chars after
  stripping padding. Brute-forcing this is economically infeasible.

Storage:

* ``secret_hash`` = ``sha256(salt || secret)`` hex digest (64 chars).
* ``salt`` = 16 random bytes URL-safe base64.

**Why SHA-256 and not Argon2id?** API keys are non-reusable opaque
secrets, not user passwords. Argon2-class hashes add per-request CPU
cost (60-100ms typical) for zero benefit when entropy is already
256 bits. The salt prevents rainbow tables; the entropy makes brute
force impossible. Constant-time comparison (``hmac.compare_digest``)
prevents timing attacks on the hash itself.

This module is **pure** — no DB, no I/O. Storage and HTTP middleware
compose the primitives below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from movate.core.models import ApiKeyEnv, ApiKeyRecord

# --- Constants -------------------------------------------------------------

KEY_PREFIX = "mvt"
TENANT_PREFIX_LEN = 8
KEY_ID_BYTES = 8  # 8 raw bytes → 13 base32 chars after stripping padding
SECRET_BYTES = 32  # 256 bits of entropy
SALT_BYTES = 16
KEY_DEFAULT_TTL_DAYS = 90

# --- Scopes (ADR 013 L2 / D3) ----------------------------------------------
#
# A small, FLAT, least-privilege scope set carried on both opaque keys
# (``ApiKeyRecord.scopes``) and OIDC tokens (mapped from a configured
# claim). No hierarchy — each scope is checked independently by
# :func:`movate.runtime.middleware.require_scope`. Adding a scope here is
# additive; never repurpose an existing string (it's an authorization
# contract).

SCOPE_READ = "read"
"""GET list/detail endpoints (catalog, runs, evals, models, pricing, …)."""
SCOPE_RUN = "run"
"""Submit an agent run (``POST /run``, ``POST /agents/{name}/runs``)."""
SCOPE_EVAL = "eval"
"""Kick off evals / benchmarks."""
SCOPE_KB_WRITE = "kb:write"
"""KB write ops — ingest / clear / reindex an agent corpus."""
SCOPE_ADMIN = "admin"
"""Tenant administration — create/update/delete agents, manage the
tenant's API keys, upload datasets."""
SCOPE_FLEET_ADMIN = "fleet-admin"
"""Cross-tenant / fleet-scoped administration. Historically the *only*
scope value; preserved verbatim so existing fleet keys keep working."""

ALL_SCOPES: frozenset[str] = frozenset(
    {SCOPE_READ, SCOPE_RUN, SCOPE_EVAL, SCOPE_KB_WRITE, SCOPE_ADMIN, SCOPE_FLEET_ADMIN}
)
"""The complete, valid scope set. Used to validate ``--scope`` input."""

# The back-compat grant for a key/record carrying NO explicit scopes
# (null/empty). Decided in ADR 013 D3: existing keys keep working on
# read/run/eval but get 403 on admin endpoints (deliberate least
# privilege — no legacy key silently gains admin). Applied as a
# READ-TIME default by :func:`effective_scopes`; never backfilled.
LEGACY_DEFAULT_SCOPES: frozenset[str] = frozenset({SCOPE_READ, SCOPE_RUN, SCOPE_EVAL})


def normalize_scopes(scopes: Iterable[str] | None) -> list[str]:
    """De-dupe + sort an iterable of scope strings into a stable list.

    Unknown scope strings are *not* rejected here — minting tooling
    validates against :data:`ALL_SCOPES` at the CLI/API edge, but the
    persistence + check path tolerates forward-compatible values so a
    newer key written by a newer minter doesn't break an older runtime.
    Empty / ``None`` → ``[]`` (the legacy-default sentinel).
    """
    if not scopes:
        return []
    return sorted({s.strip() for s in scopes if s and s.strip()})


def effective_scopes(record: ApiKeyRecord) -> set[str]:
    """Resolve the authorization scopes a stored key actually grants.

    The read-time back-compat rule (ADR 013 D3), in order:

    1. **Explicit ``scopes``** set on the record → use them verbatim.
    2. Else, **legacy single ``scope == "fleet-admin"``** (the only scope
       value that existed before this ADR — an all-powerful admin grant)
       → expand to the full :data:`ALL_SCOPES` set so existing fleet keys
       keep their admin reach.
    3. Else (both null/empty) → :data:`LEGACY_DEFAULT_SCOPES`
       (``{read, run, eval}``). Existing tenant keys keep working on
       read/run/eval but get 403 on admin endpoints.

    Pure function — no I/O. The middleware calls it once per request on
    the opaque-key path.
    """
    if record.scopes:
        return set(record.scopes)
    if record.scope == SCOPE_FLEET_ADMIN:
        return set(ALL_SCOPES)
    return set(LEGACY_DEFAULT_SCOPES)


# Token shape: mvt_<env>_<8 alnum>_<10-15 alnum>_<40-50 url-safe-b64>
# Hard prefix `mvt`, then four underscore-separated segments.
_KEY_RE = re.compile(
    r"^mvt_(?P<env>live|test)_(?P<tenant_prefix>[a-zA-Z0-9]{8})_"
    r"(?P<key_id>[A-Z0-9]{10,15})_(?P<secret>[A-Za-z0-9_\-]{40,50})$"
)


class ApiKeyParseError(Exception):
    """Raised when a presented key doesn't match the expected shape.

    The HTTP middleware translates this into ``401 Unauthorized``; never
    leak the parse failure detail to the caller (timing-attack risk).
    """


@dataclass(frozen=True)
class ParsedApiKey:
    """Decomposition of a presented key — the parts the caller can see.

    The verification path uses ``key_id`` to look up the stored record,
    then constant-time compares the presented ``secret`` against the
    stored hash.
    """

    env: ApiKeyEnv
    tenant_prefix: str
    key_id: str
    secret: str


@dataclass(frozen=True)
class MintedApiKey:
    """Output of :func:`mint_api_key` — both halves of the pair.

    ``full_key`` is shown to the user **once** at mint time and never
    again. ``record`` is what gets persisted (no plaintext secret).
    """

    full_key: str
    record: ApiKeyRecord


# --- Mint ------------------------------------------------------------------


def _b32_id(num_bytes: int) -> str:
    """Random base32 token with padding stripped (alphanumeric only)."""
    raw = secrets.token_bytes(num_bytes)
    return base64.b32encode(raw).rstrip(b"=").decode("ascii")


def _urlsafe_b64(num_bytes: int) -> str:
    """URL-safe base64, padding stripped."""
    raw = secrets.token_bytes(num_bytes)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def mint_api_key(
    *,
    tenant_id: str,
    env: ApiKeyEnv,
    label: str | None = None,
    ttl_days: int = KEY_DEFAULT_TTL_DAYS,
    scopes: Iterable[str] | None = None,
) -> MintedApiKey:
    """Generate a new API key for ``tenant_id``.

    ``tenant_id`` MUST be at least :data:`TENANT_PREFIX_LEN` characters
    so the prefix segment is well-defined; UUIDs satisfy this trivially.
    The full key is assembled but not stored — the caller persists
    ``minted.record`` and shows ``minted.full_key`` exactly once.

    ``ttl_days`` defaults to :data:`KEY_DEFAULT_TTL_DAYS` (90). Pass
    ``ttl_days=0`` to create a non-expiring key (legacy / service-account
    use — requires an explicit opt-in so expiry is never accidentally
    omitted).

    ``scopes`` (ADR 013 L2) is the least-privilege scope grant carried on
    the key. ``None``/empty mints a key with **no explicit scopes** — at
    check time :func:`effective_scopes` resolves that to the legacy
    default ``{read, run, eval}``. Callers that want admin reach must pass
    it explicitly (``scopes=["admin"]`` etc.). The scope list is
    normalized (de-duped + sorted) but not validated against
    :data:`ALL_SCOPES` here — the CLI/API edge does that.
    """
    if len(tenant_id) < TENANT_PREFIX_LEN:
        raise ValueError(f"tenant_id must be ≥ {TENANT_PREFIX_LEN} chars; got {len(tenant_id)!r}")

    key_id = _b32_id(KEY_ID_BYTES)
    secret = _urlsafe_b64(SECRET_BYTES)
    salt = _urlsafe_b64(SALT_BYTES)
    secret_hash = hash_secret(secret, salt)
    tenant_prefix = tenant_id[:TENANT_PREFIX_LEN]
    now = datetime.now(UTC)
    expires_at = now + timedelta(days=ttl_days) if ttl_days > 0 else None

    full_key = f"{KEY_PREFIX}_{env.value}_{tenant_prefix}_{key_id}_{secret}"
    record = ApiKeyRecord(
        key_id=key_id,
        tenant_id=tenant_id,
        env=env,
        secret_hash=secret_hash,
        salt=salt,
        label=label,
        created_at=now,
        expires_at=expires_at,
        scopes=normalize_scopes(scopes),
    )
    return MintedApiKey(full_key=full_key, record=record)


# --- Parse + verify --------------------------------------------------------


def parse_api_key(presented: str) -> ParsedApiKey:
    """Decompose a presented key string into its parts.

    Raises :class:`ApiKeyParseError` if the shape doesn't match — the
    HTTP middleware should map this to ``401`` without exposing the
    reason.
    """
    m = _KEY_RE.match(presented)
    if m is None:
        raise ApiKeyParseError("malformed api key")
    try:
        env = ApiKeyEnv(m.group("env"))
    except ValueError as exc:
        raise ApiKeyParseError("unknown env segment") from exc
    return ParsedApiKey(
        env=env,
        tenant_prefix=m.group("tenant_prefix"),
        key_id=m.group("key_id"),
        secret=m.group("secret"),
    )


def hash_secret(secret: str, salt: str) -> str:
    """SHA-256 of ``salt || secret`` as hex.

    The salt prevents rainbow tables across the whole table; the
    256-bit secret entropy makes brute force pointless. Both pieces
    are stored alongside each row so verification doesn't need a
    central key.
    """
    h = hashlib.sha256()
    h.update(salt.encode("ascii"))
    h.update(secret.encode("ascii"))
    return h.hexdigest()


def verify_secret(presented_secret: str, stored_hash: str, salt: str) -> bool:
    """Constant-time hash compare. Returns ``True`` on match.

    ``hmac.compare_digest`` is the right primitive even though we're
    not doing HMAC — it's the stdlib's branch-free comparison and
    timing-safe regardless of input length.
    """
    return hmac.compare_digest(hash_secret(presented_secret, salt), stored_hash)


# --- High-level "verify a presented key" -----------------------------------


@dataclass(frozen=True)
class VerificationFailure:
    """Why a key failed verification.

    The HTTP layer maps every variant to ``401`` — the discriminator is
    only for *internal* logging and metrics. Do **not** echo the reason
    back to the caller.
    """

    reason: str


def check_record(parsed: ParsedApiKey, record: ApiKeyRecord | None) -> VerificationFailure | None:
    """Validate a parsed key against a stored record.

    Returns ``None`` on success; a :class:`VerificationFailure` with a
    short reason on any failure. Pure function — no DB. Composes with
    a storage lookup at the call site::

        parsed = parse_api_key(presented)
        record = await storage.get_api_key(parsed.key_id)
        failure = check_record(parsed, record)
        if failure:
            raise HTTPException(401)

    This split lets tests assert each branch in isolation without
    standing up a storage backend.
    """
    if record is None:
        return VerificationFailure(reason="not_found")
    if record.revoked_at is not None:
        return VerificationFailure(reason="revoked")
    if record.expires_at is not None and record.expires_at < datetime.now(UTC):
        return VerificationFailure(reason="expired")
    if record.tenant_id[:TENANT_PREFIX_LEN] != parsed.tenant_prefix:
        # Tampered tenant prefix — somebody mangled the key.
        return VerificationFailure(reason="tenant_mismatch")
    if record.env != parsed.env:
        return VerificationFailure(reason="env_mismatch")
    if not verify_secret(parsed.secret, record.secret_hash, record.salt):
        return VerificationFailure(reason="bad_secret")
    return None


__all__ = [
    "ALL_SCOPES",
    "LEGACY_DEFAULT_SCOPES",
    "SCOPE_ADMIN",
    "SCOPE_EVAL",
    "SCOPE_FLEET_ADMIN",
    "SCOPE_KB_WRITE",
    "SCOPE_READ",
    "SCOPE_RUN",
    "ApiKeyParseError",
    "MintedApiKey",
    "ParsedApiKey",
    "VerificationFailure",
    "check_record",
    "effective_scopes",
    "hash_secret",
    "mint_api_key",
    "normalize_scopes",
    "parse_api_key",
    "verify_secret",
]
