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
from typer.testing import CliRunner

import movate
from movate.cli.deploy import (
    DeployConfigError,
    DeployPlan,
    _build_plan,
    _ingest_bundled_kb,
    _print_plan,
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
