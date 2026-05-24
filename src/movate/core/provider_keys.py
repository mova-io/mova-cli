"""Per-tenant BYOK provider-key encryption + resolution (ADR 018).

Each tenant can store its own OpenAI/Anthropic/etc. provider API key,
**encrypted at rest** (Fernet, reusing the Teams bot's pattern). At run time
the :class:`ProviderKeyResolver` resolves the key for ``(tenant_id, provider)``
with the precedence in ADR 018 D2:

1. the **tenant's own key** (decrypted from the store), else
2. the **shared fleet key** (the provider's env-default, e.g.
   ``OPENAI_API_KEY``) **iff** ``MOVATE_ALLOW_SHARED_PROVIDER_KEY`` is on
   (the default — back-compat), else
3. ``None`` — the caller passes nothing and the provider uses its own
   env-default exactly as today; or, when the shared-key fallback is *off*
   and the tenant has no key, a clear :class:`AuthError` (strict isolation).

This module is **pure + unit-testable**: no HTTP, no DB construction. The
resolver takes a :class:`StorageProvider` and reads one tenant-scoped row.

Encryption design (mirrors :mod:`movate.teams_bot.crypto`)
----------------------------------------------------------
* **Fernet (AES-128-CBC + HMAC-SHA256)** via the ``cryptography`` package —
  already a core dependency (no new dep). Rotation-friendly via ``MultiFernet``
  later if needed.
* **Data key from env**, not disk: ``MOVATE_PROVIDER_KEY_SECRET`` (the
  ``MDK_PROVIDER_KEY_SECRET`` alias is bridged by the CLI's env-alias shim).
* **Encrypt at the edge, decrypt only here.** The plaintext provider key is
  encrypted before ``save_tenant_provider_key`` and decrypted only inside
  :meth:`ProviderKeyResolver.resolve` — it never lands in a row, an API
  response, or a log line.

The resolver is an adapter seam: a future per-tenant cloud-KMS / Key-Vault
backend slots in behind it without touching callers (ADR 018 D2).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from movate.core.failures import AuthError
from movate.core.models import TenantProviderKey

if TYPE_CHECKING:
    from cryptography.fernet import Fernet

    from movate.storage.base import StorageProvider

# Env var holding the Fernet data-encryption key (ADR 018 D1). The CLI's
# ``sync_env_aliases`` bridges ``MDK_PROVIDER_KEY_SECRET`` ↔ this name, so
# reading the canonical ``MOVATE_*`` form transparently picks up either.
ENV_PROVIDER_KEY_SECRET = "MOVATE_PROVIDER_KEY_SECRET"

# Flag gating the shared-fleet-key fallback (ADR 018 D2). Default **on** for
# back-compat: a tenant with no key transparently uses the env-default fleet
# key — today's behavior. Set it to a falsey value ("0"/"false"/"no"/"off")
# to require per-tenant keys (strict isolation; a keyless tenant gets a clean
# AuthError instead of silently using the shared key).
ENV_ALLOW_SHARED = "MOVATE_ALLOW_SHARED_PROVIDER_KEY"

# Trailing chars kept in the masked fingerprint shown in listings (``…AbCd``).
# Long enough to disambiguate rotations, short enough to leak negligible
# entropy — same affordance as the Teams bot's key hint.
_FINGERPRINT_LEN = 4


class ProviderKeyError(Exception):
    """Raised when provider-key encryption / key resolution is misconfigured.

    Distinct from :class:`AuthError` (which is the *run-path* "no key
    configured" failure): this is an *operator* configuration error —
    ``MOVATE_PROVIDER_KEY_SECRET`` unset or malformed when a tenant key is
    being set/read. Surfaced at the edge (CLI / API) so the operator fixes
    the env, never silently swallowing a customer's key.
    """


def _truthy(value: str | None, *, default: bool) -> bool:
    """Parse an env flag; unset → ``default``; common falsey strings → False."""
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off", ""}


def shared_fallback_enabled() -> bool:
    """Whether the shared-fleet-key fallback is on (ADR 018 D2, default on)."""
    return _truthy(os.environ.get(ENV_ALLOW_SHARED), default=True)


def normalize_provider(provider: str) -> str:
    """Normalize a ``model.provider`` (or runtime) string to a BYOK key.

    The BYOK provider namespace is the LiteLLM-style *head* prefix — the part
    before the first ``/`` (``openai/gpt-4o`` → ``openai``,
    ``anthropic/claude-…`` → ``anthropic``) — lowercased. A bare value with no
    slash (e.g. a native-runtime model id already mapped to its family by the
    caller) passes through lowercased. So a tenant's ``openai`` key applies to
    every ``openai/<model>`` it runs.
    """
    return provider.split("/", 1)[0].strip().lower()


def fingerprint_of(plaintext: str) -> str:
    """Masked tail of a provider key, safe to display (``…AbCd``).

    Lets a listing show which key is configured without decrypting the stored
    ciphertext. A human affordance, not a security boundary — the tail alone
    can't be used to forge a key.
    """
    tail = plaintext[-_FINGERPRINT_LEN:] if len(plaintext) > _FINGERPRINT_LEN else plaintext
    return f"…{tail}"


def get_provider_fernet(*, key_override: bytes | str | None = None) -> Fernet:
    """Build a :class:`Fernet` from ``MOVATE_PROVIDER_KEY_SECRET``.

    Args:
        key_override: Tests pass an explicit key to avoid env coupling.
            Production leaves it ``None`` so the env var is read.

    Raises:
        ProviderKeyError: if no override AND the env var is unset, OR the key
            isn't a valid Fernet key (32 url-safe-base64-encoded bytes), OR
            the ``cryptography`` package is somehow unavailable.
    """
    try:
        from cryptography.fernet import Fernet  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - cryptography is a core dep
        raise ProviderKeyError(
            "the 'cryptography' package is required for per-tenant provider keys."
        ) from exc

    raw = key_override if key_override is not None else os.environ.get(ENV_PROVIDER_KEY_SECRET)
    if not raw:
        raise ProviderKeyError(
            f"{ENV_PROVIDER_KEY_SECRET} is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and export it before '
            "setting or resolving per-tenant provider keys."
        )

    key_bytes = raw if isinstance(raw, bytes) else raw.encode("ascii")
    try:
        return Fernet(key_bytes)
    except (ValueError, TypeError) as exc:
        raise ProviderKeyError(
            f"{ENV_PROVIDER_KEY_SECRET} is set but isn't a valid Fernet key "
            "(must be 32 url-safe-base64-encoded bytes)."
        ) from exc


def encrypt_provider_key(plaintext: str, *, fernet: Fernet | None = None) -> str:
    """Encrypt a plaintext provider key into a Fernet token (text).

    ``fernet`` is injectable for tests; production callers pass ``None`` and we
    resolve via :func:`get_provider_fernet`. Returns the token as ASCII text
    (TEXT-column safe) — the plaintext never persists.
    """
    f = fernet or get_provider_fernet()
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_provider_key(ciphertext: str, *, fernet: Fernet | None = None) -> str:
    """Decrypt a stored Fernet token back to the plaintext provider key.

    Used only inside :meth:`ProviderKeyResolver.resolve`. Raises
    :class:`ProviderKeyError` if the token was produced with a different key
    (the operator rotated/lost ``MOVATE_PROVIDER_KEY_SECRET`` → tenants must
    re-enter their keys).
    """
    f = fernet or get_provider_fernet()
    try:
        return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ProviderKeyError(
            "couldn't decrypt a stored provider key. "
            f"{ENV_PROVIDER_KEY_SECRET} may have rotated; the tenant must "
            "re-set the key with `mdk keys set <provider>`."
        ) from exc


def mint_tenant_provider_key(
    *,
    tenant_id: str,
    provider: str,
    plaintext: str,
    created_by: str | None = None,
    fernet: Fernet | None = None,
) -> TenantProviderKey:
    """Build a persistable :class:`TenantProviderKey` from a plaintext key.

    Encrypts ``plaintext`` at the edge (Fernet) and records a masked
    ``fingerprint`` for display. The caller persists the returned record via
    :meth:`StorageProvider.save_tenant_provider_key`; the plaintext is never
    stored or returned.
    """
    return TenantProviderKey(
        tenant_id=tenant_id,
        provider=normalize_provider(provider),
        ciphertext=encrypt_provider_key(plaintext, fernet=fernet),
        fingerprint=fingerprint_of(plaintext),
        created_by=created_by,
    )


class ProviderKeyResolver:
    """Resolve the provider API key for a run (ADR 018 D2).

    Precedence: tenant's own decrypted key → shared fleet env key (iff the
    fallback flag is on) → ``None`` (caller falls through to the provider's
    env default) OR a clear :class:`AuthError` when the fallback is off and the
    tenant has no key (strict isolation).

    Holds only a :class:`StorageProvider`; an optional ``fernet`` is injectable
    for tests. The resolver is the *only* place a stored key is decrypted.
    """

    def __init__(self, storage: StorageProvider, *, fernet: Fernet | None = None) -> None:
        self._storage = storage
        self._fernet = fernet

    async def resolve(self, tenant_id: str, provider: str) -> str | None:
        """Return the plaintext key for ``(tenant_id, provider)``, or ``None``.

        ``None`` means "the caller should pass nothing" — the provider then
        uses its own env-default credential, exactly as before BYOK. This is
        the back-compat no-config path.

        Raises:
            AuthError: only when the shared-key fallback is **off** AND the
                tenant has no key for this provider — strict per-tenant
                isolation, a keyless tenant fails closed with a clean message.
            ProviderKeyError: if a stored key can't be decrypted (the data key
                rotated / is misconfigured).
        """
        key = normalize_provider(provider)
        row = await self._storage.get_tenant_provider_key(key, tenant_id=tenant_id)
        if row is not None:
            # (a) tenant's own key — decrypt only here.
            return decrypt_provider_key(row.ciphertext, fernet=self._fernet)

        if shared_fallback_enabled():
            # (b) shared fleet key. We return None (not the env value) so the
            # provider's SDK reads its own env var — keeping the no-tenant-key
            # path byte-for-byte today's behavior (no key threaded through).
            return None

        # (c) strict isolation: fallback off + no tenant key → fail closed.
        # We refuse even when a shared env key is present (the operator
        # explicitly disabled sharing).
        raise AuthError(
            f"no API key configured for provider {key!r} and the shared "
            f"fleet-key fallback is disabled ({ENV_ALLOW_SHARED} is off): "
            f"set one with `mdk keys set {key}`."
        )


__all__ = [
    "ENV_ALLOW_SHARED",
    "ENV_PROVIDER_KEY_SECRET",
    "ProviderKeyError",
    "ProviderKeyResolver",
    "decrypt_provider_key",
    "encrypt_provider_key",
    "fingerprint_of",
    "get_provider_fernet",
    "mint_tenant_provider_key",
    "normalize_provider",
    "shared_fallback_enabled",
]
