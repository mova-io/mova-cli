"""API key auth — mint/parse/verify crypto + storage CRUD + CLI integration.

Three concentric layers tested separately so each branch is asserted
without standing up the layers above it:

1. **Pure crypto** (``core/auth.py``): mint produces a parseable key,
   parse rejects malformed shapes, verify accepts the right secret and
   rejects every wrong variant.
2. **Storage round-trip** (`Storage Protocol`): save/get/list/revoke/touch
   parametrized over ``InMemoryStorage`` + ``SqliteProvider``.
3. **CLI** (``movate auth ...``): create-key prints the full key once,
   list shows it active, revoke flips it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from movate.cli.main import app
from movate.core.auth import (
    KEY_PREFIX,
    ApiKeyParseError,
    VerificationFailure,
    check_record,
    hash_secret,
    mint_api_key,
    parse_api_key,
    verify_secret,
)
from movate.core.models import ApiKeyEnv, ApiKeyRecord

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Pure crypto — mint, parse, verify
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mint_produces_parseable_key() -> None:
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test")

    # Shape: mvt_<env>_<8 alnum>_<key_id>_<secret>. The secret can contain
    # underscores (URL-safe base64 alphabet), so we use the regex parser
    # to validate shape — not naive split-by-underscore.
    assert minted.full_key.startswith(f"{KEY_PREFIX}_live_{tenant_id[:8]}_")
    parsed = parse_api_key(minted.full_key)
    assert parsed.env == ApiKeyEnv.LIVE
    assert parsed.tenant_prefix == tenant_id[:8]
    assert parsed.key_id == minted.record.key_id

    # The persisted record never contains the plaintext secret.
    assert minted.record.tenant_id == tenant_id
    assert minted.record.env == ApiKeyEnv.LIVE
    assert minted.record.label == "test"
    assert minted.record.revoked_at is None
    assert minted.record.last_used_at is None
    assert parsed.secret not in minted.record.secret_hash  # not stored plaintext


@pytest.mark.unit
def test_mint_rejects_short_tenant_id() -> None:
    """tenant_id < 8 chars can't form a valid prefix segment."""
    with pytest.raises(ValueError, match="tenant_id"):
        mint_api_key(tenant_id="short", env=ApiKeyEnv.LIVE)


@pytest.mark.unit
def test_parse_round_trip() -> None:
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.TEST)

    parsed = parse_api_key(minted.full_key)
    assert parsed.env == ApiKeyEnv.TEST
    assert parsed.tenant_prefix == tenant_id[:8]
    assert parsed.key_id == minted.record.key_id


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "garbage",
        "mvt_live_abc",  # too few segments
        "mvt_live_abc12345_KEY_secret_extra",  # too many segments
        "wrongprefix_live_abc12345_KEYID12345_secret-with-good-length-here-padding",
        "mvt_unknownenv_abc12345_KEYID12345_secret-with-good-length-here-padding",
        "mvt_live_TOO-SHORT_KEYID12345_secret-with-good-length-here-padding",
    ],
)
def test_parse_rejects_malformed_shapes(bad_key: str) -> None:
    with pytest.raises(ApiKeyParseError):
        parse_api_key(bad_key)


@pytest.mark.unit
def test_verify_secret_constant_time() -> None:
    """Truth table for hash_secret + verify_secret pair."""
    salt = "deadbeefdeadbeef"
    secret = "the-real-secret-token"
    h = hash_secret(secret, salt)

    assert verify_secret(secret, h, salt) is True
    assert verify_secret("wrong-secret", h, salt) is False
    # Same secret with a different salt → fails.
    assert verify_secret(secret, h, "differentsalt123") is False


# ---------------------------------------------------------------------------
# check_record — branch coverage for the verification decision tree
# ---------------------------------------------------------------------------


def _mint_pair(tenant_id: str, env: ApiKeyEnv = ApiKeyEnv.LIVE):
    """Helper: mint + return (parsed, record) for verification tests."""
    minted = mint_api_key(tenant_id=tenant_id, env=env)
    parsed = parse_api_key(minted.full_key)
    return parsed, minted.record


@pytest.mark.unit
def test_check_record_accepts_valid_pair() -> None:
    parsed, record = _mint_pair(tenant_id=uuid4().hex)
    assert check_record(parsed, record) is None


@pytest.mark.unit
def test_check_record_rejects_missing() -> None:
    parsed, _ = _mint_pair(tenant_id=uuid4().hex)
    failure = check_record(parsed, None)
    assert failure == VerificationFailure(reason="not_found")


@pytest.mark.unit
def test_check_record_rejects_revoked() -> None:
    parsed, record = _mint_pair(tenant_id=uuid4().hex)
    revoked = record.model_copy(update={"revoked_at": datetime.now(UTC)})
    failure = check_record(parsed, revoked)
    assert failure == VerificationFailure(reason="revoked")


@pytest.mark.unit
def test_check_record_rejects_tampered_tenant() -> None:
    """Caller swapped the tenant_prefix segment — record's actual tenant_id
    starts with something else, so the prefix-check trips."""
    parsed, record = _mint_pair(tenant_id=uuid4().hex)
    # Reach into the parsed dataclass to simulate tampering.
    tampered = type(parsed)(
        env=parsed.env,
        tenant_prefix="00000000",  # ← changed
        key_id=parsed.key_id,
        secret=parsed.secret,
    )
    failure = check_record(tampered, record)
    assert failure == VerificationFailure(reason="tenant_mismatch")


@pytest.mark.unit
def test_check_record_rejects_env_mismatch() -> None:
    """A live key presented as test (or vice versa) fails before secret check."""
    parsed_live, record_live = _mint_pair(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    parsed_test = type(parsed_live)(
        env=ApiKeyEnv.TEST,
        tenant_prefix=parsed_live.tenant_prefix,
        key_id=parsed_live.key_id,
        secret=parsed_live.secret,
    )
    failure = check_record(parsed_test, record_live)
    assert failure == VerificationFailure(reason="env_mismatch")


@pytest.mark.unit
def test_check_record_rejects_wrong_secret() -> None:
    parsed, record = _mint_pair(tenant_id=uuid4().hex)
    tampered = type(parsed)(
        env=parsed.env,
        tenant_prefix=parsed.tenant_prefix,
        key_id=parsed.key_id,
        secret="x" * 50,  # wrong secret, right shape
    )
    failure = check_record(tampered, record)
    assert failure == VerificationFailure(reason="bad_secret")


# ---------------------------------------------------------------------------
# Storage round-trip — uses the shared ``storage`` fixture from conftest.py
# (parametrized over memory + sqlite + postgres)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_save_and_get_api_key(storage) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.key_id == minted.record.key_id
    assert got.tenant_id == minted.record.tenant_id
    assert got.env == ApiKeyEnv.LIVE
    assert got.secret_hash == minted.record.secret_hash


@pytest.mark.unit
async def test_save_and_get_api_key_round_trips_scopes(storage) -> None:
    """ADR 013 L2: the ``scopes`` column round-trips on every backend
    (memory + sqlite + postgres-when-available)."""
    minted = mint_api_key(
        tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE, scopes=["admin", "read", "kb:write"]
    )
    await storage.save_api_key(minted.record)
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert sorted(got.scopes) == ["admin", "kb:write", "read"]


@pytest.mark.unit
async def test_legacy_null_scopes_round_trips_as_empty(storage) -> None:
    """A scopeless key stores no scopes and reads back as an empty list —
    indistinguishable from a pre-ADR-013 row, so ``effective_scopes`` can
    apply the legacy default at check time (no destructive backfill)."""
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.scopes == []


@pytest.mark.unit
async def test_get_api_key_returns_none_for_missing(storage) -> None:
    assert await storage.get_api_key("ghost") is None


@pytest.mark.unit
async def test_list_api_keys_filters_by_tenant_and_revoked(storage) -> None:
    a_active = mint_api_key(tenant_id="aaaaaaaa" + uuid4().hex, env=ApiKeyEnv.LIVE)
    a_revoked = mint_api_key(tenant_id=a_active.record.tenant_id, env=ApiKeyEnv.LIVE)
    b = mint_api_key(tenant_id="bbbbbbbb" + uuid4().hex, env=ApiKeyEnv.LIVE)
    for m in (a_active, a_revoked, b):
        await storage.save_api_key(m.record)

    await storage.revoke_api_key(a_revoked.record.key_id, tenant_id=a_active.record.tenant_id)

    # Default: tenant a, active only.
    rows = await storage.list_api_keys(tenant_id=a_active.record.tenant_id)
    assert {k.key_id for k in rows} == {a_active.record.key_id}

    # include_revoked=True surfaces both for tenant a, still excludes b.
    rows = await storage.list_api_keys(tenant_id=a_active.record.tenant_id, include_revoked=True)
    assert {k.key_id for k in rows} == {a_active.record.key_id, a_revoked.record.key_id}


@pytest.mark.unit
async def test_revoke_api_key_sets_revoked_at(storage) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)

    await storage.revoke_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.revoked_at is not None


@pytest.mark.unit
async def test_revoke_is_idempotent(storage) -> None:
    """Re-revoking a revoked key is a silent no-op (no exception, no clobber).

    Idempotency matters because retries in middleware should be safe.
    """
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)

    await storage.revoke_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)
    first_revoke_time = (await storage.get_api_key(minted.record.key_id)).revoked_at
    assert first_revoke_time is not None

    # second call
    await storage.revoke_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)
    second = await storage.get_api_key(minted.record.key_id)
    assert second is not None
    # revoked_at preserved (not overwritten with a fresh now() on re-revoke).
    assert second.revoked_at == first_revoke_time


@pytest.mark.unit
async def test_revoke_missing_key_is_noop(storage) -> None:
    """No exception when the key was never registered — middleware retry safety."""
    # Pass an arbitrary tenant_id; the row doesn't exist regardless.
    await storage.revoke_api_key("never-existed", tenant_id="any-tenant")  # must not raise


@pytest.mark.unit
async def test_touch_api_key_updates_last_used(storage) -> None:
    minted = mint_api_key(tenant_id=uuid4().hex, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)
    assert minted.record.last_used_at is None

    await storage.touch_api_key(minted.record.key_id, tenant_id=minted.record.tenant_id)
    got = await storage.get_api_key(minted.record.key_id)
    assert got is not None
    assert got.last_used_at is not None


# ---------------------------------------------------------------------------
# Storage + crypto end-to-end: mint, persist, verify
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_full_verify_path_against_storage(storage) -> None:
    """Mint → persist → re-parse the public key → look up record → check_record."""
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE)
    await storage.save_api_key(minted.record)

    parsed = parse_api_key(minted.full_key)
    record = await storage.get_api_key(parsed.key_id)
    assert record is not None
    failure = check_record(parsed, record)
    assert failure is None

    # And after revocation, the same key fails.
    await storage.revoke_api_key(parsed.key_id, tenant_id=tenant_id)
    record2 = await storage.get_api_key(parsed.key_id)
    failure2 = check_record(parsed, record2)
    assert failure2 == VerificationFailure(reason="revoked")


# ---------------------------------------------------------------------------
# CLI — movate auth create-key | revoke | list
#
# These tests exercise the same code paths the operator hits at the
# terminal. They use HOME redirection + MOVATE_DB so storage lands in
# tmp_path, not the user's real DB.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "movate.db"
    monkeypatch.setenv("MOVATE_DB", str(db))
    monkeypatch.setenv("HOME", str(tmp_path))
    return db


@pytest.mark.unit
def test_cli_auth_create_key_prints_full_key_once(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    result = runner.invoke(
        app,
        ["auth", "create-key", "--tenant-id", tenant_id, "--env", "live", "--label", "ci"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Full key on stdout for piping into a vault / clipboard.
    assert "mvt_live_" in result.stdout
    # The "save this now, never shown again" warning goes to stderr.
    assert "save this now" in result.stderr.lower() or "shown again" in result.stderr.lower()


@pytest.mark.unit
def test_cli_auth_list_shows_active_keys(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    create = runner.invoke(app, ["auth", "create-key", "--tenant-id", tenant_id, "--env", "live"])
    assert create.exit_code == 0

    result = runner.invoke(app, ["auth", "list-keys", "--tenant-id", tenant_id])
    assert result.exit_code == 0, result.stdout
    # Non-empty table: tenant_id appears at least once.
    assert tenant_id[:8] in result.stdout


@pytest.mark.unit
def test_cli_auth_revoke_marks_inactive(isolated_db: Path) -> None:
    tenant_id = uuid4().hex
    create = runner.invoke(
        app, ["auth", "create-key", "--tenant-id", tenant_id, "--env", "live", "--quiet"]
    )
    assert create.exit_code == 0
    # In quiet mode the key_id is the only thing on stdout.
    key_id = create.stdout.strip()

    # `-y` bypasses the destructive-op confirm prompt that we added
    # so revoke-key has the same scripting affordance as Linux `rm -f`.
    revoke = runner.invoke(app, ["auth", "revoke-key", key_id, "-y"])
    assert revoke.exit_code == 0, revoke.stdout

    # `list-keys` (default: active only) no longer shows the key.
    # Check by first-8-chars to be tolerant of Rich column truncation; the
    # full key_id is the unit-of-truth, but the table renderer may wrap.
    listed = runner.invoke(app, ["auth", "list-keys", "--tenant-id", tenant_id])
    assert listed.exit_code == 0
    assert "no keys found" in listed.stderr or key_id[:6] not in listed.stdout

    # `list-keys --include-revoked` does — and the revoked status string.
    listed_all = runner.invoke(
        app, ["auth", "list-keys", "--tenant-id", tenant_id, "--include-revoked"]
    )
    assert listed_all.exit_code == 0
    assert key_id[:6] in listed_all.stdout
    assert "revoked" in listed_all.stdout


@pytest.mark.unit
def test_cli_auth_create_key_rejects_short_tenant(isolated_db: Path) -> None:
    result = runner.invoke(app, ["auth", "create-key", "--tenant-id", "short", "--env", "live"])
    assert result.exit_code != 0
    assert "tenant_id" in result.stderr.lower()


def _make_record(*, key_id: str = "k", tenant_id: str = "t" * 8) -> ApiKeyRecord:
    """Reference: a hand-built record used to assert model invariants."""
    return ApiKeyRecord(
        key_id=key_id,
        tenant_id=tenant_id,
        env=ApiKeyEnv.LIVE,
        secret_hash="0" * 64,
        salt="0" * 22,
        created_at=datetime.now(UTC),
    )
