"""``movate deploy`` — CLI + plan-builder + healthz poll loop.

Testing strategy:

* **Plan building** is pure-Python — call ``_build_plan`` directly.
* **Subprocess shell-outs** (``az acr build`` / ``az containerapp update``)
  are mocked at ``subprocess.run`` so we never touch a real Azure tenancy.
* **``/healthz`` polling** uses ``httpx.MockTransport`` to deterministically
  flip the response shape mid-poll.
* **CLI integration** uses Typer's ``CliRunner`` to exercise the full
  flag-parsing → plan → execution path with mocks for the side effects.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

import movate
from movate.cli.deploy import (
    DeployConfigError,
    DeployPlan,
    _build_plan,
    _ingest_bundled_kb,
    _print_next_steps,
    _print_plan,
    _run_predeploy_validation,
    _upload_one_agent_bundle,
    _wait_for_healthz,
)
from movate.cli.main import app as cli_app
from movate.core.user_config import (
    TargetConfig,
    UserConfig,
    save_user_config,
)
from movate.utils.git import git_short_sha

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _full_target() -> TargetConfig:
    """A TargetConfig with every Azure field populated — the happy path."""
    return TargetConfig(
        url="https://movate-prod-api.example.azurecontainerapps.io",
        key_env="MOVATE_PROD_KEY",
        azure_subscription="00000000-0000-0000-0000-000000000000",
        azure_resource_group="movate-prod-rg",
        azure_acr_name="movateprodacr",
        azure_env="prod",
    )


@pytest.fixture
def deploy_env(tmp_path: Path, monkeypatch):
    """Hermetic CLI environment: tmp config + a fully-configured 'prod' target.

    ``shutil.which`` is faked so the CLI's "is az installed?" check passes;
    the actual ``subprocess.run`` is intercepted per-test via
    ``mock_subprocess`` (so no real ``az`` ever fires).
    """
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(
        UserConfig(
            targets={"prod": _full_target()},
            active="prod",
        )
    )

    import shutil  # noqa: PLC0415

    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"az", "git"} else None,
    )
    # Stub the validate-before-deploy guardrail to a no-op for tests that
    # exercise build/roll mechanics. The repo cwd these tests run under is
    # itself a movate project (project.yaml + 6 real agents), so the real
    # guardrail would validate all of them — slow + brittle for a test
    # about `az` call shape. The dedicated guardrail tests below override
    # this patch (or call _run_predeploy_validation directly).
    monkeypatch.setattr("movate.cli.deploy._run_predeploy_validation", lambda: None)
    return tmp_path


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Capture every ``subprocess.run`` call without executing it.

    Returns a list that tests inspect for command shape. Default
    return is a successful CompletedProcess with empty stdout — so
    ``_git_short_sha`` falls through to ``"unknown"`` rather than
    raising on a None ``stdout``.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# _build_plan — pure-Python plan builder
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_plan_happy_path_derives_image_tag_from_version() -> None:
    """When all Azure fields are set and no --image-tag is given, the tag
    is composed as ``movate:<version>-<sha>``."""
    target = _full_target()
    plan = _build_plan(
        target_name="prod",
        target_cfg=target,
        image_tag=None,
        skip_build=False,
        only=None,
    )
    assert plan.target_name == "prod"
    assert plan.subscription == "00000000-0000-0000-0000-000000000000"
    assert plan.resource_group == "movate-prod-rg"
    assert plan.acr_name == "movateprodacr"
    assert plan.acr_login_server == "movateprodacr.azurecr.io"
    assert plan.env == "prod"
    assert plan.version == movate.__version__
    # Tag follows the convention; sha portion may be a real sha or 'unknown'
    # depending on whether the test runs inside a git repo.
    assert plan.image_tag.startswith(f"movate:{movate.__version__}-")
    assert plan.fq_image == f"movateprodacr.azurecr.io/{plan.image_tag}"
    # Both apps update by default.
    assert plan.apps_to_update == ["movate-prod-api", "movate-prod-worker"]


@pytest.mark.unit
def test_build_plan_image_tag_override_wins() -> None:
    """``--image-tag movate:0.5.0-abc1234`` (rollback flow) is honored verbatim."""
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag="movate:0.5.0-cafebab",
        skip_build=True,
        only=None,
    )
    assert plan.image_tag == "movate:0.5.0-cafebab"
    assert plan.skip_build is True


@pytest.mark.unit
def test_build_plan_only_api_filters_apps() -> None:
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag=None,
        skip_build=False,
        only="api",
    )
    assert plan.apps_to_update == ["movate-prod-api"]


@pytest.mark.unit
def test_build_plan_only_worker_filters_apps() -> None:
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag=None,
        skip_build=False,
        only="worker",
    )
    assert plan.apps_to_update == ["movate-prod-worker"]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field", "kwargs_override"),
    [
        ("--azure-subscription", {"azure_subscription": None}),
        ("--azure-resource-group", {"azure_resource_group": None}),
        ("--azure-acr", {"azure_acr_name": None}),
        ("--azure-env", {"azure_env": None}),
    ],
)
def test_build_plan_errors_on_missing_azure_field(
    field: str, kwargs_override: dict[str, Any]
) -> None:
    """Each missing Azure field is named in the error so the operator knows
    exactly which flag to pass to ``movate config add-target``."""
    target_kwargs = {
        "url": "https://x",
        "key_env": "K",
        "azure_subscription": "sub",
        "azure_resource_group": "rg",
        "azure_acr_name": "acr",
        "azure_env": "prod",
    }
    target_kwargs.update(kwargs_override)
    target = TargetConfig(**target_kwargs)

    with pytest.raises(DeployConfigError) as exc_info:
        _build_plan(
            target_name="prod",
            target_cfg=target,
            image_tag=None,
            skip_build=False,
            only=None,
        )
    msg = str(exc_info.value)
    assert field in msg
    # Operator pointer must be present so the error is self-fixing.
    assert "movate config add-target" in msg


@pytest.mark.unit
def test_build_plan_error_lists_all_missing_fields_at_once() -> None:
    """If two fields are missing, both appear in the error — operator
    fixes them in one shot instead of playing whack-a-mole."""
    target = TargetConfig(
        url="https://x",
        key_env="K",
        azure_subscription=None,
        azure_resource_group=None,
        azure_acr_name="acr",
        azure_env="prod",
    )
    with pytest.raises(DeployConfigError) as exc_info:
        _build_plan(
            target_name="prod",
            target_cfg=target,
            image_tag=None,
            skip_build=False,
            only=None,
        )
    msg = str(exc_info.value)
    assert "--azure-subscription" in msg
    assert "--azure-resource-group" in msg


# ---------------------------------------------------------------------------
# _git_short_sha — graceful degradation when git isn't around
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_git_short_sha_returns_empty_when_git_missing(monkeypatch) -> None:
    """No ``git`` on PATH → "" (graceful), not a crash."""
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert git_short_sha() == ""


@pytest.mark.unit
def test_git_short_sha_returns_empty_when_git_fails(monkeypatch) -> None:
    """Non-zero git exit (not a git repo) → "", not a stack trace."""

    def fail_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr("movate.utils.git.subprocess.run", fail_run)
    # shutil.which returning a path keeps us past the existence check.
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    assert git_short_sha() == ""


@pytest.mark.unit
def test_git_short_sha_strips_whitespace(monkeypatch) -> None:
    def ok_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="abc1234\n", stderr="")

    monkeypatch.setattr("movate.utils.git.subprocess.run", ok_run)
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/git" if name == "git" else None)
    assert git_short_sha() == "abc1234"


# ---------------------------------------------------------------------------
# _print_plan — dry-run output contains the actionable bits
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_print_plan_emits_resource_and_image_details(capsys) -> None:
    """Dry-run output must mention everything the operator needs to
    verify before re-running without --dry-run."""
    plan = DeployPlan(
        target_name="prod",
        subscription="sub-id",
        resource_group="rg-name",
        acr_name="myacr",
        env="prod",
        image_tag="movate:1.2.3-abc",
        skip_build=False,
        apps_to_update=["movate-prod-api", "movate-prod-worker"],
        version="1.2.3",
    )
    # _print_plan writes to its own stderr Console; capsys captures that
    # because Rich's Console(stderr=True) writes to sys.stderr.
    _print_plan(plan, dry_run=True)
    captured = capsys.readouterr()
    out = captured.err
    assert "dry-run" in out
    assert "prod" in out
    assert "rg-name" in out
    assert "myacr.azurecr.io" in out
    assert "movate:1.2.3-abc" in out
    assert "movate-prod-api" in out
    assert "movate-prod-worker" in out


# ---------------------------------------------------------------------------
# CLI integration — full deploy() through Typer
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_deploy_dry_run_prints_plan_without_az_calls(deploy_env, mock_subprocess) -> None:
    """--dry-run: no ``az`` commands fire, plan printed. ``git rev-parse``
    may still be called by the auto-sha derivation — we filter that out
    of the assertion since it's a read-only local op."""
    result = runner.invoke(cli_app, ["deploy", "--target", "prod", "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "dry-run" in result.stderr
    assert "movateprodacr.azurecr.io" in result.stderr
    az_calls = [c for c in mock_subprocess if c and c[0] == "az"]
    assert az_calls == []


@pytest.mark.unit
def test_cli_deploy_full_run_invokes_acr_build_and_two_updates(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """Default invocation: one `az acr build` + one update per app, then
    /healthz poll skipped via --no-wait so we keep the test sync."""
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    # Verify the shape of the subprocess calls: first an acr build, then
    # two containerapp updates. Each call begins with 'az'.
    assert len(mock_subprocess) == 3
    build_cmd = mock_subprocess[0]
    assert build_cmd[:3] == ["az", "acr", "build"]
    assert "movate:9.9.9-test" in build_cmd
    assert "--target" in build_cmd
    assert "runtime" in build_cmd

    update_cmds = mock_subprocess[1:]
    assert all(c[:3] == ["az", "containerapp", "update"] for c in update_cmds)
    app_names = {c[c.index("--name") + 1] for c in update_cmds}
    assert app_names == {"movate-prod-api", "movate-prod-worker"}
    # Image flag is the fully-qualified image.
    for c in update_cmds:
        assert "movateprodacr.azurecr.io/movate:9.9.9-test" in c


@pytest.mark.unit
def test_cli_deploy_skip_build_omits_acr_build(deploy_env, mock_subprocess) -> None:
    """--skip-build: only the two containerapp updates fire (rollback flow)."""
    result = runner.invoke(
        cli_app,
        [
            "deploy",
            "--target",
            "prod",
            "--no-wait",
            "--skip-build",
            "--image-tag",
            "movate:0.5.0-prev",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(mock_subprocess) == 2
    assert all(c[:3] == ["az", "containerapp", "update"] for c in mock_subprocess)


@pytest.mark.unit
def test_cli_deploy_only_api_runs_one_update(deploy_env, mock_subprocess) -> None:
    """--only api: acr build + a single update on movate-prod-api."""
    result = runner.invoke(
        cli_app,
        [
            "deploy",
            "--target",
            "prod",
            "--no-wait",
            "--only",
            "api",
            "--image-tag",
            "movate:0.5.0-x",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(mock_subprocess) == 2  # build + 1 update
    update_cmd = mock_subprocess[1]
    assert "movate-prod-api" in update_cmd
    assert "movate-prod-worker" not in update_cmd


@pytest.mark.unit
def test_cli_deploy_rejects_invalid_only_value(deploy_env, mock_subprocess) -> None:
    result = runner.invoke(
        cli_app, ["deploy", "--target", "prod", "--only", "everything", "--dry-run"]
    )
    assert result.exit_code == 2
    assert "--only" in result.stderr
    # Plan was never built → no subprocess calls.
    assert mock_subprocess == []


@pytest.mark.unit
def test_cli_deploy_errors_when_az_not_installed(deploy_env, monkeypatch) -> None:
    """No ``az`` on PATH → exit 2 with an install-link hint. Validated
    before resolve_target so a user without az gets the right message
    even with a half-broken config."""
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: None)
    result = runner.invoke(cli_app, ["deploy", "--target", "prod", "--no-wait"])
    assert result.exit_code == 2
    assert "az" in result.stderr
    assert "install" in result.stderr.lower()


@pytest.mark.unit
def test_cli_deploy_errors_when_target_missing_azure_config(
    tmp_path: Path, monkeypatch, mock_subprocess
) -> None:
    """A target registered without --azure-* flags can't deploy. We exit 2
    with a pointer back to ``config add-target``."""
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    # Target has the runtime URL + key_env, but no Azure fields.
    save_user_config(
        UserConfig(
            targets={"local": TargetConfig(url="http://127.0.0.1:8000", key_env="K")},
            active="local",
        )
    )
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/az" if name == "az" else None)

    result = runner.invoke(cli_app, ["deploy", "--target", "local", "--no-wait"])
    assert result.exit_code == 2
    assert "azure" in result.stderr.lower()
    assert "config add-target" in result.stderr
    # Plan-builder errored before any subprocess.
    assert mock_subprocess == []


@pytest.mark.unit
def test_cli_deploy_propagates_az_failure_as_exit_1(deploy_env, monkeypatch) -> None:
    """If ``az acr build`` exits non-zero we surface it as exit 1 with
    a clear error message (not a stack trace)."""

    def fail_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=42)

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fail_run)

    result = runner.invoke(
        cli_app, ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:x-y"]
    )
    assert result.exit_code == 1
    assert "az command failed" in result.stderr
    assert "exit 42" in result.stderr


@pytest.mark.unit
def test_cli_deploy_unknown_target_exits_2(tmp_path: Path, monkeypatch) -> None:
    """``--target ghost`` (not registered) → exit 2 from resolve_target."""
    cfg_path = tmp_path / "cfg.yaml"
    monkeypatch.setenv("MOVATE_CONFIG_PATH", str(cfg_path))
    save_user_config(UserConfig(targets={}, active=None))
    import shutil  # noqa: PLC0415

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/az" if name == "az" else None)

    result = runner.invoke(cli_app, ["deploy", "--target", "ghost", "--no-wait"])
    assert result.exit_code == 2
    # The resolver's error mentions the missing target.
    assert "ghost" in result.stderr or "not found" in result.stderr


# ---------------------------------------------------------------------------
# _wait_for_healthz — async poll loop with MockTransport
# ---------------------------------------------------------------------------


def _make_healthz_client_factory(transport: httpx.MockTransport, monkeypatch) -> None:
    """Patch httpx.AsyncClient inside deploy.py to use a MockTransport.

    deploy.py constructs the client inside _wait_for_healthz; we replace
    AsyncClient with a thin wrapper that injects our transport, so calls
    are routed to the test handler instead of the real network.
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr("movate.cli.deploy.httpx.AsyncClient", factory)


@pytest.mark.unit
def test_wait_for_healthz_returns_when_version_matches(monkeypatch) -> None:
    """Two polls: first reports an old version, second reports the new one
    → the function returns cleanly."""
    seen_versions = ["0.4.9", "0.5.0"]
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = call_count["n"]
        call_count["n"] += 1
        version = seen_versions[min(i, len(seen_versions) - 1)]
        return httpx.Response(200, json={"status": "ok", "version": version})

    _make_healthz_client_factory(httpx.MockTransport(handler), monkeypatch)
    # Shrink the poll interval so we don't actually wait 5s between polls.
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)

    asyncio.run(
        _wait_for_healthz(
            url="https://example.test",
            expected_version="0.5.0",
            timeout=30.0,
        )
    )
    # First poll returned the old version, second returned the new one.
    assert call_count["n"] >= 2


@pytest.mark.unit
def test_wait_for_healthz_times_out_with_exit_124(monkeypatch) -> None:
    """If the new version never appears, sys.exit(124) fires (timeout
    convention so bash scripts can branch on it)."""

    def stale_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.4.9"})

    _make_healthz_client_factory(httpx.MockTransport(stale_handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(
            _wait_for_healthz(
                url="https://example.test",
                expected_version="0.5.0",
                timeout=0.05,  # tight budget — first deadline check fails
            )
        )
    assert exc_info.value.code == 124


@pytest.mark.unit
def test_wait_for_healthz_tolerates_transient_network_errors(monkeypatch) -> None:
    """The poll loop should swallow httpx errors and keep retrying —
    ACA's first /healthz often 502s during a rollout before the new
    revision is healthy."""
    responses = [
        httpx.ConnectError("boom"),  # transient
        httpx.Response(500, text="hold on"),  # transient
        httpx.Response(200, json={"status": "ok", "version": "0.5.0"}),  # success
    ]
    idx = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        i = idx["n"]
        idx["n"] += 1
        r = responses[min(i, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    _make_healthz_client_factory(httpx.MockTransport(handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)

    asyncio.run(
        _wait_for_healthz(
            url="https://example.test",
            expected_version="0.5.0",
            timeout=30.0,
        )
    )
    assert idx["n"] >= 3


async def _no_sleep(_seconds: float) -> None:
    """Patched-in replacement for ``asyncio.sleep`` so the poll loop
    doesn't actually wait between iterations."""
    return None


# ---------------------------------------------------------------------------
# --with-kb: post-deploy bundled-KB ingest (_ingest_bundled_kb)
# ---------------------------------------------------------------------------


def _project_with_kb(root: Path, *agents_with_kb: str) -> Path:
    """Scaffold a project tree with agents/<name>/ dirs; the named ones get a
    non-empty kb/ subdir."""
    for name in ("alpha", "beta"):
        (root / "agents" / name).mkdir(parents=True)
    for name in agents_with_kb:
        kb = root / "agents" / name / "kb"
        kb.mkdir(parents=True, exist_ok=True)
        (kb / "doc.md").write_text("# doc")
    return root


@pytest.mark.unit
def test_ingest_bundled_kb_ingests_only_agents_with_kb(tmp_path: Path, monkeypatch) -> None:
    """Each uploaded agent with a non-empty kb/ dir → one `kb ingest … --target`
    call; agents without a kb/ dir are skipped."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    root = _project_with_kb(tmp_path, "alpha")  # only alpha has a kb/

    _ingest_bundled_kb(uploaded=["alpha", "beta"], project_root=root, target_name="prod")

    assert len(calls) == 1
    argv = calls[0]
    assert argv[1:3] == ["kb", "ingest"]
    assert "alpha" in argv
    assert str(root / "agents" / "alpha" / "kb") in argv
    assert argv[-2:] == ["--target", "prod"]


@pytest.mark.unit
def test_ingest_bundled_kb_skips_empty_kb_dir(tmp_path: Path, monkeypatch) -> None:
    """An agent whose kb/ exists but is empty → no ingest."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""),
    )
    root = tmp_path
    (root / "agents" / "alpha" / "kb").mkdir(parents=True)  # empty kb/

    _ingest_bundled_kb(uploaded=["alpha"], project_root=root, target_name="prod")

    assert calls == []


@pytest.mark.unit
def test_ingest_bundled_kb_failure_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    """A non-zero ingest exit warns but does not raise (deploy already won)."""
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"),
    )
    root = _project_with_kb(tmp_path, "alpha")

    # Must not raise.
    _ingest_bundled_kb(uploaded=["alpha"], project_root=root, target_name="prod")


# ---------------------------------------------------------------------------
# P3 — validate-before-deploy guardrail
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_deploy_aborts_before_build_on_invalid_project(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """A validation failure aborts the deploy BEFORE any image is built or
    pushed: exit non-zero and zero ``az`` (build/push/update) calls fire.

    The ``deploy_env`` fixture stubs the guardrail to a no-op for the
    build-mechanics tests; here we re-install a stub that raises, modeling
    a project that fails ``mdk validate --all``.
    """

    def boom() -> None:
        from movate.cli._console import error as _error  # noqa: PLC0415

        _error("validation failed for 1 item(s) (broken-agent); aborting before build.")
        raise typer.Exit(code=1)

    monkeypatch.setattr("movate.cli.deploy._run_predeploy_validation", boom)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code != 0
    assert "validation failed" in result.stderr
    # The whole point: nothing was built or pushed.
    az_calls = [c for c in mock_subprocess if c and c[0] == "az"]
    assert az_calls == []


@pytest.mark.unit
def test_cli_deploy_skip_validate_bypasses_the_gate(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """``--skip-validate`` must short-circuit the guardrail entirely — the
    (raising) validation stub is never invoked, and the deploy proceeds to
    build + roll as normal."""
    called = {"n": 0}

    def boom() -> None:
        called["n"] += 1
        raise typer.Exit(code=1)

    monkeypatch.setattr("movate.cli.deploy._run_predeploy_validation", boom)

    result = runner.invoke(
        cli_app,
        [
            "deploy",
            "--target",
            "prod",
            "--no-wait",
            "--skip-validate",
            "--image-tag",
            "movate:9.9.9-test",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert called["n"] == 0  # guardrail never ran
    # Build + two updates still happened.
    assert len(mock_subprocess) == 3
    assert mock_subprocess[0][:3] == ["az", "acr", "build"]


@pytest.mark.unit
def test_cli_deploy_dry_run_validates_then_shows_plan(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """Under ``--dry-run`` the guardrail still runs (validate, THEN show the
    plan). A passing validation lets the dry-run print its plan; a failing
    one aborts before the plan is shown."""
    order: list[str] = []

    def record_validate() -> None:
        order.append("validate")

    monkeypatch.setattr("movate.cli.deploy._run_predeploy_validation", record_validate)
    real_print_plan = __import__("movate.cli.deploy", fromlist=["_print_plan"])._print_plan

    def record_plan(plan, *, dry_run):
        order.append("plan")
        return real_print_plan(plan, dry_run=dry_run)

    monkeypatch.setattr("movate.cli.deploy._print_plan", record_plan)

    result = runner.invoke(cli_app, ["deploy", "--target", "prod", "--dry-run"])
    assert result.exit_code == 0, result.stdout + result.stderr
    # Validation ran before the plan was printed.
    assert order == ["validate", "plan"]
    assert "dry-run" in result.stderr
    # No az calls under dry-run.
    assert [c for c in mock_subprocess if c and c[0] == "az"] == []


@pytest.mark.unit
def test_run_predeploy_validation_noop_outside_project(tmp_path: Path, monkeypatch) -> None:
    """Called from a directory with no project marker up the tree → returns
    cleanly (nothing on disk to validate), never raises."""
    monkeypatch.chdir(tmp_path)
    # No project.yaml / policy.yaml / movate.yaml anywhere up the tree.
    _run_predeploy_validation()  # must not raise


@pytest.mark.unit
def test_run_predeploy_validation_noop_on_empty_project(tmp_path: Path, monkeypatch) -> None:
    """A project with no agents/ and no workflows/ → vacuous pass, no raise."""
    # An empty policy.yaml is a valid project marker that loads as defaults.
    (tmp_path / "policy.yaml").write_text("")
    monkeypatch.chdir(tmp_path)
    _run_predeploy_validation()  # must not raise


@pytest.mark.unit
def test_run_predeploy_validation_raises_exit_1_on_broken_agent(
    tmp_path: Path, monkeypatch
) -> None:
    """A project whose agent fails to load surfaces as ``typer.Exit(1)`` —
    the real per-agent validator (``validate._validate_agent``) raises
    ``typer.Exit`` on a malformed bundle, which the guardrail aggregates
    into an abort. Proves the guardrail delegates to the real validation
    primitives rather than re-implementing them."""
    (tmp_path / "policy.yaml").write_text("")  # valid project marker (defaults)
    agent_dir = tmp_path / "agents" / "broken-agent"
    agent_dir.mkdir(parents=True)
    # An agent.yaml that can't load (missing required fields / no prompt) —
    # load_agent() raises AgentLoadError → _validate_agent raises Exit(2).
    (agent_dir / "agent.yaml").write_text("name: broken-agent\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(typer.Exit) as exc_info:
        _run_predeploy_validation()
    assert exc_info.value.exit_code == 1


# ---------------------------------------------------------------------------
# P2 — post-deploy "next steps" block
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_print_next_steps_verified_contains_api_test_health_traces(capsys) -> None:
    """The verified-phase block leads with ✓ deployed and lists the API
    URL, a `run` test line scoped to the target, a doctor health check, and
    the App Insights traces pointer."""
    _print_next_steps(
        target_name="prod",
        base_url="https://movate-prod-api.example.azurecontainerapps.io",
        first_agent="faq-agent",
        phase="verified",
    )
    out = capsys.readouterr().err
    assert "deployed to prod" in out
    assert "https://movate-prod-api.example.azurecontainerapps.io" in out
    assert "run faq-agent" in out
    assert "--target prod" in out
    assert "doctor --target prod" in out
    assert "App Insights" in out
    assert "trace_id" in out


@pytest.mark.unit
def test_print_next_steps_submitted_adapts_wording_for_no_wait(capsys) -> None:
    """``--no-wait`` (submitted) phase must NOT claim health is verified —
    it says "submitted" and frames the doctor line as a verify step."""
    _print_next_steps(
        target_name="prod",
        base_url="https://x.example.io",
        first_agent="faq-agent",
        phase="submitted",
    )
    out = capsys.readouterr().err
    assert "submitted" in out
    assert "not yet verified" in out
    # Doctor line is framed as "verify:" rather than "health:".
    assert "verify:" in out


@pytest.mark.unit
def test_print_next_steps_planned_frames_as_dry_run(capsys) -> None:
    """``--dry-run`` (planned) phase frames the block as a hypothetical."""
    _print_next_steps(
        target_name="prod",
        base_url="https://x.example.io",
        first_agent="faq-agent",
        phase="planned",
    )
    out = capsys.readouterr().err
    assert "dry-run" in out
    assert "https://x.example.io" in out


@pytest.mark.unit
def test_print_next_steps_uses_placeholder_when_no_agents(capsys) -> None:
    """No project agent → the test line uses an ``<agent>`` placeholder
    rather than crashing."""
    _print_next_steps(
        target_name="prod",
        base_url="https://x.example.io",
        first_agent=None,
        phase="verified",
    )
    out = capsys.readouterr().err
    assert "run <agent>" in out


@pytest.mark.unit
def test_cli_deploy_no_wait_prints_submitted_next_steps(deploy_env, mock_subprocess) -> None:
    """End-to-end: a ``--no-wait`` deploy prints the submitted-phase
    next-steps block (health framed as a follow-up, not verified)."""
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "submitted" in result.stderr
    assert "verify:" in result.stderr
    assert "doctor --target prod" in result.stderr


@pytest.mark.unit
def test_cli_deploy_verified_prints_deployed_next_steps(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """End-to-end happy path WITHOUT --no-wait: stub the /healthz poll so
    the test stays sync, then assert the verified-phase next-steps block is
    printed (✓ deployed + the doctor health line)."""

    async def _instant_healthz(*, url, expected_version, timeout):
        return None

    monkeypatch.setattr("movate.cli.deploy._wait_for_healthz", _instant_healthz)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "deployed to prod" in result.stderr
    assert "health:" in result.stderr
    assert "doctor --target prod" in result.stderr
    # FQDN came from the resolved target URL.
    assert "movate-prod-api.example.azurecontainerapps.io" in result.stderr


# ---------------------------------------------------------------------------
# Per-agent bundle upload: inline-schema agents materialize JSON Schema parts
# (regression for `mdk add default <name>` → `mdk deploy` HTTP 400)
# ---------------------------------------------------------------------------


class _CapturingClient(httpx.Client):
    """Stand-in for ``httpx.Client`` that records the ``files=`` payload of
    the POST and returns a canned 201, so ``_upload_one_agent_bundle`` runs
    end-to-end without a network or live runtime.

    Subclasses ``httpx.Client`` so it satisfies the ``isinstance`` guard
    in ``_upload_one_agent_bundle`` (which re-imports the real ``httpx``
    locally); the real ``post`` is shadowed to capture, never send.
    """

    def __init__(self) -> None:
        super().__init__()
        self.captured_files: list[tuple[str, tuple[str, bytes, str]]] = []

    def post(self, url: str, *, files=None, headers=None, **kwargs):  # type: ignore[override]
        self.captured_files = list(files or [])
        return httpx.Response(201, json={"name": "ok"})


def _parts_by_field(
    files: list[tuple[str, tuple[str, bytes, str]]],
) -> dict[str, tuple[str, bytes, str]]:
    """Index a captured multipart payload by its form-field name."""
    return {field: spec for field, spec in files}


def _write_inline_agent(agent_dir: Path) -> None:
    """Scaffold an inline-schema agent dir mirroring the default
    ``mdk add`` template: agent.yaml with inline ``schema:`` shorthand,
    a prompt.md, and an evals dataset. No schema/*.json files on disk."""
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: inline-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input:\n"
        "    text: string\n"
        "  output:\n"
        "    message: string\n"
        "skills: []\n"
        "evals:\n"
        "  dataset: ./evals/dataset.jsonl\n"
    )
    (agent_dir / "prompt.md").write_text("Reply to {{ input.text }}.\n")
    (agent_dir / "evals").mkdir()
    (agent_dir / "evals" / "dataset.jsonl").write_text('{"input": {"text": "hi"}}\n')


def _write_pathref_agent(agent_dir: Path) -> None:
    """Scaffold a path-ref agent dir: agent.yaml points at on-disk
    schema/input.yaml + schema/output.yaml shorthand files."""
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.yaml").write_text(
        "api_version: movate/v1\n"
        "kind: Agent\n"
        "name: pathref-agent\n"
        "version: 0.1.0\n"
        "model:\n"
        "  provider: openai/gpt-4o-mini-2024-07-18\n"
        "prompt: ./prompt.md\n"
        "schema:\n"
        "  input: ./schema/input.yaml\n"
        "  output: ./schema/output.yaml\n"
        "skills: []\n"
    )
    (agent_dir / "prompt.md").write_text("Reply to {{ input.text }}.\n")
    (agent_dir / "schema").mkdir()
    (agent_dir / "schema" / "input.yaml").write_text("query: string\n")
    (agent_dir / "schema" / "output.yaml").write_text("answer: string\n")


@pytest.mark.unit
def test_upload_inline_agent_materializes_compiled_schema_parts(
    tmp_path: Path,
) -> None:
    """An inline-schema agent (default template) now uploads input.json +
    output.json parts whose bytes are a valid compiled JSON Schema — the
    fallback that fixes `mdk add default` → `mdk deploy` HTTP 400."""
    agent_dir = tmp_path / "agents" / "inline-agent"
    _write_inline_agent(agent_dir)
    client = _CapturingClient()

    reason = _upload_one_agent_bundle(
        client=client,
        base_url="https://rt.example",
        headers={"Authorization": "Bearer x"},
        agent_dir=agent_dir,
        project_root=tmp_path,
    )
    assert reason is None  # 201 → success

    parts = _parts_by_field(client.captured_files)
    # The schema parts are NO LONGER omitted for an inline agent.
    assert "input_schema" in parts
    assert "output_schema" in parts

    in_name, in_bytes, in_ctype = parts["input_schema"]
    out_name, out_bytes, out_ctype = parts["output_schema"]
    # Persisted under the canonical names the runtime expects.
    assert in_name == "input.json"
    assert out_name == "output.json"
    assert in_ctype == "application/json"
    assert out_ctype == "application/json"

    # The bytes are valid JSON Schema (compiled from the inline shorthand).
    import json as _json  # noqa: PLC0415

    in_schema = _json.loads(in_bytes)
    out_schema = _json.loads(out_bytes)
    assert in_schema["type"] == "object"
    assert "text" in in_schema["properties"]
    assert out_schema["type"] == "object"
    assert "message" in out_schema["properties"]
    # Sanity: it actually validates as a Draft 2020-12 schema.
    from jsonschema import Draft202012Validator  # noqa: PLC0415

    Draft202012Validator.check_schema(in_schema)
    Draft202012Validator.check_schema(out_schema)


@pytest.mark.unit
def test_upload_pathref_agent_still_uploads_on_disk_schema(
    tmp_path: Path,
) -> None:
    """Regression: a path-ref agent (schema/input.yaml on disk) still
    uploads its on-disk schema — the fallback only fires when no file
    exists, so this path is unchanged (YAML compiled to JSON in-flight)."""
    agent_dir = tmp_path / "agents" / "pathref-agent"
    _write_pathref_agent(agent_dir)
    client = _CapturingClient()

    reason = _upload_one_agent_bundle(
        client=client,
        base_url="https://rt.example",
        headers={"Authorization": "Bearer x"},
        agent_dir=agent_dir,
        project_root=tmp_path,
    )
    assert reason is None

    parts = _parts_by_field(client.captured_files)
    assert "input_schema" in parts
    assert "output_schema" in parts

    import json as _json  # noqa: PLC0415

    in_schema = _json.loads(parts["input_schema"][1])
    out_schema = _json.loads(parts["output_schema"][1])
    # Compiled from the on-disk shorthand files (query/answer), NOT the
    # inline-fallback fields (text/message) — proves the file path won.
    assert "query" in in_schema["properties"]
    assert "answer" in out_schema["properties"]
