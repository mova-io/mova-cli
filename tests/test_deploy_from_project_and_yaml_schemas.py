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

import json
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
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
    monkeypatch.setattr(
        deploy_mod, "_preflight_bearer", lambda *, headers, **_: headers
    )


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
    ) -> str | None:
        call_count["agent"] += 1
        if call_count["agent"] == 1:
            return deploy_mod._REASON_UNAUTHORIZED
        # Bearer must have rotated by the second call — assert it.
        assert headers["Authorization"] == "Bearer mvt_live_fresh_keyid_secret"
        return None

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        call_count["refresh"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    monkeypatch.setattr(deploy_mod, "_upload_one_agent_bundle", fake_upload_agent)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert call_count["agent"] == 2, "expected one initial 401 + one retry"
    assert call_count["refresh"] == 1, "expected exactly one refresh call"
    # Friendly recovery messaging: dim spinner + green check, no
    # "saved bearer rejected" jargon.
    assert "Minting fresh bearer key" in combined
    assert "bearer key ready" in combined
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

    def fake_upload_agent(**_: Any) -> str | None:
        call_count["agent"] += 1
        return deploy_mod._REASON_UNAUTHORIZED

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
    assert "Minting fresh bearer key" not in combined
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

    seen = {"preflight_calls": 0, "refresh_calls": 0, "upload_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/agents" and request.method == "GET":
            seen["preflight_calls"] += 1
            # First preflight call gets a stale bearer; reject. (We
            # don't expect a second preflight — auto-recovery updates
            # the in-memory headers and the upload loop is what runs
            # next.)
            return httpx.Response(401, json={"detail": "stale"})
        # Skill and agent POSTs.
        return httpx.Response(201, json={"ok": True})

    monkeypatch.setattr(
        "movate.cli.deploy.httpx.Client", _httpx_transport(handler)
    )

    def fake_refresh(target: str, **_: Any) -> tuple[str, str]:
        seen["refresh_calls"] += 1
        return "mvt_live_fresh_keyid_secret", "FAKE_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert seen["preflight_calls"] == 1, "expected exactly one preflight GET"
    assert seen["refresh_calls"] == 1, "expected exactly one auto-refresh"
    # The preflight is responsible for the user-visible recovery hint;
    # the upload loop never logs its own 401 retry banner. Friendly
    # wording: "Minting fresh bearer key" + "bearer key ready".
    assert "Minting fresh bearer key" in combined
    assert "bearer key ready" in combined
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

    monkeypatch.setattr(
        "movate.cli.deploy.httpx.Client", _httpx_transport(handler)
    )

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

    monkeypatch.setattr(
        "movate.cli.deploy.httpx.Client", _httpx_transport(handler)
    )

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
def test_preflight_200_proceeds_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    monkeypatch.setattr(
        "movate.cli.deploy.httpx.Client", _httpx_transport(handler)
    )

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert seen["preflight_calls"] == 1
    assert seen["agent_posts"] >= 1, "upload loop must run after a green preflight"
    # No recovery banner on the happy path.
    assert "Minting fresh bearer key" not in combined
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
            lambda req: httpx.Response(
                201 if req.method == "POST" else 200, json={"ok": True}
            )
        ),
    )

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert refresh_calls["n"] == 1, "expected exactly one refresh call"
    # Friendly wording — no "env var is empty" or "rejected" jargon
    # surfaces to operators on the happy recovery path.
    assert "Minting fresh bearer key" in combined
    assert "bearer key ready" in combined
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

    monkeypatch.setattr(
        "movate.cli.auth.refresh_runtime_key_inline", fake_refresh_fails
    )

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    combined = result.stdout + result.stderr
    assert result.exit_code == 2, combined
    # Recovery was attempted (the user sees the "Minting fresh
    # bearer key" line) before falling through to the actionable
    # error.
    assert "Minting fresh bearer key" in combined
    assert "$FAKE_KEY is empty" in combined
    assert "refresh-runtime-key" in combined


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
