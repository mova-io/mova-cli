"""Item #95 — `mdk deploy --mode agents` offers to RUN a smoke test.

After a successful agents-mode deploy, deploy can dispatch ONE remote run
per just-deployed agent against the target so the operator confirms the
NEW behavior is live (not just that /healthz answers). This complements
the existing copy-pasteable smoke-test commands.

Testing strategy (mirrors ``test_deploy_from_project_and_yaml_schemas``):

* Scaffold a real project (``mdk init`` + ``mdk add faq``) so the agent
  has a real ``evals/dataset.jsonl`` (first input: the capital-of-France
  question).
* Stub the bearer preflight + the per-agent upload so agents-mode reaches
  its success path WITHOUT touching real HTTP.
* Patch ``movate.cli.run._dispatch_remote_agent`` (the function-local
  import inside ``_maybe_run_smoke_test``) to record calls / simulate a
  failing run — never hitting the network.
* CliRunner runs non-interactively (no TTY), so the default offer is a
  skip; ``--smoke-test`` forces the run. TTY-gated paths patch
  ``deploy._smoke_test_is_interactive`` (the single interactivity seam)
  rather than fighting CliRunner's stdin swap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from movate.cli import deploy as deploy_mod
from movate.cli.main import app

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# Scaffold: a project with one agent + a registered target, deploy stubbed
# so the agents-mode success path runs without real HTTP.
# ---------------------------------------------------------------------------


def _scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Init a project, add the `faq` agent, register the `fake` target,
    then chdir into the project. Caller still stubs upload/preflight."""
    monkeypatch.setenv("MOVATE_HOME", str(tmp_path / ".movate"))
    monkeypatch.setenv("MOVATE_CREDENTIALS_PATH", str(tmp_path / ".movate" / "credentials"))
    monkeypatch.setenv("MOVATE_CRED_BACKEND", "file")
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


def _stub_successful_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bearer preflight a pass-through and every agent/skill
    upload succeed, so agents-mode reaches its post-deploy success path
    without any real HTTP. Also pin the credentials write to a no-op."""
    monkeypatch.setattr(deploy_mod, "_preflight_bearer", lambda *, headers, **_: headers)
    monkeypatch.setattr(
        deploy_mod,
        "_upload_one_agent_bundle",
        lambda **_: deploy_mod.AgentUploadOutcome(error=None, published_version="0.1.0"),
    )
    monkeypatch.setattr(deploy_mod, "_upload_skills", lambda **_: ([], []))


def _patch_dispatch(monkeypatch: pytest.MonkeyPatch, calls: list[dict[str, Any]]) -> None:
    """Record every ``_dispatch_remote_agent`` call (the smoke run) without
    touching the network. Patched on ``movate.cli.run`` because
    ``_maybe_run_smoke_test`` imports it function-locally from there."""

    def fake_dispatch(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr("movate.cli.run._dispatch_remote_agent", fake_dispatch)


# ---------------------------------------------------------------------------
# --smoke-test triggers a remote run; gating skips it elsewhere
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_smoke_test_flag_dispatches_remote_run_with_first_dataset_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--smoke-test`` dispatches one remote run per agent, using the
    FIRST evals/dataset.jsonl row as the input."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--smoke-test"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert len(calls) == 1, "expected one smoke run for the single deployed agent"
    assert calls[0]["agent_name"] == "faq"
    assert calls[0]["target"] == "fake"
    assert calls[0]["mock"] is False
    # First dataset row's input — proves we reuse the dataset, not a placeholder.
    assert "What is the capital of France?" in calls[0]["raw"]
    assert "Smoke test:" in combined


@pytest.mark.unit
def test_no_smoke_test_flag_skips_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--no-smoke-test`` never dispatches, even though deploy succeeds."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--no-smoke-test"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [], "must not dispatch with --no-smoke-test"


@pytest.mark.unit
def test_default_non_interactive_skips_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag + non-interactive (CliRunner has no TTY) = skip the smoke
    test (and never block on a prompt)."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [], "default non-interactive must skip the smoke offer"


@pytest.mark.unit
def test_default_interactive_offer_accepted_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag + interactive (TTY) + operator confirms = dispatch."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)
    # Force the interactive gate + auto-accept the confirm.
    monkeypatch.setattr(deploy_mod, "_smoke_test_is_interactive", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: True)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(calls) == 1, "accepting the offer should dispatch one smoke run"


@pytest.mark.unit
def test_default_interactive_offer_declined_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No flag + interactive + operator declines the offer = skip."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)
    monkeypatch.setattr(deploy_mod, "_smoke_test_is_interactive", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: False)

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    assert calls == [], "declining the offer must skip the smoke run"


# ---------------------------------------------------------------------------
# Dataset present vs absent
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dataset_absent_non_interactive_skips_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No evals/dataset.jsonl + non-interactive + --smoke-test = skip THAT
    agent with a copy-pasteable `mdk run … --target … -i '<json>'` hint,
    not a dispatch."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)
    # Remove the agent's dataset so there's no real input to dispatch.
    (tmp_path / "proj" / "agents" / "faq" / "evals" / "dataset.jsonl").unlink()

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--smoke-test"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, combined
    assert calls == [], "no dataset + non-interactive must not dispatch"
    assert "no evals/dataset.jsonl" in combined
    assert "mdk run faq --target fake" in combined


@pytest.mark.unit
def test_dataset_absent_interactive_prompts_then_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dataset + interactive = prompt once for the input JSON, then
    dispatch with what the operator typed."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)
    calls: list[dict[str, Any]] = []
    _patch_dispatch(monkeypatch, calls)
    (tmp_path / "proj" / "agents" / "faq" / "evals" / "dataset.jsonl").unlink()
    monkeypatch.setattr(deploy_mod, "_smoke_test_is_interactive", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *_a, **_k: True)
    monkeypatch.setattr(typer, "prompt", lambda *_a, **_k: '{"question": "typed?"}')

    result = runner.invoke(app, ["deploy", "--target", "fake"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.stdout + result.stderr
    assert len(calls) == 1
    assert calls[0]["raw"] == '{"question": "typed?"}'


# ---------------------------------------------------------------------------
# A failing smoke run warns but never changes the deploy exit code
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_failing_smoke_run_warns_but_deploy_stays_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_dispatch_remote_agent`` signals a bad run by raising
    ``typer.Exit(1)``. The smoke wrapper catches it, warns, and the deploy
    keeps its normal success exit (0) — a flaky first inference must not
    unwind a live rollout."""
    _scaffold(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_KEY", "mvt_live_keyid_secret")
    _stub_successful_upload(monkeypatch)

    def fake_dispatch(**_: Any) -> None:
        raise typer.Exit(code=1)

    monkeypatch.setattr("movate.cli.run._dispatch_remote_agent", fake_dispatch)

    result = runner.invoke(
        app, ["deploy", "--target", "fake", "--smoke-test"], env={"COLUMNS": "200"}
    )

    combined = result.stdout + result.stderr
    assert result.exit_code == 0, "a failing smoke test must NOT fail the deploy"
    assert "did not pass" in combined
    assert "deploy itself succeeded" in combined
    # The deploy summary still reports success.
    assert "ok=true" in combined
