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
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pydantic
import pytest
import typer
from typer.testing import CliRunner

import movate
from movate.cli.auth import PullRuntimeKeyError
from movate.cli.deploy import (
    DeployConfigError,
    DeployPlan,
    _assert_image_parity,
    _attempt_auto_recovery,
    _build_plan,
    _diagnose_failed_revision,
    _ingest_bundled_kb,
    _preflight_pgvector,
    _print_next_steps,
    _print_plan,
    _query_containerapp_image,
    _render_bearer_bootstrap_hint,
    _render_post_deploy_next_steps,
    _resolve_keyvault_name,
    _run_predeploy_validation,
    _runtime_key_is_resolvable,
    _upload_one_agent_bundle,
    _verify_bearer_roundtrip,
    _wait_for_healthz,
    _warn_if_shell_shadows_runtime_key,
)
from movate.cli.main import app as cli_app
from movate.core.user_config import (
    TargetConfig,
    UserConfig,
    save_user_config,
)
from movate.credentials.store import CredentialsStore
from movate.runtime.schemas import RunSubmission
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

    # The runtime-deploy path runs a pgvector pre-flight that shells out to
    # `az postgres flexible-server ...` via the _azure_doctor helpers (a
    # DIFFERENT subprocess hook than `mock_subprocess`). Default it to "no
    # Postgres server in the RG" so the gate cleanly skips and the existing
    # runtime-deploy tests stay hermetic. pgvector-specific tests override
    # `movate.cli._azure_doctor.subprocess.run` themselves.
    def _fake_doctor_az(cmd, *_a, **_k):
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="[]")

    monkeypatch.setattr("movate.cli._azure_doctor.subprocess.run", _fake_doctor_az)
    return tmp_path


class _FakePopen:
    """Minimal ``subprocess.Popen`` stand-in for the streaming ACR-build
    runner (``_stream_az``). Yields ``lines`` from ``stdout`` and exits
    with ``returncode`` — no real process is spawned."""

    def __init__(self, lines: list[str] | None = None, returncode: int = 0) -> None:
        self.stdout = iter(lines or [])
        self._returncode = returncode

    def wait(self, timeout: float | None = None) -> int:
        return self._returncode

    def terminate(self) -> None:  # pragma: no cover — interrupt-only path
        return

    def kill(self) -> None:  # pragma: no cover — interrupt-only path
        return


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Capture every ``az`` shell-out without executing it.

    Returns a list that tests inspect for command shape. Two hooks:

    * ``subprocess.run`` — used by the JSON-returning ``_run_az`` (e.g.
      ``az containerapp update``). Default return is a successful
      CompletedProcess with empty stdout — so ``_git_short_sha`` falls
      through to ``"unknown"`` rather than raising on a None ``stdout``.
    * ``subprocess.Popen`` — used by the streaming ``_stream_az`` for
      ``az acr build`` (stdout discarded). Default is a clean exit with
      no output.

    Both append their ``cmd`` to the shared ``calls`` list, so existing
    call-shape assertions keep working across the run/Popen split.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    def fake_popen(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _FakePopen(lines=[], returncode=0)

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake_run)
    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fake_popen)
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
def test_build_plan_bare_image_tag_is_normalized_to_default_repo() -> None:
    """A bare ``--image-tag 0.8.2.5-ca0e04e`` (no repository segment) is
    auto-prepended with the default ``movate`` repo so the fully-qualified
    image stays ``<acr>/movate:<tag>`` instead of collapsing to a repo name
    with no tag (which ACR would resolve to ``:latest`` → MANIFEST_UNKNOWN)."""
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag="0.8.2.5-ca0e04e",
        skip_build=True,
        only=None,
    )
    assert plan.image_tag == "movate:0.8.2.5-ca0e04e"
    assert plan.fq_image == "movateprodacr.azurecr.io/movate:0.8.2.5-ca0e04e"


@pytest.mark.unit
def test_build_plan_image_tag_with_repo_segment_is_left_untouched() -> None:
    """An already repository-qualified ``--image-tag`` (has a ``:``) is honored
    verbatim — normalization must not double-prepend the repo."""
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag="movate:0.5.0-cafebab",
        skip_build=True,
        only=None,
    )
    assert plan.image_tag == "movate:0.5.0-cafebab"


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
def test_build_plan_only_temporal_worker_filters_apps() -> None:
    """--only temporal-worker scopes the roll to just the Temporal worker."""
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag=None,
        skip_build=False,
        only="temporal-worker",
    )
    assert plan.apps_to_update == ["movate-prod-temporal-worker"]


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
    # two containerapp updates. Each call begins with 'az'. Read-only gates
    # are filtered out — the pgvector pre-flight's `az postgres ...` and the
    # post-update image-parity guard's `az containerapp show ...` reads — they
    # aren't part of the build/roll shape under test.
    deploy_calls = [
        c for c in mock_subprocess if c and c[0] == "az" and "postgres" not in c and "show" not in c
    ]
    assert len(deploy_calls) == 3
    build_cmd = deploy_calls[0]
    assert build_cmd[:3] == ["az", "acr", "build"]
    assert "movate:9.9.9-test" in build_cmd
    assert "--target" in build_cmd
    assert "runtime" in build_cmd

    update_cmds = deploy_calls[1:]
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
    deploy_calls = [
        c for c in mock_subprocess if c and c[0] == "az" and "postgres" not in c and "show" not in c
    ]
    assert len(deploy_calls) == 2
    assert all(c[:3] == ["az", "containerapp", "update"] for c in deploy_calls)


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
    deploy_calls = [
        c for c in mock_subprocess if c and c[0] == "az" and "postgres" not in c and "show" not in c
    ]
    assert len(deploy_calls) == 2  # build + 1 update
    update_cmd = deploy_calls[1]
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
    a clear error message (not a stack trace), plus the buffered build
    log tail so the operator sees the actual Azure error."""

    def fail_popen(cmd, *args, **kwargs):
        return _FakePopen(
            lines=["pulling base image…\n", "ERROR: failed to build: boom\n"],
            returncode=42,
        )

    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fail_popen)

    result = runner.invoke(
        cli_app, ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:x-y"]
    )
    assert result.exit_code == 1
    assert "az command failed" in result.stderr
    assert "exit 42" in result.stderr
    # The buffered tail is surfaced even in quiet (non-verbose) mode.
    assert "failed to build: boom" in result.stderr


# ---------------------------------------------------------------------------
# _stream_az — streaming runner for `az acr build`
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_stream_az_returns_full_stdout(monkeypatch) -> None:
    """The streaming runner returns the FULL captured stdout (so a caller
    that parses JSON still gets the whole blob), regardless of echo."""
    from movate.cli._progress import _NULL_LIVE_STEP  # noqa: PLC0415
    from movate.cli.deploy import _stream_az  # noqa: PLC0415

    lines = ['{"properties": ', '{"provisioningState": ', '"Succeeded"}}\n']
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.Popen",
        lambda cmd, *a, **k: _FakePopen(lines=lines, returncode=0),
    )

    out = _stream_az(["az", "acr", "build"], what="acr build", step=_NULL_LIVE_STEP, echo=False)
    assert out == "".join(lines)
    # And it round-trips as JSON — the compat contract for parse-callers.
    assert json.loads(out)["properties"]["provisioningState"] == "Succeeded"


@pytest.mark.unit
def test_stream_az_failure_prints_tail_and_exits_1(monkeypatch, capsys) -> None:
    """Non-zero exit → buffered tail + the standard error envelope on
    stderr, then typer.Exit(1)."""
    from movate.cli._progress import _NULL_LIVE_STEP  # noqa: PLC0415
    from movate.cli.deploy import _stream_az  # noqa: PLC0415

    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.Popen",
        lambda cmd, *a, **k: _FakePopen(lines=["building…\n", "ERROR: boom\n"], returncode=7),
    )

    with pytest.raises(typer.Exit) as exc:
        _stream_az(["az", "acr", "build"], what="acr build", step=_NULL_LIVE_STEP, echo=False)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "ERROR: boom" in err  # the buffered tail
    assert "az command failed" in err
    assert "exit 7" in err


@pytest.mark.unit
def test_stream_az_echo_logs_lines_to_step(monkeypatch) -> None:
    """When echo=True the streamed lines are forwarded to step.log; when
    echo=False the handle is never touched (quiet mode)."""
    from movate.cli.deploy import _stream_az  # noqa: PLC0415

    class _RecordingStep:
        def __init__(self) -> None:
            self.logged: list[str] = []

        def update(self, message: str) -> None:
            pass

        def log(self, line: str) -> None:
            self.logged.append(line)

    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.Popen",
        lambda cmd, *a, **k: _FakePopen(lines=["a\n", "b\n"], returncode=0),
    )

    echoing = _RecordingStep()
    _stream_az(["az", "acr", "build"], what="acr build", step=echoing, echo=True)  # type: ignore[arg-type]
    assert echoing.logged == ["a\n", "b\n"]

    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.Popen",
        lambda cmd, *a, **k: _FakePopen(lines=["a\n", "b\n"], returncode=0),
    )
    quiet = _RecordingStep()
    _stream_az(["az", "acr", "build"], what="acr build", step=quiet, echo=False)  # type: ignore[arg-type]
    assert quiet.logged == []


# ---------------------------------------------------------------------------
# --verbose flag wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_help_documents_verbose_flag() -> None:
    """``--verbose/-v`` is present in the deploy help text."""
    result = runner.invoke(cli_app, ["deploy", "--help"])
    assert result.exit_code == 0
    # Rich renders --help with ANSI styling + box-drawing + width-based
    # wrapping that varies by terminal/CI width, so the raw stdout can splay
    # escape codes through the text. Assert against ANSI-stripped,
    # whitespace-collapsed help instead of the raw bytes.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout)
    plain = re.sub(r"\s+", " ", plain)
    assert "--verbose" in plain
    assert "-v" in plain


@pytest.mark.unit
def test_deploy_verbose_threads_echo_to_acr_build(deploy_env, monkeypatch) -> None:
    """``--verbose`` toggles the ACR build log echo. We capture the
    ``echo`` kwarg passed to the streaming runner to prove the wiring."""
    seen: dict[str, bool] = {}

    def _fake_stream(cmd, *, what, step, echo):
        seen["echo"] = echo
        return ""

    monkeypatch.setattr("movate.cli.deploy._stream_az", _fake_stream)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-t", "--verbose"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen["echo"] is True


@pytest.mark.unit
def test_deploy_default_does_not_echo_acr_build(deploy_env, monkeypatch) -> None:
    """Without ``--verbose`` the ACR build log is NOT echoed (timer only)."""
    seen: dict[str, bool] = {}

    def _fake_stream(cmd, *, what, step, echo):
        seen["echo"] = echo
        return ""

    monkeypatch.setattr("movate.cli.deploy._stream_az", _fake_stream)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-t"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert seen["echo"] is False


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
# pgvector pre-flight gate — runtime-mode deploy aborts before rolling a
# revision when the target Postgres doesn't allow-list pgvector.
#
# The runtime's CREATE EXTENSION vector ActivationFails on an Azure Postgres
# whose azure.extensions value lacks 'vector'; ACA then silently keeps the old
# revision and the /healthz gate spins. The gate catches that up-front.
# ---------------------------------------------------------------------------


def _fake_runtime_az(pg_servers: str, pg_param: str | None):
    """`subprocess.run` + `subprocess.Popen` fakes that answer the pgvector
    pre-flight's `az postgres` reads + record every OTHER az call (acr build /
    containerapp update) so a test can assert what did — and didn't — get
    rolled.

    Returns ``(fake_run, fake_popen, calls)``. The `az postgres` reads are
    answered inline (not recorded) since they're the gate, not the roll-out
    under test. ``fake_popen`` covers the streaming ``az acr build`` path.
    """
    calls: list[list[str]] = []

    def _run(cmd, *_a, **_k):
        joined = " ".join(cmd)
        if "flexible-server list" in joined:
            return subprocess.CompletedProcess(cmd, 0, pg_servers)
        if "parameter show" in joined:
            return subprocess.CompletedProcess(cmd, 0, pg_param or "")
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "")

    def _popen(cmd, *_a, **_k):
        calls.append(list(cmd))
        return _FakePopen(lines=[], returncode=0)

    return _run, _popen, calls


@pytest.mark.unit
def test_cli_deploy_aborts_when_pgvector_not_enabled(deploy_env, monkeypatch) -> None:
    """Postgres exists but azure.extensions lacks vector → exit 2 with the fix,
    and NO revision is rolled (no acr build, no containerapp update)."""
    fake, fake_popen, calls = _fake_runtime_az(
        '[{"name": "movate-prod-pg"}]',
        '{"value": "", "allowedValues": "vector,uuid-ossp"}',
    )
    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake)
    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fake_popen)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 2, result.stdout + result.stderr
    # Nothing was built or rolled — the gate fired before the roll-out.
    assert not any(c[:3] == ["az", "acr", "build"] for c in calls)
    assert not any(c[:3] == ["az", "containerapp", "update"] for c in calls)
    # The operator gets the one-shot fix.
    assert "azure.extensions" in result.stderr
    assert "--value VECTOR" in result.stderr
    assert "restart" in result.stderr


@pytest.mark.unit
def test_cli_deploy_proceeds_when_pgvector_enabled(deploy_env, monkeypatch) -> None:
    """azure.extensions allow-lists vector → the deploy rolls normally and
    reports the green pre-flight line."""
    fake, fake_popen, calls = _fake_runtime_az(
        '[{"name": "movate-prod-pg"}]',
        '{"value": "VECTOR", "allowedValues": "vector"}',
    )
    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake)
    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fake_popen)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert any(c[:3] == ["az", "acr", "build"] for c in calls)
    update_apps = {
        c[c.index("--name") + 1] for c in calls if c[:3] == ["az", "containerapp", "update"]
    }
    assert update_apps == {"movate-prod-api", "movate-prod-worker"}
    assert "pgvector allow-listed" in result.stderr


@pytest.mark.unit
def test_cli_deploy_proceeds_when_no_postgres_server(deploy_env, monkeypatch) -> None:
    """No Postgres server in the RG (sqlite target / not deployed) → the gate
    skips silently and the deploy proceeds; no false green pre-flight line."""
    fake, fake_popen, calls = _fake_runtime_az("[]", None)
    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake)
    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fake_popen)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert any(c[:3] == ["az", "acr", "build"] for c in calls)
    assert "pgvector allow-listed" not in result.stderr


@pytest.mark.unit
def test_preflight_pgvector_raises_typer_exit_on_misconfig(monkeypatch) -> None:
    """Direct unit: the gate raises typer.Exit(2) on a confirmed misconfig and
    returns cleanly when vector is enabled."""
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag="movate:1.2.3-abc",
        skip_build=False,
        only=None,
    )

    def _enabled(cmd, *_a, **_k):
        joined = " ".join(cmd)
        if "flexible-server list" in joined:
            return subprocess.CompletedProcess(cmd, 0, '[{"name": "movate-prod-pg"}]')
        if "parameter show" in joined:
            return subprocess.CompletedProcess(cmd, 0, '{"value": "VECTOR"}')
        return subprocess.CompletedProcess(cmd, 1, "")

    monkeypatch.setattr("movate.cli._azure_doctor.subprocess.run", _enabled)
    _preflight_pgvector(plan)  # must NOT raise

    def _disabled(cmd, *_a, **_k):
        joined = " ".join(cmd)
        if "flexible-server list" in joined:
            return subprocess.CompletedProcess(cmd, 0, '[{"name": "movate-prod-pg"}]')
        if "parameter show" in joined:
            return subprocess.CompletedProcess(cmd, 0, '{"value": ""}')
        return subprocess.CompletedProcess(cmd, 1, "")

    monkeypatch.setattr("movate.cli._azure_doctor.subprocess.run", _disabled)
    with pytest.raises(typer.Exit) as exc:
        _preflight_pgvector(plan)
    assert exc.value.exit_code == 2


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
    convention so bash scripts can branch on it). With no ``plan`` there's
    nothing to diagnose, so the generic message is shown."""

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
# _healthz_version_matches — the version-match predicate (CalVer counter
# drift, ADR 066). The per-day counter N diverges between the locally
# installed metadata and the version baked into the image, so the wait must
# never key on it alone.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("seen", "expected_version", "expected_sha", "matches"),
    [
        # Exact match — the deterministic path (expected = the injected
        # MOVATE_BUILD_VERSION).
        ("2026.6.11.8", "2026.6.11.8", "47d6d65f", True),
        ("2026.6.11.8", "2026.6.11.8", None, True),
        # Counter drift but the dirty-build local segment carries the same
        # sha → the rollout is proven; counter mismatch is not a failure.
        ("2026.6.11.8+g47d6d65f.dirty", "2026.6.11.3", "47d6d65f", True),
        # Prefix tolerance both ways (`--short` sha lengths vary).
        ("2026.6.11.8+g47d6d65f12.dirty", "2026.6.11.3", "47d6d65f", True),
        ("2026.6.11.8+g47d6d65.dirty", "2026.6.11.3", "47d6d65f", True),
        # Different sha → still the old image.
        ("2026.6.11.8+gdeadbeef.dirty", "2026.6.11.3", "47d6d65f", False),
        # No sha in the reported version (clean build) + counter mismatch →
        # cannot prove the rollout; predicate stays false (current behavior).
        ("2026.6.11.8", "2026.6.11.3", "47d6d65f", False),
        # No expected sha to compare against → version string is all we have.
        ("2026.6.11.8+g47d6d65f.dirty", "2026.6.11.3", None, False),
        ("0+unknown", "2026.6.11.3", "47d6d65f", False),
    ],
)
def test_healthz_version_matches_predicate(
    seen: str, expected_version: str, expected_sha: str | None, matches: bool
) -> None:
    from movate.cli.deploy import _healthz_version_matches  # noqa: PLC0415

    assert (
        _healthz_version_matches(seen, expected_version=expected_version, expected_sha=expected_sha)
        is matches
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("image_tag", "sha"),
    [
        ("movate:2026.6.11.3-47d6d65f", "47d6d65f"),  # default _build_plan shape
        ("movate:2026.6.11.3-unknown", None),  # git_short_sha() unavailable
        ("movate:9.9.9-test", None),  # operator tag, no sha suffix
        ("movate:latest", None),
        ("2026.6.11.3-abc1234", "abc1234"),  # bare tag (no repo segment)
    ],
)
def test_image_tag_sha_extraction(image_tag: str, sha: str | None) -> None:
    from movate.cli.deploy import _image_tag_sha  # noqa: PLC0415

    assert _image_tag_sha(image_tag) == sha


@pytest.mark.unit
def test_wait_for_healthz_accepts_sha_suffix_despite_counter_drift(monkeypatch) -> None:
    """The live failure mode: the image reports a CalVer whose per-day
    counter differs from the locally expected one, but its ``+g<sha>`` local
    segment matches the deployed commit → the wait returns instead of 124."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "2026.6.11.8+g47d6d65f.dirty"})

    _make_healthz_client_factory(httpx.MockTransport(handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)

    asyncio.run(
        _wait_for_healthz(
            url="https://example.test",
            expected_version="2026.6.11.3",
            timeout=30.0,
            expected_sha="47d6d65f",
        )
    )


@pytest.mark.unit
def test_run_acr_build_returns_injected_build_version(mock_subprocess, monkeypatch) -> None:
    """The build helper must hand back the exact MOVATE_BUILD_VERSION it
    injected — that string (not movate.__version__) is what the new image's
    /healthz reports, so it's what the wait keys on."""
    from movate.cli.deploy import _run_acr_build  # noqa: PLC0415

    monkeypatch.setattr("movate.cli.deploy._resolve_build_version", lambda: "2026.6.11.8")
    plan = _build_plan(
        target_name="prod",
        target_cfg=_full_target(),
        image_tag="movate:2026.6.11.3-47d6d65f",
        skip_build=False,
        only=None,
    )
    returned = _run_acr_build(plan)
    assert returned == "2026.6.11.8"
    build_cmd = next(c for c in mock_subprocess if c[:3] == ["az", "acr", "build"])
    assert "MOVATE_BUILD_VERSION=2026.6.11.8" in build_cmd


@pytest.mark.unit
def test_cli_deploy_waits_on_built_version_not_installed_metadata(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """End-to-end wiring: after a build, the /healthz wait must receive the
    injected build version (recomputed from HEAD), NOT plan.version
    (movate.__version__, frozen at the last `uv sync`) — the divergence that
    made every deploy exit 124."""
    monkeypatch.setattr("movate.cli.deploy._resolve_build_version", lambda: "2026.6.11.8")
    captured: dict[str, Any] = {}

    async def fake_wait(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("movate.cli.deploy._wait_for_healthz", fake_wait)
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--image-tag", "movate:2026.6.11.3-47d6d65f"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert captured["expected_version"] == "2026.6.11.8"
    assert captured["expected_sha"] == "47d6d65f"


# ---------------------------------------------------------------------------
# _diagnose_failed_revision + timeout-branch root-cause surfacing (item 87)
# ---------------------------------------------------------------------------


def _flat(text: str) -> str:
    """Collapse all runs of whitespace to single spaces.

    The deploy console (rich) hard-wraps long lines at the terminal width,
    so substrings like ``rollout may still be in progress`` can land split
    across a newline in captured output. Flattening makes assertions robust
    to wrapping without weakening them."""
    return " ".join(text.split())


def _patch_revision_az(monkeypatch, *, returncode: int, stdout: str) -> None:
    """Patch deploy.py's ``subprocess.run`` (used by the revision diagnosis)
    and make ``az`` resolvable so the helper actually shells out."""

    def fake_run(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", fake_run)
    monkeypatch.setattr(
        "movate.cli.deploy.shutil.which",
        lambda name: "/usr/bin/az" if name == "az" else None,
    )


@pytest.mark.unit
def test_diagnose_failed_revision_surfaces_activation_failed(monkeypatch) -> None:
    """A revision in ``ActivationFailed`` → a cause string naming it."""
    blob = json.dumps(
        {
            "name": "movate-prod-api--abc123",
            "provisioningState": "ActivationFailed",
            "runningState": "Stopped",
            "healthState": "Unhealthy",
        }
    )
    _patch_revision_az(monkeypatch, returncode=0, stdout=blob)

    cause = _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
    assert cause is not None
    assert "movate-prod-api--abc123" in cause
    assert "ActivationFailed" in cause


@pytest.mark.unit
def test_diagnose_failed_revision_surfaces_crash_running_state(monkeypatch) -> None:
    """Provisioned OK but the container won't stay up (crash/OOM) → cause."""
    blob = json.dumps(
        {
            "name": "movate-prod-api--def456",
            "provisioningState": "Succeeded",
            "runningState": "Degraded",
            "healthState": "Unhealthy",
        }
    )
    _patch_revision_az(monkeypatch, returncode=0, stdout=blob)

    cause = _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
    assert cause is not None
    assert "Degraded" in cause


@pytest.mark.unit
def test_diagnose_failed_revision_returns_none_when_healthy(monkeypatch) -> None:
    """A healthy, running, succeeded revision → None (caller falls back to the
    generic in-progress message)."""
    blob = json.dumps(
        {
            "name": "movate-prod-api--ok000",
            "provisioningState": "Succeeded",
            "runningState": "Running",
            "healthState": "Healthy",
        }
    )
    _patch_revision_az(monkeypatch, returncode=0, stdout=blob)

    assert (
        _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
        is None
    )


@pytest.mark.unit
def test_diagnose_failed_revision_degrades_on_az_error(monkeypatch) -> None:
    """Non-zero ``az`` exit → None (never raise)."""
    _patch_revision_az(monkeypatch, returncode=1, stdout="")
    assert (
        _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
        is None
    )


@pytest.mark.unit
def test_diagnose_failed_revision_degrades_on_garbage_json(monkeypatch) -> None:
    """Non-JSON / garbage output → None (never raise)."""
    _patch_revision_az(monkeypatch, returncode=0, stdout="not json at all {{{")
    assert (
        _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
        is None
    )


@pytest.mark.unit
def test_diagnose_failed_revision_returns_none_when_az_missing(monkeypatch) -> None:
    """``az`` not on PATH → None, and no subprocess attempted."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: None)

    def explode(*args, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("subprocess.run should not be called when az is missing")

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", explode)
    assert (
        _diagnose_failed_revision(resource_group="movate-prod-rg", app_name="movate-prod-api")
        is None
    )


def _diag_plan() -> DeployPlan:
    """A minimal plan carrying the resource group + app name the diagnosis
    needs."""
    return DeployPlan(
        target_name="prod",
        subscription="00000000-0000-0000-0000-000000000000",
        resource_group="movate-prod-rg",
        acr_name="movateprodacr",
        env="prod",
        image_tag="movate:0.5.0-abc1234",
        skip_build=False,
        apps_to_update=["movate-prod-api", "movate-prod-worker"],
        version="0.5.0",
    )


@pytest.mark.unit
def test_wait_for_healthz_timeout_surfaces_revision_root_cause(monkeypatch, capsys) -> None:
    """When the gate times out AND the latest revision is ActivationFailed,
    the root cause + old-revision note are surfaced — and exit is still 124."""

    def stale_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.4.9"})

    _make_healthz_client_factory(httpx.MockTransport(stale_handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)
    blob = json.dumps(
        {
            "name": "movate-prod-api--abc123",
            "provisioningState": "ActivationFailed",
            "runningState": "Stopped",
            "healthState": "Unhealthy",
        }
    )
    _patch_revision_az(monkeypatch, returncode=0, stdout=blob)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(
            _wait_for_healthz(
                url="https://example.test",
                expected_version="0.5.0",
                timeout=0.05,
                plan=_diag_plan(),
            )
        )
    assert exc_info.value.code == 124
    out = _flat(capsys.readouterr().err)
    assert "ActivationFailed" in out
    assert "movate-prod-api--abc123" in out
    assert "OLD revision is still serving" in out
    # The logs hint reuses the existing wording style.
    assert "az containerapp logs show" in out
    # The generic fallback line must NOT be shown when we have a cause.
    assert "rollout may still be in progress" not in out


@pytest.mark.unit
def test_wait_for_healthz_timeout_generic_when_revision_healthy(monkeypatch, capsys) -> None:
    """Timeout but the latest revision looks healthy/in-progress → the generic
    timeout message, exit 124, no spurious root cause."""

    def stale_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.4.9"})

    _make_healthz_client_factory(httpx.MockTransport(stale_handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)
    blob = json.dumps(
        {
            "name": "movate-prod-api--ok000",
            "provisioningState": "Succeeded",
            "runningState": "Running",
            "healthState": "Healthy",
        }
    )
    _patch_revision_az(monkeypatch, returncode=0, stdout=blob)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(
            _wait_for_healthz(
                url="https://example.test",
                expected_version="0.5.0",
                timeout=0.05,
                plan=_diag_plan(),
            )
        )
    assert exc_info.value.code == 124
    out = _flat(capsys.readouterr().err)
    assert "rollout may still be in progress" in out
    assert "ActivationFailed" not in out


@pytest.mark.unit
def test_wait_for_healthz_timeout_generic_when_az_errors(monkeypatch, capsys) -> None:
    """Timeout and the diagnosis az call errors/garbles → degrade to the
    generic message (no crash), exit 124."""

    def stale_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "version": "0.4.9"})

    _make_healthz_client_factory(httpx.MockTransport(stale_handler), monkeypatch)
    monkeypatch.setattr("movate.cli.deploy.asyncio.sleep", _no_sleep)
    _patch_revision_az(monkeypatch, returncode=2, stdout="garbage {{{")

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(
            _wait_for_healthz(
                url="https://example.test",
                expected_version="0.5.0",
                timeout=0.05,
                plan=_diag_plan(),
            )
        )
    assert exc_info.value.code == 124
    out = _flat(capsys.readouterr().err)
    assert "rollout may still be in progress" in out


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
    # Build + two container-app updates still happened. (The runtime path also
    # runs a pgvector pre-flight `az postgres flexible-server ...` discovery
    # call, so assert on the specific calls rather than an exact total.)
    az_cmds = [c for c in mock_subprocess if c and c[0] == "az"]
    assert any(c[:3] == ["az", "acr", "build"] for c in az_cmds)
    assert sum(1 for c in az_cmds if c[:2] == ["az", "containerapp"] and "update" in c) == 2


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

    async def _instant_healthz(*, url, expected_version, timeout, plan=None, expected_sha=None):
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

    outcome = _upload_one_agent_bundle(
        client=client,
        base_url="https://rt.example",
        headers={"Authorization": "Bearer x"},
        agent_dir=agent_dir,
        project_root=tmp_path,
    )
    assert outcome.ok  # 201 → success

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

    outcome = _upload_one_agent_bundle(
        client=client,
        base_url="https://rt.example",
        headers={"Authorization": "Bearer x"},
        agent_dir=agent_dir,
        project_root=tmp_path,
    )
    assert outcome.ok

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


# ---------------------------------------------------------------------------
# Post-deploy next-steps: `mdk run` primary + a CORRECT raw curl
#
# Regression: the emitted curl POSTed {"agent", "input"} to /run and used a
# bearer that silently expands to empty when no key is saved — so a copy-paste
# hit auth_required, then 422. The block now (a) leads with `mdk run`, (b)
# emits the RunSubmission {kind, target, input} body, and (c) prints a key
# bootstrap hint instead of a blind curl when no bearer resolves.
# ---------------------------------------------------------------------------


def _hermetic_creds(monkeypatch, tmp_path: Path) -> None:
    """Point the credentials store at an empty tmp file + force the file
    backend, so key-resolution tests never read the developer's real
    ``~/.movate/credentials`` (or their OS keychain)."""
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / "credentials"))
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")


def _collapsed(text: str) -> str:
    """Whitespace-collapsed view of rendered output.

    Rich word-wraps markup lines at the (non-tty) 80-col default, turning a
    single logical line into several. Collapsing runs of whitespace back to
    single spaces lets substring assertions ignore where the wrap landed."""
    return " ".join(text.split())


def _extract_heredoc_bodies(rendered: str) -> list[dict]:
    """Pull every ``--data-binary @- <<'JSON' … JSON`` body out of the
    rendered next-steps output and JSON-parse it.

    The curl is emitted with ``soft_wrap=True`` so the body bytes survive
    verbatim — we can slice each heredoc and feed it straight to a parser
    (and, in turn, to ``RunSubmission``)."""
    bodies: list[dict] = []
    for part in rendered.split("<<'JSON'")[1:]:
        after_curl = part.split("\n", 1)[1]
        body_lines: list[str] = []
        for line in after_curl.splitlines():
            if line.strip() == "JSON":
                break
            body_lines.append(line)
        bodies.append(json.loads("\n".join(body_lines)))
    return bodies


@pytest.mark.unit
def test_next_steps_leads_with_mdk_run(capsys, tmp_path: Path, monkeypatch) -> None:
    """The recommended first step is `mdk run <agent> "<input>" --target` —
    it handles auth + the correct route/body for the operator."""
    monkeypatch.setenv("MOVATE_PROD_KEY", "mvt_prod_demotena_k_secret")
    _hermetic_creds(monkeypatch, tmp_path)

    _render_post_deploy_next_steps(
        target_name="prod",
        uploaded=["weather"],
        project_root=tmp_path,
        base_url="https://movate-prod-api.example.azurecontainerapps.io",
        key_env="MOVATE_PROD_KEY",
    )
    out = _collapsed(capsys.readouterr().err)
    assert "mdk run weather" in out
    assert "--target prod" in out


@pytest.mark.unit
def test_next_steps_curl_body_is_run_submission_shape(capsys, tmp_path: Path, monkeypatch) -> None:
    """When a key resolves, a raw curl IS emitted — but with the
    ``{kind, target, input}`` RunSubmission body, never the legacy
    ``{agent, input}`` shape that 422s."""
    monkeypatch.setenv("MOVATE_PROD_KEY", "mvt_prod_demotena_k_secret")
    _hermetic_creds(monkeypatch, tmp_path)

    _render_post_deploy_next_steps(
        target_name="prod",
        uploaded=["weather"],
        project_root=tmp_path,
        base_url="https://movate-prod-api.example.azurecontainerapps.io",
        key_env="MOVATE_PROD_KEY",
    )
    out = capsys.readouterr().err

    # The legacy footgun shape must be gone; the wire shape must be present.
    assert '"agent":' not in out
    assert '"kind": "agent"' in out
    assert '"target": "weather"' in out

    # And the emitted bytes actually parse against the real wire model.
    bodies = _extract_heredoc_bodies(out)
    assert len(bodies) == 1
    submission = RunSubmission.model_validate(bodies[0])
    assert submission.kind == "agent"
    assert submission.target == "weather"
    assert isinstance(submission.input, dict)


@pytest.mark.unit
def test_legacy_agent_input_body_is_rejected_by_run_submission() -> None:
    """Guards the regression: the OLD ``{agent, input}`` body the deploy
    used to emit is rejected by RunSubmission (extra `agent`, missing
    `kind`/`target`), confirming why the copy-pasted curl 422'd."""
    with pytest.raises(pydantic.ValidationError):
        RunSubmission.model_validate({"agent": "weather", "input": {}})


@pytest.mark.unit
def test_next_steps_prints_bootstrap_hint_when_no_key(capsys, tmp_path: Path, monkeypatch) -> None:
    """No env var AND no credentials entry → print the key-bootstrap step
    instead of a curl that would silently 401."""
    monkeypatch.delenv("MOVATE_PROD_KEY", raising=False)
    _hermetic_creds(monkeypatch, tmp_path)  # tmp creds file doesn't exist → empty store

    _render_post_deploy_next_steps(
        target_name="prod",
        uploaded=["weather"],
        project_root=tmp_path,
        base_url="https://movate-prod-api.example.azurecontainerapps.io",
        key_env="MOVATE_PROD_KEY",
    )
    raw = capsys.readouterr().err
    out = _collapsed(raw)

    # `mdk run` is still surfaced (it can pull/mint a key itself), but NO
    # raw curl heredoc — the operator gets the bootstrap commands instead.
    assert "mdk run weather" in out
    assert "<<'JSON'" not in raw
    assert "auth pull-runtime-key prod" in out
    assert "auth refresh-runtime-key prod" in out


@pytest.mark.unit
def test_runtime_key_is_resolvable_via_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOVATE_PROD_KEY", "mvt_prod_demotena_k_secret")
    _hermetic_creds(monkeypatch, tmp_path)
    assert _runtime_key_is_resolvable("MOVATE_PROD_KEY") is True


@pytest.mark.unit
def test_runtime_key_is_resolvable_via_credentials_file(monkeypatch, tmp_path: Path) -> None:
    """A blank env var falls back to the saved credentials entry."""
    monkeypatch.delenv("MOVATE_PROD_KEY", raising=False)
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")
    creds = tmp_path / "credentials"
    creds.write_text("MOVATE_PROD_KEY=mvt_prod_demotena_k_secret\n")
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(creds))
    assert _runtime_key_is_resolvable("MOVATE_PROD_KEY") is True


@pytest.mark.unit
def test_runtime_key_unresolvable_when_unset_and_unsaved(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MOVATE_PROD_KEY", raising=False)
    _hermetic_creds(monkeypatch, tmp_path)  # empty tmp store
    assert _runtime_key_is_resolvable("MOVATE_PROD_KEY") is False


@pytest.mark.unit
def test_bearer_bootstrap_hint_names_both_recovery_commands(capsys) -> None:
    """The standalone hint points at both bootstrap paths so the operator
    can pick whichever fits (Key Vault pull vs. mint-in-pod)."""
    _render_bearer_bootstrap_hint(target_name="dev", key_env="MDK_DEV_KEY")
    out = _collapsed(capsys.readouterr().err)
    assert "auth pull-runtime-key dev --keyvault" in out
    assert "auth refresh-runtime-key dev" in out
    assert "MDK_DEV_KEY" in out


# ---------------------------------------------------------------------------
# Shell-shadow warning after a deploy mints + saves a fresh runtime key.
#
# Scenario: `mdk deploy --target dev` mints a fresh bearer + saves it to
# ~/.movate/credentials, but the operator has a STALE MDK_DEV_KEY still
# exported in their shell. Shell wins over the file (autoload never
# clobbers an export), so the next `mdk run` would send the stale key and
# 401. The deploy now warns at save time to `unset` the shell var.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_warn_fires_when_shell_value_differs_from_minted(capsys, monkeypatch, tmp_path) -> None:
    # Hermetic store (no matching saved value) so `key_source` resolves the
    # env var to "shell" — the gate the consolidated helper applies (ADR 022).
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_STALE_shell_value")
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_FRESH_minted")
    out = _collapsed(capsys.readouterr().err)
    assert "unset MDK_DEV_KEY" in out
    assert "OVERRIDE" in out
    # Points at the one-command fix that auto-comments the stale export.
    assert "mdk fix unshadow-runtime-keys --apply" in out


@pytest.mark.unit
def test_warn_silent_when_shell_value_matches_minted(capsys, monkeypatch, tmp_path) -> None:
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_same_value")
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_same_value")
    assert _collapsed(capsys.readouterr().err) == ""


@pytest.mark.unit
def test_warn_silent_when_shell_unset(capsys, monkeypatch, tmp_path) -> None:
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    _warn_if_shell_shadows_runtime_key(key_env="MDK_DEV_KEY", fresh_key="mvt_dev_FRESH_minted")
    assert _collapsed(capsys.readouterr().err) == ""


def _fake_target(*, key_env: str = "MDK_DEV_KEY", azure_keyvault: str | None = None) -> Any:
    """A minimal stand-in for a resolved ``TargetConfig``.

    `_attempt_auto_recovery` only reads ``.key_env`` + ``.azure_keyvault``
    (via :func:`_resolve_keyvault_name`); ``base_url`` is passed separately.
    A namespace keeps these recovery-logic tests free of YAML/config I/O."""
    return SimpleNamespace(
        key_env=key_env,
        azure_keyvault=azure_keyvault,
        url="https://movate-dev-api.example.azurecontainerapps.io",
        azure_resource_group="movate-dev-rg",
        azure_env="dev",
    )


def _always_verifies(monkeypatch, *, ok: bool = True, reason: str = "") -> None:
    """Pin `_attempt_auto_recovery`'s round-trip verification result.

    Lets the recovery-LOGIC tests (keep vs. restore) decide the verify
    outcome without standing up an HTTP transport — the round-trip itself
    is covered separately by the `_verify_bearer_roundtrip` unit tests."""
    monkeypatch.setattr(
        "movate.cli.deploy._verify_bearer_roundtrip",
        lambda *, base_url, key: (ok, reason),
    )


def _no_kv_discovery(monkeypatch) -> None:
    """Pin resource-group Key Vault discovery to a miss.

    The mint-path recovery tests model a target with NO discoverable vault
    (so recovery falls through to the in-pod mint). Stubbing the discovery
    helper keeps them hermetic — without it, `_attempt_auto_recovery` would
    shell out to `az keyvault list` against the (fake) resource group."""
    monkeypatch.setattr(
        "movate.cli.deploy._discover_keyvault_in_resource_group",
        lambda target_cfg: None,
    )


@pytest.mark.unit
def test_auto_recovery_warns_when_shell_shadows_minted_key(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """End-to-end through `_attempt_auto_recovery`: minting a fresh key
    while a different MDK_DEV_KEY is exported surfaces the unset warning."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "movate.cli.auth.refresh_runtime_key_inline",
        lambda target_name, *, scopes=None: ("mvt_dev_FRESH_minted", "MDK_DEV_KEY"),
    )
    _always_verifies(monkeypatch)
    _no_kv_discovery(monkeypatch)
    monkeypatch.setenv("MDK_DEV_KEY", "mvt_dev_STALE_shell_value")

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    assert new_key == "mvt_dev_FRESH_minted"
    out = _collapsed(capsys.readouterr().err)
    assert "bearer key ready" in out
    assert "unset MDK_DEV_KEY" in out


@pytest.mark.unit
def test_auto_recovery_no_warn_when_shell_unset(capsys, monkeypatch, tmp_path: Path) -> None:
    """The common recovery path — shell var empty, key minted into the file
    — must NOT print the shadow warning (there is nothing to override)."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "movate.cli.auth.refresh_runtime_key_inline",
        lambda target_name, *, scopes=None: ("mvt_dev_FRESH_minted", "MDK_DEV_KEY"),
    )
    _always_verifies(monkeypatch)
    _no_kv_discovery(monkeypatch)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    out = _collapsed(capsys.readouterr().err)
    assert "bearer key ready" in out
    assert "unset MDK_DEV_KEY" not in out
    assert "OVERRIDE" not in out


# ---------------------------------------------------------------------------
# Admin-capability bearer verification — the heart of the deploy-auth fix.
#
# Regression 1: deploy used to mint a fresh `demotenant` key inside the pod
# and SAVE it as the runtime bearer, declaring "✓ bearer key ready" — but
# an in-pod mint lands in a store the serving replica may never read
# (non-durable SQLite / multi-replica), so the key 401'd on the very next
# call and CLOBBERED whatever working key the operator already had.
#
# Regression 2 (the live 403): even when the minted key DID authenticate, it
# defaulted to read,run,eval — so the read-only `GET /api/v1/agents` verify
# said "✓ verified", the key got saved, and then every agent UPLOAD 403'd
# (`missing required scope(s): admin`). The fix: mint fleet-admin, verify the
# ADMIN capability the deploy needs (GET /api/v1/auth/keys, an admin-scoped
# read) BEFORE keeping the key, prefer the fleet-admin bootstrap key from Key
# Vault, and never overwrite a previously-working saved key with a 401/403 one.
# ---------------------------------------------------------------------------


def _verify_transport(monkeypatch, handler) -> None:
    """Pin `movate.cli.deploy`'s `httpx.Client` to a MockTransport.

    `_verify_bearer_roundtrip` does `httpx.Client(...)` off the module's
    `httpx`, so this controls the round-trip's response without real
    network."""
    transport = httpx.MockTransport(handler)

    class _MockClient(httpx.Client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.pop("transport", None)
            super().__init__(*args, transport=transport, **kwargs)

    monkeypatch.setattr("movate.cli.deploy.httpx.Client", _MockClient)


@pytest.mark.unit
def test_verify_bearer_roundtrip_true_on_2xx(monkeypatch) -> None:
    _verify_transport(monkeypatch, lambda req: httpx.Response(200, json={"agents": []}))
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is True
    assert reason == ""


@pytest.mark.unit
def test_verify_bearer_roundtrip_false_on_401(monkeypatch) -> None:
    _verify_transport(monkeypatch, lambda req: httpx.Response(401, json={"detail": "nope"}))
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is False
    assert reason == "HTTP 401"


@pytest.mark.unit
def test_verify_bearer_roundtrip_rejects_read_only_key_on_403(monkeypatch) -> None:
    """THE live regression: a key that authenticates but lacks `admin` (the
    default read,run,eval in-pod mint) 403s on the admin-scoped probe. Verify
    must REJECT it so it is never saved/announced as deploy-ready — otherwise
    the very next agent UPLOAD (which needs `admin`) 403s."""
    _verify_transport(
        monkeypatch,
        lambda req: httpx.Response(403, json={"detail": "missing required scope(s): admin"}),
    )
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_read_only")
    assert ok is False
    assert "403" in reason
    assert "admin" in reason


@pytest.mark.unit
def test_verify_bearer_roundtrip_probes_admin_scoped_endpoint(monkeypatch) -> None:
    """Verify must probe the ADMIN-scoped endpoint (`GET /api/v1/auth/keys`),
    not the read-scoped `GET /api/v1/agents` — only the former proves the
    bearer can do what the deploy needs (admin uploads)."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, json={"keys": [], "count": 0})

    _verify_transport(monkeypatch, handler)
    _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_live_admin")
    assert seen["path"] == "/api/v1/auth/keys"


@pytest.mark.unit
def test_verify_bearer_roundtrip_false_on_transport_error(monkeypatch) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    _verify_transport(monkeypatch, boom)
    ok, reason = _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_x")
    assert ok is False
    assert "unreachable" in reason


@pytest.mark.unit
def test_verify_bearer_roundtrip_sends_candidate_as_bearer(monkeypatch) -> None:
    """The round-trip must present the CANDIDATE key — not whatever is in the
    environment — so it actually proves that key authenticates."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization", "")
        return httpx.Response(200, json={"agents": []})

    _verify_transport(monkeypatch, handler)
    _verify_bearer_roundtrip(base_url="https://dev.example.com", key="mvt_live_candidate")
    assert seen["auth"] == "Bearer mvt_live_candidate"


@pytest.mark.unit
def test_auto_recovery_keeps_and_saves_verified_minted_key(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """Happy mint path: candidate verifies (2xx) → it's saved + declared
    ready, and the success line says it was verified against the runtime."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    monkeypatch.setattr(
        "movate.cli.auth.refresh_runtime_key_inline",
        lambda target_name, *, scopes=None: ("mvt_live_demotena_FRESH_secret", "MDK_DEV_KEY"),
    )
    _always_verifies(monkeypatch, ok=True)
    _no_kv_discovery(monkeypatch)

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    assert new_key == "mvt_live_demotena_FRESH_secret"
    assert CredentialsStore().get("MDK_DEV_KEY") == "mvt_live_demotena_FRESH_secret"
    out = _collapsed(capsys.readouterr().err)
    assert "bearer key ready" in out
    assert "verified against the runtime" in out
    assert "minted in-pod" in out


@pytest.mark.unit
def test_auto_recovery_mint_requests_admin_capable_scope(monkeypatch, tmp_path: Path) -> None:
    """The in-pod mint MUST request an admin-capable grant (fleet-admin/admin)
    — the deploy bearer performs admin uploads (POST/PUT /api/v1/agents). A
    default-scoped (read,run,eval) key authenticates but 403s on upload, which
    is the live regression this fix closes."""
    from movate.core.auth import SCOPE_ADMIN, SCOPE_FLEET_ADMIN  # noqa: PLC0415

    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    captured: dict[str, Any] = {}

    def fake_refresh(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        captured["scopes"] = list(scopes) if scopes is not None else None
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_FLEET_secret")
        return "mvt_live_demotena_FLEET_secret", "MDK_DEV_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)
    _always_verifies(monkeypatch, ok=True)
    _no_kv_discovery(monkeypatch)

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    assert new_key == "mvt_live_demotena_FLEET_secret"
    # The mint was asked for an admin-capable scope, not the legacy default.
    assert captured["scopes"] is not None
    assert SCOPE_FLEET_ADMIN in captured["scopes"] or SCOPE_ADMIN in captured["scopes"]


@pytest.mark.unit
def test_auto_recovery_under_scoped_403_key_does_not_clobber_prior(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """A recovered key that authenticates but is UNDER-SCOPED (403 on the
    admin probe — the live regression) must be rejected: not saved, not
    announced ready, and the prior working key left intact."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    # Operator already has a working admin-capable bearer saved.
    CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_PRIOR_admin")

    def fake_refresh_saves_under_scoped(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        # Mirror the real helper saving before return so we prove a rollback.
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_READONLY_minted")
        return "mvt_live_demotena_READONLY_minted", "MDK_DEV_KEY"

    monkeypatch.setattr(
        "movate.cli.auth.refresh_runtime_key_inline", fake_refresh_saves_under_scoped
    )
    # 403: authenticated but lacks admin — exactly the upload-needs-admin path.
    _always_verifies(
        monkeypatch, ok=False, reason="HTTP 403 (key lacks admin scope; uploads need admin)"
    )
    _no_kv_discovery(monkeypatch)

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    assert new_key is None
    # Prior admin key preserved — NOT clobbered by the under-scoped recovery key.
    assert CredentialsStore().get("MDK_DEV_KEY") == "mvt_live_demotena_PRIOR_admin"
    out = _collapsed(capsys.readouterr().err)
    assert "rejected by the runtime" in out
    assert "NOT saving it" in out
    assert "Kept your previously-saved" in out


@pytest.mark.unit
def test_auto_recovery_discovers_keyvault_in_resource_group_and_pulls(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """When the target names NO vault but one is DISCOVERABLE in the resource
    group, recovery pulls the fleet-admin bootstrap key (guaranteed-trusted +
    admin-capable) instead of falling straight to an in-pod mint."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    monkeypatch.setattr(
        "movate.cli.deploy._discover_keyvault_in_resource_group",
        lambda target_cfg: "movate-dev-kv-mvt",
    )

    def fake_pull(target: str, *, keyvault: str, secret_name: str = "bootstrap-api-key"):
        assert keyvault == "movate-dev-kv-mvt"
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_BOOT_secret")
        return "mvt_live_demotena_BOOT_secret", "MDK_DEV_KEY"

    def fail_if_minted(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        raise AssertionError("must not mint when a vault was discovered")

    monkeypatch.setattr("movate.cli.auth.pull_runtime_key_inline", fake_pull)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fail_if_minted)
    _always_verifies(monkeypatch, ok=True)

    new_key = _attempt_auto_recovery(
        target_name="dev",
        base_url="https://dev.example.com",
        target_cfg=_fake_target(),  # no azure_keyvault set
    )

    assert new_key == "mvt_live_demotena_BOOT_secret"
    out = _collapsed(capsys.readouterr().err)
    assert "Discovered Key Vault" in out
    assert "pulled from Key Vault" in out


@pytest.mark.unit
def test_auto_recovery_rejected_key_does_not_clobber_prior_working_key(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """THE headline regression: a minted key that 401s must NOT overwrite a
    previously-working saved key, and must point at `pull-runtime-key`."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)
    # Operator already has a working bearer saved.
    CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_PRIOR_working")

    # The real refresh_runtime_key_inline SAVES before returning; mirror that
    # so the test proves the bad value is rolled back, not merely never-set.
    def fake_refresh_saves_bad(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_BAD_minted")
        return "mvt_live_demotena_BAD_minted", "MDK_DEV_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh_saves_bad)
    _always_verifies(monkeypatch, ok=False, reason="HTTP 401")
    _no_kv_discovery(monkeypatch)

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    # Recovery reports failure...
    assert new_key is None
    # ...the previously-working key is intact (NOT clobbered by the 401 key)...
    assert CredentialsStore().get("MDK_DEV_KEY") == "mvt_live_demotena_PRIOR_working"
    out = _collapsed(capsys.readouterr().err)
    assert "rejected by the runtime" in out
    assert "NOT saving it" in out
    assert "Kept your previously-saved" in out
    assert "pull-runtime-key dev" in out


@pytest.mark.unit
def test_auto_recovery_rejected_key_with_no_prior_is_deleted(monkeypatch, tmp_path: Path) -> None:
    """When there's no prior saved key and the candidate 401s, the bad
    candidate is removed from the store rather than left behind to 401 again."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    def fake_refresh_saves_bad(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_BAD_minted")
        return "mvt_live_demotena_BAD_minted", "MDK_DEV_KEY"

    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh_saves_bad)
    _always_verifies(monkeypatch, ok=False, reason="HTTP 401")
    _no_kv_discovery(monkeypatch)

    new_key = _attempt_auto_recovery(
        target_name="dev", base_url="https://dev.example.com", target_cfg=_fake_target()
    )

    assert new_key is None
    assert CredentialsStore().get("MDK_DEV_KEY") is None


@pytest.mark.unit
def test_auto_recovery_prefers_keyvault_pull_over_mint_when_known(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """When the target names a Key Vault, recovery PULLS the guaranteed-
    trusted bootstrap key and never shells out to mint a fresh one."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    def fake_pull(target: str, *, keyvault: str, secret_name: str = "bootstrap-api-key"):
        assert keyvault == "movate-dev-kv-mvt"
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_BOOT_secret")
        return "mvt_live_demotena_BOOT_secret", "MDK_DEV_KEY"

    def fail_if_minted(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        raise AssertionError("must not mint when a Key Vault pull is available")

    monkeypatch.setattr("movate.cli.auth.pull_runtime_key_inline", fake_pull)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fail_if_minted)
    _always_verifies(monkeypatch, ok=True)

    new_key = _attempt_auto_recovery(
        target_name="dev",
        base_url="https://dev.example.com",
        target_cfg=_fake_target(azure_keyvault="movate-dev-kv-mvt"),
    )

    assert new_key == "mvt_live_demotena_BOOT_secret"
    assert CredentialsStore().get("MDK_DEV_KEY") == "mvt_live_demotena_BOOT_secret"
    out = _collapsed(capsys.readouterr().err)
    assert "pulled from Key Vault" in out


@pytest.mark.unit
def test_auto_recovery_falls_back_to_mint_when_keyvault_pull_fails(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    """A failed Key Vault pull (RBAC, missing secret) falls back to minting
    in-pod — still round-trip verified before being kept."""
    _hermetic_creds(monkeypatch, tmp_path)
    monkeypatch.delenv("MDK_DEV_KEY", raising=False)

    def fake_pull_fails(target: str, *, keyvault: str, secret_name: str = "bootstrap-api-key"):
        raise PullRuntimeKeyError("SecretNotFound")

    def fake_refresh(target_name: str, *, scopes: Any = None) -> tuple[str, str]:
        CredentialsStore().set("MDK_DEV_KEY", "mvt_live_demotena_FRESH_secret")
        return "mvt_live_demotena_FRESH_secret", "MDK_DEV_KEY"

    monkeypatch.setattr("movate.cli.auth.pull_runtime_key_inline", fake_pull_fails)
    monkeypatch.setattr("movate.cli.auth.refresh_runtime_key_inline", fake_refresh)
    _always_verifies(monkeypatch, ok=True)

    new_key = _attempt_auto_recovery(
        target_name="dev",
        base_url="https://dev.example.com",
        target_cfg=_fake_target(azure_keyvault="movate-dev-kv-mvt"),
    )

    assert new_key == "mvt_live_demotena_FRESH_secret"
    out = _collapsed(capsys.readouterr().err)
    assert "falling back to minting" in out
    assert "minted in-pod" in out


@pytest.mark.unit
def test_resolve_keyvault_name_reads_field_else_none() -> None:
    assert _resolve_keyvault_name(_fake_target(azure_keyvault="movate-dev-kv-mvt")) == (
        "movate-dev-kv-mvt"
    )
    assert _resolve_keyvault_name(_fake_target(azure_keyvault=None)) is None
    # The vault name is NOT derived from azure_env — must be explicit.
    assert _resolve_keyvault_name(SimpleNamespace(key_env="K", azure_env="dev")) is None


# ---------------------------------------------------------------------------
# Image-parity guard: api / worker / temporal-worker must share one image
# (catches the "feature on the api image but not the worker image" drift)
# ---------------------------------------------------------------------------


def _parity_plan(env: str = "prod") -> DeployPlan:
    """A plan whose env yields app names movate-<env>-{api,worker,...}."""
    return DeployPlan(
        target_name=env,
        subscription="00000000-0000-0000-0000-000000000000",
        resource_group=f"movate-{env}-rg",
        acr_name=f"movate{env}acr",
        env=env,
        image_tag="movate:9.9.9-abc1234",
        skip_build=False,
        apps_to_update=[f"movate-{env}-api", f"movate-{env}-worker"],
        version="9.9.9",
    )


def _image_responder(images_by_app: dict[str, str | None]):
    """Fake ``subprocess.run`` keyed off the ``--name`` arg: maps each
    Container App name to a configured image (``None`` → simulate "app not
    found" / non-zero exit, e.g. the temporal-worker when Temporal is off)."""

    def fake_run(cmd, *args, **kwargs):
        name = cmd[cmd.index("--name") + 1]
        image = images_by_app.get(name)
        if image is None:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout=image + "\n", stderr="")

    return fake_run


@pytest.mark.unit
def test_query_containerapp_image_reads_tsv_value(monkeypatch) -> None:
    """Happy path: returns the trimmed image string from the tsv query."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder({"movate-prod-api": "acr.azurecr.io/movate:9.9.9-abc1234"}),
    )
    assert (
        _query_containerapp_image(_parity_plan(), "movate-prod-api")
        == "acr.azurecr.io/movate:9.9.9-abc1234"
    )


@pytest.mark.unit
def test_query_containerapp_image_none_on_nonzero_exit(monkeypatch) -> None:
    """A non-zero ``az`` exit (app not found) degrades to None, never raises."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr("movate.cli.deploy.subprocess.run", _image_responder({}))
    assert _query_containerapp_image(_parity_plan(), "movate-prod-temporal-worker") is None


@pytest.mark.unit
def test_query_containerapp_image_none_when_az_missing(monkeypatch) -> None:
    """``az`` not on PATH → None, and no subprocess attempted."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: None)

    def explode(*args, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("subprocess.run should not be called when az is missing")

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", explode)
    assert _query_containerapp_image(_parity_plan(), "movate-prod-api") is None


@pytest.mark.unit
def test_assert_image_parity_passes_when_all_three_match(monkeypatch, capsys) -> None:
    """api + worker + temporal-worker on the same image → ✓, no raise."""
    img = "acr.azurecr.io/movate:9.9.9-abc1234"
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder(
            {
                "movate-prod-api": img,
                "movate-prod-worker": img,
                "movate-prod-temporal-worker": img,
            }
        ),
    )
    _assert_image_parity(_parity_plan())  # must not raise
    out = capsys.readouterr().err
    assert "image parity" in out
    assert "3 apps" in out


@pytest.mark.unit
def test_assert_image_parity_passes_when_temporal_worker_absent(monkeypatch) -> None:
    """Temporal disabled (temporal-worker show → non-zero) — api + worker
    still compared, and a match passes."""
    img = "acr.azurecr.io/movate:9.9.9-abc1234"
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder({"movate-prod-api": img, "movate-prod-worker": img}),
    )
    _assert_image_parity(_parity_plan())  # must not raise


@pytest.mark.unit
def test_assert_image_parity_fails_on_api_worker_drift(monkeypatch, capsys) -> None:
    """api advanced but worker left on the old tag (the `--only` footgun) →
    exit 1 with both images named."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder(
            {
                "movate-prod-api": "acr.azurecr.io/movate:9.9.9-new",
                "movate-prod-worker": "acr.azurecr.io/movate:9.9.8-old",
            }
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        _assert_image_parity(_parity_plan())
    assert exc.value.exit_code == 1
    out = capsys.readouterr().err
    assert "image-parity check failed" in out
    assert "movate:9.9.9-new" in out
    assert "movate:9.9.8-old" in out


@pytest.mark.unit
def test_assert_image_parity_fails_on_temporal_worker_drift(monkeypatch) -> None:
    """api + worker match but the temporal-worker is stale → exit 1
    (durable-workflow runtime code would silently no-op)."""
    new = "acr.azurecr.io/movate:9.9.9-new"
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder(
            {
                "movate-prod-api": new,
                "movate-prod-worker": new,
                "movate-prod-temporal-worker": "acr.azurecr.io/movate:9.9.8-old",
            }
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        _assert_image_parity(_parity_plan())
    assert exc.value.exit_code == 1


@pytest.mark.unit
def test_assert_image_parity_noop_when_only_one_app_readable(monkeypatch, capsys) -> None:
    """Fewer than two images readable (can't reason about parity) → silent
    no-op; never blocks a deploy on a read hiccup."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: "/usr/bin/az")
    monkeypatch.setattr(
        "movate.cli.deploy.subprocess.run",
        _image_responder({"movate-prod-api": "acr.azurecr.io/movate:9.9.9-only"}),
    )
    _assert_image_parity(_parity_plan())  # must not raise
    out = capsys.readouterr().err
    assert "image parity" not in out
    assert "failed" not in out


@pytest.mark.unit
def test_assert_image_parity_noop_when_az_missing(monkeypatch) -> None:
    """``az`` off PATH → every read is None → no-op, no raise."""
    monkeypatch.setattr("movate.cli.deploy.shutil.which", lambda name: None)
    _assert_image_parity(_parity_plan())  # must not raise


@pytest.mark.unit
def test_cli_deploy_runs_image_parity_check_by_default(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """A full deploy issues `az containerapp show` parity reads for api +
    worker + temporal-worker after the updates."""
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    show_calls = [c for c in mock_subprocess if c[:3] == ["az", "containerapp", "show"]]
    queried = {c[c.index("--name") + 1] for c in show_calls}
    assert queried == {
        "movate-prod-api",
        "movate-prod-worker",
        "movate-prod-temporal-worker",
    }


@pytest.mark.unit
def test_cli_deploy_skip_image_parity_check_omits_reads(deploy_env, mock_subprocess) -> None:
    """--skip-image-parity-check suppresses the post-update PARITY reads.

    A single ``az containerapp show`` may still occur — the temporal-worker
    existence probe that decides whether to roll it (independent of the parity
    flag). What must NOT happen is the parity guard reading api + worker back.
    """
    result = runner.invoke(
        cli_app,
        [
            "deploy",
            "--target",
            "prod",
            "--no-wait",
            "--skip-image-parity-check",
            "--image-tag",
            "movate:9.9.9-test",
        ],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    show_calls = [c for c in mock_subprocess if c[:3] == ["az", "containerapp", "show"]]
    # The parity guard (reads api + worker images back) is skipped; the only
    # permissible show is the temporal-worker existence probe.
    non_probe = [c for c in show_calls if not any("temporal-worker" in tok for tok in c)]
    assert non_probe == [], f"parity reads not skipped: {non_probe}"


@pytest.mark.unit
def test_cli_deploy_rolls_temporal_worker_when_present(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """A full deploy also rolls movate-<env>-temporal-worker when it exists, so
    durable-workflow runtime features land there and image parity holds."""
    # Every containerapp image reads back the SAME tag → temporal-worker
    # "exists" (probe non-None) AND the parity guard passes.
    monkeypatch.setattr(
        "movate.cli.deploy._query_containerapp_image",
        lambda plan, app_name: "movateprodacr.azurecr.io/movate:9.9.9-test",
    )
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    update_calls = [c for c in mock_subprocess if c[:3] == ["az", "containerapp", "update"]]
    updated = {
        tok
        for c in update_calls
        for tok in c
        if tok.startswith("movate-prod-") and not tok.endswith("-rg")
    }
    assert updated == {"movate-prod-api", "movate-prod-worker", "movate-prod-temporal-worker"}


@pytest.mark.unit
def test_cli_deploy_skips_temporal_worker_when_absent(
    deploy_env, mock_subprocess, monkeypatch
) -> None:
    """Non-Temporal env: the probe returns None → only api + worker roll."""
    monkeypatch.setattr(
        "movate.cli.deploy._query_containerapp_image",
        lambda plan, app_name: None,
    )
    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    update_calls = [c for c in mock_subprocess if c[:3] == ["az", "containerapp", "update"]]
    updated = {
        tok
        for c in update_calls
        for tok in c
        if tok.startswith("movate-prod-") and not tok.endswith("-rg")
    }
    assert updated == {"movate-prod-api", "movate-prod-worker"}


@pytest.mark.unit
def test_cli_deploy_fails_loudly_on_image_drift(deploy_env, monkeypatch) -> None:
    """End-to-end: drift between api and worker images aborts the deploy
    with exit 1 (the parity guard wired into the deploy flow)."""

    def drift_run(cmd, *args, **kwargs):
        # The build/update calls don't matter here; only the parity `show`
        # reads need to return diverging images.
        if cmd[:3] == ["az", "containerapp", "show"]:
            name = cmd[cmd.index("--name") + 1]
            img = "movate:NEW" if name.endswith("-api") else "movate:OLD"
            return subprocess.CompletedProcess(cmd, returncode=0, stdout=img + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    def fake_popen(cmd, *args, **kwargs):
        return _FakePopen(lines=[], returncode=0)

    monkeypatch.setattr("movate.cli.deploy.subprocess.run", drift_run)
    monkeypatch.setattr("movate.cli.deploy.subprocess.Popen", fake_popen)

    result = runner.invoke(
        cli_app,
        ["deploy", "--target", "prod", "--no-wait", "--image-tag", "movate:9.9.9-test"],
    )
    assert result.exit_code == 1, result.stdout + result.stderr
    assert "image-parity check failed" in result.stderr
