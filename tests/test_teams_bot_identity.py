"""Teams bot identity binding (3.1.c) — crypto + storage + resolver + handler.

Layered coverage:

* **crypto** — encrypt/decrypt round-trip, missing-env failure, key
  rotation detection.
* **storage** — upsert / get / delete / decrypt, ciphertext is opaque.
* **resolver** — per-user MovateClient cache (LRU eviction,
  invalidation, missing binding).
* **handler** — `/movate connect` happy path, channel rejection,
  `/movate whoami` bound + unbound, `/movate disconnect`, `run`
  routing through the resolver vs fleet fallback.

Hermetic. Every test uses an in-memory sqlite + a known Fernet key
(no env var coupling). No HTTP, no real network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from movate.core.models import JobStatus
from movate.runtime.schemas import JobView, RunAccepted, RunView
from movate.teams_bot.activity import Activity
from movate.teams_bot.crypto import (
    ENV_ENCRYPTION_KEY,
    MissingEncryptionKeyError,
    TeamsCryptoError,
    decrypt_key,
    encrypt_key,
    generate_dev_key,
    get_fernet,
    hint_from_key,
)
from movate.teams_bot.handler import HandlerContext, handle_activity
from movate.teams_bot.identity import IdentityResolver
from movate.teams_bot.storage import (
    MissingBindingError,
    TeamsUsersStore,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_VALID_KEY = "mvt_test_movate01_KEYIDABCDEF_secretsecretsecretsecretsecretsecretsecre"


@pytest.fixture
def fernet():
    """Fresh dev Fernet — every test gets its own key, so a test that
    leaks ciphertext can't decrypt another test's data."""
    return get_fernet(key_override=generate_dev_key())


@pytest.fixture
async def store(fernet):
    """In-memory TeamsUsersStore. Lifecycle managed inline so each test
    sees a clean schema."""
    s = TeamsUsersStore(db_path=Path(":memory:"), fernet=fernet)
    await s.init()
    yield s
    await s.close()


def _personal_activity(text: str, *, aad_id: str = "aad-test-1") -> Activity:
    """Build an Activity in personal-scope (DM) — the scope where
    identity commands are accepted."""
    return Activity.model_validate(
        {
            "type": "message",
            "id": "act-1",
            "channelId": "msteams",
            "text": text,
            "from": {"id": "u1", "name": "tester", "aadObjectId": aad_id},
            "conversation": {"id": "c1", "conversationType": "personal"},
            "recipient": {"id": "b1", "name": "movate"},
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>movate</at>",
                    "mentioned": {"id": "b1", "name": "movate"},
                }
            ],
        }
    )


def _channel_activity(text: str, *, aad_id: str = "aad-test-1") -> Activity:
    """Same as _personal_activity but in channel scope — identity
    commands should be rejected here so the API key doesn't leak."""
    return Activity.model_validate(
        {
            "type": "message",
            "id": "act-2",
            "channelId": "msteams",
            "text": text,
            "from": {"id": "u1", "name": "tester", "aadObjectId": aad_id},
            "conversation": {"id": "c2", "conversationType": "channel"},
            "recipient": {"id": "b1", "name": "movate"},
            "entities": [
                {
                    "type": "mention",
                    "text": "<at>movate</at>",
                    "mentioned": {"id": "b1", "name": "movate"},
                }
            ],
        }
    )


def _card_text(card: dict[str, Any]) -> str:
    """Flatten Adaptive Card body to a string for substring assertions."""
    out: list[str] = []

    def walk(items: list[Any]) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t == "TextBlock":
                out.append(str(item.get("text", "")))
            elif t == "Container":
                walk(item.get("items", []) or [])
            elif t == "FactSet":
                for f in item.get("facts", []) or []:
                    out.append(f"{f.get('title', '')}: {f.get('value', '')}")

    walk(card.get("body", []) or [])
    return "\n".join(out)


# ===========================================================================
# crypto.py
# ===========================================================================


@pytest.mark.unit
def test_crypto_encrypt_decrypt_round_trip(fernet) -> None:
    """The core property: encrypt then decrypt returns the original."""
    plaintext = _VALID_KEY
    ct = encrypt_key(plaintext, fernet=fernet)
    assert ct != plaintext.encode("utf-8")  # actually encrypted
    assert decrypt_key(ct, fernet=fernet) == plaintext


@pytest.mark.unit
def test_crypto_decrypt_with_wrong_key_raises(fernet) -> None:
    """Rotation drift: ciphertext from key A can't be decrypted by key B.
    This is the failure path the handler renders as 'rebind your key'."""
    plaintext = _VALID_KEY
    ct = encrypt_key(plaintext, fernet=fernet)

    other = get_fernet(key_override=generate_dev_key())
    with pytest.raises(TeamsCryptoError, match="couldn't decrypt"):
        decrypt_key(ct, fernet=other)


@pytest.mark.unit
def test_crypto_missing_env_raises_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator forgot to set the env → typed error so the CLI can show
    the specific help message ('set the env var')."""
    monkeypatch.delenv(ENV_ENCRYPTION_KEY, raising=False)
    with pytest.raises(MissingEncryptionKeyError, match=ENV_ENCRYPTION_KEY):
        get_fernet()


@pytest.mark.unit
def test_crypto_invalid_env_key_raises_generic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operator set the env to something that isn't a valid Fernet key —
    different help message ('looks like wrong format')."""
    monkeypatch.setenv(ENV_ENCRYPTION_KEY, "not-a-valid-fernet-key")
    with pytest.raises(TeamsCryptoError, match="isn't a valid Fernet key"):
        get_fernet()


@pytest.mark.unit
def test_crypto_hint_from_key_returns_last_4() -> None:
    """The hint surfaces in cards so users can confirm which key
    they're bound to without revealing the key."""
    assert hint_from_key(_VALID_KEY) == _VALID_KEY[-4:]


@pytest.mark.unit
def test_crypto_hint_from_short_key_returns_full() -> None:
    """Defensive: a malformed key shorter than 4 chars shouldn't crash."""
    assert hint_from_key("abc") == "abc"


# ===========================================================================
# storage.py
# ===========================================================================


@pytest.mark.asyncio
async def test_store_upsert_and_get_returns_binding(store) -> None:
    binding = await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    assert binding.aad_object_id == "aad-x"
    assert binding.tenant_prefix == "movate01"
    assert binding.key_hint == _VALID_KEY[-4:]
    assert isinstance(binding.created_at, datetime)
    assert binding.created_at.tzinfo is not None

    fetched = await store.get_binding("aad-x")
    assert fetched is not None
    assert fetched == binding


@pytest.mark.asyncio
async def test_store_get_binding_returns_none_for_unknown_user(store) -> None:
    """Clean None return — the resolver uses this to decide 'unbound'."""
    assert await store.get_binding("never-bound") is None


@pytest.mark.asyncio
async def test_store_upsert_replaces_existing_binding(store) -> None:
    """``/movate connect`` over an existing binding swaps the key."""
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    other_key = _VALID_KEY[:-4] + "ZZZZ"
    new_binding = await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate02",
        api_key_plaintext=other_key,
    )
    assert new_binding.tenant_prefix == "movate02"
    assert new_binding.key_hint == "ZZZZ"
    # And the plaintext lookup returns the new key, not the old one.
    assert await store.get_decrypted_key("aad-x") == other_key


@pytest.mark.asyncio
async def test_store_get_decrypted_key_returns_plaintext(store) -> None:
    """The resolver's main read path — round-trip the original key."""
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    assert await store.get_decrypted_key("aad-x") == _VALID_KEY


@pytest.mark.asyncio
async def test_store_get_decrypted_key_raises_for_missing(store) -> None:
    """Distinct error from get_binding's None so callers can tell apart
    'no binding' from 'store layer broke'."""
    with pytest.raises(MissingBindingError):
        await store.get_decrypted_key("aad-x")


@pytest.mark.asyncio
async def test_store_delete_binding(store) -> None:
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    assert await store.delete_binding("aad-x") is True
    assert await store.get_binding("aad-x") is None
    # Re-deleting returns False (idempotent).
    assert await store.delete_binding("aad-x") is False


# ===========================================================================
# identity.py — resolver
# ===========================================================================


class _FakeMovateClient:
    """Lightweight stand-in for MovateClient — records construction args
    + supports aclose() so the resolver's cache eviction path works."""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_resolver_returns_none_for_unbound_user(store) -> None:
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    assert await resolver.client_for("never-bound") is None


@pytest.mark.asyncio
async def test_resolver_builds_client_for_bound_user(store) -> None:
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    client = await resolver.client_for("aad-x")
    assert client is not None
    assert client.api_key == _VALID_KEY  # type: ignore[attr-defined]
    assert client.base_url == "http://localhost:8000"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolver_caches_subsequent_lookups(store) -> None:
    """Second call returns the SAME object — pool stays warm."""
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    c1 = await resolver.client_for("aad-x")
    c2 = await resolver.client_for("aad-x")
    assert c1 is c2


@pytest.mark.asyncio
async def test_resolver_invalidate_drops_cached_client(store) -> None:
    """Post-/connect-rotate, the cached client carrying the OLD key must
    be evicted so the next /run picks up the new key."""
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    c1 = await resolver.client_for("aad-x")
    assert c1 is not None

    await resolver.invalidate("aad-x")
    assert c1.closed is True  # type: ignore[attr-defined]

    # Rebind with a new key → resolver builds a fresh client.
    new_key = _VALID_KEY[:-4] + "WXYZ"
    await store.upsert_binding(
        aad_object_id="aad-x",
        tenant_prefix="movate01",
        api_key_plaintext=new_key,
    )
    c2 = await resolver.client_for("aad-x")
    assert c2 is not None
    assert c2 is not c1
    assert c2.api_key == new_key  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolver_evicts_oldest_when_cache_full(store) -> None:
    """LRU semantics: cache_size=2, fill it with 3 users → first evicted."""
    for i in range(3):
        await store.upsert_binding(
            aad_object_id=f"aad-{i}",
            tenant_prefix="movate01",
            api_key_plaintext=_VALID_KEY[:-4] + f"{i:04d}",
        )
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        cache_size=2,
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    c0 = await resolver.client_for("aad-0")
    c1 = await resolver.client_for("aad-1")
    c2 = await resolver.client_for("aad-2")
    assert c0 is not None and c1 is not None and c2 is not None
    # Oldest (aad-0) was evicted on the c2 insert.
    assert c0.closed is True  # type: ignore[attr-defined]
    # c1 and c2 still in cache.
    assert c1.closed is False  # type: ignore[attr-defined]
    assert c2.closed is False  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_resolver_aclose_closes_every_cached_client(store) -> None:
    await store.upsert_binding(
        aad_object_id="aad-1",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    c = await resolver.client_for("aad-1")
    await resolver.aclose()
    assert c.closed is True  # type: ignore[attr-defined]


# ===========================================================================
# handler.py — connect / whoami / disconnect
# ===========================================================================


@pytest.fixture
async def identity_ctx(store):
    """HandlerContext wired with the store + a resolver using
    _FakeMovateClient. No runtime_client — only identity-path tests use
    this fixture."""
    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=_FakeMovateClient,  # type: ignore[arg-type]
    )
    yield HandlerContext(
        users_store=store,
        identity_resolver=resolver,
    )
    await resolver.aclose()


@pytest.mark.asyncio
async def test_connect_happy_path_binds_and_replies(identity_ctx, store) -> None:
    """End-to-end /movate connect: store the key, return a confirmation card."""
    activity = _personal_activity(f"<at>movate</at> connect {_VALID_KEY}")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    assert reply.attachments
    text = _card_text(reply.attachments[0].content)
    assert "Connected" in text
    assert "movate01" in text  # the tenant prefix from the key
    assert _VALID_KEY[-4:] in text  # last 4 chars surface in the card
    # The store actually carries the binding.
    binding = await store.get_binding("aad-test-1")
    assert binding is not None
    assert binding.tenant_prefix == "movate01"


@pytest.mark.asyncio
async def test_connect_in_channel_is_rejected(identity_ctx) -> None:
    """Channel scope must reject so the API key doesn't leak. Operator
    intent: protect users from accidentally pasting in a team channel."""
    activity = _channel_activity(f"<at>movate</at> connect {_VALID_KEY}")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "DM-only" in text


@pytest.mark.asyncio
async def test_connect_rejects_malformed_key(identity_ctx, store) -> None:
    """A user pastes the wrong thing → parse-api-key fails → card
    explains the expected shape. No binding stored."""
    activity = _personal_activity("<at>movate</at> connect not-a-real-key")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "doesn't look like" in text or "Malformed" in text
    assert await store.get_binding("aad-test-1") is None


@pytest.mark.asyncio
async def test_connect_missing_arg_returns_usage_card(identity_ctx) -> None:
    activity = _personal_activity("<at>movate</at> connect")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "missing API key" in text.lower() or "couldn't parse" in text.lower()


@pytest.mark.asyncio
async def test_connect_without_identity_wired_returns_config_error() -> None:
    """If the bot was started without enable_identity, /connect tells
    the operator how to fix it rather than crashing."""
    ctx_no_identity = HandlerContext()
    activity = _personal_activity(f"<at>movate</at> connect {_VALID_KEY}")
    reply = await handle_activity(activity, ctx_no_identity)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "not available" in text.lower()
    assert "MOVATE_TEAMS_ENCRYPTION_KEY" in text


@pytest.mark.asyncio
async def test_connect_rotation_clears_cached_client(identity_ctx, store) -> None:
    """Connect twice with different keys — second connect must invalidate
    the cached client so the next /run uses the new key."""
    activity1 = _personal_activity(f"<at>movate</at> connect {_VALID_KEY}")
    await handle_activity(activity1, identity_ctx)
    c1 = await identity_ctx.identity_resolver.client_for("aad-test-1")
    assert c1 is not None

    new_key = _VALID_KEY[:-4] + "WXYZ"
    activity2 = _personal_activity(f"<at>movate</at> connect {new_key}")
    await handle_activity(activity2, identity_ctx)
    # Old client was evicted on rebind.
    assert c1.closed is True  # type: ignore[attr-defined]
    c2 = await identity_ctx.identity_resolver.client_for("aad-test-1")
    assert c2 is not None
    assert c2.api_key == new_key  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_whoami_when_bound_returns_tenant_and_hint(identity_ctx, store) -> None:
    await store.upsert_binding(
        aad_object_id="aad-test-1",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    activity = _personal_activity("<at>movate</at> whoami")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "movate01" in text
    assert _VALID_KEY[-4:] in text


@pytest.mark.asyncio
async def test_whoami_when_unbound_returns_not_connected(identity_ctx) -> None:
    activity = _personal_activity("<at>movate</at> whoami")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "Not connected" in text


@pytest.mark.asyncio
async def test_whoami_in_channel_is_rejected(identity_ctx) -> None:
    """Even read-only commands are DM-only — they reveal which keys
    a user has bound, which is org-internal info."""
    activity = _channel_activity("<at>movate</at> whoami")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "DM-only" in text


@pytest.mark.asyncio
async def test_disconnect_removes_binding(identity_ctx, store) -> None:
    await store.upsert_binding(
        aad_object_id="aad-test-1",
        tenant_prefix="movate01",
        api_key_plaintext=_VALID_KEY,
    )
    activity = _personal_activity("<at>movate</at> disconnect")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "Disconnected" in text
    assert await store.get_binding("aad-test-1") is None


@pytest.mark.asyncio
async def test_disconnect_when_unbound_replies_no_binding(identity_ctx) -> None:
    """Idempotent disconnect — calling on an unbound user shouldn't
    crash; it just says 'nothing to remove'."""
    activity = _personal_activity("<at>movate</at> disconnect")
    reply = await handle_activity(activity, identity_ctx)
    assert reply is not None
    text = _card_text(reply.attachments[0].content)
    assert "Not connected" in text or "no binding" in text.lower()


# ===========================================================================
# handler.py — run routing through resolver vs fleet
# ===========================================================================


class _FakeRunningClient:
    """Stand-in MovateClient that scripts a successful run round-trip.
    Used in run-routing tests to assert which client the handler picked."""

    def __init__(self, *, base_url: str = "x", api_key: str = "fleet") -> None:
        self.api_key = api_key
        self.calls = 0

    async def submit_job(self, **kwargs: Any) -> RunAccepted:
        self.calls += 1
        return RunAccepted(job_id=f"job-{self.api_key}", status=JobStatus.QUEUED)

    async def wait_for_terminal(
        self,
        job_id: str,
        *,
        poll_interval_seconds: float = 1.0,
        max_wait_seconds: float | None = None,
    ) -> JobView:
        return JobView(
            job_id=job_id,
            kind="agent",  # type: ignore[arg-type]
            target="faq-agent",
            status=JobStatus.SUCCESS,
            input={},
            result_run_id=f"run-{self.api_key}",
            created_at=datetime(2026, 5, 13, tzinfo=UTC),
        )

    async def get_run(self, run_id: str) -> RunView:
        from movate.core.models import Metrics, TokenUsage  # noqa: PLC0415

        return RunView(
            run_id=run_id,
            job_id=f"job-{self.api_key}",
            agent="faq-agent",
            agent_version="0.1.0",
            prompt_hash="sha256:test",
            provider="openai/gpt-4o-mini-2024-07-18",
            provider_version="1.0",
            pricing_version="2026.05.01",
            status=JobStatus.SUCCESS,
            input={},
            output={"answer": f"used {self.api_key} key"},
            metrics=Metrics(tokens=TokenUsage(input=1, output=1), cost_usd=0.001, latency_ms=100),
            created_at=datetime(2026, 5, 13, tzinfo=UTC),
        )

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_run_routes_through_resolver_when_user_is_bound(store) -> None:
    """A bound user's /run uses THEIR key (not the fleet key) — the
    audit trail follows.

    Resolver client_factory returns a _FakeRunningClient stamped with the
    bound user's key, so the resulting RunView's output text proves the
    handler called the right client.
    """
    user_key = _VALID_KEY  # for tenant=movate01

    def factory(*, base_url: str, api_key: str) -> _FakeRunningClient:
        return _FakeRunningClient(base_url=base_url, api_key=api_key)

    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=factory,  # type: ignore[arg-type]
    )
    await store.upsert_binding(
        aad_object_id="aad-test-1",
        tenant_prefix="movate01",
        api_key_plaintext=user_key,
    )
    fleet = _FakeRunningClient(api_key="fleet-key")
    ctx = HandlerContext(
        runtime_client=fleet,  # type: ignore[arg-type]
        users_store=store,
        identity_resolver=resolver,
    )
    activity = _personal_activity('<at>movate</at> run faq-agent {"q":"hi"}')
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    # The handler used the BOUND user's client, not the fleet's.
    # Proof: the fake client stamps its api_key into the response body.
    card_text = _card_text(reply.attachments[0].content)
    assert user_key in card_text
    assert "fleet-key" not in card_text
    assert fleet.calls == 0


@pytest.mark.asyncio
async def test_run_falls_back_to_fleet_when_user_unbound(store) -> None:
    """Default (non-strict) mode: an unbound user's /run still works,
    using the fleet client. Lets internal users smoke-test before
    completing /connect."""

    def factory(*, base_url: str, api_key: str) -> _FakeRunningClient:
        return _FakeRunningClient(base_url=base_url, api_key=api_key)

    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=factory,  # type: ignore[arg-type]
    )
    fleet = _FakeRunningClient(api_key="fleet-key")
    ctx = HandlerContext(
        runtime_client=fleet,  # type: ignore[arg-type]
        users_store=store,
        identity_resolver=resolver,
        require_binding=False,  # default
    )
    activity = _personal_activity('<at>movate</at> run faq-agent {"q":"hi"}')
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card_text = _card_text(reply.attachments[0].content)
    assert "fleet-key" in card_text
    assert fleet.calls == 1


@pytest.mark.asyncio
async def test_run_rejects_unbound_user_in_strict_mode(store) -> None:
    """Multi-tenant deployments care about attribution — unbound users
    get a 'please connect' card instead of falling back."""

    def factory(*, base_url: str, api_key: str) -> _FakeRunningClient:
        return _FakeRunningClient(base_url=base_url, api_key=api_key)

    resolver = IdentityResolver(
        store=store,
        runtime_base_url="http://localhost:8000",
        client_factory=factory,  # type: ignore[arg-type]
    )
    fleet = _FakeRunningClient(api_key="fleet-key")
    ctx = HandlerContext(
        runtime_client=fleet,  # type: ignore[arg-type]
        users_store=store,
        identity_resolver=resolver,
        require_binding=True,
    )
    activity = _personal_activity('<at>movate</at> run faq-agent {"q":"hi"}')
    reply = await handle_activity(activity, ctx)
    assert reply is not None
    card_text = _card_text(reply.attachments[0].content)
    assert "Not connected" in card_text
    # Fleet was NOT used.
    assert fleet.calls == 0


# ===========================================================================
# Smoke: env coupling for /connect command in mostly-realistic config
# ===========================================================================


@pytest.mark.unit
def test_env_variable_constant_is_canonical_name() -> None:
    """Sanity: the env constant matches what we document publicly, so
    operators reading the README and the code see the same string."""
    assert ENV_ENCRYPTION_KEY == "MOVATE_TEAMS_ENCRYPTION_KEY"
