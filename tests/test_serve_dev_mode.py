"""``mdk serve --dev`` — auto-seed a local dev key + print the playground line.

``--dev`` collapses the three-step local loop (mint a key → export it as
``MOVATE_SEED_API_KEY`` → paste the same key into the playground) into one
flag. These tests pin the contract:

* ``--dev`` SEEDS a valid key that authenticates through the *unchanged*
  ``auth_dependency`` (mint → insert → a real bearer request passes). Auth
  is NOT disabled — ``--dev`` is a convenience, not a bypass.
* ``--dev`` on a non-loopback ``--host`` is REFUSED before any work (never
  seed a known key on a reachable bind).
* Default ``serve`` (no ``--dev``) seeds nothing — behavior unchanged.
* The emitted playground command carries the exact env vars the playground
  reads (``MOVATE_API_KEY`` + ``MDK_PLAYGROUND_RUNTIME_URL``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from movate.cli.dev_key import (
    DEV_KEY_ENV,
    DEV_KEY_FILENAME,
    DEV_SCOPES,
    DEV_TENANT_ID,
    mint_and_seed_dev_key,
    playground_command,
    write_dev_key_file,
)
from movate.core.auth import parse_api_key
from movate.runtime import build_app
from movate.testing import InMemoryStorage

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# mint_and_seed_dev_key — the key authenticates through the real auth dep
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_dev_key_is_a_valid_movate_key() -> None:
    """The seeded key is a well-formed ``mvt_…`` key (parses cleanly) with
    the fixed local dev tenant + test env."""
    storage = InMemoryStorage()
    await storage.init()

    full_key = await mint_and_seed_dev_key(storage)

    assert full_key.startswith("mvt_")
    parsed = parse_api_key(full_key)  # must not raise
    assert parsed.env == DEV_KEY_ENV
    assert parsed.tenant_prefix == DEV_TENANT_ID[:8]


@pytest.mark.unit
async def test_dev_key_record_persisted_with_dev_scopes() -> None:
    """The minted record lands in storage with the broad-but-not-fleet
    ``read``/``run``/``admin`` grant."""
    storage = InMemoryStorage()
    await storage.init()

    full_key = await mint_and_seed_dev_key(storage)
    parsed = parse_api_key(full_key)

    record = await storage.get_api_key(parsed.key_id)
    assert record is not None
    assert sorted(record.scopes) == sorted(DEV_SCOPES)
    assert record.tenant_id == DEV_TENANT_ID


@pytest.mark.unit
async def test_dev_key_authenticates_through_auth_dependency(tmp_path: Path) -> None:
    """HEADLINE: a request bearing the seeded dev key passes the runtime's
    own ``auth_dependency`` — proving --dev seeds a key that *works*, with
    auth still enforced (the app is built with the standard auth dep)."""
    storage = InMemoryStorage()
    await storage.init()
    full_key = await mint_and_seed_dev_key(storage)

    client = TestClient(build_app(storage, agents_path=tmp_path))

    # GET /agents requires the `read` scope (granted) → 200 with the bearer.
    r = client.get("/agents", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 200, r.text


@pytest.mark.unit
async def test_dev_key_admin_scope_can_mint(tmp_path: Path) -> None:
    """The dev key carries `admin`, so the admin-only mint endpoint works —
    confirms the grant is broad enough for the local management loop."""
    storage = InMemoryStorage()
    await storage.init()
    full_key = await mint_and_seed_dev_key(storage)

    client = TestClient(build_app(storage, agents_path=tmp_path))
    r = client.post(
        "/api/v1/auth/keys",
        json={"label": "from-dev-key"},
        headers={"Authorization": f"Bearer {full_key}"},
    )
    assert r.status_code == 201, r.text


@pytest.mark.unit
async def test_dev_key_auth_still_enforced_without_bearer(tmp_path: Path) -> None:
    """Sanity: --dev does NOT disable auth. With the dev key seeded, an
    *unauthenticated* request still 401s — the key is a convenience, not a
    bypass."""
    storage = InMemoryStorage()
    await storage.init()
    await mint_and_seed_dev_key(storage)

    client = TestClient(build_app(storage, agents_path=tmp_path))
    r = client.get("/agents")  # no Authorization header
    assert r.status_code == 401, r.text


@pytest.mark.unit
async def test_each_dev_key_is_freshly_minted() -> None:
    """No hardcoded/static secret — two boots yield two distinct keys
    (random 256-bit secrets)."""
    storage = InMemoryStorage()
    await storage.init()

    k1 = await mint_and_seed_dev_key(storage)
    k2 = await mint_and_seed_dev_key(storage)
    assert k1 != k2


# ---------------------------------------------------------------------------
# playground_command — the exact one-liner emitted on startup
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_playground_command_carries_both_env_vars() -> None:
    """The emitted command sets the two env vars the playground reads:
    ``MOVATE_API_KEY`` (bearer) + ``MDK_PLAYGROUND_RUNTIME_URL`` (target)."""
    cmd = playground_command("mvt_test_devtenan_ABCDEFGHIJKL_secret", host="127.0.0.1", port=8000)
    assert cmd == (
        "MOVATE_API_KEY=mvt_test_devtenan_ABCDEFGHIJKL_secret "
        "MDK_PLAYGROUND_RUNTIME_URL=http://127.0.0.1:8000 "
        "mdk playground serve"
    )


@pytest.mark.unit
def test_playground_command_uses_the_actual_port() -> None:
    """A non-default port is reflected in the runtime URL the playground gets."""
    cmd = playground_command("mvt_test_devtenan_ABCDEFGHIJKL_secret", host="127.0.0.1", port=9999)
    assert "MDK_PLAYGROUND_RUNTIME_URL=http://127.0.0.1:9999" in cmd


# ---------------------------------------------------------------------------
# write_dev_key_file — project-local, never ~/.movate/credentials
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_dev_key_file_under_project_mdk(tmp_path: Path) -> None:
    """The dev key is written to ``<root>/.mdk/dev-runtime-key`` (project
    state dir), not the machine-global credentials store."""
    written = write_dev_key_file("mvt_test_devtenan_ABCDEFGHIJKL_secret", root=tmp_path)
    assert written is not None
    assert written == tmp_path / ".mdk" / DEV_KEY_FILENAME
    assert written.read_text(encoding="utf-8").strip() == "mvt_test_devtenan_ABCDEFGHIJKL_secret"


@pytest.mark.unit
def test_write_dev_key_file_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    """A write failure returns None rather than raising — the printed
    command is the primary UX; the file is a convenience."""

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "write_text", boom)
    assert write_dev_key_file("mvt_test_devtenan_ABCDEFGHIJKL_secret", root=tmp_path) is None


# ---------------------------------------------------------------------------
# CLI guard: --dev refused on a non-loopback host; default seeds nothing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestServeDevHostGuard:
    @pytest.mark.parametrize("bad_host", ["0.0.0.0", "10.0.0.5", "example.com", "::"])
    def test_dev_refused_on_non_loopback_host(self, bad_host: str) -> None:
        """``--dev`` on a non-loopback ``--host`` exits non-zero with a clear
        message and NEVER reaches the serve path (no key is minted)."""
        from movate.cli.main import app  # noqa: PLC0415

        result = runner.invoke(app, ["serve", "--dev", "--host", bad_host], env={"COLUMNS": "200"})
        assert result.exit_code != 0
        combined = result.stdout + result.stderr
        assert "--dev" in combined
        assert "loopback" in combined.lower()

    @pytest.mark.parametrize("ok_host", ["127.0.0.1", "localhost", "LOCALHOST", "::1"])
    def test_loopback_hosts_pass_the_guard(self, ok_host: str) -> None:
        """The loopback predicate accepts the recognized loopback literals
        (case-insensitive) so ``--dev`` is allowed on them."""
        from movate.cli.serve import _is_loopback_host  # noqa: PLC0415

        assert _is_loopback_host(ok_host) is True

    @pytest.mark.parametrize("bad_host", ["0.0.0.0", "10.0.0.5", "example.com", "::"])
    def test_non_loopback_hosts_fail_the_predicate(self, bad_host: str) -> None:
        from movate.cli.serve import _is_loopback_host  # noqa: PLC0415

        assert _is_loopback_host(bad_host) is False


@pytest.mark.unit
async def test_default_serve_seeds_no_dev_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ``--dev`` (and without MOVATE_SEED_API_KEY), nothing is
    seeded — default serve behavior is unchanged. The dev-key path is only
    reached when ``--dev`` is passed, so an empty store after the bootstrap
    seed proves no dev key was minted."""
    from movate.cli.serve import _seed_bootstrap_key  # noqa: PLC0415

    monkeypatch.delenv("MOVATE_SEED_API_KEY", raising=False)
    storage = InMemoryStorage()
    await storage.init()

    # Mirror _run_serve's non-dev startup: only the bootstrap seed runs, and
    # with no MOVATE_SEED_API_KEY it's a no-op. The dev-key mint is gated on
    # `dev`, which is False here, so it's never called.
    await _seed_bootstrap_key(storage)

    assert storage.api_keys == []
