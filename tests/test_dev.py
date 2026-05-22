"""``mdk dev`` — unit tests for the guided authoring loop.

The watch⇄menu loop and interactive prompts aren't unit-tested (they'd
need a TTY + fiddly input mocks); the integration test_watch covers the
poll mechanics. Here we assert the pieces ``dev`` is built from:

* ``dispatch_run_once`` — runs against the scaffold under --mock, and
  fails cleanly (exit 2) on a broken agent, proving fresh reload.
* ``_compute_watched_paths`` now includes contexts.
* ``_print_output_diff`` — the unchanged/changed signal in the live loop.
* The non-interactive CLI surface prints the command sequence.

(The ``contexts:`` attach/detach helpers now live in contexts_cmd and are
tested in test_contexts_cmd.py.)
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import movate.cli.dev_cmd as dc
from movate.cli.dev_cmd import _print_output_diff
from movate.cli.main import app as cli_app
from movate.cli.watch import _compute_watched_paths, dispatch_run_once
from movate.testing import scaffold_agent

runner = CliRunner(mix_stderr=False)


# ---------------------------------------------------------------------------
# dispatch_run_once
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_run_once_clean_agent_returns_0_and_output(tmp_path: Path) -> None:
    """Scaffold runs end-to-end under --mock → exit 0 + captured output."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    rc, output = dispatch_run_once(agent_dir, '{"text": "hello"}', mock=True)
    assert rc == 0
    assert output  # non-empty captured stdout, for diffing successive runs


@pytest.mark.unit
def test_dispatch_run_once_broken_agent_returns_2_and_none(tmp_path: Path) -> None:
    """Deleting prompt.md makes load_agent fail → dispatch returns (2, None)
    and does not raise (the dev loop must survive it). Proves the run path
    reloads from disk each call rather than caching the bundle."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    (agent_dir / "prompt.md").unlink()
    rc, output = dispatch_run_once(agent_dir, '{"text": "hello"}', mock=True)
    assert rc == 2
    assert output is None


@pytest.mark.unit
def test_print_output_diff_signals_change(capsys: pytest.CaptureFixture[str]) -> None:
    # No baseline yet, or a failed run → nothing printed.
    _print_output_diff(None, "hi")
    _print_output_diff("hi", None)
    assert capsys.readouterr().err == ""

    # Unchanged → a one-line marker.
    _print_output_diff("same", "same")
    assert "unchanged" in capsys.readouterr().err

    # Changed → a diff that shows both sides.
    _print_output_diff("answer: 1", "answer: 2")
    err = capsys.readouterr().err
    assert "changed" in err
    assert "answer: 2" in err


# ---------------------------------------------------------------------------
# _compute_watched_paths now includes contexts
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_watched_paths_includes_agent_local_contexts(tmp_path: Path) -> None:
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    ctx_dir = agent_dir / "contexts"
    ctx_dir.mkdir()
    (ctx_dir / "policy.md").write_text("# policy")

    watched = _compute_watched_paths(agent_dir)
    names = {p.name for p in watched.paths}
    assert "policy.md" in names


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_cli_dev_help_renders() -> None:
    r = runner.invoke(cli_app, ["dev", "--help"], env={"COLUMNS": "200"})
    assert r.exit_code == 0
    assert "--template" in r.stdout.replace("\n", "")


@pytest.mark.unit
def test_cli_dev_non_interactive_prints_guide(tmp_path: Path) -> None:
    """CliRunner stdin is not a tty → dev prints the command sequence and
    exits 0 instead of opening a live session."""
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")
    r = runner.invoke(cli_app, ["dev", str(agent_dir)])
    assert r.exit_code == 0
    assert "mdk_dev_summary" in r.stdout
    assert "agent=demo" in r.stdout


# ---------------------------------------------------------------------------
# Actions: grounding check + test-on-deployed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_grounding_action_invokes_eval_scorecard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    dc._grounding_action(agent_dir)

    assert calls, "expected a subprocess invocation"
    assert calls[0][1] == "eval-scorecard"
    assert str(agent_dir) in calls[0]


@pytest.mark.unit
def test_test_deployed_action_runs_against_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    out = dc._test_deployed_action(agent_dir, "hello", "prod")

    assert out == "prod"  # target remembered
    assert calls and calls[0][1] == "run"
    assert "--target" in calls[0] and "prod" in calls[0]
    assert "-i" in calls[0] and "hello" in calls[0]


@pytest.mark.unit
def test_test_deployed_action_skips_without_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No target and a non-interactive _ensure_target → skip, no subprocess."""

    calls: list[list[str]] = []
    monkeypatch.setattr(dc.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(dc, "_ensure_target", lambda target, *, purpose: None)
    agent_dir = scaffold_agent(tmp_path / "demo", name="demo")

    assert dc._test_deployed_action(agent_dir, "hello", None) is None
    assert calls == []
