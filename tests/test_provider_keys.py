"""Per-tenant BYOK provider-key crypto + resolver (ADR 018).

Covers:
* Fernet encrypt/decrypt round-trip + never-plaintext + bad-key handling.
* Masked fingerprint shape.
* ``ProviderKeyResolver`` precedence: tenant key → shared fallback (on) →
  None / AuthError (off), including the back-compat default-on behavior.
* Provider-string normalization (``openai/gpt-4o`` → ``openai``).
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from movate.core.failures import AuthError
from movate.core.provider_keys import (
    ENV_ALLOW_SHARED,
    ENV_PROVIDER_KEY_SECRET,
    ProviderKeyError,
    ProviderKeyResolver,
    decrypt_provider_key,
    encrypt_provider_key,
    fingerprint_of,
    get_provider_fernet,
    mint_tenant_provider_key,
    normalize_provider,
    shared_fallback_enabled,
)
from movate.testing import InMemoryStorage

_FERNET_KEY = Fernet.generate_key()


@pytest.fixture
async def storage() -> InMemoryStorage:
    s = InMemoryStorage()
    await s.init()
    return s


# ---------------------------------------------------------------------------
# Crypto round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_encrypt_decrypt_round_trip() -> None:
    f = Fernet(_FERNET_KEY)
    secret = "sk-test-1234567890ABCD"
    token = encrypt_provider_key(secret, fernet=f)
    # The ciphertext must NOT contain the plaintext.
    assert secret not in token
    assert token != secret
    assert decrypt_provider_key(token, fernet=f) == secret


@pytest.mark.unit
def test_decrypt_with_wrong_key_raises() -> None:
    token = encrypt_provider_key("sk-abc", fernet=Fernet(_FERNET_KEY))
    other = Fernet(Fernet.generate_key())
    with pytest.raises(ProviderKeyError):
        decrypt_provider_key(token, fernet=other)


@pytest.mark.unit
def test_get_fernet_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PROVIDER_KEY_SECRET, raising=False)
    with pytest.raises(ProviderKeyError):
        get_provider_fernet()


@pytest.mark.unit
def test_get_fernet_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PROVIDER_KEY_SECRET, _FERNET_KEY.decode())
    f = get_provider_fernet()
    assert decrypt_provider_key(encrypt_provider_key("x", fernet=f), fernet=f) == "x"


@pytest.mark.unit
def test_get_fernet_malformed_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PROVIDER_KEY_SECRET, "not-a-fernet-key")
    with pytest.raises(ProviderKeyError):
        get_provider_fernet()


@pytest.mark.unit
def test_fingerprint_masks_to_tail() -> None:
    assert fingerprint_of("sk-proj-XYZ-WXYZ") == "…WXYZ"
    # Short keys pass through (still masked-prefixed).
    assert fingerprint_of("ab") == "…ab"


@pytest.mark.unit
def test_normalize_provider() -> None:
    assert normalize_provider("openai/gpt-4o-mini") == "openai"
    assert normalize_provider("anthropic/claude-haiku-4-5") == "anthropic"
    assert normalize_provider("OpenAI") == "openai"
    assert normalize_provider("mistral") == "mistral"


# ---------------------------------------------------------------------------
# mint helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mint_encrypts_and_fingerprints() -> None:
    rec = mint_tenant_provider_key(
        tenant_id="t1",
        provider="openai/gpt-4o",
        plaintext="sk-secret-ABCD",
        created_by="key-1",
        fernet=Fernet(_FERNET_KEY),
    )
    assert rec.tenant_id == "t1"
    assert rec.provider == "openai"  # normalized
    assert rec.fingerprint == "…ABCD"
    assert "sk-secret-ABCD" not in rec.ciphertext  # never plaintext at rest
    assert decrypt_provider_key(rec.ciphertext, fernet=Fernet(_FERNET_KEY)) == "sk-secret-ABCD"


# ---------------------------------------------------------------------------
# Resolver precedence
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_resolve_returns_tenant_key_first(storage: InMemoryStorage) -> None:
    f = Fernet(_FERNET_KEY)
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(tenant_id="t1", provider="openai", plaintext="sk-tenant", fernet=f)
    )
    resolver = ProviderKeyResolver(storage, fernet=f)
    # Even when a shared env key exists, the tenant's own key wins.
    assert await resolver.resolve("t1", "openai/gpt-4o") == "sk-tenant"


@pytest.mark.unit
async def test_resolve_falls_back_to_none_when_shared_on(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default-on shared fallback: no tenant key → None (caller uses env default).

    This is the BACK-COMPAT path — the resolver returns None (not an env
    value), so the provider's own SDK reads its env var exactly as before BYOK.
    """
    monkeypatch.delenv(ENV_ALLOW_SHARED, raising=False)  # unset → default on
    assert shared_fallback_enabled() is True
    resolver = ProviderKeyResolver(storage, fernet=Fernet(_FERNET_KEY))
    assert await resolver.resolve("t1", "openai/gpt-4o") is None


@pytest.mark.unit
async def test_resolve_raises_when_shared_off_and_no_tenant_key(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_ALLOW_SHARED, "0")
    assert shared_fallback_enabled() is False
    resolver = ProviderKeyResolver(storage, fernet=Fernet(_FERNET_KEY))
    with pytest.raises(AuthError, match="no API key configured for provider 'openai'"):
        await resolver.resolve("t1", "openai/gpt-4o")


@pytest.mark.unit
async def test_resolve_tenant_key_works_even_when_shared_off(
    storage: InMemoryStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict isolation (fallback off) still serves a tenant's OWN key."""
    monkeypatch.setenv(ENV_ALLOW_SHARED, "off")
    f = Fernet(_FERNET_KEY)
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(tenant_id="t1", provider="anthropic", plaintext="sk-a", fernet=f)
    )
    resolver = ProviderKeyResolver(storage, fernet=f)
    assert await resolver.resolve("t1", "anthropic/claude-haiku-4-5") == "sk-a"


@pytest.mark.unit
async def test_resolve_is_tenant_isolated(storage: InMemoryStorage) -> None:
    f = Fernet(_FERNET_KEY)
    await storage.save_tenant_provider_key(
        mint_tenant_provider_key(tenant_id="t1", provider="openai", plaintext="sk-t1", fernet=f)
    )
    resolver = ProviderKeyResolver(storage, fernet=f)
    # t2 has no key → None (default-on fallback); never sees t1's key.
    assert await resolver.resolve("t2", "openai/gpt-4o") is None
    assert await resolver.resolve("t1", "openai/gpt-4o") == "sk-t1"
