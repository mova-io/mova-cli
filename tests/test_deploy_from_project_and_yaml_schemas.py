"""PR #95 — deploy from project folder + YAML shorthand schemas.

Two related features that close the last two demo-prep gaps:

A. **`mdk deploy` from a customer project folder** auto-detects
   agents-mode and uploads `agents/*/` bundles to the deployed
   runtime via `POST /api/v1/agents`. From the movate-cli source
   tree (Dockerfile present) it stays in runtime-mode and rebuilds
   the image, unchanged.

B. **YAML shorthand schemas** — extend the existing
   `compile_shorthand` compiler with ranges (`integer(0..12)`,
   `string(1..)`) and refs (`$dim` + top-level `$defs:`) so the
   readable form covers the lead-qualifier template's full BANT
   scoring shape. Loader reads `.yaml`/`.yml` schema files +
   shape-detects shorthand vs hand-written JSON Schema.
"""

from __future__ import annotations

import io
import json
import re
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from movate.cli import deploy as deploy_mod
from movate.cli.auth import RefreshRuntimeKeyError
from movate.cli.deploy import _resolve_deploy_mode
from movate.cli.main import app
from movate.core.loader import AgentLoadError, load_agent
from movate.core.schema_shorthand import (
    SchemaShorthandError,
    compile_shorthand,
)

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# A — deploy mode auto-detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeployModeResolution:
    def test_dockerfile_in_cwd_picks_runtime(self, tmp_path: Path) -> None:
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        assert _resolve_deploy_mode(mode="auto", cwd=tmp_path) == "runtime"

    def test_project_yaml_in_cwd_picks_agents(self, tmp_path: Path) -> None:
        (tmp_path / "project.yaml").write_text("name: demo\n")
        assert _resolve_deploy_mode(mode="auto", cwd=tmp_path) == "agents"

    def test_project_yaml_in_ancestor_picks_agents(self, tmp_path: Path) -> None:
        """Walking up: if a parent dir has project.yaml, we're still
        in a project. `mdk deploy` from `agents/faq/` works."""
        (tmp_path / "project.yaml").write_text("name: demo\n")
        nested = tmp_path / "agents" / "faq"
        nested.mkdir(parents=True)
        assert _resolve_deploy_mode(mode="auto", cwd=nested) == "agents"

    def test_explicit_runtime_flag_wins_over_project_marker(self, tmp_path: Path) -> None:
        """`--mode runtime` forces runtime mode even from a project dir
        — e.g. an operator running the rebuild from inside a project
        worktree for some reason."""
        (tmp_path / "project.yaml").write_text("name: demo\n")
        assert _resolve_deploy_mode(mode="runtime", cwd=tmp_path) == "runtime"

    def test_explicit_agents_flag_wins_over_dockerfile(self, tmp_path: Path) -> None:
        """`--mode agents` from inside the movate-cli source tree forces
        agents-mode (no rebuild) — useful for CI that doesn't want to
        re-roll the image just to push an agent."""
        (tmp_path / "Dockerfile").write_text("FROM scratch\n")
        assert _resolve_deploy_mode(mode="agents", cwd=tmp_path) == "agents"

    def test_neither_marker_defaults_to_runtime(self, tmp_path: Path) -> None:
        """No Dockerfile + no project.yaml — fall through to runtime
        so the downstream preflight surfaces its hint about the missing
        Dockerfile."""
        assert _resolve_deploy_mode(mode="auto", cwd=tmp_path) == "runtime"


# ---------------------------------------------------------------------------
# A — agents-mode dry-run end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_agents_dry_run_emits_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From a project dir with at least one agent, `mdk deploy
    --target X --dry-run` should auto-detect agents-mode and emit
    the canonical summary line WITHOUT hitting the network."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    # Scaffold project + add an agent.
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    # Register a target.
    result = runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0
    # Dry-run from inside the project — mode auto-detects as agents.
    result = runner.invoke(app, ["deploy", "--target", "fake", "--dry-run"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "mode=agents" in combined
    assert "agents=1" in combined
    assert "dry_run=true" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_deploy_agents_dry_run_vacuous_pass_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project with zero agents — agents-mode deploy should be a
    vacuous-pass (ok=true, agents=0), not an error."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "empty-proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "empty-proj")
    runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    result = runner.invoke(app, ["deploy", "--target", "fake", "--dry-run"], env={"COLUMNS": "200"})
    # Empty project means nothing to upload — but mode resolution should
    # still pick agents-mode and emit a sane summary.
    combined = result.stdout + result.stderr
    assert "no agents found" in combined.lower() or "agents=0" in combined


@pytest.mark.unit
def test_deploy_mode_invalid_value_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--mode foo` should error with a usage hint, not silently
    fall through to one of the valid modes."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.chdir(tmp_path)
    runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    result = runner.invoke(
        app,
        ["deploy", "--target", "fake", "--mode", "bogus", "--dry-run"],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 2
    combined = result.stdout + result.stderr
    assert "must be" in combined.lower() and "bogus" in combined


# ---------------------------------------------------------------------------
# A — 401 auto-recovery: deploy mints a fresh key inside the pod + retries
# ---------------------------------------------------------------------------


def _scaffold_project_with_one_agent_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Init a project, add one agent, register the `fake` target.

    Shared scaffold for the auto-recovery tests below. Caller is
    expected to ``monkeypatch.chdir(tmp_path / "proj")`` afterward.
    """
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    # Isolate the credentials store: deploy's bearer auto-recovery reads +
    # writes ~/.movate/credentials (verify-before-clobber rolls a rejected
    # candidate back to the prior saved key). Pin it to a tmp file + the file
    # backend so these tests never touch the developer's real store.
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / ".movate" / "credentials"))
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")
    # The `fake` target names no Key Vault, so deploy's recovery would try to
    # DISCOVER one in the resource group (an `az keyvault list` shell-out)
    # before falling back to the in-pod mint. These tests model the
    # no-discoverable-vault → mint path, so pin discovery to a miss to keep
    # them hermetic (no real `az`).
    monkeypatch.setattr(deploy_mod, "_discover_keyvault_in_resource_group", lambda target_cfg: None)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "faq"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    result = runner.invoke(
        app,
        [
            "config",
            "add-target",
            "fake",
            "--url",
            "https://fake.example.com",
            "--key-env",
            "FAKE_KEY",
            "--azure-subscription",
            "00000000-0000-0000-0000-000000000000",
            "--azure-resource-group",
            "fake-rg",
            "--azure-acr",
            "fakeacr",
            "--azure-env",
            "dev",
        ],
        env={"COLUMNS": "200"},
    )
    assert result.exit_code == 0, result.stdout + result.stderr


def _stub_preflight_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :func:`deploy_mod._preflight_bearer` a pass-through.

    The existing upload-loop auto-recovery tests cover the path where
    the preflight passes (or wouldn't have caught the issue) but an
    upload later returns 401 — race conditions between preflight and
    upload, defense-in-depth, etc. Stubbing the preflight keeps those
    tests focused on the upload-loop logic without dragging real
    HTTP into the harness.
    """
    monkeypatch.setattr(deploy_mod, "_preflight_bearer", lambda *, headers, **_: headers)


@pytest.mark.unit
def test_deploy_agents_401_in_upload_loop_triggers_auto_recovery_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: even when the preflight passed (e.g. the token
    was revoked between preflight and upload), an in-flight 401 still
    triggers the same auto-recovery + retry path inside the upload
    loop. Verifies that pathway hasn't regressed."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_stale_keyid_secret")
    _stub_preflight_ok(monkeypatch)

    call_count = {"agent": 0, "refresh": 0}

    def fake_upload_agent(
        *, client: Any, base_url: str, headers: dict[str, str], agent_dir: Path, project_root: Any
    ) -> deploy_mod.AgentUploadOutcome:
        call_count["agent"] += 1
        if call_count["agent"] == 1:
            return deploy_mod.AgentUploadOutcome(error=deploy_mod._REASON_UNAUTHORIZED)
        # Bearer must have rotated by the second call — assert it.
        assert headers["Authorization"] == "Bearer mvt_live_fresh_keyid_secret"
        return deploy_mod.AgentUploadOutcome(error=None, published_version="0.1.0")

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        call_count["refresh"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    # The recovery path verifies the minted bearer is ADMIN-capable before
    # keeping it: the probe is GET /api/v1/auth/keys (admin-scoped). Only the
    # freshly-minted fleet-admin key gets a 200 there.
    def verify_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/keys" and request.method == "GET":
            if request.headers.get("Authorization") == "Bearer mvt_live_fresh_keyid_secret":
                return httpx.Response(200, json={"keys": [], "count": 0})
            return httpx.Response(401, json={"detail": "stale"})
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr(deploy_mod, "_upload_one_agent_bundle", fake_upload_agent)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)
    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(verify_handler))

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert call_count["agent"] == 2, "expected one initial 401 + one retry"
    assert call_count["refresh"] == 1, "expected exactly one refresh call"
    # Friendly recovery messaging: dim spinner + green check (verified), no
    # "saved bearer rejected" jargon.
    assert "Recovering a runtime bearer" in combined
    assert "bearer key ready" in combined
    assert "verified against the runtime" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_deploy_agents_401_with_no_auto_recover_skips_refresh_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-auto-recover`` disables the refresh path: a 401 stays a
    401, the deploy exits non-zero, and the descriptive message
    points the operator at ``mdk doctor --target`` + manual refresh."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_stale_keyid_secret")
    _stub_preflight_ok(monkeypatch)

    call_count = {"agent": 0, "refresh": 0}

    def fake_upload_agent(**_: Any) -> deploy_mod.AgentUploadOutcome:
        call_count["agent"] += 1
        return deploy_mod.AgentUploadOutcome(error=deploy_mod._REASON_UNAUTHORIZED)

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        call_count["refresh"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    monkeypatch.setattr(deploy_mod, "_upload_one_agent_bundle", fake_upload_agent)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--no-auto-recover"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert call_count["agent"] == 1, "expected only the initial attempt, no retry"
    assert call_count["refresh"] == 0, "refresh must not run with --no-auto-recover"
    assert "Recovering a runtime bearer" not in combined
    assert "mdk doctor --target fake" in combined
    assert "mdk auth refresh-runtime-key fake" in combined


# ---------------------------------------------------------------------------
# A — Pre-deploy bearer preflight: catch a stale token BEFORE the upload loop
# ---------------------------------------------------------------------------


def _httpx_transport(handler: Any) -> Any:
    """Build an ``httpx.Client`` subclass that pins a ``MockTransport``.

    Each handler signature: ``(request: httpx.Request) -> httpx.Response``.
    We subclass rather than replace ``httpx.Client`` with a factory
    function because :func:`_upload_one_agent_bundle` calls
    ``isinstance(client, httpx.Client)`` to verify the typing-as-Any
    parameter is actually a Client — a bare function would break that
    check. Subclassing preserves isinstance semantics.
    """
    transport = httpx.MockTransport(handler)

    class _MockClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    return _MockClient


@pytest.mark.unit
def test_preflight_401_with_auto_recovery_silently_refreshes_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preflight catches a stale bearer with one cheap GET, triggers
    auto-recovery up front, and the upload loop only ever sees the
    fresh bearer. The descriptive 401 message that the upload-loop path
    would have printed never appears."""

    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_stale_keyid_secret")

    seen = {"stale_gets": 0, "verify_gets": 0, "refresh_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # The preflight probes GET /api/v1/agents with the STALE bearer
        # (reject → triggers recovery).
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            seen["stale_gets"] += 1
            return httpx.Response(401, json={"detail": "stale"})
        # The post-recovery admin-capability verify probes GET
        # /api/v1/auth/keys with the FRESH minted bearer (accept).
        if request.url.path == "/api/v1/auth/keys" and request.method == "GET":
            if request.headers.get("Authorization") == "Bearer mvt_live_fresh_keyid_secret":
                seen["verify_gets"] += 1
                return httpx.Response(200, json={"keys": [], "count": 0})
            return httpx.Response(401, json={"detail": "stale"})
        # Skill and agent POSTs.
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        seen["refresh_calls"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert seen["stale_gets"] == 1, "expected exactly one preflight GET with the stale bearer"
    assert seen["verify_gets"] == 1, "expected one post-recovery verify GET with the fresh bearer"
    assert seen["refresh_calls"] == 1, "expected exactly one auto-refresh"
    # The preflight is responsible for the user-visible recovery hint;
    # the upload loop never logs its own 401 retry banner. Friendly
    # wording: "Recovering a runtime bearer" + "bearer key ready".
    assert "Recovering a runtime bearer" in combined
    assert "bearer key ready" in combined
    assert "verified against the runtime" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_preflight_401_with_no_auto_recover_fails_fast_without_uploading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--no-auto-recover`` + a preflight 401 = exit 2 before any
    multipart upload runs. The descriptive recovery message names the
    target and points at the doctor + refresh commands."""

    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_stale_keyid_secret")

    seen = {"preflight_calls": 0, "upload_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            seen["preflight_calls"] += 1
            return httpx.Response(401, json={"detail": "stale"})
        seen["upload_calls"] += 1
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--no-auto-recover"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert seen["preflight_calls"] == 1
    assert seen["upload_calls"] == 0, "must fail before any upload"
    assert "mdk doctor --target fake" in combined
    assert "mdk auth refresh-runtime-key fake" in combined


@pytest.mark.unit
def test_preflight_500_surfaces_runtime_error_without_attempting_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-401 non-2xx (e.g. a 5xx from a sick runtime) is not an
    auth problem — preflight surfaces the body verbatim and exits
    rather than triggering the auth recovery path, which would burn
    a fresh key against a broken runtime."""

    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_works_keyid_secret")

    refresh_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            return httpx.Response(500, json={"detail": "storage down"})
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    def fake_refresh(*_a: Any, **_kw: Any) -> tuple[str, str]:
        refresh_calls["n"] += 1
        return "x", "FAKE_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert refresh_calls["n"] == 0, "must not try to refresh on 5xx"
    assert "HTTP 500" in combined
    assert "storage down" in combined


@pytest.mark.unit
def test_preflight_200_proceeds_silently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: preflight returns 200, no warning printed, upload
    loop runs against the original bearer unchanged."""

    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_works_keyid_secret")

    seen = {"preflight_calls": 0, "agent_posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents":
            if request.method == "GET":
                seen["preflight_calls"] += 1
                return httpx.Response(200, json={"agents": []})
            seen["agent_posts"] += 1
            return httpx.Response(201, json={"ok": True})
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert seen["preflight_calls"] == 1
    assert seen["agent_posts"] >= 1, "upload loop must run after a green preflight"
    # No recovery banner on the happy path.
    assert "Recovering a runtime bearer" not in combined
    assert "ok=true" in combined


# ---------------------------------------------------------------------------
# A — Empty $MDK_<TARGET>_KEY: auto-recover the same way 401 does
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_empty_env_var_auto_recovers_then_completes_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the bearer env var is empty AND the target has Azure
    addressing AND --no-auto-recover isn't set, deploy now mints a
    fresh key inside the pod via the same `refresh_runtime_key_inline`
    code path the 401 handler uses. After recovery the upload loop
    runs to completion with the freshly-minted bearer."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.delenv("FAKE_KEY", raising=False)  # empty env var

    refresh_calls = {"n": 0}

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        refresh_calls["n"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)
    # Preflight returns 200 so we don't double-recover.
    monkeypatch.setattr(
        "movate.cli.deploy.httpx.Client",
        _httpx_transport(lambda req: httpx.Response(200, json={"ok": True}))
        if False
        else _httpx_transport(
            lambda req: httpx.Response(201 if req.method == "POST" else 200, json={"ok": True})
        ),
    )

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert refresh_calls["n"] == 1, "expected exactly one refresh call"
    # Friendly wording — no "env var is empty" or "rejected" jargon
    # surfaces to operators on the happy recovery path.
    assert "Recovering a runtime bearer" in combined
    assert "bearer key ready" in combined
    assert "verified against the runtime" in combined
    assert "ok=true" in combined


@pytest.mark.unit
def test_empty_env_var_with_no_auto_recover_fails_with_helpful_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-auto-recover` opts out: empty env var stays an exit-2
    error with the actionable recovery commands surfaced."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.delenv("FAKE_KEY", raising=False)

    refresh_calls = {"n": 0}

    def fake_refresh(*_a: Any, **_kw: Any) -> tuple[str, str]:
        refresh_calls["n"] += 1
        return "x", "FAKE_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--no-auto-recover"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    assert refresh_calls["n"] == 0, "must not refresh under --no-auto-recover"
    assert "$FAKE_KEY is empty" in combined
    assert "refresh-runtime-key" in combined
    assert "pull-runtime-key" in combined


@pytest.mark.unit
def test_empty_env_var_recovery_failure_falls_through_to_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `az containerapp exec` is flaky (Legion 500s) and recovery
    can't mint a key, the deploy must fall through to the original
    actionable error — operators see the manual recovery commands
    instead of a silent hang."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.delenv("FAKE_KEY", raising=False)

    def fake_refresh_fails(*_a: Any, **_kw: Any) -> tuple[str, str]:
        raise RefreshRuntimeKeyError("az containerapp exec returned 500")

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh_fails)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    # Recovery was attempted (the user sees the "Recovering a runtime
    # bearer" line) before falling through to the actionable error.
    assert "Recovering a runtime bearer" in combined
    assert "$FAKE_KEY is empty" in combined
    assert "refresh-runtime-key" in combined


# ---------------------------------------------------------------------------
# A — post-deploy "Next: run inference" block
# ---------------------------------------------------------------------------


def _capture_post_deploy_block(
    monkeypatch: pytest.MonkeyPatch,
    *,
    target_name: str,
    uploaded: list[str],
    project_root: Path,
    base_url: str = "https://demo.example.com",
    key_env: str = "MDK_DEV_KEY",
) -> str:
    """Call ``_render_post_deploy_next_steps`` with a Rich console pinned
    to a StringIO so the test can read what the operator would have
    seen on stderr. Avoids spinning up the full deploy machinery just
    to exercise the rendering branch.

    ``base_url`` + ``key_env`` mirror what's resolved from the target
    config in production — they're test-tunable so we can pin both
    the URL and the env-var-name rendering in the curl example."""
    buf = io.StringIO()
    # The block only emits a raw curl when a bearer resolves (otherwise it
    # prints a key-bootstrap hint). Pin a resolvable key via the env var so
    # these curl-rendering tests are hermetic — without it they'd depend on
    # whether the developer's real ~/.movate/credentials happens to hold an
    # entry for key_env.
    monkeypatch.setenv(key_env, "mvt_test_resolvable_key")
    monkeypatch.setattr(deploy_mod, "err", Console(file=buf, force_terminal=False))
    deploy_mod._render_post_deploy_next_steps(
        target_name=target_name,
        uploaded=uploaded,
        project_root=project_root,
        base_url=base_url,
        key_env=key_env,
    )
    return buf.getvalue()


@pytest.mark.unit
def test_post_deploy_block_renders_curl_per_uploaded_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The block emits one ``curl`` command per uploaded agent (sorted
    alphabetically) so operators can copy-paste against the deployed
    runtime without needing a working ``mdk`` install on the box doing
    the inference. Each curl targets ``POST <base_url>/run`` with the
    canonical RunSubmission ``{"kind": "agent", "target": "<name>",
    "input": {...}}`` body shape."""
    (tmp_path / "agents" / "zebra").mkdir(parents=True)
    (tmp_path / "agents" / "alpha").mkdir(parents=True)

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["zebra", "alpha"],
        project_root=tmp_path,
        base_url="https://api.example.com",
    )

    assert "Next: run inference against the deployed runtime" in out
    # One curl per agent; both names appear as # comment + in the body.
    assert "# alpha" in out
    assert "# zebra" in out
    assert "curl -sS -X POST https://api.example.com/run" in out
    # RunSubmission shape: the agent name is the `target`, not a bare `agent`.
    assert '"kind": "agent"' in out
    assert '"target": "alpha"' in out
    assert '"target": "zebra"' in out
    assert '"agent": "alpha"' not in out


@pytest.mark.unit
def test_post_deploy_block_curl_includes_content_type_and_api_key_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every curl must surface the two required headers — content-type
    (so the runtime parses the body as JSON) and Authorization: Bearer
    (so auth passes). The bearer renders as a shell expression that
    prefers an exported $key_env but falls back to reading the token
    from the credentials file, so the curl works in a fresh shell that
    never sourced ~/.movate/credentials."""
    (tmp_path / "agents" / "alpha").mkdir(parents=True)

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="prod",
        uploaded=["alpha"],
        project_root=tmp_path,
        key_env="MDK_PROD_KEY",
    )

    assert "content-type: application/json" in out
    # Bearer falls back to grepping the credentials file when the env
    # var is unset. Assert the structural prefix + suffix so the test
    # doesn't depend on the developer's home path in the middle.
    assert "Authorization: Bearer ${MDK_PROD_KEY:-$(grep -m1 '^MDK_PROD_KEY=' " in out
    assert "| cut -d= -f2-)}" in out
    # Regression: the bare `$MDK_PROD_KEY` form (empty in a fresh shell
    # → auth_required) must NOT be what we emit.
    assert '"Authorization: Bearer $MDK_PROD_KEY"' not in out


@pytest.mark.unit
def test_bearer_shell_expr_default_path_renders_tilde(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the default credentials location the expression greps
    ``~/.movate/credentials`` (so the shell expands ``~`` to the
    operator's home), prefers an exported env var, and never embeds
    the literal token."""
    monkeypatch.delenv("MOVATE_CREDENTIALS_PATH", raising=False)

    expr = deploy_mod._bearer_shell_expr("MDK_DEV_KEY")

    assert expr == (
        "${MDK_DEV_KEY:-$(grep -m1 '^MDK_DEV_KEY=' ~/.movate/credentials | cut -d= -f2-)}"
    )


@pytest.mark.unit
def test_bearer_shell_expr_honors_credentials_path_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When MOVATE_CREDENTIALS_PATH points outside $HOME the expression
    greps that absolute path (the curl must read the same file the
    deploy wrote to, not a hardcoded ~/.movate/credentials)."""
    creds = tmp_path / "creds"
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))

    expr = deploy_mod._bearer_shell_expr("MDK_STAGING_KEY")

    assert f"grep -m1 '^MDK_STAGING_KEY=' {creds.resolve()} " in expr
    assert expr.startswith("${MDK_STAGING_KEY:-$(")


@pytest.mark.unit
def test_post_deploy_block_uses_dataset_first_row_input_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the agent has an ``evals/dataset.jsonl`` with a row that
    has an ``input`` object, the curl body uses that JSON. Makes the
    copy-paste exercise the operator's real schema."""
    agent = tmp_path / "agents" / "alpha"
    (agent / "evals").mkdir(parents=True)
    (agent / "evals" / "dataset.jsonl").write_text(
        '{"input": {"question": "What is our refund window?"}, "expected": {"answer": "30 days"}}\n'
        '{"input": {"question": "second row"}, "expected": {}}\n'
    )

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["alpha"],
        project_root=tmp_path,
    )

    # The first row's input is embedded inside the curl body.
    assert '"question": "What is our refund window?"' in out
    # The fallback shouldn't appear when a real input is available.
    assert '"text": "..."' not in out


@pytest.mark.unit
def test_post_deploy_block_falls_back_when_no_dataset_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dataset.jsonl, no problem — fall back to a generic
    ``{"text":"..."}`` placeholder inside the curl body so the block
    still renders. The operator at least sees the shape of the request."""
    (tmp_path / "agents" / "alpha").mkdir(parents=True)

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["alpha"],
        project_root=tmp_path,
    )

    # The fallback input shape ({"text":"..."}) appears inside the
    # curl body — after json.dumps round-trip it renders with a
    # space after the colon ("text": "...").
    assert '"text": "..."' in out


@pytest.mark.unit
def test_post_deploy_block_falls_back_when_dataset_first_row_has_no_input_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed first row (missing ``input``) falls through to the
    placeholder rather than crashing the block."""
    agent = tmp_path / "agents" / "alpha"
    (agent / "evals").mkdir(parents=True)
    (agent / "evals" / "dataset.jsonl").write_text('{"expected": {"answer": "no input field"}}\n')

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["alpha"],
        project_root=tmp_path,
    )

    assert '"text": "..."' in out


@pytest.mark.unit
def test_post_deploy_block_tolerates_invalid_jsonl_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: a JSON parse error on the first row falls
    through silently. A corrupt dataset.jsonl mustn't wedge the
    post-deploy summary."""
    agent = tmp_path / "agents" / "alpha"
    (agent / "evals").mkdir(parents=True)
    (agent / "evals" / "dataset.jsonl").write_text("not json at all\n")

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["alpha"],
        project_root=tmp_path,
    )

    assert '"text": "..."' in out
    # And the curl example for that agent still renders.
    assert "# alpha" in out
    assert "curl -sS -X POST" in out


@pytest.mark.unit
def test_post_deploy_block_renders_all_agents_alphabetically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All uploaded agents get their own curl (no truncation, no
    'other agents:' summary line) so a multi-agent deploy doesn't
    hide some agents from the operator's copy-paste options. Sort
    alphabetically for deterministic output ordering."""
    for name in ["alpha", "beta", "gamma"]:
        (tmp_path / "agents" / name).mkdir(parents=True)

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["beta", "alpha", "gamma"],
        project_root=tmp_path,
    )

    # All three get a curl block.
    assert "# alpha" in out
    assert "# beta" in out
    assert "# gamma" in out
    # Output order is alphabetical (deterministic).
    assert out.index("# alpha") < out.index("# beta") < out.index("# gamma")


@pytest.mark.unit
def test_post_deploy_block_emits_no_rich_markup_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: an earlier rev wrapped each curl line in
    ``[cyan]…\\[/cyan]`` for styling. Rich's parser saw ``\\[`` as
    an escape for the literal bracket, so the close tag never fired
    and ``[/cyan]`` ended up as literal text in the rendered output.
    Operators who copy-pasted into a shell got
    ``zsh: no matches found: [/cyan]``.

    Pin the regression: the curl block must contain ZERO ``[/cyan]``
    or ``[cyan]`` substrings in the rendered output."""
    (tmp_path / "agents" / "alpha").mkdir(parents=True)

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["alpha"],
        project_root=tmp_path,
    )

    assert "[/cyan]" not in out, "Rich close tag leaked as literal text"
    assert "[cyan]" not in out, "Rich open tag leaked as literal text"
    assert "[/yellow]" not in out
    assert "[yellow]" not in out


@pytest.mark.unit
def test_post_deploy_block_apostrophes_in_dataset_input_survive_heredoc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: when dataset rows contain apostrophes (e.g.
    ``"We're evaluating MDK"``), the old emitter wrapped the JSON
    body in single quotes — every internal apostrophe terminated the
    quoted string and zsh hung on ``dquote>``.

    The 2026-05-19 fix switched to a ``<<'JSON'`` heredoc with
    ``--data-binary @-``. Heredoc bodies don't interpret quotes, so
    apostrophes appear verbatim in the JSON and the body passes
    through to curl exactly as printed.

    Pin: the apostrophe-bearing JSON value renders intact, the
    output uses the heredoc form (not single-quoted ``-d``), and
    there's no ``'"'"'`` shell-quote escape gymnastics needed."""
    agent = tmp_path / "agents" / "lead-qualifier"
    (agent / "evals").mkdir(parents=True)
    (agent / "evals" / "dataset.jsonl").write_text(
        '{"input": {"name": "Sarah", "message": '
        '"We\\u0027re evaluating MDK. It\\u0027s promising."}}\n'
    )

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["lead-qualifier"],
        project_root=tmp_path,
    )

    # The apostrophe-bearing value lands in the heredoc body intact.
    assert "We're evaluating MDK. It's promising." in out
    # New form: heredoc + --data-binary @-, NOT single-quoted -d.
    assert "--data-binary @-" in out
    assert "<<'JSON'" in out
    # The old shell-escape gymnastics shouldn't appear in the raw-curl
    # heredoc body — that's the whole point of the heredoc. (Scope to the
    # curl section: the recommended `mdk run` line above legitimately uses
    # shlex quoting, which IS correct shell escaping for a CLI argument and
    # is outside this guarantee.)
    curl_section = out.split("raw curl", 1)[-1]
    assert "'\"'\"'" not in curl_section, (
        "heredoc body should not contain shell-quote-escape gymnastics; "
        "apostrophes pass through verbatim"
    )


@pytest.mark.unit
def test_post_deploy_block_diff_input_parses_as_json_after_shell_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the 2026-05-19 operator bug: a code-reviewer
    dataset with a ``diff`` field containing escaped newlines used to
    emit a ``-d 'JSON-on-one-line'`` block. Rich terminal-wrapped that
    block at terminal width; copy-paste embedded literal newline bytes
    inside the JSON string value, and the server returned::

        Invalid control character at position 121

    The fix: ``--data-binary @-`` heredoc with pretty-printed JSON.
    Pretty JSON has structural whitespace BETWEEN keys (which JSON
    parsers ignore) so terminal wrap can't corrupt the body. String
    values stay escaped (``\\n`` two-char sequences).

    Pin: the rendered heredoc body parses as valid JSON when shell-
    processed, the ``diff`` value round-trips with newlines intact,
    and the body never contains a raw 0x0A inside a string value.
    """
    agent = tmp_path / "agents" / "code-reviewer"
    (agent / "evals").mkdir(parents=True)
    diff_value = (
        "--- a/auth.py\n+++ b/auth.py\n@@ -10,7 +10,7 @@\n"
        " def check_password(user, password):\n"
        "-    if user.password_hash == hash_password(password):\n"
        "+    if user.password_hash == password:\n"
        "         return True\n     return False\n"
    )
    (agent / "evals" / "dataset.jsonl").write_text(
        json.dumps({"input": {"diff": diff_value, "language": "python"}}) + "\n"
    )

    out = _capture_post_deploy_block(
        monkeypatch,
        target_name="dev",
        uploaded=["code-reviewer"],
        project_root=tmp_path,
    )

    # Extract the heredoc body — everything between "<<'JSON'\n" and "\nJSON".
    match = re.search(r"<<'JSON'\n(.*?)\nJSON", out, re.DOTALL)
    assert match, f"expected a <<'JSON' heredoc block in:\n{out}"
    body = match.group(1)

    # The body must parse as JSON (the whole point of the heredoc form).
    parsed = json.loads(body)
    # RunSubmission shape: {kind, target, input}, not the legacy {agent, input}.
    assert parsed["kind"] == "agent"
    assert parsed["target"] == "code-reviewer"
    # And the diff value round-trips with newlines intact.
    assert parsed["input"]["diff"] == diff_value
    # The body must NOT contain raw 0x0A inside a JSON string value —
    # the diff's newlines stay as the two-character ``\n`` escape
    # sequence in the JSON serialization (pretty-printing adds
    # structural newlines BETWEEN tokens, but never inside string
    # literals).
    # We can't check 0x0A absence in `body` directly because pretty-
    # printing adds newlines between fields. Check that every line of
    # the body that contains the ``diff`` field's value carries the
    # ``\n`` escape literal (backslash + n), not raw newlines.
    diff_field_lines = [ln for ln in body.splitlines() if '"diff":' in ln]
    assert diff_field_lines
    # The ``"diff": "..."`` value is on ONE line (pretty-print at
    # indent=2 doesn't break long strings). Its content has the
    # two-char ``\\n`` escape, not raw newlines.
    diff_line = diff_field_lines[0]
    assert "\\n" in diff_line, "diff value should escape newlines as \\\\n"


@pytest.mark.unit
def test_post_deploy_block_renders_on_successful_e2e_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a happy-path deploy (preflight 200 + upload 201)
    surfaces the post-deploy block with the curl example. Verifies the
    orchestrator wires base_url + key_env from the target config into
    the rendering call."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_works_keyid_secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            return httpx.Response(200, json={"agents": []})
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert "Next: run inference against the deployed runtime" in combined
    # curl with the target's base_url + key_env both surfaced.
    assert "curl -sS -X POST https://fake.example.com/run" in combined
    assert "Authorization: Bearer ${FAKE_KEY:-$(grep -m1 '^FAKE_KEY=' " in combined
    assert '"target": "faq"' in combined
    # The old mdk-submit / mdk-jobs lines must NOT appear.
    assert "mdk submit" not in combined
    assert "mdk jobs" not in combined


@pytest.mark.unit
def test_post_deploy_block_suppressed_on_failed_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the upload loop fails (e.g. a 5xx from the runtime), the
    operator has nothing to invoke — don't render a misleading
    'Next: run inference' block on top of an error summary."""
    _scaffold_project_with_one_agent_and_target(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_works_keyid_secret")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            return httpx.Response(200, json={"agents": []})
        # All POSTs fail.
        return httpx.Response(500, json={"detail": "upload failed"})

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _httpx_transport(handler))

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code != 0
    assert "Next: run inference against the deployed runtime" not in combined


# ---------------------------------------------------------------------------
# B — shorthand: ranges
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShorthandRanges:
    def test_integer_range_compiles(self) -> None:
        result = compile_shorthand({"score": "integer(0..12)"})
        assert result["properties"]["score"] == {
            "type": "integer",
            "minimum": 0,
            "maximum": 12,
        }

    def test_integer_lower_bound_only(self) -> None:
        result = compile_shorthand({"score": "integer(0..)"})
        assert result["properties"]["score"] == {
            "type": "integer",
            "minimum": 0,
        }

    def test_integer_upper_bound_only(self) -> None:
        result = compile_shorthand({"score": "integer(..12)"})
        assert result["properties"]["score"] == {
            "type": "integer",
            "maximum": 12,
        }

    def test_string_minlength_via_lower_bound(self) -> None:
        """For strings, `string(1..)` maps to `minLength: 1` (not
        `minimum: 1`, which is meaningless for strings)."""
        result = compile_shorthand({"name": "string(1..)"})
        assert result["properties"]["name"] == {
            "type": "string",
            "minLength": 1,
        }

    def test_string_both_bounds(self) -> None:
        result = compile_shorthand({"slug": "string(3..32)"})
        assert result["properties"]["slug"] == {
            "type": "string",
            "minLength": 3,
            "maxLength": 32,
        }

    def test_number_accepts_float_bound(self) -> None:
        result = compile_shorthand({"ratio": "number(0..1.0)"})
        assert result["properties"]["ratio"]["minimum"] == 0
        assert result["properties"]["ratio"]["maximum"] == 1.0

    def test_boolean_with_range_errors(self) -> None:
        with pytest.raises(SchemaShorthandError, match="boolean"):
            compile_shorthand({"x": "boolean(0..1)"})

    def test_unknown_ranged_type_errors(self) -> None:
        with pytest.raises(SchemaShorthandError, match="unknown ranged type"):
            compile_shorthand({"x": "blob(0..1)"})

    def test_no_dotdot_errors(self) -> None:
        with pytest.raises(SchemaShorthandError, match=r"must contain '\.\.'"):
            compile_shorthand({"x": "integer(5)"})

    def test_empty_range_errors(self) -> None:
        with pytest.raises(SchemaShorthandError, match="at least one bound"):
            compile_shorthand({"x": "integer(..)"})

    def test_optional_ranged_field(self) -> None:
        """`integer(0..3)?` should set optional + range together."""
        result = compile_shorthand({"score": "integer(0..3)?"})
        # Not in required list (optional).
        assert "score" not in result["required"]
        assert result["properties"]["score"]["minimum"] == 0
        assert result["properties"]["score"]["maximum"] == 3


# ---------------------------------------------------------------------------
# B — shorthand: $defs and $refs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestShorthandDefsAndRefs:
    def test_simple_ref_compiles(self) -> None:
        result = compile_shorthand(
            {
                "$defs": {"dim": {"score": "integer", "rationale": "string"}},
                "budget": "$dim",
            }
        )
        # $defs hoisted to root.
        assert "$defs" in result
        assert result["$defs"]["dim"]["type"] == "object"
        # Reference compiled to JSON Pointer.
        assert result["properties"]["budget"] == {"$ref": "#/$defs/dim"}

    def test_lead_qualifier_shape_matches_pasted_example(self) -> None:
        """The 12-line shorthand for lead-qualifier should compile to
        the same shape as the user's pasted hand-written JSON Schema
        (modulo $schema URL + key ordering, which JSON Schema-wise
        are irrelevant)."""
        result = compile_shorthand(
            {
                "$defs": {
                    "dim": {
                        "score": "integer(0..3)",
                        "rationale": "string(1..)",
                    },
                },
                "bant": {
                    "budget": "$dim",
                    "authority": "$dim",
                    "need": "$dim",
                    "timeline": "$dim",
                },
                "total_score": "integer(0..12)",
                "next_action": "book_meeting|nurture|enrich|disqualify",
                "rationale": "string(1..)",
                "objections": ["string(1..)"],
            }
        )
        # Spot-check the BANT structure.
        assert result["properties"]["bant"]["properties"]["budget"] == {"$ref": "#/$defs/dim"}
        assert result["properties"]["total_score"]["minimum"] == 0
        assert result["properties"]["total_score"]["maximum"] == 12
        assert set(result["properties"]["next_action"]["enum"]) == {
            "book_meeting",
            "nurture",
            "enrich",
            "disqualify",
        }
        assert result["properties"]["objections"]["items"]["type"] == "string"
        assert result["properties"]["objections"]["items"]["minLength"] == 1
        # The $defs is materialized — Draft202012Validator can resolve
        # the $ref against it.
        assert result["$defs"]["dim"]["properties"]["score"]["minimum"] == 0
        assert result["$defs"]["dim"]["properties"]["score"]["maximum"] == 3

    def test_dollar_ref_with_invalid_name_errors(self) -> None:
        with pytest.raises(SchemaShorthandError, match=r"\$ref name"):
            compile_shorthand({"x": "$"})

    def test_dollar_ref_with_unsafe_chars_errors(self) -> None:
        """JSON Pointer would break on '/' or '#' in a ref name —
        whitelist word characters only."""
        with pytest.raises(SchemaShorthandError, match=r"\$ref name"):
            compile_shorthand({"x": "$foo/bar"})


# ---------------------------------------------------------------------------
# B — loader: .yaml / .yml schema files
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoaderYamlSchemaFiles:
    def _scaffold_minimal_agent(
        self, tmp_path: Path, *, schema_ext: str, schema_body: str, output_body: str
    ) -> Path:
        """Build a minimal agent dir with an agent.yaml pointing at the
        given schema file (.yaml/.yml/.json). Returns the agent dir."""

        agent_dir = tmp_path / "agent"
        agent_dir.mkdir()
        (agent_dir / "schema").mkdir()
        (agent_dir / f"schema/input.{schema_ext}").write_text(schema_body)
        (agent_dir / f"schema/output.{schema_ext}").write_text(output_body)
        (agent_dir / "prompt.md").write_text("Test prompt.\n")
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent(
                f"""\
                api_version: movate/v1
                kind: Agent
                name: minimal
                version: 0.1.0
                description: minimal agent for loader tests
                prompt: ./prompt.md
                schema:
                  input: ./schema/input.{schema_ext}
                  output: ./schema/output.{schema_ext}
                model:
                  provider: openai/gpt-4o-mini
                """
            )
        )
        return agent_dir

    def test_loader_accepts_yaml_shorthand(self, tmp_path: Path) -> None:
        """`schema/input.yaml` written in shorthand form loads and
        compiles to a strict JSON Schema."""

        agent_dir = self._scaffold_minimal_agent(
            tmp_path,
            schema_ext="yaml",
            schema_body="text: string\n",
            output_body="message: string\n",
        )
        bundle = load_agent(agent_dir)
        # The shorthand `{text: string}` compiles to a JSON Schema with
        # type=object + properties.
        assert bundle.input_schema["type"] == "object"
        assert "text" in bundle.input_schema["properties"]
        assert bundle.input_schema["properties"]["text"]["type"] == "string"

    def test_loader_accepts_yaml_hand_written_json_schema(self, tmp_path: Path) -> None:
        """A `.yaml` file that LOOKS like a JSON Schema (has
        `type: object` + `properties:`) should be used verbatim —
        not re-run through the shorthand compiler."""

        # Explicit JSON Schema in YAML — has `type: object` AND
        # `properties:` at top level, so the loader treats it verbatim.
        hand_written = (
            "$schema: https://json-schema.org/draft/2020-12/schema\n"
            "type: object\n"
            "additionalProperties: false\n"
            "required: [text]\n"
            "properties:\n"
            "  text:\n"
            "    type: string\n"
            "    pattern: '^[a-z]+$'\n"
        )
        agent_dir = self._scaffold_minimal_agent(
            tmp_path,
            schema_ext="yaml",
            schema_body=hand_written,
            output_body=hand_written,
        )
        bundle = load_agent(agent_dir)
        # `pattern` is a JSON Schema-only feature — not expressible in
        # shorthand — so its presence proves the hand-written path was
        # taken (not re-compiled).
        assert bundle.input_schema["properties"]["text"].get("pattern") == "^[a-z]+$"

    def test_loader_accepts_yml_extension(self, tmp_path: Path) -> None:

        agent_dir = self._scaffold_minimal_agent(
            tmp_path,
            schema_ext="yml",
            schema_body="text: string\n",
            output_body="message: string\n",
        )
        bundle = load_agent(agent_dir)
        assert bundle.input_schema["properties"]["text"]["type"] == "string"

    def test_loader_still_accepts_json(self, tmp_path: Path) -> None:
        """Regression: existing .json schema files should keep loading
        with no behavior change."""

        json_body = json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            }
        )
        agent_dir = self._scaffold_minimal_agent(
            tmp_path,
            schema_ext="json",
            schema_body=json_body,
            output_body=json_body,
        )
        bundle = load_agent(agent_dir)
        assert bundle.input_schema["type"] == "object"

    def test_loader_unsupported_extension_errors(self, tmp_path: Path) -> None:

        agent_dir = self._scaffold_minimal_agent(
            tmp_path,
            schema_ext="json",  # placeholder; we'll override the agent.yaml
            schema_body="{}",
            output_body="{}",
        )
        # Manually rewrite agent.yaml to point at a .txt file, which
        # the loader should reject.
        (agent_dir / "schema" / "input.txt").write_text("not a schema")
        (agent_dir / "agent.yaml").write_text(
            (agent_dir / "agent.yaml")
            .read_text()
            .replace("./schema/input.json", "./schema/input.txt")
        )
        with pytest.raises(AgentLoadError, match="not supported"):
            load_agent(agent_dir)


# ---------------------------------------------------------------------------
# B — lead-qualifier template proof point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_lead_qualifier_template_ships_yaml_schemas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lead-qualifier template should now ship YAML shorthand
    schemas (not the old JSON Schemas) and still validate through
    `mdk validate`."""
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "proj")
    result = runner.invoke(app, ["add", "lead-qualifier"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr
    schema_dir = tmp_path / "proj" / "agents" / "lead-qualifier" / "schema"
    # YAML files ship.
    assert (schema_dir / "input.yaml").is_file()
    assert (schema_dir / "output.yaml").is_file()
    # Legacy JSON files do NOT ship (one canonical form per template).
    assert not (schema_dir / "input.json").exists()
    assert not (schema_dir / "output.json").exists()
    # Validate via mdk validate (loader path).
    result = runner.invoke(app, ["validate", "lead-qualifier"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.stdout + result.stderr


@pytest.mark.unit
def test_lead_qualifier_shorthand_yaml_compiles_to_expected_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading the lead-qualifier template should produce a JSON Schema
    structurally equivalent to the original hand-written one
    (matching BANT/total_score/next_action shapes)."""

    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["init", "proj", "--skip-snapshot"], env={"COLUMNS": "200"})
    monkeypatch.chdir(tmp_path / "proj")
    runner.invoke(app, ["add", "lead-qualifier"], env={"COLUMNS": "200"})
    bundle = load_agent(tmp_path / "proj" / "agents" / "lead-qualifier")
    out: dict[str, Any] = bundle.output_schema
    # BANT root + each dimension is a $ref to a reusable scoring shape.
    # Post-PR-#103 the lead-qualifier ships in canonical format and the
    # reusable type is named `scoring` (more readable than the pre-#103
    # shorthand `dim`). Per-field descriptions also flow through —
    # accept the dict shape with the description present.
    budget = out["properties"]["bant"]["properties"]["budget"]
    assert budget["$ref"] == "#/$defs/scoring"
    assert "description" in budget  # canonical adds per-field descriptions
    assert out["$defs"]["scoring"]["properties"]["score"]["minimum"] == 0
    assert out["$defs"]["scoring"]["properties"]["score"]["maximum"] == 3
    # total_score is bounded.
    assert out["properties"]["total_score"]["maximum"] == 12
    # next_action enum matches.
    assert set(out["properties"]["next_action"]["enum"]) == {
        "book_meeting",
        "nurture",
        "enrich",
        "disqualify",
    }
