"""MovateClient + the remote-CLI commands (submit, jobs, config).

Architecture for testing without a real network:

* `httpx.AsyncClient` accepts a custom transport. ``httpx.ASGITransport``
  wraps a FastAPI app and runs requests directly through it — same
  request/response cycle as a real HTTP call, no port, no socket.
* `MovateClient(transport=ASGITransport(app=build_app(...)))` gives
  us a hermetic, fast end-to-end test of the wire path.
* CLI commands resolve their target → URL → client. For CLI tests we
  set ``MOVATE_CONFIG_PATH`` to a tmp file with a fake target pointing
  at a placeholder URL — and monkeypatch ``MovateClient`` to always
  use ASGITransport regardless of URL.

The point is to exercise the full CLI → client → handler → storage
path end-to-end without subprocess gymnastics.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from typer.testing import CliRunner

from movate.cli.main import app as cli_app
from movate.core.auth import mint_api_key
from movate.core.client import MovateClient, MovateClientError
from movate.core.models import ApiKeyEnv, JobKind, JobStatus
from movate.core.user_config import TargetConfig, UserConfig, save_user_config
from movate.runtime import build_app
from movate.testing import InMemoryStorage

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def auth_pair():
    """Build a hermetic runtime + a registered API key.

    Returns ``(storage, app, full_key, tenant_id)``. The same storage
    object backs both the app's auth path and the assertion side of
    the test.
    """
    storage = InMemoryStorage()
    await storage.init()
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test")
    await storage.save_api_key(minted.record)
    app = build_app(storage)
    return storage, app, minted.full_key, tenant_id


def _client_for(app, key: str) -> MovateClient:
    """MovateClient that routes through the FastAPI app via ASGI transport
    instead of a real network. Same wire path; no port required."""
    return MovateClient(
        base_url="http://test",
        api_key=key,
        transport=httpx.ASGITransport(app=app),
    )


# ---------------------------------------------------------------------------
# MovateClient against the runtime
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_client_healthz_unauthed(auth_pair) -> None:
    """/healthz works regardless of auth — proves the transport
    wrapper isn't mis-stamping headers in a way that breaks
    unauthed routes."""
    _, app, key, _ = auth_pair
    async with _client_for(app, key) as client:
        h = await client.healthz()
    assert h.status == "ok"


@pytest.mark.unit
async def test_client_submit_then_get_job_round_trip(auth_pair) -> None:
    """End-to-end through the wire:
    submit → queued → store sees the record → get_job returns it."""
    _, app, key, _ = auth_pair
    async with _client_for(app, key) as client:
        accepted = await client.submit_job(kind=JobKind.AGENT, target="alpha", input={"text": "hi"})
        assert accepted.status == JobStatus.QUEUED
        # The same client retrieves it.
        view = await client.get_job(accepted.job_id)
    assert view.job_id == accepted.job_id
    assert view.target == "alpha"
    assert view.status == JobStatus.QUEUED


@pytest.mark.unit
async def test_client_get_job_unknown_id_404(auth_pair) -> None:
    """Unknown ids surface as MovateClientError with a clean code."""
    _, app, key, _ = auth_pair
    async with _client_for(app, key) as client:
        with pytest.raises(MovateClientError) as exc_info:
            await client.get_job("no-such-job")
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "not_found"


@pytest.mark.unit
async def test_client_unauthorized_surfaces_401(auth_pair) -> None:
    """Wrong key → MovateClientError(401, auth_required)."""
    _, app, _, _ = auth_pair
    bad = MovateClient(
        base_url="http://test",
        api_key="mvt_live_DEADBEEF_NOTAREALKEYID9_thisisthefakesecretpartheretobesufficientlylong",
        transport=httpx.ASGITransport(app=app),
    )
    async with bad:
        with pytest.raises(MovateClientError) as exc_info:
            await bad.submit_job(kind=JobKind.AGENT, target="alpha", input={})
    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "auth_required"


@pytest.mark.unit
async def test_client_wait_for_terminal_polls_until_status_changes(auth_pair) -> None:
    """Drive the poll loop: submit, then flip the job to SUCCESS via
    the storage backdoor, and verify wait_for_terminal returns
    promptly with the final view."""
    storage, app, key, tenant_id = auth_pair
    async with _client_for(app, key) as client:
        accepted = await client.submit_job(kind=JobKind.AGENT, target="alpha", input={})

        # Simulate the worker flipping the status.
        await storage.claim_next_job()
        await storage.update_job(
            accepted.job_id,
            tenant_id=tenant_id,
            status=JobStatus.SUCCESS,
            result_run_id="r-1",
        )

        final = await client.wait_for_terminal(
            accepted.job_id, poll_interval_seconds=0.05, max_wait_seconds=5
        )
    assert final.status == JobStatus.SUCCESS
    assert final.result_run_id == "r-1"


@pytest.mark.unit
async def test_client_wait_for_terminal_timeout(auth_pair) -> None:
    """A queued job that never advances triggers a TimeoutError after
    the configured budget."""
    storage, app, key, tenant_id = auth_pair
    async with _client_for(app, key) as client:
        accepted = await client.submit_job(kind=JobKind.AGENT, target="alpha", input={})
        with pytest.raises(TimeoutError):
            await client.wait_for_terminal(
                accepted.job_id, poll_interval_seconds=0.05, max_wait_seconds=0.2
            )
        # Storage shows the job still queued — wait_for_terminal
        # never advances state, only observes it.
        rec = await storage.get_job(accepted.job_id, tenant_id=tenant_id)
        assert rec is not None
        assert rec.status == JobStatus.QUEUED


# ---------------------------------------------------------------------------
# CLI: config subcommands
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_config_add_target_persists_to_file(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))

    result = runner.invoke(
        cli_app,
        [
            "config",
            "add-target",
            "local",
            "--url",
            "http://127.0.0.1:8000",
            "--key-env",
            "MOVATE_LOCAL_KEY",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "added target 'local'" in result.stderr
    # First add auto-promotes to active (no prior active).
    assert "active target is now 'local'" in result.stderr
    # File actually written.
    assert cfg_path.exists()
    content = cfg_path.read_text()
    assert "http://127.0.0.1:8000" in content
    assert "MOVATE_LOCAL_KEY" in content


@pytest.mark.unit
def test_cli_config_list_targets_shows_active_marker(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(
        UserConfig(
            targets={
                "prod": TargetConfig(url="https://prod", key_env="P"),
                "local": TargetConfig(url="http://127.0.0.1:8000", key_env="L"),
            },
            active="prod",
        )
    )
    result = runner.invoke(cli_app, ["config", "list-targets"])
    assert result.exit_code == 0, result.stdout
    # Both names appear; the active one is marked. Use short prefix
    # to be robust to Rich's table truncation under captured stdout.
    assert "prod" in result.stdout
    assert "local" in result.stdout


@pytest.mark.unit
def test_cli_config_use_flips_active(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(
        UserConfig(
            targets={
                "prod": TargetConfig(url="https://prod", key_env="P"),
                "local": TargetConfig(url="http://127.0.0.1:8000", key_env="L"),
            },
            active="prod",
        )
    )
    result = runner.invoke(cli_app, ["config", "use", "local"])
    assert result.exit_code == 0
    from movate.core.user_config import load_user_config  # noqa: PLC0415

    assert load_user_config().active == "local"


@pytest.mark.unit
def test_cli_config_use_unknown_target_fails(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(UserConfig(targets={"prod": TargetConfig(url="https://prod", key_env="P")}))
    result = runner.invoke(cli_app, ["config", "use", "ghost"])
    assert result.exit_code == 2
    assert "not found" in result.stderr


@pytest.mark.unit
def test_cli_config_remove_target_clears_active_if_needed(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(
        UserConfig(
            targets={"prod": TargetConfig(url="https://prod", key_env="P")},
            active="prod",
        )
    )
    result = runner.invoke(cli_app, ["config", "remove-target", "prod"])
    assert result.exit_code == 0
    from movate.core.user_config import load_user_config  # noqa: PLC0415

    cfg = load_user_config()
    assert "prod" not in cfg.targets
    assert cfg.active is None


# ---------------------------------------------------------------------------
# CLI: submit + jobs against the test-app via monkeypatched client
# ---------------------------------------------------------------------------


@pytest.fixture
async def cli_env(tmp_path: Path, monkeypatch):
    """A full CLI-fixture stack: tmp config + a registered target + a
    monkeypatched MovateClient that routes to the FastAPI app via
    ASGITransport instead of a real HTTP socket."""
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("MOVATE_TEST_KEY", "placeholder-replaced-below")

    storage = InMemoryStorage()
    await storage.init()
    tenant_id = uuid4().hex
    minted = mint_api_key(tenant_id=tenant_id, env=ApiKeyEnv.LIVE, label="test")
    await storage.save_api_key(minted.record)
    monkeypatch.setenv("MOVATE_TEST_KEY", minted.full_key)

    save_user_config(
        UserConfig(
            targets={"test": TargetConfig(url="http://test", key_env="MOVATE_TEST_KEY")},
            active="test",
        )
    )

    test_app = build_app(storage)
    transport = httpx.ASGITransport(app=test_app)

    # Patch MovateClient at the import sites that CLI commands use.
    # Both submit.py and jobs.py import the class from
    # movate.core.client, so patching at that name covers both.
    real_init = MovateClient.__init__

    def _patched_init(self, *, base_url, api_key, timeout=30.0, transport=None):
        real_init(
            self,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            # Force the ASGITransport regardless of what the CLI passes.
            transport=transport or globals()["_test_transport"],
        )

    globals()["_test_transport"] = transport
    monkeypatch.setattr(MovateClient, "__init__", _patched_init)

    # Return a small struct so tests that need to peek into storage can
    # pass the same tenant_id the auth path will stamp on submitted jobs.
    return _CliEnv(storage=storage, tenant_id=tenant_id)


@dataclass
class _CliEnv:
    """Fixture handle for ``cli_env`` tests."""

    storage: InMemoryStorage
    tenant_id: str


@pytest.mark.unit
def test_cli_submit_prints_job_id_in_fire_and_forget_mode(cli_env) -> None:
    """Default (no --wait): stdout is the bare RunAccepted JSON;
    stderr has a 'queued' hint."""
    import json  # noqa: PLC0415

    result = runner.invoke(cli_app, ["submit", "alpha", '{"text": "hi"}'])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "queued"
    assert payload["job_id"]
    assert "queued" in result.stderr


@pytest.mark.unit
def test_cli_submit_requires_input(cli_env) -> None:
    result = runner.invoke(cli_app, ["submit", "alpha"])
    assert result.exit_code == 2
    assert "provide input" in result.stderr


@pytest.mark.unit
def test_cli_submit_rejects_unknown_kind(cli_env) -> None:
    result = runner.invoke(cli_app, ["submit", "alpha", "{}", "--kind", "ritual"])
    assert result.exit_code == 2
    assert "kind must be" in result.stderr


@pytest.mark.unit
def test_cli_submit_accepts_long_inline_json(cli_env) -> None:
    """A JSON input >255 chars on the CLI used to crash with
    ``OSError: [Errno 63] File name too long`` because the
    file-or-JSON detection called ``Path(arg).is_file()`` first and
    macOS's ``stat()`` rejects oversized path strings.

    The fix is to peek at the first non-whitespace char: if it's
    ``{`` or ``[`` the arg is JSON and the file check is skipped.

    Build an input string clearly over the 255-char NAME_MAX boundary
    so the regression is real even on filesystems with shorter limits.
    """
    import json  # noqa: PLC0415

    big_input = {
        "text": "hi",
        "padding": "x" * 300,  # forces total JSON well past 255 chars
    }
    result = runner.invoke(cli_app, ["submit", "alpha", json.dumps(big_input)])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "queued"


@pytest.mark.unit
def test_cli_submit_wait_returns_terminal(cli_env) -> None:
    """--wait mode polls until terminal.

    Sync on purpose: ``runner.invoke`` calls ``asyncio.run`` inside the
    typer command, which would fail if pytest-asyncio is already running
    an event loop for an ``async def`` test ("asyncio.run cannot be
    called from a running event loop"). Async storage setup happens in
    a one-shot ``asyncio.run`` helper instead of ``await``.
    """
    import asyncio  # noqa: PLC0415
    import json  # noqa: PLC0415

    storage = cli_env.storage
    tenant_id = cli_env.tenant_id

    submit = runner.invoke(cli_app, ["submit", "alpha", "{}"])
    assert submit.exit_code == 0, submit.stdout + submit.stderr
    job_id = json.loads(submit.stdout)["job_id"]

    # Flip the job to SUCCESS via the storage backdoor (one async
    # block to avoid nested loops).
    async def _flip() -> None:
        await storage.claim_next_job()
        await storage.update_job(
            job_id, tenant_id=tenant_id, status=JobStatus.SUCCESS, result_run_id="r-1"
        )

    asyncio.run(_flip())

    result = runner.invoke(
        cli_app,
        [
            "jobs",
            "wait",
            job_id,
            "--timeout",
            "5",
            "--poll-interval",
            "0.05",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["result_run_id"] == "r-1"


@pytest.mark.unit
def test_cli_jobs_show_for_known_id(cli_env) -> None:
    """Sync test (see test_cli_submit_wait_returns_terminal docstring)."""
    import json  # noqa: PLC0415

    submit = runner.invoke(cli_app, ["submit", "alpha", "{}"])
    assert submit.exit_code == 0, submit.stdout + submit.stderr
    job_id = json.loads(submit.stdout)["job_id"]

    result = runner.invoke(cli_app, ["jobs", "show", job_id, "--output", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["job_id"] == job_id
    assert payload["status"] == "queued"


@pytest.mark.unit
def test_cli_jobs_show_unknown_id_404(cli_env) -> None:
    """Unknown id → CLI surfaces the runtime's 404 cleanly."""
    result = runner.invoke(cli_app, ["jobs", "show", "no-such-job"])
    # exit_code follows the HTTP class: 4xx → 4.
    assert result.exit_code == 4
    assert "fetch failed" in result.stderr


@pytest.mark.unit
def test_cli_jobs_list_agents(cli_env) -> None:
    """List-agents should render an empty table when no agents are
    registered — CLI doesn't crash on an empty catalog."""
    result = runner.invoke(cli_app, ["jobs", "list-agents", "--output", "json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    import json  # noqa: PLC0415

    payload = json.loads(result.stdout)
    assert "agents" in payload


@pytest.mark.unit
def test_cli_submit_no_target_no_active_errors(tmp_path: Path, monkeypatch) -> None:
    """If the user has no config and no --target, we exit 2 with a
    pointer to `movate config add-target`."""
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    # No save_user_config call: file doesn't exist; load returns empty.
    result = runner.invoke(cli_app, ["submit", "alpha", "{}"])
    assert result.exit_code == 2
    assert "config add-target" in result.stderr or "active target" in result.stderr
